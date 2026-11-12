"""Kiểm tra FE lệch bản (deploy_qa fe_drift / recover_fe / deploy_parts) — sự cố 11/11 18:35: FE 2.0.34.124 đã build,
docker load, commit "update version 2.0.34.124" lên dev-ss, nhưng BE build lỗi nên không rollout → server vẫn chạy
2.0.34.123, các lượt sau thấy "FE 0 commit" nên không bao giờ deploy lại FE.

Chạy: python3 -m unittest discover -s tests   (trong thư mục op-watch; không ssh / không mạng — remote, git, fingerprint
đều được giả lập)
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
import deploy_qa as d  # noqa: E402

RUNNING, LATEST = "2.0.34.123", "2.0.34.124"


def acfg(root="/tmp/x"):
    return {"fe_repo": "/fake/soar-fe", "fe_upstream": "dev-ss", "be_repo": "/fake/soar-be", "be_upstream": "develop-ss",
            "root": root, "custom_deploy": []}


def dcfg():
    cfg = dict(d.DEFAULTS)
    cfg.update({"host": "198.51.100.1", "user": "u", "password": "x", "base": "~/soar_v2.0",
                "fe_url": "https://fe.invalid", "api_url": "https://api.invalid", "be_release_branch": "release/x"})
    return cfg


def git_latest(subject="update version %s" % LATEST):
    """git_lines giả: log -1 --grep=update version → subject."""
    def fake(repo, *args):
        if "--grep=update version" in args:
            return [subject] if subject else []
        return []
    return fake


class FakeRemote:
    """remote() giả: ghi lại lệnh, trả lời lệnh chỉ đọc theo cấu hình. Không ssh."""
    def __init__(self, server_has_image=True, running_image="soar-fe-ss:%s" % RUNNING):
        self.cmds, self.server_has_image, self.running_image = [], server_has_image, running_image

    def __call__(self, dcfg, command, logf=None, timeout=1800):
        self.cmds.append(command)
        if command.startswith("docker inspect"):
            return self.running_image + "\n"
        if command.startswith("docker images -q"):
            return "0123abcd\n" if self.server_has_image else ""
        return ""


class Quiet(unittest.TestCase):
    def setUp(self):
        self.logs = []
        p = mock.patch.object(d, "log", side_effect=lambda m: self.logs.append(m))
        p.start()
        self.addCleanup(p.stop)


class Drift(Quiet):
    def test_latest_version_from_commit(self):
        with mock.patch.object(d, "git_lines", side_effect=git_latest()):
            self.assertEqual(d.fe_latest_version(acfg()), LATEST)
        with mock.patch.object(d, "git_lines", side_effect=git_latest("Update Version 2.0.35.1")):
            self.assertEqual(d.fe_latest_version(acfg()), "2.0.35.1")
        with mock.patch.object(d, "git_lines", side_effect=git_latest(None)):
            self.assertIsNone(d.fe_latest_version(acfg()))

    def check(self, running, latest=LATEST, soar=True):
        with mock.patch.object(d, "git_lines", side_effect=git_latest("update version %s" % latest)), \
                mock.patch.object(d, "soar_server", return_value=soar), \
                mock.patch.object(d, "fingerprint", return_value={"fe": running, "be": None}) as fp:
            out = d.fe_drift(acfg(), dcfg())
            fp.assert_called_once()
            return out

    def test_server_older_is_drift(self):
        drift = self.check(RUNNING)
        self.assertEqual((drift["running"], drift["latest"]), (RUNNING, LATEST))
        self.assertIn("FE trên server (2.0.34.123) lệch bản mới nhất dev-ss (2.0.34.124) — cần deploy lại FE",
                      d.drift_text(drift))

    def test_same_version_no_drift(self):
        self.assertIsNone(self.check(LATEST))

    def test_server_down_no_drift(self):
        self.assertIsNone(self.check(None))  # không đọc được version → không kết luận

    def test_server_newer_no_drift(self):
        self.assertIsNone(self.check("2.0.34.125"))  # không tự hạ bản

    def test_non_soar_skipped(self):
        with mock.patch.object(d, "soar_server", return_value=False), \
                mock.patch.object(d, "fingerprint") as fp:
            self.assertIsNone(d.fe_drift(acfg(), dcfg()))
            fp.assert_not_called()

    def test_uses_given_fingerprint(self):
        with mock.patch.object(d, "git_lines", side_effect=git_latest()), \
                mock.patch.object(d, "fingerprint") as fp:
            self.assertTrue(d.fe_drift(acfg(), dcfg(), {"fe": RUNNING}))
            fp.assert_not_called()

    def test_prev_version(self):
        self.assertEqual(d._prev_version("2.0.34.124"), "2.0.34.123")
        self.assertEqual(d._prev_version("2.0.5"), "2.0.4")
        self.assertIsNone(d._prev_version("2.0.34.0"))
        self.assertIsNone(d._prev_version("abc"))


DRIFT = {"running": RUNNING, "latest": LATEST, "branch": "dev-ss"}


class Recover(Quiet):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.set_image = mock.patch.object(d, "set_compose_image").start()
        self.addCleanup(mock.patch.stopall)
        self.af_run = mock.patch.object(d.af, "run", return_value="").start()
        self.push = mock.patch.object(d.af, "push").start()
        self.upload = mock.patch.object(d, "upload").start()
        self.sh = mock.patch.object(d, "sh", return_value="").start()

    def test_image_on_server_only_rollout(self):
        rem = FakeRemote(server_has_image=True)
        changes = []
        with mock.patch.object(d, "remote", side_effect=rem), \
                mock.patch.object(d, "deploy_fe") as dfe, \
                mock.patch.object(d, "local_image_exists") as local:
            v = d.recover_fe(acfg(self.root), dcfg(), self.root, None, DRIFT, changes)
        self.assertEqual(v, LATEST)
        dfe.assert_not_called()
        local.assert_not_called()
        self.upload.assert_not_called()
        self.sh.assert_not_called()  # không build, không docker save
        self.push.assert_not_called()
        self.assertFalse(any("commit" in c[0][0] for c in self.af_run.call_args_list))
        # chỉ lệnh đọc trên server (ghi compose qua set_compose_image — rollout làm phần còn lại)
        self.assertTrue(all(c.startswith(("docker inspect", "docker images -q")) for c in rem.cmds), rem.cmds)
        self.set_image.assert_called_once_with(mock.ANY, "soar-fe-nginx", "soar-fe-ss:%s" % LATEST,
                                              None)
        self.assertEqual(changes, [{"part": "fe", "service": "soar-fe-nginx", "container": "soar-fe",
                                    "old_image": "soar-fe-ss:%s" % RUNNING, "new_image": "soar-fe-ss:%s" % LATEST,
                                    "version": LATEST}])
        self.assertTrue(any("đã có trên server — chỉ rollout lại FE" in m for m in self.logs))

    def test_image_local_only_upload(self):
        rem = FakeRemote(server_has_image=False)
        changes = []
        tar = os.path.join(self.root, "soar-fe-ss.tar")
        self.sh.side_effect = lambda cmd, **kw: open(tar, "w").close() or ""
        with mock.patch.object(d, "remote", side_effect=rem), \
                mock.patch.object(d, "deploy_fe") as dfe, \
                mock.patch.object(d, "local_image_exists", return_value=True):
            v = d.recover_fe(acfg(self.root), dcfg(), self.root, None, DRIFT, changes)
        self.assertEqual(v, LATEST)
        dfe.assert_not_called()
        self.assertEqual(self.sh.call_args[0][0], ["docker", "save", "-o", tar, "soar-fe-ss:%s" % LATEST])
        self.upload.assert_called_once()
        self.assertTrue(any("docker load" in c for c in rem.cmds))
        self.push.assert_not_called()
        self.assertEqual(changes[0]["new_image"], "soar-fe-ss:%s" % LATEST)

    def test_no_image_rebuild_pinned(self):
        rem = FakeRemote(server_has_image=False)
        with mock.patch.object(d, "remote", side_effect=rem), \
                mock.patch.object(d, "deploy_fe", return_value=LATEST) as dfe, \
                mock.patch.object(d, "local_image_exists", return_value=False):
            v = d.recover_fe(acfg(self.root), dcfg(), self.root, None, DRIFT, [])
        self.assertEqual(v, LATEST)
        self.assertEqual(dfe.call_args[1]["pin_version"], LATEST)
        self.assertEqual(dfe.call_args[1]["old_image"], "soar-fe-ss:%s" % RUNNING)


class PinnedBuild(Quiet):
    """deploy_fe(pin_version=X): build-and-deploy.sh tăng từ bản trước → đúng X; KHÔNG commit / push update version."""
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(os.path.join(self.repo, "node_modules"))
        open(os.path.join(self.repo, ".env.deploy"), "w").close()

    def fake_worktree(self, repo, path, ref):
        os.makedirs(os.path.join(path, "build_dev"))
        with open(os.path.join(path, "build_dev", "version-tags.json"), "w") as f:
            json.dump({"SS": {"dev": {"version": LATEST, "image_id": "x"}}}, f)

    @staticmethod
    def fake_build(cmd, cwd=None, **kw):
        path = os.path.join(cwd, "build_dev", "version-tags.json")
        with open(path) as f:
            tags = json.load(f)
        v = tags["SS"]["dev"]["version"].split(".")
        tags["SS"]["dev"]["version"] = ".".join(v[:-1] + [str(int(v[-1]) + 1)])  # như build-and-deploy.sh
        with open(path, "w") as f:
            json.dump(tags, f)
        return ""

    def run_fe(self, **kw):
        a = acfg(self.root)
        a["fe_repo"] = self.repo
        changes = []
        with mock.patch.object(d, "fresh_worktree", side_effect=self.fake_worktree), \
                mock.patch.object(d, "sh", side_effect=self.fake_build), \
                mock.patch.object(d.af, "run", return_value="") as run, \
                mock.patch.object(d.af, "push") as push, \
                mock.patch.object(d.af, "build_env", return_value={}), \
                mock.patch.object(d, "upload"), \
                mock.patch.object(d, "remote", return_value=""), \
                mock.patch.object(d, "compose_image", return_value="soar-fe-ss:%s" % RUNNING), \
                mock.patch.object(d, "set_compose_image"), \
                mock.patch.object(d.subprocess, "run"):
            v = d.deploy_fe(a, dcfg(), self.root, None, changes, **kw)
        commits = [c for c in run.call_args_list if "commit" in c[0][0]]
        return v, commits, push, changes

    def test_pinned_no_commit(self):
        v, commits, push, changes = self.run_fe(pin_version=LATEST)
        self.assertEqual(v, LATEST)
        self.assertEqual(commits, [])
        push.assert_not_called()
        self.assertEqual(changes[0]["new_image"], "soar-fe-ss:%s" % LATEST)

    def test_normal_still_commits(self):
        v, commits, push, _ = self.run_fe()
        self.assertEqual(v, "2.0.34.125")
        self.assertEqual(len(commits), 1)
        self.assertIn("update version 2.0.34.125", commits[0][0][0])
        push.assert_called_once()


class Parts(Quiet):
    """deploy_parts: FE và BE độc lập — BE build lỗi vẫn rollout FE đã build + docker load."""
    def setUp(self):
        super().setUp()
        self.logf = tempfile.NamedTemporaryFile("w", delete=False, suffix=".log")
        self.addCleanup(os.remove, self.logf.name)
        self.addCleanup(self.logf.close)

    @staticmethod
    def fe_change(*a, **kw):
        changes = a[4] if len(a) > 4 else kw.get("changes")
        changes.append({"part": "fe", "service": "soar-fe-nginx", "container": "soar-fe", "old_image": "old",
                        "new_image": "soar-fe-ss:%s" % LATEST, "version": LATEST})
        return LATEST

    def run_parts(self, commits, drift=None, be_error=None, rollout_error=None):
        with mock.patch.object(d, "deploy_fe", side_effect=self.fe_change) as dfe, \
                mock.patch.object(d, "recover_fe", side_effect=lambda a, dc, r, l, dr, ch: self.fe_change(a, dc, r, l, ch)) as rec, \
                mock.patch.object(d, "deploy_be", side_effect=be_error or (lambda *a, **k: "20261112a")) as dbe, \
                mock.patch.object(d, "rollout", side_effect=rollout_error) as ro:
            out = d.deploy_parts(acfg(), dcfg(), "/tmp/none", self.logf, commits, drift)
        return out, dfe, rec, dbe, ro

    def test_be_build_fails_fe_still_rolled_out(self):
        (parts, fe_version, errors), dfe, rec, dbe, ro = self.run_parts(
            {"FE": [("h1", "[SOAR-1] x")], "BE": [("h2", "[SOAR-2] y")]},
            be_error=RuntimeError("docker → mã 1: COMPILATION ERROR"))
        ro.assert_called_once()
        self.assertEqual([c["part"] for c in ro.call_args[0][1]], ["fe"])
        self.assertEqual(parts, ["FE %s" % LATEST])
        self.assertEqual(fe_version, LATEST)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("BE build/deploy lỗi: docker → mã 1"), errors)
        msg = d._deploy_error_msg(errors, parts, self.logf)
        self.assertIn("BE build/deploy lỗi", msg)
        self.assertIn("vẫn đã deploy FE %s" % LATEST, msg)
        self.logf.flush()
        with open(self.logf.name, encoding="utf-8") as f:
            self.assertIn("### BE build/deploy lỗi", f.read())

    def test_drift_only_recovers_fe(self):
        (parts, fe_version, errors), dfe, rec, dbe, ro = self.run_parts({"FE": [], "BE": []}, drift=DRIFT)
        dfe.assert_not_called()  # không tạo commit update version mới
        rec.assert_called_once()
        dbe.assert_not_called()
        ro.assert_called_once()
        self.assertEqual((parts, fe_version, errors), (["FE %s" % LATEST], LATEST, []))

    def test_new_fe_commits_ignore_drift(self):
        (_, _, errors), dfe, rec, _, _ = self.run_parts({"FE": [("h1", "x")], "BE": []}, drift=DRIFT)
        dfe.assert_called_once()
        rec.assert_not_called()
        self.assertEqual(errors, [])

    def test_rollout_error_reported(self):
        (parts, fe_version, errors), *_ = self.run_parts({"FE": [], "BE": []}, drift=DRIFT,
                                                         rollout_error=d.RolloutError("FE bản mới không lên"))
        self.assertEqual(parts, [])
        self.assertIsNone(fe_version)
        self.assertTrue(errors[0].startswith("rollout FE lỗi: FE bản mới không lên"), errors)

    def test_fe_error_be_continues(self):
        with mock.patch.object(d, "deploy_fe", side_effect=RuntimeError("vite lỗi")), \
                mock.patch.object(d, "deploy_be", return_value="20261112a"), \
                mock.patch.object(d, "rollout") as ro:
            parts, fe_version, errors = d.deploy_parts(acfg(), dcfg(), "/tmp/none", self.logf,
                                                       {"FE": [("h", "x")], "BE": [("h2", "y")]})
        self.assertEqual(errors, ["FE build/deploy lỗi: vite lỗi"])
        ro.assert_not_called()  # deploy_be giả không thêm change → không gì để rollout
        self.assertIsNone(fe_version)


class Flows(Quiet):
    """--dry-run không đổi gì trên server; review-watch chỉ ghi lý do, không deploy."""
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.addCleanup(mock.patch.stopall)
        self.parts = mock.patch.object(d, "deploy_parts").start()
        self.recover = mock.patch.object(d, "recover_fe").start()
        self.rollout = mock.patch.object(d, "rollout").start()
        self.remote = mock.patch.object(d, "remote").start()
        mock.patch.object(d, "inbox").start()
        mock.patch.object(d, "notify").start()
        mock.patch.object(d, "save_pending").start()

    def proj_env(self, qa=True):
        a = acfg(self.root)
        dc = dcfg()
        dc["qa_review_status_ids"] = [7]
        mock.patch.object(d, "load", return_value=({"url": "u", "api_key": "k", "project": "p"}, a, dc)).start()
        mock.patch.object(d, "load_all", return_value=({}, [{}])).start()
        mock.patch.object(d, "qa_enabled", return_value=qa).start()
        mock.patch.object(d, "pending", return_value={"FE": [], "BE": []}).start()
        mock.patch.object(d, "git_lines", side_effect=git_latest()).start()
        mock.patch.object(d, "fingerprint", return_value={"fe": RUNNING, "be": "soar:x", "be_started": None}).start()
        mock.patch.object(d.af, "run", return_value="").start()
        return a, dc

    def test_run_one_dry_run(self):
        self.proj_env()
        api = mock.Mock()
        api.me.return_value = {"id": 1}
        mock.patch.object(d.op_watch, "OpenProject", return_value=api).start()
        mock.patch.object(d, "load_pending", return_value=["SOAR-2920"]).start()
        mock.patch.object(d, "resolved_mine", return_value=[]).start()
        d.run_one({"name": "x", "op_project": "p"}, dry_run=True)
        self.parts.assert_not_called()
        self.recover.assert_not_called()
        self.rollout.assert_not_called()
        self.remote.assert_not_called()
        self.assertTrue(any("lệch bản mới nhất dev-ss (2.0.34.124)" in m and "dry-run" in m for m in self.logs),
                        self.logs)

    def test_run_one_drift_deploys(self):
        self.proj_env()
        api = mock.Mock()
        api.me.return_value = {"id": 1}
        mock.patch.object(d.op_watch, "OpenProject", return_value=api).start()
        mock.patch.object(d, "load_pending", return_value=["SOAR-2920"]).start()
        mock.patch.object(d, "resolved_mine", return_value=[]).start()
        self.parts.return_value = ([], None, ["BE build/deploy lỗi: x"])  # dừng ngay sau deploy (không QA)
        d.run_one({"name": "x", "op_project": "p"})
        self.parts.assert_called_once()
        self.assertEqual(self.parts.call_args[0][5]["latest"], LATEST)

    def test_deploy_only_dry_run(self):
        a, dc = self.proj_env(qa=False)
        mock.patch.object(d.P, "current", return_value={"name": "x", "key": "x"}).start()
        d.deploy_only({}, a, dc, dry_run=True)
        self.parts.assert_not_called()
        self.assertTrue(any("cần deploy lại FE" in m for m in self.logs), self.logs)

    def test_review_watch_logs_but_no_deploy(self):
        self.proj_env()
        api = mock.Mock()
        api.my_work_packages.return_value = [{"displayId": "SOAR-2920", "id": 1}]
        mock.patch.object(d.op_watch, "OpenProject", return_value=api).start()
        mock.patch.object(d, "QA_ACTIVE", os.path.join(self.root, "nope")).start()
        mock.patch.object(d.subprocess, "run", return_value=mock.Mock(returncode=3)).start()
        mock.patch.object(d, "load_pending", return_value=[]).start()
        mock.patch.object(d, "wait_up", return_value=True).start()
        mock.patch.object(d, "_autofix_busy", return_value=False).start()
        mock.patch.object(d, "qa_history", return_value={}).start()
        run_one = mock.patch.object(d, "run_one").start()
        d._review_watch({"name": "x"})
        self.assertTrue(any("FE trên server (2.0.34.123) lệch bản mới nhất dev-ss (2.0.34.124) — cần deploy lại FE" in m
                            for m in self.logs), self.logs)
        self.parts.assert_not_called()
        self.recover.assert_not_called()
        self.remote.assert_not_called()
        run_one.assert_called_once()  # vẫn QA như cũ (run_one(only=…) không deploy)
        self.assertTrue(run_one.call_args[1]["only"])


if __name__ == "__main__":
    unittest.main()
