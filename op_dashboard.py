#!/usr/bin/env python3
"""
op-dashboard: trang web trên máy để theo dõi op_autofix và chọn gói Claude.

  python3 op_dashboard.py            # mở http://127.0.0.1:$DASHBOARD_PORT (mặc định 8765, đặt trong .env)
  python3 op_dashboard.py --port 9000

Trang chỉ nghe trên 127.0.0.1. Đọc trạng thái từ ~/autofix/.status, thao tác Claude từ claude.jsonl của từng lượt,
hạn mức từ rate_limit_event. Chọn gói / bật tắt tự chuyển gói ghi vào ~/autofix/.profile.json; op_autofix đọc file
này ở lượt chạy tiếp theo (lượt đang chạy giữ gói đã chọn, trừ khi hết hạn mức giữa chừng).
"""

import argparse
import calendar
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402
import op_autofix as af  # noqa: E402
import op_watch  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ACTIVE_STATES = ("queued", "preparing", "running", "shipping")
# File trong thư mục lượt chạy được xem trên trang (tên hiển thị → file).
RUN_FILES = (("Báo cáo", "report.md"), ("Gemini", "gemini.md"), ("Claude xem lại", "followup.md"),
             ("Build/test", "verify.log"), ("QA", "qa-server.md"), ("Prompt", "prompt.md"), ("Lỗi Claude", "claude.err"))
CONFIG_ARGS = {"config": None}


def load():
    cfg = op_watch.load_config(argparse.Namespace(config=CONFIG_ARGS["config"], interval=None, on_change=None,
                                                  no_notify=False))
    acfg = dict(af.AUTOFIX_DEFAULTS)
    acfg.update(cfg.get("autofix") or {})
    return cfg, acfg, os.path.expanduser(acfg["root"])


def inbox_wp_ids(cfg):
    """SOAR-xxxx → id work package, lấy từ link trong inbox (lượt chạy cũ không ghi wp_id)."""
    out = {}
    path = os.path.expanduser(cfg.get("inbox") or "")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            for m in re.finditer(r"(SOAR-\d+) https?://\S+/work_packages/(\d+)", f.read()):
                out[m.group(1)] = m.group(2)
    return out


def inbox_tail(cfg, n=30):
    path = os.path.expanduser(cfg.get("inbox") or "")
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [line.rstrip("\n") for line in f if line.startswith("- [")]
    return [re.sub(r"^- \[[ x]\] ", "", line) for line in lines[-n:]][::-1]


def watcher_running():
    res = subprocess.run(["pgrep", "-f", "op_watch.py"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return res.returncode == 0


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def window(info, key):
    w = ((info or {}).get("unifiedWindows") or {}).get(key) or {}
    return {"used": af.window_used(info, key), "resetsAt": w.get("resetsAt")} if w else None


def state():
    cfg, acfg, root = load()
    os.makedirs(root, exist_ok=True)
    prefs = af.profile_prefs(root)
    order, order_mode = af.profile_order(root, acfg)
    nxt, _ = af.choose_profile(root, acfg)
    active = nxt or (order[0] if order else {"name": None})
    profs = []
    for p in order:
        info, mtime = af.profile_usage(root, acfg, p["name"])
        profs.append({
            "name": p["name"], "config_dir": p["config_dir"], "logged_in": p["logged_in"], "kind": p["kind"],
            "token_tail": af.read_token(p.get("token_file"))[-4:] if p["kind"] == "token" else "",
            "active": p["name"] == active["name"], "block": af.profile_block(root, acfg, p),
            "remaining": af.profile_remaining(root, acfg, p),
            "five_hour": window(info, "five_hour"), "seven_day": window(info, "seven_day"),
            "status": (info or {}).get("status"), "checked_at": mtime or None,
            # Ngưỡng đang áp dụng cho gói + các ngưỡng gói tự đặt (phần còn lại theo ngưỡng chung).
            "limits": af.profile_limits(acfg, p), "own_limits": p.get("limits") or {},
        })
    wp_ids = inbox_wp_ids(cfg)
    runs = []
    for s in af.load_statuses(root):
        st = s.get("state")
        # Tiến trình đã chết mà trạng thái vẫn "đang chạy" (máy tắt giữa chừng…) thì báo rõ.
        if st in ACTIVE_STATES and s.get("pid") and not pid_alive(s["pid"]):
            st = "stale"
        run_dir = s.get("run_dir") or ""
        runs.append(dict(s, state=st, wp_id=s.get("wp_id") or wp_ids.get(s["display_id"]),
                         files=[label for label, name in RUN_FILES
                                if run_dir and os.path.exists(os.path.join(run_dir, name))]))
    active_runs = [r for r in runs if r["state"] in ACTIVE_STATES]
    others = sorted([r for r in runs if r["state"] not in ACTIVE_STATES], key=lambda r: r.get("updated_at") or "",
                    reverse=True)
    return {
        "now": time.time(), "watcher": watcher_running(), "op_url": cfg["url"],
        "auto_switch": prefs.get("auto_switch", acfg.get("auto_switch", True)), "order_mode": order_mode,
        "limits": {"five_hour": acfg.get("pause_above_utilization"), "seven_day": acfg.get("switch_above_7d"),
                   "followup_7d": acfg.get("followup_skip_above_7d")},
        "profiles": profs, "runs": active_runs + others[:40], "inbox": inbox_tail(cfg),
        "pending": af.load_pending(root),
        # Lý do hàng chờ chưa chạy được (rỗng = đã có gói còn hạn mức).
        "pending_ready": followup_block(root, acfg),
        **jobs(),
        "gemini": gemini_usage(), "parallel": parallel_settings(acfg), "review_queue": review_queue(),
        "running_now": sum(1 for r in runs if r["state"] in ("preparing", "running", "shipping")),
    }


# ---- Các job nền (systemd user) -------------------------------------------------------------------------------
def _job_texts():
    """Mô tả job lấy theo cấu hình (nhánh, lịch, thư mục báo cáo) để trang đúng với máy đang chạy."""
    a = dict(af.AUTOFIX_DEFAULTS)
    a.update(envconf._read_json(envconf.CONFIG_PATH).get("autofix") or {})
    envconf.overlay(a, envconf.AUTOFIX_ENV)
    merge_at = envconf.get("MERGE_SCHEDULE", "*-*-* 12:00:00;*-*-* 18:00:00").replace(";", ", ")
    report_at = envconf.get("REPORT_SCHEDULE", "Mon..Fri *-*-* 08:00:00")
    return {
        "merge": "Theo lịch %s: PR %s → %s, %s → %s, rồi deploy server test và QA ticket (lỡ giờ vì tắt máy thì chạy bù "
                 "khi bật máy)" % (merge_at, a["fe_branch"], a["fe_upstream"], a["be_branch"], a["be_upstream"]),
        "report": "Theo lịch %s: tạo %s/bao-cao-DD-MM-YYYY.txt (lỡ giờ thì chạy khi bật máy)" % (
            report_at, envconf.get("DAILY_REPORT_DIR", "~/Downloads/bao-cao-ngay")),
        "dashboard": "Trang này (http://127.0.0.1:%d)" % envconf.DASHBOARD_PORT,
    }


_TEXTS = _job_texts()
JOBS = (
    {"key": "watch", "name": "op-watch", "unit": "op-watch.service",
     "desc": "Theo dõi OpenProject theo chu kỳ → autofix ticket New/Reopened; định kỳ chạy hàng chờ Claude xem lại",
     "inbox": r"\*\*(AUTOFIX[^*]*|GEMINI TEST|TEST LẠI|CLAUDE REVIEW|NEW|REOPENED|COMMENT)\*\*"},
    {"key": "merge", "name": "Merge + deploy + QA", "unit": "op-merge.service", "timer": "op-merge.timer",
     "desc": _TEXTS["merge"],
     "inbox": r"\*\*(MERGE DEV[^*]*|DEPLOY 86[^*]*|QA SERVER[^*]*)\*\*"},
    {"key": "report", "name": "Báo cáo ngày", "unit": "op-report.service", "timer": "op-report.timer",
     "desc": _TEXTS["report"],
     "inbox": r"\*\*BÁO CÁO NGÀY\*\*"},
    {"key": "dashboard", "name": "Dashboard", "unit": "op-dashboard.service",
     "desc": _TEXTS["dashboard"], "inbox": None},
)
_JOB_CACHE = {"at": 0, "data": None}


def systemctl_show(unit, props):
    res = subprocess.run(["systemctl", "--user", "show", unit, "-p", ",".join(props)], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, universal_newlines=True)
    out = {}
    for line in res.stdout.splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


def inbox_lines(cfg, pattern, n=6):
    path = os.path.expanduser(cfg.get("inbox") or "")
    if not pattern or not path or not os.path.exists(path):
        return []
    rx = re.compile(pattern)
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [re.sub(r"^- \[[ x]\] ", "", ln.rstrip("\n")) for ln in f if ln.startswith("- [") and rx.search(ln)]
    return lines[-n:][::-1]


def conflict_runs(root, n=8):
    out = []
    for d in sorted(glob.glob(os.path.join(root, "conflict-*")), reverse=True)[:n]:
        first = ""
        rep = os.path.join(d, "report.md")
        if os.path.exists(rep):
            with open(rep, encoding="utf-8", errors="replace") as f:
                first = f.readline().strip()
        out.append({"name": os.path.basename(d), "verdict": first or "chưa có báo cáo",
                    "at": os.path.getmtime(d)})
    return out


def qa_runs(root, cfg, merge_running, n=15):
    """Các lượt QA trên server 86 (thư mục <SOAR>-qa-<MMDD-HHMM>), mới nhất trước."""
    final = {}
    path = os.path.expanduser(cfg.get("inbox") or "")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                m = re.search(r"\*\*QA SERVER\*\* (SOAR-\d+) — (.*?) · (\S+/qa\.md)", ln)
                if m:
                    final[os.path.dirname(m.group(3))] = m.group(2)
    dirs = sorted(glob.glob(os.path.join(root, "SOAR-*-qa-*")), key=os.path.getmtime, reverse=True)[:n]
    out = []
    for i, d in enumerate(dirs):
        name = os.path.basename(d)
        qa_md = os.path.join(d, "qa.md")
        verdict, state = final.get(d), "done"
        if not verdict and os.path.exists(qa_md):
            with open(qa_md, encoding="utf-8", errors="replace") as f:
                m = re.search(r"QA:\s*(KHÔNG RÕ YÊU CẦU|KHÔNG ĐẠT|ĐẠT|KHÔNG TỰ TEST ĐƯỢC)", f.read(300))
            verdict = m.group(1) if m else "không đọc được kết luận"
        if not verdict:
            # Chưa có qa.md: lượt mới nhất khi job merge còn chạy là đang test, còn lại là bị ngắt giữa chừng.
            state = "running" if (merge_running and i == 0) else "stale"
            verdict = "đang test" if state == "running" else "bị ngắt, chưa có kết luận"
        rounds = 0
        rp = os.path.join(d, "gemini-rounds.jsonl")
        if os.path.exists(rp):
            with open(rp, encoding="utf-8", errors="replace") as f:
                rounds = sum(1 for _ in f)
        out.append({"name": name, "display_id": name.split("-qa-")[0], "at": os.path.getmtime(d), "state": state,
                    "verdict": verdict, "rounds": rounds, "has_report": os.path.exists(qa_md)})
    return out


def merge_runs(cfg, merge_running, n=12):
    """Các lượt merge + deploy (dòng MERGE DEV trong inbox, kèm các dòng DEPLOY 86 sau nó)."""
    path = os.path.expanduser(cfg.get("inbox") or "")
    runs = []
    if path and os.path.exists(path):
        rx = re.compile(r"^- \[[ x]\] (\d{4}-\d\d-\d\d \d\d:\d\d) \*\*(MERGE DEV[^*]*|DEPLOY 86[^*]*)\*\* (.*)$")
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                m = rx.match(ln.rstrip("\n"))
                if not m:
                    continue
                at = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M"))
                kind, text = m.group(2), m.group(3)
                if kind.startswith("MERGE DEV") or not runs:
                    runs.append({"at": at, "merge": "", "deploy": [], "ok": True})
                r = runs[-1]
                if kind.startswith("MERGE DEV"):
                    r["merge"] = text
                else:
                    r["deploy"].append(text)
                if "LỖI" in kind:
                    r["ok"] = False
    for i, r in enumerate(runs):
        r["key"] = "m%d" % int(r["at"])
        r["state"] = "done" if r["ok"] else "error"
    runs = runs[::-1][:n]
    if merge_running:
        # Job đang chạy đã ghi dòng MERGE DEV (sau bước merge) thì đánh dấu lượt đó, chưa ghi thì thêm một lượt mới.
        if runs and runs[0]["at"] > time.time() - 3 * 3600:
            runs[0]["state"] = "running"
        else:
            runs.insert(0, {"key": "running", "at": time.time(), "merge": "đang merge", "deploy": [], "state": "running"})
    return runs


def qa_report(name):
    _, _, root = load()
    if not re.match(r"^SOAR-\d+-qa-[\d-]+$", name or ""):
        return ""
    d = os.path.join(root, name)
    path = os.path.join(d, "qa.md")
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()[:200000]
    rp = os.path.join(d, "gemini-rounds.jsonl")
    if os.path.exists(rp):
        with open(rp, encoding="utf-8", errors="replace") as f:
            return "Chưa có qa.md — các vòng Gemini đến giờ:\n\n```json\n" + f.read()[-60000:] + "\n```"
    return "Chưa có dữ liệu"


def jobs():
    if time.time() - _JOB_CACHE["at"] < 8 and _JOB_CACHE["data"]:
        return _JOB_CACHE["data"]
    cfg, acfg, root = load()
    out = []
    for j in JOBS:
        sv = systemctl_show(j["unit"], ["ActiveState", "SubState", "Result", "ExecMainStartTimestamp",
                                        "ExecMainExitTimestamp", "ExecMainStatus", "UnitFileState", "LoadState"])
        item = {"key": j["key"], "name": j["name"], "unit": j["unit"], "desc": j["desc"],
                "loaded": sv.get("LoadState") == "loaded", "active": sv.get("ActiveState"), "sub": sv.get("SubState"),
                "result": sv.get("Result"), "started": sv.get("ExecMainStartTimestamp"),
                "exited": sv.get("ExecMainExitTimestamp"), "enabled": sv.get("UnitFileState"),
                "recent": inbox_lines(cfg, j["inbox"])}
        if j.get("timer"):
            tv = systemctl_show(j["timer"], ["ActiveState", "NextElapseUSecRealtime", "LastTriggerUSec",
                                             "UnitFileState"])
            if j["key"] == "merge":
                item["schedule"] = sc = merge_schedule(load()[2])
                if sc["times"]:
                    item["desc"] = re.sub(r"^Theo lịch .*?: PR ", "Theo lịch %s%s: PR " % (
                        ", ".join(sc["times"]), " thứ 2 – thứ 6" if sc["days"] == "weekdays" else " hằng ngày"), j["desc"])
            item.update(timer=j["timer"], timer_active=tv.get("ActiveState"), timer_enabled=tv.get("UnitFileState"),
                        next=tv.get("NextElapseUSecRealtime"), last_trigger=tv.get("LastTriggerUSec"))
        out.append(item)
    qa = {}
    try:
        with open(os.path.join(root, "qa-pending.json"), encoding="utf-8") as f:
            qa = json.load(f)
    except (OSError, ValueError):
        pass
    merge_running = any(j["key"] == "merge" and j["active"] == "activating" for j in out)
    data = {"jobs": out, "qa_pending": qa, "conflicts": conflict_runs(root),
            "qa_runs": qa_runs(root, cfg, merge_running), "merge_runs": merge_runs(cfg, merge_running)}
    _JOB_CACHE.update(at=time.time(), data=data)
    return data


def job_log(key, n=150):
    j = next((j for j in JOBS if j["key"] == key), None)
    if not j:
        return ""
    res = subprocess.run(["journalctl", "--user", "-u", j["unit"], "-n", str(n), "--no-pager", "-o", "short-iso"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    return res.stdout


def conflict_report(name):
    _, _, root = load()
    if not re.match(r"^conflict-[\w-]+$", name or ""):
        return ""
    path = os.path.join(root, name, "report.md")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()[:200000]


# ---- Hạn mức Gemini (agy /usage, không tốn token) + số agent song song ------------------------------------------------
DEPLOY_CONFIG = envconf.DEPLOY_CONFIG
_GEMINI = {"at": 0, "data": None, "error": "", "busy": False}
GEMINI_TTL = 300


def _probe_gemini():
    _, acfg, root = load()
    cmd = [os.path.expanduser(acfg.get("gemini_cmd") or "~/.local/bin/agy"), "-p", "/usage", "--output-format", "json"]
    try:
        res = subprocess.run(cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             universal_newlines=True, timeout=90)
        groups = json.loads(res.stdout)["command"]["data"]["groups"]
        out = []
        for g in groups:
            item = {"name": g.get("name"), "desc": g.get("description", "")}
            for b in g.get("buckets", []):
                key = "five_hour" if b.get("window") == "5h" else "seven_day" if b.get("window") == "weekly" else None
                if not key:
                    continue
                reset = b.get("reset_time")
                try:
                    reset_ts = calendar.timegm(time.strptime(reset, "%Y-%m-%dT%H:%M:%SZ")) if reset else None
                except ValueError:
                    reset_ts = None
                item[key] = {"used": round(1 - float(b.get("remaining_fraction", 1)), 4), "resetsAt": reset_ts}
            out.append(item)
        _GEMINI.update(at=time.time(), data=out, error="")
        os.makedirs(os.path.join(root, ".usage"), exist_ok=True)
        with open(os.path.join(root, ".usage", "gemini.json"), "w", encoding="utf-8") as f:
            json.dump({"at": _GEMINI["at"], "groups": out}, f, ensure_ascii=False)
    except Exception as ex:  # noqa: BLE001
        _GEMINI.update(at=time.time(), error="không đọc được hạn mức Gemini: %s" % str(ex)[:150])
    finally:
        _GEMINI["busy"] = False


_REVIEWQ = {"at": 0, "data": None, "busy": False, "error": ""}
REVIEWQ_TTL = 60


def _load_review_queue():
    try:
        import deploy_qa
        _REVIEWQ.update(data=deploy_qa.review_queue(), error="")
    except Exception as ex:  # noqa: BLE001
        _REVIEWQ.update(error="không đọc được ticket chờ QA: %s" % str(ex)[:150])
    finally:
        _REVIEWQ.update(at=time.time(), busy=False)


def review_queue(force=False):
    """Ticket Review / hàng chờ đang chờ QA trên server (cache 60 giây, đọc nền: cần OpenProject + ssh server)."""
    if (force or time.time() - _REVIEWQ["at"] > REVIEWQ_TTL) and not _REVIEWQ["busy"]:
        _REVIEWQ["busy"] = True
        threading.Thread(target=_load_review_queue, daemon=True).start()
    d = _REVIEWQ["data"] or {}
    return {"items": d.get("items", []), "build": d.get("build"), "server_up": d.get("server_up"),
            "checked_at": d.get("at"), "error": _REVIEWQ["error"], "busy": _REVIEWQ["busy"],
            "running": os.path.exists(os.path.join(load()[2], ".qa-active"))}


def gemini_usage(force=False):
    """Hạn mức Gemini (cache 5 phút, đọc nền để không chặn trang)."""
    if _GEMINI["data"] is None and not _GEMINI["at"]:
        try:
            _, _, root = load()
            with open(os.path.join(root, ".usage", "gemini.json"), encoding="utf-8") as f:
                saved = json.load(f)
            _GEMINI.update(data=saved["groups"], at=saved["at"])
        except (OSError, ValueError, KeyError):
            pass
    if (force or time.time() - _GEMINI["at"] > GEMINI_TTL) and not _GEMINI["busy"]:
        _GEMINI["busy"] = True
        threading.Thread(target=_probe_gemini, daemon=True).start()
    return {"groups": _GEMINI["data"] or [], "checked_at": _GEMINI["at"] or None, "error": _GEMINI["error"],
            "busy": _GEMINI["busy"]}


def parallel_settings(acfg):
    qa = envconf.deploy_config().get("qa_parallel", 3)
    return {"autofix": int(acfg.get("max_parallel") or 1), "qa": int(qa or 1)}


def save_qa_parallel(n):
    data = envconf._read_json(DEPLOY_CONFIG)
    os.makedirs(os.path.dirname(DEPLOY_CONFIG), exist_ok=True)
    data["qa_parallel"] = n
    with open(DEPLOY_CONFIG + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(DEPLOY_CONFIG + ".tmp", 0o600)
    os.replace(DEPLOY_CONFIG + ".tmp", DEPLOY_CONFIG)


def followup_block(root, acfg):
    if not af.load_pending(root):
        return ""
    prof, why = af.choose_profile(root, af.followup_limits(acfg))
    return "" if prof else why


def find_run(root, display_id, run_name=None):
    for s in af.load_statuses(root):
        if s["display_id"] == display_id:
            if run_name and s.get("run_name") != run_name:
                # Lượt cũ hơn của cùng ticket: thư mục vẫn còn trong ~/autofix.
                path = os.path.join(root, run_name)
                return dict(s, run_dir=path, run_name=run_name) if os.path.isdir(path) else None
            return s
    return None


def run_events(display_id, start):
    _, _, root = load()
    s = find_run(root, display_id)
    if not s or not s.get("run_dir"):
        return {"lines": [], "next": 0, "diff": {}}
    events = af.read_events(os.path.join(s["run_dir"], "claude.jsonl"))
    lines = []
    for e in events:
        for text in af.describe(e):
            lines.append(text)
    diff = {}
    if s.get("state") in ACTIVE_STATES:
        for sub in ("fe", "be"):
            path = os.path.join(s["run_dir"], sub)
            if os.path.isdir(path):
                res = subprocess.run(["git", "diff", "--shortstat", "HEAD"], cwd=path, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, universal_newlines=True)
                diff[sub.upper()] = res.stdout.strip() or "chưa sửa file"
    tools = sum(1 for line in lines if not line.startswith(("💬", "■")))
    return {"lines": lines[start:], "next": len(lines), "tools": tools, "diff": diff}


def run_file(display_id, label):
    _, _, root = load()
    s = find_run(root, display_id)
    name = dict(RUN_FILES).get(label)
    if not s or not name or not s.get("run_dir"):
        return ""
    path = os.path.join(s["run_dir"], name)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    return text[-60000:] if name.endswith(".log") else text[:200000]


def spawn(args, tag):
    _, _, root = load()
    log_path = os.path.join(root, ".dashboard-%s.log" % tag)
    with open(log_path, "a", encoding="utf-8") as out:
        out.write("\n[%s] %s\n" % (datetime.now().isoformat(timespec="seconds"), " ".join(args)))
        out.flush()
        subprocess.Popen([sys.executable, os.path.join(HERE, "op_autofix.py")] + args, cwd=HERE, stdout=out,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    return log_path


TOKEN_DIR = envconf.TOKEN_DIR


def write_token(name, token):
    """Token `claude setup-token` → file riêng quyền 600 (không ghi vào config, không trả về trang)."""
    token = token.strip()
    if not re.match(r"^sk-ant-[A-Za-z0-9_-]{20,}$", token):
        raise ValueError("token không đúng dạng (sk-ant-oat01-…) — lấy bằng: claude setup-token")
    os.makedirs(TOKEN_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(TOKEN_DIR, re.sub(r"[^A-Za-z0-9_-]+", "-", unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "D")).encode("ascii", "ignore").decode()).strip("-").lower() or "goi")
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.replace(path + ".tmp", path)
    return path


def profile_entries(acfg, drop=None):
    """Danh sách gói để ghi lại config (giữ đường dẫn gốc, bỏ các trường tính toán)."""
    out = []
    for p in (acfg.get("profiles") or af.AUTOFIX_DEFAULTS["profiles"]):
        if p["name"] != drop:
            out.append({k: v for k, v in p.items() if k in ("name", "config_dir", "token_file", "limits")})
    return out


def save_profiles(new_list):
    save_autofix(profiles=new_list)


# Ngưỡng hạn mức chỉnh được trên trang: khoá trong config autofix ← tên trường gửi từ trang (phần trăm).
LIMIT_KEYS = (("pause_above_utilization", "five_hour"), ("switch_above_7d", "seven_day"),
              ("followup_skip_above_7d", "followup_7d"))


def save_autofix(**fields):
    """Ghi các khoá autofix vào config op-watch (giữ nguyên các khoá khác, quyền 600)."""
    path = CONFIG_ARGS["config"] or op_watch.CONFIG_PATH
    data = envconf._read_json(path)  # chưa có config.json (chỉ dùng .env) thì tạo mới
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data.setdefault("autofix", {}).update(fields)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(path + ".tmp", 0o600)
    os.replace(path + ".tmp", path)


MERGE_DROPIN = os.path.expanduser("~/.config/systemd/user/op-merge.timer.d/schedule.conf")
MERGE_STAMP = os.path.expanduser("~/.local/share/systemd/timers/stamp-op-merge.timer")
DAYS = {"daily": "", "weekdays": "Mon..Fri "}


def merge_schedule(root):
    """Lịch merge + deploy hiện hành (đọc từ systemd: unit + drop-in) và trạng thái hoãn."""
    out = subprocess.run(["systemctl", "--user", "cat", "op-merge.timer"], capture_output=True, text=True).stdout
    cal = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("OnCalendar="):
            v = line.split("=", 1)[1].strip()
            if not v:
                cal = []  # OnCalendar= rỗng trong drop-in = xoá các lịch trước
            else:
                cal.append(v)
    times, days = [], "daily"
    for v in cal:
        m = re.match(r"^(Mon\.\.Fri\s+)?\*-\*-\*\s+(\d{1,2}):(\d{2})(?::\d{2})?$", v)
        if m:
            times.append("%02d:%s" % (int(m.group(2)), m.group(3)))
            if m.group(1):
                days = "weekdays"
    skip = ""
    try:
        with open(os.path.join(root, ".merge-skip.json"), encoding="utf-8") as f:
            skip = json.load(f).get("until") or ""
    except (OSError, ValueError):
        pass
    if skip and skip < time.strftime("%Y-%m-%d"):
        skip = ""
    return {"times": sorted(times), "days": days, "raw": cal, "skip_until": skip}


def set_merge_schedule(times, days):
    os.makedirs(os.path.dirname(MERGE_DROPIN), exist_ok=True)
    body = "[Timer]\nOnCalendar=\n" + "".join("OnCalendar=%s*-*-* %s:00\n" % (DAYS[days], t) for t in times)
    with open(MERGE_DROPIN + ".tmp", "w", encoding="utf-8") as f:
        f.write("# Ghi bởi trang theo dõi (op_dashboard) — lịch merge + deploy + QA\n" + body)
    os.replace(MERGE_DROPIN + ".tmp", MERGE_DROPIN)
    # Persistent=true: đổi lịch sang giờ đã qua trong hôm nay thì systemd sẽ "chạy bù" ngay. Đặt mốc lần chạy trước = bây
    # giờ để lịch mới chỉ áp cho các giờ sắp tới.
    if os.path.isdir(os.path.dirname(MERGE_STAMP)):
        open(MERGE_STAMP, "a").close()
        os.utime(MERGE_STAMP, None)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "restart", "op-merge.timer"], check=True)
    _JOB_CACHE["at"] = 0


def action(body):
    cfg, acfg, root = load()
    kind = body.get("action")
    names = [p["name"] for p in af.profiles(acfg)]
    if kind == "set_parallel":
        msgs = []
        for key, lo, hi in (("autofix", 1, 10), ("qa", 1, 10)):
            if body.get(key) in (None, ""):
                continue
            n = int(body[key])
            if not lo <= n <= hi:
                raise ValueError("số agent phải từ %d đến %d" % (lo, hi))
            if key == "autofix":
                save_autofix(max_parallel=n)
                msgs.append("autofix %d" % n)
            else:
                save_qa_parallel(n)
                msgs.append("QA server %d" % n)
        if not msgs:
            raise ValueError("chưa nhập số agent")
        return "Đã lưu số agent song song: " + ", ".join(msgs) + " (áp dụng cho lượt chạy mới)"
    if kind == "merge_schedule":
        times = []
        for t in body.get("times") or []:
            m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(t))
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                raise ValueError("giờ không hợp lệ: %s (dạng HH:MM)" % t)
            times.append("%02d:%s" % (int(m.group(1)), m.group(2)))
        times = sorted(set(times))
        if not 1 <= len(times) <= 8:
            raise ValueError("cần từ 1 đến 8 giờ chạy")
        days = body.get("days") if body.get("days") in DAYS else "daily"
        set_merge_schedule(times, days)
        return "Lịch merge + deploy: %s%s" % (", ".join(times), " (thứ 2 – thứ 6)" if days == "weekdays" else " hằng ngày")
    if kind == "merge_skip":
        path = os.path.join(root, ".merge-skip.json")
        until = (body.get("until") or "").strip()
        if not until:
            if os.path.exists(path):
                os.remove(path)
            _JOB_CACHE["at"] = 0
            return "Đã bỏ hoãn — chạy lại theo lịch"
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", until) or until < time.strftime("%Y-%m-%d"):
            raise ValueError("ngày hoãn phải từ hôm nay trở đi (YYYY-MM-DD)")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"until": until, "set_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
        _JOB_CACHE["at"] = 0
        d = time.strptime(until, "%Y-%m-%d")
        return "Hoãn các lượt merge + deploy tự động tới hết %s (bấm Chạy ngay vẫn chạy)" % time.strftime("%d/%m", d)
    if kind == "review_qa_now":
        subprocess.Popen([sys.executable, os.path.join(HERE, "deploy_qa.py"), "--review-watch"], cwd=HERE,
                         stdout=open(os.path.join(root, ".review-watch.log"), "a"), stderr=subprocess.STDOUT,
                         start_new_session=True)
        _REVIEWQ["at"] = 0
        return "Đang QA các ticket Review chưa QA trên bản đang chạy (bỏ qua nếu đang có merge / QA khác)"
    if kind == "probe_gemini":
        gemini_usage(force=True)
        return "Đang cập nhật hạn mức Gemini…"
    if kind == "job_start":
        j = next((j for j in JOBS if j["key"] == body.get("key")), None)
        if not j or j["key"] == "dashboard":
            raise ValueError("không chạy được job này từ trang")
        verb = "restart" if j["key"] == "watch" else "start"
        if j["key"] == "report":
            # Chạy tay từ trang = tạo lại báo cáo hôm nay (service thường bỏ qua khi đã có).
            subprocess.Popen([sys.executable, os.path.join(HERE, "daily_report.py"), "--force"], cwd=HERE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            _JOB_CACHE["at"] = 0
            return "Đang tạo lại báo cáo hôm nay"
        if j["key"] == "merge":
            open(os.path.join(root, ".merge-force"), "w").close()  # "Chạy ngay" chạy cả khi đang hoãn
        subprocess.run(["systemctl", "--user", verb, "--no-block", j["unit"]], check=True)
        _JOB_CACHE["at"] = 0
        return "Đã %s %s" % ("khởi động lại" if verb == "restart" else "chạy", j["name"])
    if kind in ("use_profile", "move_profile"):
        # Dùng gói này = đưa lên đầu thứ tự ưu tiên; move_profile: dir -1 (lên) / +1 (xuống).
        if body.get("name") not in names:
            raise ValueError("không có gói này")
        order = [p["name"] for p in af.profile_order(root, acfg)[0]]
        i = order.index(body["name"])
        if kind == "use_profile":
            order.insert(0, order.pop(i))
        else:
            j = max(0, min(len(order) - 1, i + (1 if int(body.get("dir") or 1) > 0 else -1)))
            order[i], order[j] = order[j], order[i]
        af.save_profile_prefs(root, priority=order)
        return "Thứ tự ưu tiên: " + " → ".join(order)
    if kind == "priority_mode":
        if body.get("mode") == "quota":
            af.save_profile_prefs(root, priority=[])
            return "Tự chọn gói còn nhiều hạn mức nhất"
        order = [p["name"] for p in af.profile_order(root, acfg)[0]]
        af.save_profile_prefs(root, priority=order)
        return "Dùng theo thứ tự ưu tiên: " + " → ".join(order)
    if kind == "rename_profile":
        old, new = body.get("name"), (body.get("new_name") or "").strip()
        if old not in names:
            raise ValueError("không có gói này")
        if not new or new == old:
            raise ValueError("cần tên mới")
        if new in names:
            raise ValueError("tên %s đã có" % new)
        if len(new) > 60 or re.search(r"[/\\\x00-\x1f]", new):
            raise ValueError("tên gói không được chứa / \\ hoặc ký tự điều khiển, tối đa 60 ký tự")
        if any(s.get("state") in ACTIVE_STATES and s.get("profile") == old and pid_alive(s.get("pid"))
               for s in af.load_statuses(root)):
            raise ValueError("%s đang có lượt chạy — đổi tên sau khi chạy xong" % old)
        entries = profile_entries(acfg)
        for e in entries:
            if e["name"] == old:
                e["name"] = new
        save_profiles(entries)
        af.rename_profile_data(root, old, new)
        return "Đã đổi tên %s → %s" % (old, new)
    if kind == "set_limits":
        fields = {}
        for key, name in LIMIT_KEYS:
            if body.get(name) in (None, ""):
                continue
            pct = float(body[name])
            if not 1 <= pct <= 100:
                raise ValueError("ngưỡng phải từ 1 đến 100%")
            fields[key] = round(pct / 100, 4)
        if not fields:
            raise ValueError("chưa nhập ngưỡng")
        save_autofix(**fields)
        labels = {"five_hour": "5 giờ", "seven_day": "7 ngày", "followup_7d": "xem lại 7 ngày"}
        return "Đã lưu ngưỡng: " + ", ".join("%s %g%%" % (labels[name], fields[key] * 100)
                                              for key, name in LIMIT_KEYS if key in fields)
    if kind == "set_profile_limits":
        if body.get("name") not in names:
            raise ValueError("không có gói này")
        own = {}
        for _, name in LIMIT_KEYS:
            if body.get(name) in (None, ""):
                continue  # để trống = theo ngưỡng chung
            pct = float(body[name])
            if not 1 <= pct <= 100:
                raise ValueError("ngưỡng phải từ 1 đến 100%")
            own[name] = round(pct / 100, 4)
        entries = profile_entries(acfg)
        for e in entries:
            if e["name"] == body["name"]:
                if own:
                    e["limits"] = own
                else:
                    e.pop("limits", None)
        save_profiles(entries)
        return "Đã lưu ngưỡng riêng của %s" % body["name"] if own else \
            "%s dùng lại ngưỡng chung" % body["name"]
    if kind == "auto_switch":
        af.save_profile_prefs(root, auto_switch=bool(body.get("value")))
        return "Đã %s tự chuyển gói" % ("bật" if body.get("value") else "tắt")
    if kind == "probe":
        if body.get("name") not in names:
            raise ValueError("không có gói này")
        spawn(["--probe", body["name"]], "probe")
        return "Đang hỏi hạn mức của %s (vài giây)…" % body["name"]
    if kind == "add_profile":
        name = (body.get("name") or "").strip()
        if not name or name in names:
            raise ValueError("cần tên gói mới (chưa dùng)")
        if body.get("token"):
            path = write_token(name, body["token"])
            save_profiles(profile_entries(acfg) + [{"name": name, "token_file": path}])
            return "Đã thêm %s bằng token — bấm \"Cập nhật hạn mức\" để kiểm tra" % name
        config_dir = (body.get("config_dir") or "").strip()
        if not config_dir:
            raise ValueError("cần token (claude setup-token) hoặc thư mục cấu hình")
        os.makedirs(os.path.expanduser(config_dir), exist_ok=True)
        save_profiles(profile_entries(acfg) + [{"name": name, "config_dir": config_dir}])
        return "Đã thêm %s — đăng nhập: CLAUDE_CONFIG_DIR=%s claude rồi gõ /login" % (name, config_dir)
    if kind == "set_token":
        prof = next((p for p in af.profiles(acfg) if p["name"] == body.get("name")), None)
        if not prof or prof["kind"] != "token":
            raise ValueError("chỉ đổi token cho gói thêm bằng token")
        write_token(prof["name"], body.get("token") or "")
        return "Đã đổi token của %s" % prof["name"]
    if kind == "remove_profile":
        current = profile_entries(acfg, drop=body.get("name"))
        if not current:
            raise ValueError("phải giữ ít nhất một gói")
        gone = next((p for p in af.profiles(acfg) if p["name"] == body.get("name")), None)
        save_profiles(current)
        # Token do trang này lưu thì xoá luôn; thư mục đăng nhập thì giữ.
        if gone and gone.get("token_file", "").startswith(TOKEN_DIR + os.sep):
            if os.path.exists(gone["token_file"]):
                os.remove(gone["token_file"])
            return "Đã bỏ %s và xoá file token" % gone["name"]
        return "Đã bỏ %s khỏi danh sách (không xoá thư mục đăng nhập)" % body.get("name")
    # Bỏ qua ngưỡng hạn mức tự đặt (ô "Bỏ qua ngưỡng" trên trang); gói bị chặn thật thì vẫn không chạy được.
    force = ["--ignore-limits"] if body.get("ignore_limits") else []
    if force:
        acfg["_ignore_limits"] = True
    if kind == "followup_now":
        if not any(p["display_id"] == body.get("display_id") for p in af.load_pending(root)):
            raise ValueError("ticket không có trong hàng chờ")
        prof, why = af.choose_profile(root, af.followup_limits(acfg))
        if prof is None:
            raise ValueError("không có gói dùng được: " + why + ("" if force else " — tick \"Bỏ qua ngưỡng\" để chạy"))
        spawn(["--followup-pending", body["display_id"]] + force, body["display_id"])
        return "Đang cho Claude xem lại %s bằng %s%s" % (body["display_id"], prof["name"],
                                                        " (bỏ qua ngưỡng)" if force else "")
    if kind == "followup_drop":
        af.clear_followup(root, body.get("display_id") or "")
        return "Đã bỏ %s khỏi hàng chờ (trạng thái ticket giữ nguyên, đổi tay nếu cần)" % body.get("display_id")
    if kind in ("rerun", "review"):
        display_id = body.get("display_id") or ""
        if not re.match(r"^SOAR-\d+$", display_id):
            raise ValueError("mã ticket không hợp lệ")
        s = find_run(root, display_id) or {}
        if s.get("state") in ACTIVE_STATES and pid_alive(s.get("pid")):
            raise ValueError("%s đang chạy" % display_id)
        extra = ["--profile", body["profile"]] if body.get("profile") in names else []
        prof, why = af.choose_profile(root, acfg, body.get("profile"))
        if prof is None:
            raise ValueError("không có gói dùng được: " + why + ("" if force else " — tick \"Bỏ qua ngưỡng\" để chạy"))
        if kind == "review":
            spawn(["--review", display_id] + extra + force, display_id)
            return "Đã xếp %s vào hàng test lại (Gemini)%s" % (display_id, " · bỏ qua ngưỡng" if force else "")
        # wp_id trong trạng thái có thể là số (lượt mới) hoặc chuỗi (lấy từ inbox).
        wp_id = str(s.get("wp_id") or inbox_wp_ids(cfg).get(display_id) or body.get("wp_id") or "")
        if not wp_id.isdigit():
            raise ValueError("không tìm thấy id work package của %s" % display_id)
        spawn(["--wp", wp_id, "--event", "MANUAL"] + extra + force, display_id)
        return "Đã xếp %s vào hàng chạy lại%s%s" % (display_id, (" bằng " + body["profile"]) if extra else "",
                                                 " · bỏ qua ngưỡng" if force else "")
    raise ValueError("thao tác không hợp lệ")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # yên lặng: trang gọi liên tục
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else (
            body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def local_host(self):
        # Chặn DNS rebinding: chỉ nhận Host là 127.0.0.1 / localhost.
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self.local_host():
            return self.send(403, {"error": "forbidden"})
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                with open(os.path.join(HERE, "op_dashboard.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if url.path == "/api/state":
                return self.send(200, state())
            if url.path == "/api/events":
                return self.send(200, run_events(q.get("id", ""), int(q.get("from", "0"))))
            if url.path == "/api/joblog":
                return self.send(200, {"text": job_log(q.get("key", ""))})
            if url.path == "/api/qa":
                return self.send(200, {"text": qa_report(q.get("name", ""))})
            if url.path == "/api/conflict":
                return self.send(200, {"text": conflict_report(q.get("name", ""))})
            if url.path == "/api/file":
                return self.send(200, {"text": run_file(q.get("id", ""), q.get("name", ""))})
            return self.send(404, {"error": "not found"})
        except Exception as ex:  # noqa: BLE001 — trả lỗi cho trang, không làm chết server
            return self.send(500, {"error": str(ex)})

    def do_POST(self):
        # Header riêng: trang khác không gửi được nếu không qua preflight CORS (server không trả lời preflight).
        if not self.local_host() or self.headers.get("X-Autofix") != "1":
            return self.send(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            return self.send(200, {"message": action(body)})
        except ValueError as ex:
            return self.send(400, {"error": str(ex)})
        except Exception as ex:  # noqa: BLE001
            return self.send(500, {"error": str(ex)})


def main():
    parser = argparse.ArgumentParser(description="Trang theo dõi op_autofix + chọn gói Claude.")
    parser.add_argument("--port", type=int, default=envconf.DASHBOARD_PORT)
    parser.add_argument("--open", action="store_true", help="mở trình duyệt")
    parser.add_argument("--config", help="đường dẫn config op-watch")
    args = parser.parse_args()
    CONFIG_ARGS["config"] = args.config
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = "http://127.0.0.1:%d" % args.port
    print("op-dashboard: %s (Ctrl+C để dừng)" % url, flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
