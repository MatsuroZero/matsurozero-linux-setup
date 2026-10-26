# Discord Control Activity

A private Discord Activity web app for monitoring a Linux host and safely managing an allowlisted set of services and Docker containers. This is an incremental implementation, not yet full Telegram/Nexus feature parity.

## Implemented

- Responsive dark Activity dashboard with Overview, System, Services, Containers, Network, and AI Assistant panels.
- Pinned Discord Embedded App SDK v2.5.0 browser import; the Activity initializes through the SDK rather than relying on a missing global.
- A server-side command registration script configures the Activity Entry Point for user-installed private-channel contexts. It uses the bot token from the environment only and never sends it to the browser.
- Node.js HTTP server, health/config endpoints, Discord OAuth authorization-code exchange, Embedded App SDK authentication, and secure session cookie.
- Server-side allowlist validation against `DISCORD_ALLOWED_USER_ID`; every private API route re-checks the authenticated session identity.
- Read-only system metrics: sampled CPU utilization, memory, root filesystem usage, uptime, thermal-zone readings when exposed by Linux, network interface names, sampled per-interface RX/TX throughput, and top processes.
- Read-only inventory of known Nexus systemd services and Docker containers, including systemd autostart state.
- Health endpoint checks and a Nexus-style doctor report covering Node.js, Docker, systemd services, Compose files, and the saved registry.
- Confirmed start/stop/restart and enable/disable-autostart actions for a fixed allowlist of services, plus fixed Docker Compose project/container controls and bounded logs. Mutations use server-side origin checks and per-user action cooldown. No arbitrary shell endpoint exists.
- Fixed Serpantinum lock, suspend, reboot, and shutdown actions, each guarded by an explicit UI confirmation and server-side allowlist.
- Read-only listing and confirmed execution of pre-registered Nexus commands and profiles. The Activity accepts only saved registry names, uses argv arrays without enabling shell mode, and stops a profile at the first failed step.
- Optional AI chat using Ollama on the Activity host or Gemini when `GEMINI_API_KEY` is configured. Model choices are constrained to available Ollama tags and a fixed Gemini model allowlist; conversation history is session-only.
- Persistent profile and remembered-fact management using the same default JSON path as the Telegram bot (`~/.local/share/telegram-control-bot/ollama_memory.json`). Set `OLLAMA_MEMORY_PATH` consistently in both services if the Telegram bot overrides it.
- System service actions use `sudo -n` and fail closed unless the host has narrowly scoped sudo permissions. User service actions require a working user systemd session. Docker actions require Docker access for the server process.
- Tests cover authorization, API boundaries, metric shape, inventory shape, and input validation.

## Requirements

- Node.js 22 or newer.
- HTTPS hosting for the deployed Activity.
- Discord application with Activities enabled and an Activity URL mapping.
- Add `https://127.0.0.1` as the OAuth redirect URI in the Developer Portal, following the official Activity guide.
- Configure user installation and DM/private-channel contexts. Discord's default Activity Launch entry point is expected to launch the Activity, but DM launch still needs a real client-side verification.

## Install the user service

From the repository's `discord-control-bot` directory, run:

```bash
bash deploy/install-user-service.sh
```

The installer creates a private environment file and installs a systemd user unit template. Edit `~/.config/discord-control-activity/env` before starting the service. Set a private `LINKS_PIN` of at least six characters; the sample value is only a placeholder. The installer deliberately does not start the app until you configure the Discord application ID and client secret.

## Run locally

### Optional systemd user service template

The repository includes `deploy/discord-control-activity.service.example`. Edit its `WorkingDirectory` to the actual clone path, create `~/.config/discord-control-activity/env` with mode `600`, then install the unit under `~/.config/systemd/user/`. The template is a starting point and requires a deployment review, especially if the host needs system-level service control or Docker access. It grants write access to `~/Projects`, the Nexus registry directory, and the default shared memory directory. If saved commands need to write elsewhere, or `OLLAMA_MEMORY_PATH` points to another location, add those paths to `ReadWritePaths` after reviewing the trust implications.



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
2. Finish parity gaps in Docker project autostart workflows and additional Nexus maintenance actions.
3. Extract common control logic so Telegram, Nexus CLI, and Discord share one implementation rather than drift.
4. Verify the GitHub Actions run. The workflow exists on main and the build branch, but the connected GitHub run-list action only returns PR-triggered runs; no successful run is verified yet.

## Security

- Keep the Discord bot token server-side only. Never send it to the browser or commit it.
- Set a private `LINKS_PIN` before using credential reveal. The PIN is never logged, is checked server-side, and three failed attempts trigger a five-minute lockout. Dashboard credentials are only returned after successful authentication and PIN verification.
- Rotate any still-active credentials hard-coded in historical repositories.
- Keep `.env` untracked and never commit client secrets or bridge secrets.
- All service/container actions use fixed operation allowlists and argument arrays, not a shell. Review the sudoers configuration carefully; do not grant broad passwordless sudo.
- The app is not production-ready until HTTPS hosting, Activity mapping, DM behavior, and automated tests are verified.

## Official references

- [Building an Activity](https://discord.com/developers/docs/activities/building-an-activity)
- [Embedded App SDK](https://discord.com/developers/docs/developer-tools/embedded-app-sdk)
- [Application commands and contexts](https://discord.com/developers/docs/interactions/application-commands)
- [Interactions overview](https://discord.com/developers/docs/interactions/overview)
