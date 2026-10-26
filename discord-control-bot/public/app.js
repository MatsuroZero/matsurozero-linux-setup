import { DiscordSDK } from "@discord/embedded-app-sdk";
const $ = (selector) => document.querySelector(selector);
const alertBox = $("#alert");
let appId = "";
let modelCatalog = null;
let savedFacts = [];

let liveRefreshTimer = null;
let liveRefreshInFlight = false;

async function refreshLiveMetrics() {
  if (liveRefreshInFlight || document.visibilityState === "hidden") return;
  liveRefreshInFlight = true;
  try {
    const [system, network] = await Promise.all([api("/api/system"), api("/api/network")]);
    $("#cpu-value").textContent = system.cpu.usagePercent + "%";
    $("#cpu-detail").textContent = system.cpu.cores + " logical cores · load " + system.cpu.loadAverage.join(" / ");
    $("#cpu-meter").style.width = system.cpu.usagePercent + "%";
    $("#memory-value").textContent = system.memory.usedPercent + "%";
    $("#memory-detail").textContent = system.memory.used.label + " of " + system.memory.total.label;
    $("#memory-meter").style.width = system.memory.usedPercent + "%";
    $("#disk-value").textContent = system.disk ? system.disk.usedPercent + "%" : "N/A";
    $("#disk-detail").textContent = system.disk ? system.disk.free.label + " free of " + system.disk.total.label : "Disk metrics unavailable";
    $("#disk-meter").style.width = system.disk ? system.disk.usedPercent + "%" : "0%";
    $("#uptime-value").textContent = system.uptime;
    $("#temperature-value").textContent = system.temperatures?.maxCelsius == null ? "N/A" : system.temperatures.maxCelsius + "°C";
    $("#temperature-detail").textContent = system.temperatures?.available ? system.temperatures.sensors.length + " sensor(s) detected" : "No readable thermal sensors";
    $("#host-detail").textContent = system.hostname + " · " + system.platform + "/" + system.architecture;
    $("#sampled-at").textContent = "Updated " + new Date(system.sampledAt).toLocaleTimeString() + " · interfaces: " + (system.networkInterfaces.join(", ") || "none");
    $("#processes").innerHTML = system.topProcesses.length ? system.topProcesses.map(proc => '<div class="process-row"><strong title="' + escapeHtml(proc.name) + '">' + escapeHtml(proc.name) + ' <span style="text-align:left">#' + proc.pid + '</span></strong><span>CPU ' + proc.cpuPercent + '%</span><span>RAM ' + proc.memoryPercent + '%</span></div>').join("") : '<p class="muted">Process list unavailable</p>';
    $("#interfaces").innerHTML = system.networkInterfaces.length ? system.networkInterfaces.map(name => '<span class="interface-chip">' + escapeHtml(name) + '</span>').join("") : '<p class="muted">No active non-loopback interfaces detected.</p>';
    $("#network-traffic").innerHTML = network.available && network.interfaces.length ? network.interfaces.map(item => '<div class="network-row"><strong>' + escapeHtml(item.name) + '</strong><span>↓ ' + escapeHtml(item.rxRate) + '</span><span>↑ ' + escapeHtml(item.txRate) + '</span></div>').join("") : '<p class="muted">Network throughput is unavailable on this host.</p>';
  } catch {
    // Keep the last good snapshot visible if a background refresh fails.
  } finally {
    liveRefreshInFlight = false;
  }
}
const moduleInfo = {
  overview: ["Overview", "◈"], system: ["System monitor", "⌁"], services: ["Services", "◉"],
  containers: ["Containers", "▦"], network: ["Network", "⌘"], assistant: ["AI assistant", "✳"], links: ["Dashboards", "↗"], registry: ["Nexus commands", "⌘"], diagnostics: ["Diagnostics", "✓"], power: ["Power controls", "⏻"]
};
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}
function alert(message, kind = "") {
  alertBox.textContent = message;
  alertBox.className = `alert ${kind}`;
}
function drawCards(sections) {
  const symbols = { system: "⌁", services: "◉", containers: "▦", network: "⌘", assistant: "✳" };
  $("#cards").innerHTML = sections.map(item => `
    <article class="module-card" data-view="${item.id}" tabindex="0" role="button" aria-label="Open ${item.title}">
      <div class="module-icon">${symbols[item.id] || "◈"}</div>
      <h4>${item.title}</h4><p>${item.description}</p>
      <div class="card-bottom"><span>OPEN MODULE</span><b>↗</b></div>
    </article>`).join("");
  document.querySelectorAll(".module-card").forEach(card => {
    card.addEventListener("click", () => selectView(card.dataset.view));
    card.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") selectView(card.dataset.view); });
  });
}
function renderServices(services) {
  $("#services").innerHTML = services.map(service => {
    const actions = service.state === "active" ? ["restart", "stop"] : ["start"];
    if (["enabled", "disabled"].includes(service.autostart)) actions.push(service.autostart === "enabled" ? "disable" : "enable");
    return `<div class="inventory-row"><div><strong>${escapeHtml(service.name)}</strong><small>${escapeHtml(service.scope)} · ${escapeHtml(service.unit)}</small><small>Autostart: ${escapeHtml(service.autostart || "unknown")}</small></div><div class="inventory-actions"><span class="state state-${escapeHtml(service.state)}">${escapeHtml(service.state)}</span>${actions.map(action => `<button class="action-button" data-service-id="${escapeHtml(service.id)}" data-action="${action}">${action}</button>`).join("")}</div></div>`;
  }).join("");
  document.querySelectorAll("[data-service-id]").forEach(button => button.addEventListener("click", async () => {
    const serviceId = button.dataset.serviceId, action = button.dataset.action;
    if (["stop", "restart", "enable", "disable"].includes(action) && !window.confirm(`Confirm ${action} for ${serviceId}?`)) return;
    button.disabled = true;
    try {
      await api("/api/services/action", { method: "POST", body: JSON.stringify({ serviceId, action }) });
      alert(`Requested ${action} for ${serviceId}.`, "ok");
      renderServices((await api("/api/services")).services);
    } catch (error) { alert(error.message, "error"); }
    finally { button.disabled = false; }
  }));
}
function renderProjects(projects) {
  $("#projects").innerHTML = projects.map(project => {
    const actions = project.available ? ["start", "restart", "stop"] : [];
    return `<div class="inventory-row"><div><strong>${escapeHtml(project.name)}</strong><small>${escapeHtml(project.id)} · ${escapeHtml(project.state)}</small><small>${project.containers.length} container(s)</small></div><div class="inventory-actions">${actions.map(action => `<button class="action-button" data-project-id="${escapeHtml(project.id)}" data-action="${action}">${action}</button>`).join("")}<button class="action-button" data-project-logs="${escapeHtml(project.id)}" ${project.available ? "" : "disabled"}>logs</button></div></div>`;
  }).join("");
  document.querySelectorAll("[data-project-id]").forEach(button => button.addEventListener("click", async () => {
    const projectId = button.dataset.projectId, action = button.dataset.action;
    if (["stop", "restart"].includes(action) && !window.confirm(`Confirm ${action} for project ${projectId}?`)) return;
    button.disabled = true;
    try {
      await api("/api/projects/action", { method: "POST", body: JSON.stringify({ projectId, action }) });
      alert(`Requested ${action} for ${projectId}.`, "ok");
      renderProjects((await api("/api/projects")).projects);
    } catch (error) { alert(error.message, "error"); }
    finally { button.disabled = false; }
  }));
  document.querySelectorAll("[data-project-logs]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const result = await api("/api/projects/logs", { method: "POST", body: JSON.stringify({ projectId: button.dataset.projectLogs, lines: 100 }) });
      $("#logs-output").textContent = result.output;
    } catch (error) { $("#logs-output").textContent = error.message; }
    finally { button.disabled = false; }
  }));
}
function renderContainers(data) {
  $("#containers").innerHTML = data.available ? (data.containers.length ? data.containers.map(item => {
    const running = /^Up\b/i.test(item.status);
    const actions = running ? ["restart", "stop"] : ["start"];
    return `<div class="inventory-row"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.image)} · ${escapeHtml(item.id)}</small><small>${escapeHtml(item.status)}</small></div><div class="inventory-actions">${actions.map(action => `<button class="action-button" data-container-id="${escapeHtml(item.id)}" data-action="${action}">${action}</button>`).join("")}<button class="action-button" data-container-logs="${escapeHtml(item.id)}">logs</button></div></div>`;
  }).join("") : '<p class="muted">No containers detected.</p>') : `<p class="muted">Docker inventory unavailable: ${escapeHtml(data.detail)}</p>`;
  document.querySelectorAll("[data-container-logs]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const result = await api("/api/containers/logs", { method: "POST", body: JSON.stringify({ containerId: button.dataset.containerLogs, lines: 100 }) });
      $("#logs-output").textContent = result.output;
    } catch (error) { $("#logs-output").textContent = error.message; }
    finally { button.disabled = false; }
  }));
  document.querySelectorAll("[data-container-id]").forEach(button => button.addEventListener("click", async () => {
    const containerId = button.dataset.containerId, action = button.dataset.action;
    if (!window.confirm(`Confirm ${action} for container ${containerId}?`)) return;
    button.disabled = true;
    try {
      await api("/api/containers/action", { method: "POST", body: JSON.stringify({ containerId, action }) });
      alert(`Requested ${action} for container ${containerId}.`, "ok");
      renderContainers(await api("/api/containers"));
    } catch (error) { alert(error.message, "error"); }
    finally { button.disabled = false; }
  }));
}
async function refreshDiagnostics() {
  $("#health-results").innerHTML = '<p class="muted">Checking endpoints…</p>';
  $("#doctor-results").innerHTML = '<p class="muted">Running local checks…</p>';
  try {
    const [health, doctor] = await Promise.all([api("/api/health/details"), api("/api/doctor")]);
    $("#health-summary").textContent = `HTTP endpoints: ${health.summary.ok} responding, ${health.summary.failed} failed · Docker: ${health.docker.detail}`;
    $("#health-results").innerHTML = health.endpoints.map(item => `<div class="inventory-row"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.url)}</small></div><span class="state state-${item.ok ? "active" : "failed"}">${item.status == null ? escapeHtml(item.detail) : `HTTP ${item.status}`}</span></div>`).join("");
    $("#doctor-results").innerHTML = doctor.checks.map(item => `<div class="inventory-row"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.detail)}</small></div><span class="state state-${item.state === "ok" ? "active" : "failed"}">${escapeHtml(item.state)}</span></div>`).join("");
  } catch (error) {
    $("#health-summary").textContent = error.message;
  }
}
$("#refresh-diagnostics").addEventListener("click", refreshDiagnostics);
function renderRegistry(data) {
  $("#registry-path").textContent = `Registry: ${data.path || "not found"}`;
  $("#saved-commands").innerHTML = data.commands.length ? data.commands.map(item => `<div class="inventory-row"><div><strong>${escapeHtml(item.name)}</strong><small>Executable: ${escapeHtml(item.executable)}</small></div><button class="action-button" data-run-command="${escapeHtml(item.name)}">Run</button></div>`).join("") : '<p class="muted">No saved commands found in the Nexus registry.</p>';
  $("#saved-profiles").innerHTML = data.profiles.length ? data.profiles.map(item => `<div class="inventory-row"><div><strong>${escapeHtml(item.name)}</strong><small>${item.steps} step(s)</small></div><button class="action-button" data-run-profile="${escapeHtml(item.name)}">Run profile</button></div>`).join("") : '<p class="muted">No saved profiles found in the Nexus registry.</p>';
  document.querySelectorAll("[data-run-command]").forEach(button => button.addEventListener("click", async () => {
    const name = button.dataset.runCommand;
    if (!window.confirm(`Run saved Nexus command "${name}"? Review the command in your local Nexus registry first.`)) return;
    button.disabled = true;
    $("#registry-output").textContent = `Running ${name}…`;
    try {
      const result = await api("/api/commands/run", { method: "POST", body: JSON.stringify({ name }) });
      $("#registry-output").textContent = result.output;
    } catch (error) { $("#registry-output").textContent = error.message; }
    finally { button.disabled = false; }
  }));
  document.querySelectorAll("[data-run-profile]").forEach(button => button.addEventListener("click", async () => {
    const name = button.dataset.runProfile;
    if (!window.confirm(`Run all steps in Nexus profile "${name}"? This can change services and execute saved commands.`)) return;
    button.disabled = true;
    $("#registry-output").textContent = `Running profile ${name}…`;
    try {
      const result = await api("/api/profiles/run", { method: "POST", body: JSON.stringify({ name }) });
      $("#registry-output").textContent = result.results.map(item => `[${item.ok ? "OK" : "FAIL"}] ${item.label}\n${item.output}`).join("\n\n");
    } catch (error) { $("#registry-output").textContent = error.message; }
    finally { button.disabled = false; }
  }));
}
function renderDashboards(dashboards) {
  $("#dashboard-links").innerHTML = dashboards.map(item => {
    const href = item.url ? `<a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">Open ↗</a>` : '<span class="muted">Invalid URL configuration</span>';
    return `<div class="dashboard-row"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.url || "URL unavailable")}</small></div>${href}</div>`;
  }).join("");
}
$("#links-pin-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  if (button) button.disabled = true;
  $("#credential-results").textContent = "Verifying PIN…";
  try {
    const result = await api("/api/links/credentials", { method: "POST", body: JSON.stringify({ pin: $("#links-pin").value }) });
    $("#links-pin").value = "";
    $("#credential-results").innerHTML = result.credentials.map((item, index) => `<div class="credential-row"><strong>${escapeHtml(item.name)}</strong><small>Username</small><code>${escapeHtml(item.username)}</code><small>Password</small><code>${escapeHtml(item.password)}</code></div>`).join("");
  } catch (error) { $("#credential-results").textContent = error.message; $("#links-pin").value = ""; }
  finally { if (button) button.disabled = false; }
});
$("#hide-credentials").addEventListener("click", () => {
  $("#credential-results").textContent = "";
  $("#links-pin").value = "";
});
function renderMemoryFacts(facts) {
  savedFacts = Array.isArray(facts) ? facts : [];
  $("#memory-facts").innerHTML = savedFacts.length ? savedFacts.map((fact, index) => `<div class="memory-fact"><span>${escapeHtml(fact)}</span><button class="action-button" data-memory-index="${index}" type="button">Forget</button></div>`).join("") : '<p class="muted">No saved facts yet.</p>';
  document.querySelectorAll("[data-memory-index]").forEach(button => button.addEventListener("click", async () => {
    const fact = savedFacts[Number(button.dataset.memoryIndex)];
    if (!fact || !window.confirm("Forget this saved fact?")) return;
    button.disabled = true;
    try {
      const result = await api("/api/ai/memory/forget", { method: "POST", body: JSON.stringify({ query: fact }) });
      renderMemoryFacts(result.memory.facts);
      alert(result.removed ? "Saved fact forgotten." : "No matching fact found.", "ok");
    } catch (error) { alert(error.message, "error"); }
    finally { button.disabled = false; }
  }));
}
$("#memory-profile-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  if (button) button.disabled = true;
  try {
    await api("/api/ai/memory/profile", { method: "POST", body: JSON.stringify({ profile: $("#memory-profile").value }) });
    alert("Persistent profile saved.", "ok");
  } catch (error) { alert(error.message, "error"); }
  finally { if (button) button.disabled = false; }
});
$("#memory-fact-form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = $("#memory-fact");
  const fact = input.value.trim();
  if (!fact) return;
  const button = event.submitter;
  if (button) button.disabled = true;
  try {
    const result = await api("/api/ai/memory/remember", { method: "POST", body: JSON.stringify({ fact }) });
    input.value = "";
    renderMemoryFacts(result.facts);
    alert("Fact saved to persistent memory.", "ok");
  } catch (error) { alert(error.message, "error"); }
  finally { if (button) button.disabled = false; }
});
function populateAiModels() {
  const provider = $("#ai-provider").value;
  const info = modelCatalog?.providers?.[provider];
  const models = info?.models || [];
  $("#ai-model").innerHTML = "";
  if (!models.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = info?.available ? "No models found" : (provider === "ollama" ? "Ollama unavailable" : "Gemini not configured");
    $("#ai-model").append(option);
    $("#ai-model").disabled = true;
    $("#chat-send").disabled = true;
    return;
  }
  for (const model of models) {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = model;
    $("#ai-model").append(option);
  }
  $("#ai-model").disabled = false;
  $("#chat-send").disabled = false;
}
function appendChatMessage(role, text) {
  const empty = document.querySelector(".chat-empty");
  if (empty) empty.remove();
  const item = document.createElement("article");
  item.className = `chat-message chat-${role}`;
  const label = document.createElement("span");
  label.textContent = role === "user" ? "YOU" : role === "assistant" ? "NEXUS" : "STATUS";
  const body = document.createElement("p");
  body.textContent = text;
  item.append(label, body);
  $("#chat-messages").append(item);
  $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
  return body;
}
document.querySelectorAll("[data-power-action]").forEach(button => button.addEventListener("click", async () => {
  const action = button.dataset.powerAction;
  if (!window.confirm(`Are you sure you want to ${action === "shutdown" ? "shut down" : action} the CachyOS computer? This can disconnect the Activity.`)) return;
  button.disabled = true;
  try {
    const result = await api("/api/power/action", { method: "POST", body: JSON.stringify({ action }) });
    alert(result.description + " requested.", "ok");
  } catch (error) { alert(error.message, "error"); }
  finally { button.disabled = false; }
}));
$("#ai-provider").addEventListener("change", populateAiModels);
$("#chat-form").addEventListener("submit", async event => {
  event.preventDefault();
  const message = $("#chat-input").value.trim();
  const provider = $("#ai-provider").value;
  const model = $("#ai-model").value;
  if (!message || !model) return;
  appendChatMessage("user", message);
  $("#chat-input").value = "";
  $("#chat-send").disabled = true;
  const pending = appendChatMessage("status", "Thinking…");
  try {
    const result = await api("/api/ai/chat", { method: "POST", body: JSON.stringify({ provider, model, message }) });
    pending.parentElement.remove();
    appendChatMessage("assistant", result.answer);
  } catch (error) {
    pending.textContent = error.message || "The AI request failed.";
  } finally {
    $("#chat-send").disabled = !$("#ai-model").value;
  }
});
function selectView(view) {
  const info = moduleInfo[view] || moduleInfo.overview;
  $("#page-title").textContent = info[0];
  document.querySelectorAll(".nav").forEach(button => button.classList.toggle("active", button.dataset.view === view));
  if (view !== "links") {
    $("#credential-results").textContent = "";
    $("#links-pin").value = "";
  }
  document.querySelectorAll("[data-panel]").forEach(panel => {
    panel.style.display = panel.dataset.panel === view ? "" : "none";
  });
  if (view === "overview") alert("Host metrics are live. Service/container actions are allowlisted; AI chat works when a provider is configured. Power controls require explicit confirmation.");
  else if (view === "system") alert("Live system metrics are read-only. Service and container controls are separate panels.");
  else if (view === "services") alert("Service actions are allowlisted and may require host permissions.");
  else if (view === "containers") alert("Container actions are restricted to containers in the host inventory.");
  else if (view === "network") alert("Network interface details and sampled throughput are read-only.");
  else if (view === "links") alert("Dashboard URLs are local-network links. Credentials require the configured server PIN.");
  else if (view === "registry") alert("Only pre-registered Nexus commands and profiles are available. Each run requires confirmation.");
  else if (view === "diagnostics") alert("Health checks and doctor results are read-only snapshots of the local host.");
  else if (view === "power") alert("Power actions affect the host and require explicit confirmation.");
  else alert("Choose an available Ollama or Gemini model. Chat history is kept for this server session.");
}
document.querySelectorAll(".nav").forEach(button => button.addEventListener("click", () => selectView(button.dataset.view)));

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "include", ...options, headers: { "content-type": "application/json", ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
function withTimeout(promise, ms, message) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(message)), ms);
    }),
  ]).finally(() => clearTimeout(timer));
}

async function start() {
  try {
    // SDK initialization happens only when launched inside Discord.
    const config = await api("/api/config");
    appId = config.applicationId || "";
    const id = appId || new URLSearchParams(location.search).get("client_id") || "";
    if (!id) throw new Error("Set DISCORD_APPLICATION_ID on the Activity server.");
    alert("Loading Discord SDK…");
    const sdkModule = await withTimeout(
      Promise.resolve({ DiscordSDK }),
      12000,
      "Discord SDK import timed out. Check the embedded browser connection."
    );
    const discordSdk = new sdkModule.DiscordSDK(id);
    alert("Connecting to Discord…");
    await withTimeout(
      discordSdk.ready(),
      12000,
      "Discord SDK connection timed out. Close and relaunch the Activity."
    );
    alert("Requesting Discord authorization…");
    const { code } = await withTimeout(discordSdk.commands.authorize({
      client_id: id,
      response_type: "code",
      prompt: "none",
      scope: ["identify"],
    }), 12000, "Discord authorization timed out. Relaunch the Activity from Discord.");
    const result = await api("/api/session", { method: "POST", body: JSON.stringify({ code }) });
    const auth = await discordSdk.commands.authenticate({ access_token: result.access_token });
    if (!auth) throw new Error("Discord SDK authentication failed.");
    const user = result.user;
    $("#user-name").textContent = user.name || "Authorized user";
    $("#user-state").textContent = "Allowlist verified";
    const overview = await api("/api/overview");
    drawCards(overview.sections);
    selectView("overview");
    const [system, serviceData, containerData, projectData, models, network, memory, links, registry] = await Promise.all([api("/api/system"), api("/api/services"), api("/api/containers"), api("/api/projects"), api("/api/ai/models"), api("/api/network"), api("/api/ai/memory").catch(() => ({ profile: "", facts: [] })), api("/api/links"), api("/api/registry")]);
    modelCatalog = models;
    populateAiModels();
    $("#memory-profile").value = memory.profile || "";
    renderMemoryFacts(memory.facts || []);
    renderDashboards(links.dashboards || []);
    renderRegistry(registry);
    $("#cpu-value").textContent = `${system.cpu.usagePercent}%`;
    $("#cpu-detail").textContent = `${system.cpu.cores} logical cores · load ${system.cpu.loadAverage.join(" / ")}`;
    $("#cpu-meter").style.width = `${system.cpu.usagePercent}%`;
    $("#memory-value").textContent = `${system.memory.usedPercent}%`;
    $("#memory-detail").textContent = `${system.memory.used.label} of ${system.memory.total.label}`;
    $("#memory-meter").style.width = `${system.memory.usedPercent}%`;
    $("#disk-value").textContent = system.disk ? `${system.disk.usedPercent}%` : "N/A";
    $("#disk-detail").textContent = system.disk ? `${system.disk.free.label} free of ${system.disk.total.label}` : "Disk metrics unavailable";
    $("#disk-meter").style.width = system.disk ? `${system.disk.usedPercent}%` : "0%";
    $("#uptime-value").textContent = system.uptime;
    $("#temperature-value").textContent = system.temperatures?.maxCelsius == null ? "N/A" : `${system.temperatures.maxCelsius}°C`;
    $("#temperature-detail").textContent = system.temperatures?.available ? `${system.temperatures.sensors.length} sensor(s) detected` : "No readable thermal sensors";

    $("#host-detail").textContent = `${system.hostname} · ${system.platform}/${system.architecture}`;
    $("#sampled-at").textContent = `Sampled ${new Date(system.sampledAt).toLocaleTimeString()} · interfaces: ${system.networkInterfaces.join(", ") || "none"}`;
    $("#processes").innerHTML = system.topProcesses.length ? system.topProcesses.map(proc => `<div class="process-row"><strong title="${escapeHtml(proc.name)}">${escapeHtml(proc.name)} <span style="text-align:left">#${proc.pid}</span></strong><span>CPU ${proc.cpuPercent}%</span><span>RAM ${proc.memoryPercent}%</span></div>`).join("") : '<p class="muted">Process list unavailable</p>';
    renderServices(serviceData.services);
    renderProjects(projectData.projects);
    renderContainers(containerData);
    $("#interfaces").innerHTML = system.networkInterfaces.length ? system.networkInterfaces.map(name => `<span class="interface-chip">${escapeHtml(name)}</span>`).join("") : '<p class="muted">No active non-loopback interfaces detected.</p>';
    $("#network-traffic").innerHTML = network.available && network.interfaces.length ? network.interfaces.map(item => `<div class="network-row"><strong>${escapeHtml(item.name)}</strong><span>↓ ${escapeHtml(item.rxRate)}</span><span>↑ ${escapeHtml(item.txRate)}</span></div>`).join("") : '<p class="muted">Network throughput is unavailable on this host.</p>';
    $("#bridge-title").textContent = overview.machine.state === "connected" ? "Host metrics connected" : "Waiting for the host";
    $("#bridge-detail").textContent = overview.machine.detail;
    const statusChip = document.querySelector(".status-chip");
    if (statusChip) { statusChip.innerHTML = '<span class="pulse"></span> ALLOWLISTED'; statusChip.style.color = "var(--green)"; statusChip.style.borderColor = "#345b50"; }
    alert("Discord identity verified. Private dashboard ready.", "ok");
    if (!liveRefreshTimer) liveRefreshTimer = setInterval(refreshLiveMetrics, 10000);
  } catch (error) {
    $("#user-state").textContent = "Session unavailable";
    drawCards(Object.entries(moduleInfo).filter(([key]) => key !== "overview").map(([id, item]) => ({ id, title: item[0], description: "Available after Discord authentication and local bridge setup." })));
    selectView("overview");
    alert(error.message || "Could not initialize Discord session.", "error");
  }
}
$("#logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST", body: "{}" }); }
  finally { location.reload(); }
});
start();
