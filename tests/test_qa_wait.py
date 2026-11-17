"""QA Gemini chờ theo thời gian (wait_minutes) cho ticket phụ thuộc mốc giờ (SOAR-1495: cảnh báo SLA trước/sau hạn)."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deploy_qa as D  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class QaWaitTest(unittest.TestCase):
    def test_waits_full_duration_and_checks_server(self):
        clk, checks = FakeClock(), []
        D.qa_wait(10, guard=lambda: checks.append(clk.t) or None, sleep=clk.sleep, clock=clk.time)
        self.assertAlmostEqual(clk.t - 1000.0, 600, delta=1)
        self.assertGreaterEqual(len(checks), 4)  # mỗi 120s

    def test_server_change_aborts(self):
        clk = FakeClock()
        with self.assertRaises(D.ServerChanged):
            D.qa_wait(10, guard=lambda: "BE đổi image" if clk.t > 1300 else None, sleep=clk.sleep, clock=clk.time)
        self.assertLess(clk.t - 1000.0, 600)


class GeminiLoopWaitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        q = self.qa_dir = self.tmp.name
        os.makedirs(os.path.join(q, "ticket"))
        for rel, text in (("ticket/ticket.md", "# SOAR-1495 cảnh báo SLA"), ("diff.md", "# BE\n+ sla")):
            with open(os.path.join(q, rel), "w", encoding="utf-8") as f:
                f.write(text)
        self.dcfg = dict(D.DEFAULTS, api_url="https://api", fe_url="https://fe", qa_delete_allow=[])
        self.prompts, self.waits, self.calls = [], [], []

    def tearDown(self):
        self.tmp.cleanup()

    def run_loop(self, answers):
        answers = list(answers)

        def fake_run(cmd, **kw):
            self.prompts.append(cmd)
            return mock.Mock(stdout=json.dumps({"structured_output": answers.pop(0)}) + "\n", stderr="", returncode=0)

        with mock.patch.object(D.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(D.af, "gemini_cmd", side_effect=lambda acfg, prompt, *a, **k: [prompt]), \
                mock.patch.object(D.af, "spec_excerpts", return_value=""), \
                mock.patch.object(D, "load_qa_rules", return_value=""), \
                mock.patch.object(D, "soar_call", side_effect=lambda d, c, dc=None: self.calls.append(c) or "HTTP 200\n{}"), \
                mock.patch.object(D, "qa_wait", side_effect=lambda m, g=None: self.waits.append(m)):
            return D.gemini_qa({}, self.dcfg, self.qa_dir, "SOAR-1495", "FE x BE y")

    def test_wait_without_calls_continues_and_reports(self):
        text = self.run_loop([
            {"done": False, "verdict": "CHƯA", "calls": [{"method": "POST", "target": "/api/v1/cases", "body": "{}"}]},
            {"done": False, "verdict": "CHƯA", "calls": [], "wait_minutes": 22},
            {"done": True, "verdict": "ĐẠT", "result": "có thông báo", "evidence": "x"},
        ])
        self.assertEqual(self.waits, [22])
        self.assertIn("## WAIT 22 phút", text)
        self.assertIn("GIỜ HIỆN TẠI", self.prompts[0][0])
        self.assertIn("công cụ đã chờ", self.prompts[2][0])

    def test_wait_capped_and_total_limit(self):
        self.run_loop([
            {"done": False, "verdict": "CHƯA", "calls": [], "wait_minutes": 90},   # cắt còn 30
            {"done": False, "verdict": "CHƯA", "calls": [], "wait_minutes": 30},   # còn 15 trong tổng 45
            {"done": False, "verdict": "CHƯA", "calls": [], "wait_minutes": 10},   # hết quota → từ chối
            {"done": True, "verdict": "KHÔNG TỰ TEST ĐƯỢC", "result": "-"},
        ])
        self.assertEqual(self.waits, [30, 15])
        self.assertIn("BỊ TỪ CHỐI", self.prompts[3][0])

    def test_wait_rounds_not_counted(self):
        self.dcfg["qa_rounds"] = 2
        answers = [{"done": False, "verdict": "CHƯA", "calls": [], "wait_minutes": 5},
                   {"done": False, "verdict": "CHƯA", "calls": [{"method": "GET", "target": "/api/x"}]},
                   {"done": True, "verdict": "ĐẠT", "result": "ok"}]
        self.run_loop(answers)
        self.assertEqual(len(self.prompts), 3)  # 2 vòng + 1 vòng chờ cộng thêm


if __name__ == "__main__":
    unittest.main()
