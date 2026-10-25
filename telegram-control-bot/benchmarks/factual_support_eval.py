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

from factual_evidence import assess_answer_support, assess_claim_level_support  # noqa: E402


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


CLAIM_CASES = [
    {"name":"supported single claim","question":"What is the capital of Australia?","answer":"Canberra is the capital city of Australia.","evidence":{"title":"Canberra","excerpt":"Canberra is the capital city of Australia."},"expected":"candidate_supported"},
    {"name":"supported plus unsupported descriptive claim","question":"What is the capital of Australia?","answer":"Canberra is the capital city of Australia. It has excellent nightlife.","evidence":{"title":"Canberra","excerpt":"Canberra is the capital city of Australia."},"expected":"insufficient_overlap"},
    {"name":"supported plus unsupported number","question":"What is the capital of Australia?","answer":"Canberra is the capital city of Australia. It has 900 million residents.","evidence":{"title":"Canberra","excerpt":"Canberra is the capital city of Australia."},"expected":"insufficient_overlap"},
    {"name":"supported plus unsupported proper name","question":"What is the capital of Australia?","answer":"Canberra is the capital city of Australia. Matthew founded it in 1820.","evidence":{"title":"Canberra","excerpt":"Canberra is the capital city of Australia."},"expected":"insufficient_overlap"},
    {"name":"wrong entity with topic overlap","question":"Who developed general relativity?","answer":"Isaac Newton developed the theory of general relativity.","evidence":{"title":"General relativity","excerpt":"Albert Einstein developed the theory of general relativity."},"expected":"insufficient_overlap"},
    {"name":"contradictory polarity","question":"Is Canberra the capital of Australia?","answer":"Canberra is not the capital city of Australia.","evidence":{"title":"Canberra","excerpt":"Canberra is the capital city of Australia."},"expected":"insufficient_overlap"},
    {"name":"supported bullet then unsupported bullet","question":"Which planet is largest?","answer":"- Jupiter is the largest planet in the Solar System.\n- Jupiter has 80 moons.","evidence":{"title":"Jupiter","excerpt":"Jupiter is the largest planet in the Solar System."},"expected":"insufficient_overlap"},
    {"name":"all claims supported","question":"What is Canberra?","answer":"Canberra is the capital city of Australia. Canberra is located in the Australian Capital Territory.","evidence":{"title":"Canberra","excerpt":"Canberra is the capital city of Australia. Canberra is located in the Australian Capital Territory."},"expected":"candidate_supported"},
    {"name":"unsupported numeric claim","question":"What percentage of atmosphere is oxygen?","answer":"Oxygen makes up 21 percent of the atmosphere.","evidence":{"title":"Atmosphere","excerpt":"The atmosphere contains oxygen and nitrogen."},"expected":"insufficient_overlap"},
    {"name":"empty answer","question":"What is Canberra?","answer":"","evidence":{"title":"Canberra","excerpt":"Canberra is the capital city of Australia."},"expected":"unavailable"}
]

ADVERSARIAL_CASES = [
    {"name":"paraphrase government seat","question":"What is Australia's capital?","answer":"Canberra serves as Australia's seat of government.","evidence":{"title":"Canberra","excerpt":"Canberra is the capital city of Australia."},"expected":"candidate_supported"},
    {"name":"unsupported causal explanation sharing topic","question":"Why is the sky blue?","answer":"The sky appears blue because the oceans reflect blue light into the atmosphere.","evidence":{"title":"Rayleigh scattering","excerpt":"The sky appears blue because air molecules scatter shorter blue wavelengths of sunlight more strongly than longer red wavelengths."},"expected":"insufficient_overlap"},
    {"name":"unsupported time detail sharing entity","question":"When was Canberra founded?","answer":"Canberra is the capital city of Australia and was founded in 1820.","evidence":{"title":"Canberra","excerpt":"Canberra is the capital city of Australia."},"expected":"insufficient_overlap"},
    {"name":"unsupported comparison sharing topic","question":"Which planet is largest?","answer":"Jupiter is the largest planet in the Solar System and is twice as large as Saturn.","evidence":{"title":"Jupiter","excerpt":"Jupiter is the largest planet in the Solar System."},"expected":"insufficient_overlap"},
    {"name":"correct paraphrase with no shared main predicate","question":"What causes the sky to look blue?","answer":"Air molecules scatter blue sunlight more strongly than red sunlight.","evidence":{"title":"Rayleigh scattering","excerpt":"The sky appears blue because air molecules scatter shorter blue wavelengths of sunlight more strongly than longer red wavelengths."},"expected":"candidate_supported"},
    {"name":"unrelated claim with shared entity only","question":"What is Canberra?","answer":"Canberra is Australia's capital and secretly hosts a hidden spaceport run by aliens.","evidence":{"title":"Canberra","excerpt":"Canberra is the capital city of Australia."},"expected":"insufficient_overlap"},
    {"name":"false statement with exact topic words","question":"Does Canberra lie in Australia?","answer":"Canberra is not in Australia; Canberra is the capital city of Australia.","evidence":{"title":"Canberra","excerpt":"Canberra is the capital city of Australia."},"expected":"insufficient_overlap"},
    {"name":"unsupported date in numeric form","question":"When was Canberra founded?","answer":"Canberra was founded in 1820.","evidence":{"title":"Canberra","excerpt":"Canberra is the capital city of Australia."},"expected":"insufficient_overlap"},
    {"name":"supported concise answer","question":"What is the capital of Australia?","answer":"Canberra.","evidence":{"title":"Canberra","excerpt":"Canberra is the capital city of Australia."},"expected":"candidate_supported"},
    {"name":"source does not support causal claim","question":"Why does sky scatter light?","answer":"Air molecules scatter blue light because Earth has a magnetic field.","evidence":{"title":"Rayleigh scattering","excerpt":"Air molecules scatter shorter blue wavelengths of sunlight more strongly than longer red wavelengths."},"expected":"insufficient_overlap"},
    {"name":"reversed subject and object with same vocabulary","question":"What is Australia's capital?","answer":"Australia is the capital city of Canberra.","evidence":{"title":"Canberra","excerpt":"Canberra is the capital city of Australia."},"expected":"insufficient_overlap"},
    {"name":"unsupported alternate capital sharing every key term","question":"What is Australia's capital?","answer":"Canberra is the capital city of New Zealand.","evidence":{"title":"Canberra","excerpt":"Canberra is the capital city of Australia."},"expected":"insufficient_overlap"}
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
    claim_rows = []
    claim_false_accepts = claim_false_rejections = 0
    claim_true_accepts = claim_true_rejections = 0
    for case in CLAIM_CASES:
        actual = assess_claim_level_support(case["question"], case["answer"], case["evidence"])["status"]
        expected = case["expected"]
        claim_false_accepts += expected == "insufficient_overlap" and actual == "candidate_supported"
        claim_false_rejections += expected == "candidate_supported" and actual != "candidate_supported"
        claim_true_accepts += expected == "candidate_supported" and actual == "candidate_supported"
        claim_true_rejections += expected == "insufficient_overlap" and actual == "insufficient_overlap"
        claim_rows.append({"case": case["name"], "expected": expected, "actual": actual, "match": actual == expected})
    summary["claim_level_evaluation"] = {
        "cases": len(claim_rows),
        "matches": sum(row["match"] for row in claim_rows),
        "match_rate": round(sum(row["match"] for row in claim_rows) / len(claim_rows), 3),
        "true_accepts": claim_true_accepts,
        "true_rejections": claim_true_rejections,
        "false_accepts": claim_false_accepts,
        "false_rejections": claim_false_rejections,
        "precision": round(claim_true_accepts / (claim_true_accepts + claim_false_accepts), 3) if claim_true_accepts + claim_false_accepts else None,
        "recall": round(claim_true_accepts / (claim_true_accepts + claim_false_rejections), 3) if claim_true_accepts + claim_false_rejections else None,
        "results": claim_rows,
    }
    adversarial_rows = []
    adv_false_accepts = adv_false_rejections = 0
    adv_true_accepts = adv_true_rejections = 0
    for case in ADVERSARIAL_CASES:
        actual = assess_claim_level_support(case["question"], case["answer"], case["evidence"])["status"]
        expected = case["expected"]
        adv_false_accepts += expected == "insufficient_overlap" and actual == "candidate_supported"
        adv_false_rejections += expected == "candidate_supported" and actual != "candidate_supported"
        adv_true_accepts += expected == "candidate_supported" and actual == "candidate_supported"
        adv_true_rejections += expected == "insufficient_overlap" and actual == "insufficient_overlap"
        adversarial_rows.append({"case": case["name"], "expected": expected, "actual": actual, "match": actual == expected})
    summary["adversarial_claim_evaluation"] = {
        "cases": len(adversarial_rows),
        "matches": sum(row["match"] for row in adversarial_rows),
        "match_rate": round(sum(row["match"] for row in adversarial_rows) / len(adversarial_rows), 3),
        "true_accepts": adv_true_accepts, "true_rejections": adv_true_rejections,
        "false_accepts": adv_false_accepts, "false_rejections": adv_false_rejections,
        "precision": round(adv_true_accepts / (adv_true_accepts + adv_false_accepts), 3) if adv_true_accepts + adv_false_accepts else None,
        "recall": round(adv_true_accepts / (adv_true_accepts + adv_false_rejections), 3) if adv_true_accepts + adv_false_rejections else None,
        "results": adversarial_rows,
    }
    print(json.dumps(summary, indent=2))
    # Observational benchmark: limitations are reported, not treated as CI failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
