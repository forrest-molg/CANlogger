from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
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
    """Enumerate attached PicoScope units and assign them to bus slots.

    Tries ps2000a enumerate first (preferred — returns serials directly).
    If that finds nothing, falls back to the legacy ps2000 library, which is
    required for some 2204A firmware/host combinations where ps2000a cannot
    enumerate but ps2000_open_unit succeeds.

    Serials are sorted lowest-first so the lowest serial always becomes
    CAN_BUS_1.  Bus slots with no detected scope are disabled so the UI shows
    them as OFFLINE.  This runs only when mode == 'picoscope'.
    """
    if cfg.mode != "picoscope":
        return

    import ctypes

    _MAX_BUSES = 5
    detected: list[str] = []
    detected_driver_kind: Literal["ps2000a", "ps2000"] | None = None

    # --- ps2000a enumerate (preferred: returns serials without opening units) ---
    try:
        from picosdk.ps2000a import ps2000a as ps  # type: ignore

        count = ctypes.c_int16(0)
        serials_len = ctypes.c_int16(1024)
        serials_buf = ctypes.create_string_buffer(1024)
        status = int(ps.ps2000aEnumerateUnits(ctypes.byref(count), serials_buf, ctypes.byref(serials_len)))
        if status == 0 and int(count.value) > 0:
            raw = serials_buf.value.decode("utf-8", errors="ignore")
            detected = sorted(s.strip() for s in raw.split(",") if s.strip())
            detected_driver_kind = "ps2000a"
            logger.info("ps2000a enumeration: %d scope(s) found: %s", len(detected), detected)
        else:
            logger.info(
                "ps2000a enumeration returned status=%d count=%d — trying ps2000 fallback",
                status, int(count.value),
            )
    except Exception as exc:
        logger.warning("ps2000a enumeration failed: %s — trying ps2000 fallback", exc)

    # --- ps2000 legacy fallback (needed when ps2000a cannot enumerate) ---
    # Note: ps2000aOpenUnit with a specific serial (from this path) returns
    # PICO_NOT_FOUND in ~5 ms, so open_device() falls through to ps2000
    # quickly without hanging.
    if not detected:
        # Count PicoTech USB devices in sysfs first — avoids blocking ps2000_open_unit
        # calls when no scopes are physically connected (each call can hang for ~12s).
        sys_usb = Path("/sys/bus/usb/devices")
        pico_usb_count = 0
        try:
            for _dev in sys_usb.iterdir():
                _vf = _dev / "idVendor"
                if _vf.exists() and _vf.read_text(encoding="utf-8").strip().lower() == "0ce9":
                    pico_usb_count += 1
        except Exception:
            pass
        logger.debug("PicoTech USB devices found in sysfs: %d", pico_usb_count)

        if pico_usb_count == 0:
            logger.info("No PicoTech USB devices in sysfs — skipping ps2000 probe")
        else:
            try:
                import time as _time
                from picosdk.ps2000 import ps2000 as ps_legacy  # type: ignore

                # Give the USB device time to finish firmware loading after enumeration.
                # Without this, ps2000_open_unit() can return -1 immediately if called
                # within ~2s of the device appearing in sysfs (e.g. hot-plug during run).
                _time.sleep(2.0)

                handles: list[tuple[ctypes.c_int16, str]] = []
                for slot in range(min(pico_usb_count, _MAX_BUSES)):
                    handle_val = int(ps_legacy.ps2000_open_unit())
                    if handle_val <= 0:
                        logger.warning("ps2000_open_unit returned %d for slot %d", handle_val, slot)
                        break  # no more scopes available
                    handle = ctypes.c_int16(handle_val)
                    # info type 4 = BATCH_AND_SERIAL (e.g. "12451/0401")
                    serial_buf = ctypes.create_string_buffer(128)
                    ps_legacy.ps2000_get_unit_info(handle, serial_buf, ctypes.c_int16(128), ctypes.c_int16(4))
                    serial = serial_buf.value.decode("utf-8", errors="ignore").strip()
                    if not serial:
                        serial = f"PS2000-{slot + 1:03d}"
                    handles.append((handle, serial))
                    logger.debug("ps2000 detected scope handle=%d serial=%s", handle_val, serial)

                for handle, _ in handles:
                    ps_legacy.ps2000_close_unit(handle)

                detected = sorted(serial for _, serial in handles)
                if detected:
                    detected_driver_kind = "ps2000"
                    logger.info("ps2000 fallback enumeration: %d scope(s) found: %s", len(detected), detected)
                else:
                    logger.warning("ps2000 fallback found no scopes despite %d USB device(s) — all buses will be OFFLINE", pico_usb_count)
            except Exception as exc:
                logger.warning("ps2000 fallback enumeration failed: %s — all buses will be OFFLINE", exc)

    # --- Assign detected serials to bus slots ---
    new_devices: list[DeviceConfig] = []
    for slot in range(_MAX_BUSES):
        bus_name = f"CAN_BUS_{slot + 1}"
        if slot < len(detected):
            serial = detected[slot]
            new_devices.append(DeviceConfig(bus_name=bus_name, serial=serial, channel="A", enabled=True, driver_kind=detected_driver_kind))
            new_devices.append(DeviceConfig(bus_name=bus_name, serial=serial, channel="B", enabled=True, driver_kind=detected_driver_kind))
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
capture_service = CaptureService(config)

logger.info("CANlogger API initializing — USB probe will run after server starts")


@asynccontextmanager
async def _lifespan(app_: FastAPI):
    """Probe for PicoScope devices in a thread so the event loop isn't blocked."""
    await asyncio.to_thread(_probe_and_configure, config)
    capture_service.reinit_devices()
    logger.info("CANlogger API ready — log level %s", LOG_LEVEL)
    yield


app = FastAPI(title="CANlogger API", version="0.1.0", lifespan=_lifespan)
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


@app.get("/api/geekom/test")
async def test_geekom_connection() -> dict:
    """Test connectivity to the CANdatabase ingest server.

    Attempts a HEAD request to /health on the configured ingest URL base,
    then falls back to GET.  Returns latency, HTTP status, and config values
    so the frontend can display a clear PASS / FAIL banner.
    """
    import time
    import urllib.error
    import urllib.request

    base_url = config.geekom.ingest_url.rstrip("/").rsplit("/ingest", 1)[0]
    health_url = f"{base_url}/health"

    start = time.monotonic()
    http_status: int | None = None
    error_detail: str | None = None

    try:
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=config.geekom.post_timeout_s) as resp:
            http_status = resp.status
    except urllib.error.HTTPError as exc:
        http_status = exc.code
    except Exception as exc:
        error_detail = str(exc)

    latency_ms = round((time.monotonic() - start) * 1000, 1)
    ok = http_status is not None and http_status < 500

    return {
        "ok": ok,
        "http_status": http_status,
        "latency_ms": latency_ms,
        "error": error_detail,
        "ingest_url": config.geekom.ingest_url,
        "geekom_enabled": config.geekom.enabled,
        "health_url": health_url,
    }


@app.post("/api/devices/rescan")
async def rescan_devices() -> dict:
    """Re-enumerate attached PicoScope units and update bus assignments.

    Can be called without restarting the container whenever scopes are plugged
    or unplugged.  Capture must be stopped first.
    """
    if capture_service.runtime_status().running:
        raise HTTPException(status_code=409, detail="Stop capture before rescanning devices")
    await asyncio.to_thread(_probe_and_configure, config)
    capture_service.reinit_devices()
    runtime = capture_service.runtime_status().model_dump(mode="json")
    runtime["storage_queue"] = capture_service.queue_size()
    return runtime
