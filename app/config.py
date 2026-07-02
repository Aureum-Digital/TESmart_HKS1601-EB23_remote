"""Persistent app configuration.

Config lives as JSON in ``$CONFIG_DIR/config.json`` (default ``/config``,
which is both the Docker volume and, later, the natural place for a Home
Assistant add-on's persistent data).

Environment variables seed the config on first start only; once the file
exists, values edited through the UI/API win. This mirrors how HA add-ons
pass options into a container.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_INPUT_NAMES = {str(i): f"PC{i}" for i in range(1, 17)}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("ignoring non-integer env %s=%r", name, raw)
        return default


@dataclass
class KVMConfig:
    host: str = "10.0.4.50"
    port: int = 5000
    timeout: float = 2.0
    retries: int = 2


@dataclass
class MQTTConfig:
    enabled: bool = False
    host: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""
    base_topic: str = "tesmart/kvm"
    discovery: bool = True
    discovery_prefix: str = "homeassistant"


@dataclass
class AppConfig:
    kvm: KVMConfig = field(default_factory=KVMConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    input_names: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_INPUT_NAMES))
    # Material Design Icon name per input (e.g. "laptop"), without "mdi-" prefix
    input_icons: dict[str, str] = field(default_factory=dict)
    # Inputs hidden from the web UI grid only (API, MQTT and HA still see all 16)
    hidden_inputs: list[int] = field(default_factory=list)
    poll_interval: float = 5.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kvm": vars(self.kvm).copy(),
            "mqtt": vars(self.mqtt).copy(),
            "input_names": dict(self.input_names),
            "input_icons": dict(self.input_icons),
            "hidden_inputs": sorted(self.hidden_inputs),
            "poll_interval": self.poll_interval,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        config = cls()
        for key, value in (data.get("kvm") or {}).items():
            if hasattr(config.kvm, key):
                setattr(config.kvm, key, value)
        for key, value in (data.get("mqtt") or {}).items():
            if hasattr(config.mqtt, key):
                setattr(config.mqtt, key, value)
        names = data.get("input_names") or {}
        for key in DEFAULT_INPUT_NAMES:
            name = str(names.get(key, "")).strip()
            if name:
                config.input_names[key] = name
        icons = data.get("input_icons") or {}
        for key in DEFAULT_INPUT_NAMES:
            icon = _clean_icon(icons.get(key, ""))
            if icon:
                config.input_icons[key] = icon
        hidden: set[int] = set()
        for value in data.get("hidden_inputs") or []:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= number <= 16:
                hidden.add(number)
        config.hidden_inputs = sorted(hidden)
        try:
            config.poll_interval = max(1.0, float(data.get("poll_interval", config.poll_interval)))
        except (TypeError, ValueError):
            pass
        config.kvm.port = _coerce_port(config.kvm.port, KVMConfig().port)
        config.mqtt.port = _coerce_port(config.mqtt.port, MQTTConfig().port)
        return config

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Build the initial config from environment variables."""
        config = cls()
        config.kvm.host = _env("TESMART_HOST", config.kvm.host)
        config.kvm.port = _env_int("TESMART_PORT", config.kvm.port)
        config.mqtt.host = _env("MQTT_HOST", "")
        config.mqtt.port = _env_int("MQTT_PORT", config.mqtt.port)
        config.mqtt.username = _env("MQTT_USERNAME", "")
        config.mqtt.password = os.environ.get("MQTT_PASSWORD", "")
        config.mqtt.base_topic = _env("MQTT_BASE_TOPIC", config.mqtt.base_topic).strip("/")
        config.mqtt.enabled = bool(config.mqtt.host)
        return config


def _clean_icon(value: Any) -> str:
    """Normalise an MDI icon name: strip mdi:/mdi- prefixes, allow [a-z0-9-]."""
    icon = str(value).strip().lower()
    for prefix in ("mdi:", "mdi-"):
        if icon.startswith(prefix):
            icon = icon[len(prefix):]
    return icon if re.fullmatch(r"[a-z0-9-]+", icon) else ""


def _coerce_port(value: Any, default: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


class ConfigStore:
    """Thread-safe load/save wrapper around the JSON config file."""

    def __init__(self, path: Path = CONFIG_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.config = self._load()

    def _load(self) -> AppConfig:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                logger.info("loaded config from %s", self.path)
                return AppConfig.from_dict(data)
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("could not read %s (%s); using env/defaults", self.path, exc)
        config = AppConfig.from_env()
        self._write(config)
        return config

    def save(self, config: AppConfig) -> None:
        with self._lock:
            self.config = config
            self._write(config)

    def _write(self, config: AppConfig) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(config.to_dict(), indent=2) + "\n")
            tmp.replace(self.path)
            logger.info("saved config to %s", self.path)
        except OSError as exc:
            logger.error("could not persist config to %s: %s", self.path, exc)
