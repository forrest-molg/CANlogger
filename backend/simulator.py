from __future__ import annotations

import math
import random

import numpy as np

from config import DeviceConfig, StreamSettings
from models import WaveformWindow, unix_us_now


# CAN 2.0 standard frame bit pattern in simulator voltage notation:
# 1 = dominant (CANH ~3.5V), 0 = recessive (CANH ~2.5V)
# Represents: SOF | ID=0x18A (11b) | RTR | IDE | r0 | DLC=8 | 8 data bytes | CRC(15b)+delim | ACK+delim | EOF(7b)
# Bus encoding inversion: CAN logical 0 (dominant) → sim 1 (high V), CAN logical 1 (recessive) → sim 0 (low V)
_CAN_FRAME_BITS = [
    # SOF: dominant → 1
    1,
    # 11-bit ID 0x18A = 0b00110001010 → CAN bits MSB first: 0,0,1,1,0,0,0,1,0,1,0 → sim: 1,1,0,0,1,1,1,0,1,0,1
    1, 1, 0, 0, 1, 1, 1, 0, 1, 0, 1,
    # RTR=0 (data frame, dominant) → 1
    1,
    # IDE=0 (standard frame, dominant) → 1
    1,
    # r0=0 dominant → 1
    1,
    # DLC=8 = 1000; CAN bits: 1,0,0,0 → sim: 0,1,1,1
    0, 1, 1, 1,
    # Data byte 1: 0xAA = 10101010 → sim: 0,1,0,1,0,1,0,1
    0, 1, 0, 1, 0, 1, 0, 1,
    # Data byte 2: 0x55 = 01010101 → sim: 1,0,1,0,1,0,1,0
    1, 0, 1, 0, 1, 0, 1, 0,
    # Data byte 3: 0xDE = 11011110 → sim: 0,0,1,0,0,0,0,1
    0, 0, 1, 0, 0, 0, 0, 1,
    # Data byte 4: 0xAD = 10101101 → sim: 0,1,0,1,0,0,1,0
    0, 1, 0, 1, 0, 0, 1, 0,
    # Data byte 5: 0xBE = 10111110 → sim: 0,1,0,0,0,0,0,1
    0, 1, 0, 0, 0, 0, 0, 1,
    # Data byte 6: 0xEF = 11101111 → sim: 0,0,1,0,0,0,0,0
    0, 0, 1, 0, 0, 0, 0, 0,
    # Data byte 7: 0x12 = 00010010 → sim: 1,1,1,0,1,1,0,1
    1, 1, 1, 0, 1, 1, 0, 1,
    # Data byte 8: 0x34 = 00110100 → sim: 1,1,0,0,1,0,1,1
    1, 1, 0, 0, 1, 0, 1, 1,
    # CRC (15 bits, plausible alternating pattern) → sim: 0,1,0,1,0,1,0,1,0,1,0,1,0,1,0
    0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0,
    # CRC delimiter: recessive → 0
    0,
    # ACK slot: dominant → 1; ACK delimiter: recessive → 0
    1, 0,
    # EOF: 7 recessive bits → 0
    0, 0, 0, 0, 0, 0, 0,
    # IFS: 3 recessive bits
    0, 0, 0,
]


def generate_window(
    device: DeviceConfig,
    sample_rate_hz: int,
    window_ms: int,
    stream: StreamSettings | None = None,
) -> WaveformWindow:
    sample_count = int(sample_rate_hz * (window_ms / 1000.0))
    t = np.linspace(0.0, window_ms / 1000.0, sample_count, endpoint=False)

    pre_pct = stream.trigger_pre_trigger_pct if stream else 10
    pre_samples = int(sample_count * pre_pct / 100)

    # Voltages measured from real PicoScope captures (Test1_PicoWave.csv):
    #   CAN_H: recessive ~2.54V, dominant ~3.53V
    #   CAN_L: recessive ~2.54V, dominant ~1.55V (inverted differential)
    is_can_l = getattr(device, 'channel', 'A') == 'B'
    if is_can_l:
        dominant_v = 1.55   # CANL dominant ~1.5V
        recessive_v = 2.54  # CANL recessive ~2.5V
    else:
        dominant_v = 3.53   # CANH dominant ~3.5V
        recessive_v = 2.54  # CANH recessive ~2.5V

    # 125 kbps → 25 samples/bit at 3.125 MHz; scales automatically with sample_rate_hz
    bit_freq = 125_000
    samples_per_bit = max(1, int(sample_rate_hz / bit_freq))

    values = np.full(sample_count, recessive_v, dtype=np.float32)

    # Pre-trigger: idle (recessive) for most, SOF (dominant) in last samples_per_bit before trigger
    sof_start = max(0, pre_samples - samples_per_bit)
    values[:sof_start] = recessive_v
    values[sof_start:pre_samples] = dominant_v  # SOF dominant bit ending at trigger point

    # Post-trigger: frame bits starting after SOF (trigger fired on SOF falling edge)
    # Skip the SOF entry in _CAN_FRAME_BITS since it was placed in pre-trigger
    post_bits = _CAN_FRAME_BITS[1:]  # everything after SOF
    pos = pre_samples
    for bit in post_bits:
        end = min(pos + samples_per_bit, sample_count)
        values[pos:end] = dominant_v if bit == 1 else recessive_v
        pos = end
        if pos >= sample_count:
            break
    if pos < sample_count:
        values[pos:] = recessive_v  # idle after frame

    # Realistic noise (~20mV RMS) and ringing on transitions
    noise = np.random.normal(0.0, 0.020, sample_count).astype(np.float32)
    ringing = (0.05 * np.sin(2 * math.pi * 2_000_000 * t)).astype(np.float32)
    edge_mask = np.zeros(sample_count, dtype=np.float32)
    for i in range(1, sample_count):
        if abs(float(values[i]) - float(values[i - 1])) > 0.1:
            edge_mask[max(0, i - 5):min(sample_count, i + 20)] = 1.0
    ringing *= edge_mask

    values = values + noise + ringing

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

