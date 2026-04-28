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


class StreamStatus(BaseModel):
    device_serial: str
    bus_name: str
    active: bool
    windows_captured: int = 0
    samples_captured: int = 0
    last_window_started_at_us: int | None = None
    last_error: str | None = None


class RuntimeStatus(BaseModel):
    running: bool
    mode: str
    streams: list[StreamStatus]
    uptime_s: float = 0.0


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
