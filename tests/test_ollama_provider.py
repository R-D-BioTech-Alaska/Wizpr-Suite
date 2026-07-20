from __future__ import annotations

import unittest

from wizpr_suite.llm.providers.ollama_provider import sort_ollama_models


class OllamaProviderTests(unittest.TestCase):
    def test_qwen_coding_models_sort_first(self) -> None:
        models = sort_ollama_models(
            [
                "llama3.1:8b",
                "qwen36-27b",
                "mistral:7b",
                "qwen2.5-coder:14b",
            ]
        )

        self.assertEqual("qwen2.5-coder:14b", models[0])
        self.assertIn("qwen36-27b", models[:2])


if __name__ == "__main__":
    unittest.main()
