import test from "node:test";
import assert from "node:assert/strict";
import { assertAuthorizedDiscordUser, isAuthorizedDiscordUser } from "../src/authz.js";

const allowed = "332036461669122048";

test("accepts only the exact configured Discord user ID", () => {
  assert.equal(isAuthorizedDiscordUser(allowed, allowed), true);
  assert.equal(isAuthorizedDiscordUser("332036461669122049", allowed), false);
});

test("fails closed when the allowlist is absent or malformed", () => {
  assert.equal(isAuthorizedDiscordUser(allowed, undefined), false);
  assert.equal(isAuthorizedDiscordUser(allowed, ""), false);
  assert.equal(isAuthorizedDiscordUser("not-an-id", allowed), false);
  assert.equal(isAuthorizedDiscordUser("", allowed), false);
  assert.equal(isAuthorizedDiscordUser(null, allowed), false);
});

test("throws a stable authorization error for denied users", () => {
  assert.throws(
    () => assertAuthorizedDiscordUser("111111111111111111", allowed),
    (error) => error.code === "UNAUTHORIZED" && error.message === "Not authorized",
  );
});
