"""deploy_be build image BE qua SOAR/docker/dev/build-image.sh (giống build tay), nhánh cũ không có script thì docker build."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deploy_qa as D  # noqa: E402

IMG = "soar-be-ss-lastest-k8s:20261123a"
REL = "release-ss/lastest-k8s"


class BeBuildCmdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _script(self):
        p = os.path.join(self.module, D.BE_BUILD_SCRIPT)
        os.makedirs(os.path.dirname(p))
        open(p, "w").close()

    def test_uses_build_image_script(self):
        self._script()
        with mock.patch.object(D.envconf, "get", side_effect=lambda k, d=None: d):
            cmd, env = D.be_build_cmd(self.module, IMG, "20261123a", REL, "abc123")
        self.assertEqual(cmd, ["bash", "docker/dev/build-image.sh", IMG, "--build-arg", "GIT_BRANCH=" + REL])
        self.assertEqual(env["SOAR_VERSION"], "20261123a")

    def test_custom_version_arg_passed_to_script(self):
        self._script()
        with mock.patch.object(D.envconf, "get", side_effect=lambda k, d=None: "APP_VERSION" if k == "DEPLOY_VERSION_ARG" else d):
            cmd, _ = D.be_build_cmd(self.module, IMG, "20261123a", REL, "abc123")
        self.assertEqual(cmd[-2:], ["--build-arg", "APP_VERSION=20261123a"])

    def test_fallback_docker_build_without_script(self):
        with mock.patch.object(D.envconf, "get", side_effect=lambda k, d=None: d):
            cmd, env = D.be_build_cmd(self.module, IMG, "20261123a", REL, "abc123")
        self.assertIsNone(env)
        self.assertEqual(cmd[:2], ["docker", "build"])
        for a in ("SOAR_VERSION=20261123a", "GIT_COMMIT=abc123", "GIT_BRANCH=" + REL):
            self.assertIn(a, cmd)


if __name__ == "__main__":
    unittest.main()
