"""Evaluate the lexical evidence-support heuristic against labeled examples.

Run from this directory with:
    python benchmarks/factual_support_eval.py

This is a small diagnostic set, not a representative accuracy benchmark. It uses
synthetic examples and no network/model calls. "Supported" means the reference
text contains enough lexical evidence for the current heuristic to pass, not
that the answer has been independently proven true.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

BOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

from factual_evidence import assess_answer_support  # noqa: E402


CASES = [
    {
        "name": "direct correct capital",
        "question": "What is the capital of Australia?",
        "answer": "Canberra is the capital of Australia.",
        "evidence": {"title": "Canberra", "excerpt": "Canberra is the capital city of Australia."},
        "expected": "candidate_supported",
    },
    {
        "name": "correct paraphrase",
        "question": "What is the capital of Australia?",
        "answer": "Australia's capital city is Canberra.",
        "evidence": {"title": "Canberra", "excerpt": "Canberra is the capital city of Australia."},
        "expected": "candidate_supported",
    },
    {
        "name": "wrong capital",
        "question": "What is the capital of Australia?",
        "answer": "Sydney is the capital of Australia.",
        "evidence": {"title": "Canberra", "excerpt": "Canberra is the capital city of Australia."},
        "expected": "insufficient_overlap",
    },
    {
        "name": "unrelated answer",
        "question": "What is the capital of Australia?",
        "answer": "Mars has two moons named Phobos and Deimos.",
        "evidence": {"title": "Canberra", "excerpt": "Canberra is the capital city of Australia."},
        "expected": "insufficient_overlap",
    },
    {
        "name": "correct entity plus unsupported detail",
        "question": "What is the capital of Australia?",
        "answer": "Canberra is Australia's capital and has a population of 900 million.",
        "evidence": {"title": "Canberra", "excerpt": "Canberra is the capital city of Australia."},
        "expected": "insufficient_overlap",
    },
    {
        "name": "answer repeats only question terms",
        "question": "What is the capital of Australia?",
        "answer": "The capital of Australia.",
        "evidence": {"title": "Canberra", "excerpt": "Canberra is the capital city of Australia."},
        "expected": "insufficient_overlap",
    },
    {
        "name": "relevant article but answer misses key entity",
        "question": "Who developed the theory of general relativity?",
        "answer": "Isaac Newton developed the theory of general relativity.",
        "evidence": {"title": "General relativity", "excerpt": "Albert Einstein developed the theory of general relativity."},
        "expected": "insufficient_overlap",
    },
    {
        "name": "contradictory negation",
        "question": "Is Canberra the capital of Australia?",
        "answer": "Canberra is not the capital of Australia.",
        "evidence": {"title": "Canberra", "excerpt": "Canberra is the capital city of Australia."},
        "expected": "insufficient_overlap",
    },
    {
        "name": "unsupported named entity despite topic overlap",
        "question": "Who developed the theory of general relativity?",
        "answer": "Albert Einstein and Isaac Newton jointly developed general relativity.",
        "evidence": {"title": "General relativity", "excerpt": "Albert Einstein developed the theory of general relativity."},
        "expected": "insufficient_overlap",
    },
    {
        "name": "unsupported percentage",
        "question": "What percentage of Earth's atmosphere is oxygen?",
        "answer": "Earth's atmosphere is 21% oxygen and 79% nitrogen.",
        "evidence": {"title": "Earth's atmosphere", "excerpt": "Earth's atmosphere contains oxygen and nitrogen."},
        "expected": "insufficient_overlap",
    },
    {
        "name": "correct fact plus unsupported descriptive detail",
        "question": "What is the capital of Australia?",
        "answer": "Canberra is Australia's capital and has excellent nightlife.",
        "evidence": {"title": "Canberra", "excerpt": "Canberra is the capital city of Australia."},
        "expected": "insufficient_overlap",
    },
    {
        "name": "right answer with extra wording",
        "question": "Which planet is largest in the Solar System?",
        "answer": "Jupiter is the largest planet in the Solar System.",
        "evidence": {"title": "Jupiter", "excerpt": "Jupiter is the largest planet in the Solar System."},
        "expected": "candidate_supported",
    },
]


def main() -> int:
    rows = []
    false_accepts = 0
    false_rejections = 0
    true_accepts = 0
    true_rejections = 0
    for case in CASES:
        actual = assess_answer_support(
            case["question"], case["answer"], case["evidence"]
        )["status"]
        expected = case["expected"]
        passed = actual == expected
        false_accepts += expected == "insufficient_overlap" and actual == "candidate_supported"
        false_rejections += expected == "candidate_supported" and actual != "candidate_supported"
        true_accepts += expected == "candidate_supported" and actual == "candidate_supported"
        true_rejections += expected == "insufficient_overlap" and actual == "insufficient_overlap"
        rows.append({
            "case": case["name"],
            "expected": expected,
            "actual": actual,
            "match": passed,
        })

    precision_denominator = true_accepts + false_accepts
    recall_denominator = true_accepts + false_rejections
    summary = {
        "cases": len(rows),
        "matches": sum(row["match"] for row in rows),
        "match_rate": round(sum(row["match"] for row in rows) / len(rows), 3),
        "true_accepts": true_accepts,
        "true_rejections": true_rejections,
        "false_accepts": false_accepts,
        "false_rejections": false_rejections,
        "precision": round(true_accepts / precision_denominator, 3) if precision_denominator else None,
        "recall": round(true_accepts / recall_denominator, 3) if recall_denominator else None,
        "warning": "Synthetic diagnostic only; lexical overlap is not truth verification.",
        "results": rows,
    }
    print(json.dumps(summary, indent=2))
    # This is an observational benchmark: known heuristic limitations should be
    # visible in the metrics, not mistaken for a CI/runtime failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
