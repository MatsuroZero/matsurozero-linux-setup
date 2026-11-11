"""Tests for the optional factual evidence retrieval layer."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import factual_evidence  # noqa: E402


class EvidenceRoutingTests(unittest.TestCase):
    def test_objective_knowledge_question_uses_retrieval(self):
        self.assertTrue(
            factual_evidence.should_retrieve_factual_evidence(
                "What is the capital of Australia?"
            )
        )

    def test_personal_context_is_not_sent_to_external_source(self):
        for question in (
            "What is my favorite game?",
            "What did I tell you about my phone?",
            "Who are you?",
        ):
            with self.subTest(question=question):
                self.assertFalse(
                    factual_evidence.should_retrieve_factual_evidence(question)
                )

    def test_current_affairs_are_not_sent_to_stale_encyclopedia_search(self):
        for question in (
            "What is the latest news today?",
            "Who is the current president of France?",
            "What is currently happening in Sudan?",
        ):
            with self.subTest(question=question):
                self.assertFalse(
                    factual_evidence.should_retrieve_factual_evidence(question)
                )

    def test_recommendations_and_creative_requests_skip_retrieval(self):
        self.assertFalse(
            factual_evidence.should_retrieve_factual_evidence(
                "Recommend a game like Terraria"
            )
        )
        self.assertFalse(
            factual_evidence.should_retrieve_factual_evidence(
                "Write a story about a moon rabbit"
            )
        )

    def test_arithmetic_skips_retrieval(self):
        self.assertFalse(
            factual_evidence.should_retrieve_factual_evidence("What is 19 * 23?")
        )

    def test_question_length_is_bounded(self):
        self.assertFalse(
            factual_evidence.should_retrieve_factual_evidence("What is " + "x" * 300)
        )

    def test_general_educational_question_with_you_is_not_overfiltered(self):
        self.assertTrue(
            factual_evidence.should_retrieve_factual_evidence(
                "How do you calculate the speed of light?"
            )
        )


class EvidenceFormattingTests(unittest.TestCase):
    def test_prompt_formats_excerpt_as_untrusted_candidate_evidence(self):
        prompt = factual_evidence.format_evidence_for_prompt({
            "title": "Canberra",
            "excerpt": "Canberra is the capital of Australia.",
            "url": "https://en.wikipedia.org/?curid=123",
        })
        self.assertIn("CANDIDATE EXTERNAL EVIDENCE", prompt)
        self.assertIn("Canberra is the capital of Australia.", prompt)
        self.assertIn("untrusted quoted data", prompt)
        self.assertIn("answer using only claims directly supported", prompt)
        self.assertIn("Every material claim must be supported", prompt)
        self.assertIn("https://en.wikipedia.org/?curid=123", prompt)

    def test_html_in_excerpt_is_cleaned(self):
        prompt = factual_evidence.format_evidence_for_prompt({
            "title": "Water",
            "excerpt": "Pure water <b>boils</b> at 100&#160;°C.",
            "url": "https://en.wikipedia.org/?curid=456",
        })
        self.assertIn("Pure water boils at 100 °C.", prompt)
        self.assertNotIn("<b>", prompt)
        self.assertIn("Article extract:", prompt)

    def test_source_footer_rejects_non_wikipedia_urls(self):
        footer = factual_evidence.format_source_attribution({
            "title": "Example",
            "url": "https://example.com/forged",
        })
        self.assertEqual(footer, "")

    def test_source_footer_is_plain_text_and_grounded_in_result(self):
        footer = factual_evidence.format_source_attribution({
            "title": "Canberra",
            "url": "https://en.wikipedia.org/?curid=123",
        })
        self.assertIn("Potential reference", footer)
        self.assertIn("not independently verified", footer)
        self.assertIn("Wikipedia — Canberra", footer)
        self.assertIn("https://en.wikipedia.org/?curid=123", footer)

    def test_answer_support_uses_answer_specific_terms(self):
        evidence = {
            "title": "Canberra",
            "excerpt": "Canberra is the capital city of Australia.",
        }
        result = factual_evidence.assess_answer_support(
            "What is the capital of Australia?",
            "Canberra is the capital of Australia.",
            evidence,
        )
        self.assertEqual(result["status"], "candidate_supported")
        self.assertIn("canberra", result["matched_terms"])

    def test_wrong_city_does_not_look_supported_by_shared_question_words(self):
        evidence = {
            "title": "Canberra",
            "excerpt": "Canberra is the capital city of Australia.",
        }
        result = factual_evidence.assess_answer_support(
            "What is the capital of Australia?",
            "Sydney is the capital of Australia.",
            evidence,
        )
        self.assertEqual(result["status"], "insufficient_overlap")
        self.assertNotIn("sydney", result["matched_terms"])

    def test_unrelated_claim_does_not_pass_due_to_shared_topic_words(self):
        evidence = {
            "title": "Canberra",
            "excerpt": "Canberra is the capital city of Australia.",
        }
        result = factual_evidence.assess_answer_support(
            "What is the capital of Australia?",
            "Mars has two moons named Phobos and Deimos.",
            evidence,
        )
        self.assertEqual(result["status"], "insufficient_overlap")

    def test_paraphrase_with_supported_answer_entity_can_pass(self):
        evidence = {
            "title": "Canberra",
            "excerpt": "Canberra is the capital city of Australia.",
        }
        result = factual_evidence.assess_answer_support(
            "What is the capital of Australia?",
            "Australia's capital city is Canberra.",
            evidence,
        )
        self.assertEqual(result["status"], "candidate_supported")

    def test_answer_with_no_question_independent_terms_is_not_marked_supported(self):
        evidence = {
            "title": "Canberra",
            "excerpt": "Canberra is the capital city of Australia.",
        }
        result = factual_evidence.assess_answer_support(
            "What is the capital of Australia?",
            "The capital of Australia.",
            evidence,
        )
        self.assertEqual(result["status"], "insufficient_overlap")
        self.assertEqual(result["reason"], "no_answer_specific_terms")

    def test_contradictory_negation_is_not_marked_supported(self):
        evidence = {
            "title": "Canberra",
            "excerpt": "Canberra is the capital city of Australia.",
        }
        result = factual_evidence.assess_answer_support(
            "Is Canberra the capital of Australia?",
            "Canberra is not the capital of Australia.",
            evidence,
        )
        self.assertEqual(result["status"], "insufficient_overlap")
        self.assertEqual(result["reason"], "polarity_mismatch")

    def test_unsupported_named_entity_is_flagged(self):
        evidence = {
            "title": "General relativity",
            "excerpt": "Albert Einstein developed the theory of general relativity.",
        }
        result = factual_evidence.assess_answer_support(
            "Who developed the theory of general relativity?",
            "Albert Einstein and Isaac Newton jointly developed general relativity.",
            evidence,
        )
        self.assertEqual(result["status"], "insufficient_overlap")
        self.assertIn("isaac", result["unsupported_named_entities"])
        self.assertIn("newton", result["unsupported_named_entities"])

    def test_unsupported_numeric_detail_is_flagged_even_when_topic_words_match(self):
        evidence = {
            "title": "Canberra",
            "excerpt": "Canberra is the capital city of Australia.",
        }
        result = factual_evidence.assess_answer_support(
            "What is the capital of Australia?",
            "Canberra is Australia's capital and has a population of 900 million.",
            evidence,
        )
        self.assertEqual(result["status"], "insufficient_overlap")
        self.assertIn("900", result["unsupported_numbers"])

    def test_numbers_present_in_evidence_are_not_flagged(self):
        evidence = {
            "title": "Water",
            "excerpt": "Water boils at 100 °C at standard atmospheric pressure.",
        }
        result = factual_evidence.assess_answer_support(
            "At what temperature does water boil?",
            "Water boils at 100 °C.",
            evidence,
        )
        self.assertEqual(result["status"], "candidate_supported")

    def test_empty_answer_is_unavailable(self):
        result = factual_evidence.assess_answer_support(
            "What is the capital of Australia?", "", {
                "title": "Canberra",
                "excerpt": "Canberra is the capital of Australia.",
            }
        )
        self.assertEqual(result["status"], "unavailable")

    def test_warning_does_not_claim_answer_is_false(self):
        warning = factual_evidence.format_support_warning()
        self.assertIn("unverified", warning)
        self.assertNotIn("incorrect", warning)


class RetrievalFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_retrieval_uses_article_extracts_and_selects_top_ranked_page(self):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "query": {
                        "pages": {
                            "456": {
                                "pageid": 456,
                                "title": "Sydney",
                                "index": 2,
                                "extract": "Sydney is a city in Australia.",
                            },
                            "123": {
                                "pageid": 123,
                                "title": "Canberra",
                                "index": 1,
                                "extract": "Canberra is the capital of Australia.",
                            },
                        }
                    }
                }

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.request = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, params):
                self.request = (url, params)
                self.assertion_params = params
                return FakeResponse()

        client = FakeClient()
        with patch.object(factual_evidence.httpx, "AsyncClient", return_value=client):
            result = await factual_evidence.retrieve_wikipedia_evidence(
                "What is the capital of Australia?"
            )

        self.assertEqual(result["title"], "Canberra")
        self.assertEqual(result["excerpt"], "Canberra is the capital of Australia.")
        self.assertEqual(result["url"], "https://en.wikipedia.org/?curid=123")
        self.assertEqual(client.assertion_params["prop"], "extracts")
        self.assertEqual(client.assertion_params["explaintext"], 1)

    async def test_network_failure_falls_back_to_normal_chat(self):
        class BrokenClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                import httpx
                raise httpx.ConnectError("offline")

        with patch.object(factual_evidence.httpx, "AsyncClient", BrokenClient):
            result = await factual_evidence.retrieve_wikipedia_evidence(
                "What is the capital of Australia?"
            )
        self.assertIsNone(result)

    async def test_personal_question_does_not_make_network_request(self):
        with patch.object(factual_evidence.httpx, "AsyncClient") as client:
            result = await factual_evidence.retrieve_wikipedia_evidence(
                "What is my favorite game?"
            )
        self.assertIsNone(result)
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
