const applicationId = process.env.DISCORD_APPLICATION_ID;
const botToken = process.env.DISCORD_BOT_TOKEN;

if (!applicationId || !/^\d{17,20}$/.test(applicationId)) {
  throw new Error("Set DISCORD_APPLICATION_ID to the Discord application ID.");
}
if (!botToken) {
  throw new Error("Set DISCORD_BOT_TOKEN in the deployment environment. Never commit the token.");
}

const command = {
  name: "launch",
  description: "Open the private Nexus Control Activity",
  type: 4,
  handler: 2,
  integration_types: [1],
  contexts: [2],
};

const response = await fetch(`https://discord.com/api/v10/applications/${applicationId}/commands`, {
  method: "POST",
  headers: {
    authorization: `Bot ${botToken}`,
    "content-type": "application/json",
  },
  body: JSON.stringify(command),
  signal: AbortSignal.timeout(10000),
});

const body = await response.json().catch(() => ({}));
if (!response.ok) {
  throw new Error(`Discord rejected Entry Point command registration (${response.status}): ${body.message || "unknown error"}`);
}

console.log(JSON.stringify({
  ok: true,
  name: body.name,
  type: body.type,
  handler: body.handler,
  contexts: body.contexts,
  integration_types: body.integration_types,
}, null, 2));
