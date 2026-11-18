import fcntl
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import runstate  # noqa: E402
import op_autofix as af  # noqa: E402


class QaStateTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.path = os.path.join(self.root, runstate.QA_RUN_LOCK_NAME)

    def _hold(self, mode):
        fd = open(self.path, "a")
        fcntl.flock(fd, mode)
        self.addCleanup(fd.close)
        return fd

    def test_free(self):
        self.assertEqual(runstate.qa_state(self.root), "free")
        self.assertFalse(runstate.qa_busy(self.root))

    def test_qa_shared_two_runs(self):
        a = self._hold(fcntl.LOCK_SH)
        self._hold(fcntl.LOCK_SH)
        a.close()  # lượt QA đầu xong, lượt thứ hai còn chạy → vẫn thấy QA (lỗi cũ của .qa-active)
        self.assertEqual(runstate.qa_state(self.root), "qa")

    def test_deploy(self):
        self._hold(fcntl.LOCK_EX)
        self.assertEqual(runstate.qa_state(self.root), "deploy")
        self.assertTrue(runstate.qa_busy(self.root))

    def test_probe_does_not_keep_lock(self):
        runstate.qa_state(self.root)
        fd = open(self.path, "a")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # không ném lỗi
        fd.close()


class MergeRunningTest(unittest.TestCase):
    def _state(self, value):
        return mock.patch("runstate.subprocess.run", return_value=mock.Mock(stdout=value + "\n"))

    def test_oneshot_activating_is_running(self):
        with self._state("activating"):
            self.assertTrue(runstate.merge_running())

    def test_inactive(self):
        with self._state("inactive"):
            self.assertFalse(runstate.merge_running())

    def test_systemctl_missing(self):
        with mock.patch("runstate.subprocess.run", side_effect=OSError):
            self.assertFalse(runstate.merge_running())


class TicketLockTest(unittest.TestCase):
    def test_remove_only_when_free(self):
        root = tempfile.mkdtemp()
        path = runstate.ticket_lock_path(root, "SOAR-1")
        fd = open(path, "a")
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertFalse(runstate.remove_ticket_lock(root, "SOAR-1"))
        self.assertTrue(os.path.exists(path))
        fd.close()
        self.assertTrue(runstate.remove_ticket_lock(root, "SOAR-1"))
        self.assertFalse(os.path.exists(path))


class StillMineTest(unittest.TestCase):
    def _api(self, assignee, fail=False):
        api = mock.Mock()
        api.me.return_value = {"id": 74}
        if fail:
            api.get.side_effect = OSError("down")
        else:
            api.get.return_value = {"_links": {"assignee": {"href": assignee} if assignee else None}}
        return api

    def test_mine(self):
        self.assertTrue(af.ticket_still_mine(self._api("/api/v3/users/74"), 1))

    def test_moved_to_tester(self):
        self.assertFalse(af.ticket_still_mine(self._api("/api/v3/users/59"), 1))

    def test_unassigned(self):
        self.assertFalse(af.ticket_still_mine(self._api(None), 1))

    def test_suffix_not_confused(self):
        self.assertFalse(af.ticket_still_mine(self._api("/api/v3/users/174"), 1))

    def test_api_error_keeps_old_behaviour(self):
        self.assertTrue(af.ticket_still_mine(self._api(None, fail=True), 1))


if __name__ == "__main__":
    unittest.main()
