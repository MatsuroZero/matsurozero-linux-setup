"""Regression tests for deterministic logic in the Telegram/Ollama bot.

Run from this directory with:
    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import bot  # noqa: E402


class ArithmeticTests(unittest.TestCase):
    def test_plain_arithmetic(self):
        self.assertEqual(bot._answer_arithmetic_question("1 + 1"), "2")

    def test_natural_language_arithmetic(self):
        self.assertEqual(bot._answer_arithmetic_question("What's 1+1?"), "2")

    def test_math_at_end_of_mixed_message(self):
        self.assertEqual(
            bot._answer_arithmetic_question(
                "I love playing Minecraft Java Edition, what's 1+1"
            ),
            "2",
        )

    def test_exact_fraction(self):
        self.assertEqual(bot._answer_arithmetic_question("1 / 3"), "1/3")

    def test_division_by_zero_is_not_answered(self):
        self.assertIsNone(bot._answer_arithmetic_question("1 / 0"))

    def test_arbitrary_python_is_not_executed(self):
        self.assertIsNone(
            bot._answer_arithmetic_question("__import__('os').system('echo unsafe')")
        )


class TemporaryMemoryTests(unittest.TestCase):
    def setUp(self):
        self.session = {"temporary_facts": []}

    def test_extracts_game_preference_from_mixed_question(self):
        result = bot._extract_explicit_temporary_fact(
            self.session,
            "I love playing Minecraft Java Edition, what's 1+1",
        )
        self.assertTrue(result)
        self.assertEqual(len(self.session["temporary_facts"]), 1)
        fact = self.session["temporary_facts"][0]
        self.assertEqual(fact["category"], "gaming")
        self.assertIn("Minecraft Java Edition", fact["fact"])

    def test_deduplicates_same_fact_case_insensitively(self):
        self.assertTrue(
            bot._add_temporary_fact(self.session, "gaming", "Matthew likes Minecraft")
        )
        self.assertFalse(
            bot._add_temporary_fact(self.session, "gaming", "MATTHEW likes minecraft")
        )
        self.assertEqual(len(self.session["temporary_facts"]), 1)

    def test_rejects_unknown_category(self):
        self.assertFalse(bot._add_temporary_fact(self.session, "secrets", "something"))
        self.assertEqual(self.session["temporary_facts"], [])

    def test_does_not_turn_question_into_preference(self):
        self.assertFalse(
            bot._extract_explicit_temporary_fact(
                self.session, "Do you like playing Minecraft Java Edition?"
            )
        )
        self.assertEqual(self.session["temporary_facts"], [])

    def test_favorite_game_recall_from_recent_user_message(self):
        history = [
            {"role": "user", "content": "Minecraft is my favorite game."},
            {"role": "assistant", "content": "Got it."},
        ]
        answer = bot._answer_from_recent_disclosures(
            "What's my favorite game?", history
        )
        self.assertEqual(answer, "You said Minecraft is your favorite game.")

    def test_playing_preference_recall_from_recent_user_message(self):
        history = [
            {
                "role": "user",
                "content": "I love playing Minecraft Java Edition, what's 1+1",
            }
        ]
        answer = bot._answer_from_recent_disclosures(
            "What game do I love playing?", history
        )
        self.assertEqual(answer, "You said you love playing Minecraft Java Edition.")


if __name__ == "__main__":
    unittest.main()
