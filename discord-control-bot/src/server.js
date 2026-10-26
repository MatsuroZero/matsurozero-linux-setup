import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { randomBytes } from "node:crypto";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import { assertAuthorizedDiscordUser } from "./authz.js";
import { getSystemSnapshot, getServiceSnapshot, getContainerSnapshot, performServiceAction, performContainerAction } from "./control.js";

const ROOT = fileURLToPath(new URL("../public/", import.meta.url));
const PORT = Number(process.env.PORT || 8787);
const sessions = new Map();
const SESSION_TTL_MS = 8 * 60 * 60 * 1000;
const APP_ID = process.env.DISCORD_APPLICATION_ID || "";

function env(name) {
  const value = process.env[name];
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
  return value;
}
function cookieValue(header, name) {
  return Object.fromEntries((header || "").split(";").map(v => v.trim().split(/=(.*)/s).slice(0, 2))).get(name);
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
  if (typeof origin !== "string" || typeof req.headers.host !== "string") return false;
  try { return new URL(origin).host === req.headers.host; } catch { return false; }
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
        return send(res, 200, { ok: true, service: "discord-control-activity", hostMetrics: "available", controlActions: "disabled" });
      }
      if (req.method === "POST" && ["/api/session", "/api/logout", "/api/services/action", "/api/containers/action"].includes(url.pathname) && !hasSameOrigin(req)) {
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
        sessions.set(sid, { userId: user.id, username: user.global_name || user.username, expiresAt: Date.now() + SESSION_TTL_MS });
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
        if (req.method === "GET" && url.pathname === "/api/system") {
          return send(res, 200, await getSystemSnapshot());
        }
        if (req.method === "GET" && url.pathname === "/api/services") {
          return send(res, 200, { services: await getServiceSnapshot() });
        }
        if (req.method === "POST" && url.pathname === "/api/services/action") {
          const { serviceId, action } = await readJson(req, 4096);
          const result = await performServiceAction(serviceId, action);
          console.info(JSON.stringify({ event: "authorized_control", userId: session.userId, kind: "service", serviceId, action }));
          return send(res, 200, result);
        }
        if (req.method === "GET" && url.pathname === "/api/containers") {
          return send(res, 200, await getContainerSnapshot());
        }
        if (req.method === "POST" && url.pathname === "/api/containers/action") {
          const { containerId, action } = await readJson(req, 4096);
          const result = await performContainerAction(containerId, action);
          console.info(JSON.stringify({ event: "authorized_control", userId: session.userId, kind: "container", containerId: result.containerId, action }));
          return send(res, 200, result);
        }
        if (req.method === "GET" && url.pathname === "/api/overview") {
          return send(res, 200, {
            user: { id: session.userId, name: session.username },
            machine: { state: "connected", detail: "Read-only metrics come from the host running this Activity. Service controls and AI actions are not enabled yet." },
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
