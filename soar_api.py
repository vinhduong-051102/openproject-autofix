#!/usr/bin/env python3
"""
soar_api: gọi API / tải file của môi trường test SOAR (DEPLOY_FE_URL / DEPLOY_API_URL) cho bước QA tự động.

    soar_api.py GET /api/v1/alerts/filter                 # gọi API (tự đăng nhập, Bearer token)
    soar_api.py POST /api/v1/x '{"a": 1}'                  # body JSON
    soar_api.py PUT /api/v1/x @body.json                   # body từ file
    soar_api.py UPLOAD /api/v1/file-attachments/upload file=@/path/a.txt type=1 caseId=5   # multipart
    soar_api.py UPLOAD /api/... 'file=@/path/a.bin;filename=QA_<b>/x.txt'   # tên file gửi đi khác tên trên đĩa
    soar_api.py GET $DEPLOY_FE_URL/index.html             # file tĩnh của FE (không cần token)
    soar_api.py --header 'Accept-Language: en' GET /api/...
    soar_api.py --no-auth POST /api/v2/auth/login '{...}'
    soar_api.py --as vinhduong GET /api/...               # tài khoản khác trong deploy.json (mặc định default_account)
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
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as ex:
        return ex.code, dict(ex.headers), ex.read()


def credentials(cfg, name, password=None):
    """(username, password) của tài khoản: trong deploy.json, hoặc tài khoản test QA_AUTO_* (mật khẩu truyền / đã nhớ)."""
    accounts = cfg.get("accounts") or {cfg["app_user"]: {"password": cfg["app_password"]}}
    name = name or cfg.get("default_account") or cfg["app_user"]
    if name in accounts:
        return name, accounts[name]["password"]
    if not name.startswith("QA_AUTO_"):
        sys.exit("Tài khoản %s không có trong deploy.json; tài khoản test tự tạo phải tên QA_AUTO_*" % name)
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
        sys.exit("Chưa biết mật khẩu của %s: truyền --password (lấy từ PUT /api/v1/users/reset-password/{id})" % name)
    return name, saved[name]


def token_path(user):
    return os.path.join(CACHE, "soar_token_%s" % re.sub(r"[^\w.-]", "_", user))


def shared_login(cfg, user, password, stale=None):
    """
    Đăng nhập có khoá: nhiều tiến trình QA chạy song song dùng chung một token. Đăng nhập mới làm phiên cũ của tài
    khoản hết hạn, nên chỉ một tiến trình đăng nhập; tiến trình khác thấy token đã đổi (khác `stale`) thì dùng luôn.
    """
    os.makedirs(CACHE, mode=0o700, exist_ok=True)
    with open(token_path(user) + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        tp = token_path(user)
        if os.path.exists(tp):
            with open(tp, encoding="utf-8") as f:
                cur = f.read().strip()
            if cur and cur != stale:
                return cur
        return login(cfg, user, password)


def login(cfg, user, password):
    base = cfg["api_url"].rstrip("/")
    hdr = {"Content-Type": "application/json"}
    send("POST", base + "/api/v2/auth/login",
         json.dumps({"username": user, "password": password}).encode(), hdr)
    code, _, body = send("POST", base + "/api/v2/auth/validate-otp",
                         json.dumps({"username": user, "otp": cfg["app_otp"], "type": 0}).encode(), hdr)
    data = json.loads(body.decode("utf-8") or "{}")
    token = (data.get("data") or {}).get("access_token")
    if code != 200 or not token:
        sys.exit("Đăng nhập %s thất bại: HTTP %s %s" % (user, code, body[:300]))
    os.makedirs(CACHE, mode=0o700, exist_ok=True)
    with open(token_path(user), "w", encoding="utf-8") as f:
        f.write(token)
    os.chmod(token_path(user), 0o600)
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
    if auth and is_api:
        user, pw = credentials(cfg, account, password)
        tp = token_path(user)
        if password:
            token = shared_login(cfg, user, pw, stale=open(tp).read().strip() if os.path.exists(tp) else None)
        else:
            token = open(tp, encoding="utf-8").read().strip() if os.path.exists(tp) else shared_login(cfg, user, pw)
        headers["Authorization"] = "Bearer " + token
    code, resp_headers, data = send(method, url, body, headers)
    if code == 401 and auth and is_api:
        headers["Authorization"] = "Bearer " + shared_login(cfg, user, pw, stale=token)
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
