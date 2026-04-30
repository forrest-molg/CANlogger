<script>
  import { onMount } from "svelte";
  import {
    Radio,
    Cpu,
    PlayCircle,
    StopCircle,
    Activity,
    Clock3,
    Layers,
    HardDrive,
    GitBranch,
    ScanLine,
    RotateCcw,
    Wifi,
    WifiOff,
  } from "lucide-svelte";

  let mode = "SIMULATOR";
  let running = false;
  let uptimeS = 0;
  let storageQueue = 0;
  let streams = [];

  $: modeLive = mode === "PICOSCOPE";
  $: modeLabel = mode === "PICOSCOPE" ? "LIVE" : mode === "SIMULATOR" ? "SIM" : mode;

  let busy = false;
  let alert = null;
  let pollTimer = null;
  let snapshot = null;
  let snapshotBusy = false;
  let rescanBusy = false;

  let dbTestBusy = false;
  let dbTestResult = null; // null | { ok, http_status, latency_ms, error, ingest_url }

  const API_TIMEOUT_MS = 10000;

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

  function chartPoints(values, maxPoints = 720) {
    if (!Array.isArray(values) || values.length === 0) return [];
    const step = Math.max(1, Math.ceil(values.length / maxPoints));
    const reduced = [];
    for (let i = 0; i < values.length; i += step) reduced.push(values[i]);
    return reduced;
  }

  function chartPath(values, width = 640, height = 130, pad = 8, maxPoints = 720, yMin = null, yMax = null) {
    const points = chartPoints(values, maxPoints);
    if (points.length < 2) return "";

    let min, max;
    if (yMin !== null && yMax !== null) {
      min = yMin;
      max = yMax;
    } else {
      min = points[0];
      max = points[0];
      for (const v of points) {
        if (v < min) min = v;
        if (v > max) max = v;
      }
    }
    const range = Math.max(max - min, 1e-9);
    const plotW = width - pad * 2;
    const plotH = height - pad * 2;

    let d = "";
    for (let i = 0; i < points.length; i += 1) {
      const x = pad + (i / (points.length - 1)) * plotW;
      const y = pad + (1 - (points[i] - min) / range) * plotH;
      d += `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)} `;
    }
    return d.trim();
  }

  function voltageRange(values) {
    if (!Array.isArray(values) || values.length === 0) return "-";
    let min = values[0];
    let max = values[0];
    for (const v of values) {
      if (v < min) min = v;
      if (v > max) max = v;
    }
    return `${min.toFixed(2)}V to ${max.toFixed(2)}V`;
  }

  function voltageRangeDual(highValues, lowValues) {
    const all = [...(Array.isArray(highValues) ? highValues : []), ...(Array.isArray(lowValues) ? lowValues : [])];
    if (all.length === 0) return "-";
    let min = all[0];
    let max = all[0];
    for (const v of all) {
      if (v < min) min = v;
      if (v > max) max = v;
    }
    return `${min.toFixed(2)}V to ${max.toFixed(2)}V`;
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

  function applyStatus(status) {
    running = Boolean(status.running);
    mode = String(status.mode || "simulator").toUpperCase();
    uptimeS = Number(status.uptime_s || 0);
    storageQueue = Number(status.storage_queue || 0);
    streams = Array.isArray(status.streams) ? status.streams : [];
  }

  async function loadConfig() {
    await apiFetch("/api/config");
    // config is fixed server-side; no UI fields to populate
  }

  async function startCapture() {
    busy = true;
    try {
      // Send no overrides — server uses config/default.yaml settings (1ms/1ms, free-run)
      const status = await apiFetch("/api/capture/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
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

  async function testDbConnection() {
    dbTestBusy = true;
    dbTestResult = null;
    try {
      dbTestResult = await apiFetch("/api/geekom/test");
    } catch (err) {
      dbTestResult = { ok: false, http_status: null, latency_ms: null, error: err.message || String(err), ingest_url: "" };
    } finally {
      dbTestBusy = false;
    }
  }

  async function rescanDevices() {
    rescanBusy = true;
    try {
      const status = await apiFetch("/api/devices/rescan", { method: "POST" });
      applyStatus(status);
      showAlert("USB rescan complete.", "success");
    } catch (err) {
      showAlert(err.message || String(err), "error");
    } finally {
      rescanBusy = false;
    }
  }

  async function captureSingleWindow() {
    snapshotBusy = true;
    try {
      const data = await apiFetch("/api/capture/snapshot", {
        method: "POST",
      });
      snapshot = data;
      showAlert("Single-window capture complete.", "success");
      await pollStatus();
    } catch (err) {
      showAlert(err.message || String(err), "error");
    } finally {
      snapshotBusy = false;
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
    <div class={`badge mode-badge ${modeLive ? "live" : ""}`}><Cpu size={12} /> {modeLabel}</div>
    <div class={`badge status-badge ${running ? "running" : ""}`}>
      <span class="status-dot"></span>
      {statusLabel}
    </div>
  </div>
</header>

<main class="app-main">
  <section class="neo-panel">
    <div class="panel-header">
      <h2>Capture Control</h2>
      <span class="panel-header-meta">1 562 500 Hz · 1 ms · free-run</span>
    </div>

    <div class="button-row">
      <button class="neo-btn neo-btn-primary" on:click={startCapture} disabled={running || busy}>
        <PlayCircle size={15} />
        Start Capture
      </button>

      <button class="neo-btn neo-btn-accent" on:click={captureSingleWindow} disabled={running || busy || snapshotBusy}>
        <ScanLine size={15} />
        Capture 1 Window
      </button>

      <button class="neo-btn neo-btn-danger" on:click={stopCapture} disabled={!running || busy}>
        <StopCircle size={15} />
        Stop
      </button>
    </div>

    <div class="button-row db-test-row">
      <button class="neo-btn neo-btn-dbtest" on:click={testDbConnection} disabled={dbTestBusy}>
        {#if dbTestBusy}
          <Wifi size={15} />
          Testing…
        {:else}
          <Wifi size={15} />
          Test DB Connection
        {/if}
      </button>

      {#if dbTestResult !== null}
        <span class={`db-test-pill ${dbTestResult.ok ? 'ok' : 'fail'}`}>
          {#if dbTestResult.ok}
            <Wifi size={12} />
            CONNECTED · {dbTestResult.latency_ms} ms · HTTP {dbTestResult.http_status}
          {:else}
            <WifiOff size={12} />
            FAILED · {dbTestResult.error || `HTTP ${dbTestResult.http_status}`}
          {/if}
        </span>
        {#if dbTestResult.ingest_url}
          <span class="db-test-url">{dbTestResult.ingest_url}</span>
        {/if}
      {/if}
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
      <button
        class="neo-btn neo-btn-rescan"
        on:click={rescanDevices}
        disabled={running || rescanBusy}
        title="Re-enumerate attached PicoScopes (stop capture first)">
        <RotateCcw size={14} />
        {rescanBusy ? "Scanning…" : "Rescan USB"}
      </button>
    </div>

    <div class="streams-grid">
      {#each streams as stream (`${stream.device_serial}-${stream.bus_name}`)}
        <article class={`stream-card ${stream.state === "ERROR" ? "error" : stream.state === "ACTIVE" ? "active" : stream.state === "OFFLINE" ? "offline" : ""}`}>
          <div class="stream-top">
            <span class="stream-bus-name">{stream.bus_name}</span>
            <span class={`stream-status-pill ${stream.state === "ERROR" ? "error" : stream.state === "ACTIVE" ? "active" : stream.state === "OFFLINE" ? "offline" : ""}`}>
              <span class="pill-dot"></span>
              {stream.state}
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

  <section class="neo-panel">
    <div class="panel-header">
      <ScanLine class="panel-icon" size={17} />
      <h2>Single Window Verification</h2>
      {#if snapshot}
        <span class="panel-header-meta">Captured: {fmtTimestampUs(snapshot.captured_at_us)}</span>
      {/if}
    </div>

    {#if !snapshot}
      <p class="snapshot-empty">Run Capture 1 Window to acquire and view one waveform window per bus.</p>
    {:else}
      <div class="snapshot-grid">
        {#each snapshot.streams as result (`${result.device_serial}-${result.bus_name}`)}
          <article class={`snapshot-card ${result.state === "ERROR" ? "error" : result.state === "ACTIVE" ? "active" : result.state === "OFFLINE" ? "offline" : ""}`}>
            <div class="snapshot-top">
              <span class="stream-bus-name">{result.bus_name}</span>
              <span class={`stream-status-pill ${result.state === "ERROR" ? "error" : result.state === "ACTIVE" ? "active" : result.state === "OFFLINE" ? "offline" : ""}`}>
                <span class="pill-dot"></span>
                {result.state}
              </span>
            </div>

            {#if result.window}
              <div class="snapshot-meta">
                <span>{fmtHz(result.window.sample_rate_hz)}</span>
                <span>{fmtNum(Math.max(result.window.can_h_values_v.length, result.window.can_l_values_v.length))} samples</span>
                <span>{voltageRangeDual(result.window.can_h_values_v, result.window.can_l_values_v)}</span>
              </div>

              <svg class="wave-svg" viewBox="0 0 640 130" role="img" aria-label={`Waveform for ${result.bus_name}`}>
                <path class="wave-h" d={chartPath(result.window.can_h_values_v, 640, 130, 8, result.window.can_h_values_v.length, 0, 5)} />
                <path class="wave-l" d={chartPath(result.window.can_l_values_v, 640, 130, 8, result.window.can_l_values_v.length, 0, 5)} />
              </svg>
            {:else}
              <div class="snapshot-no-wave">No waveform captured for this bus.</div>
            {/if}

            {#if result.last_error}
              <div class="stream-error">{result.last_error}</div>
            {/if}
          </article>
        {/each}
      </div>
    {/if}
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
