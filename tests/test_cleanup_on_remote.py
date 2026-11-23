"""cleanup.on_remote: commit lên remote dưới hash khác (cherry-pick / rebase, hoặc cùng tiêu đề) được coi là đã push."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cleanup as C  # noqa: E402


def git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()


class OnRemoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = self.tmp.name
        git(self.r, "init", "-q", "-b", "main")
        open(os.path.join(self.r, "a"), "w").write("1\n")
        git(self.r, "add", "a")
        git(self.r, "commit", "-q", "-m", "init")
        git(self.r, "branch", "up")

    def tearDown(self):
        self.tmp.cleanup()

    def local_commit(self, msg, content):
        open(os.path.join(self.r, "a"), "w").write(content)
        git(self.r, "commit", "-qam", msg)
        return git(self.r, "rev-parse", "HEAD")

    def publish(self, commit, msg=None):
        git(self.r, "checkout", "-q", "up")
        if msg:  # cùng tiêu đề, nội dung khác (rebase có sửa xung đột)
            open(os.path.join(self.r, "a"), "w").write("khác\n")
            git(self.r, "commit", "-qam", msg)
        else:
            git(self.r, "cherry-pick", commit)
        git(self.r, "update-ref", "refs/remotes/origin/dev-ss", "HEAD")
        git(self.r, "checkout", "-q", "main")

    def test_cherry_picked(self):
        c = self.local_commit("[SOAR-1] fix: a", "2\n")
        self.assertFalse(C.on_remote(self.r, c))
        self.publish(c)
        self.assertTrue(C.on_remote(self.r, c))

    def test_same_subject(self):
        c = self.local_commit("[SOAR-2] fix: b", "3\n")
        self.publish(c, msg="[SOAR-2] fix: b")
        self.assertTrue(C.on_remote(self.r, c))

    def test_really_unpushed(self):
        c = self.local_commit("[SOAR-3] fix: c", "4\n")
        self.publish(self.local_commit("khác hẳn", "5\n"), msg="[SOAR-9] fix: z")
        self.assertFalse(C.on_remote(self.r, c))


if __name__ == "__main__":
    unittest.main()
