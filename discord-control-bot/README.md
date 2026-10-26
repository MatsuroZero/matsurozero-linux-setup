# Discord Control Activity

This directory is the new Discord adapter. The implementation is intentionally being built in small, testable steps instead of copying the old bots wholesale.

## Current status

- A fail-closed server-side Discord user allowlist helper exists in src/authz.js.
- Unit tests cover exact user matching, missing/malformed configuration, and denied users.
- The migration report is at ../docs/DISCORD_NEXUS_MIGRATION_REPORT.md.
- The Activity UI, OAuth exchange, interaction handlers, and shared control API are not implemented yet. Do not treat this scaffold as a runnable bot.

## Planned architecture

1. Discord Activity frontend using the official Embedded App SDK.
2. Discord interaction adapter using current application commands and components.
3. Shared Python control layer for system status, services, Docker, network, AI chat, and Nexus operations.
4. Authenticated local bridge between the public Activity host and the CachyOS machine.
5. Independent allowlists for Discord and Telegram; every privileged backend request must re-check authorization.
6. GitHub-hosted automated tests that require no live bot token or local machine.

## Configuration planned for runtime

- DISCORD_APPLICATION_ID
- DISCORD_PUBLIC_KEY
- DISCORD_BOT_TOKEN (server-side only, never sent to the browser)
- DISCORD_ALLOWED_USER_ID
- DISCORD_CLIENT_SECRET (server-side OAuth exchange only)
- CONTROL_BRIDGE_SECRET

Use the host's secret manager or an untracked local environment file. Never commit real values.

## Important

The bot token was exposed in a chat and must be regenerated before use. Historical source repositories also contain old hard-coded webhook/API credentials; rotate any that are still active.

A GitHub Actions workflow could not be added through the current repository integration, so these tests are present in the branch but are not yet automatically executed by GitHub Actions. Run them in a CI workflow once that workflow file can be added:
- npm test
- npm run check
