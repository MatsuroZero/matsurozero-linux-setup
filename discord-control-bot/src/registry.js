import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, join } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { validateServiceAction, performServiceAction } from "./control.js";

const execFileAsync = promisify(execFile);
const REGISTRY_PATH = process.env.NEXUS_REGISTRY_PATH || join(homedir(), ".config", "nexus-control", "registry.json");
const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const MAX_STEPS = 30;

async function loadRegistry() {
  try {
    const data = JSON.parse(await readFile(REGISTRY_PATH, "utf8"));
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Invalid registry root");
    const commands = data.commands && typeof data.commands === "object" && !Array.isArray(data.commands) ? data.commands : {};
    const profiles = data.profiles && typeof data.profiles === "object" && !Array.isArray(data.profiles) ? data.profiles : {};
    return { commands, profiles };
  } catch (error) {
    if (error.code === "ENOENT") return { commands: {}, profiles: {} };
    throw Object.assign(new Error("Nexus command registry is invalid or unreadable"), { status: 500 });
  }
}
export function validateName(name) {
  if (typeof name !== "string" || !NAME_PATTERN.test(name)) throw Object.assign(new Error("Invalid registry item name"), { status: 400 });
  return name;
}
export function validArgv(entry) {
  return entry && Array.isArray(entry.argv) && entry.argv.length > 0 &&
    entry.argv.length <= 64 && entry.argv.every(arg => typeof arg === "string" && arg.length <= 2048 && !arg.includes("\\0")) &&
    typeof entry.argv[0] === "string" && entry.argv[0].trim().length > 0;
}
function resolveCommand(registry, name) {
  validateName(name);
  const entry = registry.commands[name];
  if (!validArgv(entry)) throw Object.assign(new Error("Unknown or invalid saved command"), { status: 404 });
  return entry.argv;
}
export async function getRegistrySnapshot() {
  const registry = await loadRegistry();
  const commands = Object.entries(registry.commands).filter(([name, entry]) => NAME_PATTERN.test(name) && validArgv(entry)).slice(0, 200).map(([name, entry]) => ({
    name,
    executable: basename(entry.argv[0]),
  }));
  const profiles = Object.entries(registry.profiles).filter(([name, steps]) => NAME_PATTERN.test(name) && Array.isArray(steps) && steps.length <= MAX_STEPS && steps.every(step => typeof step === "string")).slice(0, 100).map(([name, steps]) => ({
    name,
    steps: steps.length,
  }));
  return { available: true, path: REGISTRY_PATH, commands, profiles };
}
async function executeSavedArgv(argv, label) {
  try {
    const { stdout, stderr } = await execFileAsync(argv[0], argv.slice(1), {
      timeout: 30_000, maxBuffer: 256 * 1024, windowsHide: true,
    });
    return { label, ok: true, output: String(stdout || stderr || "Completed without output").slice(-12000) };
  } catch (error) {
    const output = String(error.stdout || "") + String(error.stderr || "");
    throw Object.assign(new Error(`${label} failed: ${output.slice(-6000) || "process failed or timed out"}`), { status: 503 });
  }
}
export async function runSavedCommand(name) {
  const registry = await loadRegistry();
  const argv = resolveCommand(registry, name);
  const result = await executeSavedArgv(argv, name);
  console.info(JSON.stringify({ event: "saved_command_run", name, at: new Date().toISOString() }));
  return result;
}
export async function runSavedProfile(name) {
  validateName(name);
  const registry = await loadRegistry();
  const steps = registry.profiles[name];
  if (!Array.isArray(steps) || steps.length < 1 || steps.length > MAX_STEPS || !steps.every(step => typeof step === "string")) {
    throw Object.assign(new Error("Unknown or invalid saved profile"), { status: 404 });
  }
  const results = [];
  for (const step of steps) {
    if (step.startsWith("service:")) {
      const parts = step.split(":");
      if (parts.length !== 3) throw Object.assign(new Error(`Invalid service step in profile: ${step.slice(0, 100)}`), { status: 400 });
      const [, action, serviceId] = parts;
      validateServiceAction(serviceId, action);
      await performServiceAction(serviceId, action);
      results.push({ label: step, ok: true, output: "Service action completed" });
    } else {
      const commandName = step.startsWith("cmd:") ? step.slice(4) : step;
      const argv = resolveCommand(registry, commandName);
      results.push(await executeSavedArgv(argv, commandName));
    }
  }
  console.info(JSON.stringify({ event: "saved_profile_run", name, steps: steps.length, at: new Date().toISOString() }));
  return { name, ok: true, results };
}
