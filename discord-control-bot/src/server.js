import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import { assertAuthorizedDiscordUser } from "./authz.js";
import { getSystemSnapshot, getServiceSnapshot, getContainerSnapshot, getNetworkSnapshot, getProjectSnapshot, performProjectAction, getProjectLogs, getContainerLogs, validatePowerAction, performPowerAction, getHealthSnapshot, getDoctorSnapshot, performServiceAction, performContainerAction } from "./control.js";
import { chat, getModelCatalog } from "./ai.js";
import { getMemory, setMemoryProfile, rememberFact, forgetMemoryFacts } from "./memory.js";
import { getRegistrySnapshot, runSavedCommand, runSavedProfile } from "./registry.js";

const ROOT = fileURLToPath(new URL("../public/", import.meta.url));
const PORT = Number(process.env.PORT || 8787);
const sessions = new Map();
const lastActionAt = new Map();
const credentialAttempts = new Map();
const DASHBOARDS = [
  { id: "system-dashboard", name: "System dashboard", url: process.env.SYSTEM_DASHBOARD_URL || "http://marwan-pc.local:7171/login", usernameEnv: "SYSTEM_DASHBOARD_USERNAME", passwordEnv: "SYSTEM_DASHBOARD_PASSWORD" },
  { id: "odysseus", name: "Odysseus", url: process.env.ODYSSEUS_URL || "http://marwan-pc.local:7011/", usernameEnv: "ODYSSEUS_USERNAME", passwordEnv: "ODYSSEUS_PASSWORD" },
  { id: "jellyfin", name: "Jellyfin", url: process.env.JELLYFIN_URL || "http://marwan-pc.local:8096/", usernameEnv: "JELLYFIN_USERNAME", passwordEnv: "JELLYFIN_PASSWORD" },
  { id: "uptime-kuma", name: "Uptime Kuma", url: process.env.UPTIME_KUMA_URL || "http://marwan-pc.local:3001/", usernameEnv: "UPTIME_KUMA_USERNAME", passwordEnv: "UPTIME_KUMA_PASSWORD" },
];
const SESSION_TTL_MS = 8 * 60 * 60 * 1000;
const APP_ID = process.env.DISCORD_APPLICATION_ID || "";

function env(name) {
  const value = process.env[name];
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
  return value;
}
export function cookieValue(header, name) {
  for (const part of (header || "").split(";")) {
    const separator = part.indexOf("=");
    if (separator < 0 || part.slice(0, separator).trim() !== name) continue;
    return part.slice(separator + 1).trim();
  }
  return undefined;
}
function send(res, status, body, headers = {}) {
  const data = typeof body === "string" ? body : JSON.stringify(body);
  res.writeHead(status, { "content-type": typeof body === "string" ? "text/plain; charset=utf-8" : "application/json; charset=utf-8", "x-content-type-options": "nosniff", "cache-control": "no-store", ...headers });
  res.end(data);
}
async function readJson(req, maxBytes = 16_384) {
  let data = "";
  for await (const chunk of req) {
    data += chunk;
    if (data.length > maxBytes) throw Object.assign(new Error("Request too large"), { status: 413 });
  }
  try { return JSON.parse(data || "{}"); }
  catch { throw Object.assign(new Error("Invalid JSON"), { status: 400 }); }
}
function hasSameOrigin(req) {
  const origin = req.headers.origin;
  if (typeof origin !== "string") return false;

  try {
    const parsedOrigin = new URL(origin);

    // Non-local browser origins must use HTTPS.
    if (
      parsedOrigin.protocol !== "https:" &&
      parsedOrigin.hostname !== "localhost" &&
      parsedOrigin.hostname !== "127.0.0.1"
    ) return false;

    // Explicitly trust only origins configured by the server owner.
    const configuredOrigins = [
      process.env.DISCORD_ALLOWED_ORIGINS || "",
      process.env.DISCORD_ACTIVITY_ORIGIN || ""
    ]
      .join(",")
      .split(",")
      .map(value => value.trim())
      .filter(Boolean);

    for (const value of configuredOrigins) {
      try {
        const allowed = new URL(value);
        if (
          allowed.origin === parsedOrigin.origin &&
          (allowed.protocol === "https:" ||
            allowed.hostname === "localhost" ||
            allowed.hostname === "127.0.0.1")
        ) return true;
      } catch {
        // Ignore malformed configuration entries.
      }
    }

    // Preserve ordinary same-host requests.
    return typeof req.headers.host === "string" &&
      parsedOrigin.host === req.headers.host;
  } catch {
    return false;
  }
}
function safeHttpUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.toString() : "";
  } catch { return ""; }
}
function checkCredentialPin(userId, supplied) {
  const now = Date.now();
  const state = credentialAttempts.get(userId) || { count: 0, windowStart: now, lockedUntil: 0 };
  if (state.lockedUntil > now) return { ok: false, locked: true };
  if (now - state.windowStart > 5 * 60 * 1000) {
    state.count = 0;
    state.windowStart = now;
    state.lockedUntil = 0;
  }
  const expected = process.env.LINKS_PIN || "";
  if (expected.length < 6) return { ok: false, notConfigured: true };
  const candidate = typeof supplied === "string" ? supplied : "";
  const candidateBuffer = Buffer.from(candidate, "utf8");
  const expectedBuffer = Buffer.from(expected, "utf8");
  const matches = candidateBuffer.length === expectedBuffer.length && timingSafeEqual(candidateBuffer, expectedBuffer);
  if (matches) {
    credentialAttempts.delete(userId);
    return { ok: true };
  }
  state.count += 1;
  if (state.count >= 3) state.lockedUntil = now + 5 * 60 * 1000;
  credentialAttempts.set(userId, state);
  return { ok: false, locked: state.lockedUntil > now };
}
function enforceActionCooldown(userId) {
  const now = Date.now();
  const previous = lastActionAt.get(userId) || 0;
  if (now - previous < 1200) throw Object.assign(new Error("Please wait a moment before another control action."), { status: 429 });
  lastActionAt.set(userId, now);
}
function getSession(req) {
  const sid = cookieValue(req.headers.cookie, "nexus_sid");
  const session = sid && sessions.get(sid);
  if (!session || session.expiresAt < Date.now()) {
    if (sid) sessions.delete(sid);
    return null;
  }
  return session;
}
async function discordApi(path, token) {
  const response = await fetch(`https://discord.com/api/v10${path}`, { headers: { authorization: `Bearer ${token}`, accept: "application/json" }, signal: AbortSignal.timeout(8000) });
  if (!response.ok) throw new Error(`Discord API rejected request (${response.status})`);
  return response.json();
}
async function exchangeCode(code) {
  const body = new URLSearchParams({
    client_id: env("DISCORD_APPLICATION_ID"),
    client_secret: env("DISCORD_CLIENT_SECRET"),
    grant_type: "authorization_code",
    code,
    redirect_uri: env("DISCORD_REDIRECT_URI"),
  });
  const response = await fetch("https://discord.com/api/v10/oauth2/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
    signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) throw new Error("Discord OAuth code exchange failed");
  return response.json();
}
const MIME = { ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml" };

export function createAppServer() {
  return createServer(async (req, res) => {
    const url = new URL(req.url || "/", "http://localhost");
    try {
      if (req.method === "GET" && url.pathname === "/api/config") {
        return send(res, 200, { applicationId: APP_ID });
      }
      if (req.method === "GET" && url.pathname === "/api/health") {
        return send(res, 200, { ok: true, service: "discord-control-activity", hostMetrics: "available", controlActions: "allowlisted" });
      }
      if (req.method === "POST" && ["/api/session", "/api/logout", "/api/services/action", "/api/containers/action", "/api/projects/action", "/api/projects/logs", "/api/containers/logs", "/api/power/action", "/api/links/credentials", "/api/commands/run", "/api/profiles/run", "/api/ai/chat", "/api/ai/memory/profile", "/api/ai/memory/remember", "/api/ai/memory/forget"].includes(url.pathname) && !hasSameOrigin(req)) {
        console.warn("[origin-debug]", JSON.stringify({
          path: url.pathname,
          origin: req.headers.origin || null,
          host: req.headers.host || null,
          forwardedHost: req.headers["x-forwarded-host"] || null,
          forwardedProto: req.headers["x-forwarded-proto"] || null,
          secFetchSite: req.headers["sec-fetch-site"] || null
        }));
        return send(res, 403, { error: "Cross-origin request rejected" });
      }
      if (req.method === "POST" && url.pathname === "/api/session") {
        const { code } = await readJson(req);
        if (typeof code !== "string" || code.length < 1 || code.length > 2048) return send(res, 400, { error: "Invalid OAuth code" });
        const tokenData = await exchangeCode(code);
        if (typeof tokenData.access_token !== "string") return send(res, 401, { error: "Discord authorization failed" });
        const user = await discordApi("/users/@me", tokenData.access_token);
        assertAuthorizedDiscordUser(user.id, process.env.DISCORD_ALLOWED_USER_ID);
        const sid = randomBytes(32).toString("base64url");
        sessions.set(sid, { userId: user.id, username: user.global_name || user.username, expiresAt: Date.now() + SESSION_TTL_MS, history: [] });
        return send(res, 200, { ok: true, access_token: tokenData.access_token, user: { id: user.id, name: user.global_name || user.username } }, {
          "set-cookie": `nexus_sid=${sid}; HttpOnly; Secure; SameSite=None; Path=/; Max-Age=28800`,
        });
      }
      if (req.method === "GET" && url.pathname === "/api/session") {
        const session = getSession(req);
        if (!session) return send(res, 401, { authenticated: false });
        return send(res, 200, { authenticated: true, user: { id: session.userId, name: session.username } });
      }
      if (req.method === "POST" && url.pathname === "/api/logout") {
        const sid = cookieValue(req.headers.cookie, "nexus_sid");
        if (sid) sessions.delete(sid);
        return send(res, 200, { ok: true }, { "set-cookie": "nexus_sid=; HttpOnly; Secure; SameSite=None; Path=/; Max-Age=0" });
      }
      if (url.pathname.startsWith("/api/")) {
        const session = getSession(req);
        if (!session) return send(res, 401, { error: "Authenticate through Discord first" });
        try { assertAuthorizedDiscordUser(session.userId, process.env.DISCORD_ALLOWED_USER_ID); }
        catch { return send(res, 403, { error: "Not authorized" }); }
        if (req.method === "GET" && url.pathname === "/api/registry") {
          return send(res, 200, await getRegistrySnapshot());
        }
        if (req.method === "POST" && url.pathname === "/api/commands/run") {
          const { name } = await readJson(req, 4096);
          enforceActionCooldown(session.userId);
          return send(res, 200, await runSavedCommand(name));
        }
        if (req.method === "POST" && url.pathname === "/api/profiles/run") {
          const { name } = await readJson(req, 4096);
          enforceActionCooldown(session.userId);
          return send(res, 200, await runSavedProfile(name));
        }
        if (req.method === "GET" && url.pathname === "/api/links") {
          return send(res, 200, { dashboards: DASHBOARDS.map(({ id, name, url }) => ({ id, name, url: safeHttpUrl(url) })) });
        }
        if (req.method === "POST" && url.pathname === "/api/links/credentials") {
          const { pin } = await readJson(req, 4096);
          const verification = checkCredentialPin(session.userId, pin);
          if (verification.notConfigured) return send(res, 503, { error: "LINKS_PIN is not configured on the server." });
          if (!verification.ok) return send(res, 403, { error: verification.locked ? "Too many PIN attempts. Try again in five minutes." : "Incorrect PIN." });
          const credentials = DASHBOARDS.map(item => ({
            name: item.name,
            username: process.env[item.usernameEnv] || "",
            password: process.env[item.passwordEnv] || "",
          })).filter(item => item.username && item.password);
          if (!credentials.length) return send(res, 503, { error: "No dashboard credentials are configured on the server." });
          return send(res, 200, { credentials });
        }
        if (req.method === "GET" && url.pathname === "/api/ai/memory") {
          return send(res, 200, await getMemory());
        }
        if (req.method === "POST" && url.pathname === "/api/ai/memory/profile") {
          const { profile } = await readJson(req, 8192);
          return send(res, 200, await setMemoryProfile(profile));
        }
        if (req.method === "POST" && url.pathname === "/api/ai/memory/remember") {
          const { fact } = await readJson(req, 4096);
          return send(res, 200, await rememberFact(fact));
        }
        if (req.method === "POST" && url.pathname === "/api/ai/memory/forget") {
          const { query } = await readJson(req, 4096);
          return send(res, 200, await forgetMemoryFacts(query));
        }
        if (req.method === "GET" && url.pathname === "/api/ai/models") {
          return send(res, 200, await getModelCatalog());
        }
        if (req.method === "POST" && url.pathname === "/api/ai/chat") {
          const { provider, model, message } = await readJson(req, 16_384);
          const memory = await getMemory();
          const result = await chat({ provider, model, message, history: session.history, memory });
          session.history = result.history;
          console.info(JSON.stringify({ event: "ai_chat", userId: session.userId, provider, model, at: new Date().toISOString() }));
          return send(res, 200, { answer: result.answer });
        }
        if (req.method === "GET" && url.pathname === "/api/network") {
          return send(res, 200, await getNetworkSnapshot());
        }
        if (req.method === "GET" && url.pathname === "/api/health/details") {
          return send(res, 200, await getHealthSnapshot());
        }
        if (req.method === "GET" && url.pathname === "/api/doctor") {
          return send(res, 200, await getDoctorSnapshot());
        }
        if (req.method === "GET" && url.pathname === "/api/system") {
          return send(res, 200, await getSystemSnapshot());
        }
        if (req.method === "GET" && url.pathname === "/api/services") {
          return send(res, 200, { services: await getServiceSnapshot() });
        }
        if (req.method === "POST" && url.pathname === "/api/services/action") {
          const { serviceId, action } = await readJson(req, 4096);
          enforceActionCooldown(session.userId);
          const result = await performServiceAction(serviceId, action);
          console.info(JSON.stringify({ event: "authorized_control", userId: session.userId, kind: "service", serviceId, action }));
          return send(res, 200, result);
        }
        if (req.method === "POST" && url.pathname === "/api/power/action") {
          const { action } = await readJson(req, 4096);
          validatePowerAction(action);
          enforceActionCooldown(session.userId);
          const result = await performPowerAction(action);
          console.info(JSON.stringify({ event: "authorized_control", userId: session.userId, kind: "power", action }));
          return send(res, 200, result);
        }
        if (req.method === "GET" && url.pathname === "/api/projects") {
          return send(res, 200, { projects: await getProjectSnapshot() });
        }
        if (req.method === "POST" && url.pathname === "/api/projects/action") {
          const { projectId, action } = await readJson(req, 4096);
          enforceActionCooldown(session.userId);
          const result = await performProjectAction(projectId, action);
          console.info(JSON.stringify({ event: "authorized_control", userId: session.userId, kind: "docker_project", projectId, action }));
          return send(res, 200, result);
        }
        if (req.method === "POST" && url.pathname === "/api/projects/logs") {
          const { projectId, lines = 100 } = await readJson(req, 4096);
          return send(res, 200, await getProjectLogs(projectId, lines));
        }
        if (req.method === "POST" && url.pathname === "/api/containers/logs") {
          const { containerId, lines = 100 } = await readJson(req, 4096);
          return send(res, 200, await getContainerLogs(containerId, lines));
        }
        if (req.method === "GET" && url.pathname === "/api/containers") {
          return send(res, 200, await getContainerSnapshot());
        }
        if (req.method === "POST" && url.pathname === "/api/containers/action") {
          const { containerId, action } = await readJson(req, 4096);
          enforceActionCooldown(session.userId);
          const result = await performContainerAction(containerId, action);
          console.info(JSON.stringify({ event: "authorized_control", userId: session.userId, kind: "container", containerId: result.containerId, action }));
          return send(res, 200, result);
        }
        if (req.method === "GET" && url.pathname === "/api/overview") {
          return send(res, 200, {
            user: { id: session.userId, name: session.username },
            machine: { state: "connected", detail: "Metrics are from the Activity host. Service and container actions use fixed allowlists; AI chat is available when a provider is configured." },
            sections: [
              { id: "system", title: "System", description: "CPU, memory, storage, temperature, uptime" },
              { id: "services", title: "Services", description: "Jellyfin, Docker, Ollama, LocalNexus and dashboards" },
              { id: "containers", title: "Containers", description: "Inspect Docker projects, containers and logs" },
              { id: "network", title: "Network", description: "Network interfaces and connectivity" },
              { id: "assistant", title: "AI Assistant", description: "Ollama/Gemini chat and model selection" },
            ],
          });
        }
        // Intentionally no shell/command endpoint. Privileged operations will be exposed only
        // through a narrow allowlisted bridge after its authentication and tests are in place.
        return send(res, 404, { error: "Control endpoint is not implemented yet" });
      }
      if (req.method !== "GET" && req.method !== "HEAD") return send(res, 405, { error: "Method not allowed" });
      const requested = url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname.slice(1));
      const safe = normalize(requested).replace(/^([.][.][/\\])+/, "");
      if (safe.includes("..") || safe.startsWith("/") || safe.includes("\\") || extname(safe) === "") return send(res, 404, "Not found");
      const file = join(ROOT, safe);
      try {
        const data = await readFile(file);
        res.writeHead(200, { "content-type": MIME[extname(file)] || "application/octet-stream", "x-content-type-options": "nosniff", "content-security-policy": "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self'; connect-src 'self' https://discord.com; img-src 'self' data:; frame-ancestors https://discord.com https://*.discord.com", "cache-control": "no-cache" });
        return res.end(req.method === "HEAD" ? undefined : data);
      } catch { return send(res, 404, "Not found"); }
    } catch (error) {
      const status = error.status || (error.code === "UNAUTHORIZED" ? 403 : 500);
      if (status === 403) return send(res, 403, { error: "This Discord account is not on the allowlist." });
      // Keep provider responses, tokens, and stack traces out of client-visible errors.
      return send(res, status, { error: status === 500 ? "Request failed. Check server logs." : error.message });
    }
  });
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  createAppServer().listen(PORT, () => console.log(`Discord Control Activity listening on :${PORT}`));
}
