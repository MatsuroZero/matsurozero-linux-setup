import os from "node:os";
import { statfs } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

function cpuUsagePercent() {
  const cpus = os.cpus();
  let idle = 0, total = 0;
  for (const cpu of cpus) {
    for (const [key, value] of Object.entries(cpu.times)) {
      total += value;
      if (key === "idle") idle += value;
    }
  }
  return total > 0 ? Math.round((1 - idle / total) * 100) : 0;
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
  } catch {
    return null;
  }
}
async function getTopProcesses() {
  try {
    const { stdout } = await execFileAsync("ps", ["-eo", "pid,comm,%cpu,%mem", "--sort=-%cpu"], { timeout: 2000, maxBuffer: 64 * 1024 });
    return stdout.trim().split("\n").slice(1, 7).map(line => {
      const match = line.trim().match(/^(\d+)\s+(.*?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)$/);
      return match ? { pid: Number(match[1]), name: match[2], cpuPercent: Number(match[3]), memoryPercent: Number(match[4]) } : null;
    }).filter(Boolean);
  } catch {
    return [];
  }
}

export async function getSystemSnapshot() {
  const totalMemory = os.totalmem();
  const freeMemory = os.freemem();
  const networkInterfaces = os.networkInterfaces();
  const [disk, processes] = await Promise.all([getDisk(), getTopProcesses()]);
  return {
    hostname: os.hostname(),
    platform: os.platform(),
    architecture: os.arch(),
    cpu: { cores: os.cpus().length, usagePercent: cpuUsagePercent(), loadAverage: os.loadavg().map(value => Number(value.toFixed(2))) },
    memory: { total: formatBytes(totalMemory), free: formatBytes(freeMemory), used: formatBytes(totalMemory - freeMemory), usedPercent: Math.round((totalMemory - freeMemory) / totalMemory * 100) },
    disk,
    uptimeSeconds: Math.floor(os.uptime()),
    uptime: formatUptime(os.uptime()),
    networkInterfaces: Object.entries(networkInterfaces).filter(([, entries]) => entries?.some(entry => !entry.internal)).map(([name]) => name),
    topProcesses: processes,
    sampledAt: new Date().toISOString(),
  };
}
