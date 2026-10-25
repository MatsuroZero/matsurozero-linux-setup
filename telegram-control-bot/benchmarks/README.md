# Local Ollama smoke benchmark

This optional benchmark compares the same six short prompts across local Ollama
models and records request latency, Ollama-reported duration, token counts, and
raw answers. It is a repeatable smoke test, not a comprehensive quality score.

From the repository root, with Ollama running:

```bash
python telegram-control-bot/benchmarks/ollama_smoke.py \
  --models qwen3:0.6b qwen3:1.7b \
  --repeats 1 \
  --output ollama-smoke-results.json
```

It makes sequential requests and does not alter the bot service or model config.
The first request for a model may include model-load time; rerun the benchmark
to compare warm runs, and keep the model list, prompts, settings, and hardware
constant when comparing results. Do not treat one run as a reliable latency
estimate.

Review the recorded answers as well as timing. The harness reports outputs
without automatically grading nuanced language quality; the deterministic
unit tests remain the correctness gate for arithmetic and memory routing.
