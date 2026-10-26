import { DiscordSDK } from "https://cdn.jsdelivr.net/npm/@discord/embedded-app-sdk@1.3.0/+esm";

const $ = (selector) => document.querySelector(selector);
const alertBox = $("#alert");
let appId = "";
const moduleInfo = {
  overview: ["Overview", "◈"], system: ["System monitor", "⌁"], services: ["Services", "◉"],
  containers: ["Containers", "▦"], network: ["Network", "⌘"], assistant: ["AI assistant", "✳"]
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
function selectView(view) {
  const info = moduleInfo[view] || moduleInfo.overview;
  $("#page-title").textContent = info[0];
  document.querySelectorAll(".nav").forEach(button => button.classList.toggle("active", button.dataset.view === view));
  document.querySelectorAll("[data-panel]").forEach(panel => {
    panel.style.display = panel.dataset.panel === view ? "" : "none";
  });
  if (view === "overview") alert("Dashboard connected. Live host metrics are available; control actions are still read-only.");
  else if (view === "system") alert("Live system metrics are read-only.");
  else if (view === "services") alert("Service inventory is read-only. Start/stop/restart actions are not enabled yet.");
  else if (view === "containers") alert("Container inventory is read-only. Container actions are not enabled yet.");
  else if (view === "network") alert("Network interface details are read-only.");
  else alert("AI provider integration is not connected yet.");
}
document.querySelectorAll(".nav").forEach(button => button.addEventListener("click", () => selectView(button.dataset.view)));

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "include", ...options, headers: { "content-type": "application/json", ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
async function start() {
  try {
    // SDK initialization happens only when launched inside Discord.
    const config = await api("/api/config");
    appId = config.applicationId || "";
    const id = appId || new URLSearchParams(location.search).get("client_id") || "";
    if (!id) throw new Error("Set DISCORD_APPLICATION_ID on the Activity server.");
    const discordSdk = new DiscordSDK(id);
    await discordSdk.ready();
    const { code } = await discordSdk.commands.authorize({
      client_id: id,
      response_type: "code",
      prompt: "none",
      scope: ["identify"],
    });
    const result = await api("/api/session", { method: "POST", body: JSON.stringify({ code }) });
    const auth = await discordSdk.commands.authenticate({ access_token: result.access_token });
    if (!auth) throw new Error("Discord SDK authentication failed.");
    const user = result.user;
    $("#user-name").textContent = user.name || "Authorized user";
    $("#user-state").textContent = "Allowlist verified";
    const overview = await api("/api/overview");
    drawCards(overview.sections);
    selectView("overview");
    const [system, serviceData, containerData] = await Promise.all([api("/api/system"), api("/api/services"), api("/api/containers")]);
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
    $("#host-detail").textContent = `${system.hostname} · ${system.platform}/${system.architecture}`;
    $("#sampled-at").textContent = `Sampled ${new Date(system.sampledAt).toLocaleTimeString()} · interfaces: ${system.networkInterfaces.join(", ") || "none"}`;
    $("#processes").innerHTML = system.topProcesses.length ? system.topProcesses.map(proc => `<div class="process-row"><strong title="${escapeHtml(proc.name)}">${escapeHtml(proc.name)} <span style="text-align:left">#${proc.pid}</span></strong><span>CPU ${proc.cpuPercent}%</span><span>RAM ${proc.memoryPercent}%</span></div>`).join("") : '<p class="muted">Process list unavailable</p>';
    $("#services").innerHTML = serviceData.services.map(service => `<div class="inventory-row"><div><strong>${escapeHtml(service.name)}</strong><small>${escapeHtml(service.scope)} · ${escapeHtml(service.unit)}</small></div><span class="state state-${escapeHtml(service.state)}">${escapeHtml(service.state)}</span></div>`).join("");
    $("#containers").innerHTML = containerData.available ? (containerData.containers.length ? containerData.containers.map(item => `<div class="inventory-row"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.image)} · ${escapeHtml(item.id)}</small></div><span class="state state-active">${escapeHtml(item.status)}</span></div>`).join("") : '<p class="muted">No running containers detected.</p>') : `<p class="muted">Docker inventory unavailable: ${escapeHtml(containerData.detail)}</p>`;
    $("#interfaces").innerHTML = system.networkInterfaces.length ? system.networkInterfaces.map(name => `<span class="interface-chip">${escapeHtml(name)}</span>`).join("") : '<p class="muted">No active non-loopback interfaces detected.</p>';
    $("#bridge-title").textContent = overview.machine.state === "connected" ? "Host metrics connected" : "Waiting for the host";
    $("#bridge-detail").textContent = overview.machine.detail;
    const statusChip = document.querySelector(".status-chip");
    if (statusChip) { statusChip.innerHTML = '<span class="pulse"></span> READ ONLY'; statusChip.style.color = "var(--green)"; statusChip.style.borderColor = "#345b50"; }
    alert("Discord identity verified. Private dashboard ready.", "ok");
  } catch (error) {
    $("#user-state").textContent = "Session unavailable";
    alert(error.message || "Could not initialize Discord session.", "error");
    drawCards(Object.entries(moduleInfo).filter(([key]) => key !== "overview").map(([id, item]) => ({ id, title: item[0], description: "Available after Discord authentication and local bridge setup." })));
    selectView("overview");
  }
}
$("#logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST", body: "{}" }); }
  finally { location.reload(); }
});
start();
