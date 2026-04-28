from __future__ import annotations

import math
import random

import numpy as np

from config import DeviceConfig
from models import WaveformWindow, unix_us_now


def generate_window(
    device: DeviceConfig,
    sample_rate_hz: int,
    window_ms: int,
) -> WaveformWindow:
    sample_count = int(sample_rate_hz * (window_ms / 1000.0))
    t = np.linspace(0.0, window_ms / 1000.0, sample_count, endpoint=False)

    # A synthetic CAN-like differential waveform with noise and occasional ringing.
    bit_freq = 125_000
    square = np.sign(np.sin(2 * math.pi * bit_freq * t))
    noise = np.random.normal(0.0, 0.03, sample_count)
    ringing = 0.08 * np.sin(2 * math.pi * 2_000_000 * t)
    spike = np.zeros(sample_count)
    if random.random() < 0.08:
        idx = random.randint(0, max(sample_count - 50, 1))
        spike[idx : idx + 50] = np.hanning(50) * 0.3

    values = (1.25 + 0.85 * square + noise + ringing + spike).astype(np.float32)

    return WaveformWindow(
        device_serial=device.serial,
        bus_name=device.bus_name,
        channel=device.channel,
        sample_rate_hz=sample_rate_hz,
        sample_interval_ns=int(1_000_000_000 / sample_rate_hz),
        window_ms=window_ms,
        started_at_us=unix_us_now(),
        values_v=values.tolist(),
    )
