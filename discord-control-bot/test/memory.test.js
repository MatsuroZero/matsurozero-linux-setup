import test from "node:test";
import assert from "node:assert/strict";
import { validateProfile, validateFact, validateForgetQuery, formatMemoryForPrompt } from "../src/memory.js";

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
