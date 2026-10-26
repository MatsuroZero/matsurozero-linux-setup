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


class PiDigitTests(unittest.TestCase):
    def test_first_50_significant_digits_of_pi(self):
        self.assertEqual(
            bot._pi_digits(50),
            "3.1415926535897932384626433832795028841971693993751",
        )

    def test_pi_request_is_handled_deterministically(self):
        answer = bot._answer_pi_request("write 300 digits of pi")
        self.assertTrue(answer.startswith("π (300 significant digits"))
        digits = answer.splitlines()[1]
        self.assertEqual(len(digits.replace(".", "")), 300)
        self.assertEqual(digits[:22], "3.14159265358979323846")

    def test_pi_request_without_count_uses_reasonable_default(self):
        answer = bot._answer_pi_request("tell me some digits of pi")
        self.assertEqual(len(answer.splitlines()[1].replace(".", "")), 50)

    def test_unrelated_pi_mention_is_not_treated_as_digit_request(self):
        self.assertIsNone(bot._answer_pi_request("What is pi and why is it irrational?"))


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

    def test_math_expression_before_chatty_suffix(self):
        question = (
            "My dear, could you help with this mathematical question? "
            "whats 347 * 28, well technically not hard but im lazy to solve it"
        )
        self.assertEqual(bot._answer_arithmetic_question(question), "9716")
        self.assertTrue(bot._is_math_primary_request(question))

    def test_chatty_multiplication_returns_worked_exact_result(self):
        answer = bot._format_math_response(
            "What's 347 * 28, I'm too lazy to calculate it",
            "9716",
        )
        self.assertEqual(
            answer,
            "347 × 28 = 347 × (20 + 8) = 6,940 + 2,776 = 9,716.",
        )

    def test_math_primary_request_does_not_swallow_second_task(self):
        self.assertFalse(
            bot._is_math_primary_request("What's 347 * 28, and also tell me Japan's capital?")
        )

    def test_explicit_research_requests_get_larger_budget(self):
        self.assertTrue(bot._wants_long_answer("Explain this step by step for my research and cite sources"))
        self.assertFalse(bot._wants_long_answer("Hey, how are you?"))

    def test_exact_fraction(self):
        self.assertEqual(bot._answer_arithmetic_question("1 / 3"), "1/3")

    def test_division_by_zero_is_not_answered(self):
        self.assertIsNone(bot._answer_arithmetic_question("1 / 0"))

    def test_arbitrary_python_is_not_executed(self):
        self.assertIsNone(
            bot._answer_arithmetic_question("__import__('os').system('echo unsafe')")
        )


    def test_research_query_includes_recent_topic_context(self):
        history = [
            {"role": "user", "content": "Let's watch JoJo's Bizarre Adventure together, I like Jotaro a lot."},
            {"role": "assistant", "content": "The cast is amazing."},
        ]
        query = bot._evidence_question_for_turn(
            "Could you do proper research and prove the cast names?", history
        )
        self.assertIn("JoJo's Bizarre Adventure", query)
        self.assertIn("Jotaro", query)
        self.assertIn("research", query.casefold())

    def test_contextual_media_preferences_store_title_and_character(self):
        session = {"temporary_facts": [], "history": []}
        self.assertTrue(bot._extract_explicit_temporary_fact(
            session,
            "Let's watch JoJo's Bizarre Adventure together, I really love this show, "
            "it's my favorite anime, I like Jotaro a lot, do you know any of the cast?",
        ))
        facts = [item["fact"] for item in session["temporary_facts"]]
        self.assertIn("Matthew's favorite anime is JoJo's Bizarre Adventure", facts)
        self.assertIn("Matthew likes Jotaro from JoJo's Bizarre Adventure", facts)


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

    def test_preference_extraction_stops_before_followup_question(self):
        self.assertTrue(
            bot._extract_explicit_temporary_fact(
                self.session, "I love JoJo's Bizarre Adventure, do you know it?"
            )
        )
        fact = self.session["temporary_facts"][0]["fact"]
        self.assertIn("JoJo's Bizarre Adventure", fact)
        self.assertNotIn("do you know it", fact.casefold())

    def test_does_not_turn_question_into_preference(self):
        self.assertFalse(
            bot._extract_explicit_temporary_fact(
                self.session, "Do you like playing Minecraft Java Edition?"
            )
        )
        self.assertEqual(self.session["temporary_facts"], [])

    def test_favorite_show_recall_from_recent_user_message(self):
        history = [
            {"role": "user", "content": "JoJo's Bizarre Adventure is my favorite show."},
        ]
        self.assertEqual(
            bot._answer_from_recent_disclosures("What's my favorite show?", history),
            "You told me JoJo's Bizarre Adventure is your favorite show/anime.",
        )

    def test_saved_memory_recalls_love_fact_without_model(self):
        original = bot._load_ollama_memory
        try:
            bot._load_ollama_memory = lambda: {"profile": "", "facts": ["i love jojo"]}
            self.assertEqual(
                bot._answer_from_saved_memory("what do i love"),
                "You've told me you love jojo.",
            )
        finally:
            bot._load_ollama_memory = original

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
