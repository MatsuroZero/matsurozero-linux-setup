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

test("memory, project, and power APIs reject unauthenticated mutations", async () => {
  await withServer(async base => {
    const origin = new URL(base).origin;
    const cases = [
      ["/api/ai/memory", "GET"],
      ["/api/ai/memory/profile", "POST", { profile: "test" }],
      ["/api/projects/action", "POST", { projectId: "odysseus", action: "start" }],
      ["/api/power/action", "POST", { action: "lock" }],
    ];
    for (const [path, method, body] of cases) {
      const response = await fetch(base + path, {
        method,
        headers: { "content-type": "application/json", origin },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      assert.equal(response.status, 401, path);
    }
  });
});

test("dashboard links and credential reveal require an authenticated session", async () => {
  await withServer(async base => {
    const origin = new URL(base).origin;
    const links = await fetch(base + "/api/links");
    assert.equal(links.status, 401);
    const credentials = await fetch(base + "/api/links/credentials", {
      method: "POST",
      headers: { "content-type": "application/json", origin },
      body: JSON.stringify({ pin: "not-a-pin" }),
    });
    assert.equal(credentials.status, 401);
  });
});

test("Nexus registry APIs require an authenticated session", async () => {
  await withServer(async base => {
    const response = await fetch(base + "/api/registry");
    assert.equal(response.status, 401);
    const run = await fetch(base + "/api/commands/run", {
      method: "POST",
      headers: { "content-type": "application/json", origin: new URL(base).origin },
      body: JSON.stringify({ name: "unregistered" }),
    });
    assert.equal(run.status, 401);
  });
});
