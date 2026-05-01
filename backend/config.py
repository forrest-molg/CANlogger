from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, PositiveInt


class StreamSettings(BaseModel):
    # 1.5625 MHz = 12.5 samples/bit @ 125 kbps — matches PicoScope 7 capture (1563 samples / 1 ms window).
    sample_rate_hz: PositiveInt = 1_562_500
    window_ms: int = Field(default=1, ge=1, le=1000)
    cadence_ms: int = Field(default=1, ge=1, le=1000)
    channels_per_scope: int = Field(default=2, ge=1, le=2)
    # Use ps2000 streaming mode for true near-gapless capture (no re-arm dead time).
    # Falls back to block mode automatically if streaming start fails.
    streaming_mode: bool = True
    # Free-run by default: fill and emit immediately, no edge wait.
    trigger_enabled: bool = False
    trigger_threshold_v: float = Field(default=3.2, ge=0.0, le=20.0)
    trigger_direction: Literal["rising", "falling"] = "rising"
    trigger_pre_trigger_pct: int = Field(default=0, ge=0, le=90)
    # Auto-trigger: fire this many ms after arm if no edge detected.
    # Keep well below cadence_ms so captures always complete within one cadence cycle.
    # With live CAN traffic the real edge fires in microseconds; this is just a fallback.
    trigger_auto_ms: int = Field(default=50, ge=10, le=10000)


class DeviceConfig(BaseModel):
    bus_name: str
    serial: str
    channel: Literal["A", "B"] = "A"
    enabled: bool = True
    # Set by the startup probe so open_device() can skip the wrong driver entirely.
    # None means "auto-detect" (try ps2000a first, fall back to ps2000).
    driver_kind: Literal["ps2000a", "ps2000"] | None = None


class StorageSettings(BaseModel):
    spool_dir: Path = Path("/data/spool")
    spool_enabled: bool = True
    enable_postgres_upload: bool = False
    postgres_dsn: str = "postgresql://postgres:postgres@postgres:5432/canlogger"
    upload_batch_windows: int = Field(default=100, ge=1, le=10_000)


class GeekomSettings(BaseModel):
    enabled: bool = False
    ingest_url: str = "http://100.113.84.82:8000/ingest"
    post_timeout_s: int = Field(default=10, ge=1, le=60)
    max_queue: int = Field(default=2000, ge=1, le=10000)
    num_upload_workers: int = Field(default=3, ge=1, le=10)
    batch_size: int = Field(default=10, ge=1, le=100)


class AppConfig(BaseModel):
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    mode: Literal["picoscope"] = "picoscope"
    stream: StreamSettings = StreamSettings()
    storage: StorageSettings = StorageSettings()
    geekom: GeekomSettings = GeekomSettings()
    devices: list[DeviceConfig] = Field(default_factory=list)


DEFAULT_DEVICES = [
    DeviceConfig(bus_name="CAN_BUS_1", serial="AUTO", channel="A"),
    DeviceConfig(bus_name="CAN_BUS_1", serial="AUTO", channel="B"),
    DeviceConfig(bus_name="CAN_BUS_2", serial="AUTO", channel="A"),
    DeviceConfig(bus_name="CAN_BUS_2", serial="AUTO", channel="B"),
    DeviceConfig(bus_name="CAN_BUS_3", serial="AUTO", channel="A"),
    DeviceConfig(bus_name="CAN_BUS_3", serial="AUTO", channel="B"),
    DeviceConfig(bus_name="CAN_BUS_4", serial="AUTO", channel="A"),
    DeviceConfig(bus_name="CAN_BUS_4", serial="AUTO", channel="B"),
    DeviceConfig(bus_name="CAN_BUS_5", serial="AUTO", channel="A"),
    DeviceConfig(bus_name="CAN_BUS_5", serial="AUTO", channel="B"),
]


def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        return AppConfig(devices=DEFAULT_DEVICES)

    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    if "devices" not in raw:
        raw["devices"] = [d.model_dump() for d in DEFAULT_DEVICES]

    cfg = AppConfig.model_validate(raw)
    cfg.storage.spool_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def save_config(config_path: Path, config: AppConfig) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(config.model_dump(mode="json"), fh, sort_keys=False)
