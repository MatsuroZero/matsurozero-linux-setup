import test from "node:test";
import assert from "node:assert/strict";
import { createAppServer } from "../src/server.js";

async function withServer(run) {
  const server = createAppServer();
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  try { await run(`http://127.0.0.1:${address.port}`); }
  finally { await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve())); }
}

test("health endpoint is public and reports bridge state honestly", async () => {
  await withServer(async base => {
    const response = await fetch(base + "/api/health");
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { ok: true, service: "discord-control-activity", hostMetrics: "available", controlActions: "allowlisted" });
  });
});

test("control API rejects requests without a Discord session", async () => {
  await withServer(async base => {
    const response = await fetch(base + "/api/overview");
    assert.equal(response.status, 401);
    assert.match((await response.json()).error, /Authenticate/);
  });
});

test("unknown API routes never fall through to static files", async () => {
  await withServer(async base => {
    const response = await fetch(base + "/api/run-shell");
    assert.equal(response.status, 401);
  });
});

test("AI model and chat endpoints require an authenticated Discord session", async () => {
  await withServer(async base => {
    const models = await fetch(base + "/api/ai/models");
    assert.equal(models.status, 401);
    const chat = await fetch(base + "/api/ai/chat", {
      method: "POST",
      headers: { "content-type": "application/json", "origin": new URL(base).origin },
      body: JSON.stringify({ provider: "ollama", model: "qwen3:0.6b", message: "hello" }),
    });
    assert.equal(chat.status, 401);
  });
});

test("control mutations reject cross-origin requests before authentication", async () => {
  await withServer(async base => {
    const response = await fetch(base + "/api/services/action", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "origin": "https://untrusted.example",
      },
      body: JSON.stringify({ serviceId: "docker", action: "restart" }),
    });
    assert.equal(response.status, 403);
    assert.match((await response.json()).error, /Cross-origin/);
  });
});
