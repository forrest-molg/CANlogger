from __future__ import annotations

import ctypes
import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from config import DeviceConfig, StreamSettings
from models import WaveformWindow, unix_us_now


logger = logging.getLogger(__name__)


class _StreamState:
    """Mutable streaming state shared between the poll thread and the consumer."""

    def __init__(
        self,
        samples_per_window: int,
        range_v: float,
        sample_interval_ns: int,
        enabled_channels: tuple[str, ...],
        device_serial: str,
    ) -> None:
        self.samples_per_window = samples_per_window
        self.range_v = range_v
        self.sample_interval_ns = sample_interval_ns
        self.enabled_channels = enabled_channels
        self.device_serial = device_serial
        self.buffer_a: list[int] = []
        self.buffer_b: list[int] = []
        # maxsize=2000 windows (~2s at 1ms/window) — large enough to absorb the
        # initial burst when the scope delivers its full ring buffer backlog on first poll.
        self.window_queue: queue.Queue[dict] = queue.Queue(maxsize=2000)
        self.lock = threading.Lock()
        self.poll_thread: threading.Thread | None = None
        self.running = False
        self.callback_ref: object = None  # prevent ctypes callback GC
        self.overflow_count = 0
        # Sample-accurate timestamp anchor.  Set to unix_us_now() the moment
        # ps2000_run_streaming_ns returns.  Window N gets:
        #   ts_us = stream_start_us + N * samples_per_window * sample_interval_ns // 1000
        # This gives consecutive windows exactly (samples_per_window * interval_ns / 1000) µs
        # apart — matching the hardware sample clock — instead of OS wall-clock timestamps
        # that cluster during ring-buffer burst delivery and create DB query gaps.
        self.stream_start_us: int = 0
        self.windows_emitted: int = 0  # total complete windows placed in window_queue


@dataclass
class _OpenDevice:
    device: DeviceConfig
    handle: ctypes.c_int16
    driver_kind: str
    sample_count: int
    pre_trigger_samples: int
    range_v: float
    timebase: int
    sample_interval_ns: int
    legacy_time_units: int | None = None
    enabled_channels: tuple[str, ...] = ("A",)
    auto_trigger_ms: int = 50
    stream_state: _StreamState | None = None


class PicoScope2204ADriver:
    """Initial PicoScope 2204A capture implementation.

    This implementation uses PicoSDK Python bindings (`picosdk`) and ps2000a API
    in block mode. It captures one snapshot window per request for channel A/B.
    """

    def __init__(self) -> None:
        self._opened: dict[str, _OpenDevice] = {}
        self._ps: dict[str, object] = {}

    def _load_sdk(self, driver_kind: str):
        if driver_kind in self._ps:
            return self._ps[driver_kind]

        try:
            if driver_kind == "ps2000a":
                from picosdk.ps2000a import ps2000a as ps  # type: ignore
            elif driver_kind == "ps2000":
                from picosdk.ps2000 import ps2000 as ps  # type: ignore
            else:
                raise RuntimeError(f"Unsupported Pico driver kind: {driver_kind}")
        except Exception as exc:  # pragma: no cover - depends on host SDK install
            raise RuntimeError(
                "PicoSDK import failed: "
                f"{exc}. Ensure Python `picosdk` is installed and native Pico libraries are registered with the linker."
            ) from exc

        self._ps[driver_kind] = ps
        return ps

    @staticmethod
    def _check_status(status_code: int, operation: str) -> None:
        if status_code != 0:
            raise RuntimeError(f"{operation} failed with Pico status code {status_code}")

    @staticmethod
    def _range_volts_for_channel() -> float:
        # Keep 5V full-scale for CANH captures unless user asks for tighter range.
        return 5.0

    def _pick_timebase(self, ps, handle: ctypes.c_int16, sample_count: int, target_interval_ns: float) -> tuple[int, int]:
        best_timebase = None
        best_interval = None
        best_error = None

        for timebase in range(0, 2048):
            time_interval_ns = ctypes.c_float()
            max_samples = ctypes.c_int32()
            status = ps.ps2000aGetTimebase2(
                handle,
                timebase,
                sample_count,
                ctypes.byref(time_interval_ns),
                0,
                ctypes.byref(max_samples),
                0,
            )
            if status != 0:
                continue

            interval = int(time_interval_ns.value)
            error = abs(interval - target_interval_ns)
            if best_error is None or error < best_error:
                best_error = error
                best_timebase = timebase
                best_interval = interval

            if error <= 1:
                break

        if best_timebase is None or best_interval is None:
            raise RuntimeError("Unable to find a valid PicoScope timebase for requested settings")

        logger.debug(
            "Selected ps2000a timebase=%s interval_ns=%s sample_count=%s target_interval_ns=%.3f",
            best_timebase,
            best_interval,
            sample_count,
            target_interval_ns,
        )
        return best_timebase, best_interval

    @staticmethod
    def _timebase_interval_to_ns(interval: int, units: int) -> int:  # noqa: ARG004
        """Convert a ps2000_get_timebase time_interval value to nanoseconds.

        Despite the time_units output, ps2000_get_timebase always returns
        time_interval in nanoseconds — the units code indicates which internal
        oscillator is active, NOT the scale of the value.
        """
        del units  # interval is always in ns for ps2000_get_timebase
        return int(interval)

    @staticmethod
    def _legacy_units_to_ns(interval: int, units: int) -> int:
        """Convert a ps2000_get_times_and_values timestamp delta to nanoseconds.

        Uses PS2000_TIME_UNITS: 0=fs, 1=ps, 2=ns, 3=µs, 4=ms, 5=s.
        The time_units value comes from ps2000_get_timebase and is passed
        directly as the time_units parameter to ps2000_get_times_and_values,
        which then returns timestamps in that unit.
        """
        multipliers: dict[int, float] = {
            0: 1e-6,             # femtoseconds → ns
            1: 1e-3,             # picoseconds  → ns  (PS2000_PS)
            2: 1.0,              # nanoseconds  → ns  (PS2000_NS)
            3: 1_000.0,          # microseconds → ns  (PS2000_US)
            4: 1_000_000.0,      # milliseconds → ns  (PS2000_MS)
            5: 1_000_000_000.0,  # seconds      → ns  (PS2000_S)
        }
        return max(1, int(round(interval * multipliers.get(units, 1.0))))

    def _pick_legacy_timebase(
        self, ps, handle: ctypes.c_int16, sample_count: int, target_interval_ns: float
    ) -> tuple[int, int, int]:
        best_timebase = None
        best_interval_ns = None
        best_units = None
        best_error = None

        for timebase in range(0, 4096):
            time_interval = ctypes.c_int32()
            time_units = ctypes.c_int16()
            max_samples = ctypes.c_int32()
            status = ps.ps2000_get_timebase(
                handle,
                timebase,
                sample_count,
                ctypes.byref(time_interval),
                ctypes.byref(time_units),
                1,
                ctypes.byref(max_samples),
            )
            if status <= 0:
                continue

            # ps2000_get_timebase always returns time_interval in nanoseconds;
            # the time_units code indicates the oscillator, not the value scale.
            interval_ns = self._timebase_interval_to_ns(time_interval.value, time_units.value)
            error = abs(interval_ns - target_interval_ns)
            if best_error is None or error < best_error:
                best_error = error
                best_timebase = timebase
                best_interval_ns = interval_ns
                best_units = time_units.value

            if error <= 1:
                break

        if best_timebase is None or best_interval_ns is None or best_units is None:
            raise RuntimeError("Unable to find a valid legacy PicoScope timebase for requested settings")

        logger.debug(
            "Selected ps2000 timebase=%s interval_ns=%s units=%s sample_count=%s target_interval_ns=%.3f",
            best_timebase,
            best_interval_ns,
            best_units,
            sample_count,
            target_interval_ns,
        )
        return best_timebase, best_interval_ns, best_units

    def _resolve_legacy_capture_geometry(
        self, ps, handle: ctypes.c_int16, requested_sample_count: int, target_interval_ns: float
    ) -> tuple[int, int, int, int]:
        attempts: list[int] = []
        for candidate in (
            requested_sample_count,
            min(requested_sample_count, 32_000),
            min(requested_sample_count, 16_000),
            min(requested_sample_count, 8_000),
            min(requested_sample_count, 4_000),
            min(requested_sample_count, 2_000),
            min(requested_sample_count, 1_000),
        ):
            if candidate > 0 and candidate not in attempts:
                attempts.append(candidate)

        last_error: Exception | None = None
        for sample_count in attempts:
            try:
                timebase, sample_interval_ns, legacy_time_units = self._pick_legacy_timebase(
                    ps, handle, sample_count, target_interval_ns
                )
                return sample_count, timebase, sample_interval_ns, legacy_time_units
            except Exception as exc:
                last_error = exc

        raise RuntimeError("Unable to find a valid legacy PicoScope timebase for requested settings") from last_error

    def open_device(
        self,
        device: DeviceConfig,
        sample_rate_hz: int,
        window_ms: int,
        stream: StreamSettings | None = None,
        enabled_channels: list[str] | None = None,
    ) -> None:
        # Use ceil so fractional samples round up, matching PicoScope 7 behaviour
        # (e.g. 1 562 500 Hz × 1 ms = 1562.5 → 1563 samples).
        sample_count = max(1, math.ceil(sample_rate_hz * (window_ms / 1000.0)))
        target_interval_ns = 1_000_000_000 / sample_rate_hz

        # Derive trigger and pre-trigger parameters from stream config.
        trigger_enabled = stream.trigger_enabled if stream else True
        trigger_threshold_v = stream.trigger_threshold_v if stream else 3.2
        trigger_direction = stream.trigger_direction if stream else "falling"
        pre_trigger_pct = stream.trigger_pre_trigger_pct if stream else 10
        auto_trigger_ms = stream.trigger_auto_ms if stream else 50
        pre_trigger_samples = int(sample_count * pre_trigger_pct / 100)
        post_trigger_samples = sample_count - pre_trigger_samples
        channels = tuple(dict.fromkeys(enabled_channels or [device.channel]))

        logger.debug(
            "Opening PicoScope serial=%s channel=%s sample_rate_hz=%s window_ms=%s sample_count=%s pre_trigger_samples=%s post_trigger_samples=%s trigger_enabled=%s trigger_threshold_v=%s trigger_direction=%s",
            device.serial,
            device.channel,
            sample_rate_hz,
            window_ms,
            sample_count,
            pre_trigger_samples,
            post_trigger_samples,
            trigger_enabled,
            trigger_threshold_v,
            trigger_direction,
        )

        ps2000a_error: RuntimeError | None = None

        try:
            # Skip ps2000a if startup probe already identified this as a legacy ps2000 device.
            # ps2000aOpenUnit on a ps2000 device claims the USB interface but may not release
            # it cleanly on failure, leaving the device locked so ps2000_open_unit returns 0.
            if getattr(device, "driver_kind", None) == "ps2000":
                logger.debug("Skipping ps2000a attempt for serial=%s (driver_kind=ps2000)", device.serial)
                raise RuntimeError("ps2000aOpenUnit failed with Pico status code 3 (skipped — known ps2000 device)")
            ps = self._load_sdk("ps2000a")
            handle = ctypes.c_int16()
            serial_hint = None if device.serial.upper() == "AUTO" else device.serial.encode("utf-8")
            status = ps.ps2000aOpenUnit(ctypes.byref(handle), serial_hint)
            if status in (282, 286) and hasattr(ps, "ps2000aChangePowerSource"):
                logger.debug(
                    "ps2000aOpenUnit returned power status=%s for serial=%s, attempting ChangePowerSource",
                    status,
                    device.serial,
                )
                cps_status = ps.ps2000aChangePowerSource(handle, status)
                self._check_status(cps_status, "ps2000aChangePowerSource")
                status = 0
            self._check_status(status, "ps2000aOpenUnit")
            logger.debug("ps2000aOpenUnit succeeded for serial=%s handle=%s", device.serial, handle.value)

            channel_key = "PS2000A_CHANNEL_A" if device.channel == "A" else "PS2000A_CHANNEL_B"
            coupling = ps.PS2000A_COUPLING["PS2000A_DC"]
            voltage_range = ps.PS2000A_RANGE["PS2000A_5V"]

            for ch in channels:
                ch_key = "PS2000A_CHANNEL_A" if ch == "A" else "PS2000A_CHANNEL_B"
                ch_enum = ps.PS2000A_CHANNEL[ch_key]
                status = ps.ps2000aSetChannel(handle, ch_enum, 1, coupling, voltage_range, 0.0)
                self._check_status(status, "ps2000aSetChannel")

            trigger_channel_key = "PS2000A_CHANNEL_A" if "A" in channels else "PS2000A_CHANNEL_B"
            channel = ps.PS2000A_CHANNEL[trigger_channel_key]

            # Configure trigger on the capture channel.
            if trigger_enabled:
                max_adc_open = ctypes.c_int16()
                status = ps.ps2000aMaximumValue(handle, ctypes.byref(max_adc_open))
                self._check_status(status, "ps2000aMaximumValue (trigger setup)")
                max_adc_val = max_adc_open.value if max_adc_open.value != 0 else 32767
                threshold_adc = int(trigger_threshold_v / self._range_volts_for_channel() * max_adc_val)
                # PS2000A_THRESHOLD_DIRECTION: RISING=2, FALLING=3
                direction_code = 3 if trigger_direction == "falling" else 2
                logger.debug(
                    "Configuring ps2000a trigger serial=%s threshold_v=%.3f threshold_adc=%s direction=%s direction_code=%s",
                    device.serial,
                    trigger_threshold_v,
                    threshold_adc,
                    trigger_direction,
                    direction_code,
                )
                status = ps.ps2000aSetSimpleTrigger(
                    handle,
                    1,          # enable
                    channel,
                    threshold_adc,
                    direction_code,
                    0,          # delay (samples after trigger)
                    auto_trigger_ms,
                )
                self._check_status(status, "ps2000aSetSimpleTrigger")

            timebase, sample_interval_ns = self._pick_timebase(ps, handle, sample_count, target_interval_ns)

            self._opened[device.serial] = _OpenDevice(
                device=device,
                handle=handle,
                driver_kind="ps2000a",
                sample_count=sample_count,
                pre_trigger_samples=pre_trigger_samples,
                range_v=self._range_volts_for_channel(),
                timebase=timebase,
                sample_interval_ns=sample_interval_ns,
                enabled_channels=channels,
                auto_trigger_ms=auto_trigger_ms,
            )
            logger.info("Opened PicoScope with ps2000a serial=%s", device.serial)
            return
        except RuntimeError as exc:
            ps2000a_error = exc
            logger.warning("ps2000a open/config failed for serial=%s: %s", device.serial, exc)
            if "ps2000aOpenUnit failed with Pico status code 3" not in str(exc):
                raise

        logger.debug("Attempting legacy ps2000 fallback for serial=%s", device.serial)
        ps = self._load_sdk("ps2000")
        handle_value = 0
        handle = ctypes.c_int16(0)
        try:
            handle_value = ps.ps2000_open_unit()
            if handle_value <= 0:
                raise RuntimeError(f"ps2000_open_unit failed with handle/status {handle_value}")

            handle = ctypes.c_int16(handle_value)
            logger.debug("ps2000_open_unit succeeded for serial=%s handle=%s", device.serial, handle.value)

            voltage_range = ps.PS2000_VOLTAGE_RANGE["PS2000_5V"]
            for ch in channels:
                channel_key = "PS2000_CHANNEL_A" if ch == "A" else "PS2000_CHANNEL_B"
                channel = ps.PS2000_CHANNEL[channel_key]
                status = ps.ps2000_set_channel(handle, channel, 1, ps.PICO_COUPLING["DC"], voltage_range)
                if status <= 0:
                    raise RuntimeError(f"ps2000_set_channel failed with status {status}")

            # Configure trigger on legacy device.
            if trigger_enabled:
                # ps2000_set_trigger: source, threshold (ADC), direction (0=rising,1=falling), delay, autoTrigger_ms
                threshold_adc_legacy = int(trigger_threshold_v / self._range_volts_for_channel() * 32767)
                direction_legacy = 1 if trigger_direction == "falling" else 0
                ch_source = 0 if device.channel == "A" else 1
                logger.debug(
                    "Configuring ps2000 trigger serial=%s threshold_v=%.3f threshold_adc=%s direction=%s direction_code=%s source=%s",
                    device.serial,
                    trigger_threshold_v,
                    threshold_adc_legacy,
                    trigger_direction,
                    direction_legacy,
                    ch_source,
                )
                trig_status = ps.ps2000_set_trigger(
                    handle,
                    ch_source,
                    threshold_adc_legacy,
                    direction_legacy,
                    0,     # delay
                    auto_trigger_ms,
                )
                if trig_status <= 0:
                    raise RuntimeError(f"ps2000_set_trigger failed with status {trig_status}")

            legacy_sample_count, timebase, sample_interval_ns, legacy_time_units = self._resolve_legacy_capture_geometry(
                ps, handle, sample_count, target_interval_ns
            )
            legacy_pre_trigger = int(legacy_sample_count * pre_trigger_pct / 100)

            # Start streaming if requested (ps2000 only — block mode remains for ps2000a).
            streaming_mode = stream.streaming_mode if stream else False
            legacy_stream_state: _StreamState | None = None
            if streaming_mode:
                try:
                    legacy_stream_state = self._start_ps2000_streaming(
                        ps, handle, legacy_sample_count, sample_interval_ns, channels, device.serial
                    )
                except Exception as exc:
                    logger.warning("ps2000 streaming start failed for serial=%s, falling back to block mode: %s", device.serial, exc)
                    legacy_stream_state = None

            self._opened[device.serial] = _OpenDevice(
                device=device,
                handle=handle,
                driver_kind="ps2000",
                sample_count=legacy_sample_count,
                pre_trigger_samples=legacy_pre_trigger,
                range_v=self._range_volts_for_channel(),
                timebase=timebase,
                sample_interval_ns=sample_interval_ns,
                legacy_time_units=legacy_time_units,
                enabled_channels=channels,
                auto_trigger_ms=auto_trigger_ms,
                stream_state=legacy_stream_state,
            )
            logger.info("Opened PicoScope with legacy ps2000 serial=%s streaming=%s", device.serial, legacy_stream_state is not None)
        except Exception as legacy_exc:
            if handle_value > 0:
                ps.ps2000_close_unit(handle)

            if ps2000a_error is not None:
                raise RuntimeError(
                    "Unable to open PicoScope with either ps2000a or ps2000 drivers. "
                    f"ps2000a error: {ps2000a_error}; ps2000 error: {legacy_exc}. "
                    "This usually means the attached hardware family does not match the fallback driver or USB/device access is still incomplete."
                ) from legacy_exc
            raise

    # ------------------------------------------------------------------
    # Streaming mode (ps2000 only)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_ps2000_streaming_callback(stream_state: _StreamState) -> object:
        """Return a C-compatible callback for ps2000_get_streaming_last_values.

        Uses picosdk's C_CALLBACK_FUNCTION_FACTORY which is platform-safe
        (cdecl on Linux, stdcall on Windows).  The returned object MUST be
        kept alive for the duration of streaming (stored in stream_state.callback_ref).
        """
        from ctypes import POINTER, c_int16, c_uint32
        from picosdk.ctypes_wrapper import C_CALLBACK_FUNCTION_FACTORY  # type: ignore[import]

        CALLBACK = C_CALLBACK_FUNCTION_FACTORY(
            None,
            POINTER(POINTER(c_int16)),  # overviewBuffers
            c_int16,                    # overflow
            c_uint32,                   # triggeredAt
            c_int16,                    # triggered
            c_int16,                    # auto_stop
            c_uint32,                   # nValues
        )

        channels = stream_state.enabled_channels
        spw = stream_state.samples_per_window

        def _cb(buffers, overflow, triggered_at, triggered, auto_stop, n_values):  # noqa: ANN001
            if n_values == 0 or not buffers:
                return
            with stream_state.lock:
                if overflow:
                    stream_state.overflow_count += 1
                # overviewBuffers layout (noOfSamplesPerAggregate=1):
                #   buffers[0] = Ch A  |  buffers[2] = Ch B
                if "A" in channels and buffers[0]:
                    stream_state.buffer_a.extend(buffers[0][0:n_values])
                if "B" in channels and buffers[2]:
                    stream_state.buffer_b.extend(buffers[2][0:n_values])
                # Emit complete windows whenever both channels have enough data.
                while True:
                    a_ready = "A" not in channels or len(stream_state.buffer_a) >= spw
                    b_ready = "B" not in channels or len(stream_state.buffer_b) >= spw
                    if not (a_ready and b_ready):
                        break
                    # Derive timestamp from hardware sample count so consecutive windows
                    # are exactly spw*interval_ns/1000 µs apart regardless of when the
                    # callback fires.  This prevents timestamp clustering when the ring
                    # buffer delivers many windows in a single callback invocation.
                    ts_us = (
                        stream_state.stream_start_us
                        + stream_state.windows_emitted * spw * stream_state.sample_interval_ns // 1000
                    )
                    wa = stream_state.buffer_a[:spw] if "A" in channels else []
                    wb = stream_state.buffer_b[:spw] if "B" in channels else []
                    if "A" in channels:
                        stream_state.buffer_a = stream_state.buffer_a[spw:]
                    if "B" in channels:
                        stream_state.buffer_b = stream_state.buffer_b[spw:]
                    try:
                        stream_state.windows_emitted += 1
                        stream_state.window_queue.put_nowait({"ts_us": ts_us, "A": wa, "B": wb})
                    except queue.Full:
                        logger.warning("ps2000 stream window queue full serial=%s, dropping window", stream_state.device_serial)

        return CALLBACK(_cb)

    @staticmethod
    def _ps2000_stream_poll(ps: object, handle: ctypes.c_int16, stream_state: _StreamState) -> None:
        """Background thread: polls ps2000_get_streaming_last_values at 100 µs intervals."""
        while stream_state.running:
            ps.ps2000_get_streaming_last_values(handle, stream_state.callback_ref)  # type: ignore[attr-defined]
            time.sleep(0.0001)

    def _start_ps2000_streaming(
        self,
        ps: object,
        handle: ctypes.c_int16,
        sample_count: int,
        sample_interval_ns: int,
        enabled_channels: tuple[str, ...],
        device_serial: str,
    ) -> _StreamState:
        """Call ps2000_run_streaming_ns and launch the poll thread."""
        stream_state = _StreamState(
            samples_per_window=sample_count,
            range_v=self._range_volts_for_channel(),
            sample_interval_ns=sample_interval_ns,
            enabled_channels=enabled_channels,
            device_serial=device_serial,
        )

        # Internal ring buffer: must hold enough for the PC to drain between callbacks.
        # The overview_buffer_size (per-callback chunk) must be >= max_samples // 4
        # (empirically required by the ps2000 driver; the PDF example uses a 2:1 ratio).
        # 100 000 samples @ 1.5625 MS/s = ~64 ms of headroom against upload latency.
        max_samples_ring = 100_000
        overview_buffer_size = max_samples_ring // 2  # 50 000 — satisfies the ≥25% rule

        status = ps.ps2000_run_streaming_ns(  # type: ignore[attr-defined]
            handle,
            sample_interval_ns,  # c_uint: 640 (ns)
            2,                   # c_int:  PS2000_NS
            max_samples_ring,    # c_uint: internal ring buffer size
            0,                   # c_short: auto_stop=0 (run forever)
            1,                   # c_uint:  noOfSamplesPerAggregate=1 (raw)
            overview_buffer_size,# c_uint:  overview buffer size
        )
        if status <= 0:
            raise RuntimeError(f"ps2000_run_streaming_ns failed with status {status}")

        # Anchor the sample clock immediately after the hardware starts.
        stream_state.stream_start_us = unix_us_now()

        cb = self._make_ps2000_streaming_callback(stream_state)
        stream_state.callback_ref = cb
        stream_state.running = True

        t = threading.Thread(
            target=self._ps2000_stream_poll,
            args=(ps, handle, stream_state),
            name=f"ps2000-stream-{device_serial}",
            daemon=True,
        )
        stream_state.poll_thread = t
        t.start()

        logger.info(
            "ps2000 streaming started serial=%s interval_ns=%s ring_samples=%s",
            device_serial, sample_interval_ns, max_samples_ring,
        )
        return stream_state

    def is_streaming(self, serial: str) -> bool:
        """Return True if the device is running in streaming mode."""
        opened = self._opened.get(serial)
        return opened is not None and opened.stream_state is not None and opened.stream_state.running

    def capture_stream_window(self, serial: str) -> dict[str, WaveformWindow]:
        """Block until the next complete 1-ms window is available from the stream."""
        opened = self._opened.get(serial)
        if opened is None:
            raise RuntimeError(f"PicoScope {serial} is not opened")
        ss = opened.stream_state
        if ss is None or not ss.running:
            raise RuntimeError(f"PicoScope {serial} is not in streaming mode")

        try:
            item = ss.window_queue.get(timeout=5.0)
        except queue.Empty:
            raise RuntimeError(f"ps2000 stream timeout — no window received within 5 s for serial={serial}")

        return self._stream_item_to_windows(opened, item)

    def drain_stream_windows(self, serial: str, max_batch: int = 64) -> list[dict[str, WaveformWindow]]:
        """Return up to max_batch complete windows from the stream queue without blocking.

        Blocks briefly (up to 32ms) waiting for the first window, then greedily
        drains additional available windows non-blocking.  This batches what the
        callback delivers per 32ms chunk, reducing asyncio.to_thread overhead
        from 1000 calls/second to ~30 calls/second.
        """
        opened = self._opened.get(serial)
        if opened is None:
            raise RuntimeError(f"PicoScope {serial} is not opened")
        ss = opened.stream_state
        if ss is None or not ss.running:
            raise RuntimeError(f"PicoScope {serial} is not in streaming mode")

        results: list[dict[str, WaveformWindow]] = []
        try:
            # Wait up to 50ms for the first window.
            item = ss.window_queue.get(timeout=0.05)
            results.append(self._stream_item_to_windows(opened, item))
        except queue.Empty:
            return results

        # Greedily drain remaining available windows.
        while len(results) < max_batch:
            try:
                item = ss.window_queue.get_nowait()
                results.append(self._stream_item_to_windows(opened, item))
            except queue.Empty:
                break

        return results

    def _stream_item_to_windows(self, opened: _OpenDevice, item: dict) -> dict[str, WaveformWindow]:
        ts_us: int = item["ts_us"]
        out: dict[str, WaveformWindow] = {}
        for ch in ("A", "B"):
            if ch not in opened.enabled_channels:
                continue
            raw: list[int] = item.get(ch, [])
            if not raw:
                continue
            adc = np.array(raw, dtype=np.float32)
            volts = (adc / 32767.0) * opened.range_v
            out[ch] = WaveformWindow(
                device_serial=opened.device.serial,
                bus_name=opened.device.bus_name,
                channel=ch,
                sample_rate_hz=max(1, int(round(1_000_000_000 / opened.sample_interval_ns))),
                sample_interval_ns=opened.sample_interval_ns,
                window_ms=max(1, int(round((opened.sample_count * opened.sample_interval_ns) / 1_000_000))),
                started_at_us=ts_us,
                values_v=volts.tolist(),
            )
        return out

    def close_device(self, serial: str) -> None:
        opened = self._opened.pop(serial, None)
        if opened is None:
            return

        logger.debug("Closing PicoScope serial=%s driver=%s", serial, opened.driver_kind)

        # Stop streaming poll thread before closing the hardware handle.
        if opened.stream_state is not None:
            opened.stream_state.running = False
            if opened.stream_state.poll_thread is not None:
                opened.stream_state.poll_thread.join(timeout=1.0)
            logger.debug("ps2000 stream poll thread stopped serial=%s", serial)

        ps = self._load_sdk(opened.driver_kind)
        if opened.driver_kind == "ps2000a":
            ps.ps2000aStop(opened.handle)
            ps.ps2000aCloseUnit(opened.handle)
            return

        ps.ps2000_stop(opened.handle)
        ps.ps2000_close_unit(opened.handle)

    def capture_windows(
        self,
        serial: str,
        sample_rate_hz: int,
        window_ms: int,
        channels: list[str] | None = None,
    ) -> dict[str, WaveformWindow]:
        opened = self._opened.get(serial)
        if opened is None:
            raise RuntimeError(f"PicoScope device {serial} is not opened")

        requested_channels = tuple(dict.fromkeys(channels or list(opened.enabled_channels)))
        active_channels = [ch for ch in requested_channels if ch in opened.enabled_channels]
        if not active_channels:
            active_channels = list(opened.enabled_channels)

        sample_count = opened.sample_count
        logger.debug(
            "Capturing window serial=%s driver=%s sample_count=%s pre_trigger_samples=%s channels=%s",
            serial,
            opened.driver_kind,
            sample_count,
            opened.pre_trigger_samples,
            active_channels,
        )

        if opened.driver_kind == "ps2000a":
            ps = self._load_sdk("ps2000a")
            buffers: dict[str, object] = {}
            for ch in active_channels:
                channel_key = "PS2000A_CHANNEL_A" if ch == "A" else "PS2000A_CHANNEL_B"
                channel = ps.PS2000A_CHANNEL[channel_key]
                adc_buffer = (ctypes.c_int16 * sample_count)()
                status = ps.ps2000aSetDataBuffer(opened.handle, channel, adc_buffer, sample_count, 0, 0)
                self._check_status(status, "ps2000aSetDataBuffer")
                buffers[ch] = adc_buffer

            time_indisposed = ctypes.c_int32()
            status = ps.ps2000aRunBlock(
                opened.handle,
                opened.pre_trigger_samples,
                sample_count - opened.pre_trigger_samples,
                opened.timebase,
                ctypes.byref(time_indisposed),
                0,
                None,
                None,
            )
            self._check_status(status, "ps2000aRunBlock")

            ready = ctypes.c_int16(0)
            deadline = time.monotonic() + (opened.auto_trigger_ms / 1000) + 0.5
            while ready.value == 0:
                status = ps.ps2000aIsReady(opened.handle, ctypes.byref(ready))
                self._check_status(status, "ps2000aIsReady")
                if time.monotonic() > deadline:
                    raise RuntimeError("Timed out waiting for PicoScope block capture")
                time.sleep(0.0002)

            sample_count_out = ctypes.c_int32(sample_count)
            overflow = ctypes.c_int16()
            status = ps.ps2000aGetValues(
                opened.handle,
                0,
                ctypes.byref(sample_count_out),
                1,
                0,
                0,
                ctypes.byref(overflow),
            )
            self._check_status(status, "ps2000aGetValues")

            logger.debug(
                "ps2000a capture complete serial=%s returned_samples=%s overflow=%s",
                serial,
                sample_count_out.value,
                overflow.value,
            )

            max_adc = ctypes.c_int16()
            status = ps.ps2000aMaximumValue(opened.handle, ctypes.byref(max_adc))
            self._check_status(status, "ps2000aMaximumValue")
            if max_adc.value == 0:
                raise RuntimeError("PicoScope returned invalid ADC full-scale value")

            out: dict[str, WaveformWindow] = {}
            for ch, adc_buffer in buffers.items():
                adc = np.ctypeslib.as_array(adc_buffer)[: sample_count_out.value].astype(np.float32)
                volts = (adc / float(max_adc.value)) * opened.range_v
                out[ch] = WaveformWindow(
                    device_serial=opened.device.serial,
                    bus_name=opened.device.bus_name,
                    channel=ch,
                    sample_rate_hz=max(1, int(round(1_000_000_000 / opened.sample_interval_ns))),
                    sample_interval_ns=opened.sample_interval_ns,
                    window_ms=max(1, int(round((sample_count * opened.sample_interval_ns) / 1_000_000))),
                    started_at_us=unix_us_now(),
                    values_v=volts.tolist(),
                )
            return out

        ps = self._load_sdk("ps2000")
        time_indisposed = ctypes.c_int32()
        status = ps.ps2000_run_block(opened.handle, sample_count, opened.timebase, 1, ctypes.byref(time_indisposed))
        if status <= 0:
            raise RuntimeError(f"ps2000_run_block failed with status {status}")

        deadline = time.monotonic() + (opened.auto_trigger_ms / 1000) + 0.5
        while ps.ps2000_ready(opened.handle) == 0:
            if time.monotonic() > deadline:
                raise RuntimeError("Timed out waiting for legacy PicoScope block capture")
            time.sleep(0.0002)

        times = (ctypes.c_int32 * sample_count)()
        buffer_a = (ctypes.c_int16 * sample_count)() if "A" in active_channels else None
        buffer_b = (ctypes.c_int16 * sample_count)() if "B" in active_channels else None
        overflow = ctypes.c_int16()
        returned = ps.ps2000_get_times_and_values(
            opened.handle,
            ctypes.byref(times),
            ctypes.byref(buffer_a) if buffer_a is not None else None,
            ctypes.byref(buffer_b) if buffer_b is not None else None,
            None,
            None,
            ctypes.byref(overflow),
            opened.legacy_time_units,
            sample_count,
        )
        if returned <= 0:
            raise RuntimeError(f"ps2000_get_times_and_values failed with status {returned}")

        logger.debug(
            "ps2000 capture complete serial=%s returned_samples=%s overflow=%s",
            serial,
            returned,
            overflow.value,
        )

        if buffer_a is None and buffer_b is None:
            raise RuntimeError("Legacy PicoScope capture returned no buffer for selected channel")
        if returned > 1:
            delta_ns = self._legacy_units_to_ns(times[1] - times[0], opened.legacy_time_units or 0)
        else:
            delta_ns = opened.sample_interval_ns

        out: dict[str, WaveformWindow] = {}
        if buffer_a is not None:
            adc_a = np.ctypeslib.as_array(buffer_a)[:returned].astype(np.float32)
            volts_a = (adc_a / 32767.0) * opened.range_v
            out["A"] = WaveformWindow(
                device_serial=opened.device.serial,
                bus_name=opened.device.bus_name,
                channel="A",
                sample_rate_hz=max(1, int(round(1_000_000_000 / delta_ns))),
                sample_interval_ns=delta_ns,
                window_ms=max(1, int(round((returned * delta_ns) / 1_000_000))),
                started_at_us=unix_us_now(),
                values_v=volts_a.tolist(),
            )
        if buffer_b is not None:
            adc_b = np.ctypeslib.as_array(buffer_b)[:returned].astype(np.float32)
            volts_b = (adc_b / 32767.0) * opened.range_v
            out["B"] = WaveformWindow(
                device_serial=opened.device.serial,
                bus_name=opened.device.bus_name,
                channel="B",
                sample_rate_hz=max(1, int(round(1_000_000_000 / delta_ns))),
                sample_interval_ns=delta_ns,
                window_ms=max(1, int(round((returned * delta_ns) / 1_000_000))),
                started_at_us=unix_us_now(),
                values_v=volts_b.tolist(),
            )
        return out

    def capture_window(self, serial: str, sample_rate_hz: int, window_ms: int) -> WaveformWindow:
        opened = self._opened.get(serial)
        if opened is None:
            raise RuntimeError(f"PicoScope device {serial} is not opened")

        captured = self.capture_windows(serial, sample_rate_hz, window_ms, channels=[opened.device.channel])
        if opened.device.channel in captured:
            return captured[opened.device.channel]
        return next(iter(captured.values()))
