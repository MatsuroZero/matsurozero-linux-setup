"""Regression tests for benchmark case-to-grader wiring."""
import sys
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR / "benchmarks"))

import ollama_smoke  # noqa: E402


class BenchmarkGraderRegressionTests(unittest.TestCase):
    def test_case_ids_are_unique(self):
        ids = [case["id"] for case in ollama_smoke.CASES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_missing_population_case_checks_abstention(self):
        case_ids = {case["id"] for case in ollama_smoke.CASES}
        self.assertIn("evidence-missing-population", case_ids)
        passed, _ = ollama_smoke.grade(
            "evidence-missing-population",
            "I cannot determine Canberra's exact population from the provided evidence.",
        )
        self.assertTrue(passed)

    def test_wrong_capital_case_checks_correction(self):
        case_ids = {case["id"] for case in ollama_smoke.CASES}
        self.assertIn("evidence-reject-wrong-capital", case_ids)
        passed, _ = ollama_smoke.grade(
            "evidence-reject-wrong-capital",
            "No, Sydney is not the capital of Australia; the evidence says Canberra is.",
        )
        self.assertTrue(passed)


if __name__ == "__main__":
    unittest.main()
