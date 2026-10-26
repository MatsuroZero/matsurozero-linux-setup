# Discord Control Activity

A private Discord Activity web app for monitoring a Linux host and safely managing an allowlisted set of services and Docker containers. This is an incremental implementation, not yet full Telegram/Nexus feature parity.

## Implemented

- Responsive dark Activity dashboard with Overview, System, Services, Containers, Network, and AI Assistant panels.
- Pinned Discord Embedded App SDK v2.5.0 browser import; the Activity initializes through the SDK rather than relying on a missing global.
- A server-side command registration script configures the Activity Entry Point for user-installed private-channel contexts. It uses the bot token from the environment only and never sends it to the browser.
- Node.js HTTP server, health/config endpoints, Discord OAuth authorization-code exchange, Embedded App SDK authentication, and secure session cookie.
- Server-side allowlist validation against `DISCORD_ALLOWED_USER_ID`; every private API route re-checks the authenticated session identity.
- Read-only system metrics: sampled CPU utilization, memory, root filesystem usage, uptime, thermal-zone readings when exposed by Linux, network interface names, sampled per-interface RX/TX throughput, and top processes.
- Read-only inventory of known Nexus systemd services and Docker containers.
- Confirmed start/stop/restart actions for a fixed allowlist of services and containers, with server-side origin checks and per-user action cooldown. No arbitrary shell endpoint exists.
- Optional AI chat using Ollama on the Activity host or Gemini when `GEMINI_API_KEY` is configured. Model choices are constrained to the available Ollama tags and a fixed Gemini model allowlist; conversation history is session-only.
- System service actions use `sudo -n` and fail closed unless the host has narrowly scoped sudo permissions. User service actions require a working user systemd session. Docker actions require Docker access for the server process.
- Tests cover authorization, API boundaries, metric shape, inventory shape, and input validation.

## Requirements

- Node.js 22 or newer.
- HTTPS hosting for the deployed Activity.
- Discord application with Activities enabled and an Activity URL mapping.
- Add `https://127.0.0.1` as the OAuth redirect URI in the Developer Portal, following the official Activity guide.
- Configure user installation and DM/private-channel contexts. Discord's default Activity Launch entry point is expected to launch the Activity, but DM launch still needs a real client-side verification.

## Run locally

1. Copy `.env.example` to an untracked `.env` and fill in values.
2. Set `DISCORD_APPLICATION_ID`, `DISCORD_CLIENT_SECRET`, and `DISCORD_ALLOWED_USER_ID`.
3. Set `DISCORD_REDIRECT_URI=https://127.0.0.1` to match the placeholder redirect URI registered in Discord.
4. Export the environment variables in your shell or configure them in your service manager. This server does not automatically load `.env`.
5. Run `npm run check`, `npm test`, then `npm start`.
6. To configure the default Activity launch command for user-installed DMs/private channels, run `npm run register:entrypoint` once with `DISCORD_APPLICATION_ID` and `DISCORD_BOT_TOKEN` set in the server environment. Activities must already be enabled in the Developer Portal. This upserts the global `launch` Entry Point command with Discord's automatic Activity handler.
6. Open the Activity through Discord. A normal browser preview cannot complete the Embedded App SDK handshake.

To monitor your CachyOS PC, run this server on that PC. When run elsewhere, the metrics and controls apply to the server host, not your PC.

## Still to implement

1. Verify Activity launch in DMs/private channels on the actual Discord client and complete the Developer Portal URL mapping.
2. Align the service registry and container/project workflows with all current Telegram bot and Nexus CLI capabilities.
3. Integrate persistent memory/profile/remember/forget behavior from the existing Telegram bot, plus network throughput, temperature sensors, Docker logs/projects, and safe power controls.
4. Extract common control logic so Telegram, Nexus CLI, and Discord share one implementation rather than drift.
5. Automated GitHub Actions execution. The repository integration blocked adding a workflow file, so tests are committed but have not run in GitHub Actions yet.

## Security

- The Discord bot token previously pasted into chat must be regenerated before use. Never send it to the browser or commit it.
- Rotate any still-active credentials hard-coded in historical repositories.
- Keep `.env` untracked and never commit client secrets or bridge secrets.
- All service/container actions use fixed operation allowlists and argument arrays, not a shell. Review the sudoers configuration carefully; do not grant broad passwordless sudo.
- The app is not production-ready until HTTPS hosting, Activity mapping, DM behavior, and automated tests are verified.

## Official references

- [Building an Activity](https://discord.com/developers/docs/activities/building-an-activity)
- [Embedded App SDK](https://discord.com/developers/docs/developer-tools/embedded-app-sdk)
- [Application commands and contexts](https://discord.com/developers/docs/interactions/application-commands)
- [Interactions overview](https://discord.com/developers/docs/interactions/overview)
