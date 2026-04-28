from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, PositiveInt


class StreamSettings(BaseModel):
    # Default for 125 kbps CAN gives 40 samples/bit and good SI visibility.
    sample_rate_hz: PositiveInt = 5_000_000
    window_ms: int = Field(default=10, ge=1, le=1000)
    cadence_ms: int = Field(default=10, ge=1, le=1000)
    channels_per_scope: int = Field(default=1, ge=1, le=2)


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


class AppConfig(BaseModel):
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    mode: Literal["simulator", "picoscope"] = "simulator"
    stream: StreamSettings = StreamSettings()
    storage: StorageSettings = StorageSettings()
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
