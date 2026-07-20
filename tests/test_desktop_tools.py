from __future__ import annotations

import unittest

from wizpr_suite.core.desktop_tools import parse_desktop_tool_request


class DesktopToolTests(unittest.TestCase):
    def test_safe_desktop_apps_are_recognized(self) -> None:
        request = parse_desktop_tool_request("please open Notepad")
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual("notepad", request.tool)
        self.assertEqual("Notepad", request.label)

    def test_non_tool_question_is_not_intercepted(self) -> None:
        self.assertIsNone(parse_desktop_tool_request("what is Notepad used for"))
        self.assertIsNone(parse_desktop_tool_request("write a note about calculators"))

    def test_arbitrary_shell_command_is_not_recognized(self) -> None:
        self.assertIsNone(parse_desktop_tool_request("run rm -rf everything"))
        self.assertIsNone(parse_desktop_tool_request("execute my custom script"))


if __name__ == "__main__":
    unittest.main()
