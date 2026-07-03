"""FastAPI application: web UI + REST API + lifecycle wiring."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .config import AppConfig, ConfigStore
from .controller import KVMController
from .mqtt_bridge import MqttBridge
from .tesmart import LED_TIMEOUT_MODES, NUM_INPUTS, TESmartConnectionError, TESmartError

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

store = ConfigStore()
if store.addon_options:  # HA add-on option wins over the LOG_LEVEL env default
    _level = str(store.addon_options.get("log_level", "")).upper()
    if _level:
        logging.getLogger().setLevel(getattr(logging, _level, logging.INFO))
controller = KVMController(store.config)
bridge: MqttBridge | None = None


def _start_mqtt() -> None:
    global bridge
    bridge = MqttBridge(store.config, controller, asyncio.get_running_loop())
    bridge.start()


def _stop_mqtt() -> None:
    global bridge
    if bridge is not None:
        bridge.stop()
        bridge = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    controller.start_polling()
    _start_mqtt()
    yield
    _stop_mqtt()
    await controller.stop_polling()


app = FastAPI(title="TESmart KVM Controller", lifespan=lifespan)


# --------------------------------------------------------------- API models


class ConfigPayload(BaseModel):
    kvm: dict[str, Any] = Field(default_factory=dict)
    mqtt: dict[str, Any] = Field(default_factory=dict)
    input_names: dict[str, str] = Field(default_factory=dict)
    input_icons: dict[str, str] = Field(default_factory=dict)
    hidden_inputs: list[int] | None = None  # None = leave unchanged; list replaces
    poll_interval: float | None = None


# ------------------------------------------------------------------- routes


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/icon.png", include_in_schema=False)
async def icon() -> FileResponse:
    return FileResponse(STATIC_DIR / "icon.png")


@app.get("/api/status")
async def get_status() -> dict[str, Any]:
    snapshot = controller.state.snapshot()
    snapshot["kvm_host"] = controller.kvm.host
    snapshot["kvm_port"] = controller.kvm.port
    snapshot["mqtt_enabled"] = store.config.mqtt.enabled
    snapshot["mqtt_connected"] = bridge.connected if bridge else False
    snapshot["input_names"] = dict(controller.state.input_names)
    snapshot["input_icons"] = dict(store.config.input_icons)
    snapshot["hidden_inputs"] = list(store.config.hidden_inputs)
    return snapshot


@app.post("/api/refresh")
async def refresh_status() -> dict[str, Any]:
    try:
        return await controller.refresh()
    except TESmartConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except TESmartError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/input/{number}")
async def select_input(number: int) -> dict[str, Any]:
    if not 1 <= number <= NUM_INPUTS:
        raise HTTPException(status_code=422, detail=f"input must be 1..{NUM_INPUTS}")
    return await _run_command(controller.select_input(number))


@app.post("/api/buzzer/mute")
async def buzzer_mute() -> dict[str, Any]:
    return await _run_command(controller.mute_buzzer())


@app.post("/api/buzzer/unmute")
async def buzzer_unmute() -> dict[str, Any]:
    return await _run_command(controller.unmute_buzzer())


@app.post("/api/led-timeout/{mode}")
async def led_timeout(mode: str) -> dict[str, Any]:
    if mode not in LED_TIMEOUT_MODES:
        raise HTTPException(
            status_code=422, detail=f"mode must be one of {sorted(LED_TIMEOUT_MODES)}"
        )
    return await _run_command(controller.set_led_timeout(mode))  # type: ignore[arg-type]


async def _run_command(coro) -> dict[str, Any]:
    try:
        return await coro
    except TESmartConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (TESmartError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    # Note: returns the MQTT password so the settings form round-trips;
    # this app is meant for a trusted LAN (see README).
    data = store.config.to_dict()
    # true when running as a HA add-on: connection/MQTT/polling come from the
    # add-on configuration, so the web UI shows those fields read-only
    data["addon_managed"] = store.addon_managed
    return data


@app.post("/api/config")
async def update_config(payload: ConfigPayload) -> JSONResponse:
    merged = store.config.to_dict()
    merged["kvm"].update(payload.kvm)
    merged["mqtt"].update(payload.mqtt)
    merged["input_names"].update(payload.input_names)
    # empty string clears an icon; from_dict drops empties after the merge
    merged["input_icons"].update(payload.input_icons)
    if payload.hidden_inputs is not None:
        merged["hidden_inputs"] = payload.hidden_inputs
    if payload.poll_interval is not None:
        merged["poll_interval"] = payload.poll_interval
    config = AppConfig.from_dict(merged)
    config.mqtt.enabled = bool(config.mqtt.host)
    store.save(config)

    controller.apply_config(config)
    _stop_mqtt()
    _start_mqtt()
    logger.info("configuration updated and applied")
    return JSONResponse({"saved": True, "config": config.to_dict()})


@app.get("/api/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}
