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
    assert.deepEqual(await response.json(), { ok: true, service: "discord-control-activity", hostMetrics: "available", controlActions: "disabled" });
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
