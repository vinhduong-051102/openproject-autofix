import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deploy_qa as d  # noqa: E402

DIFF = """# BE 84727fcc
diff --git a/SOAR/src/main/java/com/cyai/soar/dto/request/CaseRequest.java b/SOAR/src/main/java/com/cyai/soar/dto/request/CaseRequest.java
+  @NotEmpty(message = "CASE_IDS_REQUIRED")
+  private String x = "short_x";
diff --git a/SOAR/src/main/resources/i18n/messages_vi.properties b/SOAR/src/main/resources/i18n/messages_vi.properties
+case.ids.required=Vui l\\u00f2ng ch\\u1ecdn \\u00edt nh\\u1ea5t m\\u1ed9t s\\u1ef1 vi\\u1ec7c.
+short=abc
diff --git a/SOAR/src/test/java/com/cyai/soar/XTest.java b/SOAR/src/test/java/com/cyai/soar/XTest.java
+  assert "TEST_ONLY_CODE_X";
"""


class BeEvidenceTest(unittest.TestCase):
    def test_added_literals(self):
        lits = d.be_added_literals(DIFF)
        self.assertIn("CASE_IDS_REQUIRED", lits)
        self.assertIn("Vui lòng chọn ít nhất một sự việc.", lits)
        self.assertNotIn("TEST_ONLY_CODE_X", lits)  # code test không tính
        self.assertNotIn("abc", lits)

    def test_exception_suffix_stripped(self):
        diff = ("# BE abcdef1\ndiff --git a/SOAR/src/main/java/com/cyai/soar/exception/authenticate/"
                "ForgotPasswordOtpInvalidException.java b/x\n")
        _, tokens = d.be_tokens(diff)
        self.assertIn("forgot-password-otp-invalid", tokens)

    def test_related_by_hyphen_words(self):
        self.assertTrue(d._related("/api/v1/reports/export-user-implement", {"user"}))  # SOAR-940
        self.assertTrue(d._related("/api/v2/auth/forgot-password", {"forgot-password-otp-invalid"}))  # SOAR-965
        self.assertFalse(d._related("/api/v1/users/me", {"tenancy", "common-history"}))
        self.assertFalse(d._related("/api/v2/auth/reset-password", {"forgot-password-otp-invalid"}))

    def _qa(self, status, body):
        return {"method": "PUT", "target": "/api/v1/cases/swap-solution-batch", "account": "admint",
                "status": status, "out": body}

    def test_verify_pass_accepts_4xx_with_new_code(self):
        import tempfile
        qd = tempfile.mkdtemp()
        with open(os.path.join(qd, "diff.md"), "w", encoding="utf-8") as f:
            f.write(DIFF)
        text = ("QA: ĐẠT\n\n## Kết quả\nok\n\n## Toàn bộ request\n"
                "## [admint] PUT /api/v1/cases/swap-solution-batch\n```\nHTTP 400\n{\"data\":{\"code\":\"CASE_IDS_REQUIRED\"}}\n```\n")
        reqs = d.qa_requests_from_md(text)
        self.assertEqual([(r["method"], r["status"]) for r in reqs], [("PUT", 400)])
        _, reasons = d.verify_pass(text, qd, {"qa_be_log_check": False})
        self.assertEqual(reasons, [])
        text2 = text.replace("CASE_IDS_REQUIRED\"}}", "Case IDs cannot be empty\"}}")
        _, reasons2 = d.verify_pass(text2, qd, {"qa_be_log_check": False})
        self.assertIn("BE chưa có bằng chứng", reasons2)  # 4xx thông báo CŨ → chưa chứng minh bản sửa


if __name__ == "__main__":
    unittest.main()
