# Discord / Telegram / Nexus migration report

Audit date: 2026-10-26

This audit records repository evidence without copying any tokens, webhook URLs, or credentials.

## Summary

- **MaroHDAlt/topmodderv1.2** and **MaroHDAlt/scordbothv01** could not be retrieved through the connected GitHub account (API 404), so their code could not be assessed.
- **scordbotv0.2** uses discord.js 12.5.1, Mongoose, a modular command loader, d! prefix commands, giveaway support, and server-list metadata. Its old event/API patterns should not be copied directly.
- **topmodderv1.3** uses discord.js 12.5.1 and modular prefix commands. Its feature set includes moderation/logging, server settings, economy, parties/events, giveaways, help, and mini-games. Its npm test script is only a placeholder.
- **questmaster** uses discord.js 12.5.1, Mongoose, and modular commands for profiles, inventory, levels, quests, shop, combat, and PvP. This game system is largely separate from Linux controls.
- **RestaurantBaratieTest** is empty at the default branch.
- **RestaurantBaratieWebsite** is an Express/serverless-style deployment with a small uptime/vote endpoint. Its source contains a hard-coded webhook credential and should not be reused as-is.
- **RestaurantBaratieBot** uses discord.js 14.2.0, slash commands, buttons, modals, MongoDB, and stateful order/event workflows. It is the best reference for interaction-first Discord UI, but is an older codebase with tight configuration coupling and no meaningful automated tests.
- **RestaurantBaratieGlitch** resembles the Baratie bot with slash commands, modals, MongoDB state, static assets, and Glitch-era deployment files. It looks like a deployment variant rather than a separate architecture to adopt.
- **RestaurantBaratie** currently exposes a README rather than app source. The README describes an alpha food-ordering and event bot based on slash commands.
- **yanAI** uses discord.js 14.3.0, SQLite/Sequelize, and separate content/type/module matching. It maps input to saved responses; it is not a general generative AI model. Its intent-routing idea may be useful for deterministic commands.

## Existing Telegram feature inventory

The inspected bot includes allowlisted access, an interactive menu, system status, CPU/RAM/disk/temperatures, top processes, uptime, network, service listing/actions, Docker/project controls, power controls, PIN-protected dashboard links, Ollama/Gemini chat, memory/profile/remember/forget, and a health endpoint. Lifecycle and lock-watcher scripts are also present.

## Existing Nexus feature inventory

The Nexus CLI includes service status/actions/autostart for Jellyfin, Docker, Ollama, LocalNexus, Telegram bot, dashboards, and Sunshine; Docker project/container listing and actions; logs; user-defined commands and profiles; health/doctor/overview checks; and Uptime Kuma status/logs. Several features overlap with Telegram but are implemented separately today.

## Current Discord guidance

Use the current Discord Embedded App SDK for the in-client Activity, plus application commands and interactions. Do not use the old message-prefix approach from the v12 bots. Activities are web apps and need an HTTPS URL mapping. User-installed app commands can be configured for DMs/private channels; the Activity launch path must be verified in the Developer Portal and with the target app's contexts. Authenticate the Activity through Discord and enforce authorization server-side on every request, never relying only on frontend state.

Official docs:
- [Building an Activity](https://discord.com/developers/docs/activities/building-an-activity)
- [How Activities work](https://discord.com/developers/docs/activities/how-activities-work)
- [Embedded App SDK](https://discord.com/developers/docs/developer-tools/embedded-app-sdk)
- [Interactions and commands](https://discord.com/developers/docs/interactions/overview)
- [Application commands and contexts](https://discord.com/developers/docs/interactions/application-commands)
- [discord.js v14 migration notes](https://discordjs.guide/legacy/additional-info/changes-in-v14)

## Recommended architecture

1. Extract shared, tested Linux-control operations into a Python control core. Keep Telegram as an adapter and delegate its operations to that core.
2. Keep Nexus CLI behavior, then gradually route overlapping actions through the same core.
3. Add a current Discord interaction service using slash commands, buttons, selects, and modals.
4. Add a responsive Discord Activity frontend. It must not contain credentials or perform privileged operations directly.
5. Use a narrow, authenticated local control bridge. Do not expose arbitrary shell execution to the public internet.
6. Fail closed by default. Check the configured Discord allowlisted user on every command, API request, and state-changing action. Keep Telegram's allowlist independent.
7. Run unit tests, build checks, and dependency checks in GitHub Actions. CI can validate code, but cannot prove live Discord login or local-host connectivity without a separately configured integration environment.

## Security actions

- The Discord bot token was exposed in the chat. Revoke and regenerate it before running new code.
- Historical source contains hard-coded webhook/API credentials. Rotate any still-active values and do not copy them into the new project.
- Keep tokens, OAuth secrets, database credentials, and webhook URLs out of Git. Tests should not need live secrets.
- Add confirmation for service restarts/stops, container changes, and power operations.
