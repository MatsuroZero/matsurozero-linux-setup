#!/usr/bin/env python3
"""Small, repeatable local Ollama smoke benchmark for the Telegram bot models.

This is an opt-in diagnostic, not a claim of general model quality. It records
request latency and outputs for the same fixed prompts so runs can be compared.
It does not change the bot service, model settings, or Ollama configuration.
"""
import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

CASES = [
    {
        "id": "direct-answer",
        "prompt": "Answer in one short sentence: What is the purpose of a computer's RAM?",
    },
    {
        "id": "follow-two-instructions",
        "prompt": "Give exactly two numbered ways to improve a slow desktop. Keep each under 12 words.",
    },
    {
        "id": "mixed-request",
        "prompt": "What is 19 * 23? Also explain in one short sentence why multiplication is useful.",
    },
    {
        "id": "grounded-recall",
        "prompt": "Context: The user said their preferred editor is Vim. Question: Which editor did the user name? Answer only from context.",
    },
    {
        "id": "unknown-recall",
        "prompt": "Context: The user said they played a game yesterday. Question: What is their favorite game? Do not guess.",
    },
    {
        "id": "summarization",
        "prompt": "Summarize in one sentence: A small model is quick but may miss details; deterministic tools can verify arithmetic, while retrieval supplies relevant facts.",
    },
]


def request_chat(base_url, model, prompt, timeout):
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Be accurate, concise, and follow every instruction. "
                    "Do not invent facts absent from the supplied context. "
                    "For this benchmark, do not show chain-of-thought."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": 96},
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    elapsed_ms = (time.perf_counter() - started) * 1000
    message = result.get("message") or {}
    return {
        "latency_ms": round(elapsed_ms, 2),
        "ollama_total_ms": round(result.get("total_duration", 0) / 1_000_000, 2),
        "load_ms": round(result.get("load_duration", 0) / 1_000_000, 2),
        "prompt_tokens": result.get("prompt_eval_count"),
        "output_tokens": result.get("eval_count"),
        "response": str(message.get("content", "")).strip(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:11434")
    parser.add_argument("--models", nargs="+", default=["qwen3:0.6b", "qwen3:1.7b"])
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "url": args.url,
        "settings": {"think": False, "temperature": 0, "num_predict": 96},
        "note": "Latency depends on hardware, warm/cold model state, and concurrent load.",
        "results": [],
    }
    for model in args.models:
        for repeat in range(args.repeats):
            for case in CASES:
                row = {"model": model, "repeat": repeat + 1, "case": case["id"]}
                try:
                    row.update(request_chat(args.url, model, case["prompt"], args.timeout))
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                    row["error"] = f"{type(exc).__name__}: {exc}"
                report["results"].append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)

    successful = [row for row in report["results"] if "latency_ms" in row]
    if successful:
        grouped = {}
        for model in args.models:
            rows = [row for row in successful if row["model"] == model]
            if rows:
                latencies = [row["latency_ms"] for row in rows]
                grouped[model] = {
                    "successful_requests": len(rows),
                    "total_requests": len(CASES) * args.repeats,
                    "median_latency_ms": round(statistics.median(latencies), 2),
                    "mean_latency_ms": round(statistics.mean(latencies), 2),
                }
        report["summary"] = grouped
        print(json.dumps({"summary": grouped}, ensure_ascii=False), flush=True)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")


if __name__ == "__main__":
    main()
