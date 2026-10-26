export function isAuthorizedDiscordUser(userId, allowedUserId = process.env.DISCORD_ALLOWED_USER_ID) {
  if (typeof userId !== "string" && typeof userId !== "number") return false;
  if (allowedUserId === undefined || allowedUserId === null) return false;

  const candidate = String(userId).trim();
  const allowed = String(allowedUserId).trim();

  // Discord snowflakes are decimal strings. Fail closed on empty or malformed IDs.
  if (!/^\d{17,20}$/.test(candidate) || !/^\d{17,20}$/.test(allowed)) return false;
  return candidate === allowed;
}

export function assertAuthorizedDiscordUser(userId, allowedUserId) {
  if (!isAuthorizedDiscordUser(userId, allowedUserId)) {
    const error = new Error("Not authorized");
    error.code = "UNAUTHORIZED";
    throw error;
  }
}
