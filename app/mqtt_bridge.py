"""MQTT bridge: publishes KVM state and executes commands from MQTT.

Topics (relative to the configurable base topic, default ``tesmart/kvm``):

Published (retained):
    <base>/availability        "online" / "offline" (also the LWT)
    <base>/current_input       "1".."16"
    <base>/current_input_name  friendly name of the active input
    <base>/status              JSON snapshot (connected, input, errors, ...)

Subscribed:
    <base>/set/input           "1".."16" or an input's friendly name
    <base>/set/buzzer          "mute" / "unmute"
    <base>/set/led_timeout     "10" / "30" / "never"
    <base>/refresh             any payload -> query the device now

Optional Home Assistant MQTT discovery announces a select (input), a sensor
(current input), a switch (buzzer, optimistic), a select (LED timeout,
optimistic) and a refresh button.

paho-mqtt runs its network loop in a background thread; commands are handed
to the asyncio loop with ``run_coroutine_threadsafe`` so the device I/O
stays on the main loop and never blocks the broker thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import paho.mqtt.client as mqtt

from .config import AppConfig
from .controller import KVMController
from .tesmart import LED_TIMEOUT_MODES, NUM_INPUTS, TESmartError

logger = logging.getLogger(__name__)


class MqttBridge:
    def __init__(
        self,
        config: AppConfig,
        controller: KVMController,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.config = config
        self.controller = controller
        self.loop = loop
        self.base = config.mqtt.base_topic.strip("/") or "tesmart/kvm"
        self.connected = False
        self._client: mqtt.Client | None = None
        controller.add_listener(self._on_state_change)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if not self.config.mqtt.enabled or not self.config.mqtt.host:
            logger.info("MQTT disabled (no broker configured)")
            return
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"tesmart-kvm-{re.sub(r'[^a-zA-Z0-9]', '-', self.base)}",
        )
        if self.config.mqtt.username:
            client.username_pw_set(self.config.mqtt.username, self.config.mqtt.password or None)
        client.will_set(f"{self.base}/availability", "offline", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client = client
        try:
            client.connect_async(self.config.mqtt.host, self.config.mqtt.port, keepalive=60)
            client.loop_start()
            logger.info(
                "MQTT connecting to %s:%d (base topic %s)",
                self.config.mqtt.host,
                self.config.mqtt.port,
                self.base,
            )
        except (OSError, ValueError) as exc:
            logger.error("MQTT startup failed: %s", exc)
            self._client = None

    def stop(self) -> None:
        self.controller.remove_listener(self._on_state_change)
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.publish(f"{self.base}/availability", "offline", qos=1, retain=True)
            client.loop_stop()
            client.disconnect()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.exception("error stopping MQTT client")
        self.connected = False

    # ------------------------------------------------------- paho callbacks

    def _on_connect(self, client: mqtt.Client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            logger.error("MQTT connection refused: %s", reason_code)
            return
        self.connected = True
        logger.info("MQTT connected")
        client.subscribe([(f"{self.base}/set/+", 0), (f"{self.base}/refresh", 0)])
        client.publish(f"{self.base}/availability", "online", qos=1, retain=True)
        if self.config.mqtt.discovery:
            self.publish_discovery()
        self._publish_state(self.controller.state.snapshot())

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected = False
        logger.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, client: mqtt.Client, userdata, msg: mqtt.MQTTMessage) -> None:
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        logger.debug("MQTT <- %s: %r", msg.topic, payload)
        coro = self._dispatch(msg.topic, payload)
        if coro is not None:
            asyncio.run_coroutine_threadsafe(self._guarded(coro, msg.topic), self.loop)

    def _dispatch(self, topic: str, payload: str):
        if topic == f"{self.base}/refresh":
            return self.controller.refresh(raise_errors=False)
        if topic == f"{self.base}/set/input":
            number = self._resolve_input(payload)
            if number is None:
                logger.warning("MQTT set/input: unrecognised payload %r", payload)
                return None
            return self.controller.select_input(number)
        if topic == f"{self.base}/set/buzzer":
            if payload.lower() in ("mute", "off", "false", "0"):
                return self.controller.mute_buzzer()
            if payload.lower() in ("unmute", "on", "true", "1"):
                return self.controller.unmute_buzzer()
            logger.warning("MQTT set/buzzer: unrecognised payload %r", payload)
            return None
        if topic == f"{self.base}/set/led_timeout":
            mode = payload.lower()
            if mode in LED_TIMEOUT_MODES:
                return self.controller.set_led_timeout(mode)  # type: ignore[arg-type]
            logger.warning("MQTT set/led_timeout: unrecognised payload %r", payload)
            return None
        return None

    def _resolve_input(self, payload: str) -> int | None:
        """Accept an input number ("7") or a configured friendly name."""
        try:
            number = int(payload)
        except ValueError:
            for key, name in self.controller.state.input_names.items():
                if name.strip().lower() == payload.strip().lower():
                    return int(key)
            return None
        return number if 1 <= number <= NUM_INPUTS else None

    async def _guarded(self, coro, topic: str) -> None:
        try:
            await coro
        except (TESmartError, ValueError) as exc:
            logger.error("MQTT command on %s failed: %s", topic, exc)

    # ------------------------------------------------------------ publishing

    def _on_state_change(self, snapshot: dict[str, Any]) -> None:
        self._publish_state(snapshot)

    def _publish_state(self, snapshot: dict[str, Any]) -> None:
        client = self._client
        if client is None or not self.connected:
            return
        if snapshot["current_input"] is not None:
            client.publish(
                f"{self.base}/current_input", str(snapshot["current_input"]), retain=True
            )
            client.publish(
                f"{self.base}/current_input_name",
                snapshot["current_input_name"] or "",
                retain=True,
            )
        client.publish(f"{self.base}/status", json.dumps(snapshot), retain=True)

    # ------------------------------------------------- Home Assistant discovery

    def publish_discovery(self) -> None:
        """Publish (or refresh) HA MQTT discovery configs. Retained."""
        client = self._client
        if client is None:
            return
        node = re.sub(r"[^a-zA-Z0-9_-]", "_", self.base) or "tesmart_kvm"
        device = {
            "identifiers": [node],
            "name": "TESmart KVM",
            "manufacturer": "TESmart",
            "model": "HKS1601-EB23",
        }
        availability = [{"topic": f"{self.base}/availability"}]
        # hidden_inputs only affects the web UI grid; HA always sees all 16
        names = [
            self.controller.state.input_names.get(str(i), f"PC{i}")
            for i in range(1, NUM_INPUTS + 1)
        ]
        prefix = self.config.mqtt.discovery_prefix.strip("/") or "homeassistant"

        entities: dict[str, dict[str, Any]] = {
            f"{prefix}/select/{node}/input/config": {
                "name": "Input",
                "unique_id": f"{node}_input",
                "command_topic": f"{self.base}/set/input",
                "state_topic": f"{self.base}/current_input_name",
                "options": names,
                "icon": "mdi:video-switch",
            },
            f"{prefix}/sensor/{node}/current_input/config": {
                "name": "Current input",
                "unique_id": f"{node}_current_input",
                "state_topic": f"{self.base}/current_input",
                "icon": "mdi:numeric",
            },
            f"{prefix}/switch/{node}/buzzer/config": {
                "name": "Buzzer",
                "unique_id": f"{node}_buzzer",
                "command_topic": f"{self.base}/set/buzzer",
                "payload_on": "unmute",
                "payload_off": "mute",
                "optimistic": True,
                "icon": "mdi:volume-high",
            },
            f"{prefix}/select/{node}/led_timeout/config": {
                "name": "LED timeout",
                "unique_id": f"{node}_led_timeout",
                "command_topic": f"{self.base}/set/led_timeout",
                "options": ["10", "30", "never"],
                "optimistic": True,
                "icon": "mdi:led-on",
            },
            f"{prefix}/button/{node}/refresh/config": {
                "name": "Refresh status",
                "unique_id": f"{node}_refresh",
                "command_topic": f"{self.base}/refresh",
                "payload_press": "refresh",
                "icon": "mdi:refresh",
            },
        }
        for topic, payload in entities.items():
            payload["device"] = device
            payload["availability"] = availability
            client.publish(topic, json.dumps(payload), qos=1, retain=True)
        logger.info("published HA discovery configs under %s/", prefix)
