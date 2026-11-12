"""Kiểm tra bước kiểm tra lại kết luận ĐẠT của deploy_qa (verify_pass) — sự cố SOAR-2896: Gemini gặp HTTP 500 ở
POST /api/v1/correlation-rules/filter nhưng vẫn kết luận ĐẠT.

Chạy: python3 -m unittest discover -s tests   (trong thư mục op-watch; không gọi server / OpenProject)
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import deploy_qa as d  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "SOAR-2896-qa")
NO_LOG = {"qa_be_log_check": False}


def qa_md(requests, verdict="ĐẠT"):
    """qa.md dạng Gemini với các request (method, target, output)."""
    blocks = ["## %s %s\n(why)\n```\n%s\n```" % (m, t, out) for m, t, out in requests]
    return "QA: %s\n\n_QA bằng Gemini (x), %d request_\n\n## Kết quả\nĐạt.\n\n## Bằng chứng\n- ...\n\n## Ghi chú\n-\n\n" \
           "## Toàn bộ request\n%s\n" % (verdict, len(blocks), "\n\n".join(blocks))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(FIXTURE, "diff.md"), os.path.join(self.dir, "diff.md"))  # có phần BE (correlation)

    def tearDown(self):
        shutil.rmtree(self.dir)


class Soar2896(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIXTURE, "qa.md"), encoding="utf-8") as f:
            self.text = f.read()

    def test_parse_requests(self):
        reqs = d.qa_requests_from_md(self.text)
        self.assertEqual(len(reqs), 24)  # "24 request" trong qa.md
        bad = [r for r in reqs if r["status"] and r["status"] >= 500]
        self.assertEqual([(r["method"], r["target"]) for r in bad], [("POST", "/api/v1/correlation-rules/filter")])

    def test_guard_downgrades(self):
        text, reasons = d.verify_pass(self.text, FIXTURE, NO_LOG)
        self.assertIn("HTTP 5xx", reasons)
        self.assertTrue(text.startswith("QA: KHÔNG ĐẠT\n"))
        result = d.af.report_section(text, "Kết quả")
        self.assertIn("HTTP 5xx", result)
        evidence = d.af.report_section(text, "Bằng chứng")
        self.assertIn("POST /api/v1/correlation-rules/filter", evidence)
        self.assertIn("HTTP 500", evidence)
        self.assertIn("Internal Server Error", evidence)

    def test_be_log_also_reported(self):
        lines = ["2026-11-11T14:07:01.000+07:00 ERROR 1 --- [soar] [io-8888-exec-3] o.h.e.j.s.SqlExceptionHelper : "
                 "ERROR: column cr.case_severity_field does not exist"]
        text, reasons = d.verify_pass(self.text, FIXTURE, dict(be_container="soar"), time.time() - 60, time.time(),
                                      read_log=lambda s, e: (lines, ""))
        self.assertEqual(reasons, ["HTTP 5xx", "lỗi log BE"])
        self.assertIn("does not exist", d.af.report_section(text, "Bằng chứng"))


class Rules(Base):
    def test_search_only_with_be(self):
        text, reasons = d.verify_pass(qa_md([("SEARCH", "case_severity_field", "SEARCH ... 3 kết quả")]), self.dir, NO_LOG)
        self.assertEqual(reasons, ["BE chưa có bằng chứng"])
        self.assertTrue(text.startswith("QA: KHÔNG TỰ TEST ĐƯỢC\n"))

    def test_unrelated_2xx_not_enough(self):
        text, reasons = d.verify_pass(qa_md([("GET", "/api/v1/users/me", "HTTP 200\n\n{}")]), self.dir, NO_LOG)
        self.assertTrue(text.startswith("QA: KHÔNG TỰ TEST ĐƯỢC"))

    def test_related_2xx_passes(self):
        src = qa_md([("SEARCH", "x", "1 kết quả"), ("POST", "/api/v1/correlation-rules/filter", "HTTP 200\n\n{}")])
        text, reasons = d.verify_pass(src, self.dir, NO_LOG)
        self.assertEqual(reasons, [])
        self.assertEqual(text, src)

    def test_fe_only_search_passes(self):
        os.remove(os.path.join(self.dir, "diff.md"))
        src = qa_md([("SEARCH", "x", "1 kết quả")])
        self.assertEqual(d.verify_pass(src, self.dir, NO_LOG), (src, []))

    def test_not_pass_untouched(self):
        src = qa_md([("GET", "/api/v1/correlation-rules", "HTTP 500\n\n{}")], verdict="KHÔNG ĐẠT")
        self.assertEqual(d.verify_pass(src, self.dir, NO_LOG), (src, []))

    def test_501_and_log_read_failure(self):
        src = qa_md([("GET", "/api/v1/correlation-rules/1", "HTTP 501\n\n{}")])
        text, reasons = d.verify_pass(src, self.dir, {}, time.time() - 60, time.time(),
                                      read_log=lambda s, e: ([], "không đọc được log BE (timeout)"))
        self.assertEqual(reasons, ["HTTP 5xx"])
        self.assertIn("không đọc được log BE", d.af.report_section(text, "Ghi chú"))

    def test_log_check_disabled(self):
        src = qa_md([("POST", "/api/v1/correlation-rules/filter", "HTTP 200\n\n{}")])
        called = []
        d.verify_pass(src, self.dir, NO_LOG, time.time() - 60, time.time(),
                      read_log=lambda s, e: called.append(1) or (["x"], ""))
        self.assertEqual(called, [])

    def test_claude_jsonl(self):
        cmd = "python3 %s --as QA_AUTO_x POST /api/v1/correlation-rules/filter '{\"page\":0}'" % d.SOAR_API
        rows = [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": cmd}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "HTTP 500\n\n{}"}]}]}}]
        with open(os.path.join(self.dir, "claude.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")
        reqs = d.qa_requests_from_claude(os.path.join(self.dir, "claude.jsonl"))
        self.assertEqual([(r["method"], r["target"], r["status"]) for r in reqs],
                         [("POST", "/api/v1/correlation-rules/filter", 500)])
        # Claude tự viết qa.md "ĐẠT" không kèm transcript → vẫn bị hạ nhờ claude.jsonl
        text, reasons = d.verify_pass("QA: ĐẠT\n\n## Kết quả\nok\n\n## Bằng chứng\n-\n", self.dir, NO_LOG)
        self.assertIn("HTTP 5xx", reasons)


class BeLog(unittest.TestCase):
    LOG = """2026-11-11T07:06:59.000000000Z 2026-11-11T14:06:59.000+07:00 ERROR 1 --- [soar] [io-8888-exec-1] c.c.s.Foo : ngoài khoảng
2026-11-11T07:07:01.100000000Z 2026-11-11T14:07:01.100+07:00 ERROR 1 --- [soar] [eduler_Worker-4] c.c.s.s.j.HealthCheckTestConnectionJob   : lỗi không mong đợi:
2026-11-11T07:07:01.100000001Z com.cyai.soar.exception.integrate_source.IntegrateSourceNotFoundException:
--
2026-11-11T07:07:02.000000000Z 2026-11-11T14:07:02.000+07:00  WARN 1 --- [soar] [nature-backfill] c.c.s.s.a.AiEventSignatureBackfill : java.lang.NullPointerException: x
2026-11-11T07:07:03.000000000Z 2026-11-11T14:07:03.000+07:00 ERROR 1 --- [soar] [io-8888-exec-12] o.h.e.j.s.SqlExceptionHelper : ERROR: column cr.case_severity_field does not exist
2026-11-11T07:07:03.000000001Z   Position: 123
2026-11-11T07:07:04.000000000Z 2026-11-11T14:07:04.000+07:00 ERROR 1 --- [soar] [io-8888-exec-13] o.h.e.j.s.SqlExceptionHelper : ERROR: column cr.case_severity_field does not exist
2026-11-11T07:07:05.000000000Z 2026-11-11T14:07:05.000+07:00 ERROR 1 --- [soar] [io-8888-exec-14] c.c.s.a.ExceptionHandlerAdvice : (handle) lỗi
2026-11-11T07:07:05.000000001Z org.springframework.dao.InvalidDataAccessResourceUsageException: could not extract ResultSet
2026-11-11T07:07:05.000000002Z 	at org.hibernate.Foo.bar(Foo.java:1)
2026-11-11T07:07:06.000000000Z 2026-11-11T14:07:06.000+07:00 ERROR 1 --- [soar] [scheduling-1] c.c.s.X : Caused by: org.postgresql.util.PSQLException: ERROR: relation "x" does not exist
"""

    def test_parse(self):
        import calendar
        since = calendar.timegm((2026, 11, 11, 7, 7, 0, 0, 0, 0))
        lines = d.parse_be_log(self.LOG, since, since + 600)
        joined = "\n".join(lines)
        self.assertNotIn("ngoài khoảng", joined)
        self.assertNotIn("HealthCheckTestConnectionJob", joined)  # job nền, không phải luồng request
        self.assertNotIn("NullPointerException", joined)  # WARN
        self.assertEqual(joined.count("case_severity_field does not exist"), 1)  # bỏ trùng
        self.assertIn("InvalidDataAccessResourceUsageException", joined)
        self.assertNotIn("\tat ", joined)
        self.assertIn('relation "x" does not exist', joined)  # lỗi SQL: tính cả luồng nền
        ignored = d.parse_be_log(self.LOG, since, since + 600, {"qa_be_log_ignore": ["ExceptionHandlerAdvice"]})
        self.assertNotIn("InvalidDataAccessResourceUsageException", "\n".join(ignored))

    def test_cap(self):
        import calendar
        since = calendar.timegm((2026, 11, 11, 7, 0, 0, 0, 0, 0))
        log = "\n".join("2026-11-11T07:01:%02d.0Z 2026-11-11T14:01:%02d.0+07:00 ERROR 1 --- [soar] [io-8888-exec-1] "
                        "c.X : lỗi loại %s %s" % (i, i, chr(65 + i), "y" * 500) for i in range(30))
        lines = d.parse_be_log(log, since, since + 600)
        self.assertEqual(len(lines), 21)
        self.assertTrue(lines[-1].startswith("… (còn 10"))
        self.assertTrue(all(len(x) <= 302 for x in lines))

    def test_read_failure_does_not_raise(self):
        orig = d.remote
        d.remote = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sshpass → mã 255: timeout"))
        try:
            lines, note = d.be_log_errors({"be_container": "soar"}, time.time() - 60, time.time())
        finally:
            d.remote = orig
        self.assertEqual(lines, [])
        self.assertIn("không đọc được log BE", note)


if __name__ == "__main__":
    unittest.main()
