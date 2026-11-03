#!/usr/bin/env python3
"""
op-watch: theo dõi OpenProject, báo khi có bug cần sửa.

Mỗi lượt (mặc định 2 phút) lấy các work package của dự án được giao cho mình ở các trạng thái
cần làm (mặc định New, Reopened) và báo khi:
  - NEW       : có việc mới vào danh sách (bug mới tạo / mới được giao cho mình)
  - REOPENED  : việc chuyển sang Reopened
  - COMMENT   : có comment mới của người khác trên việc đang theo dõi
Báo bằng: dòng log, thông báo desktop (notify-send), file inbox markdown, và lệnh tuỳ chọn (--on-change).

Cấu hình: file .env cạnh script (mẫu .env.example) và/hoặc ~/.config/op-watch/config.json (mẫu config.example.json);
biến môi trường > .env > config.json. Trạng thái lưu ở ~/.local/state/op-watch/state.json (OP_WATCH_STATE).
"""

import argparse
import base64
import html
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402  (nạp .env; biến môi trường > .env > config.json > mặc định)

CONFIG_PATH = envconf.CONFIG_PATH
STATE_PATH = envconf.STATE_PATH
DEFAULT_INBOX = os.path.expanduser("~/op-watch-inbox.md")

DEFAULTS = {
    "url": "https://openproject.example.com",  # đặt OP_URL trong .env
    "api_key": "",
    "project": "223",
    # id trạng thái: New = 1, Reopened = 100 (Resolved = 95, Closed = 12)
    "statuses": {"1": "New", "100": "Reopened"},
    "interval": 120,
    "inbox": DEFAULT_INBOX,
    "notify": True,
    "on_change": "",
    # Lệnh chạy định kỳ (không phụ thuộc sự kiện), vd xử lý hàng chờ Claude xem lại khi hạn mức đã làm mới.
    "on_tick": "",
    "tick_interval": 600,
}

stop = False


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def load_config(args):
    cfg = dict(DEFAULTS)
    path = args.config or CONFIG_PATH
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    envconf.overlay(cfg, envconf.OP_ENV)
    auto = dict(cfg.get("autofix") or {})
    envconf.overlay(auto, envconf.AUTOFIX_ENV, managed=envconf.AUTOFIX_MANAGED)
    if auto or "autofix" in cfg:
        cfg["autofix"] = auto
    if args.interval:
        cfg["interval"] = args.interval
    if args.on_change is not None:
        cfg["on_change"] = args.on_change
    if args.no_notify:
        cfg["notify"] = False
    if not cfg["api_key"]:
        sys.exit("Thiếu API key: đặt OP_API_KEY (trong .env hoặc biến môi trường) hoặc \"api_key\" trong %s" % path)
    cfg["url"] = cfg["url"].rstrip("/")
    cfg["statuses"] = {str(k): v for k, v in cfg["statuses"].items()}
    return cfg


class OpenProject:
    def __init__(self, url, api_key):
        self.url = url
        token = base64.b64encode(("apikey:" + api_key).encode()).decode()
        self.headers = {"Authorization": "Basic " + token, "Accept": "application/hal+json"}
        self.names = {}

    def get(self, path, params=None):
        query = "?" + urllib.parse.urlencode(params) if params else ""
        req = urllib.request.Request(self.url + path + query, headers=self.headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def my_work_packages(self, project, status_ids):
        filters = [
            {"assignee": {"operator": "=", "values": ["me"]}},
            {"status": {"operator": "=", "values": list(status_ids)}},
        ]
        out, offset = [], 1
        while True:
            data = self.get("/api/v3/projects/%s/work_packages" % project, {
                "filters": json.dumps(filters),
                "pageSize": 100,
                "offset": offset,
                "sortBy": json.dumps([["id", "asc"]]),
            })
            elements = data.get("_embedded", {}).get("elements", [])
            out.extend(elements)
            if len(out) >= data.get("total", 0) or not elements:
                return out
            offset += 1

    def activities(self, wp_id):
        data = self.get("/api/v3/work_packages/%s/activities" % wp_id)
        return data.get("_embedded", {}).get("elements", [])

    def me(self):
        return self.get("/api/v3/users/me")

    def user_name(self, href):
        """Activities chỉ trả href của user, không kèm tên: tra một lần rồi nhớ."""
        if not href:
            return "?"
        if href not in self.names:
            try:
                self.names[href] = (self.get(href).get("name") or "?").strip()
            except (urllib.error.URLError, OSError, ValueError):
                return href.rstrip("/").split("/")[-1]
        return self.names[href]


def load_state():
    if not os.path.exists(STATE_PATH):
        return None
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def plain(text, limit=300):
    """Comment OpenProject là markdown + HTML: bỏ thẻ, gộp khoảng trắng cho vừa thông báo."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def wp_info(cfg, wp):
    return {
        "id": wp["id"],
        "display_id": wp.get("displayId") or "#%s" % wp["id"],
        "subject": wp.get("subject", ""),
        "status": wp["_links"]["status"].get("title", ""),
        "status_id": wp["_links"]["status"]["href"].rstrip("/").split("/")[-1],
        "priority": wp["_links"].get("priority", {}).get("title", ""),
        "author": (wp["_links"].get("author", {}).get("title") or "").strip(),
        "updated_at": wp.get("updatedAt", ""),
        "link": "%s/work_packages/%s" % (cfg["url"], wp["id"]),
    }


def last_comment_id(activities):
    ids = [a["id"] for a in activities if (a.get("comment") or {}).get("raw")]
    return max(ids) if ids else 0


def emit(cfg, event, info, detail=""):
    title = "[%s] %s %s" % (event, info["display_id"], info["priority"])
    body = info["subject"] + ("\n" + detail if detail else "")
    log("%s — %s%s | %s" % (title, info["subject"], (" | " + detail) if detail else "", info["link"]))

    if cfg["notify"]:
        try:
            subprocess.run(["notify-send", "-u", "critical" if event != "COMMENT" else "normal",
                            "-a", "OpenProject", title, body], timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass

    if cfg.get("inbox"):
        line = "- [ ] %s **%s** %s %s — %s%s\n" % (
            datetime.now().strftime("%Y-%m-%d %H:%M"), event, info["display_id"],
            info["link"], info["subject"], (" — " + detail) if detail else "")
        with open(os.path.expanduser(cfg["inbox"]), "a", encoding="utf-8") as f:
            f.write(line)

    if cfg.get("on_change"):
        env = dict(os.environ, OP_EVENT=event, OP_WP_ID=str(info["id"]), OP_DISPLAY_ID=info["display_id"],
                   OP_SUBJECT=info["subject"], OP_STATUS=info["status"], OP_LINK=info["link"],
                   OP_DETAIL=detail)
        try:
            subprocess.Popen(cfg["on_change"], shell=True, env=env)
        except OSError as ex:
            log("on_change lỗi: %s" % ex)


def poll(cfg, api, me_id, state, notify_existing):
    watched = api.my_work_packages(cfg["project"], cfg["statuses"].keys())
    first_run = state is None
    old = (state or {}).get("items", {})
    items = {}

    for wp in watched:
        info = wp_info(cfg, wp)
        key = str(info["id"])
        prev = old.get(key)
        entry = {"status_id": info["status_id"], "updated_at": info["updated_at"],
                 "comment_id": prev.get("comment_id", 0) if prev else 0}

        if prev is None:
            # Việc mới vào danh sách: ghi mốc comment hiện có để chỉ báo comment sau thời điểm này.
            entry["comment_id"] = last_comment_id(api.activities(info["id"]))
            if not first_run or notify_existing:
                event = "REOPENED" if cfg["statuses"].get(info["status_id"]) == "Reopened" else "NEW"
                emit(cfg, event, info, "tạo bởi %s" % info["author"] if event == "NEW" and info["author"] else "")
        else:
            if prev["status_id"] != info["status_id"] and cfg["statuses"].get(info["status_id"]) == "Reopened":
                emit(cfg, "REOPENED", info)
            if prev["updated_at"] != info["updated_at"]:
                acts = api.activities(info["id"])
                for act in acts:
                    raw = (act.get("comment") or {}).get("raw")
                    author_href = act.get("_links", {}).get("user", {}).get("href", "")
                    if not raw or act["id"] <= prev.get("comment_id", 0) or author_href.endswith("/%s" % me_id):
                        continue
                    emit(cfg, "COMMENT", info, "%s: %s" % (api.user_name(author_href), plain(raw, 200)))
                entry["comment_id"] = max(prev.get("comment_id", 0), last_comment_id(acts))
        items[key] = entry

    if first_run:
        log("Lần chạy đầu: đang theo dõi %d việc (%s)%s" % (
            len(items), ", ".join(cfg["statuses"].values()),
            "" if notify_existing else " — không báo các việc đã có, chỉ báo thay đổi từ giờ"))
    return {"items": items, "polled_at": datetime.now().isoformat(timespec="seconds")}


def main():
    parser = argparse.ArgumentParser(description="Theo dõi OpenProject, báo bug mới / reopen / comment mới.")
    parser.add_argument("--once", action="store_true", help="chạy một lượt rồi thoát")
    parser.add_argument("--interval", type=int, help="số giây giữa hai lượt (mặc định 120)")
    parser.add_argument("--config", help="đường dẫn config (mặc định %s)" % CONFIG_PATH)
    parser.add_argument("--on-change", help="lệnh shell chạy mỗi khi có sự kiện (nhận biến OP_EVENT, OP_WP_ID, …)")
    parser.add_argument("--no-notify", action="store_true", help="không hiện thông báo desktop")
    parser.add_argument("--notify-existing", action="store_true",
                        help="lần chạy đầu cũng báo các việc đang có (mặc định chỉ ghi mốc)")
    parser.add_argument("--reset", action="store_true", help="xoá trạng thái đã lưu trước khi chạy")
    parser.add_argument("--list", action="store_true", help="in danh sách việc đang theo dõi rồi thoát")
    args = parser.parse_args()

    cfg = load_config(args)
    api = OpenProject(cfg["url"], cfg["api_key"])

    if args.list:
        for wp in api.my_work_packages(cfg["project"], cfg["statuses"].keys()):
            info = wp_info(cfg, wp)
            print("%-10s %-9s %-8s %s\n           %s" % (
                info["display_id"], info["status"], info["priority"], info["subject"], info["link"]))
        return

    if args.reset and os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)

    me = api.me()
    log("op-watch: %s (id %s), dự án %s, %ss/lượt" % (me.get("name"), me["id"], cfg["project"], cfg["interval"]))

    def handle_signal(signum, frame):
        global stop
        stop = True
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # SIGUSR1 (nút "Kiểm tra ngay" trên trang theo dõi): bỏ thời gian chờ, hỏi OpenProject ngay.
    wake = {"now": False}

    def handle_wake(signum, frame):
        wake["now"] = True
    signal.signal(signal.SIGUSR1, handle_wake)

    state = load_state()
    backoff = 0
    last_tick = 0
    while not stop:
        if cfg.get("on_tick") and time.time() - last_tick >= cfg.get("tick_interval", 600) and not args.once:
            last_tick = time.time()
            try:
                subprocess.Popen(cfg["on_tick"], shell=True)
            except OSError as ex:
                log("on_tick lỗi: %s" % ex)
        try:
            state = poll(cfg, api, me["id"], state, args.notify_existing)
            save_state(state)
            backoff = 0
        except (urllib.error.URLError, OSError, ValueError, KeyError) as ex:
            backoff = min(backoff * 2 or cfg["interval"], 1800)
            log("Lỗi khi gọi OpenProject (%s), thử lại sau %ss" % (ex, backoff))
        if args.once:
            break
        deadline = time.time() + (backoff or cfg["interval"])
        while not stop and time.time() < deadline and not wake["now"]:
            time.sleep(1)
        if wake["now"]:
            wake["now"] = False
            backoff = 0
            log("kiểm tra ngay (yêu cầu từ trang theo dõi)")


if __name__ == "__main__":
    main()
