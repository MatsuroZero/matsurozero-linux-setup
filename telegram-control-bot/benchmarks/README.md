# Local Ollama quality and latency benchmark

This optional benchmark compares eighteen general-purpose prompts, including evidence-grounding and abstention cases across local
Ollama models. It records response latency, Ollama-reported duration, model
load time, token counts, estimated generation throughput, raw answers, and
transparent heuristic checks for obvious requirements.

From the repository root, with Ollama running:

```bash
python telegram-control-bot/benchmarks/ollama_smoke.py \
  --models qwen3:0.6b qwen3:1.7b \
  --repeats 1 \
  --output ollama-smoke-results.json
```

It makes sequential requests, keeps each model alive for ten minutes, uses a
fixed seed and temperature zero, and does not alter the bot service or global
Ollama configuration. A first request may still include model-load time.
Rerun to compare warm runs; keep model list, prompts, settings, and hardware
constant when comparing results.

The quality pass rate is **not** a comprehensive model score. Checks look for
simple observable properties such as the expected arithmetic result, requested
list length, grounded recall, and explicit abstention when a fact is missing.
They can miss incorrect reasoning and can occasionally flag a valid paraphrase.
Review the raw answers too. The deterministic unit tests remain the correctness
gate for arithmetic and memory routing.
