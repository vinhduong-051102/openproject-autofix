#!/usr/bin/env python3
"""
soar_api: gọi API / tải file của server test của dự án (DEPLOY_FE_URL / DEPLOY_API_URL, hoặc Server test của dự án
trong Cấu hình → Dự án; dự án lấy theo biến OP_PROJECT_KEY) cho QA tự động / Claude hỗ trợ.

Cách đăng nhập theo "auth" của dự án (Cấu hình → Dự án → Đăng nhập):
  kind "soar"   (mặc định) POST /api/v2/auth/login rồi /api/v2/auth/validate-otp (app_otp) → data.access_token
  kind "json"   POST login_path với login_body (mẫu JSON, {user} {password} {otp}); otp_path / otp_body nếu có bước
                OTP; token lấy theo token_path (vd data.access_token; trống = tự tìm access_token / token / jwt…),
                không có token mà server đặt cookie → gửi lại cookie. form: true = gửi dạng form thay vì JSON.
  kind "static" token cố định (API key) gửi ở header (mặc định Authorization: Bearer <token>)
  kind "none"   không đăng nhập
  header / scheme: tên header + tiền tố token (mặc định Authorization / Bearer; scheme "-" = gửi nguyên token)

    soar_api.py GET /api/v1/alerts/filter                 # gọi API (tự đăng nhập, Bearer token)
    soar_api.py POST /api/v1/x '{"a": 1}'                  # body JSON
    soar_api.py PUT /api/v1/x @body.json                   # body từ file
    soar_api.py UPLOAD /api/v1/file-attachments/upload file=@/path/a.txt type=1 caseId=5   # multipart
    soar_api.py UPLOAD /api/... 'file=@/path/a.bin;filename=QA_<b>/x.txt'   # tên file gửi đi khác tên trên đĩa
    soar_api.py GET $DEPLOY_FE_URL/index.html             # file tĩnh của FE (không cần token)
    soar_api.py --header 'Accept-Language: en' GET /api/...
    soar_api.py --no-auth POST /api/v2/auth/login '{...}'
    soar_api.py --as ten_tai_khoan GET /api/...               # tài khoản khác trong deploy.json (mặc định default_account)
    soar_api.py --as QA_AUTO_khach --password 'Xyz@123' GET /api/...   # tài khoản test QA tự tạo (tên QA_AUTO_*);
                                                           # mật khẩu được nhớ, lần sau chỉ cần --as

In ra: dòng `HTTP <mã>`, các header quan trọng, rồi body (JSON được format). Chỉ cho gọi tới fe_url / api_url trong
cấu hình (DEPLOY_FE_URL / DEPLOY_API_URL trong .env hoặc deploy.json). Token lưu ở $OP_WATCH_CACHE_DIR (mặc định
~/.cache/op-watch); hết hạn (401) thì tự đăng nhập lại.
Lưu ý: đăng nhập mới sẽ đăng xuất phiên trình duyệt đang mở của cùng tài khoản.
"""

import fcntl
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402  (nạp .env)

CONFIG = envconf.DEPLOY_CONFIG
CACHE = envconf.CACHE_DIR
QA_ACCOUNTS = os.path.join(CACHE, "qa_accounts.json")


def load_config():
    """deploy.json + biến DEPLOY_* trong .env / môi trường (biến môi trường thắng)."""
    cfg = envconf.deploy_config()
    if not cfg.get("api_url"):
        sys.exit("Thiếu DEPLOY_API_URL (.env) hoặc \"api_url\" trong %s" % CONFIG)
    return cfg


RESET_RX = re.compile(r"/api/v\d+/users/(reset-password|change-password|lock|unlock|force-logout)", re.I)


def guard_tester_accounts(cfg, method, target):
    """Không cho QA đặt lại mật khẩu / khoá / đăng xuất cưỡng bức người dùng: tài khoản tester dùng chung với người thật."""
    if method != "GET" and RESET_RX.search(urllib.parse.urlsplit(target).path):
        m = re.search(r"/(\d+)(?:$|[/?])", urllib.parse.urlsplit(target).path)
        if not m or not _is_qa_auto(cfg, m.group(1)):
            sys.exit("Chỉ được đặt lại mật khẩu / khoá tài khoản QA_AUTO_* (id %s không phải QA_AUTO_*)" % (m.group(1) if m else "?"))


def _is_qa_auto(cfg, user_id):
    """Hỏi server user id này có tên QA_AUTO_* không (bằng tài khoản mặc định)."""
    try:
        name, pw = credentials(cfg, None)
        token = shared_login(cfg, name, pw)
        code, _, body = send("GET", cfg["api_url"].rstrip("/") + "/api/v1/users/%s" % user_id, None,
                             auth_headers(cfg, token))
        return code == 200 and str((json.loads(body or b"{}").get("data") or {}).get("username", "")).startswith("QA_AUTO_")
    except (ValueError, OSError, SystemExit):
        return False


def resolve(cfg, target):
    if target.startswith("/"):
        return cfg["api_url"].rstrip("/") + target
    allowed = {urllib.parse.urlsplit(cfg[k]).netloc for k in ("fe_url", "api_url")}
    if urllib.parse.urlsplit(target).netloc not in allowed:
        sys.exit("Chỉ được gọi tới %s" % ", ".join(sorted(allowed)))
    return target


def send(method, url, body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, _Headers(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as ex:
        return ex.code, _Headers(ex.headers.items()), ex.read()


class _Headers(dict):
    """dict header (khoá trùng: cái cuối) nhưng items() giữ đủ danh sách (nhiều Set-Cookie)."""
    def __init__(self, pairs):
        pairs = list(pairs)
        super().__init__(pairs)
        self.pairs = pairs

    def items(self):
        return self.pairs


def credentials(cfg, name, password=None):
    """(username, password): tài khoản trong deploy.json; tài khoản test QA_AUTO_*; hoặc tài khoản tester ghi trong ticket
    (nằm trong qa_ticket_accounts) — hai loại sau: mật khẩu truyền --password lần đầu, tool nhớ."""
    if not cfg.get("accounts") and not cfg.get("app_user"):
        sys.exit("Chưa có tài khoản đăng nhập app (Cấu hình → Dự án → Server test → Tài khoản đăng nhập app)")
    accounts = cfg.get("accounts") or {cfg["app_user"]: {"password": cfg.get("app_password") or ""}}
    name = name or cfg.get("default_account") or cfg["app_user"]
    if name in accounts:
        return name, accounts[name]["password"]
    ticket_accounts = set(cfg.get("qa_ticket_accounts") or [])
    if not name.startswith("QA_AUTO_") and name not in ticket_accounts:
        sys.exit("Tài khoản %s không có trong deploy.json; tài khoản test tự tạo phải tên QA_AUTO_*; tài khoản tester "
                 "ghi trong ticket phải nằm trong qa_ticket_accounts (%s)"
                 % (name, ", ".join(sorted(ticket_accounts)) or "đang trống"))
    saved = {}
    if os.path.exists(QA_ACCOUNTS):
        with open(QA_ACCOUNTS, encoding="utf-8") as f:
            saved = json.load(f)
    if password:
        saved[name] = password
        os.makedirs(CACHE, mode=0o700, exist_ok=True)
        with open(QA_ACCOUNTS, "w", encoding="utf-8") as f:
            json.dump(saved, f)
        os.chmod(QA_ACCOUNTS, 0o600)
    if name not in saved:
        sys.exit("Chưa biết mật khẩu của %s: truyền --password (QA_AUTO_*: lấy từ PUT /api/v1/users/reset-password/{id}; tài khoản tester: mật khẩu ghi trong ticket)" % name)
    return name, saved[name]


def auth_cfg(cfg):
    a = dict(cfg.get("auth") or {})
    a["kind"] = a.get("kind") or "soar"
    return a


def _scope(cfg):
    """Tiền tố file cache theo dự án (dự án kiểu SOAR giữ tên cũ)."""
    return "" if auth_cfg(cfg)["kind"] == "soar" else "%s_" % re.sub(r"[^\w.-]", "_", cfg.get("_project") or "p")


def token_path(user, cfg=None):
    sc = _scope(cfg or {})
    return os.path.join(CACHE, "%s_token_%s%s" % ("api" if sc else "soar", sc, re.sub(r"[^\w.-]", "_", user or "-")))


def _fill(tpl, user, password, otp, json_mode=True):
    """Điền {user} {password} {otp} vào mẫu body (giá trị được escape JSON khi body là JSON)."""
    esc = (lambda v: json.dumps(str(v or ""))[1:-1]) if json_mode else (lambda v: str(v or ""))
    return tpl.replace("{user}", esc(user)).replace("{password}", esc(password)).replace("{otp}", esc(otp))


def _dig(data, path):
    for k in [x for x in (path or "").split(".") if x]:
        if isinstance(data, list) and k.isdigit() and int(k) < len(data):
            data = data[int(k)]
        elif isinstance(data, dict):
            data = data.get(k)
        else:
            return None
    return data


TOKEN_KEYS = ("access_token", "accessToken", "token", "jwt", "id_token", "idToken", "auth_token", "authToken")


def find_token(data, path=""):
    """Token trong JSON trả về: theo token_path, không có thì tìm các khoá hay gặp (cả trong data / result)."""
    if path:
        v = _dig(data, path)
        return v if isinstance(v, str) else ""
    for root in (data, (data or {}).get("data") if isinstance(data, dict) else None,
                 (data or {}).get("result") if isinstance(data, dict) else None):
        if isinstance(root, dict):
            for k in TOKEN_KEYS:
                if isinstance(root.get(k), str) and root[k]:
                    return root[k]
    return ""


def auth_headers(cfg, token):
    """Header gửi kèm request API theo cách đăng nhập của dự án."""
    a = auth_cfg(cfg)
    if a["kind"] == "none" or not token:
        return {}
    if token.startswith("cookie:"):
        return {"Cookie": token[7:]}
    scheme = a.get("scheme") or "Bearer"  # "-" = gửi nguyên token, không tiền tố
    return {a.get("header") or "Authorization": ("%s %s" % (scheme, token)) if scheme != "-" else token}


def shared_login(cfg, user, password, stale=None):
    """
    Đăng nhập có khoá: nhiều tiến trình QA chạy song song dùng chung một token. Đăng nhập mới làm phiên cũ của tài
    khoản hết hạn, nên chỉ một tiến trình đăng nhập; tiến trình khác thấy token đã đổi (khác `stale`) thì dùng luôn.
    """
    os.makedirs(CACHE, mode=0o700, exist_ok=True)
    with open(token_path(user, cfg) + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        tp = token_path(user, cfg)
        if os.path.exists(tp):
            with open(tp, encoding="utf-8") as f:
                cur = f.read().strip()
            if cur and cur != stale:
                return cur
        return login(cfg, user, password)


def _login_json(cfg, a, user, password):
    """kind "json": POST login_path (+ bước OTP) → token / cookie."""
    base = cfg["api_url"].rstrip("/")
    form = bool(a.get("form"))
    otp = cfg.get("app_otp") or ""

    def post(path, tpl):
        if form:
            body = urllib.parse.urlencode(json.loads(_fill(tpl, user, password, otp))).encode()
            hdr = {"Content-Type": "application/x-www-form-urlencoded"}
        else:
            body, hdr = _fill(tpl, user, password, otp).encode(), {"Content-Type": "application/json"}
        url = path if path.startswith("http") else base + "/" + path.lstrip("/")
        return send("POST", url, body, hdr)
    code, headers, body = post(a.get("login_path") or "/api/auth/login",
                               a.get("login_body") or '{"username": "{user}", "password": "{password}"}')
    if a.get("otp_path"):
        code, headers, body = post(a["otp_path"], a.get("otp_body") or '{"username": "{user}", "otp": "{otp}"}')
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        data = {}
    token = find_token(data, a.get("token_path") or "")
    if not token and code < 400:
        cookie = "; ".join(v.split(";", 1)[0] for k, v in headers.items() if k.lower() == "set-cookie")
        token = "cookie:" + cookie if cookie else ""
    if code >= 400 or not token:
        sys.exit("Đăng nhập %s thất bại: HTTP %s %s" % (user, code, body[:300]))
    return token


def login(cfg, user, password):
    a = auth_cfg(cfg)
    if a["kind"] == "static":
        if not a.get("token"):
            sys.exit("Dự án dùng token cố định nhưng chưa nhập token (Cấu hình → Dự án → Đăng nhập)")
        return a["token"]
    if a["kind"] == "none":
        return ""
    if a["kind"] == "json":
        token = _login_json(cfg, a, user, password)
    else:
        base = cfg["api_url"].rstrip("/")
        hdr = {"Content-Type": "application/json"}
        send("POST", base + "/api/v2/auth/login",
             json.dumps({"username": user, "password": password}).encode(), hdr)
        code, _, body = send("POST", base + "/api/v2/auth/validate-otp",
                             json.dumps({"username": user, "otp": cfg.get("app_otp") or "", "type": 0}).encode(), hdr)
        data = json.loads(body.decode("utf-8") or "{}")
        token = (data.get("data") or {}).get("access_token")
        if code != 200 or not token:
            sys.exit("Đăng nhập %s thất bại: HTTP %s %s" % (user, code, body[:300]))
    os.makedirs(CACHE, mode=0o700, exist_ok=True)
    with open(token_path(user, cfg), "w", encoding="utf-8") as f:
        f.write(token)
    os.chmod(token_path(user, cfg), 0o600)
    return token


def multipart(fields):
    boundary = uuid.uuid4().hex
    out = []
    for item in fields:
        key, _, value = item.partition("=")
        out.append(b"--" + boundary.encode())
        if value.startswith("@"):
            # file=@/duong/dan[;filename=TEN] — TEN gửi nguyên văn (có thể chứa "/", "<", ký tự Unicode…) để test
            # đúng cách server xử lý tên file; không có thì dùng tên file trên đĩa.
            path, sep, given = value[1:].partition(";filename=")
            name = given if sep else os.path.basename(path)
            name = name.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            out.append(('Content-Disposition: form-data; name="%s"; filename="%s"' % (key, name)).encode())
            out.append(("Content-Type: " + ctype).encode())
            out.append(b"")
            with open(path, "rb") as f:
                out.append(f.read())
        else:
            out.append(('Content-Disposition: form-data; name="%s"' % key).encode())
            out.append(b"")
            out.append(value.encode())
    out.append(b"--" + boundary.encode() + b"--")
    out.append(b"")
    return b"\r\n".join(out), "multipart/form-data; boundary=" + boundary


def main():
    args = sys.argv[1:]
    extra, auth, account, password = {}, True, None, None
    while args and args[0].startswith("--"):
        flag = args.pop(0)
        if flag == "--header":
            k, _, v = args.pop(0).partition(":")
            extra[k.strip()] = v.strip()
        elif flag == "--no-auth":
            auth = False
        elif flag == "--as":
            account = args.pop(0)
        elif flag == "--password":
            password = args.pop(0)
    if len(args) < 2:
        sys.exit(__doc__)
    method, target, rest = args[0].upper(), args[1], args[2:]
    cfg = load_config()
    url = resolve(cfg, target)
    guard_tester_accounts(cfg, "POST" if method == "UPLOAD" else method, url)
    body, headers = None, dict(extra)
    if method == "UPLOAD":
        method = "POST"
        body, headers["Content-Type"] = multipart(rest)
    elif rest:
        raw = rest[0]
        if raw.startswith("@"):
            with open(raw[1:], "rb") as f:
                body = f.read()
        else:
            body = raw.encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    is_api = url.startswith(cfg["api_url"].rstrip("/"))
    kind = auth_cfg(cfg)["kind"]
    if kind == "none":
        auth = False
    if auth and is_api:
        if kind == "static":
            user, pw, token = "", "", login(cfg, "", "")
        else:
            user, pw = credentials(cfg, account, password)
            tp = token_path(user, cfg)
            if password:
                token = shared_login(cfg, user, pw, stale=open(tp).read().strip() if os.path.exists(tp) else None)
            else:
                token = open(tp, encoding="utf-8").read().strip() if os.path.exists(tp) else shared_login(cfg, user, pw)
        headers.update(auth_headers(cfg, token))
    code, resp_headers, data = send(method, url, body, headers)
    if code == 401 and auth and is_api and kind != "static":
        headers.update(auth_headers(cfg, shared_login(cfg, user, pw, stale=token)))
        code, resp_headers, data = send(method, url, body, headers)
    print("HTTP %s" % code)
    for key in ("Content-Type", "Content-Disposition", "Content-Length", "Location"):
        if key in resp_headers:
            print("%s: %s" % (key, resp_headers[key]))
    print()
    text = data.decode("utf-8", "replace")
    try:
        text = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
    except ValueError:
        pass
    print(text[:20000] + ("\n… (cắt, tổng %d ký tự)" % len(text) if len(text) > 20000 else ""))


if __name__ == "__main__":
    main()
