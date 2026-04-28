# CANlogger

CANlogger is a transferable multi-stream waveform logger designed for a mini PC that will run continuously for weeks. This first implementation provides:

- 5 parallel stream workers (simulator mode now, PicoScope mode scaffolded)
- Adjustable snapshot capture settings: sample rate, window duration, and cadence
- Precise microsecond window timestamps
- Durable local spool files for high-rate waveform windows
- Optional PostgreSQL upload path (feature flag)
- Svelte-based setup GUI for start/stop and config
- Docker Compose packaging for easy transfer to another machine

## Why 5 MS/s at 125 kbps

For CAN at 125 kbps, bit time is 8 microseconds.

- At 5 MS/s, sample period is 0.2 microseconds
- Samples per bit = 8 / 0.2 = 40 samples/bit

This provides strong signal-integrity visibility without pushing data rates as high as the hardware maximum.

## Current Scope

Implemented now:

- Simulator-based end-to-end pipeline to validate architecture before hardware arrives
- API and GUI for setup and runtime monitoring
- Dockerized app + PostgreSQL service
- Local spool persistence in JSONL windows

Planned next (hardware-on-desk phase):

- Replace simulator path with PicoSDK integration in backend/picoscope_driver.py
- Add live scope discovery and serial auto-mapping from USB
- Add reconnect handling tied to actual device behavior

## Project Structure

- backend/main.py: FastAPI service and control API
- backend/config.py: typed config load/save from config/default.yaml
- backend/capture_service.py: parallel worker orchestration
- backend/simulator.py: synthetic CAN-like waveform generation
- backend/storage.py: spool writer and optional PostgreSQL uploader
- backend/picoscope_driver.py: PicoScope integration contract stub
- config/default.yaml: editable runtime defaults for 5 buses
- frontend/src/App.svelte: control panel UI
- frontend/src/app.css: neumorphic dark theme
- frontend/src/main.js: Svelte entrypoint
- frontend/package.json: frontend dependency and build configuration
- scripts/init_db.sql: PostgreSQL schema bootstrap
- docker-compose.yml: portable deployment
- start.sh: start stack
- stop.sh: stop stack

## Quick Start

1. Open a terminal in Documents/CANlogger.
2. Start the stack:

```bash
chmod +x start.sh stop.sh
./start.sh
```

3. Open:

- GUI: http://localhost:8000
- API docs: http://localhost:8000/docs

4. In the GUI:

- Confirm sample rate/window/cadence
- Click Save Config
- Click Start Capture
- Watch per-stream counters increment

Note: the frontend is built automatically in Docker; no manual Node steps are required on the mini PC.

5. Stop stack when done:

```bash
./stop.sh
```

## Configuration

Main runtime settings are in config/default.yaml.

Key fields:

- stream.sample_rate_hz: default 5000000
- stream.window_ms: default 10
- stream.cadence_ms: default 10
- storage.enable_postgres_upload: default false
- devices: five buses with serial placeholders

To change settings while stopped, edit config/default.yaml.
To change settings in app, use GUI Save Config.

## Data Model (Current)

Each captured window contains:

- device serial
- bus name
- channel
- sample rate
- sample interval (ns)
- window length (ms)
- window start timestamp (us)
- voltage samples array

Spool output path (inside container): /data/spool/YYYY-MM-DD/windows-HH.jsonl

## PostgreSQL Notes

The schema is created by scripts/init_db.sql.

Raw waveform payload is currently written as JSONB when storage.enable_postgres_upload=true.
This is intentionally optional because high-rate raw insert volume can overwhelm a DB if always enabled.

Recommended production posture:

- Keep local spool as primary high-rate sink
- Upload selected/compacted windows to PostgreSQL asynchronously

## Run Without Docker (Developer Mode)

```bash
cd frontend
npm install
npm run build

cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e .
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Hardware Integration Plan

When your five PicoScope 2204A units arrive:

1. Install PicoSDK on the mini PC.
2. Implement capture methods in backend/picoscope_driver.py.
3. Switch mode in config/default.yaml:

```yaml
mode: picoscope
```

4. Map real serials in devices list.
5. Run 1-scope test, then 5-scope burn-in.

## Transfer to Mini PC

1. Copy or clone the CANlogger folder.
2. Install Docker and Docker Compose plugin.
3. Run ./start.sh.
4. Open GUI and verify status.

This gives repeatable setup on any compatible Linux host.

## Implementation Status

- [x] Project scaffold in Documents/CANlogger
- [x] Configurable 10 ms snapshot cadence (adjustable)
- [x] 5 parallel stream workers (simulated)
- [x] Microsecond timestamping
- [x] Setup GUI for initial deployment
- [x] Dockerized app + PostgreSQL
- [ ] Real PicoScope API capture implementation
- [ ] Long-duration endurance tuning with hardware attached
