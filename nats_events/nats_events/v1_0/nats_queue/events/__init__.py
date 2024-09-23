"""ACA-Py Events to NATS."""

import asyncio
import base64
import logging
import os
import re
import time
from string import Template
from typing import Any, Optional

import orjson
from aries_cloudagent.config.injection_context import InjectionContext
from aries_cloudagent.core.event_bus import Event, EventBus, EventWithMetadata
from aries_cloudagent.core.profile import Profile
from aries_cloudagent.core.util import SHUTDOWN_EVENT_PATTERN, STARTUP_EVENT_PATTERN
from aries_cloudagent.transport.error import TransportError
from aries_cloudagent.transport.outbound.message import OutboundMessage
from nats.aio.client import Client as NATS
from nats.aio.errors import ErrConnectionClosed, ErrNoServers, ErrTimeout
from nats.js import JetStreamContext

from ..config import EventConfig, OutboundConfig, get_config

LOGGER = logging.getLogger(__name__)

NATS_CREDS_FILE = os.getenv("NATS_CREDS_FILE")


async def setup(context: InjectionContext):
    """Setup the plugin."""
    LOGGER.info("> Setting up NATS plugin")
    config = get_config(context.settings).event or EventConfig.default()

    bus = context.inject(EventBus)
    if not bus:
        raise ValueError("EventBus missing in context")

    for event in config.event_topic_maps.keys():
        LOGGER.info("subscribing to event: %s", event)
        bus.subscribe(re.compile(event), handle_event)

    bus.subscribe(STARTUP_EVENT_PATTERN, on_startup)
    bus.subscribe(SHUTDOWN_EVENT_PATTERN, on_shutdown)
    LOGGER.info("< Successfully set up NATS plugin.")


RECORD_RE = re.compile(r"acapy::record::([^:]*)(?:::(.*))?")
WEBHOOK_RE = re.compile(r"acapy::webhook::{.*}")


async def nats_jetstream_setup(profile: Profile, event: Event) -> JetStreamContext:
    """Connect, setup and return the NATS instance."""
    connection_url = (get_config(profile.settings).connection).connection_url
    LOGGER.info("Connecting to NATS url: %s", connection_url)
    nats = NATS()
    connect_kwargs = {
        "servers": [connection_url],
    }
    if NATS_CREDS_FILE:
        connect_kwargs["user_credentials"] = NATS_CREDS_FILE

    try:
        await nats.connect(**connect_kwargs)
        LOGGER.info("NATS connection established.")
        profile.context.injector.bind_instance(NATS, nats)

        # Create and store JetStream context
        js = nats.jetstream(timeout=10)
        profile.context.injector.bind_instance(JetStreamContext, js)
    except (ErrConnectionClosed, ErrTimeout, ErrNoServers) as err:
        LOGGER.error("Caught error in NATS setup: %s", err)
        raise TransportError(f"No NATS instance setup: {err}")
    return js


async def define_stream(js: JetStreamContext, stream_name: str, subjects: list[str]):
    """Define a JetStream stream."""
    try:
        LOGGER.info("Defining %s with subjects %s", stream_name, subjects)
        await js.add_stream(name=stream_name, subjects=subjects)
        LOGGER.info("Stream %s defined with subjects %s", stream_name, subjects)
    except ErrTimeout as err:
        LOGGER.error("Timeout error defining stream %s: %s", stream_name, err)
        raise TransportError(f"Timeout error defining stream {stream_name}: {err}")
    except Exception as err:
        LOGGER.error("Error defining stream %s: %s", stream_name, err)
        raise TransportError(f"Error defining stream {stream_name}: {err}")


async def on_startup(profile: Profile, event: Event, retries: int = 5, delay: int = 5):
    """Setup NATS on startup."""
    LOGGER.info("Setup NATS JetStream on startup")
    js = await nats_jetstream_setup(profile, event)

    # Check JetStream context with retries
    attempt = 0
    while attempt < retries:
        try:
            account_info = await asyncio.wait_for(js.account_info(), timeout=60)
            is_working = account_info.streams > 0
            LOGGER.info("JetStream account info: %s", account_info)
            if is_working:
                LOGGER.info(
                    "JetStream is working with %d streams", account_info.streams
                )
                break
            else:
                LOGGER.warning("JetStream is not working properly, no streams found")
        except asyncio.TimeoutError:
            LOGGER.error(
                "Attempt %d: Timeout while checking JetStream account info", attempt + 1
            )
        except Exception as err:
            LOGGER.error(
                "Attempt %d: Error checking JetStream account info: %s",
                attempt + 1,
                err,
            )

        attempt += 1
        if attempt < retries:
            LOGGER.info("Retrying in %d seconds...", delay)
            await asyncio.sleep(delay)
        else:
            raise TransportError(
                "Failed to verify JetStream account info after multiple attempts"
            )

    config_events = get_config(profile.settings).event or EventConfig.default()
    for _, template in config_events.event_topic_maps.items():
        subjects = [template.replace("$wallet_id", "*")]
        name = template.replace(".$wallet_id", "").replace(".", "_")
        await define_stream(js, name, subjects)
    LOGGER.info("Successfully setup NATS JetStream.")


async def on_shutdown(profile: Profile, event: Event):
    """Called on shutdown."""
    LOGGER.info("NATS plugin shutdown hook called.")
    nats = profile.inject_or(NATS)
    if nats:
        await nats.close()
        LOGGER.info("NATS connection closed.")


def _derive_category(topic: str):
    match = RECORD_RE.match(topic)
    if match:
        return match.group(1)
    if WEBHOOK_RE.match(topic):
        return "webhook"


def process_event_payload(event_payload: Any):
    """Process event payload."""
    processed_event_payload = None
    if isinstance(event_payload, dict):
        processed_event_payload = event_payload
    else:
        try:
            processed_event_payload = orjson.loads(event_payload)
        except orjson.JSONDecodeError as err:
            LOGGER.error("Failed to decode event payload: %s", err)
            LOGGER.error("Payload: %s", event_payload)
            raise
    return processed_event_payload


async def publish_with_retry(
    js: JetStreamContext, subject: str, payload: bytes, retries: int = 3, delay: int = 5
):
    """Publish a message with retry logic."""
    attempt = 0
    while attempt < retries:
        try:
            ack = await js.publish(subject, payload)
            if ack.duplicate:
                LOGGER.warning("Duplicate message detected for subject %s", subject)
            else:
                LOGGER.info(
                    "Published message to subject %s with payload %s", subject, payload
                )
                return
        except (ErrConnectionClosed, ErrTimeout, ErrNoServers) as err:
            LOGGER.error(
                "Attempt %d: Failed to publish message to subject %s: %s",
                attempt + 1,
                subject,
                err,
            )

        attempt += 1
        if attempt < retries:
            LOGGER.info("Retrying in %d seconds...", delay)
            await asyncio.sleep(delay)
        else:
            raise TransportError(
                f"Failed to publish message to subject {subject} after {retries} attempts"
            )


def process_outbound_message_payload(payload: OutboundMessage):
    """Handle OutboundMessage types, to make them JSON serializable."""
    payload_ = payload.__dict__.copy()
    payload_["target"] = payload_["target"].__dict__.copy()
    payload_["target_list"] = [
        target.__dict__.copy() for target in payload_["target_list"]
    ]
    if isinstance(payload_["enc_payload"], bytes):
        payload_["enc_payload"] = payload_["enc_payload"].decode()

    return payload_


async def handle_event(profile: Profile, event: EventWithMetadata):
    """Push events from aca-py events."""
    config_events = get_config(profile.settings).event or EventConfig.default()
    pattern = event.metadata.pattern.pattern
    template = config_events.event_topic_maps.get(pattern)

    if not template:
        LOGGER.warning("Could not infer template from pattern: %s", pattern)
        return

    if "outbound-message" in template and isinstance(event.payload, OutboundMessage):
        event_payload_to_process = process_outbound_message_payload(event.payload)
    else:
        event_payload_to_process = event.payload

    js = profile.inject_or(JetStreamContext)
    if not js:
        LOGGER.warning("JetStream context not available. Setting up JetStream again")
        js = await nats_jetstream_setup(profile, event)

    LOGGER.info("Handling event: %s", event)
    wallet_id: Optional[str] = profile.settings.get("wallet.id")
    try:
        event_payload = process_event_payload(event_payload_to_process)
    except TypeError:
        LOGGER.warning("!!! FYI !!! Encountered TypeError")
        try:
            event_payload = event_payload_to_process.serialize()
        except AttributeError:
            LOGGER.warning("!!! FYI !!! Encountered AttributeError")
            try:
                event_payload = process_event_payload(event_payload_to_process.payload)
            except TypeError:
                LOGGER.warning("!!! FYI !!! Encountered TypeError2")
                event_payload = process_event_payload(
                    event_payload_to_process.enc_payload
                )
    payload = {
        "wallet_id": wallet_id or "base",
        "state": event_payload.get("state"),
        "topic": event.topic,
        "category": _derive_category(event.topic),
        "payload": event_payload,
    }
    try:
        nats_subject = Template(template).substitute(**payload)
        LOGGER.info("Sending message %s with NATS subject %s", payload, nats_subject)

        origin = profile.settings.get("default_label")
        group_id = profile.settings.get("wallet.group_id")

        metadata = {"time_ns": time.time_ns()}
        metadata_wallet_id = {"x-wallet-id": wallet_id} if wallet_id else {}
        metadata_group_id = {"group_id": group_id} if group_id else {}
        metadata_origin = {"origin": origin} if origin else {}
        metadata.update(metadata_wallet_id)
        metadata.update(metadata_group_id)
        metadata.update(metadata_origin)

        outbound_payload = orjson.dumps({"payload": payload, "metadata": metadata})

        # Publish message with retry logic
        await publish_with_retry(js, nats_subject, outbound_payload)

        # Deliver/dispatch events to webhook_urls directly
        webhook_urls = profile.settings.get("admin.webhook_urls")
        if config_events.deliver_webhook and webhook_urls:
            config_outbound = (
                get_config(profile.settings).outbound or OutboundConfig.default()
            )
            for endpoint in webhook_urls:
                api_key = None
                if len(endpoint.split("#")) > 1:
                    endpoint_hash_split = endpoint.split("#")
                    endpoint = endpoint_hash_split[0]
                    api_key = endpoint_hash_split[1]
                webhook_topic = config_events.event_webhook_topic_maps.get(event.topic)
                if endpoint and webhook_topic:
                    endpoint = f"{endpoint}/topic/{webhook_topic}/"
                    headers = {"x-wallet-id": wallet_id} if wallet_id else {}
                    if api_key is not None:
                        headers["x-api-key"] = api_key
                    outbound_msg = {
                        "service": {"url": endpoint},
                        "payload": base64.urlsafe_b64encode(
                            orjson.dumps(payload)
                        ).decode(),
                        "headers": headers,
                    }
                    await publish_with_retry(
                        js,
                        config_outbound.acapy_outbound_topic,
                        orjson.dumps(outbound_msg),
                    )
    except (ErrConnectionClosed, ErrTimeout, ErrNoServers, ValueError) as err:
        LOGGER.exception("Failed to process and send webhook, %s", err)
