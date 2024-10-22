"""ACA-Py Over NATS."""

import logging

from .config import get_config

LOGGER = logging.getLogger(__name__)

__all__ = ["get_config"]
