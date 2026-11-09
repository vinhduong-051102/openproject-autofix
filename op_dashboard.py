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
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import urllib.error
import urllib.parse
import urllib.request

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
    af._CFG.clear()
    af._CFG.update(cfg)  # af.ticket_acfg / af.clean: repo theo dự án của ticket
    return cfg, acfg, os.path.expanduser(acfg["root"])


def projects_brief(cfg):
    """[{key, name, prefix, op_project, enabled, qa}] cho trang (lọc / nhãn dự án theo tiền tố mã ticket)."""
    import projects as P
    try:
        return [dict({k: p[k] for k in ("key", "name", "prefix", "op_project", "enabled", "qa")},
                     repos=len(p["repos"]) if (p["repos"] or not p["_legacy"]) else -1)  # -1: repo từ cấu hình cũ
                for p in P.projects(cfg)]
    except ValueError:
        return []


def inbox_wp_ids(cfg):
    """SOAR-xxxx → id work package, lấy từ link trong inbox (lượt chạy cũ không ghi wp_id)."""
    out = {}
    path = os.path.expanduser(cfg.get("inbox") or "")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            for m in re.finditer(r"(%s) https?://\S+/work_packages/(\d+)" % envconf.TICKET_RX, f.read()):
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
        if st in ACTIVE_STATES and s.get("pid") and not af.status_alive(s):
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
        "projects": projects_brief(cfg),
    }


# ---- Các job nền (systemd user) -------------------------------------------------------------------------------
def _job_texts():
    """Mô tả job lấy theo cấu hình (nhánh, lịch, thư mục báo cáo) để trang đúng với máy đang chạy."""
    a = dict(af.AUTOFIX_DEFAULTS)
    a.update(envconf._read_json(envconf.CONFIG_PATH).get("autofix") or {})
    envconf.overlay(a, envconf.AUTOFIX_ENV)
    merge_at = envconf.get("MERGE_SCHEDULE", "*-*-* 12:00:00;*-*-* 18:00:00").replace(";", ", ")
    report_at = envconf.get("REPORT_SCHEDULE", "Mon..Fri *-*-* 08:00:00")
    limits_at = envconf.get("LIMITS_SCHEDULE", "*-*-* 08:00:00").replace(";", ", ")
    return {
        "merge": "Theo lịch %s: PR %s → %s, %s → %s, rồi deploy server test và QA ticket (lỡ giờ vì tắt máy thì chạy bù "
                 "khi bật máy)" % (merge_at, a["fe_branch"], a["fe_upstream"], a["be_branch"], a["be_upstream"]),
        "report": "Theo lịch %s: tạo %s/bao-cao-DD-MM-YYYY.txt (lỡ giờ thì chạy khi bật máy)" % (
            report_at, envconf.get("DAILY_REPORT_DIR", "~/Downloads/bao-cao-ngay")),
        "limits": "Theo lịch %s: cập nhật hạn mức 5 giờ / 7 ngày của mọi gói Claude đã đăng nhập (op_autofix.py --probe,"
                  " mỗi gói một câu rất ngắn); lỡ giờ vì tắt máy thì chạy khi bật máy" % limits_at,
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
    {"key": "limits", "name": "Cập nhật hạn mức", "unit": "op-limits.service", "timer": "op-limits.timer",
     "desc": _TEXTS["limits"], "inbox": None},
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


def qa_procs_alive():
    """Có tiến trình QA server đang chạy không (deploy_qa.py --qa / --review-watch, hoặc merge_to_dev → deploy + QA)."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                cmd = f.read()
        except OSError:
            continue
        if b"deploy_qa.py" in cmd or b"merge_to_dev.py" in cmd:
            return True
    return False


def _last_write(d):
    t = os.path.getmtime(d)
    for name in os.listdir(d):
        try:
            t = max(t, os.path.getmtime(os.path.join(d, name)))
        except OSError:
            pass
    return t


def qa_runs(root, cfg, merge_running, n=15):
    """Các lượt QA trên server 86 (thư mục <SOAR>-qa-<MMDD-HHMM>), mới nhất trước."""
    final = {}
    path = os.path.expanduser(cfg.get("inbox") or "")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                m = re.search(r"\*\*QA SERVER\*\* (%s) — (.*?) · (\S+/qa\.md)" % envconf.TICKET_RX, ln)
                if m:
                    final[os.path.dirname(m.group(3))] = m.group(2)
    dirs = sorted((d for pre in envconf.TICKET_PREFIXES for d in glob.glob(os.path.join(root, "%s-*-qa-*" % pre))),
                  key=os.path.getmtime, reverse=True)[:n]
    out = []
    alive = merge_running or qa_procs_alive()
    for i, d in enumerate(dirs):
        name = os.path.basename(d)
        qa_md = os.path.join(d, "qa.md")
        verdict, state = final.get(d), "done"
        if not verdict and os.path.exists(qa_md):
            with open(qa_md, encoding="utf-8", errors="replace") as f:
                m = re.search(r"QA:\s*(KHÔNG RÕ YÊU CẦU|KHÔNG ĐẠT|ĐẠT|KHÔNG TỰ TEST ĐƯỢC)", f.read(300))
            verdict = m.group(1) if m else "không đọc được kết luận"
        if not verdict:
            # Chưa có qa.md: còn tiến trình QA chạy và thư mục vừa được ghi (mỗi vòng Gemini ghi gemini-rounds.jsonl;
            # một lần gọi tối đa QA_CALL_TIMEOUT × 2 lần thử) → đang test; không thì bị ngắt giữa chừng. Trước đây chỉ
            # lượt mới nhất khi job merge chạy mới là "đang test" → QA song song / QA từ nút bấm bị báo nhầm "bị ngắt".
            state = "running" if alive and time.time() - _last_write(d) < 20 * 60 else "stale"
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
    if not re.match(r"^%s-qa-[\d-]+$" % envconf.TICKET_RX, name or ""):
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
            "running": os.path.exists(os.path.join(load()[2], ".qa-active")),
            "qa_name": envconf.deploy_config().get("qa_user_name") or "người QA"}


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
        import repos as R
        _, acfg_, _ = load()
        for r, path in R.trees(af.ticket_acfg(acfg_, s.get("display_id") or ""), s["run_dir"]):
            if os.path.isdir(path):
                res = subprocess.run(["git", "diff", "--shortstat", "HEAD"], cwd=path, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, universal_newlines=True)
                diff[r["label"]] = res.stdout.strip() or "chưa sửa file"
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
    if str(kind).startswith("config_"):  # trang Cấu hình
        import config_page
        return config_page.action(body, save_autofix)
    if str(kind).startswith("tg_"):  # yêu cầu tester qua Telegram
        import tg_requests
        return tg_requests.action(body)
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
        return "Đã lưu số agent song song: " + ", ".join(msgs) + " (áp dụng ngay cho các lượt đang xếp hàng; lượt đang chạy vẫn chạy tiếp)"
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
    if kind == "task_draft":
        import task_create
        did = task_create.start_draft(body.get("prompt"), body.get("files") or [], body.get("opts") or {})
        return {"message": "AI đang soạn mô tả task (khoảng 1 phút)…", "id": did}
    if kind == "task_refine":
        import task_create
        n = task_create.refine_draft(body.get("id"), body.get("answers") or [], body.get("subject") or "",
                                     body.get("description") or "")
        return {"message": "AI đang soạn lại mô tả (lần %d) với thông tin bạn nhập…" % n, "id": body.get("id")}
    if kind == "task_split":
        import task_split
        sid = task_split.start_split(body.get("ticket"), body.get("hint") or "")
        return {"message": "AI đang đọc ticket + file đính kèm để tách việc (vài phút)…", "id": sid}
    if kind == "task_split_create":
        import task_split
        r = task_split.create_children(body.get("id"), body.get("tasks") or [], body.get("assignee") or "me")
        msg = "Đã tạo %d việc con: %s" % (len(r["created"]), ", ".join(x["display_id"] for x in r["created"]) or "không có")
        if r["errors"]:
            msg += " — lỗi: " + "; ".join(r["errors"])[:300]
        return dict(r, message=msg)
    if kind == "task_create":
        import task_create
        r = task_create.create(body.get("id"), body.get("subject"), body.get("description"), body.get("type") or "Bug",
                               body.get("priority") or "High", body.get("assignee") or "me")
        msg = "Đã tạo %s trên OpenProject (New%s)" % (r["display_id"], ", %d file" % r["attachments"] if r["attachments"] else "")
        if r["errors"]:
            msg += " — lỗi upload: " + "; ".join(r["errors"])[:300]
        return dict(r, message=msg)
    if kind == "poll_now":
        pid = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", "op-watch.service"],
                             capture_output=True, text=True).stdout.strip()
        if not pid or pid == "0":
            raise ValueError("op-watch không chạy (systemctl --user start op-watch)")
        os.kill(int(pid), signal.SIGUSR1)
        review_queue(force=True)
        _JOB_CACHE["at"] = 0
        return "Đang kiểm tra OpenProject ngay (task mới / Reopened / comment) và làm mới danh sách chờ QA"
    if kind == "review_qa_one":
        tid = (body.get("display_id") or "").strip()
        if not re.match(r"^[A-Z][A-Z0-9]*-\d+$", tid):
            raise ValueError("mã ticket không hợp lệ")
        if os.path.exists(os.path.join(root, ".qa-active")):
            raise ValueError("đang có lượt QA khác chạy trên server — chờ xong rồi bấm lại")
        if subprocess.run(["systemctl", "--user", "is-active", "--quiet", "op-merge.service"]).returncode == 0:
            raise ValueError("đang merge + deploy — chờ xong rồi bấm lại")
        subprocess.Popen([sys.executable, os.path.join(HERE, "deploy_qa.py"), "--qa", tid, "--force"], cwd=HERE,
                         stdout=open(os.path.join(root, ".review-watch.log"), "a"), stderr=subprocess.STDOUT,
                         start_new_session=True)
        _REVIEWQ["at"] = 0
        return "Đang QA %s trên server (bản đang chạy, kể cả khi chưa có commit) — kết quả hiện ở Hoạt động · QA 86" % tid
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
        if any(s.get("state") in ACTIVE_STATES and s.get("profile") == old and af.status_alive(s)
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
    if kind == "assist":
        display_id = body.get("display_id") or ""
        if not re.match(r"^%s$" % envconf.TICKET_RX, display_id):
            raise ValueError("mã ticket không hợp lệ")
        s = find_run(root, display_id) or {}
        if s.get("state") in ACTIVE_STATES and af.status_alive(s):
            raise ValueError("%s đang chạy" % display_id)
        run_name = s.get("run_name") or ""
        if not re.match(r"^%s-[\w-]+$" % envconf.TICKET_RX, run_name) or not run_name.startswith(display_id + "-"):
            raise ValueError("không tìm thấy lượt chạy của %s" % display_id)
        note = (body.get("note") or "").strip()
        if len(note) > 4000:
            raise ValueError("ghi chú quá dài (tối đa 4000 ký tự)")
        extra = ["--profile", body["profile"]] if body.get("profile") in names else []
        prof, why = af.choose_profile(root, acfg, body.get("profile"))
        if prof is None:
            raise ValueError("không có gói dùng được: " + why + ("" if force else " — tick \"Bỏ qua ngưỡng\" để chạy"))
        spawn(["--assist", run_name] + (["--note", note] if note else []) + extra + force, display_id)
        return "Claude đang hỗ trợ %s (tải file đính kèm, ticket liên quan, GET API server test)%s" % (
            display_id, " · có ghi chú" if note else "")
    if kind in ("rerun", "review"):
        display_id = body.get("display_id") or ""
        if not re.match(r"^%s$" % envconf.TICKET_RX, display_id):
            raise ValueError("mã ticket không hợp lệ")
        s = find_run(root, display_id) or {}
        if s.get("state") in ACTIVE_STATES and af.status_alive(s):
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
        # Chặn DNS rebinding: chỉ nhận Host là 127.0.0.1 / localhost / IP nghe thêm (DASHBOARD_EXTRA_BIND).
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost") or host in envconf.DASHBOARD_EXTRA_BIND

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
            if url.path == "/api/op":
                return self.send(200, op_work_packages(q.get("scope", "mine"), q.get("status", "open"), q.get("q", ""),
                                                       q.get("page", "1") or 1, q.get("refresh") == "1",
                                                       q.get("project", "")))
            if url.path == "/api/op/statuses":
                try:
                    return self.send(200, op_allowed_statuses(q.get("id", "")))
                except ValueError as ex:
                    return self.send(400, {"error": str(ex)})
                except OPError as ex:
                    return self.send(502, {"error": str(ex)})
            if url.path == "/api/op/assignees":
                try:
                    return self.send(200, op_allowed_assignees(q.get("id", "")))
                except ValueError as ex:
                    return self.send(400, {"error": str(ex)})
                except OPError as ex:
                    return self.send(502, {"error": str(ex)})
            if url.path == "/api/op/wp":
                return self.send(200, op_wp_detail(q.get("id", "")))
            if url.path == "/api/op/att":
                got = op_attachment(q.get("id", ""))
                if not got:
                    return self.send(404, {"error": "không lấy được file đính kèm"})
                data, ctype, name = got
                inline = bool(_ATT_INLINE.match(ctype)) and q.get("dl") != "1"
                # Loại khác (html, svg, ...) luôn tải về, không hiển thị trong origin của dashboard
                self.send_response(200)
                self.send_header("Content-Type", ctype if inline else "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", "%s; filename*=UTF-8''%s" % (
                    "inline" if inline else "attachment", urllib.parse.quote(name)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(data)
                return None
            if url.path == "/api/compose-log":
                return self.send(200, compose_log(int(q.get("from", "0") or 0)))
            if url.path == "/api/qa":
                return self.send(200, {"text": qa_report(q.get("name", ""))})
            if url.path == "/api/taskdraft":
                import task_create
                if q.get("id"):
                    return self.send(200, task_create.get_draft(q.get("id")))
                dcfg_ = envconf.deploy_config()
                return self.send(200, {"drafts": task_create.list_drafts(),
                                       "qa_user": {"id": dcfg_.get("qa_user_id"), "name": dcfg_.get("qa_user_name")}})
            if url.path == "/api/tasksplit":
                import task_split
                try:
                    st = task_split.get_split(q["id"]) if q.get("id") else task_split.latest_for(q.get("ticket", ""))
                except ValueError as ex:
                    return self.send(400, {"error": str(ex)})
                return self.send(200, {"split": st})
            if url.path == "/api/conflict":
                return self.send(200, {"text": conflict_report(q.get("name", ""))})
            if url.path == "/api/file":
                return self.send(200, {"text": run_file(q.get("id", ""), q.get("name", ""))})
            if url.path.startswith("/api/config"):  # trang Cấu hình (config_page.py)
                import config_page
                _, acfg_, _ = load()
                try:
                    res = config_page.http_get_q(url.path, q, acfg_)
                except ValueError as ex:
                    return self.send(400, {"error": str(ex)})
                return self.send(200 if res is not None else 404, res if res is not None else {"error": "not found"})
            if url.path.startswith("/api/tg/"):  # yêu cầu tester qua Telegram (tg_requests.py)
                import tg_requests
                try:
                    code, body, ctype = tg_requests.http_get(url.path, q)
                except ValueError as ex:
                    return self.send(400, {"error": str(ex)})
                return self.send(code, body, ctype)
            return self.send(404, {"error": "not found"})
        except Exception as ex:  # noqa: BLE001 — trả lỗi cho trang, không làm chết server
            return self.send(500, {"error": str(ex)})

    def do_POST(self):
        # Header riêng: trang khác không gửi được nếu không qua preflight CORS (server không trả lời preflight).
        if not self.local_host() or self.headers.get("X-Autofix") != "1":
            return self.send(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 200000:
                raise ValueError("dữ liệu quá lớn")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body phải là JSON object")
            if urlparse(self.path).path == "/api/op/status":
                try:
                    res = op_set_status(body.get("id"), body.get("status_id"), body.get("lock_version"))
                    op_changed()
                    return self.send(200, res)
                except OPError as ex:
                    return self.send(502, {"error": str(ex)})
            if urlparse(self.path).path == "/api/op/assignee":
                try:
                    res = op_set_assignee(body.get("id"), body.get("user_id"), body.get("lock_version"))
                    op_changed()
                    return self.send(200, res)
                except OPError as ex:
                    return self.send(502, {"error": str(ex)})
            if urlparse(self.path).path == "/api/op/comment":
                try:
                    res = op_comment(body.get("id"), body.get("text"))
                    op_changed()
                    return self.send(200, res)
                except OPError as ex:
                    return self.send(502, {"error": str(ex)})
            if urlparse(self.path).path == "/api/op/changed":
                op_changed()  # trang thấy tiến trình nền vừa cập nhật (autofix / QA / Telegram) → bỏ cache
                return self.send(200, {"ok": True})
            res = action(body)
            if body.get("action") in OP_WRITE_ACTIONS:
                op_changed()
            return self.send(200, res if isinstance(res, dict) else {"message": res})
        except ValueError as ex:
            return self.send(400, {"error": str(ex)})
        except Exception as ex:  # noqa: BLE001
            return self.send(500, {"error": str(ex)})


COMPOSE_TAIL = 256 * 1024  # lần đầu (from=0) hoặc file bị ghi đè: chỉ gửi đuôi chừng này byte


_OP_CACHE = {}

# Thao tác từ trang có ghi lên OpenProject → xoá cache đọc OpenProject (danh sách ticket, trạng thái ticket trong mục
# Telegram / Xin ý kiến) để lần tải ngay sau đó lấy dữ liệu mới.
OP_WRITE_ACTIONS = {"task_create", "task_split_create", "rerun", "assist", "review", "followup_now", "review_qa_now", "review_qa_one", "job_start",
                    "tg_approve", "tg_link", "tg_retry", "tg_ask_reopen", "tg_ask_resolve", "tg_triage"}


def op_changed():
    _OP_CACHE.clear()
    mod = sys.modules.get("tg_requests")
    if mod is not None:
        mod._LIVE.clear()
_OP_LOCK = threading.Lock()


def op_work_packages(scope="mine", status="open", q="", page=1, refresh=False, project=""):
    """Work package OpenProject cho tab OpenProject (đọc qua API token trong config, cache 60s).
    scope: mine (giao cho tôi) | all (cả project); status: open | all; q: tìm theo tiêu đề / nội dung / SOAR-xxx."""
    scope = "all" if scope == "all" else "mine"
    status = "all" if status == "all" else "open"
    q, page = (q or "").strip()[:100], max(1, min(int(page or 1), 200))
    key = (scope, status, q, page, project or "")
    with _OP_LOCK:
        hit = _OP_CACHE.get(key)
        if hit and not refresh and time.time() - hit[0] < 60:
            return hit[1]
    import projects as P
    cfg = load()[0]
    items_p = P.projects(cfg)
    if project == "all" and len(items_p) > 1:
        chosen = items_p
    else:
        chosen = [(P.get(cfg, project) if project else None) or items_p[0]]
    cfg = P.scope(cfg, None, chosen[0])[0]
    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    filters = []
    if scope == "mine":
        filters.append({"assignee": {"operator": "=", "values": ["me"]}})
    if status == "open":
        filters.append({"status": {"operator": "o", "values": []}})
    if q and re.fullmatch(r"#?\d{1,7}", q):
        # Gõ số (2598 / #2598): mã hiển thị <tiền tố>-2598 (vd SOAR-2598), không có thì id nội bộ 2598
        n = q.lstrip("#")
        tries = ["%s-%s" % (p["prefix"], n) for p in chosen] + [n]
    elif q and re.fullmatch(r"[A-Za-z]+-\d+", q):
        tries = [q.upper()]
    else:
        tries = []
    if tries:
        # Tìm đúng mã (SOAR-2577 / 2577): filter search không khớp displayId → lấy thẳng work package đó
        # (không theo bộ lọc Của tôi / Đang mở: gõ mã là muốn thấy đúng ticket đó)
        data = {"_embedded": {"elements": []}, "total": 0}
        for t in tries:
            try:
                data = {"_embedded": {"elements": [api.get("/api/v3/work_packages/%s" % t)]}, "total": 1}
                break
            except Exception:  # noqa: BLE001
                continue
        filters = None
    elif q:
        filters.append({"search": {"operator": "**", "values": [q]}})
    size = 50
    try:
        if filters is None:
            pass
        elif len(chosen) > 1:  # tất cả dự án: một truy vấn chung lọc theo danh sách dự án
            data = api.get("/api/v3/work_packages", {
                "filters": json.dumps(filters + [{"project": {"operator": "=", "values": [p["op_project"] for p in chosen]}}]),
                "pageSize": size, "offset": page, "sortBy": json.dumps([["updatedAt", "desc"]])})
        else:
            data = api.get("/api/v3/projects/%s/work_packages" % cfg["project"], {
                "filters": json.dumps(filters), "pageSize": size, "offset": page,
                "sortBy": json.dumps([["updatedAt", "desc"]])})
    except Exception as ex:  # noqa: BLE001
        return {"error": "Không gọi được OpenProject: %s" % str(ex)[:200], "items": [], "total": 0}
    base = cfg["url"].rstrip("/")
    items = []
    for e in data.get("_embedded", {}).get("elements", []):
        L = e.get("_links", {})
        t = lambda k: (L.get(k) or {}).get("title") or ""  # noqa: E731
        items.append({"id": e.get("id"), "display_id": e.get("displayId") or "#%s" % e.get("id"),
                      "subject": e.get("subject") or "", "status": t("status"), "type": t("type"),
                      "priority": t("priority"), "assignee": t("assignee"), "updated": e.get("updatedAt") or "",
                      "project": t("project"),
                      "url": "%s/work_packages/%s" % (base, e.get("id"))})
    out = {"items": items, "total": data.get("total", len(items)), "page": page, "page_size": size,
           "fetched": datetime.now().strftime("%H:%M:%S"), "op_url": base}
    with _OP_LOCK:
        if len(_OP_CACHE) > 200:
            _OP_CACHE.clear()
        _OP_CACHE[key] = (time.time(), out)
    return out


_OP_API = {}


def op_api():
    """Một client OpenProject dùng chung (giữ cache tên user giữa các lần gọi)."""
    cfg = load()[0]
    key = (cfg["url"], cfg["api_key"])
    if key not in _OP_API:
        _OP_API.clear()
        _OP_API[key] = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    return _OP_API[key], cfg


_OP_ME = {}


def op_me_id(api):
    """id user của API token (bình luận của tôi trong khung chat), cache theo url."""
    if api.url not in _OP_ME:
        try:
            _OP_ME[api.url] = str(api.me().get("id") or "")
        except Exception:  # noqa: BLE001
            return ""
    return _OP_ME[api.url]


class OPError(Exception):
    """Lỗi do OpenProject trả về khi ghi (trả 502 cho client kèm thông báo của OP)."""


def op_comment(wp_id, text):
    """Gửi bình luận (markdown) lên work package. Ghi thật lên OpenProject."""
    wp_id = str(wp_id or "").strip()
    text = (text or "").strip() if isinstance(text, str) else ""
    if not re.fullmatch(r"\d{1,12}", wp_id):
        raise ValueError("mã work package không hợp lệ")
    if not text:
        raise ValueError("bình luận trống")
    if len(text) > 20000:
        raise ValueError("bình luận quá dài (tối đa 20000 ký tự)")
    api, _cfg = op_api()
    try:
        res = af.op_write(api, "POST", "/api/v3/work_packages/%s/activities" % wp_id, {"comment": {"raw": text}},
                          normalize=False)
    except urllib.error.HTTPError as ex:
        try:
            msg = json.loads(ex.read().decode("utf-8", "replace")).get("message") or ""
        except Exception:  # noqa: BLE001
            msg = ""
        raise OPError("OpenProject trả %s%s" % (ex.code, (": " + msg) if msg else ""))
    except Exception as ex:  # noqa: BLE001
        raise OPError("Không gọi được OpenProject: %s" % str(ex)[:200])
    with _OP_LOCK:
        _OP_CACHE.clear()  # danh sách sắp theo updatedAt → làm mới
    print("op-dashboard: [ghi OP] bình luận wp %s (%d ký tự) → activity %s" % (wp_id, len(text), (res or {}).get("id")),
          flush=True)
    return {"ok": True, "activity_id": (res or {}).get("id")}


def _op_http_error(ex):
    try:
        msg = json.loads(ex.read().decode("utf-8", "replace")).get("message") or ""
    except Exception:  # noqa: BLE001
        msg = ""
    return OPError("OpenProject trả %s%s" % (ex.code, (": " + msg) if msg else ""))


def op_allowed_statuses(wp_id):
    """Trạng thái được phép chuyển tới (theo workflow của OpenProject cho user của API token).
    Dùng form API (POST /form chỉ kiểm tra, không ghi gì)."""
    wp_id = str(wp_id or "").strip()
    if not re.fullmatch(r"\d{1,12}", wp_id):
        raise ValueError("mã work package không hợp lệ")
    api, _cfg = op_api()
    try:
        w = api.get("/api/v3/work_packages/%s" % wp_id)
        form = af.op_write(api, "POST", "/api/v3/work_packages/%s/form" % wp_id, {"lockVersion": w["lockVersion"]})
    except urllib.error.HTTPError as ex:
        raise _op_http_error(ex)
    except Exception as ex:  # noqa: BLE001
        raise OPError("Không gọi được OpenProject: %s" % str(ex)[:200])
    st = ((form.get("_embedded") or {}).get("schema") or {}).get("status") or {}
    cur = ((w.get("_links") or {}).get("status") or {}).get("href", "").rstrip("/").split("/")[-1]
    allowed = [{"id": x["href"].rstrip("/").split("/")[-1], "title": x.get("title") or ""}
               for x in (st.get("_links") or {}).get("allowedValues") or []]
    return {"writable": bool(st.get("writable", True)), "current": cur, "statuses": allowed,
            "lock_version": w["lockVersion"]}


def op_set_status(wp_id, status_id, lock_version=None):
    """Đổi trạng thái work package (ghi thật lên OpenProject). Chỉ nhận trạng thái nằm trong danh sách được phép.
    lock_version (lúc mở chi tiết) khác hiện tại → báo người khác vừa sửa, không ghi đè."""
    status_id = str(status_id or "").strip()
    if not re.fullmatch(r"\d{1,6}", status_id):
        raise ValueError("trạng thái không hợp lệ")
    info = op_allowed_statuses(wp_id)
    if not info["writable"]:
        raise ValueError("tài khoản API token không được đổi trạng thái ticket này")
    names = {x["id"]: x["title"] for x in info["statuses"]}
    if status_id not in names:
        raise ValueError("không được chuyển sang trạng thái này theo workflow OpenProject")
    if lock_version not in (None, "") and str(lock_version) != str(info["lock_version"]):
        raise OPError("Ticket vừa được người khác cập nhật — tải lại chi tiết rồi đổi lại")
    api, _cfg = op_api()
    try:
        af.op_write(api, "PATCH", "/api/v3/work_packages/%s" % str(wp_id).strip(), {
            "lockVersion": info["lock_version"], "_links": {"status": {"href": "/api/v3/statuses/%s" % status_id}}})
    except urllib.error.HTTPError as ex:
        raise _op_http_error(ex)
    except Exception as ex:  # noqa: BLE001
        raise OPError("Không gọi được OpenProject: %s" % str(ex)[:200])
    with _OP_LOCK:
        _OP_CACHE.clear()
    print("op-dashboard: [ghi OP] đổi trạng thái wp %s → %s (%s)" % (wp_id, names[status_id], status_id), flush=True)
    return {"ok": True, "status": names[status_id]}


def op_allowed_assignees(wp_id):
    """Người có thể được giao ticket (available_assignees theo schema form; POST /form không ghi gì)."""
    wp_id = str(wp_id or "").strip()
    if not re.fullmatch(r"\d{1,12}", wp_id):
        raise ValueError("mã work package không hợp lệ")
    api, _cfg = op_api()
    try:
        w = api.get("/api/v3/work_packages/%s" % wp_id)
        form = af.op_write(api, "POST", "/api/v3/work_packages/%s/form" % wp_id, {"lockVersion": w["lockVersion"]})
        sch = ((form.get("_embedded") or {}).get("schema") or {}).get("assignee") or {}
        href = ((sch.get("_links") or {}).get("allowedValues") or {}).get("href") \
            or "/api/v3/work_packages/%s/available_assignees" % wp_id
        users = api.get(href, {"pageSize": 500}).get("_embedded", {}).get("elements", [])
    except urllib.error.HTTPError as ex:
        raise _op_http_error(ex)
    except Exception as ex:  # noqa: BLE001
        raise OPError("Không gọi được OpenProject: %s" % str(ex)[:200])
    cur = ((w.get("_links") or {}).get("assignee") or {}).get("href") or ""
    users = sorted(({"id": str(u.get("id")), "name": (u.get("name") or "").strip(), "type": u.get("_type") or ""}
                    for u in users if u.get("id") is not None), key=lambda u: u["name"].lower())
    return {"writable": bool(sch.get("writable", True)), "current": cur.rstrip("/").split("/")[-1] if cur else "",
            "me": op_me_id(api), "users": users, "lock_version": w["lockVersion"]}


def op_set_assignee(wp_id, user_id, lock_version=None):
    """Đổi người được giao (ghi thật lên OpenProject). user_id "" / "none" = bỏ giao; chỉ nhận người trong
    available_assignees. lock_version khác hiện tại → báo người khác vừa sửa, không ghi đè."""
    user_id = str(user_id or "").strip()
    if user_id == "none":
        user_id = ""
    if user_id and not re.fullmatch(r"\d{1,12}", user_id):
        raise ValueError("người được giao không hợp lệ")
    info = op_allowed_assignees(wp_id)
    if not info["writable"]:
        raise ValueError("tài khoản API token không được đổi người được giao ticket này")
    names = {u["id"]: u["name"] for u in info["users"]}
    if user_id and user_id not in names:
        raise ValueError("người này không được giao ticket trong dự án")
    if lock_version not in (None, "") and str(lock_version) != str(info["lock_version"]):
        raise OPError("Ticket vừa được người khác cập nhật — tải lại chi tiết rồi đổi lại")
    api, _cfg = op_api()
    link = {"href": "/api/v3/users/%s" % user_id} if user_id else {"href": None}
    try:
        af.op_write(api, "PATCH", "/api/v3/work_packages/%s" % str(wp_id).strip(), {
            "lockVersion": info["lock_version"], "_links": {"assignee": link}})
    except urllib.error.HTTPError as ex:
        raise _op_http_error(ex)
    except Exception as ex:  # noqa: BLE001
        raise OPError("Không gọi được OpenProject: %s" % str(ex)[:200])
    with _OP_LOCK:
        _OP_CACHE.clear()
    name = names.get(user_id, "") if user_id else ""
    print("op-dashboard: [ghi OP] đổi người được giao wp %s → %s (%s)" % (wp_id, name or "chưa giao", user_id or "-"),
          flush=True)
    return {"ok": True, "assignee": name}


def op_html(html, base):
    """HTML từ OpenProject (đã được OP làm sạch): ảnh đính kèm → qua dashboard (/api/op/att), link work package
    → mở chi tiết trong dashboard (#wp-<id>). Máy xem qua Tailscale không vào được op.cytech.ai."""
    html = html or ""
    b = re.escape(base)
    html = re.sub(r'(?:%s)?/api/v3/attachments/(\d+)/content' % b, r'/api/op/att?id=\1', html)
    html = re.sub(r'href="(?:%s)?(?:/projects/[^/"]+)?/work_packages/(\d+)(?:/[^"]*)?"' % b, r'href="#wp-\1"', html)
    return html


def op_wp_detail(wp_id):
    wp_id = (wp_id or "").strip()
    if not re.fullmatch(r"\d+|[A-Za-z]+-\d+", wp_id):
        return {"error": "mã work package không hợp lệ"}
    api, cfg = op_api()
    base = cfg["url"].rstrip("/")
    try:
        w = api.get("/api/v3/work_packages/%s" % wp_id.upper())
        acts = api.get("/api/v3/work_packages/%s/activities" % w["id"]).get("_embedded", {}).get("elements", [])
        atts = api.get("/api/v3/work_packages/%s/attachments" % w["id"]).get("_embedded", {}).get("elements", [])
    except urllib.error.HTTPError as ex:
        return {"error": "OpenProject trả %s cho %s" % (ex.code, wp_id)}
    except Exception as ex:  # noqa: BLE001
        return {"error": "Không gọi được OpenProject: %s" % str(ex)[:200]}
    L = w.get("_links", {})
    t = lambda k: (L.get(k) or {}).get("title") or ""  # noqa: E731
    parent = L.get("parent") or {}
    pid = (parent.get("href") or "").rstrip("/").split("/")[-1]
    me = op_me_id(api)
    activities = []
    for a in acts:
        comment = ((a.get("comment") or {}).get("html") or "").strip()
        details = [d.get("html") or d.get("raw") or "" for d in a.get("details") or []]
        if not comment and not details:
            continue
        uhref = ((a.get("_links") or {}).get("user") or {}).get("href") or ""
        activities.append({"id": a.get("id"), "mine": bool(me) and uhref.rstrip("/").split("/")[-1] == me,
                           "user": api.user_name(uhref),
                           "at": a.get("createdAt") or "", "comment": op_html(comment, base),
                           "details": details, "internal": bool(a.get("internal"))})
    return {
        "id": w.get("id"), "display_id": w.get("displayId") or "#%s" % w.get("id"), "subject": w.get("subject") or "",
        "lock_version": w.get("lockVersion"),
        "type": t("type"), "status": t("status"), "priority": t("priority"), "assignee": t("assignee"),
        "responsible": t("responsible"), "author": t("author"), "version": t("version"), "category": t("category"),
        "parent": {"id": pid, "title": parent.get("title") or ""} if pid.isdigit() else None,
        "created": w.get("createdAt") or "", "updated": w.get("updatedAt") or "",
        "start": w.get("startDate"), "due": w.get("dueDate"), "done": w.get("percentageDone"),
        "description": op_html((w.get("description") or {}).get("html"), base),
        "attachments": [{"id": a.get("id"), "name": a.get("fileName") or "", "size": a.get("fileSize") or 0,
                         "type": a.get("contentType") or "", "author": ((a.get("_links") or {}).get("author") or {}).get("title") or "",
                         "at": a.get("createdAt") or ""} for a in atts],
        "activities": activities, "url": "%s/work_packages/%s" % (base, w.get("id")),
    }


_ATT_INLINE = re.compile(r"^(image/(png|jpe?g|gif|webp|bmp)|application/pdf|text/plain|video/(mp4|webm))$")


def op_attachment(att_id):
    """(bytes, content-type, tên file) của file đính kèm; None nếu không lấy được. Giới hạn 50 MB."""
    if not re.fullmatch(r"\d{1,12}", att_id or ""):
        return None
    api, cfg = op_api()
    try:
        meta = api.get("/api/v3/attachments/%s" % att_id)
        if (meta.get("fileSize") or 0) > 50 * 1024 * 1024:
            return None
        req = urllib.request.Request(cfg["url"] + "/api/v3/attachments/%s/content" % att_id, headers=api.headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read(50 * 1024 * 1024 + 1)
    except Exception:  # noqa: BLE001
        return None
    return data, meta.get("contentType") or "application/octet-stream", meta.get("fileName") or "file"


def compose_log(start=0):
    """Log docker compose của deploy server (deploy_qa ghi ~/autofix/.compose-live.log từng dòng).
    from=<offset byte> → phần mới từ offset; file ngắn hơn offset (deploy mới ghi đè) → gửi lại từ đầu (reset)."""
    root = load()[2]
    path = os.path.join(root, ".compose-live.log")
    try:
        with open(os.path.join(root, ".compose-live.json"), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    try:
        size, mtime = os.path.getsize(path), os.path.getmtime(path)
    except OSError:
        return {"exists": False, "text": "", "next": 0, "reset": start > 0, "state": st}
    reset = start > size or start < 0
    tail = reset or start == 0
    if tail:
        start = max(0, size - COMPOSE_TAIL)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(size - start)
    if tail and start > 0:  # nhảy vào giữa file: bỏ dòng dở đầu
        data = data[data.find(b"\n") + 1:]
    return {"exists": True, "text": data.decode("utf-8", "replace"), "next": size, "reset": reset,
            "size": size, "mtime": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
            "running": bool(st.get("running")), "state": st}


def serve_extra(host, port):
    """Nghe thêm trên host (vd IP Tailscale). IP chưa có (tailscaled chưa lên lúc boot) → thử lại mỗi 30s."""
    while True:
        try:
            srv = ThreadingHTTPServer((host, port), Handler)
        except OSError as ex:
            print("op-dashboard: chưa nghe được %s:%d (%s) — thử lại sau 30s" % (host, port, ex), flush=True)
            time.sleep(30)
            continue
        print("op-dashboard: http://%s:%d" % (host, port), flush=True)
        srv.serve_forever()


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
    for host in envconf.DASHBOARD_EXTRA_BIND:
        threading.Thread(target=serve_extra, args=(host, args.port), daemon=True).start()
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
