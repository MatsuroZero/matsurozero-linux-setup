const SNOWFLAKE = /^\d{17,20}$/;

function normalizeAllowedIds(value) {
  const candidates = Array.isArray(value)
    ? value
    : typeof value === "string"
      ? value.split(/[\s,]+/)
      : [];
  return new Set(
    candidates
      .map(item => String(item).trim())
      .filter(item => SNOWFLAKE.test(item)),
  );
}

export function isAuthorizedDiscordUser(
  userId,
  allowedUserIds = process.env.DISCORD_ALLOWED_USER_IDS || process.env.DISCORD_ALLOWED_USER_ID,
) {
  // Discord snowflakes must stay strings to avoid numeric precision loss.
  if (typeof userId !== "string") return false;
  const candidate = userId.trim();
  if (!SNOWFLAKE.test(candidate)) return false;
  return normalizeAllowedIds(allowedUserIds).has(candidate);
}

export function assertAuthorizedDiscordUser(userId, allowedUserIds) {
  if (!isAuthorizedDiscordUser(userId, allowedUserIds)) {
    const error = new Error("Not authorized");
    error.code = "UNAUTHORIZED";
    throw error;
  }
}
