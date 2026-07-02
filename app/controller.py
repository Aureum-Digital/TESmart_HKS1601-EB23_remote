"""KVM controller: owns the device client, cached state and the poll loop.

The web layer and the MQTT bridge both talk to this object. State changes
(input switched, connection lost/recovered) are pushed to registered
listeners so MQTT publishes immediately instead of waiting for a poll.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import AppConfig
from .tesmart import LedTimeoutMode, TESmartError, TESmartKVM

logger = logging.getLogger(__name__)

StateListener = Callable[[dict[str, Any]], None]


@dataclass
class KVMState:
    connected: bool = False
    current_input: int | None = None
    last_error: str | None = None
    last_updated: float | None = None
    buzzer_muted: bool | None = None  # not readable from device; last commanded value
    led_timeout: str | None = None  # not readable from device; last commanded value
    input_names: dict[str, str] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        name = None
        if self.current_input is not None:
            name = self.input_names.get(str(self.current_input), f"PC{self.current_input}")
        return {
            "connected": self.connected,
            "current_input": self.current_input,
            "current_input_name": name,
            "last_error": self.last_error,
            "last_updated": self.last_updated,
            "buzzer_muted": self.buzzer_muted,
            "led_timeout": self.led_timeout,
        }


class KVMController:
    def __init__(self, config: AppConfig) -> None:
        self.state = KVMState(input_names=dict(config.input_names))
        self._listeners: list[StateListener] = []
        self._poll_task: asyncio.Task | None = None
        self._poll_interval = config.poll_interval
        self._wake = asyncio.Event()
        self.apply_config(config)

    def apply_config(self, config: AppConfig) -> None:
        """(Re)build the device client after a config change."""
        self.kvm = TESmartKVM(
            host=config.kvm.host,
            port=config.kvm.port,
            timeout=float(config.kvm.timeout),
            retries=int(config.kvm.retries),
        )
        self._poll_interval = config.poll_interval
        self.state.input_names = dict(config.input_names)
        logger.info("controller targeting KVM at %s:%d", config.kvm.host, config.kvm.port)
        self.poke()

    def add_listener(self, listener: StateListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: StateListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self) -> None:
        snapshot = self.state.snapshot()
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:  # noqa: BLE001 - a bad listener must not kill others
                logger.exception("state listener failed")

    # ------------------------------------------------------------- commands

    async def select_input(self, input_number: int) -> dict[str, Any]:
        await self._run(self.kvm.select_input(input_number))
        # Trust the command optimistically, then confirm with a real query;
        # the poll loop would catch it anyway but this keeps UI/MQTT snappy.
        self.state.current_input = input_number
        self._notify()
        await self.refresh(raise_errors=False)
        return self.state.snapshot()

    async def mute_buzzer(self) -> dict[str, Any]:
        await self._run(self.kvm.mute_buzzer())
        self.state.buzzer_muted = True
        self._notify()
        return self.state.snapshot()

    async def unmute_buzzer(self) -> dict[str, Any]:
        await self._run(self.kvm.unmute_buzzer())
        self.state.buzzer_muted = False
        self._notify()
        return self.state.snapshot()

    async def set_led_timeout(self, mode: LedTimeoutMode) -> dict[str, Any]:
        await self._run(self.kvm.set_led_timeout(mode))
        self.state.led_timeout = str(mode)
        self._notify()
        return self.state.snapshot()

    async def refresh(self, raise_errors: bool = True) -> dict[str, Any]:
        """Query the device for the active input and update state."""
        try:
            current = await self.kvm.get_current_input()
        except TESmartError as exc:
            self._mark_error(exc)
            if raise_errors:
                raise
        else:
            changed = (
                not self.state.connected
                or self.state.current_input != current
                or self.state.last_error is not None
            )
            self.state.connected = True
            self.state.current_input = current
            self.state.last_error = None
            self.state.last_updated = time.time()
            if changed:
                self._notify()
        return self.state.snapshot()

    async def _run(self, coro) -> None:
        """Run a fire-and-forget device command, tracking connection state."""
        try:
            await coro
        except TESmartError as exc:
            self._mark_error(exc)
            raise
        else:
            if not self.state.connected or self.state.last_error:
                self.state.connected = True
                self.state.last_error = None
                self._notify()

    def _mark_error(self, exc: Exception) -> None:
        message = str(exc)
        changed = self.state.connected or self.state.last_error != message
        self.state.connected = False
        self.state.last_error = message
        logger.warning("KVM error: %s", message)
        if changed:
            self._notify()

    # ------------------------------------------------------------ poll loop

    def start_polling(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.get_running_loop().create_task(self._poll_loop())

    async def stop_polling(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    def poke(self) -> None:
        """Ask the poll loop to refresh as soon as possible."""
        self._wake.set()

    async def _poll_loop(self) -> None:
        logger.info("status poll loop started (every %.1fs)", self._poll_interval)
        while True:
            try:
                await self.refresh(raise_errors=False)
            except Exception:  # noqa: BLE001 - poll loop must survive anything
                logger.exception("unexpected error in poll loop")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self._poll_interval)
            except asyncio.TimeoutError:
                pass
