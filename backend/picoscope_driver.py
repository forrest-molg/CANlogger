from __future__ import annotations

import ctypes
import logging
import time
from dataclasses import dataclass

import numpy as np

from config import DeviceConfig, StreamSettings
from models import WaveformWindow, unix_us_now


logger = logging.getLogger(__name__)


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
    def _legacy_units_to_ns(interval: int, units: int) -> int:
        multipliers = {
            0: 1,
            1: 1_000,
            2: 1_000_000,
            3: 1_000_000_000,
        }
        return int(interval * multipliers.get(units, 1))

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

            interval_ns = self._legacy_units_to_ns(time_interval.value, time_units.value)
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
        sample_count = max(1, int(sample_rate_hz * (window_ms / 1000.0)))
        target_interval_ns = 1_000_000_000 / sample_rate_hz

        # Derive trigger and pre-trigger parameters from stream config.
        trigger_enabled = stream.trigger_enabled if stream else True
        trigger_threshold_v = stream.trigger_threshold_v if stream else 3.2
        trigger_direction = stream.trigger_direction if stream else "falling"
        pre_trigger_pct = stream.trigger_pre_trigger_pct if stream else 10
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
                    5000,       # auto-trigger timeout ms (prevent infinite hang)
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
                    5000,  # auto-trigger timeout ms
                )
                if trig_status <= 0:
                    raise RuntimeError(f"ps2000_set_trigger failed with status {trig_status}")

            legacy_sample_count, timebase, sample_interval_ns, legacy_time_units = self._resolve_legacy_capture_geometry(
                ps, handle, sample_count, target_interval_ns
            )
            legacy_pre_trigger = int(legacy_sample_count * pre_trigger_pct / 100)

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
            )
            logger.info("Opened PicoScope with legacy ps2000 serial=%s", device.serial)
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

    def close_device(self, serial: str) -> None:
        opened = self._opened.pop(serial, None)
        if opened is None:
            return

        logger.debug("Closing PicoScope serial=%s driver=%s", serial, opened.driver_kind)

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
            deadline = time.monotonic() + 2.0
            while ready.value == 0:
                status = ps.ps2000aIsReady(opened.handle, ctypes.byref(ready))
                self._check_status(status, "ps2000aIsReady")
                if time.monotonic() > deadline:
                    raise RuntimeError("Timed out waiting for PicoScope block capture")
                time.sleep(0.001)

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

        deadline = time.monotonic() + 2.0
        while ps.ps2000_ready(opened.handle) == 0:
            if time.monotonic() > deadline:
                raise RuntimeError("Timed out waiting for legacy PicoScope block capture")
            time.sleep(0.001)

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
