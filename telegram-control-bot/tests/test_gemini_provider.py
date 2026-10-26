"""Unit tests for the Gemini OpenAI-compatible API client."""
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from gemini_provider import GeminiProvider, GeminiProviderError, model_display_name


class GeminiProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_base_url_preserves_openai_api_path(self):
        provider = GeminiProvider("test-key")
        self.assertEqual(provider.base_url, "https://generativelanguage.googleapis.com/v1beta/openai/")

    async def test_requires_api_key(self):
        with self.assertRaises(GeminiProviderError):
            GeminiProvider("")

    async def test_lists_chat_models_and_filters_embeddings(self):
        provider = GeminiProvider("test-key")
        response = httpx.Response(200, json={"data": [
            {"id": "gemini-2.5-flash"},
            {"id": "text-embedding-004"},
            {"id": "gemini-2.5-pro"},
        ]})
        with patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=response)) as get:
            models = await provider.list_models()
        self.assertEqual(get.await_args.args[0], "models")
        self.assertEqual(models, ["gemini-2.5-flash", "gemini-2.5-pro"])

    async def test_chat_uses_openai_compatible_endpoint(self):
        provider = GeminiProvider("test-key")
        response = httpx.Response(200, json={"choices": [{"message": {"content": "Hello"}}]})
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=response)) as post:
            result = await provider.chat(model="gemini-test", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(result["choices"][0]["message"]["content"], "Hello")
        self.assertEqual(post.await_args.args[0], "chat/completions")
        self.assertEqual(post.await_args.kwargs["json"]["model"], "gemini-test")

    async def test_unauthorized_error_does_not_expose_key(self):
        provider = GeminiProvider("sensitive-test-key")
        response = httpx.Response(401, json={"error": {"message": "invalid key"}})
        with patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=response)):
            with self.assertRaisesRegex(GeminiProviderError, "API key"):
                await provider.list_models()

    def test_display_name_removes_models_prefix(self):
        self.assertEqual(model_display_name("models/gemini-2.5-flash"), "gemini-2.5-flash")


if __name__ == "__main__":
    unittest.main()
