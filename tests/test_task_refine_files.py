import base64
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import task_create as tc  # noqa: E402


def b64(s):
    return base64.b64encode(s.encode()).decode()


class RefineFilesTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.d = os.path.join(self.root, "0101-000000-abcd")
        os.makedirs(os.path.join(self.d, "files"))
        open(os.path.join(self.d, "files", "a.png"), "wb").write(b"x" * 10)
        old = [{"name": "a.png", "size": 10, "type": "image/png"}]
        json.dump({"prompt": "p", "files": old, "opts": {}}, open(os.path.join(self.d, "request.json"), "w"))
        tc.set_state(self.d, state="ready", files=old, draft={"subject": "S", "description": "D"})
        patches = [mock.patch.object(tc, "load", return_value=({}, {"root": self.root}, {})),
                   mock.patch.object(tc, "drafts_root", return_value=self.root),
                   mock.patch.object(tc.threading, "Thread")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def read(self, name):
        return json.load(open(os.path.join(self.d, name), encoding="utf-8"))

    def test_files_saved_and_linked_to_answer(self):
        tc.refine_draft("0101-000000-abcd", [{"q": "Ảnh lỗi?", "a": "", "files": ["a.png"]}], files=[
            {"name": "a.png", "type": "image/png", "data": b64("new")},
            {"name": "log.txt", "type": "text/plain", "data": b64("log")}])
        names = [x["name"] for x in self.read("request.json")["files"]]
        self.assertEqual(names, ["a.png", "a_1.png", "log.txt"])
        self.assertEqual(open(os.path.join(self.d, "files", "a.png"), "rb").read(), b"x" * 10)  # file cũ giữ nguyên
        self.assertEqual([x["name"] for x in self.read("state.json")["files"]], names)
        ans = self.read("refine.json")["answers"]
        self.assertIn("a_1.png", ans[0]["a"])
        self.assertEqual(ans[0]["q"], "Ảnh lỗi?")
        self.assertIn("log.txt", ans[1]["a"])  # file chung không gắn ý nào

    def test_only_files_is_enough(self):
        self.assertEqual(tc.refine_draft("0101-000000-abcd", [], files=[{"name": "x.txt", "data": b64("1")}]), 1)

    def test_nothing_entered_rejected(self):
        with self.assertRaises(ValueError):
            tc.refine_draft("0101-000000-abcd", [{"q": "?", "a": " "}])

    def test_limit_counts_existing_files_and_writes_nothing(self):
        files = [{"name": "f%d.txt" % i, "data": b64("1")} for i in range(tc.MAX_FILES)]
        with self.assertRaises(ValueError):
            tc.refine_draft("0101-000000-abcd", [], files=files)
        self.assertEqual(os.listdir(os.path.join(self.d, "files")), ["a.png"])


if __name__ == "__main__":
    unittest.main()
