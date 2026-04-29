# CANlogger

## Install on Any Linux PC (Ubuntu / Debian)

Run these commands in order on a fresh machine. Nothing else is required.

### Step 1 — Base packages

```bash
sudo apt update
sudo apt install -y curl ca-certificates gnupg lsb-release git
```

### Step 2 — Tailscale (remote access + SSH over your private network)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
```

A login URL is printed — open it in a browser, sign in, and approve the machine.
Confirm it joined your network:

```bash
tailscale ip -4
tailscale status
```

### Step 3 — Docker Engine and Docker Compose plugin

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
newgrp docker
docker --version
docker compose version
```

### Step 4 — Install CANlogger

```bash
curl -fsSL https://raw.githubusercontent.com/forrest-molg/CANlogger/main/install.sh \
  | bash -s -- --repo forrest-molg/CANlogger --tag latest
```

The installer downloads the release bundle, places CANlogger in `/opt/CANlogger`, and
enables a systemd service so the app starts automatically on every boot.

### Step 5 — Verify the app is running

```bash
sudo systemctl status canlogger --no-pager
docker ps
curl -s http://localhost:8000/api/capture/status
```

Open in browser:

- GUI: http://localhost:8000
- API docs: http://localhost:8000/docs

---

### Day-to-day service commands

```bash
sudo systemctl status canlogger    # show current status and recent logs
sudo systemctl restart canlogger   # restart the app (e.g. after config changes)
sudo systemctl stop canlogger      # stop
sudo systemctl start canlogger     # start
sudo journalctl -u canlogger -f    # stream live logs
```

### Install a specific version

Replace `latest` with a tag name to pin to an exact release:

```bash
curl -fsSL https://raw.githubusercontent.com/forrest-molg/CANlogger/main/install.sh \
  | bash -s -- --repo forrest-molg/CANlogger --tag v1.0.0
```

### Install from a local clone (developer workflow)

```bash
git clone https://github.com/forrest-molg/CANlogger.git
cd CANlogger
./install.sh --local
```

### Publish a new release to GitHub

Tag and push — the GitHub Actions workflow builds the bundle and uploads it automatically:

```bash
git tag v1.0.0
git push origin v1.0.0
```

CANlogger is a transferable multi-stream waveform logger designed for a mini PC that will run continuously for weeks. This first implementation provides:

- 5 parallel stream workers (PicoScope mode implemented, simulator still available)
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

- Simulator-based end-to-end pipeline for validation
- PicoScope 2204A block capture path via `picosdk` + `libps2000a`
- API and GUI for setup and runtime monitoring
- Dockerized app + PostgreSQL service
- Local spool persistence in JSONL windows

Planned next (hardware-on-desk phase):

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

Current single-device bring-up (CAN H on channel A):

1. Keep mode in config/default.yaml as:

```yaml
mode: picoscope
```

2. Enable `CAN_BUS_1` on channel `A` and set `CAN_BUS_2..5` to `enabled: false`.
3. Start with `./start.sh` and confirm stream states in GUI/API:

- `CAN_BUS_1`: `ACTIVE` (or `ERROR` with diagnostic text)
- `CAN_BUS_2..5`: `OFFLINE`

4. When adding more scopes, assign real serial numbers and enable those buses.

If Pico import fails in Docker, the image now installs `libps2000a` from Pico's apt repository and sets `LD_LIBRARY_PATH=/opt/picoscope/lib`.
The container also bind-mounts `/dev/bus/usb` and `/run/udev` so Pico's Linux driver can see live USB topology correctly.

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
- [x] Real PicoScope API capture implementation
- [ ] Long-duration endurance tuning with hardware attached
