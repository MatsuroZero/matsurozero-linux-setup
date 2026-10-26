import test from "node:test";
import assert from "node:assert/strict";
import { getSystemSnapshot, getServiceSnapshot, getContainerSnapshot, getNetworkSnapshot, getProjectSnapshot, validateProjectAction } from "../src/control.js";

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

test("control action validators reject unknown targets and unsupported actions", async () => {
  const { validateServiceAction, validateContainerAction } = await import("../src/control.js");
  assert.throws(() => validateServiceAction("arbitrary", "restart"), error => error.status === 400);
  assert.throws(() => validateServiceAction("docker", "exec"), error => error.status === 400);
  assert.throws(() => validateContainerAction("name; arbitrary command", "start"), error => error.status === 400);
  assert.throws(() => validateContainerAction("valid-name", "exec"), error => error.status === 400);
});

test("thermal and network snapshots expose bounded structured data", async () => {
  const system = await getSystemSnapshot();
  assert.equal(typeof system.temperatures.available, "boolean");
  assert.ok(Array.isArray(system.temperatures.sensors));
  assert.ok(system.temperatures.sensors.length <= 32);
  const network = await getNetworkSnapshot(100);
  assert.equal(typeof network.available, "boolean");
  assert.ok(Array.isArray(network.interfaces));
  assert.ok(network.interfaces.every(item => typeof item.name === "string" && typeof item.rxBytesPerSecond === "number" && typeof item.txBytesPerSecond === "number"));
});

test("Docker project actions accept only known project names and verbs", () => {
  assert.equal(validateProjectAction("odysseus", "start").action, "start");
  assert.throws(() => validateProjectAction("arbitrary-path", "start"), error => error.status === 400);
  assert.throws(() => validateProjectAction("odysseus", "exec"), error => error.status === 400);
});

test("Docker project inventory is bounded and reports availability", async () => {
  const projects = await getProjectSnapshot();
  assert.deepEqual(projects.map(project => project.id), ["odysseus", "uptime-kuma", "localnexus"]);
  assert.ok(projects.every(project => typeof project.available === "boolean" && Array.isArray(project.containers)));
});
