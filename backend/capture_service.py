from __future__ import annotations

import asyncio
import contextlib
import time

from config import AppConfig, DeviceConfig
from models import RuntimeStatus, StartCaptureRequest, StreamStatus
from simulator import generate_window
from storage import WindowStorage


class CaptureService:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._storage = WindowStorage(config.storage)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._status: dict[str, StreamStatus] = {
            d.serial: StreamStatus(device_serial=d.serial, bus_name=d.bus_name, active=False)
            for d in config.devices
        }
        self._running = False
        self._started_monotonic: float | None = None

    async def start(self, request: StartCaptureRequest | None = None) -> RuntimeStatus:
        if request:
            if request.sample_rate_hz is not None:
                self._config.stream.sample_rate_hz = request.sample_rate_hz
            if request.window_ms is not None:
                self._config.stream.window_ms = request.window_ms
            if request.cadence_ms is not None:
                self._config.stream.cadence_ms = request.cadence_ms

        if self._running:
            return self.runtime_status()

        self._running = True
        self._started_monotonic = time.monotonic()
        await self._storage.start()
        for device in self._config.devices:
            if not device.enabled:
                continue
            task = asyncio.create_task(self._capture_loop(device), name=f"capture-{device.serial}")
            self._tasks[device.serial] = task
            self._status[device.serial].active = True
        return self.runtime_status()

    async def stop(self) -> RuntimeStatus:
        self._running = False
        for serial, task in list(self._tasks.items()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._tasks.pop(serial, None)
            self._status[serial].active = False

        await self._storage.stop()
        return self.runtime_status()

    def runtime_status(self) -> RuntimeStatus:
        uptime = 0.0
        if self._running and self._started_monotonic is not None:
            uptime = max(time.monotonic() - self._started_monotonic, 0.0)
        return RuntimeStatus(
            running=self._running,
            mode=self._config.mode,
            streams=list(self._status.values()),
            uptime_s=uptime,
        )

    def queue_size(self) -> int:
        return self._storage.queue_size()

    async def _capture_loop(self, device: DeviceConfig) -> None:
        cadence_s = self._config.stream.cadence_ms / 1000.0
        while self._running:
            try:
                window = generate_window(
                    device=device,
                    sample_rate_hz=self._config.stream.sample_rate_hz,
                    window_ms=self._config.stream.window_ms,
                )
                await self._storage.enqueue(window)

                status = self._status[device.serial]
                status.windows_captured += 1
                status.samples_captured += len(window.values_v)
                status.last_window_started_at_us = window.started_at_us
                status.last_error = None
            except Exception as exc:
                self._status[device.serial].last_error = str(exc)

            await asyncio.sleep(cadence_s)
