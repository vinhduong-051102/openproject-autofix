#!/usr/bin/env python3
"""
gdocs: tải tài liệu Google (Docs / Sheets / Slides / file trên Drive) có link trong ticket / comment, để Claude (chạy
offline, không WebFetch) và Gemini đọc được — dùng tài khoản Google CỦA BẠN (tài liệu đã chia sẻ cho email đó).

Cấp quyền: OAuth 2.0 cho ứng dụng cài trên máy (client loại "Desktop app") — redirect về trang theo dõi
(http://127.0.0.1:<cổng>/api/google/callback) + PKCE + state; quyền drive.readonly (chỉ đọc) + openid email (biết tài
khoản nào đã kết nối). Cấu hình trên trang theo dõi: Cấu hình → Google Docs.

Lưu ở ~/.config/op-watch (quyền 600, không commit):
  google.json          client_id, client_secret, email (gợi ý đăng nhập), refresh_token, connected_email, connected_at
  google-access.json   access token đang dùng + hạn (làm mới tự động bằng refresh token)

Link đọc được:
  docs.google.com/document/d/<id>      → xuất markdown (lỗi thì text thường)
  docs.google.com/spreadsheets/d/<id>  → xuất xlsx, trích chữ mọi trang tính (lỗi thì csv — chỉ trang tính đầu)
  docs.google.com/presentation/d/<id>  → xuất text
  docs.google.com/drawings/d/<id>      → ảnh PNG
  drive.google.com/file/d/<id>, open?id=<id>, uc?id=<id> → tải bản gốc, trích chữ (pdf / docx / xlsx / zip / text…)
Chưa kết nối Google: thử tải ẩn danh (tài liệu công khai); không được thì ghi lý do vào ticket.md, không ném lỗi.
Kết quả cache theo (id, modifiedTime) trong <root>/.gdocs-cache.

    gdocs.py <link>     # thử: tải vào ./gdocs, in mục "Tài liệu Google"
    gdocs.py status     # tình trạng kết nối (không in bí mật)
"""
import base64
import fcntl
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402

CFG_PATH = os.path.join(envconf.CONFIG_DIR, "google.json")
ACCESS_PATH = os.path.join(envconf.CONFIG_DIR, "google-access.json")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
DRIVE = "https://www.googleapis.com/drive/v3/files"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive.readonly"
SCOPES = "openid email " + SCOPE_DRIVE
CALLBACK_PATH = "/api/google/callback"

LINK_RX = re.compile(
    r"https?://(?:docs|drive)\.google\.com/[^\s)\]\"'<>`]+")
ID_RX = re.compile(r"/(document|spreadsheets|presentation|drawings|forms|file)/(?:u/\d+/)?d/([A-Za-z0-9_-]{20,})")
FOLDER_RX = re.compile(r"/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]{20,})")
MAX_LINKS = 6
MAX_BIN = 20 * 1024 * 1024    # file gốc trên Drive lớn hơn thì không tải
MAX_TEXT = 2 * 1024 * 1024    # chữ ghi ra mỗi file tối đa (ký tự)
STATE_TTL = 600               # state OAuth dùng được trong 10 phút, một lần
TIMEOUT = 60
CACHE_VER = "v1"

G_DOC = "application/vnd.google-apps.document"
G_SHEET = "application/vnd.google-apps.spreadsheet"
G_SLIDES = "application/vnd.google-apps.presentation"
G_DRAWING = "application/vnd.google-apps.drawing"
G_FOLDER = "application/vnd.google-apps.folder"
G_SHORTCUT = "application/vnd.google-apps.shortcut"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
KIND_LABEL = {"document": "Google Docs", "spreadsheets": "Google Sheets", "presentation": "Google Slides",
              "drawings": "Google Drawings", "forms": "Google Forms", "file": "file Drive", "folder": "thư mục Drive"}
MIME_KIND = {G_DOC: "document", G_SHEET: "spreadsheets", G_SLIDES: "presentation", G_DRAWING: "drawings",
             G_FOLDER: "folder"}


class GError(Exception):
    def __init__(self, msg, status=None):
        Exception.__init__(self, msg)
        self.status = status


# ---- cấu hình (file quyền 600) ---------------------------------------------------------------------------------------

def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_private(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.tmp-%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class _Lock:
    """Khoá file quanh đọc-sửa-ghi google.json (trang theo dõi + các lượt autofix cùng làm mới token)."""

    def __enter__(self):
        os.makedirs(envconf.CONFIG_DIR, mode=0o700, exist_ok=True)
        self.f = open(os.path.join(envconf.CONFIG_DIR, ".google.lock"), "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def load_cfg():
    return _read(CFG_PATH)


def _update_cfg(**fields):
    """Sửa vài khoá của google.json (None = xoá khoá)."""
    with _Lock():
        cfg = load_cfg()
        for k, v in fields.items():
            if v is None:
                cfg.pop(k, None)
            else:
                cfg[k] = v
        _write_private(CFG_PATH, cfg)
        return cfg


def status():
    """Tình trạng cho trang / CLI — KHÔNG có client_secret / token."""
    c = load_cfg()
    email, conn = (c.get("email") or "").strip(), (c.get("connected_email") or "").strip()
    scopes = (c.get("scopes") or "").split()
    return {
        "configured": bool(c.get("client_id") and c.get("client_secret")),
        "connected": bool(c.get("refresh_token")),
        "client_id": c.get("client_id") or "", "secret_set": bool(c.get("client_secret")), "email": email,
        "connected_email": conn, "connected_at": c.get("connected_at") or "",
        "drive_scope": (SCOPE_DRIVE in scopes) if scopes else bool(c.get("refresh_token")),
        "mismatch": bool(email and conn and email.lower() != conn.lower()),
        "last_error": c.get("last_error") or "", "config_path": CFG_PATH.replace(os.path.expanduser("~"), "~"),
    }


def save_settings(client_id=None, client_secret=None, email=None):
    """Lưu client id / secret / email. Secret trống = giữ nguyên. Đổi client id → bỏ kết nối cũ (token của client cũ)."""
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    email = (email or "").strip()
    if client_id and not re.match(r"^[\w.-]+\.apps\.googleusercontent\.com$", client_id):
        raise ValueError("Client ID không đúng dạng (…apps.googleusercontent.com)")
    if client_secret and not re.match(r"^[\w-]{10,}$", client_secret):
        raise ValueError("Client secret không đúng dạng (thường bắt đầu bằng GOCSPX-)")
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ValueError("email không đúng dạng")
    with _Lock():
        c = load_cfg()
        dropped = bool(client_id and c.get("client_id") and client_id != c["client_id"] and c.get("refresh_token"))
        if dropped:
            for k in ("refresh_token", "connected_email", "connected_at", "scopes"):
                c.pop(k, None)
            _drop_access()
        if client_id:
            c["client_id"] = client_id
        if client_secret:
            c["client_secret"] = client_secret
        c["email"] = email
        _write_private(CFG_PATH, c)
    return "Đã lưu cấu hình Google%s" % (" — đổi Client ID nên đã bỏ kết nối cũ, bấm Kết nối Google lại" if dropped else "")


# ---- HTTP (một chỗ để test thay thế) -----------------------------------------------------------------------------------

def _request(url, data=None, headers=None, limit=None):
    """→ (mã HTTP, headers, body bytes, URL cuối sau chuyển hướng). Lỗi HTTP trả mã, lỗi mạng ném GError.
    limit: đọc tối đa limit+1 byte (người gọi tự kiểm tra quá cỡ)."""
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, headers=dict(headers or {}, **{"User-Agent": "op-watch-gdocs"}))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.headers, (r.read(limit + 1) if limit else r.read()), r.geturl()
    except urllib.error.HTTPError as ex:
        return ex.code, ex.headers, ex.read(65536), url
    except (urllib.error.URLError, OSError) as ex:
        raise GError("không kết nối được Google (%s)" % getattr(ex, "reason", ex))


def _json(body):
    try:
        d = json.loads(body.decode("utf-8", "replace"))
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


# ---- OAuth -----------------------------------------------------------------------------------------------------------

_PENDING = {}          # state → {verifier, redirect, exp} (trong tiến trình trang theo dõi)
_PENDING_LOCK = threading.Lock()


def redirect_uri(port):
    return "http://127.0.0.1:%d%s" % (int(port), CALLBACK_PATH)


def auth_url(port):
    """URL màn hình đồng ý của Google (mở ở tab mới). Tạo state + PKCE verifier dùng một lần trong STATE_TTL giây."""
    c = load_cfg()
    if not (c.get("client_id") and c.get("client_secret")):
        raise ValueError("chưa nhập Client ID / Client secret — bấm Lưu trước")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)  # 86 ký tự, trong giới hạn 43–128 của PKCE
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    redirect = redirect_uri(port)
    now = time.time()
    with _PENDING_LOCK:
        for k in [k for k, v in _PENDING.items() if v["exp"] < now]:
            _PENDING.pop(k, None)
        _PENDING[state] = {"verifier": verifier, "redirect": redirect, "exp": now + STATE_TTL}
    q = {"client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
         "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
         "code_challenge": challenge, "code_challenge_method": "S256", "state": state}
    if c.get("email"):
        q["login_hint"] = c["email"]
    return AUTH_URL + "?" + urllib.parse.urlencode(q)


def _jwt_email(id_token):
    """Email trong id_token (nhận thẳng từ endpoint token của Google qua TLS nên không cần kiểm chữ ký)."""
    try:
        part = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("email") or ""
    except (IndexError, ValueError, AttributeError):
        return ""


def finish(state, code=None, error=None):
    """Xử lý redirect từ Google: kiểm state (một lần, còn hạn) → đổi code lấy token (PKCE) → lưu refresh token + email.
    Trả thông báo; lỗi ném ValueError (thông báo không chứa code / token)."""
    with _PENDING_LOCK:
        p = _PENDING.pop(state or "", None)
    if not p:
        raise ValueError("phiên kết nối không hợp lệ hoặc đã dùng (state sai) — bấm Kết nối Google lại trên trang theo dõi")
    if p["exp"] < time.time():
        raise ValueError("phiên kết nối đã hết hạn (quá %d phút) — bấm Kết nối Google lại" % (STATE_TTL // 60))
    if error:
        raise ValueError("Google trả lỗi: %s" % ("bạn đã từ chối cấp quyền" if error == "access_denied" else error[:100]))
    if not code:
        raise ValueError("thiếu mã xác nhận từ Google")
    c = load_cfg()
    st, _, body, _ = _request(TOKEN_URL, {
        "client_id": c.get("client_id", ""), "client_secret": c.get("client_secret", ""), "code": code,
        "code_verifier": p["verifier"], "grant_type": "authorization_code", "redirect_uri": p["redirect"]})
    d = _json(body)
    if st != 200 or not d.get("access_token"):
        raise ValueError("đổi mã lấy token thất bại (%s): %s" % (st, (d.get("error_description") or d.get("error")
                                                                     or "")[:200]))
    if not d.get("refresh_token"):
        raise ValueError("Google không trả refresh token — vào myaccount.google.com/permissions gỡ ứng dụng rồi kết nối lại")
    email = _jwt_email(d.get("id_token") or "")
    if not email:
        s2, _, b2, _ = _request(USERINFO_URL, headers={"Authorization": "Bearer " + d["access_token"]})
        email = (_json(b2).get("email") or "") if s2 == 200 else ""
    scopes = d.get("scope") or ""
    _update_cfg(refresh_token=d["refresh_token"], connected_email=email or "(không rõ)", scopes=scopes,
                connected_at=datetime.now().isoformat(timespec="seconds"), last_error=None)
    _save_access(d["refresh_token"], d["access_token"], d.get("expires_in"))
    msg = "Đã kết nối Google Docs với tài khoản %s" % (email or "(không đọc được email)")
    if SCOPE_DRIVE not in scopes.split():
        msg += " — NHƯNG chưa cấp quyền xem file Drive (ô đó bị bỏ tích): kết nối lại và tích ô cho phép xem file"
    want = (c.get("email") or "").strip()
    if want and email and want.lower() != email.lower():
        msg += " — khác email đã nhập (%s)" % want
    return msg


def finish_from_url(url):
    """Trình duyệt không cùng máy với trang (vd mở qua Tailscale): dán URL 127.0.0.1/... sau khi Google chuyển hướng."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse((url or "").strip()).query)
    g = lambda k: (q.get(k) or [None])[0]  # noqa: E731
    if not g("state"):
        raise ValueError("URL không có state / code — dán nguyên URL trên thanh địa chỉ sau khi Google chuyển hướng")
    return finish(g("state"), g("code"), g("error"))


def _rt_key(rt):
    return hashlib.sha256(rt.encode()).hexdigest()[:16]


def _save_access(rt, at, expires_in):
    try:
        _write_private(ACCESS_PATH, {"rt": _rt_key(rt), "access_token": at,
                                     "exp": time.time() + int(expires_in or 3600) - 120})
    except OSError:
        pass


def _drop_access():
    try:
        os.remove(ACCESS_PATH)
    except OSError:
        pass


def access_token(force=False):
    """Access token còn hạn (làm mới bằng refresh token khi cần). Chưa kết nối → None."""
    c = load_cfg()
    rt = c.get("refresh_token")
    if not rt:
        return None
    a = _read(ACCESS_PATH)
    if not force and a.get("rt") == _rt_key(rt) and a.get("access_token") and a.get("exp", 0) > time.time():
        return a["access_token"]
    st, _, body, _ = _request(TOKEN_URL, {"client_id": c.get("client_id", ""), "client_secret": c.get("client_secret", ""),
                                          "refresh_token": rt, "grant_type": "refresh_token"})
    d = _json(body)
    if st != 200 or not d.get("access_token"):
        if d.get("error") == "invalid_grant":
            msg = ("Google đã thu hồi quyền / refresh token hết hạn (ứng dụng OAuth để chế độ Testing thì token chỉ sống "
                   "7 ngày) — bấm Kết nối Google lại trên trang theo dõi")
        elif d.get("error") in ("invalid_client", "unauthorized_client"):
            msg = "Client ID / secret không hợp lệ (%s) — kiểm tra lại trên trang theo dõi" % d["error"]
        else:
            msg = "làm mới token Google thất bại (%s %s)" % (st, (d.get("error") or "")[:80])
        try:
            _update_cfg(last_error=msg)
        except OSError:
            pass
        raise GError(msg)
    _save_access(rt, d["access_token"], d.get("expires_in"))
    if c.get("last_error"):
        _update_cfg(last_error=None)
    return d["access_token"]


def disconnect():
    """Thu hồi quyền ở Google (revoke refresh token) + xoá token trên máy. Client ID / secret giữ nguyên."""
    c = load_cfg()
    rt = c.get("refresh_token")
    note = ""
    if rt:
        try:
            st, _, _, _ = _request(REVOKE_URL, {"token": rt},
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
            if st not in (200, 400):  # 400 = token đã hết hiệu lực từ trước
                note = " (Google trả %s khi thu hồi — gỡ tay ở myaccount.google.com/permissions nếu cần)" % st
        except GError as ex:
            note = " (không gọi được Google để thu hồi: %s — gỡ tay ở myaccount.google.com/permissions)" % ex
    _update_cfg(refresh_token=None, connected_email=None, connected_at=None, scopes=None, last_error=None)
    _drop_access()
    return "Đã ngắt kết nối Google" + note


def callback_page(ok, message):
    """Trang HTML nhỏ sau khi Google chuyển hướng về trang theo dõi."""
    return ("<!DOCTYPE html><html lang=\"vi\"><meta charset=\"utf-8\"><title>Google Docs — op-watch</title>"
            "<meta name=\"viewport\" content=\"width=device-width\">"
            "<body style=\"font:15px system-ui,sans-serif;max-width:560px;margin:60px auto;padding:0 16px;"
            "color-scheme:light dark\"><h2>%s</h2><p>%s</p><p style=\"color:gray\">%s</p></body></html>") % (
        "Đã kết nối" if ok else "Chưa kết nối được", html.escape(message),
        "Có thể đóng tab này và quay lại trang theo dõi." if ok else "Quay lại trang theo dõi → Cấu hình → Google Docs.")


# ---- link ------------------------------------------------------------------------------------------------------------

def find_links(text):
    """[(kind, id, url)] — kind: document | spreadsheets | presentation | drawings | forms | file | folder | open.
    Bỏ trùng theo id, giữ thứ tự."""
    out, seen = [], set()
    for m in LINK_RX.finditer((text or "").replace("&amp;", "&")):
        url = m.group(0).rstrip(".,;:")
        u = urllib.parse.urlparse(url)
        mm, fm = ID_RX.search(u.path), FOLDER_RX.search(u.path)
        if mm:
            kind, fid = mm.group(1), mm.group(2)
        elif fm:
            kind, fid = "folder", fm.group(1)
        else:
            fid = (urllib.parse.parse_qs(u.query).get("id") or [""])[0]
            if not re.match(r"^[A-Za-z0-9_-]{20,}$", fid):
                continue
            kind = "open"
        if fid in seen:
            continue
        seen.add(fid)
        out.append((kind, fid, url))
    return out[:MAX_LINKS]


def _fragment_note(url):
    u = urllib.parse.urlparse(url)
    frag = urllib.parse.parse_qs(u.fragment)
    q = urllib.parse.parse_qs(u.query)
    notes = []
    if frag.get("bookmark") or frag.get("heading"):
        notes.append("link trỏ vào %s trong tài liệu — bản xuất không giữ vị trí này, tìm phần liên quan theo nội dung "
                     "ticket" % ("bookmark " + frag["bookmark"][0] if frag.get("bookmark") else
                                 "đề mục " + frag["heading"][0]))
    if q.get("tab") and q["tab"][0] not in ("t.0",):
        notes.append("link mở tab %s của tài liệu (tài liệu nhiều tab)" % q["tab"][0])
    if q.get("gid") or frag.get("gid"):
        notes.append("link trỏ vào trang tính gid=%s" % (q.get("gid") or frag.get("gid"))[0])
    return notes


def _safe(name, used):
    base = re.sub(r"[^\w.\-]+", "_", name or "file", flags=re.UNICODE).strip("._")[:80] or "file"
    out, i = base, 1
    while out.lower() in used:
        out, i = "%s_%d" % (base, i), i + 1
    used.add(out.lower())
    return out


# ---- tải qua Drive API --------------------------------------------------------------------------------------------------

def _api_error(st, body, who):
    """Lỗi Drive API → câu dễ hiểu."""
    e = _json(body).get("error") or {}
    reasons = {x.get("reason") for x in e.get("errors") or [] if isinstance(x, dict)}
    for det in e.get("details") or []:
        if isinstance(det, dict) and det.get("reason"):
            reasons.add(det["reason"])
    msg = (e.get("message") or "")[:160] if isinstance(e, dict) else ""
    if st == 404:
        return "tài khoản %s không có quyền xem tài liệu này (hoặc link sai) — nhờ chủ tài liệu chia sẻ cho %s" % (who, who)
    if reasons & {"accessNotConfigured", "SERVICE_DISABLED"}:
        return ("Google Drive API chưa bật trong project Google Cloud của OAuth client (APIs & Services → Library → "
                "Google Drive API → Enable)")
    if reasons & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientScopes"}:
        return "chưa cấp quyền xem file Drive — bấm Kết nối Google lại và tích ô cho phép xem file Google Drive"
    if "exportSizeLimitExceeded" in reasons:
        return "tài liệu quá lớn để Google xuất (giới hạn 10 MB)"
    if reasons & {"cannotDownloadFile", "cannotExportFile"}:
        return "chủ tài liệu chặn tải xuống / sao chép với người xem"
    if st == 403:
        return "tài khoản %s không có quyền xem tài liệu này (403%s)" % (who, ": " + msg if msg else "")
    if st == 429 or st >= 500:
        return "Google đang bận / giới hạn (%s) — lượt sau thử lại" % st
    return "Drive API lỗi %s%s" % (st, ": " + msg if msg else "")


def _api_get(url, who, limit=None):
    """GET Drive API, tự làm mới token một lần khi 401. → (body, headers)."""
    for attempt in (0, 1):
        tok = access_token(force=attempt == 1)
        st, hdr, body, _ = _request(url, headers={"Authorization": "Bearer " + tok}, limit=limit)
        if st == 401 and attempt == 0:
            continue
        if st == 401:
            raise GError("token Google không hợp lệ — bấm Kết nối Google lại trên trang theo dõi")
        if st != 200:
            raise GError(_api_error(st, body, who), st)
        return body, hdr
    raise GError("không lấy được tài liệu")


def _meta(fid, who):
    q = urllib.parse.urlencode({"fields": "id,name,mimeType,modifiedTime,size,shortcutDetails",
                                "supportsAllDrives": "true"})
    m = _json(_api_get("%s/%s?%s" % (DRIVE, fid, q), who)[0])
    if m.get("mimeType") == G_SHORTCUT and (m.get("shortcutDetails") or {}).get("targetId"):
        return _meta(m["shortcutDetails"]["targetId"], who)
    return m


def _export(fid, mime, who):
    q = urllib.parse.urlencode({"mimeType": mime, "supportsAllDrives": "true"})
    body, _ = _api_get("%s/%s/export?%s" % (DRIVE, fid, q), who, limit=MAX_BIN)
    if len(body) > MAX_BIN:
        raise GError("bản xuất quá %d MB" % (MAX_BIN // 1024 // 1024))
    return body


def _media(fid, who):
    q = urllib.parse.urlencode({"alt": "media", "supportsAllDrives": "true"})
    body, _ = _api_get("%s/%s?%s" % (DRIVE, fid, q), who, limit=MAX_BIN)
    if len(body) > MAX_BIN:
        raise GError("file quá %d MB" % (MAX_BIN // 1024 // 1024))
    return body


def _text(data):
    # Google xuất tiếng Việt có chỗ ở dạng tổ hợp (NFD) → chuẩn NFC để Grep / tìm theo nội dung ticket khớp được.
    return unicodedata.normalize("NFC", data.decode("utf-8-sig", "replace").replace("\r\n", "\n"))


def _pdf_text(path):
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-layout", "-q", path, "-"], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def _convert(meta, fid, who, d):
    """Tải + chuyển một tài liệu vào thư mục d. → (files [{file, chars|None, label}], notes)."""
    mime, name = meta.get("mimeType") or "", meta.get("name") or fid
    used, used_src, files, notes = set(), set(), [], []

    def put_text(stem, ext, text, label="nội dung"):
        cut = text[:MAX_TEXT]
        if len(text) > MAX_TEXT:
            notes.append("chữ dài %d ký tự, chỉ giữ %d" % (len(text), MAX_TEXT))
        fn = _safe(stem, used) + ext
        with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
            f.write(cut)
        files.append({"file": fn, "chars": len(cut), "label": label})

    def put_bin(fn, data, label):
        os.makedirs(os.path.join(d, "src"), exist_ok=True)
        fn = "src/" + _safe(fn, used_src)
        with open(os.path.join(d, fn), "wb") as f:
            f.write(data)
        files.append({"file": fn, "chars": None, "label": label})
        return os.path.join(d, fn)

    if mime == G_DOC:
        try:
            put_text(name, ".md", _text(_export(fid, "text/markdown", who)))
        except GError as ex:
            # 400 = Google không xuất được markdown; quá 10 MB (markdown nhúng ảnh base64) → text thường nhỏ hơn
            # nhiều. Lỗi quyền / mạng thì báo luôn.
            if ex.status != 400 and "quá lớn" not in str(ex):
                raise
            notes.append("xuất markdown lỗi (%s) — dùng text thường" % str(ex)[:80])
            put_text(name, ".txt", _text(_export(fid, "text/plain", who)))
    elif mime == G_SHEET:
        import tg_requests
        data = _export(fid, XLSX, who)
        try:
            put_text(name, ".txt", tg_requests._xlsx_text(data), "chữ mọi trang tính (tối đa 500 dòng / trang)")
            put_bin(name + ".xlsx", data, "bản xlsx")
        except Exception as ex:  # noqa: BLE001 — xlsx lạ: lấy csv
            notes.append("đọc xlsx lỗi (%s) — chỉ lấy csv trang tính đầu tiên" % str(ex)[:80])
            put_text(name, ".csv", _text(_export(fid, "text/csv", who)), "csv — chỉ trang tính đầu tiên")
    elif mime == G_SLIDES:
        put_text(name, ".txt", _text(_export(fid, "text/plain", who)), "chữ trên các trang chiếu")
    elif mime == G_DRAWING:
        fn = _safe(name, used) + ".png"
        with open(os.path.join(d, fn), "wb") as f:
            f.write(_export(fid, "image/png", who))
        files.append({"file": fn, "chars": None, "label": "ảnh (đọc bằng Read)"})
    elif mime == G_FOLDER:
        raise GError("link là thư mục Drive — tool không tải cả thư mục, dán link từng file vào ticket")
    elif mime.startswith("application/vnd.google-apps."):
        raise GError("loại %s chưa hỗ trợ" % mime.rsplit(".", 1)[-1])
    else:
        size = int(meta.get("size") or 0)
        if size > MAX_BIN:
            raise GError("file %s quá %d MB, không tải" % (name, MAX_BIN // 1024 // 1024))
        data = _media(fid, who)
        if mime.startswith("image/"):
            fn = _safe(name, used)
            with open(os.path.join(d, fn), "wb") as f:
                f.write(data)
            files.append({"file": fn, "chars": None, "label": "ảnh (đọc bằng Read)"})
            return files, notes
        path = put_bin(name, data, "bản gốc")
        if mime == "application/pdf" or name.lower().endswith(".pdf"):
            t = _pdf_text(path)
            if t is None:
                notes.append("không trích được chữ PDF (cần pdftotext) — đọc bản gốc")
            else:
                put_text(name, ".txt", t, "chữ trích từ PDF")
        else:
            import tg_requests
            try:
                docs, n2 = tg_requests.extract_docs(path)
            except Exception as ex:  # noqa: BLE001
                docs, n2 = [], ["không trích được chữ: %s" % str(ex)[:120]]
            notes += n2[:5]
            for x in docs:
                put_text(name if x["name"] == os.path.basename(path) else "%s__%s" % (name, x["name"]), ".txt",
                         x["text"], "chữ" if x["name"] == os.path.basename(path) else "chữ của " + x["name"])
            if not docs:
                notes.append("định dạng chưa trích được chữ — đọc bản gốc nếu cần")
    return files, notes


def _cache_dir():
    d = os.path.join(envconf.autofix_root(), ".gdocs-cache")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _fetch_api(fid, who):
    """Tải qua Drive API (có cache theo modifiedTime). → (thư mục cache, result)."""
    meta = _meta(fid, who)
    real = meta.get("id") or fid
    key = hashlib.sha1(("%s|%s|%s" % (CACHE_VER, real, meta.get("modifiedTime"))).encode()).hexdigest()[:12]
    cache = os.path.join(_cache_dir(), "%s-%s" % (real, key))
    rj = os.path.join(cache, "result.json")
    if os.path.exists(rj):
        try:
            with open(rj, encoding="utf-8") as f:
                return cache, json.load(f)
        except (OSError, ValueError):
            pass
    tmp = cache + ".tmp-%d" % os.getpid()
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        files, notes = _convert(meta, real, who, tmp)
        res = {"title": meta.get("name") or real, "mime": meta.get("mimeType"), "modified": meta.get("modifiedTime"),
               "files": files, "notes": notes}
        with open(os.path.join(tmp, "result.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)
        for old in os.listdir(_cache_dir()):  # bản cũ của cùng tài liệu
            if old.startswith(real + "-") and ".tmp-" not in old:
                shutil.rmtree(os.path.join(_cache_dir(), old), ignore_errors=True)
        os.replace(tmp, cache)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return cache, res


# ---- tải ẩn danh (tài liệu công khai, chưa kết nối Google) --------------------------------------------------------------

ANON = {"document": ("https://docs.google.com/document/d/%s/export?format=txt", ".txt", "nội dung"),
        "spreadsheets": ("https://docs.google.com/spreadsheets/d/%s/export?format=csv", ".csv",
                         "csv — chỉ trang tính đầu tiên"),
        "presentation": ("https://docs.google.com/presentation/d/%s/export/txt", ".txt", "chữ trên các trang chiếu")}


def _fetch_anon(kind, fid, d):
    """Tài liệu công khai: tải không cần đăng nhập. Không công khai → GError(lý do)."""
    if kind not in ANON:
        raise GError("cần kết nối Google để tải loại link này")
    url, ext, label = ANON[kind]
    st, hdr, body, final = _request(url % fid, limit=MAX_BIN)
    host = urllib.parse.urlparse(final).netloc
    ctype = (hdr.get("Content-Type") or "") if hdr else ""
    if st in (401, 403) or "accounts.google.com" in host or (st == 200 and ctype.startswith("text/html")):
        raise GError("tài liệu không công khai (%s)" % (st if st != 200 else "chuyển tới trang đăng nhập"))
    if st == 404:
        raise GError("không thấy tài liệu (404) — link sai hoặc đã xoá")
    if st != 200:
        raise GError("Google trả %s" % st)
    title = fid
    m = re.search(r"filename\*=UTF-8''([^;]+)", (hdr.get("Content-Disposition") or "") if hdr else "")
    if m:
        title = os.path.splitext(urllib.parse.unquote(m.group(1)))[0]
    text = _text(body[:MAX_BIN])[:MAX_TEXT]
    fn = _safe(title, set()) + ext
    with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
        f.write(text)
    return {"title": title, "files": [{"file": fn, "chars": len(text), "label": label + " (tải ẩn danh)"}],
            "notes": ["tài liệu công khai — tải không cần đăng nhập"]}


# ---- ghép vào ticket ----------------------------------------------------------------------------------------------------

def fetch_link(kind, fid, url, gd_dir):
    """Tải một link vào gd_dir. → {url, title, mime, modified, files: [{path, chars, label}], notes, error}. Không ném."""
    rec = {"url": url, "kind": kind, "id": fid, "title": None, "mime": None, "modified": None, "files": [],
           "notes": _fragment_note(url), "error": None}
    c = load_cfg()
    who = c.get("connected_email") or "đã kết nối"
    os.makedirs(gd_dir, exist_ok=True)
    try:
        if c.get("refresh_token") and c.get("client_id"):
            cache, res = _fetch_api(fid, who)
            for x in res["files"]:
                dst = os.path.join(gd_dir, x["file"])
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy(os.path.join(cache, x["file"]), dst)
        else:
            try:
                res = _fetch_anon(kind if kind != "open" else "", fid, gd_dir)
            except GError as ex:
                why = "chưa cấu hình" if not c.get("client_id") else "đã cấu hình nhưng chưa kết nối"
                raise GError("chưa cấp quyền Google (%s) — %s. Cấp quyền: trang theo dõi → Cấu hình → Google Docs → "
                             "Kết nối Google bằng tài khoản được chia sẻ tài liệu" % (why, str(ex)))
        rec.update(title=res.get("title"), mime=res.get("mime"), modified=res.get("modified"))
        rec["notes"] += res.get("notes") or []
        rec["files"] = [dict(x, path=os.path.join(gd_dir, x["file"])) for x in res["files"]]
    except GError as ex:
        rec["error"] = str(ex)
    except Exception as ex:  # noqa: BLE001 — một link lỗi không chặn ticket
        rec["error"] = "lỗi không mong đợi: %s" % str(ex)[:200]
    return rec


# ---- dò mục liên quan trong tài liệu dài (SRS hàng trăm nghìn ký tự) -----------------------------------------------------

HEAD_RX = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){1,3})\.?[ \t]+(\S.{0,150})$")
LONG_DOC = 30000       # tài liệu dài hơn (ký tự) thì lập mục lục + dò mục liên quan
SECTIONS_FILE = ".sections.json"
_STOP = set("và của cho các có là được trong khi thì với một những này đó để không theo trên từ ra vào đã sẽ hay hoặc "
            "the and for with this that from are was update cập nhật cho phép".split())


def _words(text):
    text = re.sub(r"https?://\S+", " ", unicodedata.normalize("NFC", text or "").lower())
    return re.findall(r"[0-9a-zà-ỹđ_]+", text)


def _bigrams(words):
    out = set()
    for a, b in zip(words, words[1:]):
        if not (a in _STOP and b in _STOP) and len(a + b) > 4:
            out.add(a + " " + b)
    return out


def headings(lines):
    """[(dòng 1-based, số mục, tiêu đề, độ sâu)] — dòng bắt đầu bằng số mục (2.20 / 2.20.1 …), bỏ dòng thụt (ô bảng)."""
    out = []
    for i, ln in enumerate(lines, 1):
        m = HEAD_RX.match(ln.rstrip())
        if m and not ln[:1].isspace():
            out.append((i, m.group(1), m.group(2).strip(), m.group(1).count(".") + 1))
    return out


def spec_sections(path, query, top=3):
    """Dò các mục (độ sâu 2, vd 2.20) khớp nội dung ticket nhất trong một tài liệu dài. Ghi <file>.muc-luc.md (mục liên
    quan + mục lục kèm số dòng). → {"toc": đường dẫn, "sections": [{num, title, start, end, score, subs}]} hoặc None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except OSError:
        return None
    heads = headings(lines)
    if len(heads) < 3:
        return None
    total = len(lines)
    secs = []
    for idx, (ln, num, title, depth) in enumerate(heads):
        if depth != 2:
            continue
        end = next((h[0] - 1 for h in heads[idx + 1:] if h[3] <= 2), total)
        subs = [(h[0], h[1], h[2]) for h in heads[idx + 1:] if ln < h[0] <= end and h[3] == 3]
        secs.append({"num": num, "title": title, "start": ln, "end": end, "subs": subs})
    if not secs:
        return None
    q = _bigrams(_words(query))
    grams = [_bigrams(_words("\n".join(lines[s["start"] - 1:s["end"]]))) & q for s in secs]
    import math
    df = {}
    for g in grams:
        for x in g:
            df[x] = df.get(x, 0) + 1
    for s, g in zip(secs, grams):
        s["score"] = round(sum(math.log(1 + len(secs) / df[x]) for x in g), 1)
        s["hits"] = sorted(g, key=lambda x: df[x])[:8]
    best = [s for s in sorted(secs, key=lambda s: -s["score"]) if s["score"] > 0][:top]
    toc = path + ".muc-luc.md"
    out = ["# Mục lục %s" % os.path.basename(path), "",
           "Đọc bằng Read(file, offset=<dòng bắt đầu>, limit=<số dòng>). Tìm thêm bằng Grep trên file gốc.", "",
           "## Mục có vẻ liên quan tới ticket (tool tự dò theo từ khoá — vẫn phải tự kiểm tra)", ""]
    for s in best:
        out.append("- **%s %s** — dòng %d–%d (điểm %s; khớp: %s)" % (s["num"], s["title"], s["start"], s["end"],
                                                                   s["score"], ", ".join(s["hits"])))
        out += ["  - %s %s — dòng %d" % (n, t, l) for l, n, t in s["subs"][:30]]
    out += ["", "## Toàn bộ mục lục", ""]
    out += ["%s- %s %s — dòng %d" % ("  " * (h[3] - 1), h[1], h[2], h[0]) for h in heads if h[3] <= 3]
    with open(toc, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return {"toc": toc, "sections": [{k: s[k] for k in ("num", "title", "start", "end", "score")} for s in best]}


def ticket_section(text, out_dir):
    """Tải mọi link Google trong text (mô tả + comment) vào out_dir/gdocs/ → mục "## Tài liệu Google" cho ticket.md
    (đường dẫn tính từ out_dir). "" nếu không có link. Không ném lỗi."""
    links = find_links(text)
    if not links:
        return ""
    gd = os.path.join(out_dir, "gdocs")
    lines = ["", "## Tài liệu Google (tool đã tải về — đọc các tài liệu đặc tả / SRS này trước khi sửa)", "",
             "Đường dẫn tính từ thư mục chứa ticket.md.", ""]
    sec_map = {}
    for kind, fid, url in links:
        r = fetch_link(kind, fid, url, gd)
        if r["error"]:
            lines.append("- %s %s — **không đọc được**: %s" % (KIND_LABEL.get(kind, "Google"), url, r["error"]))
            continue
        lines.append("- **%s** (%s%s) %s" % (r["title"], KIND_LABEL.get(MIME_KIND.get(r["mime"]), KIND_LABEL.get(kind, "")),
                                            ", sửa lần cuối " + r["modified"][:16].replace("T", " ") if r["modified"] else "",
                                            url))
        for x in r["files"]:
            lines.append("  - %s: `%s`%s" % (x["label"], os.path.relpath(x["path"], out_dir),
                                            " (%d ký tự)" % x["chars"] if x["chars"] is not None else ""))
            if (x["chars"] or 0) > LONG_DOC:
                found = spec_sections(x["path"], text)
                if found:
                    sec_map[os.path.relpath(x["path"], out_dir)] = found
                    lines.append("  - mục lục + mục liên quan (tự dò): `%s`" % os.path.relpath(found["toc"], out_dir))
                    lines += ["    - **%s %s** — dòng %d–%d" % (y["num"], y["title"], y["start"], y["end"])
                              for y in found["sections"]]
        lines += ["  - (%s)" % n for n in r["notes"][:6]]
    if sec_map:
        with open(os.path.join(gd, SECTIONS_FILE), "w", encoding="utf-8") as f:
            json.dump(sec_map, f, ensure_ascii=False)
    try:
        os.rmdir(gd)  # không tải được gì → bỏ thư mục rỗng
    except OSError:
        pass
    return "\n".join(lines) + "\n"


def text_files(ticket_dir):
    """File chữ đã tải (ticket/gdocs/*.md|txt|csv) — cho QA / tách việc."""
    gd = os.path.join(ticket_dir, "gdocs")
    try:
        return [os.path.join(gd, n) for n in sorted(os.listdir(gd)) if n.endswith((".md", ".txt", ".csv")) and not n.endswith(".muc-luc.md")]
    except OSError:
        return []


def test_link(url):
    """Thử một link từ trang theo dõi: tên + 300 ký tự đầu, hoặc lỗi."""
    import tempfile
    links = find_links(url)
    if not links:
        raise ValueError("không nhận ra link Google Docs / Sheets / Slides / Drive")
    tmp = tempfile.mkdtemp(prefix="gdocs-test-")
    try:
        r = fetch_link(*links[0], tmp)
        if r["error"]:
            return {"ok": False, "message": r["error"]}
        first = next((x for x in r["files"] if x["chars"] is not None), None)
        prev = ""
        if first:
            with open(first["path"], encoding="utf-8", errors="replace") as f:
                prev = f.read(300)
        total = sum(x["chars"] or 0 for x in r["files"])
        return {"ok": True, "title": r["title"], "chars": total, "preview": prev, "notes": r["notes"],
                "message": "Đọc được \"%s\" (%d ký tự)" % (r["title"], total)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def action(body, port):
    """Thao tác từ trang theo dõi (action gdocs_*)."""
    kind = body.get("action")
    try:
        if kind == "gdocs_status":
            return status()
        if kind == "gdocs_save":
            msg = save_settings(body.get("client_id"), body.get("client_secret"), body.get("email"))
            return dict(status(), message=msg)
        if kind == "gdocs_auth_url":
            return {"url": auth_url(port), "redirect": redirect_uri(port),
                    "message": "Đăng nhập Google ở tab mới (hiệu lực %d phút)" % (STATE_TTL // 60)}
        if kind == "gdocs_finish":
            return dict(status(), message=finish_from_url(body.get("url")))
        if kind == "gdocs_test":
            return test_link(body.get("url") or "")
        if kind == "gdocs_disconnect":
            return dict(status(), message=disconnect())
    except GError as ex:
        raise ValueError(str(ex))
    raise ValueError("thao tác Google không hợp lệ")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if sys.argv[1] == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=1))
        sys.exit(0)
    sec = ticket_section(" ".join(sys.argv[1:]), os.getcwd())
    print(sec or "không thấy link Google Docs / Drive")
