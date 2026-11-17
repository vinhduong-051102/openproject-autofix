"""CẦN TEST TAY → tự chạy bước Claude hỗ trợ (tự test trên server) trước khi comment / chuyển UNREVIEWED (SOAR-1100)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import op_autofix as af  # noqa: E402

DEV = "## Người quyết định\nNGƯỜI QUYẾT: DEV\n"
TESTER = "## Người quyết định\nNGƯỜI QUYẾT: TESTER\n"


class WantsAssistTest(unittest.TestCase):
    def test_manual_test_assisted_when_server_available(self):
        with mock.patch.object(af, "server_api_ok", return_value=True):
            self.assertTrue(af.wants_assist({}, "CẦN TEST TAY", TESTER))

    def test_manual_test_not_assisted_without_server(self):
        with mock.patch.object(af, "server_api_ok", return_value=False):
            self.assertFalse(af.wants_assist({}, "CẦN TEST TAY", TESTER))

    def test_assist_auto_off(self):
        with mock.patch.object(af, "server_api_ok", return_value=True):
            self.assertFalse(af.wants_assist({"assist_auto": False}, "CẦN TEST TAY", TESTER))

    def test_decision_rules_unchanged(self):
        with mock.patch.object(af, "review_decider", side_effect=lambda t: ("dev" if "DEV" in t else "tester", "")):
            self.assertTrue(af.wants_assist({}, "CẦN QUYẾT ĐỊNH", DEV))
            self.assertFalse(af.wants_assist({}, "CẦN QUYẾT ĐỊNH", TESTER))
        self.assertFalse(af.wants_assist({}, "ĐÃ SỬA", DEV))


if __name__ == "__main__":
    unittest.main()
