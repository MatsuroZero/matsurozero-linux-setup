import os from "node:os";
import { statfs } from "node:fs/promises";
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
export async function getSystemSnapshot() {
  const totalMemory = os.totalmem();
  const freeMemory = os.freemem();
  const networkInterfaces = os.networkInterfaces();
  const [disk, processes, usagePercent] = await Promise.all([getDisk(), getTopProcesses(), cpuUsagePercent()]);
  return {
    hostname: os.hostname(), platform: os.platform(), architecture: os.arch(),
    cpu: { cores: os.cpus().length, usagePercent, loadAverage: os.loadavg().map(value => Number(value.toFixed(2))) },
    memory: { total: formatBytes(totalMemory), free: formatBytes(freeMemory), used: formatBytes(totalMemory - freeMemory), usedPercent: Math.round((totalMemory - freeMemory) / totalMemory * 100) },
    disk, uptimeSeconds: Math.floor(os.uptime()), uptime: formatUptime(os.uptime()),
    networkInterfaces: Object.entries(networkInterfaces).filter(([, entries]) => entries?.some(entry => !entry.internal)).map(([name]) => name),
    topProcesses: processes, sampledAt: new Date().toISOString(),
  };
}
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
