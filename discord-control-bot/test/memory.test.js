import test from "node:test";
import assert from "node:assert/strict";
import { validateProfile, validateFact, validateForgetQuery, formatMemoryForPrompt, getMemory, setMemoryProfile, rememberFact, forgetMemoryFacts } from "../src/memory.js";
import { mkdtemp, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

test("memory profile and fact inputs are bounded and normalized", () => {
  assert.equal(validateProfile("  owner profile  "), "owner profile");
  assert.equal(validateFact("  keep this  "), "keep this");
  assert.throws(() => validateProfile("x".repeat(4001)), error => error.status === 400);
  assert.throws(() => validateFact(""), error => error.status === 400);
  assert.throws(() => validateFact("x".repeat(501)), error => error.status === 400);
});

test("forget query supports a full saved fact but rejects oversized queries", () => {
  assert.equal(validateForgetQuery("A saved fact"), "a saved fact");
  assert.throws(() => validateForgetQuery("x".repeat(501)), error => error.status === 400);
});

test("memory prompt is bounded and includes profile and facts", () => {
  const prompt = formatMemoryForPrompt({ profile: "Prefers concise answers", facts: ["Uses CachyOS"] });
  assert.match(prompt, /Prefers concise answers/);
  assert.match(prompt, /Uses CachyOS/);
  assert.ok(prompt.length <= 8000);
});

test("persistent memory round-trips with bounded private file permissions", async () => {
  const directory = await mkdtemp(join(tmpdir(), "nexus-memory-test-"));
  const filePath = join(directory, "memory.json");
  try {
    const initial = await getMemory(filePath);
    assert.deepEqual(initial, { profile: "", facts: [] });
    await setMemoryProfile("profile for tests", filePath);
    await rememberFact("likes tests", filePath);
    await rememberFact("LIKES TESTS", filePath);
    let saved = await getMemory(filePath);
    assert.equal(saved.profile, "profile for tests");
    assert.deepEqual(saved.facts, ["likes tests"]);
    const info = await stat(filePath);
    assert.equal(info.mode & 0o077, 0);
    const result = await forgetMemoryFacts("likes tests", filePath);
    assert.equal(result.removed, 1);
    assert.deepEqual((await getMemory(filePath)).facts, []);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
