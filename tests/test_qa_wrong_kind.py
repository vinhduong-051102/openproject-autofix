import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import op_autofix as af  # noqa: E402
import deploy_qa as dq  # noqa: E402


class GeminiPromptArgTest(unittest.TestCase):
    def test_short_prompt_inline(self):
        d = tempfile.mkdtemp()
        self.assertEqual(af.gemini_prompt_arg("ngắn", [d]), "ngắn")

    def test_long_prompt_goes_to_file(self):
        d = tempfile.mkdtemp()
        big = "Tiếng Việt có dấu " * 20000  # > 131072 byte UTF-8
        arg = af.gemini_prompt_arg(big, [d])
        self.assertLess(len(arg.encode("utf-8")), 2000)
        files = [f for f in os.listdir(d) if f.startswith(".gemini-prompt-")]
        self.assertEqual(len(files), 1)
        self.assertIn(os.path.join(d, files[0]), arg)
        with open(os.path.join(d, files[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), big)

    def test_cmd_arg_under_kernel_limit(self):
        d = tempfile.mkdtemp()
        cmd = af.gemini_cmd({"gemini_cmd": "agy"}, "x" * 300000, [d], commands=False)
        self.assertTrue(all(len(c.encode("utf-8")) < 131072 for c in cmd))


class QaWrongKindTest(unittest.TestCase):
    def test_kind_parse(self):
        self.assertEqual(af.qa_wrong_kind("KẾT QUẢ: QA SAI\nLOẠI QA SAI: CHẤM SAI\n..."), "CHẤM SAI")
        self.assertEqual(af.qa_wrong_kind("KẾT QUẢ: QA SAI\nLOẠI QA SAI: TEST SAI"), "TEST SAI")
        self.assertEqual(af.qa_wrong_kind("KẾT QUẢ: QA SAI\nkhông ghi loại"), "TEST SAI")

    def _env(self, engine_first_line):
        root = tempfile.mkdtemp()
        qd = os.path.join(root, "SOAR-1-qa-1118-1700")
        os.makedirs(qd)
        with open(os.path.join(qd, "qa.md"), "w", encoding="utf-8") as f:
            f.write("QA: KHÔNG TỰ TEST ĐƯỢC\n")
        with open(os.path.join(qd, "qa-engine.md"), "w", encoding="utf-8") as f:
            f.write(engine_first_line + "\n\n## Kết quả\nĐã đối chiếu: 400 CASE_IDS_REQUIRED.\n")
        api = mock.Mock()
        api.get.return_value = {"lockVersion": 3, "_links": {"status": {"href": "/api/v3/statuses/95"}}}
        return root, os.path.join(qd, "qa.md"), api

    def _run(self, report, engine_first_line):
        root, qa_path, api = self._env(engine_first_line)
        writes = []
        with mock.patch.object(af, "op_write", side_effect=lambda *a, **k: writes.append(a)), \
                mock.patch.object(af, "save_qa_rules", return_value=1), \
                mock.patch.object(af.envconf, "deploy_config", return_value={"qa_user_id": 59, "qa_user_name": "anh Tám",
                                                                            "qa_review_status_ids": [98]}), \
                mock.patch.object(af.envconf, "autofix_root", return_value=root), \
                mock.patch.object(dq, "fail_count") as fc, \
                mock.patch.object(dq, "PENDING", os.path.join(root, "qa-pending.json")):
            msg = af.report_qa_wrong(api, 7, "SOAR-1", report, "run", qa_report=qa_path)
        return msg, writes, fc, root

    def test_cham_sai_hands_to_tester(self):
        msg, writes, fc, _ = self._run("KẾT QUẢ: QA SAI\nLOẠI QA SAI: CHẤM SAI\n", "QA: ĐẠT")
        self.assertIn("đã chuyển anh Tám", msg)
        patch = [w for w in writes if w[1] == "PATCH"][0]
        self.assertEqual(patch[3]["_links"]["assignee"]["href"], "/api/v3/users/59")
        self.assertNotIn("status", patch[3]["_links"])  # Resolved giữ nguyên
        fc.assert_called_with("SOAR-1", 0)

    def test_cham_sai_but_engine_not_pass_requeues(self):
        msg, writes, fc, root = self._run("KẾT QUẢ: QA SAI\nLOẠI QA SAI: CHẤM SAI\n", "QA: KHÔNG ĐẠT")
        self.assertFalse([w for w in writes if w[1] == "PATCH"])
        with open(os.path.join(root, "qa-pending.json"), encoding="utf-8") as f:
            self.assertIn("SOAR-1", json.load(f)["tickets"])
        self.assertIn("~10 phút", msg)

    def test_test_sai_requeues(self):
        msg, writes, fc, root = self._run("KẾT QUẢ: QA SAI\nLOẠI QA SAI: TEST SAI\n", "QA: ĐẠT")
        self.assertFalse([w for w in writes if w[1] == "PATCH"])
        with open(os.path.join(root, "qa-pending.json"), encoding="utf-8") as f:
            self.assertIn("SOAR-1", json.load(f)["tickets"])


if __name__ == "__main__":
    unittest.main()
