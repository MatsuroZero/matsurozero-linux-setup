import { chmod, mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

export const DEFAULT_MEMORY_PATH = join(homedir(), ".local", "share", "telegram-control-bot", "ollama_memory.json");
const PROFILE_LIMIT = 4000;
const FACT_LIMIT = 500;
const FACT_COUNT_LIMIT = 100;

function resolvePath(filePath = process.env.OLLAMA_MEMORY_PATH || DEFAULT_MEMORY_PATH) {
  if (typeof filePath !== "string" || !filePath.trim()) throw new Error("Invalid memory file path");
  return filePath;
}
function normalizeMemory(value) {
  const profile = typeof value?.profile === "string" ? value.profile.slice(0, PROFILE_LIMIT) : "";
  const seen = new Set();
  const facts = [];
  for (const raw of Array.isArray(value?.facts) ? value.facts : []) {
    if (typeof raw !== "string") continue;
    const fact = raw.trim().slice(0, FACT_LIMIT);
    const key = fact.toLocaleLowerCase();
    if (fact && !seen.has(key)) {
      seen.add(key);
      facts.push(fact);
      if (facts.length >= FACT_COUNT_LIMIT) break;
    }
  }
  return { profile, facts };
}
export function validateProfile(profile) {
  if (typeof profile !== "string" || profile.length > PROFILE_LIMIT) {
    throw Object.assign(new Error("Profile must be a string of at most 4000 characters"), { status: 400 });
  }
  return profile.trim();
}
export function validateFact(fact) {
  if (typeof fact !== "string" || !fact.trim() || fact.trim().length > FACT_LIMIT) {
    throw Object.assign(new Error("Fact must contain 1 to 500 characters"), { status: 400 });
  }
  return fact.trim();
}
export function validateForgetQuery(query) {
  if (typeof query !== "string" || !query.trim() || query.trim().length > 100) {
    throw Object.assign(new Error("Forget query must contain 1 to 100 characters"), { status: 400 });
  }
  return query.trim().toLocaleLowerCase();
}
export async function getMemory(filePath) {
  const target = resolvePath(filePath);
  await mkdir(dirname(target), { recursive: true, mode: 0o700 });
  try {
    const parsed = JSON.parse(await readFile(target, "utf8"));
    return normalizeMemory(parsed);
  } catch (error) {
    if (error.code !== "ENOENT") {
      throw Object.assign(new Error("Memory file could not be read as valid JSON"), { status: 500 });
    }
    const initial = { profile: "", facts: [] };
    await saveMemory(initial, target);
    return initial;
  }
}
export async function saveMemory(memory, filePath) {
  const target = resolvePath(filePath);
  await mkdir(dirname(target), { recursive: true, mode: 0o700 });
  const temp = `${target}.tmp-${process.pid}`;
  await writeFile(temp, JSON.stringify(normalizeMemory(memory), null, 2) + "\n", { encoding: "utf8", mode: 0o600 });
  await rename(temp, target);
  await chmod(target, 0o600).catch(() => {});
}
export async function setMemoryProfile(profile, filePath) {
  const validated = validateProfile(profile);
  const memory = await getMemory(filePath);
  memory.profile = validated;
  await saveMemory(memory, filePath);
  return memory;
}
export async function rememberFact(fact, filePath) {
  const validated = validateFact(fact);
  const memory = await getMemory(filePath);
  if (!memory.facts.some(item => item.toLocaleLowerCase() === validated.toLocaleLowerCase())) {
    if (memory.facts.length >= FACT_COUNT_LIMIT) throw Object.assign(new Error("Memory is full; forget a fact before adding another"), { status: 409 });
    memory.facts.push(validated);
    await saveMemory(memory, filePath);
  }
  return memory;
}
export async function forgetMemoryFacts(query, filePath) {
  const needle = validateForgetQuery(query);
  const memory = await getMemory(filePath);
  const before = memory.facts.length;
  memory.facts = memory.facts.filter(item => !item.toLocaleLowerCase().includes(needle));
  const removed = before - memory.facts.length;
  if (removed) await saveMemory(memory, filePath);
  return { memory, removed };
}
export function formatMemoryForPrompt(memory) {
  const parts = [];
  if (memory?.profile) parts.push(`User profile:\n${memory.profile}`);
  if (Array.isArray(memory?.facts) && memory.facts.length) {
    parts.push(`Remembered facts:\n${memory.facts.slice(0, 100).map(fact => `- ${fact}`).join("\n")}`);
  }
  return parts.join("\n\n").slice(0, 8000);
}
