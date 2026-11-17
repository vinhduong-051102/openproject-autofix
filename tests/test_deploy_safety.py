"""Kiểm tra tài khoản registry trước khi build + deploy chờ QA đang chạy (sự cố 17/11)."""
import fcntl
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deploy_qa as D  # noqa: E402


class RegistryPreflightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = self.tmp.name
        os.makedirs(os.path.join(home, ".docker"))
        with open(os.path.join(home, ".docker", "config.json"), "w") as f:
            f.write('{"auths": {"reg.example": {"auth": "dXNlcjpwYXNz"}}}')  # user:pass
        self.env_file = os.path.join(home, "git2.env")
        with open(self.env_file, "w") as f:
            f.write("GIT2_USER=user\nGIT2_PASSWORD='old'\n")
        self.home = mock.patch.dict(os.environ, {"HOME": home})
        self.home.start()
        self.dcfg = {"registry_host": "reg.example", "registry_env_file": self.env_file}

    def tearDown(self):
        self.home.stop()
        self.tmp.cleanup()

    def test_both_ok(self):
        with mock.patch.object(D, "_http_status", return_value=200):
            self.assertEqual(D.registry_preflight(self.dcfg), {"docker": None, "api": None})

    def test_rejected_credentials_reported(self):
        with mock.patch.object(D, "_http_status", return_value=401) as st:
            out = D.registry_preflight(self.dcfg)
        self.assertIn("docker login reg.example -u user", out["docker"])
        self.assertIn("GIT2_PASSWORD", out["api"])
        st.assert_any_call("https://reg.example/api/v1/user", "user", "old")

    def test_network_error_not_blamed_on_password(self):
        with mock.patch.object(D, "_http_status", return_value=None):
            self.assertEqual(D.registry_preflight(self.dcfg), {"docker": None, "api": None})

    def test_deploy_parts_skips_fe_and_custom_but_deploys_be(self):
        acfg = {"custom_deploy": [{"label": "FLOW"}]}
        logf = mock.Mock(name="logf")
        logf.name = "/tmp/x.log"
        commits = {"FE": [("a", "s")], "BE": [("b", "s")], "FLOW": [("c", "s")]}
        with mock.patch.object(D, "registry_preflight", return_value={"docker": "từ chối", "api": None}), \
                mock.patch.object(D, "deploy_fe") as fe, mock.patch.object(D, "deploy_custom") as cu, \
                mock.patch.object(D, "deploy_be", return_value="tag1") as be, \
                mock.patch.object(D, "rollout"), mock.patch.object(D.qa_issues, "record"):
            parts, _, errors = D.deploy_parts(acfg, {"be_image": "img"}, "/tmp", logf, commits)
        fe.assert_not_called()
        cu.assert_not_called()
        be.assert_called_once()
        self.assertTrue(any(e.startswith("FE chưa build") for e in errors))
        self.assertTrue(any(e.startswith("FLOW chưa build") for e in errors))


class DeployWaitsQaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(D, "QA_RUN_LOCK", os.path.join(self.tmp.name, ".qa-run.lock"))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_deploy_waits_for_running_qa_then_times_out(self):
        qa = D.qa_lock({"qa_wait_deploy_minutes": 0})  # một lượt QA đang chạy (khoá đọc)
        with mock.patch.object(D.qa_issues, "record") as rec:
            fd = D.deploy_lock({"deploy_wait_qa_minutes": 0})
        rec.assert_called_once()
        self.assertEqual(rec.call_args[0][0], "deploy_wait_qa_timeout")
        fd.close()
        qa.close()

    def test_two_qa_runs_share_and_deploy_gets_lock_when_free(self):
        a = D.qa_lock({"qa_wait_deploy_minutes": 0})
        b = D.qa_lock({"qa_wait_deploy_minutes": 0})   # QA song song được
        a.close()
        b.close()
        with mock.patch.object(D.qa_issues, "record") as rec:
            fd = D.deploy_lock({"deploy_wait_qa_minutes": 0})
        rec.assert_not_called()
        # deploy xong → hạ xuống khoá đọc cho QA của chính lượt đó; QA khác vẫn vào được
        D.qa_lock({"qa_wait_deploy_minutes": 0}, fd)
        other = open(D.QA_RUN_LOCK, "a")
        fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
        other.close()
        fd.close()


if __name__ == "__main__":
    unittest.main()
