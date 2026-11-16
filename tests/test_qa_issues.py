"""
Nhật ký sự cố QA (qa_issues.py): ghi / phân loại / tổng hợp, và các chỗ deploy_qa gọi vào.
Chạy: python3 -m unittest discover -s tests   (trong thư mục op-watch; không gọi server / OpenProject)
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import qa_issues as q  # noqa: E402
import deploy_qa as d  # noqa: E402


class QaIssuesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.old = q.PATH
        q.PATH = os.path.join(self.dir, "qa-issues.jsonl")
        self.addCleanup(setattr, q, "PATH", self.old)

    def test_record_classify_summary(self):
        q.record("deploy_fe", "FE build/deploy lỗi: npm run build TS2322\nchi tiết", path="/x.log")
        q.record("gemini_timeout", "Gemini lỗi: không trả lời sau 285s", ticket="SOAR-1")
        q.record("khong-co", "x")
        items = q.load()
        self.assertEqual([i["kind"] for i in items], ["deploy_fe", "gemini_timeout", "other"])
        self.assertEqual(items[0]["phase"], "server")
        self.assertEqual(items[0]["cause"], "fe_build")
        self.assertEqual(items[0]["msg"], "FE build/deploy lỗi: npm run build TS2322")  # chỉ dòng đầu
        self.assertEqual(items[1]["cause"], "timeout")
        md = q.summary_md(14)
        self.assertIn("## QA server", md)
        self.assertIn("## QA test", md)
        self.assertIn("Gợi ý cải thiện", md)

    def test_record_never_raises(self):
        q.PATH = os.path.join(self.dir, "khong", "\0hong")  # đường dẫn hỏng
        q.record("deploy_fe", "x")  # không ném lỗi

    def test_part_error_records(self):
        d._part_error("rollout FE", RuntimeError("nginx upstream lỗi"), None)
        d._part_error("BE build/deploy", RuntimeError("mvn BUILD FAILURE"), None)
        kinds = [(i["kind"], i["cause"]) for i in q.load()]
        self.assertEqual(kinds, [("rollout", "nginx"), ("deploy_be", "be_build")])


if __name__ == "__main__":
    unittest.main()
