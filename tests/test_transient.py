import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deploy_qa as d  # noqa: E402


class TransientTest(unittest.TestCase):
    def test_200_body_with_error_words_is_not_transient(self):
        # SOAR-3004: /api/v1/healthchecks/alerts trả 200, thân liệt kê lỗi SMTP "Connection refused / timed out"
        out = 'HTTP 200\nContent-Type: application/json\n\n{"data":[{"message":"smtp: Connection refused / timed out"}]}'
        self.assertFalse(d.is_transient(out))

    def test_gateway_errors_are_transient(self):
        self.assertTrue(d.is_transient("HTTP 502\n\nBad Gateway"))
        self.assertTrue(d.is_transient("HTTP 503\n"))

    def test_500_is_not_transient(self):
        self.assertFalse(d.is_transient('HTTP 500\n\n{"status":500}'))

    def test_no_status_line_connection_errors(self):
        self.assertTrue(d.is_transient("curl: (7) Failed to connect: Connection refused"))
        self.assertTrue(d.is_transient("HẾT THỜI GIAN (120s) timed out"))
        self.assertFalse(d.is_transient("some other text"))


if __name__ == "__main__":
    unittest.main()
