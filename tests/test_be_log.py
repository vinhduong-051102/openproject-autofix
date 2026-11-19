import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deploy_qa as d  # noqa: E402


class BeLogTest(unittest.TestCase):
    def test_masks_bot_token_and_password(self):
        line = "POST https://api.telegram.org/bot1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/sendMediaGroup password=abc"
        with mock.patch.object(d, "remote", return_value=line):
            out = d.be_log_search({"host": "h", "be_container": "soar"}, "Telegram")
        self.assertNotIn("AAAAAAAA", out)
        self.assertNotIn("abc", out)
        self.assertIn("sendMediaGroup", out)

    def test_command_quotes_pattern_and_caps_minutes(self):
        with mock.patch.object(d, "remote", return_value="") as r:
            d.be_log_search({"host": "h"}, "a'; rm -rf /", '{"since_minutes": 9999}')
        cmd = r.call_args[0][1]
        self.assertIn("--since 180m", cmd)
        self.assertIn("'a'\"'\"'; rm -rf /'", cmd)

    def test_short_pattern_rejected(self):
        self.assertIn("≥ 3", d.be_log_search({"host": "h"}, "ab"))


if __name__ == "__main__":
    unittest.main()
