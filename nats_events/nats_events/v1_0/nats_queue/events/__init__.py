"""ACA-Py Events to NATS."""

import base64
import logging
import os
import re
import time
from string import Template
from typing import Any, Optional, cast

import orjson
from aries_cloudagent.config.injection_context import InjectionContext
from aries_cloudagent.core.event_bus import Event, EventBus, EventWithMetadata
from aries_cloudagent.core.profile import Profile
from aries_cloudagent.core.util import SHUTDOWN_EVENT_PATTERN, STARTUP_EVENT_PATTERN
from aries_cloudagent.transport.error import TransportError
from nats.aio.client import Client as NATS
from nats.aio.errors import ErrConnectionClosed, ErrNoServers, ErrTimeout
from nats.js import JetStreamContext

from ..config import EventConfig, OutboundConfig, get_config

LOGGER = logging.getLogger(__name__)


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


async def nats_setup(profile: Profile, event: Event) -> NATS:
    """Connect, setup and return the NATS instance."""
    connection_url = (get_config(profile.settings).connection).connection_url
    LOGGER.info("Connecting to NATS url: %s", connection_url)
    nats = NATS()
    NATS_CREDS_FILE = os.getenv("NATS_CREDS_FILE")
    try:
        await nats.connect(servers=[connection_url], user_credentials=NATS_CREDS_FILE)
        LOGGER.info("NATS connection established.")
        profile.context.injector.bind_instance(NATS, nats)

        # Create and store JetStream context
        js = nats.jetstream()
        profile.context.injector.bind_instance(JetStreamContext, js)
    except (ErrConnectionClosed, ErrTimeout, ErrNoServers) as err:
        LOGGER.error("Caught error in NATS setup: %s", err)
        raise TransportError(f"No NATS instance setup: {err}")
    return nats


async def define_stream(js: JetStreamContext, stream_name: str, subjects: list[str]):
    """Define a JetStream stream."""
    try:
        await js.add_stream(name=stream_name, subjects=subjects)
        LOGGER.info("Stream %s defined with subjects %s", stream_name, subjects)
    except Exception as err:
        LOGGER.error("Error defining stream %s: %s", stream_name, err)
        raise TransportError(f"Error defining stream {stream_name}: {err}")


async def on_startup(profile: Profile, event: Event):
    """Setup NATS on startup."""
    LOGGER.info("Setup NATS on startup")
    await nats_setup(profile, event)
    js = profile.inject(JetStreamContext)
    config_events = get_config(profile.settings).event or EventConfig.default()
    for pattern, template in config_events.event_topic_maps.items():
        subjects = [template.replace("$wallet_id", "*")]
        await define_stream(js, pattern, subjects)
    LOGGER.info("Successfully setup NATS.")


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
        processed_event_payload = orjson.loads(event_payload)
    return processed_event_payload


async def handle_event(profile: Profile, event: EventWithMetadata):
    """Push events from aca-py events."""
    config_events = get_config(profile.settings).event or EventConfig.default()
    pattern = event.metadata.pattern.pattern
    template = config_events.event_topic_maps.get(pattern)

    if not template:
        LOGGER.warning("Could not infer template from pattern: %s", pattern)
        return

    if "-with-state" not in template:
        # We are only interested in state change webhooks. This avoids duplicate events
        return

    nats = profile.inject_or(NATS)
    if not nats:
        nats = await nats_setup(profile, event)

    js = profile.inject(JetStreamContext)

    LOGGER.debug("Handling event: %s", event)
    wallet_id = cast(Optional[str], profile.settings.get("wallet.id"))
    try:
        event_payload = process_event_payload(event.payload)
    except TypeError:
        try:
            event_payload = event.payload.serialize()
        except AttributeError:
            try:
                event_payload = process_event_payload(event.payload.payload)
            except TypeError:
                event_payload = process_event_payload(event.payload.enc_payload)
    payload = {
        "wallet_id": wallet_id or "base",
        "state": event_payload.get("state"),
        "topic": event.topic,
        "category": _derive_category(event.topic),
        "payload": event_payload,
    }
    webhook_urls = profile.settings.get("admin.webhook_urls")
    try:
        nats_subject = Template(template).substitute(**payload)
        LOGGER.debug("Sending message %s with NATS subject %s", payload, nats_subject)

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

        await js.publish(nats_subject, outbound_payload)

        # Deliver/dispatch events to webhook_urls directly
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
                    await js.publish(
                        config_outbound.acapy_outbound_topic,
                        orjson.dumps(outbound_msg),
                    )
    except (ErrConnectionClosed, ErrTimeout, ErrNoServers, ValueError) as err:
        LOGGER.exception("Failed to process and send webhook, %s", err)
