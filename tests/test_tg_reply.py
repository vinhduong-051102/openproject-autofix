import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tg_requests as tr  # noqa: E402


class TgBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "media"), exist_ok=True)
        self.p_root = mock.patch.object(tr, "_ROOT", self.tmp)
        self.p_root.start()
        tr._LOCAL.conn = None
        self.cfg = dict(tr.settings())
        self.p_set = mock.patch.object(tr, "settings", lambda: self.cfg)
        self.p_set.start()
        self.p_chat = mock.patch.object(tr, "chat_cfg", lambda cid: {"name": "Tester A", "kind": "user", "asks": True})
        self.p_chat.start()

    def tearDown(self):
        tr._LOCAL.conn = None
        self.p_root.stop()
        self.p_set.stop()
        self.p_chat.stop()

    def msg(self, rid, tg_id, sender, text, chat=1):
        d = datetime(2026, 11, 19, 10, 0, tg_id % 60, tzinfo=timezone.utc).astimezone().isoformat(timespec="seconds")
        cur = tr.db().execute("INSERT INTO messages (chat_id, tg_msg_id, sender, sender_name, text, date, request_id)"
                              " VALUES (?, ?, ?, ?, ?, ?, ?)", (chat, tg_id, sender, "Tester A", text, d, rid))
        return cur.lastrowid

    def req(self, status="pending", **f):
        rid = tr._new_request(1, "Tester A", tr.now(), "x")
        tr.update(rid, status=status, **f)
        return rid


class BulletsTest(unittest.TestCase):
    def test_single_point_stays_sentence(self):
        self.assertEqual(tr.bullets(["Màn nào bị lỗi ạ?"]), "Màn nào bị lỗi ạ?")

    def test_many_points_become_dashes_with_sub_points(self):
        out = tr.bullets(["Màn nào?", "Tài khoản nào?\n- quyền gì\n- tenant nào", " "])
        self.assertEqual(out, "- Màn nào?\n- Tài khoản nào?\n  + quyền gì\n  + tenant nào")

    def test_clarify_text_uses_bullets(self):
        one = tr.clarify_text({"title": "Lỗi A"}, ["Màn nào?"])
        self.assertIn("bổ sung giúp em: Màn nào?", one)
        many = tr.clarify_text({"title": "Lỗi A"}, ["Màn nào?", "Tài khoản nào?"])
        self.assertIn(":\n- Màn nào?\n- Tài khoản nào?", many)
        self.assertNotIn("1. ", many)


class ClarifyAutoTest(TgBase):
    def test_auto_sends_when_enabled(self):
        rid = self.req(ai_missing=json.dumps(["Màn nào?"]))
        self.msg(rid, 5, "tester", "lỗi rồi")
        self.cfg["clarify_auto"] = True
        self.assertTrue(tr.maybe_clarify(rid))
        r = tr.row("SELECT * FROM requests WHERE id=?", (rid,))
        self.assertEqual(r["status"], "need_info")
        self.assertEqual(r["clarify_count"], 1)
        o = tr.row("SELECT * FROM outbox WHERE request_id=?", (rid,))
        self.assertEqual(o["purpose"], "clarify")
        self.assertEqual(o["reply_to"], 5)

    def test_disabled_or_limit_reached_does_not_send(self):
        rid = self.req(ai_missing=json.dumps(["Màn nào?"]))
        self.cfg["clarify_auto"] = False
        self.assertFalse(tr.maybe_clarify(rid))
        self.cfg["clarify_auto"] = True
        tr.update(rid, clarify_count=tr.CLARIFY_MAX)
        self.assertFalse(tr.maybe_clarify(rid))
        self.assertIsNone(tr.row("SELECT * FROM outbox WHERE request_id=?", (rid,)))


class ReplyTest(TgBase):
    def test_style_samples_skip_tool_messages_and_prefer_same_chat(self):
        self.msg(None, 1, "me", "ok a, để e xem", chat=1)
        self.msg(None, 2, "me", "🤖 Để tạo việc …", chat=1)
        self.msg(None, 3, "me", "chiều e deploy nhé", chat=2)
        self.msg(None, 4, "tester", "anh ơi", chat=1)
        out = tr.style_samples(1)
        self.assertEqual(out[0], "ok a, để e xem")
        self.assertIn("chiều e deploy nhé", out)
        self.assertFalse(any(t.startswith("🤖") for t in out))
        self.assertNotIn("anh ơi", out)

    def test_draft_then_send_reply(self):
        rid = self.req(status="answer")
        self.msg(rid, 7, "tester", "SOAR-1 xong chưa a?")
        self.msg(None, 8, "me", "để e check nha")
        fake_cfg = ({"url": "http://op", "api_key": "k"}, {}, {})
        with mock.patch("task_create.load", return_value=fake_cfg), \
                mock.patch.object(tr, "_refs_in", return_value=[]), \
                mock.patch.object(tr, "_run_claude_json",
                                  return_value={"reply": "- xong rồi a\n- chiều e deploy", "confident": False}) as ai:
            tr.draft_reply(rid, hint="nói chiều deploy")
        prompt = ai.call_args[0][0]
        self.assertIn("để e check nha", prompt)  # văn phong lấy từ tin của mình
        self.assertIn("nói chiều deploy", prompt)
        r = tr.row("SELECT * FROM requests WHERE id=?", (rid,))
        self.assertEqual(r["status"], "answer")
        self.assertIn("chiều e deploy", r["reply_draft"])
        self.assertTrue(r["reply_note"])  # confident false → có ghi chú
        self.assertIsNone(tr.row("SELECT * FROM outbox WHERE request_id=?", (rid,)))  # chưa gửi gì

        tr.send_reply(rid, "xong rồi a")
        o = tr.row("SELECT * FROM outbox WHERE request_id=?", (rid,))
        self.assertEqual((o["purpose"], o["reply_to"], o["text"]), ("reply", 7, "xong rồi a"))
        self.assertEqual(tr.row("SELECT status FROM requests WHERE id=?", (rid,))["status"], "answered")
        tr.mark_outbox(o["id"], sent_id=99)
        self.assertEqual(tr.row("SELECT reply_msg_id FROM requests WHERE id=?", (rid,))["reply_msg_id"], 99)

    def test_send_error_returns_to_answer(self):
        rid = self.req(status="answer")
        self.msg(rid, 7, "tester", "hỏi?")
        tr.send_reply(rid, "trả lời")
        o = tr.row("SELECT * FROM outbox WHERE request_id=?", (rid,))
        tr.mark_outbox(o["id"], error="flood wait")
        r = tr.row("SELECT * FROM requests WHERE id=?", (rid,))
        self.assertEqual(r["status"], "answer")
        self.assertIn("flood wait", r["reply_note"])

    def test_empty_reply_rejected(self):
        rid = self.req(status="answer")
        with self.assertRaises(ValueError):
            tr.send_reply(rid, "  ")


class TriageQuestionTest(TgBase):
    def test_question_goes_to_answer_with_draft_and_new_missing_auto_asks(self):
        rid = self.req(status="triage")
        a = self.msg(rid, 11, "tester", "SOAR-1 xong chưa a?")
        b = self.msg(rid, 12, "tester", "màn báo cáo lỗi")
        self.cfg["clarify_auto"] = True
        out = {"items": [
            {"kind": "question", "title": "Hỏi tiến độ SOAR-1", "message_ids": [a], "refs": ["SOAR-1"]},
            {"kind": "new", "type": "Bug", "title": "Lỗi báo cáo", "message_ids": [b], "clear": False,
             "missing": ["Màn nào?", "Tài khoản nào?"]}]}
        fake_cfg = ({"url": "http://op", "api_key": "k"}, {}, {})
        with mock.patch("task_create.load", return_value=fake_cfg), \
                mock.patch("op_watch.OpenProject"), \
                mock.patch.object(tr, "_refs_in", return_value=[]), \
                mock.patch.object(tr, "_recent_wps", return_value=""), \
                mock.patch.object(tr, "docs_block", return_value=""), \
                mock.patch.object(tr, "media_docs", return_value={"docs": []}), \
                mock.patch.object(tr, "_run_claude_json", side_effect=[out, {"reply": "chưa a, mai e làm"}]):
            tr.triage(rid)
        q = tr.row("SELECT * FROM requests WHERE id=?", (rid,))
        self.assertEqual((q["ai_kind"], q["status"], q["reply_draft"]), ("question", "answer", "chưa a, mai e làm"))
        new = tr.row("SELECT * FROM requests WHERE id!=? ORDER BY id DESC LIMIT 1", (rid,))
        self.assertEqual(new["status"], "need_info")
        o = tr.row("SELECT * FROM outbox WHERE request_id=?", (new["id"],))
        self.assertIn("- Màn nào?\n- Tài khoản nào?", o["text"])
        self.assertIsNone(tr.row("SELECT * FROM outbox WHERE request_id=?", (rid,)))  # câu trả lời chưa gửi


if __name__ == "__main__":
    unittest.main()
