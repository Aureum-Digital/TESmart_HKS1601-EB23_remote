"""Async TCP client for the TESmart HKS1601-EB23 KVM switch.

Each command opens a fresh TCP connection, writes one frame and closes.
For queries the connection is kept open until the response arrives (the
device only answers on a still-open socket). All device access is
serialised through an asyncio lock so concurrent web/MQTT requests do not
interleave on the switch.
"""

from __future__ import annotations

import asyncio
import logging

from .protocol import (
    FRAME_LEN,
    LedTimeoutMode,
    TESmartConnectionError,
    build_buzzer,
    build_led_timeout,
    build_network_config,
    build_query_input,
    build_select_input,
    parse_current_input,
)

logger = logging.getLogger(__name__)


class TESmartKVM:
    """Async client for a TESmart 16-port KVM reachable over TCP."""

    def __init__(
        self,
        host: str,
        port: int = 5000,
        timeout: float = 2.0,
        retries: int = 2,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retries = max(0, retries)
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TESmartKVM({self.host}:{self.port})"

    async def select_input(self, input_number: int) -> None:
        """Switch to input 1..16."""
        await self._send(build_select_input(input_number))

    async def get_current_input(self) -> int:
        """Return the active input, one-based (1..16)."""
        response = await self._send(build_query_input(), expect_response=True)
        return parse_current_input(response)

    async def mute_buzzer(self) -> None:
        await self._send(build_buzzer(mute=True))

    async def unmute_buzzer(self) -> None:
        await self._send(build_buzzer(mute=False))

    async def set_led_timeout(self, mode: LedTimeoutMode) -> None:
        await self._send(build_led_timeout(mode))

    async def set_network_config(
        self,
        ip: str | None = None,
        port: int | None = None,
        gateway: str | None = None,
        netmask: str | None = None,
    ) -> None:
        """EXPERIMENTAL: push undocumented ASCII network config to the device.

        Can render the KVM unreachable if given wrong values. Deliberately
        not wired to the web UI or REST API.
        """
        payload = build_network_config(ip=ip, port=port, gateway=gateway, netmask=netmask)
        logger.warning("sending EXPERIMENTAL network config: %s", payload.decode("ascii"))
        await self._send(payload)

    async def _send(self, payload: bytes, expect_response: bool = False) -> bytes:
        """Send one payload on a fresh connection, optionally reading a reply."""
        async with self._lock:
            last_error: Exception | None = None
            for attempt in range(1, self.retries + 2):
                try:
                    return await self._send_once(payload, expect_response)
                except (OSError, asyncio.TimeoutError, ConnectionError) as exc:
                    last_error = exc
                    logger.debug(
                        "attempt %d/%d to %s:%d failed: %s",
                        attempt,
                        self.retries + 1,
                        self.host,
                        self.port,
                        exc,
                    )
            raise TESmartConnectionError(
                f"KVM at {self.host}:{self.port} unreachable after "
                f"{self.retries + 1} attempt(s): {last_error}"
            ) from last_error

    async def _send_once(self, payload: bytes, expect_response: bool) -> bytes:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout
        )
        try:
            logger.debug("TX %s:%d -> %s", self.host, self.port, payload.hex(" "))
            writer.write(payload)
            await writer.drain()

            if not expect_response:
                return b""

            # Keep the socket open and accumulate bytes until a full frame
            # is buffered or the timeout elapses; the device answers only on
            # the still-open connection and may deliver the frame in pieces.
            buffer = b""
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout
            while len(buffer) < FRAME_LEN:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError(
                        f"no full response within {self.timeout}s "
                        f"(got {buffer.hex(' ') or 'nothing'})"
                    )
                chunk = await asyncio.wait_for(reader.read(64), remaining)
                if not chunk:  # device closed the connection
                    raise ConnectionError(
                        f"connection closed before full response "
                        f"(got {buffer.hex(' ') or 'nothing'})"
                    )
                buffer += chunk
            logger.debug("RX %s:%d <- %s", self.host, self.port, buffer.hex(" "))
            return buffer
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:  # pragma: no cover - best-effort close
                pass
