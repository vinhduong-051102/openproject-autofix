"""Gemini (agy) chạy nền: dạng quyền gửi cho agy và luật "Gemini không ra kết luận → KHÔNG push" (sự cố SOAR-2909:
read_file(<dir>/**) làm sandbox lỗi "globs not supported", Gemini không chạy được mà công cụ vẫn push + chuyển Resolved).

Chạy: python3 -m unittest discover -s tests   (trong thư mục op-watch; không gọi agy / git / OpenProject)
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import op_autofix as af  # noqa: E402


def settings_of(cmd):
    return json.loads(cmd[cmd.index("--settings") + 1])


class GeminiCmd(unittest.TestCase):
    ACFG = {"gemini_cmd": "~/.local/bin/agy", "gemini_model": "m"}

    def test_review_no_glob_sandbox_only(self):
        d = tempfile.mkdtemp()
        try:
            cmd = af.gemini_cmd(self.ACFG, "p", [d, d + "/", "/"], timeout_s=60)
        finally:
            shutil.rmtree(d)
        s = settings_of(cmd)
        rd = os.path.realpath(d)
        self.assertEqual(s["permissions"]["allow"], ["read_file(%s)" % rd])  # không glob, không trùng, bỏ "/"
        self.assertEqual(s["permissions"]["deny"], ["write_file(%s)" % rd])  # không write_file(/) (làm hỏng sandbox)
        self.assertEqual(s["toolPermission"], "proceed-in-sandbox")
        self.assertNotIn("command(*)", s["permissions"]["allow"])  # command(*) cho cả chạy ngoài sandbox
        self.assertFalse(any("*" in r for r in s["permissions"]["allow"] + s["permissions"]["deny"]))
        self.assertIn("--sandbox", cmd)
        self.assertEqual(cmd[cmd.index("--mode") + 1], "plan")
        self.assertEqual(cmd[cmd.index("--print-timeout") + 1], "60s")
        self.assertEqual(cmd[cmd.index("--model") + 1], "m")

    def test_qa_no_commands(self):
        cmd = af.gemini_cmd(self.ACFG, "p", ["/tmp"], commands=False, extra=["--output-format", "json"], model="q")
        s = settings_of(cmd)
        self.assertIn("command(*)", s["permissions"]["deny"])
        self.assertNotIn("toolPermission", s)
        self.assertNotIn("--sandbox", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "q")
        self.assertIn("--output-format", cmd)

    def test_fail_reason(self):
        self.assertEqual(af.gemini_fail_reason("error getting token source: You are not logged into Antigravity."),
                         af.GEMINI_NO_LOGIN)
        self.assertIn("từ chối quyền", af.gemini_fail_reason(
            'jetski: no output produced — a tool required the "read_file" permission that headless mode cannot '
            'prompt for, so it was auto-denied.'))
        self.assertEqual(af.gemini_fail_reason(""), "Gemini không chạy được")
        self.assertEqual(af.gemini_failed_text("Gemini không trả kết luận: %s\n\n```" % af.GEMINI_NO_LOGIN),
                         af.GEMINI_NO_LOGIN)


class DeliverPolicy(unittest.TestCase):
    """_deliver: Gemini không ra kết luận → không push, không đổi trạng thái ticket, đánh dấu chờ; ĐẠT → push."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.run_dir = os.path.join(self.root, "SOAR-1-1111-1429")
        self.fe = os.path.join(self.run_dir, "fe")
        os.makedirs(self.fe)
        self.acfg = dict(af.AUTOFIX_DEFAULTS, root=self.root, gemini_enabled=True, review_status_id=1,
                         resolved_status_id=2, followup_status_id=None)
        repo = {"label": "FE", "branch": "dev"}
        self.patches = [
            mock.patch.object(af.R, "trees", return_value=[(repo, self.fe)]),
            mock.patch.object(af, "run", return_value=""),
            mock.patch.object(af, "fix_commits", return_value=["abc12345"]),
            mock.patch.object(af, "verify", return_value=""),
            mock.patch.object(af, "assisted", return_value=False),
            mock.patch.object(af, "worktree_snapshot", return_value={}),
            mock.patch.object(af, "merge_remote", return_value=""),
            mock.patch.object(af, "evidence_lines", return_value=[]),
            mock.patch.object(af, "notify"), mock.patch.object(af, "inbox"), mock.patch.object(af, "log"),
        ]
        for p in self.patches:
            p.start()
        self.push = mock.patch.object(af, "push").start()
        self.op_write = mock.patch.object(af, "op_write").start()
        self.status = mock.patch.object(af, "set_wp_status").start()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.root)

    def deliver(self, verdict, text):
        with mock.patch.object(af, "gemini_review", return_value=(verdict, text)):
            return af._deliver(None, {}, self.acfg, 7, "SOAR-1", {"subject": "s"}, self.run_dir, self.fe, "",
                               "KẾT QUẢ: ĐÃ SỬA\n")

    def test_gemini_failed_no_push(self):
        shipped, msg = self.deliver("", "Gemini không trả kết luận: %s\n" % af.GEMINI_NO_LOGIN)
        self.assertFalse(shipped)
        self.push.assert_not_called()
        self.op_write.assert_not_called()
        self.status.assert_not_called()
        self.assertIn(af.GEMINI_NO_LOGIN, msg)
        self.assertIn("CHƯA push, chờ xem lại", msg)
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, af.GEMINI_PENDING)))
        with mock.patch.object(af, "any_fix", return_value=True):
            self.assertEqual(af.gemini_pending_run(self.acfg, "SOAR-1"), "SOAR-1-1111-1429")
            self.assertEqual(af.gemini_pending_run(self.acfg, "SOAR-12"), "")
        with mock.patch.object(af, "any_fix", return_value=False):  # đã push tay → không còn chờ
            self.assertEqual(af.gemini_pending_run(self.acfg, "SOAR-1"), "")

    def test_gemini_pass_pushes_and_clears_hold(self):
        af.gemini_hold(self.run_dir, "SOAR-1", "Gemini không chạy được")
        shipped, msg = self.deliver("ĐẠT", "KẾT LUẬN: ĐẠT\n")
        self.assertTrue(shipped, msg)
        self.push.assert_called_once()
        self.status.assert_called_once_with(None, 7, 2)  # Resolved
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, af.GEMINI_PENDING)))

    def test_review_of_gemini_failed_no_push(self):
        with mock.patch.object(af, "gemini_review", return_value=("", "Gemini không trả kết luận: x\n")):
            shipped, msg = af._deliver(None, {}, self.acfg, 7, "SOAR-1", {"subject": "s"}, self.run_dir, self.fe,
                                       "", "", review_of={"FE": ["abc12345"]})
        self.assertFalse(shipped)
        self.push.assert_not_called()
        self.status.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, af.GEMINI_PENDING)))


if __name__ == "__main__":
    unittest.main()
