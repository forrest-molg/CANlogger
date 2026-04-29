from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from capture_service import CaptureService
from config import AppConfig, DeviceConfig, load_config, save_config
from diagnostics import run_pico_diagnostics
from models import ConfigUpdateRequest, StartCaptureRequest


LOG_LEVEL = os.getenv("CANLOGGER_LOG_LEVEL", "DEBUG").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.DEBUG),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"
FRONTEND_DIST_DIR = PROJECT_ROOT / "frontend" / "dist"
FRONTEND_DIST_ASSETS_DIR = FRONTEND_DIST_DIR / "assets"

def _probe_and_configure(cfg: AppConfig) -> None:
    """Enumerate attached PicoScope 2204A units and assign them to bus slots.

    Serials are sorted lowest-first so the lowest serial number always becomes
    CAN_BUS_1.  Bus slots with no detected scope are disabled so the UI shows
    them as OFFLINE.  This runs only when mode == 'picoscope'.
    """
    if cfg.mode != "picoscope":
        return

    import ctypes

    detected: list[str] = []
    try:
        from picosdk.ps2000a import ps2000a as ps  # type: ignore

        count = ctypes.c_int16(0)
        serials_len = ctypes.c_int16(1024)
        serials_buf = ctypes.create_string_buffer(1024)
        ps.ps2000aEnumerateUnits(ctypes.byref(count), serials_buf, ctypes.byref(serials_len))
        raw = serials_buf.value.decode("utf-8", errors="ignore")
        detected = sorted(s.strip() for s in raw.split(",") if s.strip())
    except Exception as exc:
        logger.warning("PicoScope enumeration failed at startup: %s — all buses will be OFFLINE", exc)

    _MAX_BUSES = 5
    new_devices: list[DeviceConfig] = []
    for slot in range(_MAX_BUSES):
        bus_name = f"CAN_BUS_{slot + 1}"
        if slot < len(detected):
            serial = detected[slot]
            new_devices.append(DeviceConfig(bus_name=bus_name, serial=serial, channel="A", enabled=True))
            new_devices.append(DeviceConfig(bus_name=bus_name, serial=serial, channel="B", enabled=True))
        else:
            new_devices.append(DeviceConfig(bus_name=bus_name, serial=f"OFFLINE-{slot + 1:03d}", channel="A", enabled=False))

    cfg.devices = new_devices
    logger.info(
        "PicoScope auto-detect: %d scope(s) found %s; %d bus slot(s) offline",
        len(detected),
        detected,
        _MAX_BUSES - len(detected),
    )


config: AppConfig = load_config(CONFIG_PATH)
_probe_and_configure(config)
capture_service = CaptureService(config)

logger.info("CANlogger API starting with log level %s", LOG_LEVEL)

app = FastAPI(title="CANlogger API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if FRONTEND_DIST_ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST_ASSETS_DIR), name="assets")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def ui() -> FileResponse:
    index_file = FRONTEND_DIST_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="GUI not found. Build frontend first.")
    return FileResponse(index_file)


@app.get("/api/config")
def get_config() -> dict:
    return config.model_dump(mode="json")


@app.post("/api/config")
def update_config(update: ConfigUpdateRequest) -> dict:
    if capture_service.runtime_status().running:
        raise HTTPException(status_code=409, detail="Stop capture before updating config")

    if update.sample_rate_hz is not None:
        config.stream.sample_rate_hz = update.sample_rate_hz
    if update.window_ms is not None:
        config.stream.window_ms = update.window_ms
    if update.cadence_ms is not None:
        config.stream.cadence_ms = update.cadence_ms

    save_config(CONFIG_PATH, config)
    return config.model_dump(mode="json")


@app.post("/api/capture/start")
async def start_capture(request: StartCaptureRequest) -> dict:
    status = await capture_service.start(request)
    return status.model_dump(mode="json")


@app.post("/api/capture/stop")
async def stop_capture() -> dict:
    status = await capture_service.stop()
    return status.model_dump(mode="json")


@app.post("/api/capture/snapshot")
async def capture_snapshot() -> dict:
    try:
        snapshot = await capture_service.capture_snapshot()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return snapshot.model_dump(mode="json")


@app.get("/api/status")
def status() -> dict:
    runtime = capture_service.runtime_status().model_dump(mode="json")
    runtime["storage_queue"] = capture_service.queue_size()
    return runtime


@app.get("/api/diagnostics/pico")
async def pico_diagnostics() -> dict:
    # Safe by default: avoids live driver open probes that may destabilize the process on some stacks.
    return await asyncio.to_thread(run_pico_diagnostics, False)


@app.get("/api/diagnostics/pico/deep")
async def pico_diagnostics_deep() -> dict:
    # Explicit deep probe includes live driver open attempts.
    return await asyncio.to_thread(run_pico_diagnostics, True)
