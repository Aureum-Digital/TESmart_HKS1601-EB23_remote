"""Persistent app configuration.

Config lives as JSON in ``$CONFIG_DIR/config.json``. The directory is
resolved as: ``$CONFIG_DIR`` if set, ``/data`` when running as a Home
Assistant add-on (detected via ``/data/options.json``), else ``/config``
(the standalone Docker volume).

Standalone: environment variables seed the config on first start only; once
the file exists, values edited through the UI/API win.

Home Assistant add-on: the options from the add-on's Configuration tab
(``/data/options.json``) override connection/MQTT/polling settings on every
start, so the HA panel is the source of truth for those. Input names, icons
and hidden inputs stay web-UI-managed and persist in ``/data/config.json``.
If no MQTT host is set, the broker is auto-discovered from the Supervisor's
MQTT service (e.g. the Mosquitto add-on).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ADDON_OPTIONS_FILE = Path(os.environ.get("ADDON_OPTIONS_FILE", "/data/options.json"))


def _default_config_dir() -> Path:
    env = os.environ.get("CONFIG_DIR")
    if env:
        return Path(env)
    if ADDON_OPTIONS_FILE.exists():  # running as a Home Assistant add-on
        return ADDON_OPTIONS_FILE.parent
    return Path("/config")


CONFIG_DIR = _default_config_dir()
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


def _load_addon_options() -> dict[str, Any] | None:
    """Read the Home Assistant add-on options, if running as an add-on."""
    if not ADDON_OPTIONS_FILE.exists():
        return None
    try:
        options = json.loads(ADDON_OPTIONS_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("could not read add-on options %s: %s", ADDON_OPTIONS_FILE, exc)
        return None
    logger.info("running as Home Assistant add-on (options from %s)", ADDON_OPTIONS_FILE)
    return options if isinstance(options, dict) else None


def _supervisor_mqtt_service() -> dict[str, Any] | None:
    """Ask the HA Supervisor for the MQTT service (e.g. Mosquitto add-on)."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    request = urllib.request.Request(
        "http://supervisor/services/mqtt",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        logger.info("no MQTT service from Supervisor (%s)", exc)
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) and data.get("host") else None


class ConfigStore:
    """Thread-safe load/save wrapper around the JSON config file."""

    def __init__(self, path: Path = CONFIG_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.addon_options = _load_addon_options()
        self.addon_managed = self.addon_options is not None
        self.config = self._load()
        if self.addon_options is not None:
            self._apply_addon_options(self.config, self.addon_options)

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

    def _apply_addon_options(self, config: AppConfig, options: dict[str, Any]) -> None:
        """Overlay HA add-on options; they win on every start (not persisted)."""
        host = str(options.get("kvm_host") or "").strip()
        if host:
            config.kvm.host = host
        config.kvm.port = _coerce_port(options.get("kvm_port"), config.kvm.port)
        try:
            config.poll_interval = max(
                1.0, float(options.get("poll_interval", config.poll_interval))
            )
        except (TypeError, ValueError):
            pass

        mqtt_host = str(options.get("mqtt_host") or "").strip()
        if mqtt_host:
            config.mqtt.host = mqtt_host
            config.mqtt.port = _coerce_port(options.get("mqtt_port"), config.mqtt.port)
            config.mqtt.username = str(options.get("mqtt_username") or "")
            config.mqtt.password = str(options.get("mqtt_password") or "")
        else:
            service = _supervisor_mqtt_service()
            if service:
                config.mqtt.host = str(service["host"])
                config.mqtt.port = _coerce_port(service.get("port"), 1883)
                config.mqtt.username = str(service.get("username") or "")
                config.mqtt.password = str(service.get("password") or "")
                logger.info(
                    "MQTT broker auto-discovered via Supervisor: %s:%d",
                    config.mqtt.host,
                    config.mqtt.port,
                )
            else:
                config.mqtt.host = ""
        base_topic = str(options.get("mqtt_base_topic") or "").strip("/")
        if base_topic:
            config.mqtt.base_topic = base_topic
        config.mqtt.discovery = bool(options.get("mqtt_discovery", config.mqtt.discovery))
        config.mqtt.enabled = bool(config.mqtt.host)

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
