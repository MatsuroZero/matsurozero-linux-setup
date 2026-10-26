# Discord Control Activity

A private Discord Activity web app scaffold for the Linux control system. The dashboard, server, OAuth code exchange, Discord Embedded App SDK handshake, server-side allowlist, and basic HTTP tests are now present. Live machine control and the Discord launch command are not complete yet.

## Implemented in this milestone

- Dark responsive Activity dashboard with modules for system status, services, containers, network, and AI assistant.
- Node.js HTTP server, static asset serving, health/config endpoints, and short-lived in-memory sessions.
- Discord OAuth authorization-code exchange on the server and SDK authentication in the Activity.
- Server-side allowlist validation against `DISCORD_ALLOWED_USER_ID`; privileged API routes re-check the session identity.
- Unit tests for allowlist behavior and HTTP boundary behavior.
- Live read-only host metrics: CPU usage sampled over a short interval, memory, root filesystem usage, uptime, network interfaces, and top processes.
- No arbitrary shell execution endpoint. Service, Docker, power, and AI actions remain disabled until the shared control layer is implemented.

## Requirements

- Node.js 22 or newer.
- HTTPS hosting for the deployed Activity.
- Discord application with an Activity URL mapping and the Embedded App SDK enabled.
- In Discord Developer Portal, add `https://127.0.0.1` as the OAuth redirect URI, following the official Activity guide.
- Configure user-install / DM contexts and test launch behavior in your own application settings. A DM launch has not yet been end-to-end verified.

## Run locally

1. Copy `.env.example` to an untracked `.env` and fill in values.
2. Set `DISCORD_APPLICATION_ID`, `DISCORD_CLIENT_SECRET`, and `DISCORD_ALLOWED_USER_ID`.
3. Set `DISCORD_REDIRECT_URI=https://127.0.0.1` as the OAuth placeholder redirect URI registered in the Developer Portal.
4. Run `npm run check`, `npm test`, then `npm start`.
5. Open the Activity through Discord. Ordinary browser preview is not a substitute for testing the Embedded App SDK handshake.

This simple server does not load `.env` automatically. Export variables in the shell or use a deployment platform's secret/environment configuration.

## Still to implement

1. Discord slash-command/Activity launch integration and verified DM launch flow.
2. Authenticated, narrow local bridge to the CachyOS control host.
3. Shared control core extracted from Telegram bot and Nexus CLI, with typed allowlisted operations.
4. Real status/action handlers for system, services, Docker, network, power controls, and AI chat.
5. Automated GitHub Actions execution of the tests. The repository integration blocked adding a workflow file; tests are committed but no workflow run has occurred yet.

## Security

- The bot token previously pasted into chat must be regenerated before use. Do not put it in the browser.
- Rotate any still-active credentials that were hard-coded in historical repositories.
- Keep `.env` untracked and never commit client secrets or bridge secrets.
- The current API reports metrics for the machine running the Node server. To monitor your CachyOS PC, run the server on that PC. Do not expose unrestricted shell execution or a privileged machine API to the public internet.

## Official references

- [Building an Activity](https://discord.com/developers/docs/activities/building-an-activity)
- [Embedded App SDK](https://discord.com/developers/docs/developer-tools/embedded-app-sdk)
- [Application commands and contexts](https://discord.com/developers/docs/interactions/application-commands)
- [Interactions overview](https://discord.com/developers/docs/interactions/overview)
