# Discord Control Activity

A private Discord Activity dashboard for monitoring and controlling the Linux host that runs it. This is an incremental build in a draft PR, not yet a deployed or fully parity-complete replacement for the Telegram bot and Nexus CLI.

## Implemented

- Responsive Activity UI with Overview, System, Services, Containers, Network, AI Assistant, Dashboards, Nexus Commands, Diagnostics, and Power Controls.
- Discord Embedded App SDK browser import, server-side OAuth code exchange, secure session cookie, and strict allowlist checks against `DISCORD_ALLOWED_USER_ID`.
- Read-only CPU, memory, root disk, uptime, top processes, Linux thermal zones when exposed, network interfaces, and sampled RX/TX throughput. The Activity refreshes these metrics every 10 seconds while visible, without overlapping refreshes.
- Fixed allowlist of systemd services, status/autostart state, start/stop/restart, and enable/disable autostart actions.
- Fixed Docker container inventory and start/stop/restart/log actions, plus configured Compose project status/actions/logs for `odysseus`, `uptime-kuma`, and `localnexus`.
- Confirmation-based Serpantinum lock, suspend, reboot, and shutdown actions.
- Health endpoint checks and a Nexus-style doctor summary.
- Listing and confirmed execution of pre-registered Nexus commands/profiles. The Activity accepts saved names only and executes stored argument arrays without enabling shell mode. Profiles stop after the first failed step.
- Ollama local chat and optional Gemini chat with bounded input, model allowlists, session-only conversation history, and persistent profile/fact memory.
- Persistent memory uses the Telegram bot's default JSON path: `~/.local/share/telegram-control-bot/ollama_memory.json`. Set `OLLAMA_MEMORY_PATH` consistently if the Telegram bot uses a custom path.
- Local dashboard links and a PIN-protected credential reveal endpoint. The PIN is blank by default, must be configured to at least six characters, and is rate-limited after failed attempts.
- Node.js tests for authorization, API boundaries, control validation, memory persistence, registry validation, and diagnostic response shapes.

## Requirements and Discord setup

- Node.js 22 or newer.
- HTTPS hosting for the Activity and an Activity URL mapping in the Discord Developer Portal.
- Activities enabled for the application.
- User installation and private-channel contexts enabled. The provided registration script configures a user-installed Entry Point command for private channels.
- Register the redirect URI used by the server in the Developer Portal. The sample is `https://127.0.0.1`; the server's `DISCORD_REDIRECT_URI` must match the registered value exactly.
- Configure `DISCORD_APPLICATION_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_ALLOWED_USER_ID`, and `DISCORD_REDIRECT_URI` in the server environment.
- Set `LINKS_PIN` and only add dashboard credentials if you want the credential reveal feature enabled.

The Entry Point registration script uses `DISCORD_BOT_TOKEN` from the server environment. The token is never sent to the browser or stored in the repository.

## Install the user service

From the `discord-control-bot` directory:

```bash
bash deploy/install-user-service.sh
```

The installer creates `~/.config/discord-control-activity/env` with private permissions and installs a systemd user unit template. Edit the environment file before starting the service. It deliberately does not start the app until you configure the Discord application ID and client secret.

After configuration:

```bash
systemctl --user enable --now discord-control-activity.service
journalctl --user -u discord-control-activity.service -f
```

Review `deploy/discord-control-activity.service.example` before enabling it. The unit has filesystem hardening and grants write access to `~/Projects`, the Nexus registry directory, and the default shared-memory directory. Add other write paths only when needed and after reviewing why a saved command requires them.

## Manual checks

From `discord-control-bot`, with Node.js 22+ and environment variables configured:

```bash
npm run check
npm test
npm start
```

The GitHub Actions workflow is in `.github/workflows/discord-control-ci.yml`. The connected GitHub integration has not exposed a verifiable successful run for the current head yet, so the test suite must still be considered **unverified**. The workflow includes syntax checks, the Node test suite, and should also validate the installer shell script.

## Important limits

- DM/private-channel Activity launch and the Developer Portal URL mapping still require verification in the actual Discord client.
- The app must run on the PC it is meant to control. If hosted elsewhere, metrics and actions apply to that other host.
- The current code has not yet extracted one shared control core used by Telegram, Nexus CLI, and Discord, so the implementations can drift.
- Docker project autostart management and editing/removing saved Nexus commands/profiles are not implemented in the Activity; those can still be managed locally with Nexus CLI.
- The memory file is shared by path and format, but the Telegram bot does not use the Activity's write coordination. Avoid simultaneous memory edits from both apps until cross-process locking is added.
- Power actions can disconnect the Activity. System service actions fail closed unless narrowly scoped sudo permissions are configured. Docker actions require Docker access for the server process.
- The app is not production-ready until CI passes, HTTPS hosting and URL mapping are configured, and DM launch is verified.

## Security notes

- Never put the bot token, client secret, Gemini API key, or dashboard passwords in frontend code or Git.
- Dashboard credentials are returned only after Discord session authentication and successful PIN verification. Three failed PIN attempts trigger a five-minute lockout; credentials are cleared from the UI when leaving the dashboard panel.
- Service, project, container, power, command, and profile APIs are allowlisted and require an authenticated session. Mutating POST routes reject cross-origin requests.
- Saved Nexus commands can still perform whatever their local registered argv permits. Review the local registry before running commands or profiles remotely.

## Official references

- [Building an Activity](https://discord.com/developers/docs/activities/building-an-activity)
- [Embedded App SDK](https://discord.com/developers/docs/developer-tools/embedded-app-sdk)
- [Application commands and contexts](https://discord.com/developers/docs/interactions/application-commands)
- [Interactions overview](https://discord.com/developers/docs/interactions/overview)
