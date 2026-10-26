import test from "node:test";
import assert from "node:assert/strict";
import { validateName, validArgv } from "../src/registry.js";

test("registry names follow Nexus naming rules", () => {
  assert.equal(validateName("restart_jellyfin"), "restart_jellyfin");
  assert.throws(() => validateName("../arbitrary"), error => error.status === 400);
  assert.throws(() => validateName(""), error => error.status === 400);
});

test("saved command argv is validated without shell interpretation", () => {
  assert.equal(validArgv({ argv: ["/usr/bin/uptime"] }), true);
  assert.equal(validArgv({ argv: [] }), false);
  assert.equal(validArgv({ argv: ["/usr/bin/tool", 42] }), false);
  assert.equal(validArgv({ argv: [""] }), false);
});
