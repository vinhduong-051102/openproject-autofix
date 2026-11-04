#!/usr/bin/env python3
"""
tg_requests: yêu cầu của tester gửi qua Telegram (chat riêng với mình) → duyệt trên trang theo dõi → task OpenProject.

Luồng:
  tg_watch.py (userbot Telethon, đăng nhập bằng tài khoản của mình) lưu tin nhắn của tester + của mình vào SQLite,
  gom các tin liên tiếp của tester (TG_GROUP_WINDOW giây) thành một yêu cầu "Chờ duyệt".
  Trên trang theo dõi, mình chọn:
    - Duyệt → AI soạn task theo mẫu tester (task_create.start_draft) → tự tạo task New giao cho mình (autofix tự sửa)
      hoặc mở bản nháp để sửa trước khi tạo;
    - Gắn vào việc có sẵn → comment + đính kèm vào work package đó;
    - Từ chối / Bỏ qua.
  Chỉ việc đã duyệt mới lên OpenProject. Phản hồi cho tester (TG_NOTIFY_TESTER) được xếp vào outbox, tg_watch.py gửi.

Dữ liệu: <autofix root>/.telegram/requests.db, ảnh / file: <autofix root>/.telegram/media/<chat_id>/.
Chỉ dùng thư viện chuẩn (Python 3.8) — trang theo dõi import module này; Telethon chỉ cần cho tg_watch.py.
"""
import base64
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402

# ---------------------------------------------------------------- cấu hình (chỉnh trên trang theo dõi)
# Thứ tự ưu tiên: telegram.json (trang theo dõi ghi)  >  .env (TG_*)  >  mặc định. Đọc lại khi file đổi, nên tg_watch.py
# và trang theo dõi áp giá trị mới ngay; riêng RESTART_KEYS cần khởi động lại op-telegram (trang tự làm khi lưu).
SETTINGS_PATH = envconf.path("TG_CONFIG", os.path.join(envconf.CONFIG_DIR, "telegram.json"))
FIELDS = {  # khoá: (biến .env, kiểu, mặc định)
    "api_id": ("TG_API_ID", "int", 0),
    "api_hash": ("TG_API_HASH", "str", ""),
    "testers": ("TG_TESTERS", "list", []),
    "group_window": ("TG_GROUP_WINDOW", "int", 120),
    "detect_mode": ("TG_DETECT_MODE", "str", "all"),
    "request_tags": ("TG_REQUEST_TAGS", "list", ["#bug", "#yc", "#request", "#task", "#feature"]),
    "max_media_mb": ("TG_MAX_MEDIA_MB", "int", 20),
    "notify_tester": ("TG_NOTIFY_TESTER", "bool", False),
    "done_statuses": ("TG_DONE_STATUSES", "list", ["Resolved", "Closed"]),
    "sync_minutes": ("TG_SYNC_MINUTES", "int", 5),
    "msg_created": ("TG_MSG_CREATED", "msg", "✅ Đã tiếp nhận: {title}\nMã việc: {display_id}"),
    "msg_linked": ("TG_MSG_LINKED", "msg", "✅ Đã ghi nhận bổ sung vào {display_id}: {title}"),
    "msg_rejected": ("TG_MSG_REJECTED", "msg", "❌ Chưa tiếp nhận: {title}\nLý do: {reason}"),
    "msg_done": ("TG_MSG_DONE", "msg", "🎉 {display_id} đã xử lý ({status}): {title}. Nhờ bạn kiểm tra lại giúp."),
    "ai_filter": ("TG_AI_FILTER", "bool", True),
    "ai_model": ("TG_AI_MODEL", "str", "sonnet"),
}
RESTART_KEYS = ("api_id", "api_hash", "testers")
SECRET_KEYS = ("api_hash",)
LIMITS = {"group_window": (10, 3600), "max_media_mb": (1, 200), "sync_minutes": (1, 120)}
MSG_FIELDS = ("title", "display_id", "reason", "status")
_SETTINGS = {"mtime": None, "data": None}


def _conv(kind, v, from_env):
    if kind == "int":
        return int(str(v).strip() or 0)
    if kind == "bool":
        return v if isinstance(v, bool) else envconf._bool(v)
    if kind == "list":
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else envconf._list(v)
    v = str(v)
    return v.replace("\\n", "\n") if kind == "msg" and from_env else v


def settings():
    """Cấu hình Telegram hiện hành (cache theo mtime của telegram.json)."""
    try:
        mtime = os.path.getmtime(SETTINGS_PATH)
    except OSError:
        mtime = None
    if _SETTINGS["data"] is None or mtime != _SETTINGS["mtime"]:
        js = envconf._read_json(SETTINGS_PATH)
        out, src = {}, {}
        for k, (env, kind, default) in FIELDS.items():
            if js.get(k) not in (None, ""):
                out[k], src[k] = _conv(kind, js[k], False), "trang"
            elif envconf.get(env) is not None:
                out[k], src[k] = _conv(kind, envconf.get(env), True), ".env"
            else:
                out[k], src[k] = default, "mặc định"
        out["detect_mode"] = (out["detect_mode"] or "all").lower()
        out["_source"] = src
        _SETTINGS.update(mtime=mtime, data=out)
    return _SETTINGS["data"]


def public_settings():
    """Cấu hình cho trang theo dõi: không trả khoá bí mật (chỉ cho biết đã đặt chưa)."""
    s = dict(settings())
    for k in SECRET_KEYS:
        s[k + "_set"] = bool(s.pop(k))
    s["path"] = SETTINGS_PATH
    s["service"] = service_state()
    s["login"] = get_meta("login", {})
    s["watcher"] = get_meta("watcher", {})
    s["defaults"] = {k: FIELDS[k][2] for k in FIELDS if FIELDS[k][1] == "msg"}
    return s


def _check_template(key, text):
    try:
        text.format(**{f: "x" for f in MSG_FIELDS})
    except (KeyError, IndexError, ValueError) as ex:
        raise ValueError("mẫu tin %s sai: %s (chỉ dùng {title} {display_id} {reason} {status})" % (key, ex))


def save_settings(body):
    """Ghi các khoá gửi từ trang vào telegram.json (quyền 600). Trả (thông báo, có cần khởi động lại op-telegram)."""
    cur = envconf._read_json(SETTINGS_PATH)
    before = {k: settings()[k] for k in RESTART_KEYS}
    for k, (env, kind, default) in FIELDS.items():
        if k not in body:
            continue
        v = body[k]
        if k in SECRET_KEYS and not str(v or "").strip():
            continue  # để trống = giữ nguyên
        if k == "api_id" and not str(v or "").strip():
            cur.pop(k, None)  # trống = dùng .env
            continue
        if kind == "int":
            try:
                v = int(str(v).strip() or 0)
            except ValueError:
                raise ValueError("%s phải là số" % k)
            lo, hi = LIMITS.get(k, (0, 10 ** 12))
            if not lo <= v <= hi:
                raise ValueError("%s phải từ %d đến %d" % (k, lo, hi))
        elif kind == "bool":
            v = bool(v)
        elif kind == "list":
            v = _conv("list", re.split(r"[,\n]", v) if isinstance(v, str) else v, False)
        else:
            v = str(v).strip() if kind == "str" else str(v).replace("\r\n", "\n").strip()
        if k == "detect_mode" and v not in ("all", "hashtag"):
            raise ValueError("chế độ nhận yêu cầu phải là all hoặc hashtag")
        if k == "api_hash" and not re.match(r"^[0-9a-f]{32}$", v):
            raise ValueError("API hash phải là 32 ký tự hex (my.telegram.org → API development tools)")
        if k == "testers":
            bad = [t for t in v if not re.match(r"^(@[A-Za-z0-9_]{4,32}|-?\d{3,})$", t)]
            if bad:
                raise ValueError("tester không hợp lệ: %s (dùng ID số hoặc @username)" % ", ".join(bad))
        if kind == "msg":
            if not v:
                cur.pop(k, None)  # trống = dùng mẫu mặc định / .env
                continue
            _check_template(k, v)
        cur[k] = v
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    fd = os.open(SETTINGS_PATH + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(SETTINGS_PATH + ".tmp", SETTINGS_PATH)
    _SETTINGS["data"] = None
    after = {k: settings()[k] for k in RESTART_KEYS}
    return "Đã lưu cấu hình Telegram", before != after


def service_state():
    """Trạng thái systemd của op-telegram: active / activating / inactive / failed / not-installed."""
    import subprocess
    res = subprocess.run(["systemctl", "--user", "show", "op-telegram", "-p", "LoadState,ActiveState"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True).stdout
    kv = dict(line.split("=", 1) for line in res.splitlines() if "=" in line)
    if kv.get("LoadState") != "loaded":
        return "not-installed"
    return kv.get("ActiveState") or "unknown"


def service_action(op):
    """Cài + bật / khởi động lại / dừng op-telegram từ trang theo dõi."""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    if op == "install":
        cmd = ["bash", os.path.join(here, "install.sh"), "op-telegram"]
    elif op in ("restart", "stop"):
        cmd = ["systemctl", "--user", op, "op-telegram"]
    else:
        raise ValueError("thao tác service không hợp lệ")
    res = subprocess.run(cmd, cwd=here, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                         timeout=120)
    if res.returncode != 0:
        raise ValueError("op-telegram %s lỗi: %s" % (op, res.stdout.strip()[-300:]))
    return {"install": "Đã cài và bật op-telegram", "restart": "Đã khởi động lại op-telegram",
            "stop": "Đã dừng op-telegram"}[op]


LOGIN_KINDS = ("login_phone", "login_code", "login_password", "login_cancel")


def queue_login(kind, **args):
    """Lệnh đăng nhập cho tg_watch.py (tiến trình duy nhất giữ file phiên). Bỏ lệnh đăng nhập cũ chưa xử lý để khỏi
    gửi mã / số điện thoại cũ khi service khởi động lại sau."""
    if service_state() not in ("active", "activating"):
        raise ValueError("op-telegram chưa chạy — bấm “Cài & bật op-telegram” trước")
    db().execute("DELETE FROM commands WHERE done_at IS NULL AND kind IN (%s)" % ",".join("?" * len(LOGIN_KINDS)),
                 LOGIN_KINDS)
    queue_command(kind, **args)
    return {"login_phone": "Đang gửi mã xác nhận…", "login_code": "Đang kiểm tra mã…",
            "login_password": "Đang kiểm tra mật khẩu…", "login_cancel": "Đã huỷ đăng nhập"}[kind]


def restart_watcher():
    """Khởi động lại op-telegram nếu đang chạy. Trả mô tả kết quả."""
    import subprocess
    st = service_state()
    if st not in ("active", "activating"):
        return "op-telegram chưa chạy — cài: ./install.sh op-telegram"
    res = subprocess.run(["systemctl", "--user", "restart", "op-telegram"], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, universal_newlines=True)
    return "đã khởi động lại op-telegram" if res.returncode == 0 else "khởi động lại op-telegram lỗi: " + res.stdout[-200:]


MAX_ATTACH = 10
MAX_ATTACH_BYTES = 20 * 1024 * 1024

STATUS_LABELS = {
    "collecting": "Đang gom tin", "triage": "AI đang lọc", "pending": "Chờ duyệt", "drafting": "AI đang soạn", "draft_ready": "Chờ tạo task",
    "created": "Đã tạo task", "linked": "Đã gắn việc", "rejected": "Từ chối", "ignored": "Bỏ qua", "error": "Lỗi",
}
OPEN_STATES = ("collecting", "triage", "pending", "drafting", "draft_ready", "error")
AI_KINDS = {"new": "Việc mới", "existing": "Việc đã có", "chat": "Trò chuyện"}
# Cột thêm sau (DB cũ): kết quả lọc của AI.
MIGRATE = ("ai_kind TEXT", "ai_type TEXT", "ai_summary TEXT", "ai_reason TEXT", "ai_refs TEXT", "ai_dup TEXT",
           "ai_at TEXT", "ai_error TEXT")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL, tg_msg_id INTEGER NOT NULL,
    sender TEXT NOT NULL,                 -- tester | me
    sender_name TEXT, text TEXT, media_path TEXT, media_type TEXT,
    date TEXT NOT NULL,                   -- ISO có múi giờ
    reply_to INTEGER, request_id INTEGER, edited INTEGER DEFAULT 0,
    UNIQUE (chat_id, tg_msg_id)
);
CREATE INDEX IF NOT EXISTS ix_tgm_chat ON messages(chat_id, date);
CREATE INDEX IF NOT EXISTS ix_tgm_req ON messages(request_id);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL, tester_name TEXT,
    status TEXT NOT NULL, title TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_message_at TEXT NOT NULL,
    decided_at TEXT, reject_reason TEXT, note TEXT,
    auto_create INTEGER DEFAULT 1,         -- duyệt: AI soạn xong thì tự tạo task
    draft_id TEXT, wp_id INTEGER, display_id TEXT, wp_url TEXT,
    wp_status TEXT, wp_synced_at TEXT, done_notified INTEGER DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS ix_tgr_status ON requests(status);
CREATE TABLE IF NOT EXISTS outbox (           -- tin nhắn gửi tester (tg_watch.py gửi)
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, text TEXT NOT NULL,
    reply_to INTEGER, created_at TEXT NOT NULL, sent_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS commands (         -- việc trang theo dõi nhờ tg_watch.py làm (vd nhập tin cũ)
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, args TEXT, created_at TEXT NOT NULL,
    done_at TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

_LOCAL = threading.local()


def root():
    import task_create
    _, acfg, _ = task_create.load()
    return os.path.join(os.path.expanduser(acfg["root"]), ".telegram")


_ROOT = None


def data_dir():
    global _ROOT
    if _ROOT is None:
        _ROOT = envconf.path("TG_DATA_DIR", "") or root()
        os.makedirs(os.path.join(_ROOT, "media"), exist_ok=True)
    return _ROOT


def media_dir():
    return os.path.join(data_dir(), "media")


def db():
    c = getattr(_LOCAL, "conn", None)
    if c is None:
        c = sqlite3.connect(os.path.join(data_dir(), "requests.db"), timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        for suffix in ("", "-wal", "-shm"):  # có tin nhắn tester + lệnh đăng nhập: chỉ chủ máy đọc được
            if os.path.exists(os.path.join(data_dir(), "requests.db" + suffix)):
                os.chmod(os.path.join(data_dir(), "requests.db" + suffix), 0o600)
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        have = {r[1] for r in c.execute("PRAGMA table_info(requests)")}
        for col in MIGRATE:
            if col.split()[0] not in have:
                c.execute("ALTER TABLE requests ADD COLUMN " + col)
        _LOCAL.conn = c
    return c


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse(s):
    return datetime.fromisoformat(s)


def rows(sql, args=()):
    return [dict(r) for r in db().execute(sql, args).fetchall()]


def row(sql, args=()):
    r = db().execute(sql, args).fetchone()
    return dict(r) if r else None


def update(rid, **f):
    f["updated_at"] = now()
    db().execute("UPDATE requests SET %s WHERE id=:_id" % ",".join("%s=:%s" % (k, k) for k in f), dict(f, _id=rid))


def get_meta(k, default=None):
    r = row("SELECT v FROM meta WHERE k=?", (k,))
    return json.loads(r["v"]) if r else default


def set_meta(k, v):
    db().execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, json.dumps(v, ensure_ascii=False)))


# ---------------------------------------------------------------- nhận tin (tg_watch.py gọi)

def _tagged(text):
    t = (text or "").lower()
    return any(tag.lower() in t for tag in settings()["request_tags"])


def _new_request(chat_id, name, date_iso, text):
    ts = now()
    cur = db().execute(
        "INSERT INTO requests (chat_id, tester_name, status, title, created_at, updated_at, last_message_at)"
        " VALUES (?, ?, 'collecting', ?, ?, ?, ?)",
        (chat_id, name, (text or "").strip().split("\n")[0][:150] or "(tệp đính kèm)", date_iso, ts, date_iso))
    return cur.lastrowid


def _attach(rid, mid, date_iso):
    db().execute("UPDATE messages SET request_id=? WHERE id=?", (rid, mid))
    db().execute("UPDATE requests SET last_message_at=?, updated_at=? WHERE id=?", (date_iso, now(), rid))


def ingest(chat_id, tg_msg_id, is_tester, sender_name, text, date, media_path=None, media_type=None,
           reply_to=None, live=True):
    """Lưu một tin nhắn. Tin của tester (live) được gom vào yêu cầu. Trả về id yêu cầu (nếu có)."""
    date_iso = date.astimezone().isoformat(timespec="seconds")
    cur = db().execute(
        "INSERT OR IGNORE INTO messages (chat_id, tg_msg_id, sender, sender_name, text, media_path, media_type, date,"
        " reply_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_id, tg_msg_id, "tester" if is_tester else "me", sender_name, text, media_path, media_type, date_iso,
         reply_to))
    if not cur.rowcount or not is_tester or not live:
        return None
    mid = cur.lastrowid
    # Trả lời vào tin thuộc yêu cầu chưa xử lý → bổ sung cho yêu cầu đó
    if reply_to:
        parent = row("SELECT request_id FROM messages WHERE chat_id=? AND tg_msg_id=?", (chat_id, reply_to))
        if parent and parent["request_id"]:
            req = row("SELECT * FROM requests WHERE id=?", (parent["request_id"],))
            if req and req["status"] in ("collecting", "pending"):
                _attach(req["id"], mid, date_iso)
                return req["id"]
    open_req = row("SELECT * FROM requests WHERE chat_id=? AND status='collecting' ORDER BY id DESC LIMIT 1",
                   (chat_id,))
    within = bool(open_req) and (date - parse(open_req["last_message_at"])).total_seconds() <= settings()["group_window"]
    tagged = _tagged(text)
    if settings()["detect_mode"] == "hashtag":
        if within and not tagged:
            _attach(open_req["id"], mid, date_iso)
            return open_req["id"]
        if not tagged:
            return None
    elif within:
        _attach(open_req["id"], mid, date_iso)
        return open_req["id"]
    if open_req:
        update(open_req["id"], status=_after_collect())
    rid = _new_request(chat_id, sender_name, date_iso, text)
    _attach(rid, mid, date_iso)
    return rid


def _after_collect():
    """Gom tin xong: bật lọc AI thì chờ AI lọc, không thì Chờ duyệt."""
    return "triage" if settings()["ai_filter"] else "pending"


def edit_message(chat_id, tg_msg_id, text):
    db().execute("UPDATE messages SET text=?, edited=1 WHERE chat_id=? AND tg_msg_id=?", (text, chat_id, tg_msg_id))


def finalize_due():
    """Yêu cầu đang gom tin mà quá GROUP_WINDOW không có tin mới → Chờ duyệt."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings()["group_window"])
    for r in rows("SELECT id, last_message_at FROM requests WHERE status='collecting'"):
        if parse(r["last_message_at"]) < cutoff:
            update(r["id"], status=_after_collect())


# ---------------------------------------------------------------- đọc cho trang theo dõi

def messages_of(rid):
    return rows("SELECT * FROM messages WHERE request_id=? ORDER BY date, tg_msg_id", (rid,))


def context_before(chat_id, before, n=6):
    return list(reversed(rows("SELECT * FROM messages WHERE chat_id=? AND date<? ORDER BY date DESC, tg_msg_id DESC"
                              " LIMIT ?", (chat_id, before, n))))


def overview(status="open", q="", days=14):
    if status == "open":
        where, args = "status IN (%s)" % ",".join("?" * len(OPEN_STATES)), list(OPEN_STATES)
    elif status == "all":
        where, args = "1=1", []
    else:
        where, args = "status=?", [status]
    if q:
        where += " AND (title LIKE ? OR id IN (SELECT request_id FROM messages WHERE text LIKE ?))"
        args += ["%" + q + "%", "%" + q + "%"]
    items = rows("SELECT r.*, (SELECT COUNT(*) FROM messages m WHERE m.request_id=r.id) AS n_messages,"
                 " (SELECT text FROM messages m WHERE m.request_id=r.id ORDER BY date LIMIT 1) AS first_text"
                 " FROM requests r WHERE %s ORDER BY r.id DESC LIMIT 200" % where, tuple(args))
    for it in items:
        it["status_label"] = STATUS_LABELS.get(it["status"], it["status"])
        _ai_fields(it, live=it["status"] in ("pending", "triage"))
    counts = {k: 0 for k in STATUS_LABELS}
    for r in rows("SELECT status, COUNT(*) AS n FROM requests GROUP BY status"):
        counts[r["status"]] = r["n"]
    today = datetime.now().astimezone().date()
    start = today - timedelta(days=days - 1)
    per_day = {(start + timedelta(days=i)).isoformat(): 0 for i in range(days)}
    for r in rows("SELECT created_at FROM requests WHERE status!='ignored' AND created_at>=?", (start.isoformat(),)):
        d = parse(r["created_at"]).astimezone().date().isoformat()
        if d in per_day:
            per_day[d] += 1
    done = row("SELECT COUNT(*) AS n FROM requests WHERE status IN ('created','linked') AND done_notified=1")["n"]
    return {
        "items": items, "counts": counts, "done": done,
        "per_day": [{"date": k, "n": v} for k, v in per_day.items()],
        "watcher": get_meta("watcher", {}), "chats": chats(),
        "notify_tester": settings()["notify_tester"], "group_window": settings()["group_window"],
        "detect_mode": settings()["detect_mode"], "ai_filter": settings()["ai_filter"], "op_url": _op_url(),
    }


def _op_url():
    try:
        import task_create
        return task_create.load()[0]["url"].rstrip("/")
    except Exception:  # noqa: BLE001
        return ""


def detail(rid):
    r = row("SELECT * FROM requests WHERE id=?", (int(rid),))
    if not r:
        raise ValueError("không có yêu cầu này")
    r["status_label"] = STATUS_LABELS.get(r["status"], r["status"])
    _ai_fields(r, live=True)
    msgs = messages_of(r["id"])
    r["messages"] = msgs
    r["context"] = context_before(r["chat_id"], msgs[0]["date"]) if msgs else []
    if r.get("draft_id"):
        try:
            import task_create
            r["draft"] = task_create.get_draft(r["draft_id"])
        except ValueError:
            r["draft"] = None
    return r


def chats():
    return rows("SELECT chat_id, MAX(CASE WHEN sender='tester' THEN sender_name END) AS name, COUNT(*) AS n,"
                " MAX(date) AS last FROM messages GROUP BY chat_id ORDER BY last DESC")


def chat_messages(chat_id, limit=300):
    return list(reversed(rows("SELECT * FROM messages WHERE chat_id=? ORDER BY date DESC, tg_msg_id DESC LIMIT ?",
                              (int(chat_id), int(limit)))))


def media_file(rel):
    full = os.path.realpath(os.path.join(media_dir(), rel or ""))
    if not full.startswith(os.path.realpath(media_dir()) + os.sep) or not os.path.isfile(full):
        return None
    return full


# ---------------------------------------------------------------- xử lý (trang theo dõi gọi)

def _fmt_time(iso):
    return parse(iso).astimezone().strftime("%d/%m/%Y %H:%M")


def _prompt_for(req, msgs):
    lines = ["Yêu cầu của tester **%s** gửi qua Telegram (nguyên văn, theo thứ tự thời gian):" % req["tester_name"], ""]
    for m in msgs:
        who = m["sender_name"] if m["sender"] == "tester" else "Tôi (dev)"
        extra = " [đính kèm: %s]" % os.path.basename(m["media_path"]) if m["media_path"] else (
            " [%s]" % m["media_type"] if m["media_type"] else "")
        lines.append("- %s · %s: %s%s" % (_fmt_time(m["date"]), who, m["text"] or "", extra))
    if req.get("ai_summary"):
        lines += ["", "AI lọc việc tóm tắt: " + req["ai_summary"]]
        if req.get("ai_type"):
            lines.append("Loại việc đề xuất: " + req["ai_type"])
    refs = [x for x in (req.get("ai_refs") or []) if isinstance(x, dict) and x.get("display_id")]
    if refs:
        lines += ["", "Ticket liên quan ĐÃ CÓ (không viết lại thành việc mới, chỉ nhắc tới nếu cần):"]
        lines += ["- %s (%s): %s" % (x["display_id"], x.get("status") or "?", x.get("subject") or "") for x in refs]
    if req.get("note"):
        lines += ["", "Ghi chú của dev khi duyệt: " + req["note"]]
    lines += ["", "Viết ticket từ yêu cầu trên. Cuối mô tả thêm dòng `_Nguồn: Telegram – %s, %s_`."
              % (req["tester_name"], _fmt_time(msgs[0]["date"]) if msgs else "")]
    return "\n".join(lines)


def _files_for(msgs):
    files, total = [], 0
    for m in msgs:
        if not m["media_path"] or len(files) >= MAX_ATTACH:
            continue
        p = media_file(m["media_path"])
        if not p:
            continue
        size = os.path.getsize(p)
        if total + size > MAX_ATTACH_BYTES:
            continue
        total += size
        with open(p, "rb") as f:
            files.append({"name": os.path.basename(p), "type": mimetypes.guess_type(p)[0] or "",
                          "data": base64.b64encode(f.read()).decode()})
    return files


def _queue_reply(req, key, **extra):
    """Xếp tin trả lời tester (mẫu `key` trong cấu hình). Mẫu sai thì dùng mẫu mặc định — không được làm hỏng thao tác
    vừa xong (vd task đã tạo mà báo lỗi → bấm chạy lại sẽ tạo trùng)."""
    if not settings()["notify_tester"]:
        return
    first = row("SELECT tg_msg_id FROM messages WHERE request_id=? AND sender='tester' ORDER BY date LIMIT 1",
                (req["id"],))
    fields = dict({f: "" for f in MSG_FIELDS}, title=req.get("title") or "", display_id=req.get("display_id") or "",
                  **extra)
    try:
        text = settings()[key].format(**fields)
    except (KeyError, IndexError, ValueError):
        text = FIELDS[key][2].format(**fields)
    db().execute("INSERT INTO outbox (chat_id, text, reply_to, created_at) VALUES (?, ?, ?, ?)",
                 (req["chat_id"], text, first["tg_msg_id"] if first else None, now()))


def approve(rid, auto_create=True, note="", force=False):
    """Duyệt: AI soạn ticket theo mẫu tester (chạy nền). auto_create: soạn xong tự tạo task New giao cho mình.
    AI đã lọc là việc đã có / trò chuyện thì phải force (mình xác nhận vẫn tạo việc mới)."""
    import task_create
    req = detail(rid)
    if not force and req.get("ai_kind") in ("existing", "chat"):
        raise ValueError("AI đánh giá là %s%s — xác nhận “Vẫn tạo việc mới” nếu chắc chắn không trùng" % (
            AI_KINDS[req["ai_kind"]].lower(), (" (" + req["ai_dup"] + ")") if req.get("ai_dup") else ""))
    if req["status"] not in ("pending", "rejected", "ignored", "error", "collecting"):
        raise ValueError("yêu cầu đang ở trạng thái %s" % req["status_label"])
    msgs = req["messages"]
    if not msgs:
        raise ValueError("yêu cầu không có tin nhắn")
    if note:
        req["note"] = note
    did = task_create.start_draft(_prompt_for(req, msgs), _files_for(msgs), {"source": "telegram", "request_id": rid})
    update(rid, status="drafting", draft_id=did, auto_create=1 if auto_create else 0, decided_at=now(),
           note=note or req.get("note"), error=None, reject_reason=None)
    return {"message": "Đã duyệt #%s — AI đang soạn task%s" % (rid, ", xong sẽ tự tạo trên OpenProject" if auto_create
                                                               else ", xong bấm “Xem bản nháp” để sửa và tạo"),
            "draft_id": did}


def _wp_info(api, wp):
    return {"wp_id": wp["id"], "display_id": wp.get("displayId") or "WP-%s" % wp["id"]}


def link(rid, wp_ref, note=""):
    """Gắn vào việc có sẵn: comment nội dung tester gửi + đính kèm file vào work package đó."""
    import op_autofix as af
    import task_create
    req = detail(rid)
    cfg, _, _ = task_create.load()
    import op_watch
    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    ref = str(wp_ref or "").strip().lstrip("#")
    if not ref:
        raise ValueError("nhập số work package hoặc mã (vd 72107 hoặc SOAR-2834)")
    wp = api.get("/api/v3/work_packages/%s" % ref.upper())
    info = _wp_info(api, wp)
    uploaded, errors = {}, []
    for m in req["messages"]:
        p = m["media_path"] and media_file(m["media_path"])
        if p:
            try:
                att = task_create.upload_attachment(api, wp["id"], p, os.path.basename(p),
                                                    mimetypes.guess_type(p)[0] or "")
                uploaded[os.path.basename(p)] = att["id"]
            except Exception as ex:  # noqa: BLE001
                errors.append("%s: %s" % (os.path.basename(p), ex))
    lines = ["**Bổ sung từ tester %s (Telegram):**" % req["tester_name"], ""]
    for m in req["messages"]:
        who = m["sender_name"] if m["sender"] == "tester" else "Dev"
        lines.append("> **%s** – %s: %s" % (who, _fmt_time(m["date"]), (m["text"] or "").replace("\n", "\n> ")))
        name = m["media_path"] and os.path.basename(m["media_path"])
        if name and name in uploaded:
            is_img = (mimetypes.guess_type(name)[0] or "").startswith("image/")
            lines.append(("> ![%s](/api/v3/attachments/%s/content)" if is_img else "> Đính kèm: [%s](/api/v3/attachments/%s/content)")
                         % (name, uploaded[name]))
        lines.append(">")
    if note:
        lines += ["", note]
    af.op_write(api, "POST", "/api/v3/work_packages/%s/activities" % wp["id"], {"comment": {"raw": "\n".join(lines)}})
    update(rid, status="linked", decided_at=now(), wp_url="%s/work_packages/%s" % (cfg["url"], wp["id"]),
           wp_status=(wp.get("_links", {}).get("status") or {}).get("title"), error=None, **info)
    req = detail(rid)
    _queue_reply(req, "msg_linked")
    af.inbox(cfg, "**TELEGRAM** bổ sung vào %s — %s" % (info["display_id"], req["title"]))
    msg = "Đã gắn yêu cầu #%s vào %s" % (rid, info["display_id"])
    return {"message": msg + (" — lỗi đính kèm: " + "; ".join(errors)[:300] if errors else "")}


def reject(rid, reason=""):
    req = detail(rid)
    update(rid, status="rejected", decided_at=now(), reject_reason=reason or None)
    _queue_reply(dict(req, reject_reason=reason), "msg_rejected", reason=reason or "không có")
    return "Đã từ chối yêu cầu #%s" % rid


def delete(rid):
    """Xoá yêu cầu khỏi danh sách. Tin nhắn vẫn giữ trong lịch sử chat (bỏ gắn), task đã tạo trên OpenProject không
    bị đụng tới; tin trả lời tester chưa gửi của yêu cầu này cũng bỏ."""
    req = detail(rid)
    first = row("SELECT tg_msg_id FROM messages WHERE request_id=? AND sender='tester' ORDER BY date LIMIT 1", (rid,))
    db().execute("UPDATE messages SET request_id=NULL WHERE request_id=?", (rid,))
    if first:
        db().execute("DELETE FROM outbox WHERE sent_at IS NULL AND chat_id=? AND reply_to=?",
                     (req["chat_id"], first["tg_msg_id"]))
    db().execute("DELETE FROM requests WHERE id=?", (rid,))
    return "Đã xoá yêu cầu #%s%s" % (rid, " (task %s trên OpenProject vẫn giữ)" % req["display_id"]
                                     if req.get("display_id") else "")


def set_status(rid, status):
    if status not in ("pending", "ignored"):
        raise ValueError("trạng thái không hợp lệ")
    update(int(rid), status=status, decided_at=None if status == "pending" else now())
    return "Đã chuyển #%s sang %s" % (rid, STATUS_LABELS[status])


def edit_title(rid, title):
    title = (title or "").strip()
    if not title:
        raise ValueError("cần tiêu đề")
    update(int(rid), title=title[:200])
    return "Đã đổi tiêu đề #%s" % rid


def from_messages(chat_id, message_ids):
    """Tạo yêu cầu từ các tin chọn trên trang (vd tin cũ đã nhập)."""
    ids = [int(x) for x in message_ids or []]
    if not ids:
        raise ValueError("chưa chọn tin nhắn nào")
    msgs = rows("SELECT * FROM messages WHERE chat_id=? AND id IN (%s) ORDER BY date" % ",".join("?" * len(ids)),
                tuple([int(chat_id)] + ids))
    if not msgs:
        raise ValueError("không tìm thấy tin nhắn")
    name = next((m["sender_name"] for m in msgs if m["sender"] == "tester"), None) or str(chat_id)
    first = next((m["text"] for m in msgs if m["text"]), "")
    rid = _new_request(int(chat_id), name, msgs[0]["date"], first)
    for m in msgs:
        db().execute("UPDATE messages SET request_id=? WHERE id=?", (rid, m["id"]))
    update(rid, status=_after_collect(), last_message_at=msgs[-1]["date"])
    return {"message": "Đã tạo yêu cầu #%s từ %d tin nhắn%s" % (rid, len(msgs), " — AI đang lọc"
                                                               if settings()["ai_filter"] else ""), "id": rid}


def queue_command(kind, **args):
    db().execute("INSERT INTO commands (kind, args, created_at) VALUES (?, ?, ?)", (kind, json.dumps(args), now()))


def mark_created(rid, res):
    """task_create.create đã tạo work package cho yêu cầu này (tự động hoặc mình bấm tạo trong bản nháp)."""
    update(rid, status="created", wp_id=res["wp_id"], display_id=res["display_id"], wp_url=res.get("url"), error=None)
    _queue_reply(detail(rid), "msg_created")


def action(body):
    """Thao tác từ trang theo dõi (action bắt đầu bằng tg_)."""
    kind = body.get("action")
    rid = body.get("id")
    if kind == "tg_approve":
        return approve(int(rid), bool(body.get("auto", True)), (body.get("note") or "").strip(), bool(body.get("force")))
    if kind == "tg_delete":
        return delete(int(rid))
    if kind == "tg_triage":
        req = detail(int(rid))
        if req["status"] not in ("pending", "ignored", "rejected", "error", "draft_ready"):
            raise ValueError("yêu cầu đang ở trạng thái %s" % req["status_label"])
        update(req["id"], status="triage", ai_at=None, ai_error=None, draft_id=None)
        return "Đã chuyển #%s cho AI lọc lại (vài chục giây)" % req["id"]
    if kind == "tg_link":
        return link(int(rid), body.get("wp"), (body.get("note") or "").strip())
    if kind == "tg_reject":
        return reject(int(rid), (body.get("reason") or "").strip())
    if kind == "tg_status":
        return set_status(rid, body.get("status"))
    if kind == "tg_title":
        return edit_title(rid, body.get("title"))
    if kind == "tg_from_messages":
        return from_messages(body.get("chat_id"), body.get("message_ids"))
    if kind == "tg_import":
        limit = max(1, min(int(body.get("limit") or 200), 5000))
        queue_command("import", chat_id=int(body.get("chat_id")), limit=limit)
        return "Đã nhờ tiến trình Telegram nhập %d tin gần nhất (vài giây sau tải lại danh sách)" % limit
    if kind == "tg_retry":
        req = detail(int(rid))
        if req["status"] != "error":
            raise ValueError("chỉ chạy lại yêu cầu đang lỗi")
        update(req["id"], status="pending", error=None)
        return approve(req["id"], bool(req["auto_create"]), req.get("note") or "", True)
    if kind == "tg_login_phone":
        phone = re.sub(r"[\s().-]", "", body.get("phone") or "")
        if not re.match(r"^\+\d{8,15}$", phone):
            raise ValueError("số điện thoại dạng quốc tế, vd +84912345678")
        return queue_login("login_phone", phone=phone)
    if kind == "tg_login_code":
        code = re.sub(r"\s", "", body.get("code") or "")
        if not re.match(r"^\d{4,8}$", code):
            raise ValueError("mã xác nhận là 4–8 chữ số")
        return queue_login("login_code", code=code)
    if kind == "tg_login_password":
        if not body.get("password"):
            raise ValueError("nhập mật khẩu xác minh 2 bước")
        return queue_login("login_password", password=body["password"])
    if kind == "tg_login_cancel":
        return queue_login("login_cancel")
    if kind == "tg_logout":
        if service_state() != "active":
            raise ValueError("op-telegram chưa chạy")
        queue_command("logout")
        return "Đang đăng xuất Telegram…"
    if kind == "tg_service":
        return service_action(body.get("op"))
    if kind == "tg_settings":
        msg, need_restart = save_settings(body.get("settings") or {})
        return msg + (" — " + restart_watcher() if need_restart else " (áp dụng ngay)")
    raise ValueError("thao tác không hợp lệ")


# ---------------------------------------------------------------- lọc việc bằng AI (tg_watch.py gọi nền)

TRIAGE_PROMPT = """Bạn lọc việc từ tin nhắn Telegram giữa tester ({tester}) và dev ({me}) của dự án SOAR. Chạy nền, chỉ
trả kết quả JSON, không dùng công cụ nào.

## Tin cần lọc (mỗi dòng có [mã tin])
{messages}

## Ngữ cảnh trước đó (chỉ để hiểu, KHÔNG lọc các tin này)
{context}

## Ticket được nhắc trong tin (trạng thái thật trên OpenProject)
{refs}

## Ticket gần đây của dự án (cập nhật trong 21 ngày; dùng để phát hiện trùng)
{recent}

## Yêu cầu Telegram đã tạo / gắn trước đó
{previous}

## Phân loại
Tách các tin cần lọc thành các việc (một việc = một nhóm tin nói về cùng một vấn đề). Mỗi việc thuộc đúng một loại:
- "new": tester yêu cầu một việc CHƯA có ticket: báo lỗi mới (type "Bug") hoặc yêu cầu dev làm gì đó (type "Task",
  vd cập nhật prompt, kiểm tra lại cấu hình). Phải đủ rõ để viết ticket.
- "existing": tin chỉ gửi link / nhắc tới ticket đã có (vd link work_packages/SOAR-2841 kèm "a gửi nha" = nhờ xem ticket
  đó), hoặc nội dung trùng ticket / yêu cầu đã có ở danh sách trên. Điền duplicate_of = mã ticket đó. Trong reason ghi
  rõ việc đó ĐÃ LÀM TỚI ĐÂU theo trạng thái ticket + autofix (vd "Resolved, autofix đã sửa và push" / "Reopened, autofix
  đang sửa" / "New, chưa ai làm").
- "chat": trò chuyện, hỏi thăm, trả lời, xác nhận ("oke", "vâng"), bàn về công cụ / token… không phải việc cần làm.
Tin của dev ({me}) chỉ là ngữ cảnh, không tự tạo việc. Không bịa việc từ ngữ cảnh. Nghi ngờ giữa new và existing thì
chọn existing kèm lý do. Một tin vừa nhắc ticket cũ vừa nêu yêu cầu mới (vd "verify bug X thì lòi thêm bug Y, Z — hỏi
AI tại sao rồi cập nhật prompt") → việc mới là phần yêu cầu mới (Task), các bug đã có ticket ghi vào refs.

## Kết quả: chỉ một khối JSON
{{"items": [{{"kind": "new" | "existing" | "chat", "type": "Bug" | "Task" | null, "title": "tiêu đề ngắn tiếng Việt",
  "summary": "1-3 câu: tester muốn gì, đã có gì", "message_ids": [mã tin...], "refs": ["SOAR-xxxx", ...],
  "duplicate_of": "SOAR-xxxx" | null, "reason": "vì sao phân loại vậy (nêu trạng thái ticket nếu đã có)"}}]}}
Mọi [mã tin] ở phần "Tin cần lọc" phải nằm trong đúng một việc.
"""

REF_RE = re.compile(r"(?:work_packages/|\b)(SOAR-\d+)\b|work_packages/(\d+)\b", re.I)
_TRIAGE_LOCK = threading.Lock()
_RECENT = {"at": 0, "text": ""}


AUTOFIX_LABELS = {"queued": "autofix đang xếp hàng", "preparing": "autofix đang chuẩn bị", "running": "autofix đang sửa",
                  "shipping": "autofix đang kiểm tra / push", "done": "autofix đã chạy xong", "error": "autofix lỗi",
                  "paused": "autofix hoãn"}
_LIVE = {}  # display_id → (thời điểm, thông tin ticket trên OpenProject) — trang tải lại liên tục, đọc API tối đa 1 lần/phút


def autofix_status(display_id):
    """Trạng thái autofix của ticket (~/autofix/.status/<mã>.json): việc đã / đang làm tới đâu."""
    try:
        with open(os.path.join(envconf.autofix_root(), ".status", "%s.json" % display_id), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    return {"state": st.get("state"), "label": AUTOFIX_LABELS.get(st.get("state"), st.get("state") or ""),
            "verdict": (st.get("verdict") or "")[:300], "at": st.get("updated_at"), "run": st.get("run_name")}


def _live_wp(display_id):
    import time
    hit = _LIVE.get(display_id)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        import task_create
        import op_watch
        cfg = task_create.load()[0]
        info = _wp_line(op_watch.OpenProject(cfg["url"], cfg["api_key"]).get("/api/v3/work_packages/%s" % display_id))
    except Exception:  # noqa: BLE001 — không đọc được thì giữ thông tin lúc lọc
        info = None
    _LIVE[display_id] = (time.time(), info)
    return info


def work_state(ref):
    """done: ticket đã ở trạng thái xong (TG_DONE_STATUSES); doing: autofix đang làm / đã sửa chờ duyệt; todo: chưa làm."""
    done = [x.lower() for x in settings()["done_statuses"]]
    af_st = ref.get("autofix") or {}
    if (ref.get("status") or "").lower() in done:
        return "done"
    if af_st.get("state") in ("queued", "preparing", "running", "shipping"):
        return "doing"
    if af_st.get("state") == "done":
        return "review"  # autofix chạy xong nhưng ticket chưa sang trạng thái xong (vd UNREVIEWED: chờ mình duyệt)
    return "todo"


WORK_LABELS = {"done": "đã làm xong", "doing": "đang làm", "review": "đã sửa, chờ duyệt", "todo": "chưa làm"}


def _ai_fields(r, live=False):
    """Thêm nhãn + danh sách ticket liên quan. live: đọc lại trạng thái OpenProject + autofix hiện tại (kết quả lọc chỉ
    là ảnh chụp lúc AI lọc — sau đó autofix có thể đã sửa xong)."""
    r["ai_kind_label"] = AI_KINDS.get(r.get("ai_kind") or "", "")
    try:
        r["ai_refs"] = json.loads(r.get("ai_refs") or "[]")
    except ValueError:
        r["ai_refs"] = []
    if live and r["ai_refs"] and r.get("status") in OPEN_STATES:
        for x in r["ai_refs"]:
            if not x.get("display_id"):
                continue
            cur = _live_wp(x["display_id"])
            if cur:
                x.update(cur)
            x["autofix"] = autofix_status(x["display_id"])
            x["work"] = work_state(x)
            x["work_label"] = WORK_LABELS[x["work"]]
        main = [x for x in r["ai_refs"] if x.get("display_id") == r.get("ai_dup")] or r["ai_refs"]
        works = {x.get("work") for x in main if x.get("work")}
        r["ai_work"] = ("done" if works == {"done"} else "doing" if "doing" in works else
                        "review" if "review" in works else "todo" if works else None)
        r["ai_work_label"] = WORK_LABELS.get(r["ai_work"] or "", "")
    return r


def _wp_line(w):
    links = w.get("_links") or {}
    return {"display_id": w.get("displayId") or "WP-%s" % w["id"], "id": w["id"], "subject": w.get("subject", ""),
            "status": (links.get("status") or {}).get("title") or "", "assignee": (links.get("assignee") or {}).get("title") or "",
            "author": (links.get("author") or {}).get("title") or "", "created": (w.get("createdAt") or "")[:10]}


def _fmt_wp(x):
    return "- %s | %s | giao: %s | tạo bởi: %s %s | %s" % (x["display_id"], x["status"], x["assignee"] or "-",
                                                         x["author"] or "-", x["created"], x["subject"][:140])


def _recent_wps(api, cfg):
    import time
    if time.time() - _RECENT["at"] < 300:
        return _RECENT["text"]
    data = api.get("/api/v3/projects/%s/work_packages" % cfg["project"], {
        "filters": json.dumps([{"status": {"operator": "*", "values": []}},
                               {"updatedAt": {"operator": ">t-", "values": ["21"]}}]),
        "sortBy": json.dumps([["updatedAt", "desc"]]), "pageSize": 200})
    text = "\n".join(_fmt_wp(_wp_line(w)) for w in data.get("_embedded", {}).get("elements", [])) or "(không lấy được)"
    _RECENT.update(at=time.time(), text=text)
    return text


def _fmt_autofix(display_id):
    st = autofix_status(display_id)
    if not st:
        return " | autofix: chưa chạy"
    return " | %s %s: %s" % (st["label"], (st["at"] or "")[:16].replace("T", " "), st["verdict"][:160])


def _refs_in(msgs, api):
    seen, out = set(), []
    for m in msgs:
        for a, b in REF_RE.findall(m.get("text") or ""):
            ref = (a or b).upper()
            if ref in seen:
                continue
            seen.add(ref)
            try:
                out.append(_wp_line(api.get("/api/v3/work_packages/%s" % ref)))
            except Exception as ex:  # noqa: BLE001
                out.append({"display_id": ref, "id": None, "subject": "(không đọc được: %s)" % str(ex)[:80],
                            "status": "", "assignee": "", "author": "", "created": ""})
    return out


def _msg_line(m):
    who = m["sender_name"] if m["sender"] == "tester" else "Dev"
    att = " [%s]" % m["media_type"] if m.get("media_type") else ""
    return "[%s] %s · %s%s: %s" % (m["id"], _fmt_time(m["date"]), who, att, (m["text"] or "").replace("\n", " ⏎ ")[:1500])


def _run_claude_json(prompt, model):
    """Claude headless không công cụ, trả dict JSON trong câu trả lời."""
    import subprocess
    import task_create
    import op_autofix as af
    _, acfg, _ = task_create.load()
    prof, why = af.choose_profile(os.path.expanduser(acfg["root"]), acfg)
    if prof is None:
        raise RuntimeError("không có gói Claude dùng được (%s)" % why)
    af.use_profile(acfg, prof)
    cmd = [acfg["claude"], "-p", prompt, "--permission-mode", "dontAsk", "--max-budget-usd", "0.5",
           "--output-format", "json", "--disallowedTools", "Bash", "Read", "Write", "Edit", "Glob", "Grep",
           "WebFetch", "WebSearch", "Task"]
    if model:
        cmd += ["--model", model]
    res = subprocess.run(cmd, cwd=data_dir(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         universal_newlines=True, env=af.build_env(acfg), timeout=300)
    try:
        text = json.loads(res.stdout).get("result") or ""
    except ValueError:
        raise RuntimeError("Claude lỗi (mã %s): %s" % (res.returncode, (res.stderr or res.stdout)[-300:]))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("Claude không trả JSON: %s" % text[:200])
    return json.loads(m.group(0))


def triage(rid):
    """AI lọc một yêu cầu: tách thành các việc, đánh dấu trò chuyện / việc đã có. Việc nào cũng chờ mình duyệt."""
    import task_create
    import op_watch
    req = row("SELECT * FROM requests WHERE id=?", (rid,))
    msgs = messages_of(rid)
    if not req or not msgs:
        return
    cfg, _, _ = task_create.load()
    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    ctx = context_before(req["chat_id"], msgs[0]["date"], 15)
    refs = _refs_in(ctx[-6:] + msgs, api)
    prev = rows("SELECT id, title, display_id, status FROM requests WHERE status IN ('created','linked','drafting',"
                "'draft_ready') AND id!=? ORDER BY id DESC LIMIT 40", (rid,))
    watcher = get_meta("watcher", {}) or {}
    prompt = TRIAGE_PROMPT.format(
        tester=req["tester_name"] or "tester", me=watcher.get("me") or "dev",
        messages="\n".join(_msg_line(m) for m in msgs), context="\n".join(_msg_line(m) for m in ctx) or "(không có)",
        refs="\n".join(_fmt_wp(x) + _fmt_autofix(x["display_id"]) for x in refs) or "(không có)",
        recent=_recent_wps(api, cfg),
        previous="\n".join("- #%s %s → %s" % (p["id"], p["title"], p["display_id"] or STATUS_LABELS.get(p["status"]))
                           for p in prev) or "(không có)")
    out = _run_claude_json(prompt, settings()["ai_model"])
    items = [it for it in (out.get("items") or []) if isinstance(it, dict)]
    valid = {m["id"] for m in msgs}
    ref_info = {x["display_id"]: x for x in refs}
    used, groups = set(), []
    for it in items:
        ids = [int(i) for i in it.get("message_ids") or [] if str(i).isdigit() and int(i) in valid and int(i) not in used]
        used.update(ids)
        kind = it.get("kind") if it.get("kind") in AI_KINDS else "new"
        groups.append((kind, it, ids))
    left = sorted(valid - used)
    if left:  # AI bỏ sót tin: gộp vào việc đầu tiên (hoặc coi là trò chuyện)
        if groups:
            groups[0][2].extend(left)
        else:
            groups.append(("chat", {"reason": "AI không xếp được tin nào"}, left))
    # Việc thật lên trước để giữ yêu cầu gốc; trò chuyện gộp chung
    groups.sort(key=lambda g: g[0] == "chat")
    first = True
    for kind, it, ids in groups:
        if not ids:
            continue
        dup = (it.get("duplicate_of") or "").upper() or None
        rlist = [ref_info.get(r.upper()) or {"display_id": r.upper()} for r in (it.get("refs") or []) if r]
        if dup and dup not in [x["display_id"] for x in rlist]:
            rlist.insert(0, ref_info.get(dup) or {"display_id": dup})
        fields = dict(ai_kind=kind, ai_type=it.get("type") if it.get("type") in ("Bug", "Task") else None,
                      ai_summary=(it.get("summary") or "")[:1000], ai_reason=(it.get("reason") or "")[:1000],
                      ai_refs=json.dumps(rlist, ensure_ascii=False), ai_dup=dup, ai_at=now(), ai_error=None,
                      status="ignored" if kind == "chat" else "pending",
                      decided_at=now() if kind == "chat" else None)
        if it.get("title"):
            fields["title"] = it["title"][:200]
        if first:
            target, first = rid, False
        else:
            target = _new_request(req["chat_id"], req["tester_name"], msgs[0]["date"], it.get("title") or "")
        db().execute("UPDATE messages SET request_id=? WHERE id IN (%s)" % ",".join("?" * len(ids)), [target] + ids)
        last = max(m["date"] for m in msgs if m["id"] in ids)
        update(target, last_message_at=last, **fields)


def triage_due():
    """Lọc các yêu cầu đang chờ AI (một luồng nền; gọi lại khi đang chạy thì bỏ qua)."""
    if not _TRIAGE_LOCK.acquire(blocking=False):
        return
    def work():
        try:
            for r in rows("SELECT id FROM requests WHERE status='triage' ORDER BY id LIMIT 5"):
                try:
                    triage(r["id"])
                except Exception as ex:  # noqa: BLE001
                    # Lọc lỗi: đưa về Chờ duyệt để mình tự xem, không mất yêu cầu.
                    update(r["id"], status="pending", ai_error=str(ex)[:400], ai_at=now())
        finally:
            _TRIAGE_LOCK.release()
    threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- việc nền (tg_watch.py gọi định kỳ)

def advance_drafts():
    """Theo dõi bản nháp AI: soạn xong → tự tạo task (auto_create) hoặc chờ mình tạo; bản nháp đã tạo → ghi nhận."""
    import task_create
    out = []
    for r in rows("SELECT * FROM requests WHERE status IN ('drafting','draft_ready') AND draft_id IS NOT NULL"):
        try:
            st = task_create.get_draft(r["draft_id"])
        except ValueError as ex:
            update(r["id"], status="error", error=str(ex))
            continue
        state = st.get("state")
        if state == "created":
            mark_created(r["id"], {"wp_id": st.get("wp_id"), "display_id": st.get("display_id"),
                                   "url": None})
            out.append("#%s → %s" % (r["id"], st.get("display_id")))
        elif state == "error":
            update(r["id"], status="error", error="AI soạn lỗi: %s" % (st.get("error") or "?"))
        elif state == "ready" and r["status"] == "drafting":
            d = st.get("draft") or {}
            if r["auto_create"]:
                try:
                    res = task_create.create(r["draft_id"], d.get("subject"), d.get("description"),
                                             d.get("type") or "Bug", d.get("priority") or "High", "me")
                except Exception as ex:  # noqa: BLE001
                    update(r["id"], status="error", error="Tạo task lỗi: %s" % str(ex)[:400])
                    continue
                # Task đã có trên OpenProject: từ đây lỗi cũng không được chuyển sang "Lỗi" (chạy lại = tạo trùng).
                update(r["id"], title=d.get("subject") or r["title"])
                mark_created(r["id"], res)
                out.append("#%s → %s" % (r["id"], res["display_id"]))
            else:
                update(r["id"], status="draft_ready", title=d.get("subject") or r["title"])
    # Bản nháp tạo xong thì điền link
    for r in rows("SELECT id, wp_id FROM requests WHERE status='created' AND wp_url IS NULL AND wp_id IS NOT NULL"):
        try:
            import task_create as tc
            cfg, _, _ = tc.load()
            update(r["id"], wp_url="%s/work_packages/%s" % (cfg["url"], r["wp_id"]))
        except Exception:  # noqa: BLE001
            pass
    return out


def sync_wp_status():
    """Cập nhật trạng thái work package của các yêu cầu đã tạo / gắn; xong (TG_DONE_STATUSES) thì báo tester 1 lần."""
    reqs = rows("SELECT * FROM requests WHERE status='created' AND wp_id IS NOT NULL AND done_notified=0")
    if not reqs:
        return 0
    import task_create
    import op_watch
    cfg, _, _ = task_create.load()
    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    for r in reqs:
        try:
            wp = api.get("/api/v3/work_packages/%s" % r["wp_id"])
        except Exception:  # noqa: BLE001
            continue
        st = ((wp.get("_links") or {}).get("status") or {}).get("title") or ""
        update(r["id"], wp_status=st, wp_synced_at=now())
        if st.lower() in [x.lower() for x in settings()["done_statuses"]]:
            update(r["id"], done_notified=1)
            _queue_reply(r, "msg_done", status=st)
    return len(reqs)


def pending_outbox():
    return rows("SELECT * FROM outbox WHERE sent_at IS NULL AND error IS NULL ORDER BY id LIMIT 20")


def mark_outbox(oid, error=None):
    db().execute("UPDATE outbox SET sent_at=?, error=? WHERE id=?", (None if error else now(), error, oid))


def pending_commands():
    # Lệnh đăng nhập quá 10 phút (vd gửi khi service đang tắt) bỏ qua — mã xác nhận cũng đã hết hạn.
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).astimezone().isoformat(timespec="seconds")
    db().execute("UPDATE commands SET done_at=?, result='quá hạn', args=NULL WHERE done_at IS NULL AND kind LIKE 'login_%'"
                 " AND created_at<?", (now(), cutoff))
    return rows("SELECT * FROM commands WHERE done_at IS NULL ORDER BY id LIMIT 5")


def finish_command(cid, result):
    # Xoá args: lệnh đăng nhập chứa số điện thoại / mã / mật khẩu 2 bước.
    db().execute("UPDATE commands SET done_at=?, result=?, args=NULL WHERE id=?", (now(), str(result)[:500], cid))


# ---------------------------------------------------------------- GET cho trang theo dõi (/api/tg/...)

def http_get(path, q):
    """Trả (mã HTTP, body, content-type) cho op_dashboard.py."""
    if path == "/api/tg/settings":
        return 200, public_settings(), "application/json; charset=utf-8"
    if path == "/api/tg/overview":
        return 200, overview(q.get("status", "open"), q.get("q", "")), "application/json; charset=utf-8"
    if path == "/api/tg/request":
        return 200, detail(q.get("id")), "application/json; charset=utf-8"
    if path == "/api/tg/chat":
        return 200, {"messages": chat_messages(q.get("chat_id"), q.get("limit", 300))}, "application/json; charset=utf-8"
    if path == "/api/tg/media":
        p = media_file(q.get("path", ""))
        if not p:
            return 404, {"error": "không có file"}, "application/json; charset=utf-8"
        ctype = mimetypes.guess_type(p)[0] or ""
        with open(p, "rb") as f:
            data = f.read()
        # Chỉ hiển thị ảnh / video trong trang; loại khác (html, svg…) luôn tải về
        if not re.match(r"^(image/(png|jpe?g|gif|webp)|video/(mp4|webm))$", ctype):
            ctype = "application/octet-stream"
        return 200, data, ctype
    return 404, {"error": "not found"}, "application/json; charset=utf-8"
