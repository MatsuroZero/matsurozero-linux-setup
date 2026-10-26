const OLLAMA_BASE_URL = process.env.OLLAMA_BASE_URL || "http://127.0.0.1:11434";
const GEMINI_MODEL_ALLOWLIST = new Set(["gemini-2.5-flash"]);
const MAX_MESSAGE_CHARS = 4000;

function ollamaUrl(path) {
  const base = new URL(OLLAMA_BASE_URL);
  if (!["http:", "https:"].includes(base.protocol) || base.username || base.password) {
    throw new Error("OLLAMA_BASE_URL must be a valid HTTP(S) URL without embedded credentials");
  }
  return new URL(path, base).toString();
}
async function ollamaModels() {
  try {
    const response = await fetch(ollamaUrl("/api/tags"), { signal: AbortSignal.timeout(3500) });
    if (!response.ok) return { available: false, models: [] };
    const data = await response.json();
    return { available: true, models: Array.isArray(data.models) ? data.models.map(item => item.name).filter(name => typeof name === "string").slice(0, 100) : [] };
  } catch { return { available: false, models: [] }; }
}
export async function getModelCatalog() {
  const local = await ollamaModels();
  return {
    providers: {
      ollama: { available: local.available, models: local.models, label: "Ollama (local)" },
      gemini: { available: Boolean(process.env.GEMINI_API_KEY), models: process.env.GEMINI_API_KEY ? [...GEMINI_MODEL_ALLOWLIST] : [], label: "Gemini (cloud)" },
    },
  };
}
export async function chat({ provider, model, message, history = [] }) {
  if (typeof message !== "string" || !message.trim() || message.length > MAX_MESSAGE_CHARS) {
    throw Object.assign(new Error("Message must contain 1 to 4000 characters"), { status: 400 });
  }
  if (!["ollama", "gemini"].includes(provider)) throw Object.assign(new Error("Unsupported AI provider"), { status: 400 });
  const catalog = await getModelCatalog();
  const providerInfo = catalog.providers[provider];
  if (!providerInfo.available) throw Object.assign(new Error(provider === "ollama" ? "Ollama is unavailable on the Activity host" : "Gemini is not configured on the server"), { status: 503 });
  if (typeof model !== "string" || !providerInfo.models.includes(model)) throw Object.assign(new Error("Choose a model from the available model list"), { status: 400 });

  const prior = Array.isArray(history) ? history.filter(item => item && ["user", "assistant"].includes(item.role) && typeof item.content === "string").slice(-12) : [];
  const promptHistory = [...prior, { role: "user", content: message }];
  let answer;
  if (provider === "ollama") {
    const response = await fetch(ollamaUrl("/api/chat"), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ model, stream: false, messages: [{ role: "system", content: "You are Nexus, a concise and helpful assistant running for the owner of this Linux control dashboard." }, ...promptHistory] }),
      signal: AbortSignal.timeout(90_000),
    });
    if (!response.ok) throw Object.assign(new Error("Ollama chat request failed"), { status: 502 });
    const data = await response.json();
    answer = data.message?.content;
  } else {
    const response = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-goog-api-key": process.env.GEMINI_API_KEY },
      body: JSON.stringify({
        systemInstruction: { parts: [{ text: "You are Nexus, a concise and helpful assistant running for the owner of this Linux control dashboard." }] },
        contents: promptHistory.map(item => ({ role: item.role === "assistant" ? "model" : "user", parts: [{ text: item.content }] })),
        generationConfig: { temperature: 0.7, maxOutputTokens: 1200 },
      }),
      signal: AbortSignal.timeout(90_000),
    });
    if (!response.ok) throw Object.assign(new Error("Gemini chat request failed"), { status: 502 });
    const data = await response.json();
    answer = data.candidates?.[0]?.content?.parts?.map(part => part.text || "").join("");
  }
  if (typeof answer !== "string" || !answer.trim()) throw Object.assign(new Error("The AI provider returned an empty response"), { status: 502 });
  return { answer: answer.slice(0, 12000), history: [...promptHistory, { role: "assistant", content: answer.slice(0, 12000) }].slice(-24) };
}
