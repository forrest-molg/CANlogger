from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict

from config import AppConfig, DeviceConfig
from geekom_uploader import GeekomAsyncUploader
from models import BusWaveformWindow, RuntimeStatus, SnapshotResponse, SnapshotStreamResult, StartCaptureRequest, StreamStatus, WaveformWindow, unix_us_now
from picoscope_driver import PicoScope2204ADriver
from storage import WindowStorage


logger = logging.getLogger(__name__)


class CaptureService:
    @staticmethod
    def _device_key(device: DeviceConfig) -> str:
        return f"{device.bus_name}:{device.serial}:{device.channel}"

    @staticmethod
    def _merge_state(states: list[str]) -> str:
        if any(s == "ERROR" for s in states):
            return "ERROR"
        if any(s == "ACTIVE" for s in states):
            return "ACTIVE"
        if all(s == "OFFLINE" for s in states):
            return "OFFLINE"
        return "IDLE"

    @staticmethod
    def _combine_bus_window(device_serial: str, bus_name: str, window_a: WaveformWindow | None, window_b: WaveformWindow | None) -> BusWaveformWindow:
        base = window_a or window_b
        if base is None:
            raise RuntimeError(f"No channel data captured for bus {bus_name}")
        return BusWaveformWindow(
            device_serial=device_serial,
            bus_name=bus_name,
            sample_rate_hz=base.sample_rate_hz,
            sample_interval_ns=base.sample_interval_ns,
            window_ms=base.window_ms,
            started_at_us=base.started_at_us,
            can_h_values_v=(window_a.values_v if window_a else []),
            can_l_values_v=(window_b.values_v if window_b else []),
        )

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._storage = WindowStorage(config.storage)
        self._pico_driver = PicoScope2204ADriver()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._task_devices: dict[str, list[str]] = {}
        self._status: dict[str, StreamStatus] = {}
        for device in config.devices:
            state = "IDLE" if device.enabled else "OFFLINE"
            last_error = None if device.enabled else "Offline (disabled in config)"
            self._status[self._device_key(device)] = StreamStatus(
                device_serial=device.serial,
                bus_name=device.bus_name,
                channel=device.channel,
                active=False,
                state=state,
                last_error=last_error,
            )
        self._running = False
        self._started_monotonic: float | None = None
        self._geekom: GeekomAsyncUploader | None = None

    async def start(self, request: StartCaptureRequest | None = None) -> RuntimeStatus:
        logger.info("Capture start requested mode=%s running=%s", self._config.mode, self._running)
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
        if self._config.geekom.enabled:
            g = self._config.geekom
            self._geekom = GeekomAsyncUploader(
                ingest_url=g.ingest_url,
                post_timeout_s=g.post_timeout_s,
                max_queue=g.max_queue,
                num_workers=g.num_upload_workers,
                batch_size=g.batch_size,
            )
            self._geekom.start()
        if self._config.mode == "picoscope":
            devices_by_serial: dict[str, list[DeviceConfig]] = {}
            for device in self._config.devices:
                device_key = self._device_key(device)
                status = self._status[device_key]
                if not device.enabled:
                    status.active = False
                    status.state = "OFFLINE"
                    status.last_error = "Offline (disabled in config)"
                    continue
                devices_by_serial.setdefault(device.serial, []).append(device)

            for serial, serial_devices in devices_by_serial.items():
                channels = list(dict.fromkeys(d.channel for d in serial_devices))
                primary = serial_devices[0]
                task_key = f"scope:{serial}"
                try:
                    self._pico_driver.open_device(
                        primary,
                        sample_rate_hz=self._config.stream.sample_rate_hz,
                        window_ms=self._config.stream.window_ms,
                        stream=self._config.stream,
                        enabled_channels=channels,
                    )
                except Exception as exc:
                    logger.exception("Failed to open PicoScope device serial=%s", serial)
                    for device in serial_devices:
                        status = self._status[self._device_key(device)]
                        status.active = False
                        status.state = "ERROR"
                        status.last_error = str(exc)
                    continue

                task = asyncio.create_task(self._capture_loop_scope(serial, serial_devices), name=f"capture-scope-{serial}")
                self._tasks[task_key] = task
                self._task_devices[task_key] = [self._device_key(d) for d in serial_devices]
                for device in serial_devices:
                    status = self._status[self._device_key(device)]
                    status.active = True
                    status.state = "ACTIVE"
                    status.last_error = None
                logger.info("Capture scope worker started serial=%s channels=%s", serial, channels)

        return self.runtime_status()

    async def stop(self) -> RuntimeStatus:
        logger.info("Capture stop requested running=%s active_tasks=%s", self._running, len(self._tasks))
        self._running = False
        for task_key, task in list(self._tasks.items()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._tasks.pop(task_key, None)
            for device_key in self._task_devices.pop(task_key, []):
                self._status[device_key].active = False
                if self._status[device_key].state != "OFFLINE":
                    self._status[device_key].state = "IDLE"

        if self._config.mode == "picoscope":
            closed_serials: set[str] = set()
            for device in self._config.devices:
                if device.enabled and device.serial not in closed_serials:
                    self._pico_driver.close_device(device.serial)
                    closed_serials.add(device.serial)
                    logger.debug("Device closed during stop serial=%s", device.serial)

        await self._storage.stop()
        if self._geekom is not None:
            self._geekom.stop()
            self._geekom = None
        return self.runtime_status()

    def runtime_status(self) -> RuntimeStatus:
        uptime = 0.0
        if self._running and self._started_monotonic is not None:
            uptime = max(time.monotonic() - self._started_monotonic, 0.0)

        grouped: dict[tuple[str, str], list[StreamStatus]] = defaultdict(list)
        for st in self._status.values():
            grouped[(st.device_serial, st.bus_name)].append(st)

        bus_streams: list[StreamStatus] = []
        for (device_serial, bus_name), sts in grouped.items():
            bus_streams.append(
                StreamStatus(
                    device_serial=device_serial,
                    bus_name=bus_name,
                    channel=None,
                    active=any(s.active for s in sts),
                    state=self._merge_state([s.state for s in sts]),
                    windows_captured=max((s.windows_captured for s in sts), default=0),
                    samples_captured=max((s.samples_captured for s in sts), default=0),
                    last_window_started_at_us=max((s.last_window_started_at_us or 0 for s in sts), default=0) or None,
                    last_error=next((s.last_error for s in sts if s.last_error), None),
                )
            )

        return RuntimeStatus(
            running=self._running,
            mode=self._config.mode,
            streams=bus_streams,
            uptime_s=uptime,
        )

    def reinit_devices(self) -> None:
        """Rebuild per-device status from current config.devices.

        Called after _probe_and_configure() updates config.devices (on startup
        and on hot-rescan).  Preserves stats for devices that were already known;
        creates fresh IDLE/OFFLINE entries for newly discovered or removed ones.
        """
        new_status: dict[str, StreamStatus] = {}
        for device in self._config.devices:
            key = self._device_key(device)
            if key in self._status:
                new_status[key] = self._status[key]
            else:
                state = "IDLE" if device.enabled else "OFFLINE"
                last_error = None if device.enabled else "Offline (disabled in config)"
                new_status[key] = StreamStatus(
                    device_serial=device.serial,
                    bus_name=device.bus_name,
                    channel=device.channel,
                    active=False,
                    state=state,
                    last_error=last_error,
                )
        self._status = new_status
        logger.info(
            "Device status reinitialized: %d device(s) from config",
            len(new_status),
        )

    def queue_size(self) -> int:
        return self._storage.queue_size()

    async def capture_snapshot(self) -> SnapshotResponse:
        if self._running:
            raise RuntimeError("Stop continuous capture before taking a single-window snapshot")

        logger.info("Snapshot capture requested mode=%s", self._config.mode)

        results: list[SnapshotStreamResult] = []
        for device in self._config.devices:
            device_key = self._device_key(device)
            status = self._status[device_key]
            if not device.enabled:
                status.active = False
                status.state = "OFFLINE"
                status.last_error = "Offline (disabled in config)"
                results.append(
                    SnapshotStreamResult(
                        device_serial=device.serial,
                        bus_name=device.bus_name,
                        state="OFFLINE",
                        captured=False,
                        last_error=status.last_error,
                        window=None,
                    )
                )
                continue

        enabled_devices = [d for d in self._config.devices if d.enabled]
        if self._config.mode == "picoscope":
            devices_by_serial: dict[str, list[DeviceConfig]] = {}
            for device in enabled_devices:
                devices_by_serial.setdefault(device.serial, []).append(device)

            for serial, serial_devices in devices_by_serial.items():
                channels = list(dict.fromkeys(d.channel for d in serial_devices))
                primary = serial_devices[0]
                try:
                    logger.debug(
                        "Snapshot grouped capture begin serial=%s channels=%s buses=%s",
                        serial,
                        channels,
                        [d.bus_name for d in serial_devices],
                    )
                    await asyncio.to_thread(
                        self._pico_driver.open_device,
                        primary,
                        self._config.stream.sample_rate_hz,
                        self._config.stream.window_ms,
                        self._config.stream,
                        channels,
                    )
                    try:
                        captured = await asyncio.to_thread(
                            self._pico_driver.capture_windows,
                            serial,
                            self._config.stream.sample_rate_hz,
                            self._config.stream.window_ms,
                            channels,
                        )
                    finally:
                        await asyncio.to_thread(self._pico_driver.close_device, serial)

                    buses: dict[str, dict[str, WaveformWindow | None]] = defaultdict(lambda: {"A": None, "B": None})
                    for device in serial_devices:
                        w = captured.get(device.channel)
                        if w is not None:
                            w = w.model_copy(update={"bus_name": device.bus_name, "channel": device.channel, "device_serial": device.serial})
                        buses[device.bus_name][device.channel] = w

                    for bus_name, by_ch in buses.items():
                        combined = self._combine_bus_window(serial, bus_name, by_ch.get("A"), by_ch.get("B"))
                        for device in [d for d in serial_devices if d.bus_name == bus_name]:
                            device_key = self._device_key(device)
                            status = self._status[device_key]
                            status.active = False
                            status.state = "IDLE"
                            status.windows_captured += 1
                            status.samples_captured += len(combined.can_h_values_v or combined.can_l_values_v)
                            status.last_window_started_at_us = combined.started_at_us
                            status.last_error = None

                        results.append(
                            SnapshotStreamResult(
                                device_serial=serial,
                                bus_name=bus_name,
                                state="ACTIVE",
                                captured=True,
                                last_error=None,
                                window=combined,
                            )
                        )
                        logger.info(
                            "Snapshot capture success serial=%s bus=%s samples_h=%s samples_l=%s",
                            serial,
                            bus_name,
                            len(combined.can_h_values_v),
                            len(combined.can_l_values_v),
                        )
                except Exception as exc:
                    logger.exception("Snapshot grouped capture failed serial=%s", serial)
                    bus_names = sorted({d.bus_name for d in serial_devices})
                    for device in serial_devices:
                        status = self._status[self._device_key(device)]
                        status.active = False
                        status.state = "ERROR"
                        status.last_error = str(exc)
                    for bus_name in bus_names:
                        results.append(
                            SnapshotStreamResult(
                                device_serial=serial,
                                bus_name=bus_name,
                                state="ERROR",
                                captured=False,
                                last_error=str(exc),
                                window=None,
                            )
                        )
        return SnapshotResponse(
            mode=self._config.mode,
            captured_at_us=unix_us_now(),
            streams=results,
        )

    async def _capture_loop_scope(self, serial: str, devices: list[DeviceConfig]) -> None:
        cadence_s = self._config.stream.cadence_ms / 1000.0
        channels = list(dict.fromkeys(d.channel for d in devices))
        logger.debug("Scope capture loop entered serial=%s channels=%s cadence_s=%.4f", serial, channels, cadence_s)
        while self._running:
            try:
                if self._pico_driver.is_streaming(serial):
                    # Streaming mode: batch-drain the window queue to avoid asyncio.to_thread
                    # overhead at 1000 windows/second.  drain_stream_windows blocks up to 50ms
                    # waiting for the first window, then greedily returns up to 64 more.
                    captured_batch: list[dict] = await asyncio.to_thread(
                        self._pico_driver.drain_stream_windows,
                        serial,
                    )
                else:
                    # Block mode: arm → poll → retrieve (returns one window).
                    captured_batch = [await asyncio.to_thread(
                        self._pico_driver.capture_windows,
                        serial,
                        self._config.stream.sample_rate_hz,
                        self._config.stream.window_ms,
                        channels,
                    )]

                if not captured_batch:
                    continue

                for captured in captured_batch:
                    for device in devices:
                        device_key = self._device_key(device)
                        status = self._status[device_key]
                        window = captured.get(device.channel)
                        if window is None:
                            status.active = False
                            status.state = "ERROR"
                            status.last_error = f"No captured data for channel {device.channel}"
                            continue

                        window = window.model_copy(update={"bus_name": device.bus_name, "channel": device.channel, "device_serial": device.serial})
                        captured[device.channel] = window

                    buses: dict[str, dict[str, WaveformWindow | None]] = defaultdict(lambda: {"A": None, "B": None})
                    for device in devices:
                        buses[device.bus_name][device.channel] = captured.get(device.channel)

                    for bus_name, by_ch in buses.items():
                        combined = self._combine_bus_window(serial, bus_name, by_ch.get("A"), by_ch.get("B"))
                        await self._storage.enqueue(combined)
                        if self._geekom is not None:
                            self._geekom.enqueue(combined)
                        for device in [d for d in devices if d.bus_name == bus_name]:
                            status = self._status[self._device_key(device)]
                            status.active = True
                            status.state = "ACTIVE"
                            status.windows_captured += 1
                            status.samples_captured += len(combined.can_h_values_v or combined.can_l_values_v)
                            status.last_window_started_at_us = combined.started_at_us
                            status.last_error = None
            except Exception as exc:
                for device in devices:
                    status = self._status[self._device_key(device)]
                    status.active = False
                    status.state = "ERROR"
                    status.last_error = str(exc)
                logger.exception("Scope capture loop failed serial=%s", serial)
                # Back off on error to avoid a tight failure loop; no sleep on success.
                await asyncio.sleep(1.0)
