import { DiscordSDK } from "https://cdn.jsdelivr.net/npm/@discord/embedded-app-sdk@1.3.0/+esm";

const $ = (selector) => document.querySelector(selector);
const alertBox = $("#alert");
let appId = "";
const moduleInfo = {
  overview: ["Overview", "◈"], system: ["System monitor", "⌁"], services: ["Services", "◉"],
  containers: ["Containers", "▦"], network: ["Network", "⌘"], assistant: ["AI assistant", "✳"]
};
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
      <div class="card-bottom"><span>BRIDGE PENDING</span><b>↗</b></div>
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
  if (view === "overview") alert("Dashboard connected. Machine bridge is not configured yet.");
  else alert(`${info[0]} module selected. Live data and controls will activate when the authenticated CachyOS bridge is connected.`);
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
      state: "",
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
    const system = await api("/api/system");
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
    $("#processes").innerHTML = system.topProcesses.length ? system.topProcesses.map(proc => `<div class="process-row"><strong title="${proc.name}">${proc.name} <span style="text-align:left">#${proc.pid}</span></strong><span>CPU ${proc.cpuPercent}%</span><span>RAM ${proc.memoryPercent}%</span></div>`).join("") : '<p class="muted">Process list unavailable</p>';
    $("#bridge-title").textContent = overview.machine.state === "connected" ? "CachyOS bridge connected" : "Waiting for your CachyOS machine";
    $("#bridge-detail").textContent = overview.machine.detail;
    alert("Discord identity verified. Private dashboard ready.", "ok");
  } catch (error) {
    $("#user-state").textContent = "Session unavailable";
    alert(error.message || "Could not initialize Discord session.", "error");
    drawCards(Object.entries(moduleInfo).filter(([key]) => key !== "overview").map(([id, item]) => ({ id, title: item[0], description: "Available after Discord authentication and local bridge setup." })));
  }
}
$("#logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST", body: "{}" }); }
  finally { location.reload(); }
});
start();
