#!/usr/bin/env python3
"""
daily_report: báo cáo công việc hằng ngày (thứ 2 – thứ 6) vào $DAILY_REPORT_DIR/bao-cao-DD-MM-YYYY.txt
(mặc định ~/Downloads/bao-cao-ngay)

    Ngày: 30/10/2026
    1. Hôm qua đã làm gì?
    - [SOAR-xxxx] - <tiêu đề>
    2. Hôm nay làm gì ?
    - [SOAR-yyyy] - <tiêu đề>

- "Hôm qua": chép nguyên mục "Hôm nay" của báo cáo ngày làm việc trước. Chưa có báo cáo trước → các ticket SOAR mình
  có thao tác (comment / đổi trạng thái…) trong ngày làm việc trước, tối đa 5, mới nhất trước.
- "Hôm nay": tối đa 5 việc mới nhất đang giao cho mình (New / Reopened / TO DO / In progress / UNREVIEWED, cập nhật gần
  nhất trước); thiếu thì thêm các ticket mình vừa Resolved gần nhất chưa có trong "Hôm qua".

    daily_report.py            # tạo báo cáo hôm nay (đã có thì thôi; thứ 7, CN thì bỏ qua)
    daily_report.py --force    # tạo lại
    daily_report.py --date 2026-11-03 --print
Chạy tự động bằng op-report.timer (8:00 thứ 2 – thứ 6, máy tắt lúc đó thì chạy khi bật máy).
"""
import argparse
import glob
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import op_autofix as af  # noqa: E402
import op_watch  # noqa: E402

import envconf  # noqa: E402

OUT_DIR = envconf.path("DAILY_REPORT_DIR", "~/Downloads/bao-cao-ngay")
PROJECT = envconf.get("OP_PROJECT") or str(envconf._read_json(envconf.CONFIG_PATH).get("project") or "223")
MAX_ITEMS = int(envconf.get("DAILY_REPORT_MAX_ITEMS", "5"))
# New, Reopened, TO DO, In progress, UNREVIEWED
OPEN_STATUSES = envconf._list(envconf.get("DAILY_REPORT_OPEN_STATUSES", "1,100,97,7,94"))
RESOLVED = envconf.get("OP_STATUS_RESOLVED", "95")
FILE_RX = re.compile(r"bao-cao-(\d\d)-(\d\d)-(\d{4})\.txt$")


def report_path(day):
    return os.path.join(OUT_DIR, "bao-cao-%s.txt" % day.strftime("%d-%m-%Y"))


def prev_workday(day):
    d = day - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def line(wp):
    return "- [%s] - %s" % (wp.get("displayId") or "WP-%s" % wp["id"], wp.get("subject", "").strip())


def previous_today_section(day):
    """Mục "Hôm nay" của báo cáo gần nhất trước `day` (None nếu chưa có báo cáo nào)."""
    best = None
    for path in glob.glob(os.path.join(OUT_DIR, "bao-cao-*.txt")):
        m = FILE_RX.search(path)
        if not m:
            continue
        d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if d < day and (best is None or d > best[0]):
            best = (d, path)
    if not best:
        return None
    with open(best[1], encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"^2\. Hôm nay làm gì ?\?\s*\n(.*)", text, re.M | re.S)
    items = [ln.rstrip() for ln in (m.group(1) if m else "").splitlines() if ln.startswith("- ")]
    return items


def query(api, filters, sort, size):
    data = api.get("/api/v3/projects/%s/work_packages" % PROJECT, {
        "filters": json.dumps(filters), "sortBy": json.dumps(sort), "pageSize": size})
    return data.get("_embedded", {}).get("elements", [])


def worked_on(api, me, day):
    """Ticket SOAR mình có thao tác trong ngày `day`, mới nhất trước."""
    start, end = day.isoformat(), (day + timedelta(days=1)).isoformat()
    cands = query(api, [{"updatedAt": {"operator": "<>d", "values": [start, end]}}], [["updatedAt", "desc"]], 100)
    out = []
    for wp in cands:
        acts = api.get("/api/v3/work_packages/%s/activities" % wp["id"]).get("_embedded", {}).get("elements", [])
        last = None
        for a in acts:
            user = (a.get("_links", {}).get("user") or {}).get("href", "")
            if user.endswith("/%s" % me) and start <= (a.get("createdAt") or "")[:10] < end:
                last = max(last or "", a["createdAt"])
        if last:
            out.append((last, wp))
        if len(out) >= MAX_ITEMS:
            break
    return [wp for _, wp in sorted(out, key=lambda x: x[0], reverse=True)]


def today_items(api, exclude):
    mine = {"assignee": {"operator": "=", "values": ["me"]}}
    items = query(api, [mine, {"status": {"operator": "=", "values": OPEN_STATUSES}}], [["updatedAt", "desc"]],
                  MAX_ITEMS)
    lines = [line(wp) for wp in items]
    if len(lines) < MAX_ITEMS:
        for wp in query(api, [mine, {"status": {"operator": "=", "values": [RESOLVED]}}], [["updatedAt", "desc"]], 20):
            ln = line(wp)
            if ln not in lines and ln not in exclude:
                lines.append(ln)
            if len(lines) >= MAX_ITEMS:
                break
    return lines


def wait_network(url, seconds=300):
    """Chạy ngay khi bật máy: chờ mạng / DNS tới máy chủ OpenProject (tối đa `seconds`)."""
    import socket
    import time
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname
    deadline = time.time() + seconds
    while host and time.time() < deadline:
        try:
            socket.getaddrinfo(host, 443)
            return
        except OSError:
            time.sleep(10)


def build(day):
    cfg = op_watch.load_config(argparse.Namespace(config=None, interval=None, on_change=None, no_notify=True))
    wait_network(cfg["url"])
    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    me = api.get("/api/v3/users/me")["id"]
    yesterday = previous_today_section(day)
    if yesterday is None:
        yesterday = [line(wp) for wp in worked_on(api, me, prev_workday(day))]
    today = today_items(api, set(yesterday))
    text = "Ngày: %s\n1. Hôm qua đã làm gì?\n%s\n2. Hôm nay làm gì ?\n%s\n" % (
        day.strftime("%d/%m/%Y"), "\n".join(yesterday) or "- (không có)", "\n".join(today) or "- (không có)")
    return cfg, text


def main():
    parser = argparse.ArgumentParser(description="Tạo báo cáo công việc hằng ngày vào %s" % OUT_DIR)
    parser.add_argument("--date", help="YYYY-MM-DD (mặc định hôm nay)")
    parser.add_argument("--force", action="store_true", help="tạo lại dù đã có / dù là cuối tuần")
    parser.add_argument("--print", action="store_true", help="in nội dung ra màn hình")
    args = parser.parse_args()
    day = date.fromisoformat(args.date) if args.date else date.today()
    path = report_path(day)
    if not args.force:
        if day.weekday() >= 5:
            print("cuối tuần, không tạo báo cáo")
            return
        if os.path.exists(path):
            print("đã có %s" % path)
            return
    cfg, text = build(day)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print("[%s] đã tạo %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), path))
    if args.print:
        print(text)
    af.inbox(cfg, "**BÁO CÁO NGÀY** %s · %s" % (day.strftime("%d/%m/%Y"), path))
    af.notify("[BÁO CÁO NGÀY] %s" % day.strftime("%d/%m/%Y"), path)


if __name__ == "__main__":
    main()
