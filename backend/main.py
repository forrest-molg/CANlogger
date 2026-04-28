from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from capture_service import CaptureService
from config import AppConfig, load_config, save_config
from models import ConfigUpdateRequest, StartCaptureRequest

APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"
FRONTEND_DIR = PROJECT_ROOT / "frontend"

config: AppConfig = load_config(CONFIG_PATH)
capture_service = CaptureService(config)

app = FastAPI(title="CANlogger API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def ui() -> FileResponse:
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="GUI not found")
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


@app.get("/api/status")
def status() -> dict:
    runtime = capture_service.runtime_status().model_dump(mode="json")
    runtime["storage_queue"] = capture_service.queue_size()
    return runtime
