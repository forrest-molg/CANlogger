from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import orjson
import psycopg
from psycopg.rows import dict_row

from config import StorageSettings
from models import BusWaveformWindow


class WindowStorage:
    def __init__(self, settings: StorageSettings) -> None:
        self._settings = settings
        self._queue: asyncio.Queue[BusWaveformWindow] = asyncio.Queue(maxsize=5000)
        self._runner: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()

    async def start(self) -> None:
        self._shutdown.clear()
        self._runner = asyncio.create_task(self._run(), name="window-storage")

    async def stop(self) -> None:
        self._shutdown.set()
        if self._runner:
            await self._runner
            self._runner = None

    async def enqueue(self, window: BusWaveformWindow) -> None:
        await self._queue.put(window)

    def queue_size(self) -> int:
        return self._queue.qsize()

    async def _run(self) -> None:
        while not self._shutdown.is_set() or not self._queue.empty():
            batch: list[BusWaveformWindow] = []
            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                batch.append(first)
            except TimeoutError:
                continue

            while len(batch) < self._settings.upload_batch_windows and not self._queue.empty():
                batch.append(self._queue.get_nowait())

            if self._settings.spool_enabled:
                self._write_spool_batch(batch)
            if self._settings.enable_postgres_upload:
                await self._upload_postgres_batch(batch)

            for _ in batch:
                self._queue.task_done()

    def _write_spool_batch(self, batch: list[BusWaveformWindow]) -> None:
        now = datetime.now(tz=timezone.utc)
        day_dir = self._settings.spool_dir / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        file_path = day_dir / f"windows-{now.strftime('%H')}.jsonl"

        with file_path.open("ab") as fh:
            for window in batch:
                fh.write(orjson.dumps(window.model_dump()))
                fh.write(b"\n")

    async def _upload_postgres_batch(self, batch: list[BusWaveformWindow]) -> None:
        await asyncio.to_thread(self._upload_postgres_sync, batch)

    def _upload_postgres_sync(self, batch: list[BusWaveformWindow]) -> None:
        rows = [
            {
                "device_serial": w.device_serial,
                "bus_name": w.bus_name,
                "channel": "AB",
                "sample_rate_hz": w.sample_rate_hz,
                "sample_interval_ns": w.sample_interval_ns,
                "window_ms": w.window_ms,
                "started_at_us": w.started_at_us,
                "payload": orjson.dumps({"can_h_values_v": w.can_h_values_v, "can_l_values_v": w.can_l_values_v}).decode("utf-8"),
            }
            for w in batch
        ]

        with psycopg.connect(self._settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO waveform_windows (
                        device_serial,
                        bus_name,
                        channel,
                        sample_rate_hz,
                        sample_interval_ns,
                        window_ms,
                        started_at_us,
                        payload_json
                    ) VALUES (
                        %(device_serial)s,
                        %(bus_name)s,
                        %(channel)s,
                        %(sample_rate_hz)s,
                        %(sample_interval_ns)s,
                        %(window_ms)s,
                        %(started_at_us)s,
                        %(payload)s
                    )
                    """,
                    rows,
                )
            conn.commit()
