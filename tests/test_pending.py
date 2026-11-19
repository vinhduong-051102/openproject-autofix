import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deploy_qa as d  # noqa: E402


class PendingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = mock.patch.object(d, "PENDING", os.path.join(self.tmp, "qa-pending.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_readded_after_qa_start_is_kept(self):
        # SOAR-1014: QA xong (bắt đầu lúc t0) → autofix QA SAI thêm lại → lượt QA lưu hàng chờ không được xoá
        d.save_pending(["SOAR-911", "SOAR-1014"])
        waiting = d.load_pending()
        t0 = d._now_us()
        d.add_pending(["SOAR-1014"], "QA SAI")
        d.save_merged_pending(waiting, {"SOAR-1014": t0}, [], set())
        self.assertEqual(sorted(d.load_pending()), ["SOAR-1014", "SOAR-911"])

    def test_done_without_readd_is_removed(self):
        d.save_pending(["SOAR-911", "SOAR-1014"])
        waiting = d.load_pending()
        t0 = d._now_us()
        d.save_merged_pending(waiting, {"SOAR-1014": t0}, [], set())
        self.assertEqual(d.load_pending(), ["SOAR-911"])

    def test_readd_by_other_writer_mid_run(self):
        waiting = []
        t0 = d._now_us()
        d.add_pending(["SOAR-3004"])  # QA SAI của ticket đã QA xong trong lượt này
        d.save_merged_pending(waiting, {"SOAR-3004": t0, "SOAR-982": t0}, ["SOAR-1438"], set())
        self.assertEqual(sorted(d.load_pending()), ["SOAR-1438", "SOAR-3004"])

    def test_legacy_set_done_still_drops(self):
        d.save_pending(["A", "B"])
        self.assertEqual(d._merged_pending(["A", "B"], {"A"}, [], set()), {"B"})


if __name__ == "__main__":
    unittest.main()
