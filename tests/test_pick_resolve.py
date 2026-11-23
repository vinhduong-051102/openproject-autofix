"""pick_resolve: độ phủ dòng (thiếu / kéo thừa) + vòng gỡ xung đột cherry-pick (Claude được thay bằng hàm giả)."""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pick_resolve as PR  # noqa: E402


def git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True).stdout.strip()


def write(repo, name, text):
    open(os.path.join(repo, name), "w").write(text)


class PickTest(unittest.TestCase):
    """Nhánh nguồn `src` có tính năng X (dòng onConnected) rồi commit C thêm connectListeners; nhánh đích `dst` cũ, chưa có X."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = self.r = self.tmp.name
        git(r, "init", "-q")
        git(r, "checkout", "-q", "-b", "dst")
        write(r, "ws.ts", "function connect() {\n  subscribe();\n  resubscribe();\n}\n")
        git(r, "add", ".")
        git(r, "commit", "-qm", "base")
        self.base = git(r, "rev-parse", "HEAD")
        git(r, "checkout", "-q", "-b", "src")
        write(r, "ws.ts", "function connect(onConnected) {\n  subscribe();\n  resubscribe();\n  onConnected?.();\n}\n")
        git(r, "commit", "-qam", "feature X")
        write(r, "ws.ts", "const listeners = new Set();\nfunction connect(onConnected) {\n  subscribe();\n"
                          "  resubscribe();\n  onConnected?.();\n  listeners.forEach((l) => l());\n}\n")
        git(r, "commit", "-qam", "[SOAR-1] feat: listeners")
        self.c = git(r, "rev-parse", "HEAD")
        git(r, "checkout", "-q", "dst")
        # nhánh đích đổi cùng chỗ → cherry-pick xung đột
        write(r, "ws.ts", "function connect() {\n  subscribe();\n  resubscribe();\n  log('ok');\n}\n")
        git(r, "commit", "-qam", "dst change")
        self.ref = git(r, "rev-parse", "HEAD")

    def tearDown(self):
        self.tmp.cleanup()

    def fake_claude(self, content, verdict="KẾT QUẢ: ĐÃ GỠ"):
        def run(acfg, prompt, wt, report_dir, edit, tag, log):
            if edit:
                write(wt, "ws.ts", content)
            report = os.path.join(report_dir, tag + ".md")
            open(report, "w").write(verdict + "\n")
            return verdict, report
        return run

    def pick(self):
        subprocess.run(["git", "cherry-pick", "-x", self.c], cwd=self.r, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(PR.unmerged(self.r), ["ws.ts"])
        self.assertTrue(PR.in_progress(self.r))

    def test_resolve_then_coverage_flags_pulled_in_line(self):
        self.pick()
        bad = "const listeners = new Set();\nfunction connect() {\n  subscribe();\n  resubscribe();\n  log('ok');\n" \
              "  onConnected?.();\n  listeners.forEach((l) => l());\n}\n"
        with mock.patch.object(PR, "_claude", self.fake_claude(bad)), \
                mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}):
            ok, note, files = PR.resolve_all({}, self.r, "dst", "SOAR-1", os.path.join(self.r, ".rep"), lambda m: None)
        self.assertTrue(ok, note)
        self.assertFalse(PR.in_progress(self.r))
        missing, extra = PR.coverage(self.r, self.ref, [self.c])
        self.assertEqual(missing, [])
        self.assertIn(("ws.ts", "onConnected?.();"), extra)  # dòng của tính năng X bị kéo sang

    def test_coverage_flags_missing_line(self):
        self.pick()
        lost = "function connect() {\n  subscribe();\n  resubscribe();\n  log('ok');\n}\n"
        with mock.patch.object(PR, "_claude", self.fake_claude(lost)), \
                mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}):
            ok, _, _ = PR.resolve_all({}, self.r, "dst", "SOAR-1", os.path.join(self.r, ".rep"), lambda m: None)
        missing, _ = PR.coverage(self.r, self.ref, [self.c])
        self.assertIn(("ws.ts", "const listeners = new Set();"), missing)
        self.assertIn(("ws.ts", "listeners.forEach((l) => l());"), missing)

    def test_claude_gives_up_keeps_conflict(self):
        self.pick()
        with mock.patch.object(PR, "_claude", self.fake_claude(open(os.path.join(self.r, "ws.ts")).read(),
                                                               "KẾT QUẢ: KHÔNG GỠ ĐƯỢC")):
            ok, note, _ = PR.resolve_all({}, self.r, "dst", "SOAR-1", os.path.join(self.r, ".rep"), lambda m: None)
        self.assertFalse(ok)
        self.assertIn("không gỡ được", note)
        self.assertTrue(PR.in_progress(self.r))

    def test_disabled(self):
        ok, note, _ = PR.resolve_all({"claude_resolve_conflicts": False}, self.r, "dst", "SOAR-1", self.r, lambda m: None)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
