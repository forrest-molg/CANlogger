async function getJson(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Request failed: ${res.status}`);
  }
  return res.json();
}

function setInputs(cfg) {
  document.getElementById("sampleRate").value = cfg.stream.sample_rate_hz;
  document.getElementById("windowMs").value = cfg.stream.window_ms;
  document.getElementById("cadenceMs").value = cfg.stream.cadence_ms;
}

function renderStatus(status) {
  const runtime = document.getElementById("runtime");
  const lines = [];
  lines.push(`<div><strong>Running:</strong> ${status.running}</div>`);
  lines.push(`<div><strong>Mode:</strong> ${status.mode}</div>`);
  lines.push(`<div><strong>Uptime:</strong> ${status.uptime_s.toFixed(1)} s</div>`);
  lines.push(`<div><strong>Storage Queue:</strong> ${status.storage_queue}</div>`);

  for (const stream of status.streams) {
    lines.push(`
      <div class="stream">
        <h3>${stream.bus_name} (${stream.device_serial})</h3>
        <div class="meta">active=${stream.active}, windows=${stream.windows_captured}, samples=${stream.samples_captured}</div>
        <div class="meta">last_window_started_at_us=${stream.last_window_started_at_us || "-"}</div>
        <div class="meta">last_error=${stream.last_error || "none"}</div>
      </div>
    `);
  }

  runtime.innerHTML = lines.join("\n");
}

async function loadConfig() {
  const cfg = await getJson("/api/config");
  setInputs(cfg);
}

function currentSettings() {
  return {
    sample_rate_hz: Number(document.getElementById("sampleRate").value),
    window_ms: Number(document.getElementById("windowMs").value),
    cadence_ms: Number(document.getElementById("cadenceMs").value),
  };
}

async function saveConfig() {
  const payload = currentSettings();
  await getJson("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function startCapture() {
  const payload = currentSettings();
  await getJson("/api/capture/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function stopCapture() {
  await getJson("/api/capture/stop", { method: "POST" });
}

async function pollStatus() {
  try {
    const status = await getJson("/api/status");
    renderStatus(status);
  } catch (err) {
    document.getElementById("runtime").textContent = String(err);
  }
}

window.addEventListener("DOMContentLoaded", async () => {
  document.getElementById("saveBtn").addEventListener("click", () => saveConfig().catch(alert));
  document.getElementById("startBtn").addEventListener("click", () => startCapture().catch(alert));
  document.getElementById("stopBtn").addEventListener("click", () => stopCapture().catch(alert));

  await loadConfig();
  await pollStatus();
  setInterval(pollStatus, 2000);
});
