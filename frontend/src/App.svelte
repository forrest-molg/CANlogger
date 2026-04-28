<script>
  import { onMount } from "svelte";
  import {
    Radio,
    Cpu,
    SlidersHorizontal,
    PlayCircle,
    StopCircle,
    Save,
    Activity,
    Clock3,
    Layers,
    HardDrive,
    GitBranch,
  } from "lucide-svelte";

  let sampleRate = 5000000;
  let windowMs = 10;
  let cadenceMs = 10;

  let mode = "SIMULATOR";
  let running = false;
  let uptimeS = 0;
  let storageQueue = 0;
  let streams = [];

  let busy = false;
  let alert = null;
  let pollTimer = null;

  const API_TIMEOUT_MS = 10000;

  $: sampleHint = `${Math.round(sampleRate / 125000)} samples / bit · ${fmtHz(sampleRate)}`;
  $: windowHint = `${fmtNum(Math.round((sampleRate * windowMs) / 1000))} pts / window`;
  $: cadenceHint = `${(1000 / Math.max(cadenceMs, 1)).toFixed(1)} windows / s`;

  $: statusLabel = running ? "RUNNING" : "IDLE";
  $: activeCount = streams.filter((s) => s.active).length;

  function fmtNum(value) {
    if (value === null || value === undefined) return "-";
    return Number(value).toLocaleString();
  }

  function fmtHz(hz) {
    if (hz >= 1000000) return `${(hz / 1000000).toFixed(1)} MS/s`;
    if (hz >= 1000) return `${(hz / 1000).toFixed(0)} kS/s`;
    return `${hz} S/s`;
  }

  function fmtUptime(seconds) {
    const s = Math.floor(seconds || 0);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return `${h}h ${m}m ${sec}s`;
    if (m > 0) return `${m}m ${sec}s`;
    return `${sec}s`;
  }

  function fmtTimestampUs(us) {
    if (!us) return "-";
    const d = new Date(us / 1000);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    const ms = String(d.getMilliseconds()).padStart(3, "0");
    return `${hh}:${mm}:${ss}.${ms}`;
  }

  function showAlert(message, kind = "success") {
    alert = { message, kind };
    setTimeout(() => {
      if (alert && alert.message === message) alert = null;
    }, 3500);
  }

  async function apiFetch(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), API_TIMEOUT_MS);

    try {
      const response = await fetch(path, {
        ...options,
        signal: controller.signal,
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || `Request failed (${response.status})`);
      }
      return await response.json();
    } finally {
      clearTimeout(timeout);
    }
  }

  function applyConfig(cfg) {
    sampleRate = cfg.stream.sample_rate_hz;
    windowMs = cfg.stream.window_ms;
    cadenceMs = cfg.stream.cadence_ms;
  }

  function applyStatus(status) {
    running = Boolean(status.running);
    mode = String(status.mode || "simulator").toUpperCase();
    uptimeS = Number(status.uptime_s || 0);
    storageQueue = Number(status.storage_queue || 0);
    streams = Array.isArray(status.streams) ? status.streams : [];
  }

  async function loadConfig() {
    const cfg = await apiFetch("/api/config");
    applyConfig(cfg);
  }

  async function saveConfig() {
    if (running) return;
    busy = true;
    try {
      const cfg = await apiFetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sample_rate_hz: Number(sampleRate),
          window_ms: Number(windowMs),
          cadence_ms: Number(cadenceMs),
        }),
      });
      applyConfig(cfg);
      showAlert("Config saved.", "success");
    } catch (err) {
      showAlert(err.message || String(err), "error");
    } finally {
      busy = false;
    }
  }

  async function startCapture() {
    busy = true;
    try {
      const status = await apiFetch("/api/capture/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sample_rate_hz: Number(sampleRate),
          window_ms: Number(windowMs),
          cadence_ms: Number(cadenceMs),
        }),
      });
      applyStatus({ ...status, storage_queue: storageQueue });
      showAlert("Capture started.", "success");
    } catch (err) {
      showAlert(err.message || String(err), "error");
    } finally {
      busy = false;
    }
  }

  async function stopCapture() {
    busy = true;
    try {
      const status = await apiFetch("/api/capture/stop", { method: "POST" });
      applyStatus({ ...status, storage_queue: 0 });
      showAlert("Capture stopped.", "success");
    } catch (err) {
      showAlert(err.message || String(err), "error");
    } finally {
      busy = false;
    }
  }

  async function pollStatus() {
    try {
      const status = await apiFetch("/api/status");
      applyStatus(status);
    } catch {
      // transient during container restarts; keep UI stable
    }
  }

  onMount(async () => {
    await loadConfig().catch(() => undefined);
    await pollStatus();
    pollTimer = setInterval(pollStatus, 1500);
    return () => {
      if (pollTimer) clearInterval(pollTimer);
    };
  });
</script>

<header class="app-header">
  <div class="header-brand">
    <div class="brand-icon"><Radio size={20} /></div>
    <div class="brand-text">
      <span class="brand-name">CANlogger</span>
      <span class="brand-sub">Multi-Stream CAN Waveform Logger</span>
    </div>
  </div>

  <div class="header-badges">
    <div class="badge mode-badge"><Cpu size={12} /> {mode}</div>
    <div class={`badge status-badge ${running ? "running" : ""}`}>
      <span class="status-dot"></span>
      {statusLabel}
    </div>
  </div>
</header>

<main class="app-main">
  <section class="neo-panel">
    <div class="panel-header">
      <SlidersHorizontal class="panel-icon" size={17} />
      <h2>Capture Control</h2>
    </div>

    <div class="settings-grid">
      <div class="setting-field">
        <label for="sampleRate">Sample Rate</label>
        <div class="input-wrap">
          <input id="sampleRate" class="neo-input" type="number" min="100000" max="10000000" step="500000" bind:value={sampleRate} disabled={running} />
          <span class="input-unit">Hz</span>
        </div>
        <p class="input-hint">{sampleHint}</p>
      </div>

      <div class="setting-field">
        <label for="windowMs">Window Length</label>
        <div class="input-wrap">
          <input id="windowMs" class="neo-input" type="number" min="1" max="1000" step="1" bind:value={windowMs} disabled={running} />
          <span class="input-unit">ms</span>
        </div>
        <p class="input-hint">{windowHint}</p>
      </div>

      <div class="setting-field">
        <label for="cadenceMs">Cadence</label>
        <div class="input-wrap">
          <input id="cadenceMs" class="neo-input" type="number" min="1" max="1000" step="1" bind:value={cadenceMs} disabled={running} />
          <span class="input-unit">ms</span>
        </div>
        <p class="input-hint">{cadenceHint}</p>
      </div>
    </div>

    <div class="button-row">
      <button class="neo-btn" on:click={saveConfig} disabled={running || busy}>
        <Save size={15} />
        Save Config
      </button>

      <div class="btn-spacer"></div>

      <button class="neo-btn neo-btn-primary" on:click={startCapture} disabled={running || busy}>
        <PlayCircle size={15} />
        Start Capture
      </button>

      <button class="neo-btn neo-btn-danger" on:click={stopCapture} disabled={!running || busy}>
        <StopCircle size={15} />
        Stop
      </button>
    </div>

    {#if alert}
      <div class={`alert-banner ${alert.kind}`}>{alert.message}</div>
    {/if}
  </section>

  <section class="neo-panel">
    <div class="panel-header">
      <Activity class="panel-icon" size={17} />
      <h2>Stream Monitor</h2>
      <span class="panel-header-meta">{activeCount} / {streams.length} active</span>
    </div>

    <div class="streams-grid">
      {#each streams as stream (stream.device_serial)}
        <article class={`stream-card ${stream.last_error ? "error" : stream.active ? "active" : ""}`}>
          <div class="stream-top">
            <span class="stream-bus-name">{stream.bus_name}</span>
            <span class={`stream-status-pill ${stream.last_error ? "error" : stream.active ? "active" : ""}`}>
              <span class="pill-dot"></span>
              {stream.last_error ? "ERROR" : stream.active ? "ACTIVE" : "IDLE"}
            </span>
          </div>

          <div class="stream-serial">{stream.device_serial}</div>

          <div class="stream-stats">
            <div class="stat-box">
              <span class="stat-label">Windows</span>
              <span class="stat-value accent">{fmtNum(stream.windows_captured)}</span>
            </div>
            <div class="stat-box">
              <span class="stat-label">Samples</span>
              <span class="stat-value">{fmtNum(stream.samples_captured)}</span>
            </div>
          </div>

          <div class="stream-timestamp">Last: {fmtTimestampUs(stream.last_window_started_at_us)}</div>

          {#if stream.last_error}
            <div class="stream-error">{stream.last_error}</div>
          {/if}
        </article>
      {/each}
    </div>
  </section>
</main>

<footer class="app-footer">
  <div class="footer-stat">
    <Clock3 size={13} />
    <span>Uptime</span>
    <strong>{fmtUptime(uptimeS)}</strong>
  </div>

  <div class="footer-stat">
    <Layers size={13} />
    <span>Storage Queue</span>
    <strong>{fmtNum(storageQueue)}</strong>
  </div>

  <div class="footer-stat">
    <HardDrive size={13} />
    <span>Spool</span>
    <strong>Local JSONL</strong>
  </div>

  <div class="footer-stat footer-stat-right">
    <GitBranch size={13} />
    <span>Version</span>
    <strong>v0.2.0</strong>
  </div>
</footer>
