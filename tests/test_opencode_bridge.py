from __future__ import annotations

import unittest

from wizpr_suite.core.opencode_bridge import _parse_model_lines, sort_opencode_models


class OpenCodeBridgeTests(unittest.TestCase):
    def test_parse_model_lines_accepts_plain_and_table_output(self) -> None:
        raw = """
        provider/model
        | ollama/qwen36-27b |
        opencode/north-mini-code-free
        https://example.com/not-a-model
        """

        models = _parse_model_lines(raw)

        self.assertIn("ollama/qwen36-27b", models)
        self.assertIn("opencode/north-mini-code-free", models)
        self.assertNotIn("https://example.com/not-a-model", models)
        self.assertNotIn("provider/model", models)

    def test_sort_opencode_models_prefers_local_qwen_coding_models(self) -> None:
        models = sort_opencode_models(
            [
                "opencode/north-mini-code-free",
                "ollama-cloud/qwen3.5:397b",
                "ollama/qwen36-27b",
                "ollama/llama3.1:8b",
            ]
        )

        self.assertEqual("ollama/qwen36-27b", models[0])


if __name__ == "__main__":
    unittest.main()
