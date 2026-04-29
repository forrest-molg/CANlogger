from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, PositiveInt


class StreamSettings(BaseModel):
    # 3.125 MHz / 1 ms matches the real PicoScope capture: 0.32 us intervals, ~3129 samples/window.
    sample_rate_hz: PositiveInt = 3_125_000
    window_ms: int = Field(default=1, ge=1, le=1000)
    cadence_ms: int = Field(default=10, ge=1, le=1000)
    channels_per_scope: int = Field(default=1, ge=1, le=2)
    # Trigger settings — matched to real CAN bus capture (3.2V falling edge, 10% pre-trigger).
    trigger_enabled: bool = True
    trigger_threshold_v: float = Field(default=3.2, ge=0.0, le=20.0)
    trigger_direction: Literal["rising", "falling"] = "falling"
    trigger_pre_trigger_pct: int = Field(default=10, ge=0, le=90)


class DeviceConfig(BaseModel):
    bus_name: str
    serial: str
    channel: Literal["A", "B"] = "A"
    enabled: bool = True


class StorageSettings(BaseModel):
    spool_dir: Path = Path("/data/spool")
    enable_postgres_upload: bool = False
    postgres_dsn: str = "postgresql://postgres:postgres@postgres:5432/canlogger"
    upload_batch_windows: int = Field(default=100, ge=1, le=10_000)


class GeekomSettings(BaseModel):
    enabled: bool = False
    ingest_url: str = "http://100.113.84.82:8000/ingest"
    bus_id: int = Field(default=1, ge=1, le=5)
    post_timeout_s: int = Field(default=10, ge=1, le=60)
    max_queue: int = Field(default=20, ge=1, le=200)


class AppConfig(BaseModel):
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    mode: Literal["simulator", "picoscope"] = "simulator"
    stream: StreamSettings = StreamSettings()
    storage: StorageSettings = StorageSettings()
    geekom: GeekomSettings = GeekomSettings()
    devices: list[DeviceConfig] = Field(default_factory=list)


DEFAULT_DEVICES = [
    DeviceConfig(bus_name="CAN_BUS_1", serial="SIM-001", channel="A"),
    DeviceConfig(bus_name="CAN_BUS_2", serial="SIM-002", channel="A"),
    DeviceConfig(bus_name="CAN_BUS_3", serial="SIM-003", channel="A"),
    DeviceConfig(bus_name="CAN_BUS_4", serial="SIM-004", channel="A"),
    DeviceConfig(bus_name="CAN_BUS_5", serial="SIM-005", channel="A"),
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
