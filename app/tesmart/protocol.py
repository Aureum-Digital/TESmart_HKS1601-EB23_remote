"""TESmart HKS1601-EB23 protocol implementation.

Frame format (host -> device), 6 bytes:

    AA BB 03 <command> <data> EE

Known commands:
    0x01  select input        data = 1..16 (one-based)
    0x02  buzzer              data = 0x00 mute, 0x01 unmute
    0x03  LED timeout         data = seconds (0x0A, 0x1E) or 0x00 for never
    0x10  query active input  data = 0x00

Status response (device -> host), 6 bytes:

    AA BB 03 11 <zero_based_input> <checksum>

The active input is ZERO-indexed in the response (0x00 = PC1 .. 0x0F = PC16)
and the final byte is a checksum observed to be (0x16 + zero_based_input),
NOT a fixed 0xEE tail. The device only sends the response if the TCP
connection is kept open after the query is written.

The device also accepts undocumented ASCII strings on the same TCP port to
change its network configuration (e.g. ``IP:10.0.4.50;``). These are
experimental and are exposed only through :func:`build_network_config`.
"""

from __future__ import annotations

import logging
from typing import Literal

logger = logging.getLogger(__name__)

FRAME_HEADER = bytes([0xAA, 0xBB, 0x03])
FRAME_TAIL = 0xEE
FRAME_LEN = 6

CMD_SELECT_INPUT = 0x01
CMD_BUZZER = 0x02
CMD_LED_TIMEOUT = 0x03
CMD_QUERY_INPUT = 0x10
RSP_CURRENT_INPUT = 0x11

# Observed checksum base for the 0x11 status response: 0x16 + zero_based_input
RSP_CHECKSUM_BASE = 0x16

NUM_INPUTS = 16

LedTimeoutMode = Literal["10", "30", "never"]
LED_TIMEOUT_MODES: dict[str, int] = {"10": 0x0A, "30": 0x1E, "never": 0x00}


class TESmartError(Exception):
    """Base error for TESmart operations."""


class TESmartConnectionError(TESmartError):
    """Raised when the KVM cannot be reached or does not answer."""


class TESmartProtocolError(TESmartError):
    """Raised when a device response cannot be parsed."""


def build_frame(command: int, data: int) -> bytes:
    """Build a standard 6-byte command frame."""
    if not 0 <= command <= 0xFF or not 0 <= data <= 0xFF:
        raise ValueError("command and data must be single bytes")
    return FRAME_HEADER + bytes([command, data, FRAME_TAIL])


def validate_input_number(input_number: int) -> int:
    if not isinstance(input_number, int) or isinstance(input_number, bool):
        raise ValueError(f"input number must be an int, got {input_number!r}")
    if not 1 <= input_number <= NUM_INPUTS:
        raise ValueError(f"input number must be 1..{NUM_INPUTS}, got {input_number}")
    return input_number


def build_select_input(input_number: int) -> bytes:
    """AA BB 03 01 <n> EE — n is one-based (1..16)."""
    return build_frame(CMD_SELECT_INPUT, validate_input_number(input_number))


def build_query_input() -> bytes:
    """AA BB 03 10 00 EE — ask for the active input."""
    return build_frame(CMD_QUERY_INPUT, 0x00)


def build_buzzer(mute: bool) -> bytes:
    """AA BB 03 02 00 EE mutes, AA BB 03 02 01 EE unmutes."""
    return build_frame(CMD_BUZZER, 0x00 if mute else 0x01)


def build_led_timeout(mode: LedTimeoutMode) -> bytes:
    """AA BB 03 03 <seconds> EE — 0x0A, 0x1E or 0x00 (never)."""
    try:
        data = LED_TIMEOUT_MODES[str(mode)]
    except KeyError:
        raise ValueError(
            f"LED timeout mode must be one of {sorted(LED_TIMEOUT_MODES)}, got {mode!r}"
        ) from None
    return build_frame(CMD_LED_TIMEOUT, data)


def parse_current_input(response: bytes) -> int:
    """Parse a status response and return the ONE-based active input (1..16).

    Accepts the observed response ``AA BB 03 11 <idx> <0x16 + idx>``. The
    header is searched for anywhere in the buffer so stray leading bytes are
    tolerated. A checksum mismatch is logged but does not reject the frame,
    since the checksum scheme is inferred from observation, not documented.
    """
    header = FRAME_HEADER + bytes([RSP_CURRENT_INPUT])
    start = response.find(header)
    if start < 0:
        raise TESmartProtocolError(
            f"no status frame (AA BB 03 11) in response: {response.hex(' ') or '<empty>'}"
        )
    frame = response[start : start + FRAME_LEN]
    if len(frame) < FRAME_LEN:
        raise TESmartProtocolError(
            f"truncated status frame: {frame.hex(' ')}"
        )
    zero_based = frame[4]
    if not 0 <= zero_based < NUM_INPUTS:
        raise TESmartProtocolError(
            f"active input byte 0x{zero_based:02X} out of range in frame {frame.hex(' ')}"
        )
    expected_checksum = (RSP_CHECKSUM_BASE + zero_based) & 0xFF
    if frame[5] != expected_checksum:
        logger.warning(
            "status frame checksum mismatch: got 0x%02X, expected 0x%02X (frame %s)",
            frame[5],
            expected_checksum,
            frame.hex(" "),
        )
    return zero_based + 1


def build_network_config(
    ip: str | None = None,
    port: int | None = None,
    gateway: str | None = None,
    netmask: str | None = None,
) -> bytes:
    """EXPERIMENTAL: build the undocumented ASCII network-config payload.

    The device accepts raw ASCII strings such as ``IP:10.0.4.50;`` on the
    same TCP port. This is not in the official protocol PDF; a bad value can
    make the KVM unreachable on the network. Not exposed via the web UI or
    REST API on purpose.
    """
    parts: list[str] = []
    if ip is not None:
        parts.append(f"IP:{ip};")
    if port is not None:
        parts.append(f"PT:{port};")
    if gateway is not None:
        parts.append(f"GW:{gateway};")
    if netmask is not None:
        parts.append(f"MA:{netmask};")
    if not parts:
        raise ValueError("at least one network parameter is required")
    return "".join(parts).encode("ascii")
