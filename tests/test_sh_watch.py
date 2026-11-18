import io
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deploy_qa as d  # noqa: E402


class ShWatchTest(unittest.TestCase):
    def test_ok_streams_log_and_returns_output(self):
        log = io.StringIO()
        out = d.sh_watch(["bash", "-c", "echo a; echo b"], logf=log, idle=5)
        self.assertEqual(out, "a\nb\n")
        self.assertIn("a\nb\n", log.getvalue())

    def test_error_code(self):
        with self.assertRaises(RuntimeError) as cm:
            d.sh_watch(["bash", "-c", "echo boom; exit 3"], idle=5, label="FLOW")
        self.assertNotIsInstance(cm.exception, d.Stalled)
        self.assertIn("FLOW → mã 3", str(cm.exception))

    def test_idle_kills_whole_group(self):
        # tiến trình con (giống docker build dưới bash) cũng phải chết
        log = io.StringIO()
        t0 = time.time()
        with self.assertRaises(d.Stalled) as cm:
            d.sh_watch(["bash", "-c", "echo bat-dau; sleep 60 & wait"], logf=log, idle=1.5, grace=1, label="FLOW")
        self.assertLess(time.time() - t0, 15)
        self.assertIn("FLOW treo", str(cm.exception))
        self.assertIn("dòng cuối: bat-dau", str(cm.exception))
        self.assertIn("### FLOW treo", log.getvalue())

    def test_term_ignored_then_sigkill(self):
        t0 = time.time()
        with self.assertRaises(d.Stalled):
            d.sh_watch(["bash", "-c", "trap '' TERM; echo x; sleep 60"], idle=1, grace=1)
        self.assertLess(time.time() - t0, 15)

    def test_steady_output_not_killed(self):
        out = d.sh_watch(["bash", "-c", "for i in 1 2 3 4; do echo $i; sleep 0.6; done"], idle=1.5)
        self.assertEqual(out.split(), ["1", "2", "3", "4"])

    def test_total_timeout(self):
        with self.assertRaises(d.Stalled) as cm:
            d.sh_watch(["bash", "-c", "while true; do echo .; sleep 0.2; done"], idle=10, timeout=1.5, grace=1)
        self.assertIn("quá", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
