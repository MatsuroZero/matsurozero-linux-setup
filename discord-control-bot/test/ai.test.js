import test from "node:test";
import assert from "node:assert/strict";
import { chat } from "../src/ai.js";

test("AI adapter rejects blank or oversized messages before network access", async () => {
  await assert.rejects(
    chat({ provider: "ollama", model: "qwen3:0.6b", message: "   " }),
    error => error.status === 400,
  );
  await assert.rejects(
    chat({ provider: "ollama", model: "qwen3:0.6b", message: "x".repeat(4001) }),
    error => error.status === 400,
  );
});

test("AI adapter rejects unsupported providers before network access", async () => {
  await assert.rejects(
    chat({ provider: "arbitrary", model: "anything", message: "hello" }),
    error => error.status === 400,
  );
});
