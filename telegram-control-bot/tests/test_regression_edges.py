"""Additional edge-case tests for deterministic bot behavior."""
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import bot  # noqa: E402


class ArithmeticEdgeCaseTests(unittest.TestCase):
    def test_parentheses_and_precedence(self):
        self.assertEqual(bot._answer_arithmetic_question("(2 + 3) * 4"), "20")

    def test_integer_division_returns_exact_integer(self):
        self.assertEqual(bot._answer_arithmetic_question("12 / 3"), "4")

    def test_negative_result(self):
        self.assertEqual(bot._answer_arithmetic_question("3 - 8"), "-5")

    def test_caret_is_treated_as_exponentiation(self):
        self.assertEqual(bot._answer_arithmetic_question("2^3"), "8")

    def test_negative_integer_exponent_returns_exact_fraction(self):
        self.assertEqual(bot._answer_arithmetic_question("2**-2"), "1/4")

    def test_rejects_exponent_outside_safe_range(self):
        self.assertIsNone(bot._answer_arithmetic_question("2**1001"))

    def test_rejects_expression_with_code_suffix(self):
        self.assertIsNone(bot._answer_arithmetic_question("1 + 1; print('oops')"))

    def test_rejects_empty_input(self):
        self.assertIsNone(bot._answer_arithmetic_question("   "))

    def test_standalone_math_is_eligible_for_fast_path(self):
        self.assertTrue(bot._is_standalone_arithmetic_question("What's (2 + 3) * 4?"))

    def test_mixed_message_is_not_short_circuited(self):
        self.assertFalse(
            bot._is_standalone_arithmetic_question(
                "I love Terraria, what's 1 + 1?"
            )
        )

    def test_rejects_expressions_with_too_many_ast_nodes(self):
        expression = " + ".join(["1"] * 40)
        self.assertIsNone(bot._answer_arithmetic_question(expression))


class PromptMemorySelectionTests(unittest.TestCase):
    def test_retrieves_only_relevant_tagged_saved_facts(self):
        memory = {
            "profile": (
                "ABOUT MATTHEW: Matthew is a law student.\n"
                "GAMING AND MEDIA: Matthew enjoys Terraria.\n"
                "FOOD: Matthew likes pizza."
            ),
            "facts": [
                "gaming: Matthew's favorite game is Terraria",
                "food: Matthew's favorite food is pizza",
            ],
        }
        with patch.object(bot, "_load_ollama_memory", return_value=memory):
            prompt = bot._ollama_system_prompt("Recommend games", [])
        self.assertIn("Matthew's favorite game is Terraria", prompt)
        self.assertNotIn("Matthew's favorite food is pizza", prompt)
        self.assertIn("CORE IDENTITY CONTEXT", prompt)
        self.assertIn("RELEVANT GAMING MEMORY", prompt)
        self.assertNotIn("RELEVANT FOOD MEMORY", prompt)

    def test_general_greeting_does_not_load_unrelated_tagged_facts(self):
        memory = {
            "profile": "",
            "facts": [
                "gaming: Matthew's favorite game is Terraria",
                "food: Matthew's favorite food is pizza",
            ],
        }
        with patch.object(bot, "_load_ollama_memory", return_value=memory):
            prompt = bot._ollama_system_prompt("Hello!", [])
        self.assertNotIn("Matthew's favorite game is Terraria", prompt)
        self.assertNotIn("Matthew's favorite food is pizza", prompt)


class MemoryEdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.session = {"temporary_facts": []}

    def test_food_does_not_accidentally_match_style_category(self):
        categories = bot._text_categories("I love food.")
        self.assertIn("food", categories)
        self.assertNotIn("style", categories)

    def test_generic_show_verb_does_not_match_creative_category(self):
        categories = bot._text_categories("Show me how the project works.")
        self.assertNotIn("creative", categories)

    def test_topic_categories_are_independent(self):
        self.assertIn("linux", bot._text_categories("My Linux desktop is slow."))
        self.assertIn("learning", bot._text_categories("I'm studying law."))
        self.assertIn("food", bot._text_categories("I want a pizza recipe."))
        self.assertNotIn("gaming", bot._text_categories("I want a pizza recipe."))

    def test_new_favorite_replaces_old_favorite_of_same_type(self):
        bot._add_temporary_fact(self.session, "gaming", "Matthew's favorite game is Minecraft")
        bot._add_temporary_fact(self.session, "gaming", "Matthew's favorite game is Terraria")
        facts = self.session["temporary_facts"]
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["fact"], "Matthew's favorite game is Terraria")

    def test_favorite_game_update_preserves_unrelated_facts(self):
        bot._add_temporary_fact(self.session, "food", "Matthew's favorite food is pizza")
        bot._add_temporary_fact(self.session, "gaming", "Matthew's favorite game is Minecraft")
        bot._add_temporary_fact(self.session, "gaming", "Matthew's favorite game is Terraria")
        facts = [item["fact"] for item in self.session["temporary_facts"]]
        self.assertEqual(len(facts), 2)
        self.assertIn("Matthew's favorite food is pizza", facts)
        self.assertIn("Matthew's favorite game is Terraria", facts)

    def test_caps_temporary_facts_at_twenty(self):
        for index in range(25):
            self.assertTrue(
                bot._add_temporary_fact(self.session, "gaming", f"Fact {index}")
            )
        facts = self.session["temporary_facts"]
        self.assertEqual(len(facts), 20)
        self.assertEqual(facts[0]["fact"], "Fact 5")
        self.assertEqual(facts[-1]["fact"], "Fact 24")

    def test_does_not_recall_facts_from_assistant_claims(self):
        history = [
            {"role": "assistant", "content": "Your favorite game is Minecraft."}
        ]
        self.assertIsNone(
            bot._answer_from_recent_disclosures("What's my favorite game?", history)
        )

    def test_does_not_guess_when_no_disclosure_exists(self):
        history = [{"role": "user", "content": "I played a game yesterday."}]
        self.assertIsNone(
            bot._answer_from_recent_disclosures("What's my favorite game?", history)
        )

    def test_latest_explicit_favorite_game_wins(self):
        history = [
            {"role": "user", "content": "Minecraft is my favorite game."},
            {"role": "user", "content": "Actually, Terraria is my favorite game."},
        ]
        self.assertEqual(
            bot._answer_from_recent_disclosures("What's my favorite game?", history),
            "You said Terraria is your favorite game.",
        )


if __name__ == "__main__":
    unittest.main()
