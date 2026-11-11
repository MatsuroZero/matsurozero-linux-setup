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

test("authorization rejects numeric IDs to avoid snowflake precision loss", () => {
  assert.equal(isAuthorizedDiscordUser(Number(allowed)), false);
});

test("accepts any valid ID in a comma-separated allowlist", () => {
  const second = "222222222222222222";
  const third = "333333333333333333";
  assert.equal(isAuthorizedDiscordUser(second, `${allowed}, ${third}`), false);
  assert.equal(isAuthorizedDiscordUser(third, `${allowed}, ${third}`), true);
});

test("accepts array allowlists and ignores malformed entries", () => {
  const second = "222222222222222222";
  assert.equal(isAuthorizedDiscordUser(second, [allowed, "not-an-id", second]), true);
  assert.equal(isAuthorizedDiscordUser(second, [allowed, "not-an-id"]), false);
});

test("defaults to the plural environment allowlist with legacy fallback", () => {
  const previousPlural = process.env.DISCORD_ALLOWED_USER_IDS;
  const previousSingle = process.env.DISCORD_ALLOWED_USER_ID;
  try {
    process.env.DISCORD_ALLOWED_USER_IDS = `${allowed},222222222222222222`;
    delete process.env.DISCORD_ALLOWED_USER_ID;
    assert.equal(isAuthorizedDiscordUser("222222222222222222"), true);
    delete process.env.DISCORD_ALLOWED_USER_IDS;
    process.env.DISCORD_ALLOWED_USER_ID = allowed;
    assert.equal(isAuthorizedDiscordUser(allowed), true);
    assert.equal(isAuthorizedDiscordUser(Number(allowed)), false);
  } finally {
    if (previousPlural === undefined) delete process.env.DISCORD_ALLOWED_USER_IDS;
    else process.env.DISCORD_ALLOWED_USER_IDS = previousPlural;
    if (previousSingle === undefined) delete process.env.DISCORD_ALLOWED_USER_ID;
    else process.env.DISCORD_ALLOWED_USER_ID = previousSingle;
  }
});
