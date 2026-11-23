#!/usr/bin/env python3
"""
cpu_guard: giữ CPU ổn định khi nhiều lượt chạy (autofix / QA / gắp nhánh…) cùng build nặng (mvn, npm, tsc…).

Mỗi CPU_GUARD_INTERVAL giây đo % CPU toàn máy (/proc/stat):
- CPU trung bình CPU_GUARD_SUSTAIN giây gần nhất ≥ CPU_GUARD_HIGH → tạm dừng (SIGSTOP cả cây tiến trình) MỘT lượt đang ăn CPU
  nhiều nhất trong các lượt mới bắt đầu nhất (lượt cũ được làm cho xong trước); luôn để ít nhất CPU_GUARD_MIN_RUNNING
  lượt nặng chạy. Đo lại, còn cao thì dừng thêm lượt nữa.
- CPU trung bình CPU_GUARD_RESUME_SUSTAIN giây gần nhất ≤ CPU_GUARD_LOW (lượt khác xong / build xong) → chạy tiếp (SIGCONT)
  lượt dừng lâu nhất, từng lượt một.
- Một lượt không bị dừng quá CPU_GUARD_MAX_PAUSE giây (timeout của Claude / build tính cả thời gian bị dừng): hết hạn
  thì chạy tiếp dù CPU còn cao và được miễn dừng CPU_GUARD_PROTECT giây — lượt khác bị dừng thay.
- Ưu tiên dừng lượt có build chạy thẳng dưới script (verify / test của tool, timeout 30 phút). Build đang chạy trong
  Bash của Claude / agy (timeout lệnh chỉ vài phút, đồng hồ vẫn chạy khi bị dừng) chỉ dừng khi không còn lựa chọn và
  tối đa CPU_GUARD_MAX_PAUSE_AGENT giây.

Không dừng lượt đang push / fetch / ssh (đứt kết nối giữa chừng). Tắt dịch vụ / lỗi / khởi động lại → chạy tiếp mọi
lượt đã dừng (trạng thái lưu ở <root>/.cpu-guard.json để lần chạy sau còn biết mà SIGCONT).

    cpu_guard.py               # chạy nền (systemd: op-cpu-guard.service)
    cpu_guard.py --status      # in trạng thái
    cpu_guard.py --resume-all  # chạy tiếp mọi lượt đã dừng
"""
import argparse
import json
import os
import re
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402

# Tiến trình gốc của một "lượt chạy": script của tool chạy theo từng việc (không tính dịch vụ thường trực).
TASK_SCRIPTS = ("op_autofix.py", "deploy_qa.py", "cherry.py", "pick_resolve.py", "task_create.py", "task_split.py",
                "merge_to_dev.py", "cleanup.py", "ui_check.py")
# Cây có các tiến trình này thì không dừng: dừng giữa chừng dễ đứt kết nối / làm hỏng thao tác với remote.
UNSAFE_RE = re.compile(r"(^|/)(ssh|scp|rsync|sshpass|git-remote-\w+)\b|\bgit\b.*\b(push|fetch|pull|clone)\b|"
                       r"\bdocker\b.*\b(push|pull|save|load)\b")


def settings():
    g = envconf.get
    return {
        "enabled": envconf._bool(g("CPU_GUARD_ENABLED", "1")),
        "interval": max(1.0, float(g("CPU_GUARD_INTERVAL", "5"))),
        "high": float(g("CPU_GUARD_HIGH", "90")),
        "low": float(g("CPU_GUARD_LOW", "65")),
        "sustain": float(g("CPU_GUARD_SUSTAIN", "20")),
        "resume_sustain": float(g("CPU_GUARD_RESUME_SUSTAIN", "15")),
        "max_pause": float(g("CPU_GUARD_MAX_PAUSE", "300")),
        "max_pause_agent": float(g("CPU_GUARD_MAX_PAUSE_AGENT", "90")),
        "protect": float(g("CPU_GUARD_PROTECT", "600")),
        "min_running": max(1, int(g("CPU_GUARD_MIN_RUNNING", "1"))),
        # Lượt "nặng" = cây tiến trình dùng ≥ chừng này % của 1 nhân (dừng lượt đang chờ API không giảm CPU).
        "task_min_cpu": float(g("CPU_GUARD_TASK_MIN_CPU", "50")),
    }


def state_path(root=None):
    return os.path.join(root or envconf.autofix_root(), ".cpu-guard.json")


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---- /proc ---------------------------------------------------------------------------------------------------------
def cpu_times():
    """(busy, total) jiffies toàn máy."""
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    total = sum(vals[:8])  # bỏ guest (đã tính trong user)
    return total - idle, total


def read_procs():
    """{pid: {ppid, state, cpu (jiffies), start, cmd}} của user hiện tại."""
    uid = os.getuid()
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            if os.stat("/proc/" + d).st_uid != uid:
                continue
            with open("/proc/%s/stat" % d) as f:
                rest = f.read().rsplit(")", 1)[1].split()
            with open("/proc/%s/cmdline" % d, "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except (OSError, IndexError):
            continue
        out[int(d)] = {"ppid": int(rest[1]), "state": rest[0], "cpu": int(rest[11]) + int(rest[12]),
                       "start": int(rest[19]), "cmd": cmd}
    return out


def children_map(procs):
    kids = {}
    for pid, p in procs.items():
        kids.setdefault(p["ppid"], []).append(pid)
    return kids


def tree(pid, kids):
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        todo.extend(kids.get(p, []))
    return out


def task_name(cmd):
    """Tên ngắn của lượt từ dòng lệnh: --resume <run> / --wp <id> / tên ticket SOAR-x."""
    m = re.search(r"--resume\s+(\S+)", cmd) or re.search(r"\b([A-Z]+-\d+(?:-\d{4}-\d{4})?)\b", cmd)
    script = next((s for s in TASK_SCRIPTS if s in cmd), "task")
    if m:
        return "%s %s" % (script[:-3], m.group(1))
    m = re.search(r"--wp\s+(\d+)", cmd)
    return "%s #%s" % (script[:-3], m.group(1)) if m else script[:-3]


def find_tasks(procs, kids):
    """Tiến trình gốc của từng lượt: script trong TASK_SCRIPTS mà tổ tiên không phải lượt khác."""
    roots = []
    for pid, p in procs.items():
        if not any(re.search(r"(^|[\s/])%s(\s|$)" % re.escape(s), p["cmd"]) for s in TASK_SCRIPTS):
            continue
        if not re.search(r"python", p["cmd"].split(" ", 1)[0]):
            continue  # sh -c "python3 …": lấy chính tiến trình python
        anc, nested = p["ppid"], False
        while anc in procs and anc > 1:
            if anc in roots or any(s in procs[anc]["cmd"] and "python" in procs[anc]["cmd"].split(" ", 1)[0]
                                   for s in TASK_SCRIPTS):
                nested = True
                break
            anc = procs[anc]["ppid"]
        if not nested:
            roots.append(pid)
    return roots


AGENT_RE = re.compile(r"(^|/)(claude|agy)(\s|$)")


def is_agent(cmd):
    return bool(AGENT_RE.search(cmd.split(" -", 1)[0]))


def signal_tree(pids, sig):
    n = 0
    for p in pids:
        try:
            os.kill(p, sig)
            n += 1
        except OSError:
            pass
    return n


# ---- Trạng thái ----------------------------------------------------------------------------------------------------
def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class Guard:
    def __init__(self, cfg, path):
        self.cfg, self.path = cfg, path
        # pid gốc → {name, since, start (để biết pid còn là tiến trình cũ), pids}
        self.paused = {}
        self.protected = {}  # pid gốc → hết miễn dừng lúc
        self.samples = []  # (lúc đo, % CPU máy)
        self.window_from = 0.0  # chỉ tính mẫu sau lần dừng / chạy tiếp gần nhất (đo lại tác dụng)
        self.last_cpu = None
        self.last_pid_cpu = {}  # pid → (start, jiffies) lần đo trước
        self.last_action = 0.0
        self.usage = 0.0
        self.avg_high = self.avg_low = None
        self.tasks = []

    # Đúng tiến trình cũ (pid có thể đã bị dùng lại sau khi lượt kết thúc).
    def _same(self, procs, pid, start):
        return pid in procs and procs[pid]["start"] == start

    def pause(self, procs, kids, t):
        pids = tree(t["pid"], kids)
        signal_tree(pids, signal.SIGSTOP)  # cha trước: không còn đẻ thêm con trong lúc dừng
        time.sleep(0.2)
        procs2 = read_procs()  # con sinh ra ngay trước khi cha dừng
        extra = [p for p in tree(t["pid"], children_map(procs2)) if p not in pids]
        signal_tree(extra, signal.SIGSTOP)
        self.paused[t["pid"]] = {"name": t["name"], "since": time.time(), "start": t["start"],
                                 "pids": pids + extra, "cpu": round(t["cpu"]), "agent": t["agent"],
                                 "limit": self.cfg["max_pause_agent"] if t["agent"] else self.cfg["max_pause"]}
        self.last_action = time.time()
        log("TẠM DỪNG %s (pid %d, %d tiến trình, đang dùng %d%% 1 nhân%s) — CPU máy trung bình %.0f%%, tối đa %ds" % (
            t["name"], t["pid"], len(pids + extra), t["cpu"], ", build trong Claude/agy" if t["agent"] else "",
            self.avg_high or self.usage, self.paused[t["pid"]]["limit"]))

    def resume(self, pid, reason, procs=None):
        info = self.paused.pop(pid, None)
        if not info:
            return
        procs = procs if procs is not None else read_procs()
        pids = list(info.get("pids") or [])
        if self._same(procs, pid, info.get("start")):
            pids = list(dict.fromkeys(tree(pid, children_map(procs)) + pids))
        # Con trước, cha sau: cha chạy lại thì con đã sẵn sàng.
        signal_tree([p for p in reversed(pids) if p in procs], signal.SIGCONT)
        self.last_action = time.time()
        log("CHẠY TIẾP %s (pid %d) sau %ds — %s" % (info["name"], pid, time.time() - info["since"], reason))

    def resume_all(self, reason):
        procs = read_procs()
        for pid in list(self.paused):
            self.resume(pid, reason, procs)
        self.write()

    def write(self):
        save_state(self.path, {"updated": time.time(), "usage": round(self.usage, 1), "cfg": self.cfg,
                               "avg_high": self.avg_high and round(self.avg_high, 1),
                               "avg_low": self.avg_low and round(self.avg_low, 1),
                               "paused": {str(k): v for k, v in self.paused.items()},
                               "tasks": self.tasks, "pid": os.getpid()})

    def restore(self):
        """Lần chạy trước bị tắt ngang: SIGCONT mọi lượt còn ghi là đã dừng."""
        old = load_state(self.path).get("paused") or {}
        procs = read_procs()
        kids = children_map(procs)
        for k, info in old.items():
            pid = int(k)
            pids = list(info.get("pids") or [])
            if self._same(procs, pid, info.get("start")):
                pids = list(dict.fromkeys(tree(pid, kids) + pids))
            live = [p for p in reversed(pids) if p in procs]
            if live:
                signal_tree(live, signal.SIGCONT)
                log("CHẠY TIẾP %s (pid %d) — còn dừng từ lần chạy trước" % (info.get("name"), pid))

    def tick(self):
        now = time.time()
        busy, total = cpu_times()
        procs = read_procs()
        kids = children_map(procs)
        if self.last_cpu:
            db, dt = busy - self.last_cpu[0], total - self.last_cpu[1]
            self.usage = 100.0 * db / dt if dt > 0 else 0.0
        self.last_cpu = (busy, total)
        hz = os.sysconf("SC_CLK_TCK")
        interval = self.cfg["interval"]

        # Lượt đã dừng mà tiến trình gốc đã chết / bị thay (huỷ từ trang, SIGKILL…) → bỏ khỏi danh sách.
        for pid in list(self.paused):
            if not self._same(procs, pid, self.paused[pid]["start"]):
                info = self.paused.pop(pid)
                signal_tree([p for p in info.get("pids") or [] if p in procs], signal.SIGCONT)
                log("BỎ THEO DÕI %s (pid %d) — tiến trình gốc đã kết thúc" % (info["name"], pid))
        self.protected = {p: t for p, t in self.protected.items() if t > now and p in procs}

        # % của 1 nhân mỗi tiến trình từ lần đo trước (tiến trình mới: tính từ lúc nó bắt đầu).
        delta = {}
        for pid, p in procs.items():
            prev = self.last_pid_cpu.get(pid)
            base = prev[1] if prev and prev[0] == p["start"] else (0 if self.last_pid_cpu else p["cpu"])
            delta[pid] = max(0, p["cpu"] - base)
        self.last_pid_cpu = {pid: (p["start"], p["cpu"]) for pid, p in procs.items()}
        pct = lambda pids: 100.0 * sum(delta.get(p, 0) for p in pids) / hz / interval  # noqa: E731

        tasks = []
        for root in find_tasks(procs, kids):
            pids = tree(root, kids)
            agent_pids = {q for p in pids if p in procs and is_agent(procs[p]["cmd"]) for q in tree(p, kids)}
            unsafe = any(UNSAFE_RE.search(procs[p]["cmd"]) for p in pids if p in procs)
            cpu, agent_cpu = pct(pids), pct(agent_pids)
            tasks.append({"pid": root, "name": task_name(procs[root]["cmd"]), "start": procs[root]["start"],
                          "cpu": cpu, "agent": agent_cpu >= max(cpu - agent_cpu, self.cfg["task_min_cpu"] / 2),
                          "procs": len(pids), "unsafe": unsafe, "paused": root in self.paused})
        self.tasks = [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in t.items()} for t in tasks]

        # Dừng quá lâu → chạy tiếp, miễn dừng một thời gian.
        for pid, info in list(self.paused.items()):
            limit = info.get("limit") or self.cfg["max_pause"]
            if now - info["since"] >= limit:
                self.resume(pid, "đã dừng tối đa %ds" % limit, procs)
                self.protected[pid] = now + self.cfg["protect"]

        if self.last_pid_cpu and self.usage is not None:
            self.samples.append((now, self.usage))
        keep = max(self.cfg["sustain"], self.cfg["resume_sustain"]) + self.cfg["interval"] * 2
        self.samples = [x for x in self.samples if x[0] >= now - keep]

        def avg(window):
            """Trung bình % CPU trong `window` giây gần nhất; chưa đủ dữ liệu cho cả cửa sổ → None."""
            xs = [u for t, u in self.samples if t >= max(now - window, self.window_from)]
            need = max(1, int(window / self.cfg["interval"]))
            return sum(xs) / len(xs) if len(xs) >= need else None

        hi, lo = avg(self.cfg["sustain"]), avg(self.cfg["resume_sustain"])
        self.avg_high, self.avg_low = hi, lo
        cooldown = now - self.last_action >= max(self.cfg["interval"] * 2, 10)
        if self.cfg["enabled"] and hi is not None and hi >= self.cfg["high"] and cooldown:
            heavy = [t for t in tasks if not t["paused"] and t["cpu"] >= self.cfg["task_min_cpu"]]
            cands = [t for t in heavy if not t["unsafe"] and t["pid"] not in self.protected]
            if len(heavy) > self.cfg["min_running"] and cands:
                # Build của script trước build trong Bash của Claude; rồi lượt bắt đầu muộn nhất (lượt cũ gần xong hơn).
                cands.sort(key=lambda t: (not t["agent"], t["start"], t["cpu"]), reverse=True)
                self.pause(procs, kids, cands[0])
                self.window_from = now  # đo lại cả cửa sổ trước khi dừng thêm
        elif self.paused and (not self.cfg["enabled"] or (lo is not None and lo <= self.cfg["low"] and cooldown)):
            first = min(self.paused, key=lambda p: self.paused[p]["since"])
            self.resume(first, "CPU máy trung bình %.0f%%" % lo if self.cfg["enabled"] else "đã tắt cpu_guard", procs)
            self.window_from = now
        for t in self.tasks:
            t["paused"] = t["pid"] in self.paused
        self.write()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--resume-all", action="store_true")
    args = ap.parse_args()
    path = state_path()
    if args.status:
        print(json.dumps(load_state(path), ensure_ascii=False, indent=1))
        return
    g = Guard(settings(), path)
    g.restore()
    if args.resume_all:
        save_state(path, dict(load_state(path), paused={}))
        return

    def stop(signum, _frame):
        g.resume_all("cpu_guard dừng (tín hiệu %d)" % signum)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log("cpu_guard bắt đầu: %s" % json.dumps(g.cfg))
    try:
        while True:
            try:
                g.cfg = settings()
            except ValueError:
                pass
            g.tick()
            time.sleep(g.cfg["interval"])
    finally:
        g.resume_all("cpu_guard thoát")


if __name__ == "__main__":
    main()
