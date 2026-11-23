import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cpu_guard as cg  # noqa: E402

BUSY = "import os,subprocess,sys,time\n" \
       "subprocess.Popen([sys.executable,'-c','while True: pass'])\n" \
       "time.sleep(3600)\n"


def state(pid):
    with open("/proc/%d/stat" % pid) as f:
        return f.read().rsplit(")", 1)[1].split()[0]


class CpuGuardTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        script = os.path.join(self.dir, "op_autofix.py")
        with open(script, "w") as f:
            f.write(BUSY)
        self.procs = []
        for i in range(2):
            self.procs.append(subprocess.Popen([sys.executable, script, "--resume", "SOAR-%d-1123-0000" % (9000 + i)]))
            time.sleep(0.3)
        self.addCleanup(self.kill)
        cfg = dict(cg.settings(), enabled=True, interval=1, high=0, low=-1, sustain=1, resume_sustain=1,
                   max_pause=60, min_running=1, task_min_cpu=30)
        self.g = cg.Guard(cfg, os.path.join(self.dir, "state.json"))
        self.g.last_action = 0
        # Chỉ thấy lượt của test (không đụng lượt autofix thật đang chạy trên máy).
        real = cg.find_tasks
        own = {p.pid for p in self.procs}
        patcher = mock.patch.object(cg, "find_tasks", lambda procs, kids: [r for r in real(procs, kids) if r in own])
        patcher.start()
        self.addCleanup(patcher.stop)

    def kill(self):
        for p in self.procs:
            for c in cg.tree(p.pid, cg.children_map(cg.read_procs())):
                try:
                    os.kill(c, 9)
                except OSError:
                    pass
            p.wait()

    def mine(self):
        pids = {p.pid for p in self.procs}
        return [t for t in self.g.tasks if t["pid"] in pids]

    def tick(self, n=1):
        for _ in range(n):
            self.g.tick()
            time.sleep(1)

    def test_pause_newest_then_resume(self):
        self.tick(3)
        self.assertEqual(len(self.mine()), 2)
        newest = self.procs[1].pid
        self.assertIn(newest, self.g.paused)
        self.assertNotIn(self.procs[0].pid, self.g.paused)  # luôn chừa ít nhất 1 lượt nặng chạy
        kid = [p for p in cg.tree(newest, cg.children_map(cg.read_procs())) if p != newest][0]
        self.assertEqual(state(kid), "T")
        # CPU rảnh → chạy tiếp
        self.g.cfg.update(high=1000, low=1000)
        self.g.last_action = 0
        self.tick(2)
        self.assertEqual(self.g.paused, {})
        self.assertNotEqual(state(kid), "T")

    def test_max_pause_and_restore(self):
        self.tick(3)
        pid = self.procs[1].pid
        self.assertIn(pid, self.g.paused)
        # Guard mới (lần chạy trước bị tắt ngang) → restore SIGCONT
        g2 = cg.Guard(self.g.cfg, self.g.path)
        g2.restore()
        kid = [p for p in cg.tree(pid, cg.children_map(cg.read_procs())) if p != pid][0]
        self.assertNotEqual(state(kid), "T")
        # hết max_pause → chạy tiếp và được miễn dừng
        self.g.paused[pid]["since"] -= 1000
        self.tick(1)
        self.assertNotIn(pid, self.g.paused)
        self.assertIn(pid, self.g.protected)


if __name__ == "__main__":
    unittest.main()
