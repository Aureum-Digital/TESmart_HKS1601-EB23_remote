"""Standalone protocol/client library for TESmart HKS1601-EB23 KVM switches.

This package has no web or MQTT dependencies so it can be reused as-is in
other projects (e.g. a native Home Assistant integration).
"""

from .client import TESmartKVM
from .protocol import (
    LED_TIMEOUT_MODES,
    NUM_INPUTS,
    LedTimeoutMode,
    TESmartConnectionError,
    TESmartError,
    TESmartProtocolError,
)

__all__ = [
    "TESmartKVM",
    "TESmartError",
    "TESmartConnectionError",
    "TESmartProtocolError",
    "LedTimeoutMode",
    "LED_TIMEOUT_MODES",
    "NUM_INPUTS",
]
