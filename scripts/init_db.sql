CREATE TABLE IF NOT EXISTS waveform_windows (
    id BIGSERIAL PRIMARY KEY,
    device_serial TEXT NOT NULL,
    bus_name TEXT NOT NULL,
    channel TEXT NOT NULL,
    sample_rate_hz INTEGER NOT NULL,
    sample_interval_ns INTEGER NOT NULL,
    window_ms INTEGER NOT NULL,
    started_at_us BIGINT NOT NULL,
    payload_json JSONB NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_waveform_windows_bus_time
ON waveform_windows (bus_name, started_at_us DESC);

CREATE INDEX IF NOT EXISTS idx_waveform_windows_device_time
ON waveform_windows (device_serial, started_at_us DESC);
