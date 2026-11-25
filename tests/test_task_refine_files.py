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
        files = [{"name": "f%d.txt" % i, "data": b64(str(i))} for i in range(tc.MAX_FILES)]
        with self.assertRaises(ValueError):
            tc.refine_draft("0101-000000-abcd", [], files=files)
        self.assertEqual(os.listdir(os.path.join(self.d, "files")), ["a.png"])

    def test_same_files_resent_are_not_duplicated(self):
        # Bấm "Soạn lại" nhiều lần: form gửi lại đúng câu trả lời + file → không sinh a_1/a_2, không lặp câu trả lời
        files = [{"name": "mock.json", "data": b64("{}")}, {"name": "srs.docx", "data": b64("srs")}]
        ans = [{"q": "Mock?", "a": "", "files": ["mock.json"]}, {"q": "Tài khoản?", "a": "superadmin"}]
        for _ in range(3):
            tc.refine_draft("0101-000000-abcd", ans, files=files)
            tc.set_state(self.d, state="ready")
        self.assertEqual(sorted(os.listdir(os.path.join(self.d, "files"))), ["a.png", "mock.json", "srs.docx"])
        self.assertEqual([x["name"] for x in self.read("request.json")["files"]], ["a.png", "mock.json", "srs.docx"])
        ref = self.read("refine.json")
        self.assertEqual(ref["round"], 3)
        self.assertEqual(len([a for a in ref["answers"] if a["q"] == "Tài khoản?"]), 1)
        self.assertEqual(len([a for a in ref["answers"] if a["q"] == "Mock?"]), 1)
        self.assertIn("mock.json", [a for a in ref["answers"] if a["q"] == "Mock?"][0]["a"])

    def test_same_content_new_name_maps_to_existing(self):
        tc.refine_draft("0101-000000-abcd", [{"q": "Ảnh?", "a": "", "files": ["copy.png"]}],
                        files=[{"name": "copy.png", "data": base64.b64encode(b"x" * 10).decode()}])
        self.assertEqual(os.listdir(os.path.join(self.d, "files")), ["a.png"])
        self.assertIn("a.png", self.read("refine.json")["answers"][0]["a"])

    def test_duplicates_do_not_count_toward_limit(self):
        files = [{"name": "f.txt", "data": b64("same")} for _ in range(tc.MAX_FILES + 3)]
        tc.refine_draft("0101-000000-abcd", [], files=files)
        self.assertEqual(sorted(os.listdir(os.path.join(self.d, "files"))), ["a.png", "f.txt"])


class CreateDedupTest(unittest.TestCase):
    def test_create_uploads_each_content_once(self):
        root = tempfile.mkdtemp()
        d = os.path.join(root, "0101-000000-beef")
        os.makedirs(os.path.join(d, "files"))
        for n, data in (("a.html", b"A"), ("a_1.html", b"A"), ("a_2.html", b"A"), ("b.json", b"B")):
            open(os.path.join(d, "files", n), "wb").write(data)
        tc.set_state(d, state="ready", files=[{"name": n} for n in ("a.html", "b.json", "a_1.html", "a_2.html")])
        api = mock.Mock()
        api.get.side_effect = lambda path: {"_embedded": {"elements": []}} if "types" in path or "priorities" in path \
            else {"lockVersion": 1}
        api.me.return_value = {"id": 74}
        ups = []
        with mock.patch.object(tc, "load", return_value=({"url": "u", "api_key": "k", "project": 1}, {"root": root}, {})), \
                mock.patch.object(tc, "drafts_root", return_value=root), \
                mock.patch.object(tc.op_watch, "OpenProject", return_value=api), \
                mock.patch.object(tc.af, "op_write", return_value={"id": 5, "displayId": "SOAR-1"}), \
                mock.patch.object(tc.af, "inbox"), \
                mock.patch.object(tc, "upload_attachment",
                                  side_effect=lambda api_, wp, path, name, ct: ups.append(name) or {"id": len(ups)}):
            res = tc.create("0101-000000-beef", "S", "![x](attachment:a_2.html)")
        self.assertEqual(ups, ["a.html", "b.json"])
        self.assertEqual(res["attachments"], 2)


if __name__ == "__main__":
    unittest.main()
