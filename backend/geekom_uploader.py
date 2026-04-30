"""Geekom A6 database uploader.

Compresses and POSTs BusWaveformWindow data to the Geekom A6 ingest endpoint
over Tailscale.  Runs the HTTP POST in a background thread so the capture loop
is never blocked.

Wire format — one JSON object per bus window:
  [
    {
      "time_utc":      "2026-04-29T15:30:00.123456+00:00",
      "bus_id":        1,
      "n_samples":     1563,
      "samples_h_b64": "<base64(lz4(int16 CAN-H))",
      "samples_l_b64": "<base64(lz4(int16 CAN-L))"
    }
  ]

Both sides agree that sample_rate = _WIRE_SAMPLE_RATE (1 562 500 Hz).
The Geekom API splits each object into two DB rows (channel='H' and 'L').

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

# Fixed sample rate agreed between CANlogger and CANdatabase.
# Not sent over the wire — the Geekom API hardcodes this on insert.
_WIRE_SAMPLE_RATE = 1_562_500


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


# One persistent HTTP session per worker thread — avoids TCP reconnect overhead.
_thread_session = threading.local()


def _get_session() -> requests.Session:
    """Return (or create) the per-thread requests.Session with keep-alive."""
    if not hasattr(_thread_session, "session"):
        s = requests.Session()
        # Increase pool size to handle bursts without connection cycling.
        adapter = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=4)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _thread_session.session = s
    return _thread_session.session


def _upload_batch(windows: list[BusWaveformWindow], ingest_url: str, post_timeout_s: int) -> bool:
    """Build and POST a batch of combined H+L bus-window objects."""
    payload = []
    for window in windows:
        if not window.can_h_values_v and not window.can_l_values_v:
            continue
        bus_id = int(window.bus_name.split("_")[-1])
        t_utc = datetime.fromtimestamp(window.started_at_us / 1_000_000, tz=timezone.utc)
        n_samples = max(len(window.can_h_values_v), len(window.can_l_values_v))
        payload.append({
            "time_utc":      t_utc.isoformat(),
            "bus_id":        bus_id,
            "n_samples":     n_samples,
            "samples_h_b64": _pack_samples(window.can_h_values_v) if window.can_h_values_v else "",
            "samples_l_b64": _pack_samples(window.can_l_values_v) if window.can_l_values_v else "",
        })
    if not payload:
        return True
    try:
        session = _get_session()
        resp = session.post(ingest_url, json=payload, timeout=post_timeout_s)
        resp.raise_for_status()
        logger.debug("Geekom batch upload OK n=%d inserted=%s", len(payload), resp.json().get("inserted"))
        return True
    except requests.RequestException as exc:
        logger.warning("Geekom batch upload failed n=%d — %s", len(payload), exc)
        return False


class GeekomAsyncUploader:
    """Non-blocking uploader that POSTs capture windows to the Geekom A6 database.

    The HTTP POST is performed in a background daemon thread so the capture
    loop is never blocked.  Windows dropped when the queue is full are logged
    as warnings but capture continues uninterrupted.
    """

    def __init__(self, ingest_url: str, post_timeout_s: int, max_queue: int, num_workers: int = 3, batch_size: int = 10) -> None:
        self._ingest_url = ingest_url
        self._post_timeout_s = post_timeout_s
        self._num_workers = num_workers
        self._batch_size = batch_size
        self._q: queue.Queue[BusWaveformWindow | None] = queue.Queue(maxsize=max_queue)
        self._threads: list[threading.Thread] = [
            threading.Thread(target=self._worker, name=f"geekom-uploader-{i}", daemon=True)
            for i in range(num_workers)
        ]

    def start(self) -> None:
        for t in self._threads:
            t.start()
        logger.info("Geekom async uploader started url=%s workers=%d batch_size=%d", self._ingest_url, self._num_workers, self._batch_size)

    def stop(self) -> None:
        for _ in self._threads:
            self._q.put(None)  # one sentinel per worker
        for t in self._threads:
            t.join(timeout=15)
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
            # Block until at least one window is available.
            item = self._q.get()
            if item is None:
                break
            batch = [item]
            # Drain up to (batch_size - 1) more windows without blocking.
            for _ in range(self._batch_size - 1):
                try:
                    extra = self._q.get_nowait()
                    if extra is None:
                        # Sentinel reached — put it back and stop after this batch.
                        self._q.put(None)
                        break
                    batch.append(extra)
                except queue.Empty:
                    break
            _upload_batch(batch, self._ingest_url, self._post_timeout_s)
