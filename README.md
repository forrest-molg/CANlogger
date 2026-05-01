# CANlogger

CANlogger is the **capture daemon** in the CAN bus waveform recording system. It runs on any Linux PC with one or more PicoScope 2204A oscilloscopes attached via USB. It samples CAN-H and CAN-L continuously at 1.5625 MS/s (12.5 samples per CAN bit at 125 kbps), packages each 1 ms of waveform into a compressed chunk, and streams those chunks to the [CANdatabase backend](https://github.com/forrest-molg/candb) over HTTP in real time.

## System Context

```
 ┌─────────────────────────────────────────────────┐
 │  Capture machine (any Linux PC)                 │
 │                                                 │
 │  PicoScope 2204A ──USB──► canlogger-app         │
 │  (up to 5 scopes, 2 ch each)  (Docker)          │
 │        │                         │              │
 │        │ 1.5625 MS/s             │ POST /ingest │
 │        │ CAN-H + CAN-L           ▼              │
 └────────┼─────────────────────────┼──────────────┘
          │                         │  (same host or Tailscale)
          │                    ┌────▼───────────────────┐
          │                    │  candb (Geekom A6)     │
          │                    │  TimescaleDB + FastAPI │
          │                    └────────────────────────┘
          │                              │
          └──── raw CAN-H/L voltage      │  decoded + stored
                (not decoded here)       ▼
                                  canui waveform viewer
```

CANlogger is purely a capture transport — it does **no protocol decoding**. Decoding happens server-side in candb so the decoder can be improved without touching the capture hardware.

## What Is Implemented

- **PicoScope 2204A capture** via `picosdk` using the `ps2000` legacy API (with automatic ps2000a fallback). The `ps2000` streaming mode is used for gapless continuous capture — no re-arm dead time between 1 ms windows.
- **Automatic scope enumeration** at startup via `ps2000aEnumerateUnits`, falling back to legacy USB sysfs counting if ps2000a cannot enumerate. Scopes are sorted by serial number and assigned to bus slots 1–5 lowest-first.
- **Two channels per scope**: channel A = CAN-H, channel B = CAN-L. Both are captured simultaneously.
- **Up to 5 buses** (5 PicoScopes × 2 channels = 10 signal streams).
- **Geekom uploader**: each 1 ms window is LZ4-compressed, base64-encoded, and POSTed to candb in batches of 10. Three worker threads give ~1500 windows/s upload capacity with keep-alive HTTP sessions.
- **Local spool** (JSONL files) as an optional overflow buffer (disabled by default — at 1000 windows/s the disk fills in hours).
- **FastAPI control API** for start/stop, config read/write, stream status, and PicoScope diagnostics.
- **Svelte setup GUI** bundled into the container — accessible at port 8001.
- **Docker Compose packaging** for repeatable deployment on any Linux host.

## Performance & Data Rates

All figures use the default config (`sample_rate_hz: 1562500`, `window_ms: 1`, `cadence_ms: 1`, `channels_per_scope: 2`, `batch_size: 10`, `num_upload_workers: 3`).

### ADC sampling

| Parameter | Value | Derivation |
|---|---|---|
| Sample rate | **1,562,500 Hz** (1.5625 MS/s) | `stream.sample_rate_hz` |
| Samples per CAN bit (125 kbps) | **12.5** | 1,562,500 ÷ 125,000 |
| Samples per 1 ms window | **1,563** | 1,562,500 × 0.001 (rounded up) |
| Raw bytes per window (int16) | **3,126 bytes** | 1,563 × 2 |
| Channels per scope | **2** | CAN-H (ch A) + CAN-L (ch B) |
| Raw throughput per channel | **3.125 MB/s** | 1,562,500 × 2 bytes |
| Raw throughput per bus (2 ch) | **6.25 MB/s** | |
| Raw throughput, 5 buses | **31.25 MB/s** | |

### LZ4 compression (wire format)

Each 1 ms window is LZ4-compressed before sending. CAN signals are near-constant voltage (idle = recessive ≈ 0 V, active ≈ 3.5 V differential) with typical bus utilisation of 10–40 %. Observed compression ratios are **4× to 10×**.

| Bus utilisation | Approx LZ4 ratio | Compressed bytes/window/channel | KB/s per channel |
|---|---|---|---|
| Low (< 10 % bus load) | ~10× | ~310 bytes | ~310 KB/s |
| Typical (20–40 %) | ~5× | ~625 bytes | ~625 KB/s |
| Heavy (> 80 %) | ~2.5× | ~1,250 bytes | ~1,250 KB/s |

### Network: capture machine → candb (Tailscale or LAN)

Traffic is HTTP POST (`/ingest`) carrying JSON-wrapped base64 LZ4 chunks. Each POST contains `batch_size = 10` windows for one bus (both channels). HTTP keep-alive sessions are reused, so per-request overhead is ~200–400 bytes.

| Scenario | POSTs/s per bus | Payload MB/s per bus | Payload MB/s, 5 buses |
|---|---|---|---|
| Typical (5× LZ4) | 200 | **~1.3 MB/s** | **~6.5 MB/s** |
| Heavy (2.5× LZ4) | 200 | ~2.6 MB/s | ~13 MB/s |

**Upload capacity headroom** (3 workers × keep-alive, ~5 ms/POST → 200 POSTs/worker/s): 600 POSTs/s × 10 windows = **6,000 windows/s**, vs the 2,000 windows/s produced by one fully-loaded bus — **3× headroom per bus**. At 5 buses (10,000 windows/s) with `num_upload_workers: 3` you will want to increase workers or accept occasional queue growth during bursts.

### Upload queue sizing

`geekom.max_queue: 2000` — if candb is unreachable for up to ~0.1 s (at 10,000 windows/s, 5 buses) the queue absorbs the backlog without dropping windows. Older windows are dropped first when the queue is full.

## Hardware Requirements

| Item | Details |
|---|---|
| PicoScope 2204A | USB 2.0, firmware `2204A/060`, USB ID `0ce9:1007` |
| Host OS | Ubuntu 22.04 / Debian 12 or newer (64-bit) |
| Docker Engine | v24+ with Compose plugin |
| USB | Direct passthrough — no USB hub between scope and host |
| CAN bus | 125 kbps standard frame (ISO 11898-1) |

A udev rule (`scripts/99-picoscope.rules`) grants non-root access to PicoScope devices.

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

- GUI: http://localhost:8001
- API docs: http://localhost:8001/docs

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


## Project Structure

```
CANlogger/
├── backend/
│   ├── main.py              # FastAPI service: control API + GUI file server
│   ├── config.py            # Typed Pydantic config (load/save config/default.yaml)
│   ├── capture_service.py   # Parallel worker orchestration — one thread per bus channel
│   ├── picoscope_driver.py  # PicoScope 2204A driver (ps2000a + ps2000 legacy fallback)
│   ├── geekom_uploader.py   # HTTP upload queue: LZ4+base64 POSTs to candb /ingest
│   ├── storage.py           # Local JSONL spool writer (overflow buffer, normally off)
│   ├── diagnostics.py       # USB device enumeration and driver health reporting
│   └── models.py            # Shared Pydantic data models
├── config/
│   └── default.yaml         # Editable runtime configuration (survives container restarts)
├── frontend/
│   └── src/App.svelte       # Svelte control panel (start/stop, config, stream status)
├── scripts/
│   ├── 99-picoscope.rules   # udev rule: grant non-root USB access to PicoScope
│   └── init_db.sql          # Optional local PostgreSQL schema bootstrap
├── docker-compose.yml       # Two services: canlogger-app + canlogger-postgres
├── Dockerfile
├── start.sh / stop.sh
└── install.sh               # One-line installer from GitHub release bundle
```

## Quick Start

### 1. Install dependencies

```bash
sudo apt update
sudo apt install -y curl ca-certificates gnupg lsb-release git

# Docker Engine
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER && newgrp docker

# PicoScope udev rule (grants USB access without sudo)
sudo cp scripts/99-picoscope.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

### 2. Configure the candb ingest target

Edit `config/default.yaml` and set the address of the machine running candb:

```yaml
geekom:
  enabled: true
  ingest_url: http://<candb-host-ip>:8000/ingest
```

If both services run on the same machine, leave it as `host.docker.internal:8000/ingest`.

### 3. Start

```bash
./start.sh
```

Open the GUI at **http://localhost:8001** — click **Start Capture** to begin recording.

API docs: **http://localhost:8001/docs**

### 4. Stop

```bash
./stop.sh
```

## Configuration Reference

All settings live in `config/default.yaml`. Changes take effect after restarting the stack. Most fields can also be written live via `POST /api/config` while capture is stopped.

| Setting | Default | Description |
|---|---|---|
| `stream.sample_rate_hz` | `1562500` | ADC sample rate. 1.5625 MS/s = 12.5 samples/bit @ 125 kbps. |
| `stream.window_ms` | `1` | Length of each waveform chunk (ms). |
| `stream.cadence_ms` | `1` | Target interval between chunk starts — equal to window = gapless. |
| `stream.channels_per_scope` | `2` | 1 = CAN-H only, 2 = CAN-H + CAN-L. |
| `stream.streaming_mode` | `true` | Use ps2000 streaming API (gapless). Set `false` for block-mode snapshots. |
| `geekom.enabled` | `true` | Enable upload to candb. |
| `geekom.ingest_url` | `http://host.docker.internal:8000/ingest` | candb ingest endpoint. |
| `geekom.batch_size` | `10` | Windows per POST. |
| `geekom.num_upload_workers` | `3` | Parallel POST threads. |
| `storage.spool_enabled` | `false` | Write JSONL spool files locally (overflow buffer — fills disk fast). |
| `devices` | 5 × AUTO | One entry per channel. `serial: AUTO` = assigned by startup probe. |

### Sample Rate Guide (125 kbps CAN)

| `sample_rate_hz` | Samples per bit | Notes |
|---|---|---|
| `1000000` | 8 | Usable minimum |
| `1562500` | **12.5** | **Default — matches PicoScope 7 capture setting** |
| `2000000` | 16 | |
| `3125000` | 25 | |
| `5000000` | 40 | Highest accuracy; ~5 MB/s compressed per bus |

## API Reference

The FastAPI service runs on port 8000 inside the container (port 8001 on the host).

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/capture/status` | Stream states for all buses (ACTIVE / IDLE / ERROR / OFFLINE) |
| `POST` | `/api/capture/start` | Start capture on all enabled buses |
| `POST` | `/api/capture/stop` | Stop capture |
| `GET` | `/api/config` | Read current config |
| `POST` | `/api/config` | Update config (capture must be stopped) |
| `GET` | `/api/diagnostics` | PicoScope USB diagnostics |

## Troubleshooting

**Stream shows ERROR instead of ACTIVE**

```bash
docker logs canlogger-app --tail 50
# Look for: "ps2000a enumeration" or "ps2000 fallback" lines
curl http://localhost:8001/api/diagnostics
```

**PicoScope not detected**

- Confirm USB device is visible: `lsusb | grep 0ce9`
- Confirm udev rule is installed: `ls /etc/udev/rules.d/99-picoscope.rules`
- Try replugging the scope — the driver needs a 2-second USB settle delay on first connect.

**Upload queue backing up**

Check the candb machine is reachable: `curl http://<candb-ip>:8000/health`

## Developer Workflow

```bash
# Run outside Docker (no USB required — useful for API/GUI dev)
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
uvicorn main:app --host 0.0.0.0 --port 8000

# Frontend dev server
cd frontend
npm install && npm run dev
```

## Releasing a New Version

```bash
# On the develop branch, when ready to release:
git checkout main
git merge develop
git tag -a v1.1 -m "v1.1 release notes here"
git push origin main && git push origin v1.1
```

The install script at `install.sh` pulls the tagged bundle from GitHub releases.
