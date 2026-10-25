#!/usr/bin/env python3
"""Repeatable local Ollama quality/latency benchmark for small general-purpose models.

The checks are deliberately transparent heuristics, not a general intelligence score.
The script is opt-in and does not change the bot service or Ollama configuration.
"""
import argparse
import json
import re
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

CASES = [
    {
        "id": "direct-answer",
        "prompt": "Answer in one short sentence: What is the purpose of a computer's RAM?",
        "check": "ram",
    },
    {
        "id": "follow-two-instructions",
        "prompt": "Give exactly two numbered ways to improve a slow desktop. Keep each under 12 words.",
        "check": "two_numbered",
    },
    {
        "id": "mixed-request",
        "prompt": "What is 19 * 23? Also explain in one short sentence why multiplication is useful.",
        "check": "contains_437",
    },
    {
        "id": "grounded-recall",
        "prompt": "Context: The user said their preferred editor is Vim. Question: Which editor did the user name? Answer only from context.",
        "check": "vim",
    },
    {
        "id": "unknown-recall",
        "prompt": "Context: The user said they played a game yesterday. Question: What is their favorite game? Do not guess.",
        "check": "abstain",
    },
    {
        "id": "summarization",
        "prompt": "Summarize in one sentence: A small model is quick but may miss details; deterministic tools can verify arithmetic, while retrieval supplies relevant facts.",
        "check": "summary",
    },
    {
        "id": "basic-fact",
        "prompt": "Which planet in our solar system is the largest? Answer with the planet name only.",
        "check": "jupiter",
    },
    {
        "id": "causal-explanation",
        "prompt": "In two short sentences, explain why keeping backups helps after a disk failure.",
        "check": "backup_cause",
    },
    {
        "id": "basic-fact-capital",
        "prompt": "What is the capital of Australia? Answer with the city name only.",
        "check": "canberra",
    },
    {
        "id": "basic-fact-boiling-point",
        "prompt": "At standard atmospheric pressure, at what temperature does pure water boil in Celsius? Answer briefly.",
        "check": "boiling_point",
    },
    {
        "id": "basic-fact-hexagon",
        "prompt": "How many sides does a hexagon have? Answer with the number only.",
        "check": "six_sides",
    },
    {
        "id": "exact-list-size",
        "prompt": "List exactly three everyday uses of a web browser, as a numbered list and nothing else.",
        "check": "three_numbered",
    },
    {
        "id": "extract-facts",
        "prompt": "Extract the city and device from this text: 'Nora lives in Cairo and uses a Galaxy phone.' Return one line as City: <city>; Device: <device>.",
        "check": "extraction",
    },
    {
        "id": "missing-detail-abstention",
        "prompt": "What exact time did Nora buy her phone? The context only says she uses a Galaxy phone. Do not infer a time.",
        "check": "abstain_time",
    },
    {
        "id": "evidence-grounded-capital",
        "prompt": "Evidence: Canberra is the capital city of Australia. Question: What is the capital of Australia? Answer in one short sentence using only the evidence.",
        "check": "evidence_canberra",
    },
    {
        "id": "evidence-missing-population",
        "prompt": "Evidence: Canberra is the capital city of Australia. Question: What is Canberra's exact population? Use only the evidence. If it is not stated, say you cannot determine it.",
        "check": "evidence_abstain",
    },
    {
        "id": "evidence-reject-wrong-capital",
        "prompt": "Evidence: Canberra is the capital city of Australia. Question: Is Sydney the capital of Australia? Answer using only the evidence and correct the claim if needed.",
        "check": "evidence_contradiction",
    },
    {
        "id": "concise-definition",
        "prompt": "Define an API in one sentence for a beginner.",
        "check": "api_definition",
    },
]

# These checks intentionally test only clear, objective properties. They cannot
# determine whether a response is fully correct, safe, or useful in every context.
def grade(case_id, response):
    text = str(response or "").strip()
    lower = text.casefold()
    if not text:
        return False, "empty response"
    if case_id == "direct-answer":
        ok = "ram" in lower and any(word in lower for word in ("temporary", "quick", "access", "store"))
    elif case_id == "follow-two-instructions":
        numbers = re.findall(r"(?m)^\s*\d+[.)]\s+(.+)$", text)
        ok = len(numbers) == 2 and all(len(item.split()) <= 12 for item in numbers)
    elif case_id == "mixed-request":
        ok = bool(re.search(r"(?<!\d)437(?!\d)", text)) and len(text.split()) >= 8
    elif case_id == "grounded-recall":
        ok = "vim" in lower
    elif case_id == "unknown-recall":
        unsupported_titles = ("minecraft", "terraria", "fortnite", "roblox", "genshin", "valorant", "minecraft")
        ok = any(phrase in lower for phrase in ("don't know", "do not know", "not provided", "not specified", "does not include", "does not provide", "did not provide", "no information", "cannot determine", "can't determine", "unknown", "not enough information")) and not any(title in lower for title in unsupported_titles)
    elif case_id == "summarization":
        ok = all(term in lower for term in ("model", "arithmetic", "retrieval"))
    elif case_id == "basic-fact":
        ok = bool(re.search(r"\bjupiter\b", lower))
    elif case_id == "causal-explanation":
        ok = any(term in lower for term in ("restore", "recover", "replace", "copy", "preventing data loss", "prevent data loss", "data is protected")) and any(term in lower for term in ("data", "files", "information"))
    elif case_id == "basic-fact-capital":
        ok = bool(re.search(r"\bcanberra\b", lower))
    elif case_id == "basic-fact-boiling-point":
        ok = bool(re.search(r"\b100\s*(?:°\s*)?(?:c|celsius)?\b", lower))
    elif case_id == "basic-fact-hexagon":
        ok = bool(re.search(r"\b6\b|\bsix\b", lower))
    elif case_id == "exact-list-size":
        numbers = re.findall(r"(?m)^\s*\d+[.)]\s+(.+)$", text)
        ok = len(numbers) == 3
    elif case_id == "extract-facts":
        ok = bool(re.search(r"city\s*:\s*cairo", lower)) and bool(re.search(r"device\s*:\s*galaxy", lower))
    elif case_id == "missing-detail-abstention":
        ok = any(phrase in lower for phrase in ("not provided", "not specified", "does not specify", "doesn't specify", "does not say", "doesn't say", "does not provide", "does not include", "context only states", "cannot determine", "can't determine", "unknown", "no information", "not enough information"))
    elif case_id == "evidence-grounded-capital":
        ok = "canberra" in lower
    elif case_id == "evidence-missing-population":
        abstains = any(phrase in lower for phrase in ("cannot determine", "can't determine", "not possible to determine", "not possible to establish", "not specified", "not stated", "not provided", "insufficient information", "does not say", "does not state", "does not provide", "none of the provided evidence"))
        ok = abstains and not any(char.isdigit() for char in lower)
    elif case_id == "evidence-reject-wrong-capital":
        denies = any(phrase in lower for phrase in ("no, ", "not the capital", "incorrect", "is not the capital", "isn't the capital"))
        ok = denies and "canberra" in lower
    elif case_id == "concise-definition":
        api_concept = any(term in lower for term in ("interface", "rules and tools", "set of rules", "standardized protocols", "standardized ways", "methods", "functions", "specification"))
        api_purpose = any(term in lower for term in ("software", "program", "application", "system", "service", "communicate", "components", "access and interact", "share data", "exchange data"))
        ok = api_concept and api_purpose and len(text.split()) <= 45
    else:
        ok = False
    return bool(ok), "pass" if ok else "heuristic check failed"


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
        "keep_alive": "10m",
        "options": {"temperature": 0, "seed": 123, "num_predict": 96},
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
    answer = str(message.get("content", "")).strip()
    return {
        "latency_ms": round(elapsed_ms, 2),
        "ollama_total_ms": round(result.get("total_duration", 0) / 1_000_000, 2),
        "load_ms": round(result.get("load_duration", 0) / 1_000_000, 2),
        "prompt_tokens": result.get("prompt_eval_count"),
        "output_tokens": result.get("eval_count"),
        "tokens_per_second": round(
            result.get("eval_count", 0) / (result.get("eval_duration", 0) / 1_000_000_000), 2
        ) if result.get("eval_duration") else None,
        "response": answer,
    }


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.999999)))
    return round(ordered[index], 2)


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
        "settings": {"think": False, "temperature": 0, "seed": 123, "num_predict": 96, "keep_alive": "10m"},
        "note": "Quality checks are transparent heuristics, not a comprehensive correctness or safety measure. Latency depends on hardware and system load.",
        "results": [],
    }
    for model in args.models:
        for repeat in range(args.repeats):
            for case in CASES:
                row = {"model": model, "repeat": repeat + 1, "case": case["id"]}
                try:
                    row.update(request_chat(args.url, model, case["prompt"], args.timeout))
                    row["quality_pass"], row["quality_note"] = grade(case["id"], row["response"])
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                    row["error"] = f"{type(exc).__name__}: {exc}"
                report["results"].append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)

    grouped = {}
    for model in args.models:
        rows = [row for row in report["results"] if row["model"] == model]
        successful = [row for row in rows if "latency_ms" in row]
        latencies = [row["latency_ms"] for row in successful]
        passing = sum(1 for row in successful if row.get("quality_pass"))
        grouped[model] = {
            "successful_requests": len(successful),
            "total_requests": len(CASES) * args.repeats,
            "quality_checks_passed": passing,
            "quality_checks_scored": len(successful),
            "quality_pass_rate_percent": round(100 * passing / len(successful), 1) if successful else None,
            "median_latency_ms": round(statistics.median(latencies), 2) if latencies else None,
            "mean_latency_ms": round(statistics.mean(latencies), 2) if latencies else None,
            "p95_latency_ms": percentile(latencies, 0.95),
            "median_tokens_per_second": round(statistics.median([
                row["tokens_per_second"] for row in successful
                if row.get("tokens_per_second") is not None
            ]), 2) if any(row.get("tokens_per_second") is not None for row in successful) else None,
        }
    report["summary"] = grouped
    print(json.dumps({"summary": grouped}, ensure_ascii=False), flush=True)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")


if __name__ == "__main__":
    main()
