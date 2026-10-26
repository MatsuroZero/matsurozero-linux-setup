# Gemini model support

The Telegram bot can use local Ollama models or Gemini through Google's OpenAI-compatible API.

## Configure locally

1. Create or rotate your Google AI Studio API key at https://aistudio.google.com/apikey. Never paste it into chat, source code, GitHub issues, or logs.
2. Add these lines to the existing `.env` file in the bot's deployment directory. Do not replace the file or remove existing settings:

   ```dotenv
   GEMINI_API_KEY=put_your_rotated_key_here
   GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
   GEMINI_TIMEOUT=90
   ```

3. Restart the existing `telegram-control-bot.service` using your normal deployment process.

## Use it

- `/chat`: list installed Ollama models and show the Gemini arrow button.
- Tap **Gemini models →** to fetch model IDs available to the configured API key.
- `/chat gemini-2.5-flash`: select a currently available Gemini model directly (replace the example with an ID from the list).
- `/chat qwen3:1.7b`: select a local model directly.
- `/bye`: end the active chat.

The model list is fetched from Google's API rather than hardcoded. The existing assistant prompt, saved profile, persistent facts, short conversation history, math fast paths, and evidence retrieval are shared with Gemini. The temporary conversation transcript is kept in bot memory and resets when a new chat starts; persistent `/memory`, `/profile`, `/remember`, and `/forget` storage remains shared.

**Privacy:** Gemini prompts and the conversation context included with each request are sent to Google's API for processing. Do not send sensitive information unless you are comfortable sharing it with that provider. The API key is read from the local environment and must never be committed.
