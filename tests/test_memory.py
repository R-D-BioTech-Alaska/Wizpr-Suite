from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wizpr_suite.core.memory import PersistentMemory


class PersistentMemoryTests(unittest.TestCase):
    def test_memory_persists_facts_and_turns_across_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td)
            first = PersistentMemory(app_dir)
            self.assertTrue(first.remember_fact("My greenhouse is in Alaska"))
            first.record_turn("What did I tell you?", "Your greenhouse is in Alaska.")

            second = PersistentMemory(app_dir)
            context = second.context(max_recent_turns=5, max_characters=4000)

            self.assertIn("My greenhouse is in Alaska", context)
            self.assertIn("What did I tell you?", context)
            self.assertIn("Your greenhouse is in Alaska.", context)
            self.assertEqual(1, second.stats().facts)
            self.assertEqual(1, second.stats().turns)

    def test_explicit_remember_and_forget_commands_manage_facts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            memory = PersistentMemory(Path(td))

            self.assertEqual(("remember", "my ring is black"), memory.apply_explicit_memory_command("remember that my ring is black"))
            self.assertEqual(("remember", ""), memory.apply_explicit_memory_command("Remember my ring is black"))
            self.assertEqual(("forget", "1"), memory.apply_explicit_memory_command("forget that ring is black"))
            self.assertEqual(0, memory.stats().facts)

    def test_context_respects_recent_turn_limit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            memory = PersistentMemory(Path(td))
            for index in range(5):
                memory.record_turn(f"user {index}", f"assistant {index}")

            context = memory.context(max_recent_turns=2, max_characters=4000)

            self.assertNotIn("user 2", context)
            self.assertIn("user 3", context)
            self.assertIn("assistant 4", context)


if __name__ == "__main__":
    unittest.main()
