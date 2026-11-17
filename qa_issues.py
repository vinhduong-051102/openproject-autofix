#!/usr/bin/env python3
"""
Nhật ký sự cố QA test + QA server (deploy lên server test) để cải thiện tool các lần sau.

Mỗi sự cố là một dòng JSON trong <autofix_root>/qa-issues.jsonl:
  {"at": "...", "phase": "server" | "qa", "kind": "...", "cause": "...", "ticket": "SOAR-x" | null,
   "build": "...", "msg": "...", "path": "<log / thư mục QA>", "extra": {...}}

- phase=server: lỗi build / deploy / rollout / server không lên / server đổi giữa lúc QA.
- phase=qa: lỗi công cụ QA (Gemini treo, không trả kết quả, chưa đăng nhập), QA kết luận ĐẠT sai (bị hạ),
  Claude kết luận QA SAI (QA test sai cách), cần test tay, lỗi ngoại lệ trong lượt QA.

Dùng:
  python3 qa_issues.py                 # tổng hợp 14 ngày gần nhất ra màn hình
  python3 qa_issues.py --days 30 --md  # ghi báo cáo Markdown (mặc định ~/Downloads/autofix-reports/qa-issues.md)
  python3 qa_issues.py --backfill      # nạp sự cố cũ từ inbox + báo cáo autofix (chạy một lần, không nạp trùng)
Ghi sự cố lỗi (đĩa đầy, quyền…) không bao giờ làm hỏng lượt deploy / QA.
"""
import argparse
import fcntl
import glob
import json
import os
import re
import sys
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402

PATH = os.path.join(envconf.autofix_root(), "qa-issues.jsonl")
_DEFAULT_PATH = PATH
REPORT = os.path.expanduser("~/Downloads/autofix-reports/qa-issues.md")
KEEP_DAYS = 180
_mu = threading.Lock()

# kind → (nhóm, mô tả, gợi ý cải thiện)
KINDS = {
    # ---- server ----
    "merge_conflict": ("server", "Merge nhánh deploy bị xung đột",
                       "Thêm luật tự gộp cho file hay xung đột (vd db/master*.xml) hoặc báo người merge sớm hơn."),
    "deploy_fe": ("server", "Build / deploy FE lỗi",
                  "Xem log deploy-*.log: lỗi build (tsc/vite) → chặn ở bước autofix verify; lỗi docker/ssh → kiểm tra máy build."),
    "deploy_be": ("server", "Build / deploy BE lỗi",
                  "Xem log deploy-*.log: mvn lỗi → chặn ở bước verify trước khi push; thiếu biến môi trường → bổ sung .env."),
    "rollout": ("server", "Rollout (đổi container) lỗi",
                "Kiểm tra healthcheck / thời gian chờ container mới, cấu hình nginx upstream."),
    "deploy_custom": ("server", "Deploy repo tuỳ chỉnh lỗi", "Xem log deploy của repo tuỳ chỉnh."),
    "registry_auth": ("server", "Tài khoản registry bị từ chối (kiểm tra trước khi build)",
                      "Mật khẩu registry đã đổi: chạy `docker login <registry>` và sửa GIT2_PASSWORD trong ~/.git2.env "
                      "(cả hai cùng tài khoản)."),
    "deploy_wait_qa_timeout": ("server", "Deploy chờ lượt QA đang chạy quá lâu",
                               "Lượt QA chờ theo mốc thời gian dài / treo: giảm qa_wait_total_minutes hoặc tăng "
                               "deploy_wait_qa_minutes."),
    "server_not_up": ("server", "Server không lên sau deploy",
                      "Tăng wait_minutes nếu server chỉ chậm; nếu BE crash → đọc docker logs BE, thêm kiểm tra migration."),
    "server_down": ("server", "Server không trả lời trước khi QA",
                    "Kiểm tra server 86 / mạng / chứng chỉ; cân nhắc tự khởi động lại container."),
    "fe_version_unreadable": ("server", "Không đọc được version FE trên server",
                              "Kiểm tra thẻ meta version trong index.html của bản build."),
    "fe_drift": ("server", "FE trên server lệch bản mới nhất",
                 "Lần deploy trước dở dang (build xong mà chưa rollout) — xem sự cố deploy ngay trước đó."),
    "server_changed": ("server", "Server bị đổi / restart giữa lúc QA",
                       "Có người deploy tay hoặc container tự restart; báo nhóm tránh deploy tay trong khung QA."),
    # ---- qa ----
    "gemini_timeout": ("qa", "Gemini không trả lời (treo)",
                       "Tăng timeout mỗi vòng hoặc giảm số request mỗi vòng; xem hạn mức Gemini."),
    "gemini_empty": ("qa", "Gemini không trả kết quả / JSON hỏng",
                     "Xem stderr trong gemini-rounds.jsonl; thường do hạn mức hoặc lỗi CLI tạm thời."),
    "gemini_no_login": ("qa", "Gemini chưa đăng nhập", "Đăng nhập lại agy trên máy chạy tool."),
    "gemini_error": ("qa", "Gemini lỗi khi chạy", "Xem thông báo lỗi; kiểm tra agy còn cài / đúng đường dẫn."),
    "gemini_retry": ("qa", "Gemini phải thử lại một vòng", "Nhiều lần → cân nhắc tăng timeout / giảm tải song song."),
    "qa_engine_error": ("qa", "Claude QA lỗi khi chạy", "Xem claude.err trong thư mục QA; hạn mức gói Claude."),
    "qa_no_report": ("qa", "QA không ghi báo cáo (qa.md)", "Prompt QA / công cụ dừng giữa chừng — xem log thư mục QA."),
    "qa_false_pass": ("qa", "QA kết luận ĐẠT nhưng bị hạ (HTTP 5xx / lỗi log BE / thiếu request)",
                      "Bổ sung rule QA: phải đọc mã HTTP, không kết luận ĐẠT khi có 5xx hoặc chưa gọi API đã sửa."),
    "qa_wrong": ("qa", "Claude kết luận QA SAI (QA test sai cách)",
                 "Rule đã thêm vào qa-rules.md — xem lại rule có đủ tổng quát, xoá rule trùng / mâu thuẫn."),
    "qa_fix_not_deployed": ("qa", "QA theo yêu cầu khi server chưa có bản sửa (không tính)",
                            "Đợi deploy bản có bản sửa rồi mới bấm QA trên server; kết quả này không tính vào bộ đếm."),
    "qa_manual": ("qa", "Cần test tay (QA tự động không làm được)",
                  "Xem có thể bổ sung công cụ (UI, tài khoản, dữ liệu mẫu) để lần sau tự test được."),
    "qa_tool_retry_exhausted": ("qa", "Lỗi công cụ QA quá số lần thử trên một bản build",
                                "Xem các sự cố gemini_* cùng ticket; có thể ticket gây treo công cụ (trang nặng, request lớn)."),
    "qa_exception": ("qa", "Lỗi ngoại lệ trong lượt QA ticket", "Lỗi code của tool — xem traceback trong extra."),
    "deploy_check_error": ("qa", "Không kiểm tra được bản deploy khi QA KHÔNG ĐẠT", "Xem lỗi trong extra."),
    "be_log_unreadable": ("qa", "Không đọc được log BE khi kiểm tra kết luận ĐẠT",
                          "Kiểm tra ssh / docker logs trên server; tên container BE trong deploy.json."),
    "other": ("qa", "Khác", ""),
}

# Nguyên nhân gốc đoán từ nội dung lỗi (để gom nhóm "vì sao")
CAUSES = [
    (r"unauthorized|authGroup\.Verify|Không đọc được registry|registry bị từ chối|docker login", "registry_auth"),
    ("xung đột", "merge_conflict"),
    (r"Thiếu [A-Z_]+|biến môi trường|\.env", "config_missing"),
    (r"npm run build|vite|tsc|TS\d{4}", "fe_build"),
    (r"mvn|BUILD FAILURE|COMPILATION ERROR|Tests run:.*Failures: [1-9]", "be_build"),
    (r"docker|container|compose", "docker"),
    (r"sshpass|ssh|scp|Connection (refused|reset|timed out)", "ssh_network"),
    (r"không trả lời sau|timeout|timed out|Timeout", "timeout"),
    (r"hạn mức|quota|rate.?limit|429|RESOURCE_EXHAUSTED", "quota"),
    (r"đăng nhập|login|401|auth", "auth"),
    (r"HTTP 5\d\d|\b5\d\d\b|5xx", "http_5xx"),
    (r"nginx|upstream", "nginx"),
]


def classify(msg):
    for rx, cause in CAUSES:
        if re.search(rx, msg or "", re.I):
            return cause
    return ""


def record(kind, msg, ticket=None, build=None, path=None, phase=None, at=None, **extra):
    """Ghi một sự cố. Không bao giờ ném lỗi ra ngoài.
    Đang chạy unittest mà chưa đổi PATH sang file tạm → không ghi (test giả lập lỗi không được lẫn vào nhật ký thật)."""
    if PATH == _DEFAULT_PATH and ("unittest" in sys.modules or os.environ.get("QA_ISSUES_OFF") == "1"):
        return
    try:
        first = (str(msg).strip().splitlines() or [""])[0][:500]
        item = {"at": at or datetime.now().isoformat(timespec="seconds"),
                "phase": phase or KINDS.get(kind, KINDS["other"])[0], "kind": kind if kind in KINDS else "other",
                "cause": "" if kind in ("qa_wrong", "qa_manual") else classify(str(msg)), "ticket": ticket, "build": build, "msg": first,
                "path": path}
        extra = {k: (v[-3000:] if isinstance(v, str) else v) for k, v in extra.items() if v not in (None, "", [], {})}
        if extra:
            item["extra"] = extra
        line = json.dumps(item, ensure_ascii=False) + "\n"
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with _mu, open(PATH, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line)
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001 — ghi nhật ký không được làm hỏng deploy / QA
        pass


def load(days=None):
    items = []
    try:
        with open(PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    items.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    if days:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        items = [i for i in items if i.get("at", "") >= since]
    return items


def prune(keep_days=KEEP_DAYS):
    """Bỏ sự cố cũ hơn keep_days ngày."""
    items = load()
    since = (datetime.now() - timedelta(days=keep_days)).isoformat(timespec="seconds")
    keep = [i for i in items if i.get("at", "") >= since]
    if len(keep) == len(items):
        return 0
    with _mu, open(PATH + ".tmp", "w", encoding="utf-8") as f:
        f.writelines(json.dumps(i, ensure_ascii=False) + "\n" for i in keep)
    os.replace(PATH + ".tmp", PATH)
    return len(items) - len(keep)


def summary_md(days=14):
    items = load(days)
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    out = ["# Sự cố QA test / QA server — %d ngày gần nhất" % days, "",
           "_Cập nhật %s · %d sự cố · nguồn `%s`_" % (now, len(items), PATH), ""]
    if not items:
        return "\n".join(out + ["Không có sự cố."]) + "\n"
    by_kind = defaultdict(list)
    for i in items:
        by_kind[i["kind"]].append(i)
    for phase, title in (("server", "QA server (deploy / server test)"), ("qa", "QA test")):
        kinds = sorted((k for k in by_kind if KINDS.get(k, KINDS["other"])[0] == phase),
                       key=lambda k: -len(by_kind[k]))
        if not kinds:
            continue
        out += ["## %s" % title, "", "| Loại | Số lần | Lần cuối | Nguyên nhân | Ticket |", "|---|---|---|---|---|"]
        for k in kinds:
            rows = by_kind[k]
            causes = Counter(r.get("cause") or "?" for r in rows).most_common(3)
            tickets = Counter(r["ticket"] for r in rows if r.get("ticket")).most_common(5)
            out.append("| %s (`%s`) | %d | %s | %s | %s |" % (
                KINDS.get(k, KINDS["other"])[1], k, len(rows), max(r["at"] for r in rows)[:16].replace("T", " "),
                ", ".join("%s×%d" % c for c in causes), ", ".join("%s×%d" % t for t in tickets) or "—"))
        out.append("")
        for k in kinds:
            rows = sorted(by_kind[k], key=lambda r: r["at"], reverse=True)
            hint = KINDS.get(k, KINDS["other"])[2]
            out += ["### %s — %d lần" % (KINDS.get(k, KINDS["other"])[1], len(rows))]
            if hint:
                out.append("**Gợi ý cải thiện:** %s" % hint)
            out.append("")
            seen = set()
            for r in rows:
                key = re.sub(r"\d+", "#", r.get("msg", ""))[:160]
                if key in seen:
                    continue
                seen.add(key)
                out.append("- %s %s%s%s" % (r["at"][:16].replace("T", " "), "**%s** " % r["ticket"] if r.get("ticket") else "",
                                           r.get("msg", "").replace("|", "\\|")[:300],
                                           " · `%s`" % r["path"] if r.get("path") else ""))
                if len(seen) >= 8:
                    break
            out.append("")
    return "\n".join(out) + "\n"


def write_report(days=14, path=REPORT):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(summary_md(days))
        return path
    except OSError:
        return None


# ---- nạp sự cố cũ ---------------------------------------------------------------------------------------------
_INBOX_RX = re.compile(r"^- \[[ x]\] (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \*\*(DEPLOY 86 LỖI|QA SERVER LỖI CÔNG CỤ|QA SERVER LỖI|"
                       r"QA SERVER TẠM DỪNG)\*\* (.*)$")


def _inbox_kind(tag, text):
    if tag == "QA SERVER TẠM DỪNG":
        return "server_changed"
    if tag == "QA SERVER LỖI CÔNG CỤ":
        return "qa_tool_retry_exhausted"
    if tag == "QA SERVER LỖI":
        return "qa_exception"
    if "xung đột" in text:
        return "merge_conflict"
    if "chưa lên sau" in text:
        return "server_not_up"
    if "không trả lời" in text or re.search(r"urlopen error|timed out", text):
        return "server_down"
    if re.search(r"mvn|-DskipTests|BUILD FAILURE", text):
        return "deploy_be"
    if "không đọc được version FE" in text:
        return "fe_version_unreadable"
    if "FE " in text[:40] or "FE build" in text:
        return "deploy_fe"
    if "BE " in text[:40] or "BE build" in text:
        return "deploy_be"
    if "rollout" in text:
        return "rollout"
    return "deploy_be" if "sshpass" in text or "bash" in text else "rollout" if "docker" in text else "other"


def backfill(inbox_path=os.path.expanduser("~/op-watch-inbox.md")):
    have = {(i.get("at", "")[:16], i.get("msg", "")[:80]) for i in load()}
    n = 0
    try:
        lines = open(inbox_path, encoding="utf-8").read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        m = _INBOX_RX.match(line)
        if not m:
            continue
        at = m.group(1).replace(" ", "T") + ":00"
        text = m.group(3)
        if (at[:16], text[:80]) in have:
            continue
        tk = re.search(r"\b(SOAR-\d+)\b", text)
        logm = re.search(r"\(log ([^)]+)\)", text)
        record(_inbox_kind(m.group(2), text), text, ticket=tk.group(1) if tk else None, at=at,
               path=logm.group(1) if logm else None, source="inbox")
        have.add((at[:16], text[:80]))
        n += 1
    # Claude kết luận QA SAI / CẦN TEST TAY sau QA server (báo cáo autofix event QA_FAIL)
    for rep in glob.glob(os.path.join(envconf.autofix_root(), "*", "report.md")):
        try:
            head = open(rep, encoding="utf-8").read(4000)
        except OSError:
            continue
        m = re.match(r"KẾT QUẢ:\s*(QA SAI|CẦN TEST TAY)", head)
        if not m:
            continue
        at = datetime.fromtimestamp(os.path.getmtime(rep)).isoformat(timespec="seconds")
        run = os.path.basename(os.path.dirname(rep))
        tk = re.match(r"(SOAR-\d+)", run)
        reason = re.search(r"## (?:Nguyên nhân|Kết quả|Tóm tắt)[^\n]*\n+([^\n]+)", head)
        msg = "%s: %s" % (m.group(1), reason.group(1).strip() if reason else run)
        if (at[:16], msg[:80]) in have:
            continue
        record("qa_wrong" if m.group(1) == "QA SAI" else "qa_manual", msg, ticket=tk.group(1) if tk else None,
               at=at, path=os.path.dirname(rep), source="report")
        have.add((at[:16], msg[:80]))
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="Tổng hợp sự cố QA test / QA server")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--md", nargs="?", const=REPORT, help="ghi báo cáo Markdown (mặc định %s)" % REPORT)
    ap.add_argument("--backfill", action="store_true", help="nạp sự cố cũ từ inbox + báo cáo autofix")
    ap.add_argument("--prune", action="store_true", help="xoá sự cố cũ hơn %d ngày" % KEEP_DAYS)
    a = ap.parse_args()
    if a.backfill:
        print("đã nạp %d sự cố cũ" % backfill())
    if a.prune:
        print("đã xoá %d sự cố cũ" % prune())
    if a.md:
        print("đã ghi %s" % write_report(a.days, a.md))
    else:
        sys.stdout.write(summary_md(a.days))


if __name__ == "__main__":
    main()
