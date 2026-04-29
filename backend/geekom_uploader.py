"""Geekom A6 database uploader.

Compresses and POSTs BusWaveformWindow data to the Geekom A6 ingest endpoint
over Tailscale.  Runs the HTTP POST in a background thread so the capture loop
is never blocked.

Usage (from capture_service.py):
    uploader = GeekomAsyncUploader(settings)
    uploader.start()
    uploader.enqueue(combined_bus_window)   # non-blocking
    uploader.stop()
"""
from __future__ import annotations

import base64
import logging
import queue
import threading
from datetime import datetime, timezone

import lz4.frame
import numpy as np
import requests

from models import BusWaveformWindow

logger = logging.getLogger(__name__)

# ADC full-scale used during voltage conversion in the PicoScope driver.
# Both ps2000a and ps2000 paths use 32767 as the denominator.
_MAX_ADC = 32767
_RANGE_V = 5.0  # volts full-scale (PS2000A_5V range)


def _volts_to_int16(values_v: list[float]) -> np.ndarray:
    """Convert float voltage array back to little-endian int16 ADC counts."""
    arr = np.asarray(values_v, dtype=np.float32)
    adc = np.round(arr / _RANGE_V * _MAX_ADC).clip(-32768, 32767).astype("<i2")
    return adc


def _pack_samples(values_v: list[float]) -> str:
    """LZ4-compress int16 ADC counts and return as base64 ASCII string."""
    adc = _volts_to_int16(values_v)
    compressed = lz4.frame.compress(adc.tobytes())
    return base64.b64encode(compressed).decode("ascii")


def _build_chunk(channel: str, t_utc: datetime, sample_rate: int, values_v: list[float], bus_id: int) -> dict:
    return {
        "time_utc": t_utc.isoformat(),
        "bus_id": bus_id,
        "channel": channel,
        "sample_rate": sample_rate,
        "n_samples": len(values_v),
        "samples_b64": _pack_samples(values_v),
    }


def _upload(window: BusWaveformWindow, ingest_url: str, bus_id: int, post_timeout_s: int) -> bool:
    """Build and POST both CAN-H and CAN-L chunks for one window."""
    t_utc = datetime.fromtimestamp(window.started_at_us / 1_000_000, tz=timezone.utc)
    chunks = []
    if window.can_h_values_v:
        chunks.append(_build_chunk("H", t_utc, window.sample_rate_hz, window.can_h_values_v, bus_id))
    if window.can_l_values_v:
        chunks.append(_build_chunk("L", t_utc, window.sample_rate_hz, window.can_l_values_v, bus_id))
    if not chunks:
        return True

    try:
        resp = requests.post(ingest_url, json=chunks, timeout=post_timeout_s)
        resp.raise_for_status()
        logger.debug(
            "Geekom upload OK bus=%s t_utc=%s inserted=%s",
            window.bus_name,
            t_utc.isoformat(),
            resp.json().get("inserted"),
        )
        return True
    except requests.RequestException as exc:
        logger.warning("Geekom upload failed bus=%s — %s", window.bus_name, exc)
        return False


class GeekomAsyncUploader:
    """Non-blocking uploader that POSTs capture windows to the Geekom A6 database.

    The HTTP POST is performed in a background daemon thread so the capture
    loop is never blocked.  Windows dropped when the queue is full are logged
    as warnings but capture continues uninterrupted.
    """

    def __init__(self, ingest_url: str, bus_id: int, post_timeout_s: int, max_queue: int) -> None:
        self._ingest_url = ingest_url
        self._bus_id = bus_id
        self._post_timeout_s = post_timeout_s
        self._q: queue.Queue[BusWaveformWindow | None] = queue.Queue(maxsize=max_queue)
        self._thread = threading.Thread(target=self._worker, name="geekom-uploader", daemon=True)

    def start(self) -> None:
        self._thread.start()
        logger.info("Geekom async uploader started url=%s bus_id=%s", self._ingest_url, self._bus_id)

    def stop(self) -> None:
        self._q.put(None)  # sentinel
        self._thread.join(timeout=15)
        logger.info("Geekom async uploader stopped")

    def enqueue(self, window: BusWaveformWindow) -> None:
        try:
            self._q.put_nowait(window)
        except queue.Full:
            logger.warning(
                "Geekom upload queue full — window dropped bus=%s started_at_us=%s",
                window.bus_name,
                window.started_at_us,
            )

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            _upload(item, self._ingest_url, self._bus_id, self._post_timeout_s)
