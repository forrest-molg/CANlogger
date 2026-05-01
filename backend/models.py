from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class WaveformWindow(BaseModel):
    device_serial: str
    bus_name: str
    channel: Literal["A", "B"]
    sample_rate_hz: int
    sample_interval_ns: int
    window_ms: int
    started_at_us: int
    values_v: list[float]


class BusWaveformWindow(BaseModel):
    device_serial: str
    bus_name: str
    sample_rate_hz: int
    sample_interval_ns: int
    window_ms: int
    started_at_us: int
    can_h_values_v: list[float]
    can_l_values_v: list[float]


class StreamStatus(BaseModel):
    device_serial: str
    bus_name: str
    channel: Literal["A", "B"] | None = None
    active: bool
    state: Literal["ACTIVE", "IDLE", "OFFLINE", "ERROR"] = "IDLE"
    windows_captured: int = 0
    samples_captured: int = 0
    last_window_started_at_us: int | None = None
    last_error: str | None = None
    restart_count: int = 0
    last_restart_at_us: int | None = None


class RuntimeStatus(BaseModel):
    running: bool
    mode: str
    streams: list[StreamStatus]
    uptime_s: float = 0.0


class SnapshotStreamResult(BaseModel):
    device_serial: str
    bus_name: str
    state: Literal["ACTIVE", "IDLE", "OFFLINE", "ERROR"]
    captured: bool
    last_error: str | None = None
    window: BusWaveformWindow | None = None


class SnapshotResponse(BaseModel):
    mode: str
    captured_at_us: int
    streams: list[SnapshotStreamResult]


class StartCaptureRequest(BaseModel):
    sample_rate_hz: int | None = Field(default=None, ge=100_000, le=10_000_000)
    window_ms: int | None = Field(default=None, ge=1, le=1000)
    cadence_ms: int | None = Field(default=None, ge=1, le=1000)


class ConfigUpdateRequest(BaseModel):
    sample_rate_hz: int | None = Field(default=None, ge=100_000, le=10_000_000)
    window_ms: int | None = Field(default=None, ge=1, le=1000)
    cadence_ms: int | None = Field(default=None, ge=1, le=1000)


def unix_us_now() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1_000_000)
