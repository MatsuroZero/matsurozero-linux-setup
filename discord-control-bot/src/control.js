import os from "node:os";
import { statfs, readFile, readdir, access } from "node:fs/promises";
import { join } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const SERVICES = [
  { id: "jellyfin", name: "Jellyfin", scope: "system", unit: "jellyfin.service" },
  { id: "docker", name: "Docker", scope: "system", unit: "docker.service" },
  { id: "ollama", name: "Ollama", scope: "system", unit: "ollama.service" },
  { id: "localnexus", name: "LocalNexus", scope: "user", unit: "localnexus.service" },
  { id: "telegram-bot", name: "Telegram control bot", scope: "user", unit: "telegram-control-bot.service" },
  { id: "dashboard", name: "System dashboard", scope: "system", unit: "system-dashboard.service" },
  { id: "dashboard-ydotool", name: "Dashboard ydotool", scope: "system", unit: "system-dashboard-ydotool.service" },
  { id: "sunshine", name: "Sunshine", scope: "user", unit: "sunshine.service" },
];

async function runReadOnly(program, args, timeout = 2500) {
  try {
    const { stdout } = await execFileAsync(program, args, { timeout, maxBuffer: 128 * 1024, windowsHide: true });
    return { ok: true, stdout: stdout.trim() };
  } catch (error) {
    return { ok: false, stdout: String(error.stdout || "").trim(), error: error.code === "ENOENT" ? "not-installed" : "unavailable" };
  }
}
function cpuTicks() {
  let idle = 0, total = 0;
  for (const cpu of os.cpus()) {
    for (const [key, value] of Object.entries(cpu.times)) {
      total += value;
      if (key === "idle") idle += value;
    }
  }
  return { idle, total };
}
async function cpuUsagePercent() {
  const before = cpuTicks();
  await new Promise(resolve => setTimeout(resolve, 100));
  const after = cpuTicks();
  const total = after.total - before.total;
  const idle = after.idle - before.idle;
  return total > 0 ? Math.max(0, Math.min(100, Math.round((1 - idle / total) * 100))) : 0;
}
function formatBytes(value) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value, unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return { bytes: value, label: `${size.toFixed(unit >= 3 ? 1 : 0)} ${units[unit]}` };
}
function formatUptime(seconds) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return days ? `${days}d ${hours}h` : hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}
async function getDisk() {
  try {
    const disk = await statfs("/");
    const total = disk.blocks * disk.bsize;
    const free = disk.bavail * disk.bsize;
    const used = Math.max(0, total - free);
    return { total: formatBytes(total), used: formatBytes(used), free: formatBytes(free), usedPercent: total ? Math.round(used / total * 100) : 0 };
  } catch { return null; }
}
async function getTopProcesses() {
  const result = await runReadOnly("ps", ["-eo", "pid,comm,%cpu,%mem", "--sort=-%cpu"]);
  if (!result.ok && !result.stdout) return [];
  return result.stdout.split("\n").slice(1, 7).map(line => {
    const match = line.trim().match(/^(\d+)\s+(.*?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)$/);
    return match ? { pid: Number(match[1]), name: match[2], cpuPercent: Number(match[3]), memoryPercent: Number(match[4]) } : null;
  }).filter(Boolean);
}
async function getTemperatureSnapshot() {
  try {
    const entries = await readdir("/sys/class/thermal");
    const zones = entries.filter(name => /^thermal_zone\d+$/.test(name)).slice(0, 32);
    const sensors = (await Promise.all(zones.map(async zone => {
      try {
        const [type, raw] = await Promise.all([
          readFile(`/sys/class/thermal/${zone}/type`, "utf8").catch(() => zone),
          readFile(`/sys/class/thermal/${zone}/temp`, "utf8"),
        ]);
        const value = Number(raw.trim());
        if (!Number.isFinite(value)) return null;
        const celsius = Math.round((Math.abs(value) > 1000 ? value / 1000 : value) * 10) / 10;
        return { id: zone, name: type.trim().slice(0, 80) || zone, celsius };
      } catch { return null; }
    }))).filter(Boolean);
    return { available: sensors.length > 0, sensors, maxCelsius: sensors.length ? Math.max(...sensors.map(sensor => sensor.celsius)) : null };
  } catch {
    return { available: false, sensors: [], maxCelsius: null };
  }
}
async function readNetworkCounters() {
  const raw = await readFile("/proc/net/dev", "utf8");
  const counters = new Map();
  for (const line of raw.split("\n").slice(2)) {
    const split = line.split(":");
    if (split.length !== 2) continue;
    const name = split[0].trim();
    const values = split[1].trim().split(/\s+/).map(Number);
    if (name && values.length >= 9 && values.every(Number.isFinite)) {
      counters.set(name, { rxBytes: values[0], txBytes: values[8] });
    }
  }
  return counters;
}
export async function getNetworkSnapshot(sampleMs = 350) {
  try {
    const before = await readNetworkCounters();
    const started = Date.now();
    await new Promise(resolve => setTimeout(resolve, Math.max(100, Math.min(1000, sampleMs))));
    const after = await readNetworkCounters();
    const elapsedSeconds = Math.max(0.001, (Date.now() - started) / 1000);
    const interfaces = [...after.entries()].filter(([name]) => name !== "lo").map(([name, counters]) => {
      const previous = before.get(name) || { rxBytes: counters.rxBytes, txBytes: counters.txBytes };
      const rxBytesPerSecond = Math.max(0, Math.round((counters.rxBytes - previous.rxBytes) / elapsedSeconds));
      const txBytesPerSecond = Math.max(0, Math.round((counters.txBytes - previous.txBytes) / elapsedSeconds));
      return {
        name,
        rxBytes: counters.rxBytes,
        txBytes: counters.txBytes,
        rxBytesPerSecond,
        txBytesPerSecond,
        rxRate: `${formatBytes(rxBytesPerSecond).label}/s`,
        txRate: `${formatBytes(txBytesPerSecond).label}/s`,
      };
    });
    return { available: true, sampledMs: Math.round(elapsedSeconds * 1000), interfaces };
  } catch {
    return { available: false, sampledMs: 0, interfaces: [] };
  }
}
export async function getSystemSnapshot() {
  const totalMemory = os.totalmem();
  const freeMemory = os.freemem();
  const networkInterfaces = os.networkInterfaces();
  const [disk, processes, usagePercent, temperatures] = await Promise.all([getDisk(), getTopProcesses(), cpuUsagePercent(), getTemperatureSnapshot()]);
  return {
    hostname: os.hostname(), platform: os.platform(), architecture: os.arch(),
    cpu: { cores: os.cpus().length, usagePercent, loadAverage: os.loadavg().map(value => Number(value.toFixed(2))) },
    memory: { total: formatBytes(totalMemory), free: formatBytes(freeMemory), used: formatBytes(totalMemory - freeMemory), usedPercent: Math.round((totalMemory - freeMemory) / totalMemory * 100) },
    disk, uptimeSeconds: Math.floor(os.uptime()), uptime: formatUptime(os.uptime()),
    networkInterfaces: Object.entries(networkInterfaces).filter(([, entries]) => entries?.some(entry => !entry.internal)).map(([name]) => name),
    temperatures, topProcesses: processes, sampledAt: new Date().toISOString(),
  };
}
const PROJECTS = [
  { id: "odysseus", name: "Odysseus", composeFile: join(os.homedir(), "Projects", "odysseus", "docker-compose.yml") },
  { id: "uptime-kuma", name: "Uptime Kuma", composeFile: join(os.homedir(), "Projects", "uptime-kuma", "compose.yaml") },
  { id: "localnexus", name: "LocalNexus", composeFile: join(os.homedir(), "Projects", "ReverseEngineering", "LocalNexus", "compose.yaml") },
];
const PROJECT_BY_ID = new Map(PROJECTS.map(project => [project.id, project]));
const SERVICE_BY_ID = new Map(SERVICES.map(service => [service.id, service]));
const SERVICE_ACTIONS = new Set(["start", "stop", "restart"]);
const CONTAINER_ACTIONS = new Set(["start", "stop", "restart"]);

export function validateServiceAction(serviceId, action) {
  if (typeof serviceId !== "string" || !SERVICE_BY_ID.has(serviceId)) {
    throw Object.assign(new Error("Unknown service"), { status: 400 });
  }
  if (!SERVICE_ACTIONS.has(action)) throw Object.assign(new Error("Unsupported service action"), { status: 400 });
  return { service: SERVICE_BY_ID.get(serviceId), action };
}
export async function performServiceAction(serviceId, action) {
  const target = validateServiceAction(serviceId, action);
  const args = target.service.scope === "user"
    ? ["--user", target.action, target.service.unit]
    : ["-n", "systemctl", target.action, target.service.unit];
  const program = target.service.scope === "user" ? "systemctl" : "sudo";
  try {
    await execFileAsync(program, args, { timeout: 15_000, maxBuffer: 32 * 1024, windowsHide: true });
  } catch (error) {
    const detail = target.service.scope === "system"
      ? "System service action was denied. Configure a narrow sudoers rule for this exact unit, or use a user-level service."
      : "User service action failed. Check the user systemd session and unit name.";
    throw Object.assign(new Error(detail), { status: 503 });
  }
  console.info(JSON.stringify({ event: "service_action", serviceId, action, at: new Date().toISOString() }));
  return { ok: true, serviceId, action };
}
export function validateContainerAction(containerId, action) {
  if (typeof containerId !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(containerId)) {
    throw Object.assign(new Error("Invalid container identifier"), { status: 400 });
  }
  if (!CONTAINER_ACTIONS.has(action)) throw Object.assign(new Error("Unsupported container action"), { status: 400 });
  return { containerId, action };
}
export async function performContainerAction(containerId, action) {
  const request = validateContainerAction(containerId, action);
  const listed = await runReadOnly("docker", ["ps", "-a", "--format", "{{json .}}"]);
  if (!listed.ok && !listed.stdout) throw Object.assign(new Error("Docker is unavailable or permission was denied"), { status: 503 });
  const found = listed.stdout.split("\n").filter(Boolean).map(line => {
    try {
      const item = JSON.parse(line);
      return { id: item.ID, name: item.Names };
    } catch { return null; }
  }).filter(Boolean).find(item => item.id === request.containerId || item.name === request.containerId);
  if (!found) throw Object.assign(new Error("Container is not in the current Docker inventory"), { status: 404 });
  try {
    await execFileAsync("docker", [request.action, found.id], { timeout: 30_000, maxBuffer: 32 * 1024, windowsHide: true });
  } catch {
    throw Object.assign(new Error("Docker action failed. Check container state and Docker permissions."), { status: 503 });
  }
  console.info(JSON.stringify({ event: "container_action", containerId: found.id, action, at: new Date().toISOString() }));
  return { ok: true, containerId: found.id, action };
}

export async function getServiceSnapshot() {
  return Promise.all(SERVICES.map(async service => {
    const args = service.scope === "user" ? ["--user", "is-active", service.unit] : ["is-active", service.unit];
    const result = await runReadOnly("systemctl", args);
    const state = result.stdout || result.error || "unknown";
    return { ...service, state: ["active", "inactive", "failed", "activating", "deactivating", "unknown", "not-installed", "unavailable"].includes(state) ? state : "unknown" };
  }));
}
export async function getContainerSnapshot() {
  const result = await runReadOnly("docker", ["ps", "-a", "--format", "{{json .}}"]);
  if (!result.ok && !result.stdout) return { available: false, detail: result.error || "unavailable", containers: [] };
  const containers = result.stdout ? result.stdout.split("\n").filter(Boolean).slice(0, 100).map(line => {
    try {
      const item = JSON.parse(line);
      return { id: item.ID || "", name: item.Names || "", image: item.Image || "", status: item.Status || "", ports: item.Ports || "" };
    } catch { return null; }
  }).filter(Boolean) : [];
  return { available: true, detail: containers.length ? `${containers.length} container(s)` : "Docker is available; no containers found", containers };
}

const PROJECT_ACTIONS = new Set(["start", "stop", "restart"]);
function validateProjectAction(projectId, action) {
  if (typeof projectId !== "string" || !PROJECT_BY_ID.has(projectId)) {
    throw Object.assign(new Error("Unknown Docker project"), { status: 400 });
  }
  if (!PROJECT_ACTIONS.has(action)) throw Object.assign(new Error("Unsupported project action"), { status: 400 });
  return { project: PROJECT_BY_ID.get(projectId), action };
}
function parseComposePs(raw) {
  return raw.split("\n").filter(Boolean).slice(0, 100).map(line => {
    try {
      const item = JSON.parse(line);
      return {
        name: item.Name || item.Service || item.ID || "container",
        state: String(item.State || item.Status || "unknown").toLowerCase(),
        image: item.Image || "",
        status: item.Status || item.State || "unknown",
      };
    } catch { return null; }
  }).filter(Boolean);
}
export async function getProjectSnapshot() {
  return Promise.all(PROJECTS.map(async project => {
    try { await access(project.composeFile); }
    catch { return { id: project.id, name: project.name, available: false, state: "compose-file-missing", containers: [] }; }
    const result = await runReadOnly("docker", ["compose", "-f", project.composeFile, "ps", "--format", "json"], 10_000);
    if (!result.ok && !result.stdout) return { id: project.id, name: project.name, available: false, state: "docker-unavailable", containers: [] };
    const containers = parseComposePs(result.stdout);
    const running = containers.filter(item => item.state.includes("running")).length;
    const state = !containers.length ? "stopped" : running === containers.length ? "running" : running > 0 ? "partial" : "stopped";
    return { id: project.id, name: project.name, available: true, state, containers };
  }));
}
export async function performProjectAction(projectId, action) {
  const request = validateProjectAction(projectId, action);
  try { await access(request.project.composeFile); }
  catch { throw Object.assign(new Error("Configured Compose file is missing on the Activity host"), { status: 404 }); }
  const composeAction = request.action === "start" ? ["up", "-d"] : [request.action];
  try {
    const { stdout, stderr } = await execFileAsync("docker", ["compose", "-f", request.project.composeFile, ...composeAction], { timeout: 90_000, maxBuffer: 256 * 1024, windowsHide: true });
    console.info(JSON.stringify({ event: "docker_project_action", projectId, action, at: new Date().toISOString() }));
    return { ok: true, projectId, action, output: String(stdout || stderr || "").slice(-12000) };
  } catch {
    throw Object.assign(new Error("Docker Compose action failed. Check the project file and Docker permissions."), { status: 503 });
  }
}
export async function getProjectLogs(projectId, lines = 100) {
  if (typeof projectId !== "string" || !PROJECT_BY_ID.has(projectId)) throw Object.assign(new Error("Unknown Docker project"), { status: 400 });
  if (!Number.isInteger(lines) || lines < 1 || lines > 1000) throw Object.assign(new Error("Log line count must be between 1 and 1000"), { status: 400 });
  const project = PROJECT_BY_ID.get(projectId);
  try { await access(project.composeFile); }
  catch { throw Object.assign(new Error("Configured Compose file is missing on the Activity host"), { status: 404 }); }
  try {
    const { stdout, stderr } = await execFileAsync("docker", ["compose", "-f", project.composeFile, "logs", "--tail", String(lines), "--no-color"], { timeout: 20_000, maxBuffer: 256 * 1024, windowsHide: true });
    return { projectId, lines, output: String(stdout || stderr || "No log output").slice(-30000) };
  } catch {
    throw Object.assign(new Error("Could not read Docker Compose logs"), { status: 503 });
  }
}
export async function getContainerLogs(containerId, lines = 100) {
  validateContainerAction(containerId, "start");
  if (!Number.isInteger(lines) || lines < 1 || lines > 1000) throw Object.assign(new Error("Log line count must be between 1 and 1000"), { status: 400 });
  const inventory = await getContainerSnapshot();
  if (!inventory.available) throw Object.assign(new Error("Docker inventory is unavailable"), { status: 503 });
  const found = inventory.containers.find(item => item.id === containerId || item.name === containerId);
  if (!found) throw Object.assign(new Error("Container is not in the current Docker inventory"), { status: 404 });
  try {
    const { stdout, stderr } = await execFileAsync("docker", ["logs", "--tail", String(lines), found.id], { timeout: 20_000, maxBuffer: 256 * 1024, windowsHide: true });
    return { containerId: found.id, lines, output: String(stdout || stderr || "No log output").slice(-30000) };
  } catch {
    throw Object.assign(new Error("Could not read container logs"), { status: 503 });
  }
}
