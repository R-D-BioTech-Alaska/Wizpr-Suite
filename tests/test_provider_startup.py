from __future__ import annotations

import subprocess
import sys
import unittest


class ProviderStartupTests(unittest.TestCase):
    def test_openai_provider_does_not_import_sdk_until_used(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import wizpr_suite.llm.providers.openai_provider; print('openai' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertEqual("False", result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
