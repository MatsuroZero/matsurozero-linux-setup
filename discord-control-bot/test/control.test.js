import test from "node:test";
import assert from "node:assert/strict";
import { getSystemSnapshot } from "../src/control.js";

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
