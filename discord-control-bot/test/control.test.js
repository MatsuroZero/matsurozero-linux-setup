import test from "node:test";
import assert from "node:assert/strict";
import { getSystemSnapshot, getServiceSnapshot, getContainerSnapshot } from "../src/control.js";

test("system snapshot returns bounded, typed read-only metrics", async () => {
  const snapshot = await getSystemSnapshot();
  assert.equal(typeof snapshot.hostname, "string");
  assert.ok(snapshot.cpu.cores >= 1);
  assert.ok(snapshot.cpu.usagePercent >= 0 && snapshot.cpu.usagePercent <= 100);
  assert.ok(snapshot.memory.total.bytes > 0);
  assert.ok(snapshot.memory.usedPercent >= 0 && snapshot.memory.usedPercent <= 100);
  assert.ok(Array.isArray(snapshot.networkInterfaces));
  assert.ok(Array.isArray(snapshot.topProcesses));
  assert.ok(snapshot.topProcesses.length <= 6);
});

test("service inventory uses a fixed allowlist", async () => {
  const services = await getServiceSnapshot();
  assert.ok(services.length >= 7);
  assert.ok(services.every(service => typeof service.unit === "string" && typeof service.state === "string"));
  assert.ok(services.every(service => !("command" in service)));
});

test("container inventory is read-only and bounded", async () => {
  const result = await getContainerSnapshot();
  assert.equal(typeof result.available, "boolean");
  assert.ok(Array.isArray(result.containers));
  assert.ok(result.containers.length <= 100);
  assert.ok(result.containers.every(container => typeof container.id === "string" && typeof container.name === "string"));
});
