#!/usr/bin/env python3
"""
backfill_time: bổ sung trường Dev + giờ làm / test cho các việc autofix đã làm TRƯỚC khi có tính năng ghi tự động.

Nguồn: log autofix / deploy-qa (journal op-watch + op-merge, và các file .ship-* / .recover-* / .dashboard-* /
review-watch trong thư mục autofix). Mỗi lượt chạy:
  - Development = "chạy Claude" → bước kế tiếp của ticket (Claude viết xong báo cáo)
  - Testing     = "kiểm tra lại + push" / "giao lại" → dòng kết luận cuối của lượt
  - Testing (QA server) = bắt đầu QA (tên thư mục <SOAR>-qa-MMDD-HHMM, không có thì lúc bắt đầu đợt QA, tối đa
    8 phút, ghi "ước lượng") → dòng kết luận QA
Ghi đúng NGÀY của lượt. Mỗi bản ghi có khoá lượt chạy trong ghi chú → chạy lại không ghi trùng.
Dev: điền mình nếu trống (ticket có ít nhất một lượt Claude). Start / Finish date: ngày lượt đầu / cuối nếu trống.

  python3 backfill_time.py                 # chỉ in kế hoạch
  python3 backfill_time.py --apply         # ghi lên OpenProject
  python3 backfill_time.py SOAR-2834 --apply
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402
import op_autofix as af  # noqa: E402
import op_watch  # noqa: E402

LINE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] \[(autofix|deploy-qa)\] (%s)(?: \([A-Z_]+\))?(:| —) (.*)$" % envconf.TICKET_RX)
QA_END = re.compile(r"^(ĐẠT|KHÔNG ĐẠT|KHÔNG TỰ TEST ĐƯỢC|KHÔNG RÕ YÊU CẦU)\b")
CLAUDE_CONT = re.compile(r"(chạy tiếp|đổi gói|hết hạn mức|làm tiếp|chuyển sang gói)", re.I)
DEV_CAP = 30 * 60  # một lượt Claude sửa thường 1–15 phút; dài hơn 30 phút là lượt bị ngắt (gồm cả lúc máy tắt)
QA_CAP = 8 * 60  # QA không còn thư mục (không biết lúc bắt đầu riêng): tính từ đầu đợt QA, tối đa 8 phút (đo thật 1–7 phút)


def read_lines():
    """Mọi dòng log autofix / deploy-qa (bỏ trùng), sắp theo thời gian."""
    out = set()
    try:
        res = subprocess.run(["journalctl", "--user", "-u", "op-watch", "-u", "op-merge", "-o", "cat", "--no-pager"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True, errors="replace")
        out.update(l.strip() for l in res.stdout.splitlines())
    except OSError:
        pass
    root = envconf.autofix_root()
    for pat in (".ship-*.log", ".recover-*.log", ".dashboard-*.log", ".review-watch.log", "rerun-*.log", "review-*.log",
                ".requeue-*.log", "deploy-qa-run.log", "*-qa-*/autofix.log"):
        for p in glob.glob(os.path.join(root, pat)):
            with open(p, encoding="utf-8", errors="replace") as f:
                out.update(l.strip() for l in f)
    # Lệnh chạy tay trong Claude Code (vd `! python3 op_autofix.py --ship …`) ghi output vào thư mục task tạm.
    for p in glob.glob("/tmp/claude-*/*/*/tasks/*.output"):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                out.update(l.strip() for l in f)
        except OSError:
            pass
    rows = []
    for l in out:
        m = LINE.match(l)
        if m:
            rows.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"), m.group(2), m.group(3),
                         m.group(4) == " —", m.group(5)))
    rows.sort(key=lambda r: r[0])
    return rows


def autofix_segments(rows):
    """[(ticket, kind, start, end, key, note)] từ các dòng [autofix]."""
    segs, by_t = [], {}
    for r in rows:
        if r[1] == "autofix":
            by_t.setdefault(r[2], []).append(r)
    for t, rs in by_t.items():
        run = None
        for i, (ts, _, _, final, msg) in enumerate(rs):
            m = re.match(r"tải ticket, tạo worktree .*/(%s-\d{4}-\d{4})" % envconf.TICKET_RX, msg)
            if m:
                run = m.group(1)
            g = re.match(r"giao lại (%s-[\w-]+)" % envconf.TICKET_RX, msg)
            if g:
                run = g.group(1)
            if msg.startswith("chạy Claude"):
                end = next((x[0] for x in rs[i + 1:] if not CLAUDE_CONT.search(x[4])), None)
                # Lượt bị ngắt (máy khởi động lại) thì dòng kế tiếp là lúc giao lại → gồm cả lúc máy tắt: chặn trần.
                if end and (end - ts).total_seconds() > DEV_CAP:
                    end = ts + timedelta(seconds=DEV_CAP)
                if end and (end - ts).total_seconds() < 6 * 3600:
                    segs.append((t, "dev", ts, end, "%s@%s" % (run or t, ts.strftime("%m%d-%H%M")),
                                 "[Autofix] Claude sửa (%s) — bổ sung từ log" % (run or t)))
            starts_test = msg.startswith("kiểm tra lại + push") or bool(g) or msg.startswith("test lại")
            # "Gemini test lại" chỉ mở đoạn Testing khi không ngay sau "kiểm tra lại + push" / "giao lại" (đã tính)
            if not starts_test and msg == "Gemini test lại":
                starts_test = not any(x[4].startswith(("kiểm tra lại + push", "giao lại", "test lại"))
                                      and 0 <= (ts - x[0]).total_seconds() < 3600 for x in rs[max(0, i - 6):i])
            if starts_test:
                # Kết thúc: dòng kết luận "SOAR-x — …" (lượt tự động), không có (vd --ship chạy tay chỉ in kết luận
                # trơn) thì dòng log cuối của lượt — chuỗi dòng cách nhau ≤ 40 phút, trước lượt mới.
                end, prev = None, ts
                for x in rs[i + 1:]:
                    if re.match(r"(tải ticket|chờ tới lượt|giao lại)", x[4]) or (x[0] - prev).total_seconds() > 2400:
                        break
                    end, prev = x[0], x[0]
                    if x[3]:
                        break
                if end and (end - ts).total_seconds() < 4 * 3600:
                    segs.append((t, "test", ts, end, "%s@%s" % (run or t, ts.strftime("%m%d-%H%M")),
                                 "[Autofix] Kiểm tra build/test + Gemini test lại (%s) — bổ sung từ log" % (run or t)))
    return segs


def qa_segments(rows):
    root = envconf.autofix_root()
    segs, batch = [], None
    for ts, src, t, _, msg in rows:
        if src != "deploy-qa":
            continue
        if msg.startswith("dấu server") or msg.startswith("QA theo yêu cầu") or msg.startswith("vừa chuyển Review"):
            batch = ts if msg.startswith("dấu server") or batch is None or (ts - batch).total_seconds() > 3600 else batch
        m = QA_END.match(msg)
        if not m or "lỗi công cụ QA" in msg:
            continue
        start, est = None, False
        for d in glob.glob(os.path.join(root, "%s-qa-*" % t)):
            dm = re.search(r"-qa-(\d{4}-\d{4})$", d)
            if dm:
                st = datetime.strptime("%d%s" % (ts.year, dm.group(1)), "%Y%m%d-%H%M")
                if timedelta(0) <= ts - st < timedelta(hours=2) and (start is None or st > start):
                    start = st
        if start is None and batch and timedelta(0) <= ts - batch < timedelta(hours=2):
            start, est = max(batch, ts - timedelta(seconds=QA_CAP)), True
        if start:
            segs.append((t, "test", start, ts, "qa@%s" % ts.strftime("%m%d-%H%M"),
                         "[QA tự động] Gemini QA trên server test — %s — bổ sung từ log%s" % (
                             msg[:80], " (ước lượng)" if est else "")))
    return segs


def existing_keys(api, wp):
    href = ((wp.get("_links") or {}).get("timeEntries") or {}).get("href")
    if not href:
        return ""
    try:
        data = api.get(href + ("&" if "?" in href else "?") + "pageSize=500")
    except Exception:  # noqa: BLE001
        return ""
    return "\n".join(((e.get("comment") or {}).get("raw") or "") for e in data.get("_embedded", {}).get("elements", []))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickets", nargs="*")
    ap.add_argument("--apply", action="store_true", help="ghi lên OpenProject (mặc định chỉ in kế hoạch)")
    args = ap.parse_args()
    cfg = op_watch.load_config(argparse.Namespace(config=None, interval=None, on_change=None, no_notify=True))
    acfg = dict(af.AUTOFIX_DEFAULTS)
    acfg.update(cfg.get("autofix") or {})
    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    rows = read_lines()
    segs = [s for s in autofix_segments(rows) + qa_segments(rows) if (s[3] - s[2]).total_seconds() >= 30]
    if args.tickets:
        segs = [s for s in segs if s[0] in args.tickets]
    tickets = sorted({s[0] for s in segs}, key=lambda x: int(x.split("-")[1]))
    print("%d ticket, %d lượt (Development %d, Testing %d)%s" % (
        len(tickets), len(segs), sum(1 for s in segs if s[1] == "dev"), sum(1 for s in segs if s[1] == "test"),
        "" if args.apply else " — CHỈ IN KẾ HOẠCH, thêm --apply để ghi"))
    tot = {"dev": 0, "test": 0, "new": 0, "skip": 0}
    for t in tickets:
        mine = sorted((s for s in segs if s[0] == t), key=lambda s: s[2])
        try:
            wp = api.get("/api/v3/work_packages/%s" % t)
        except Exception as ex:  # noqa: BLE001
            print("%s: không đọc được (%s)" % (t, str(ex)[:80]))
            continue
        have = existing_keys(api, wp)
        dev_m = sum(int(round((s[3] - s[2]).total_seconds() / 60.0)) for s in mine if s[1] == "dev")
        test_m = sum(int(round((s[3] - s[2]).total_seconds() / 60.0)) for s in mine if s[1] == "test")
        todo = [s for s in mine if "[%s]" % s[4] not in have]
        print("%s (%s): %d lượt, Development %d phút, Testing %d phút · cần ghi %d, đã có %d" % (
            t, ((wp.get("_links") or {}).get("status") or {}).get("title"), len(mine), dev_m, test_m, len(todo),
            len(mine) - len(todo)))
        tot["skip"] += len(mine) - len(todo)
        if not args.apply:
            for s in todo:
                print("   %s %s %s→%s  %d phút  %s" % (s[2].strftime("%d/%m"), s[1].ljust(4), s[2].strftime("%H:%M"),
                                                       s[3].strftime("%H:%M"), round((s[3] - s[2]).total_seconds() / 60),
                                                       s[5][:70]))
            continue
        # Dev + ngày bắt đầu / kết thúc (chỉ khi trống)
        if any(s[1] == "dev" for s in mine):
            print("   Dev:", af.set_dev(api, dict(acfg, set_dates=False), t))
        try:
            w = api.get("/api/v3/work_packages/%s" % t)
            body = {"lockVersion": w["lockVersion"]}
            first, last = mine[0][2].strftime("%Y-%m-%d"), mine[-1][3].strftime("%Y-%m-%d")
            if acfg.get("set_dates", True) and not w.get("startDate") and not (w.get("dueDate") and w["dueDate"] < first):
                body["startDate"] = first
            if acfg.get("set_dates", True) and not w.get("dueDate") and last >= (body.get("startDate") or w.get("startDate") or last):
                body["dueDate"] = last
            if len(body) > 1:
                af.op_write(api, "PATCH", "/api/v3/work_packages/%s" % w["id"], body)
                print("   ngày:", {k: v for k, v in body.items() if k != "lockVersion"})
        except Exception as ex:  # noqa: BLE001
            print("   ngày: lỗi %s" % str(ex)[:120])
        for s in todo:
            act = af.time_activity(api, wp["id"], acfg.get("time_activity_dev" if s[1] == "dev" else "time_activity_test"))
            minutes = max(1, int(round((s[3] - s[2]).total_seconds() / 60.0)))
            body = {"hours": "PT%dM" % minutes, "spentOn": s[2].strftime("%Y-%m-%d"),
                    "comment": {"raw": ("%s [%s]" % (s[5], s[4]))[:250]},
                    "_links": {"entity": {"href": "/api/v3/work_packages/%s" % wp["id"]}}}
            if act:
                body["_links"]["activity"] = {"href": act}
            try:
                af.op_write(api, "POST", "/api/v3/time_entries", body)
                tot[s[1]] += minutes
                tot["new"] += 1
            except Exception as ex:  # noqa: BLE001
                print("   ghi giờ lỗi %s: %s" % (s[4], str(ex)[:120]))
    if args.apply:
        print("Đã ghi %d bản ghi (Development %d phút, Testing %d phút), bỏ qua %d bản đã có." % (
            tot["new"], tot["dev"], tot["test"], tot["skip"]))


if __name__ == "__main__":
    main()
