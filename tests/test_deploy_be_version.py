"""Version BE dạng MAJOR.MINOR.PATCH: gốc = số lớn nhất đã có (mặc định 2.1.4), feat → MINOR, còn lại → PATCH."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deploy_qa as D  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()


class BeVersionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        git(self.repo, "init", "-q")
        self.base = self.commit("init")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, msg):
        git(self.repo, "commit", "-q", "--allow-empty", "-m", msg)
        return git(self.repo, "rev-parse", "HEAD")

    def test_regex_and_helpers(self):
        for m in ("feat: x", "feat(ai-chat): x", "[SOAR-1] feat: x", "[SOAR-1] - feat(x): y", "[NO-TICKET] feat: x"):
            self.assertTrue(D.FEAT_RE.match(m), m)
        for m in ("fix: x", "[SOAR-1] - Quản lý file - feature mới", "refactor(feat): x"):
            self.assertFalse(D.FEAT_RE.match(m), m)
        self.assertEqual(D.bump_version((2, 1, 4), False), (2, 1, 5))
        self.assertEqual(D.bump_version((2, 1, 4), True), (2, 2, 0))
        known = D.be_known_versions(["soar-be-ss-lastest-k8s:20261123b", "soar-be-ss-k8s:2.1.4", "soar-fe-ss:2.8.1",
                                     "git2.cytech.ai/x/soar-be-ss:2.0.9"], {})
        self.assertEqual(sorted(known), [(2, 0, 9), (2, 1, 4)])

    def test_floor_patch_and_minor(self):
        dcfg = {"be_version_floor": "2.1.4"}
        self.commit("[SOAR-1] - Lỗi: sửa")
        self.assertEqual(D.next_be_version(self.repo, dcfg, ["soar-be-ss-lastest-k8s:20261123b"], {}, self.base), "2.1.5")
        self.commit("feat(ai-chat): thêm")
        self.assertEqual(D.next_be_version(self.repo, dcfg, [], {}, self.base), "2.2.0")

    def test_continues_from_last_tool_build(self):
        head_before = self.commit("feat: cũ, đã nằm trong 2.2.0")
        images = {"soar-be-ss-lastest-k8s:2.2.0": head_before}
        self.commit("fix: mới")
        # feat cũ đã có trong bản 2.2.0 → chỉ tăng PATCH
        self.assertEqual(D.next_be_version(self.repo, {}, ["soar-be-ss-k8s:2.1.4"], images, self.base), "2.2.1")

    def test_skips_taken_number(self):
        self.commit("fix: x")
        refs = ["soar-be-ss-k8s:2.1.4", "soar-be-ss-lastest-k8s:2.1.4"]
        self.assertEqual(D.next_be_version(self.repo, {}, refs, {}, self.base), "2.1.5")


if __name__ == "__main__":
    unittest.main()
