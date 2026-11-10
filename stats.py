#!/usr/bin/env python3
"""
stats: thống kê task HOÀN THÀNH và task BỊ REOPEN theo lịch sử đổi trạng thái trên OpenProject.

Nguồn: work package của các dự án (projects.py) có cập nhật trong khoảng thời gian → activities (journal) từng ticket →
các lần đổi trạng thái "Status changed from A to B". Activities cache trên đĩa theo (id, updatedAt) nên lần sau chỉ tải
ticket có thay đổi: ~/.cache/op-watch/stats-activities.json.

  Hoàn thành  = chuyển sang một trạng thái hoàn thành (mặc định Resolved / Done / Closed) trong khoảng.
  Bị reopen   = chuyển sang Reopened (thường do tester mở lại).
  Trả lại nội bộ = từ trạng thái hoàn thành về trạng thái đang làm khác Reopened (UNREVIEWED khi QA trên server trượt,
                TO DO khi chờ Claude xem lại, Review, New…) — không tính sang Rejected / Canceled.
  Tỉ lệ reopen = số ticket bị reopen sau lần hoàn thành trong khoảng / số ticket hoàn thành trong khoảng.
  Của tôi     = ticket tôi (user của API key) chuyển hoàn thành, hoặc trường Dev / người được giao là tôi.
  Autofix     = lần hoàn thành có comment **[Autofix] của tôi trong ±10 phút.

  python3 stats.py [--days 30] [--scope mine|all] [--project soar]
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402

DONE = {"Resolved", "Done", "Closed"}
NOT_REOPEN = DONE | {"Rejected", "Canceled", "Cancelled"}
CACHE_PATH = os.path.join(envconf.CACHE_DIR, "stats-activities.json")
STATUS_RX = re.compile(r"^Status (?:changed from (.+?) to (.+?)|set to (.+?))$")
_LOCK = threading.Lock()
_CACHE = None
_USERS = {}


def _parse_t(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _load_cache():
    global _CACHE
    if _CACHE is None:
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                _CACHE = json.load(f)
        except (OSError, ValueError):
            _CACHE = {}
    return _CACHE


def _save_cache():
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with _LOCK:
        data = json.dumps(_CACHE, ensure_ascii=False)
    with open(CACHE_PATH + ".tmp", "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(CACHE_PATH + ".tmp", CACHE_PATH)


def _uid(link):
    href = (link or {}).get("href") or ""
    return href.rstrip("/").split("/")[-1] if "/users/" in href else ""


def timeline(api, wp):
    """Các lần đổi trạng thái + comment Autofix của một ticket (cache theo updatedAt)."""
    key, upd = str(wp["id"]), wp.get("updatedAt") or ""
    cache = _load_cache()
    hit = cache.get(key)
    if hit and hit.get("u") == upd:
        return hit
    acts = api.get("/api/v3/work_packages/%s/activities" % key)
    trans, autofix = [], []
    for e in (acts.get("_embedded") or {}).get("elements") or []:
        at, user = e.get("createdAt") or "", _uid((e.get("_links") or {}).get("user"))
        for d in e.get("details") or []:
            m = STATUS_RX.match((d.get("raw") or "").strip())
            if m:
                trans.append([at, m.group(1) or "", m.group(2) or m.group(3), user])
        raw = ((e.get("comment") or {}).get("raw") or "").lstrip()
        if raw.startswith("**[Autofix]"):
            autofix.append([at, user])
    item = {"u": upd, "t": trans, "a": autofix}
    with _LOCK:
        cache[key] = item
    return item


def _user_name(api, uid):
    if uid and uid not in _USERS:
        try:
            _USERS[uid] = api.get("/api/v3/users/%s" % uid).get("name") or uid
        except Exception:  # noqa: BLE001 — không có quyền xem user
            _USERS[uid] = "user #%s" % uid
    return _USERS.get(uid, "")


def _work_packages(api, op_project, since):
    """Ticket của dự án có cập nhật từ `since` (YYYY-MM-DD)."""
    out, offset = [], 1
    until = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    flt = json.dumps([{"updatedAt": {"operator": "<>d", "values": [since, until]}},
                      {"status": {"operator": "*", "values": []}}])
    while True:
        d = api.get("/api/v3/projects/%s/work_packages" % op_project,
                    {"pageSize": 200, "offset": offset, "filters": flt, "sortBy": json.dumps([["updatedAt", "desc"]])})
        els = (d.get("_embedded") or {}).get("elements") or []
        out += els
        if len(out) >= int(d.get("total") or 0) or not els or offset > 25:
            return out
        offset += 1


def collect(days=30, scope="mine", project="", progress=None):
    """Số liệu thống kê. progress(done, total) được gọi trong lúc tải activities."""
    import op_autofix as af
    import op_dashboard as D
    import projects as P
    days = max(1, min(int(days or 30), 180))
    api, cfg = D.op_api()
    me = D.op_me_id(api)
    projs = [p for p in P.projects(cfg) if not project or p["key"] == project]
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # Lấy rộng hơn khoảng thống kê 1 ngày (lệch múi giờ); ticket được tính theo thời điểm đổi trạng thái.
    since = (start - timedelta(days=1)).strftime("%Y-%m-%d")
    wps = []
    for p in projs:
        for w in _work_packages(api, p["op_project"], since):
            wps.append((p, w))
    total, done_n = len(wps), [0]

    def one(pw):
        tl = timeline(api, pw[1])
        done_n[0] += 1
        if progress and done_n[0] % 10 == 0:
            progress(done_n[0], total)
        return pw, tl

    with ThreadPoolExecutor(max_workers=6) as ex:
        rows = list(ex.map(one, wps))
    _save_cache()

    dev_name = "Dev"
    local = datetime.now().astimezone().tzinfo
    day = lambda t: _parse_t(t).astimezone(local).strftime("%Y-%m-%d")
    series = {}
    d0 = start.astimezone(local).date()
    for i in range(days):
        series[(d0 + timedelta(days=i)).strftime("%Y-%m-%d")] = {"done": 0, "reopen": 0, "returned": 0, "autofix": 0}
    done_list, reopen_map, by_project, by_user, reopen_by, returned = [], {}, {}, {}, {}, []
    for (p, w), tl in rows:
        links = w.get("_links") or {}
        dev_key = af.dev_field_key(api, w, dev_name) if scope == "mine" else None
        mine_static = me and (_uid(links.get("assignee")) == me or (dev_key and _uid(links.get(dev_key)) == me))
        trans = sorted(tl["t"])
        resolved_by_me = any(t[2] in DONE and t[3] == me for t in trans)
        if scope == "mine" and not (mine_static or resolved_by_me):
            continue
        disp = w.get("displayId") or "%s-%s" % (p["prefix"], w["id"])
        base = {"id": w["id"], "display_id": disp, "subject": w.get("subject") or "", "project": p["name"],
                "status": (links.get("status") or {}).get("title") or ""}
        last_done = None
        for at, frm, to, user in trans:
            t = _parse_t(at)
            in_win = t >= start
            if to in DONE and frm not in DONE:
                last_done = (at, user)
                if in_win:
                    auto = any(abs((_parse_t(a[0]) - t).total_seconds()) <= 600 and a[1] == user for a in tl["a"])
                    series.setdefault(day(at), {"done": 0, "reopen": 0, "returned": 0, "autofix": 0})["done"] += 1
                    if auto:
                        series[day(at)]["autofix"] += 1
                    by_project.setdefault(p["name"], {"done": 0, "reopen": 0})["done"] += 1
                    by_user[user] = by_user.get(user, 0) + 1
                    done_list.append(dict(base, at=at, to=to, by=user, autofix=auto))
            elif to != "Reopened" and frm in DONE and to not in NOT_REOPEN:
                # Trả lại nội bộ: QA trên server trượt → UNREVIEWED, chờ Claude xem lại → TO DO, Review…
                if in_win:
                    series.setdefault(day(at), {"done": 0, "reopen": 0, "returned": 0, "autofix": 0})["returned"] += 1
                    returned.append(dict(base, at=at, frm=frm, to=to, by=user))
            elif to == "Reopened":
                if in_win:
                    series.setdefault(day(at), {"done": 0, "reopen": 0, "returned": 0, "autofix": 0})["reopen"] += 1
                    by_project.setdefault(p["name"], {"done": 0, "reopen": 0})["reopen"] += 1
                    r = reopen_map.setdefault(w["id"], dict(base, count=0, events=[], resolved_by=""))
                    r["count"] += 1
                    r["events"].append({"at": at, "from": frm, "to": to, "by": user})
                    r["resolved_by"] = (last_done or ("", ""))[1]
                    reopen_by[user] = reopen_by.get(user, 0) + 1
    done_ids = {d["id"] for d in done_list}
    reopened_after_done = {i for i, r in reopen_map.items() if i in done_ids or r["resolved_by"]}
    names = {u: _user_name(api, u) for u in set(by_user) | set(reopen_by) | {x["by"] for x in returned} |
             {r["resolved_by"] for r in reopen_map.values()} | {e["by"] for r in reopen_map.values() for e in r["events"]}
             if u}
    for d in done_list + returned:
        d["by_name"] = names.get(d["by"], "")
    for r in reopen_map.values():
        r["resolved_by_name"] = names.get(r["resolved_by"], "")
        for e in r["events"]:
            e["by_name"] = names.get(e["by"], "")
        r["last_at"] = max(e["at"] for e in r["events"])
    n_done = len(done_ids)
    return {
        "days": days, "scope": scope, "project": project, "from": start.isoformat(), "generated_at": time.time(),
        "op_url": cfg["url"], "scanned": total,
        "kpi": {"done": n_done, "done_events": len(done_list),
                "autofix": len({d["id"] for d in done_list if d["autofix"]}),
                "reopened": len(reopen_map), "reopen_events": sum(r["count"] for r in reopen_map.values()),
                "returned": len({x["id"] for x in returned}), "returned_events": len(returned),
                "reopen_rate": round(len(reopened_after_done & done_ids) / n_done, 4) if n_done else None},
        "series": [dict(v, date=k) for k, v in sorted(series.items()) if k >= d0.strftime("%Y-%m-%d")],
        "by_project": [dict(v, project=k) for k, v in sorted(by_project.items())],
        "by_user": sorted(({"name": names.get(u, u or "?"), "done": n} for u, n in by_user.items()), key=lambda x: -x["done"]),
        "reopen_by": sorted(({"name": names.get(u, u or "?"), "reopen": n} for u, n in reopen_by.items()), key=lambda x: -x["reopen"]),
        "reopens": sorted(reopen_map.values(), key=lambda r: (r["count"], r["last_at"]), reverse=True),
        "returned": sorted(returned, key=lambda d: d["at"], reverse=True)[:200],
        "done": sorted(done_list, key=lambda d: d["at"], reverse=True)[:300],
    }


# ---- chạy nền cho trang theo dõi: tính một lần, trả kết quả cache 10 phút ----
_JOBS = {}


def get(days=30, scope="mine", project="", refresh=False):
    key = "%s|%s|%s" % (days, scope, project)
    job = _JOBS.get(key)
    if job and job.get("running"):
        return {"building": True, "progress": job.get("progress")}
    if job and job.get("data") and not refresh and time.time() - job["data"]["generated_at"] < 600:
        return job["data"]
    if job and job.get("error") and not refresh and time.time() - job.get("at", 0) < 60:
        return {"error": job["error"]}
    job = _JOBS[key] = {"running": True, "progress": None, "data": (job or {}).get("data")}

    def run():
        try:
            job["data"] = collect(days, scope, project, lambda d, t: job.update(progress=[d, t]))
            job.pop("error", None)
        except Exception as ex:  # noqa: BLE001
            job["error"], job["at"] = str(ex), time.time()
        finally:
            job["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return {"building": True, "progress": None, "stale": job.get("data")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--scope", default="mine", choices=("mine", "all"))
    ap.add_argument("--project", default="")
    a = ap.parse_args()
    d = collect(a.days, a.scope, a.project, lambda x, t: print("\r%d/%d" % (x, t), end="", file=sys.stderr))
    print(file=sys.stderr)
    print(json.dumps({k: d[k] for k in ("kpi", "by_project", "by_user", "reopen_by")}, ensure_ascii=False, indent=1))
    for r in d["reopens"][:10]:
        print("REOPEN x%d %s %s — %s" % (r["count"], r["display_id"], r["subject"][:60], r["resolved_by_name"]))


if __name__ == "__main__":
    main()
