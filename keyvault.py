#!/usr/bin/env python3
"""
keyvault: lấy key (token Claude của các gói…) từ KeyVault theo HUONG_DAN_LAY_KEY.md
(https://github.com/vinhduong-051102/key_store).

Mỗi lần lấy key: POST /api/v1/exports (bản mã) → POST /exports/{id}/unlock với OTP Google Authenticator (khoá giải mã
đã bọc bằng public key của máy này) → giải mã tại máy (X25519 + HKDF-SHA256 + AES-256-GCM).

Lưu ở ~/.config/op-watch (quyền 600, không commit):
  keyvault.json            server, request_id, claim_token, api_key, recipient_id
  keyvault-recipient.key   private key X25519 — TỰ SINH trên máy này (Cách A an toàn: private key không đi qua mạng)

Đăng ký (một lần) — trên trang theo dõi (Hệ thống → Agent & hạn mức → KeyVault) hoặc dòng lệnh:
  python3 keyvault.py request --name "op-watch vinhdq" --contact a@b.vn --reason "Lấy token Claude cho autofix"
  python3 keyvault.py status
  python3 keyvault.py claim            # khi đã duyệt: in secret để thêm vào Google Authenticator
  python3 keyvault.py activate --otp 123456
Đã có API key (quản trị viên đăng ký giúp — Cách B):
  python3 keyvault.py import --api-key kv_… [--priv recipient.key]   # không có --priv: dùng khoá của máy này
                                                                     # (pubkey in public key để gửi quản trị viên)
Lấy key:
  python3 keyvault.py keys
  python3 keyvault.py fetch --key-id <id> --otp 123456 [--profile "Gói của Dũng"]   # --profile: ghi thành token gói

Server: KV_SERVER (env) → "keyvault.server" trong config.json → http://100.112.133.34:8000. http:// chỉ cho phép với
địa chỉ Tailscale (100.64.0.0/10) / mạng nội bộ / localhost (đường truyền đã mã hoá hoặc không ra Internet); còn lại
phải https://.
"""
import argparse
import base64
import ipaddress
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402

DEFAULT_SERVER = "http://100.112.133.34:8000"
STATE_PATH = os.path.join(envconf.CONFIG_DIR, "keyvault.json")
PRIV_PATH = os.path.join(envconf.CONFIG_DIR, "keyvault-recipient.key")
WRAP_INFO = b"keyvault-export-wrap-v1"


class KVError(Exception):
    pass


# ---------- crypto (cryptography ≥ 2.5; truyền backend cho bản cũ trên Python 3.8) ----------

def _crypto():
    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as ex:
        raise KVError("thiếu thư viện cryptography (apt install python3-cryptography): %s" % ex)
    return {"backend": default_backend(), "hashes": hashes, "ser": serialization, "Priv": X25519PrivateKey,
            "Pub": X25519PublicKey, "AESGCM": AESGCM, "HKDF": HKDF}


def _raw_pub(pub):
    c = _crypto()
    return pub.public_bytes(c["ser"].Encoding.Raw, c["ser"].PublicFormat.Raw)


def _write_private(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


def load_private_key(path=None):
    path = path or PRIV_PATH
    c = _crypto()
    try:
        data = open(path, "rb").read()
    except FileNotFoundError:
        raise KVError("chưa có private key (%s)" % path)
    try:
        key = c["ser"].load_pem_private_key(data, password=None, backend=c["backend"])
    except TypeError:
        raise KVError("private key đang đặt passphrase — tool chạy nền cần khoá không passphrase")
    if not isinstance(key, c["Priv"]):
        raise KVError("private key không phải X25519")
    return key


def ensure_key():
    """Private key của máy này (sinh mới nếu chưa có). Trả public key base64."""
    if not os.path.exists(PRIV_PATH):
        c = _crypto()
        priv = c["Priv"].generate()
        _write_private(PRIV_PATH, priv.private_bytes(c["ser"].Encoding.PEM, c["ser"].PrivateFormat.PKCS8,
                                                     c["ser"].NoEncryption()))
    return base64.b64encode(_raw_pub(load_private_key().public_key())).decode()


def _gcm_open(key, blob, aad):
    from cryptography.exceptions import InvalidTag
    try:
        return _crypto()["AESGCM"](key).decrypt(blob[:12], blob[12:], aad)
    except InvalidTag:
        raise KVError("giải mã thất bại — private key của máy này không khớp public key đã đăng ký")


def decrypt(priv, created, unlocked, aad):
    c = _crypto()
    wrapped = base64.b64decode(unlocked["wrapped_key"])
    eph = wrapped[:32]
    shared = priv.exchange(c["Pub"].from_public_bytes(eph))
    kek = c["HKDF"](algorithm=c["hashes"].SHA256(), length=32, salt=None,
                    info=WRAP_INFO + eph + _raw_pub(priv.public_key()), backend=c["backend"]).derive(shared)
    dek = _gcm_open(kek, wrapped[32:], aad)
    return _gcm_open(dek, base64.b64decode(created["ciphertext"]), aad)


# ---------- trạng thái ----------

def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(st):
    _write_private(STATE_PATH, json.dumps(st, ensure_ascii=False, indent=2).encode())


def server(st=None):
    st = load_state() if st is None else st
    if os.environ.get("KV_SERVER"):
        return os.environ["KV_SERVER"].rstrip("/")
    if st.get("server"):
        return st["server"].rstrip("/")
    cfg = envconf._read_json(os.path.join(envconf.CONFIG_DIR, "config.json"))
    return ((cfg.get("keyvault") or {}).get("server") or DEFAULT_SERVER).rstrip("/")


def _http_ok(url):
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("localhost",):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")


def check_server(url):
    url = (url or "").strip().rstrip("/")
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise KVError("địa chỉ KeyVault không hợp lệ: %r (vd http://100.112.133.34:8000)" % url)
    if p.scheme == "http" and not _http_ok(url) and not os.environ.get("KV_ALLOW_HTTP"):
        raise KVError("từ chối http:// tới địa chỉ Internet — dùng https:// (http chỉ cho Tailscale / mạng nội bộ)")
    return url


def call(method, path, body=None, api_key=None, headers=None, st=None):
    url = check_server(server(st)) + path
    hdrs = {"Content-Type": "application/json", "User-Agent": "op-watch-kv/1.0"}
    if api_key:
        hdrs["X-API-Key"] = api_key
    hdrs.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    ctx = ssl.create_default_context() if url.startswith("https://") else None
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        try:
            detail = json.loads(err.read()).get("detail")
        except Exception:  # noqa: BLE001
            detail = err.reason
        if isinstance(detail, list):  # lỗi kiểm tra dữ liệu của FastAPI
            detail = "; ".join("%s: %s" % (".".join(str(x) for x in d.get("loc", [])[1:]), d.get("msg")) for d in detail)
        raise KVError("KeyVault %s: %s" % (err.code, detail))
    except (urllib.error.URLError, OSError) as ex:
        raise KVError("không kết nối được KeyVault %s: %s" % (server(st), getattr(ex, "reason", ex)))


def _api_key(st):
    key = os.environ.get("KV_API_KEY") or st.get("api_key")
    if not key:
        raise KVError("chưa có API key KeyVault — đăng ký trước (Hệ thống → Agent & hạn mức → KeyVault)")
    return key


def _claim_hdr(st):
    if not st.get("request_id") or not st.get("claim_token"):
        raise KVError("chưa gửi yêu cầu đăng ký")
    return {"X-Claim-Token": st["claim_token"]}


def summary():
    """Trạng thái cục bộ (không gọi mạng) cho trang theo dõi. Không trả bí mật."""
    st = load_state()
    stage = st.get("stage") or ("active" if st.get("api_key") and st.get("activated") else
                                "claimed" if st.get("api_key") else "pending" if st.get("request_id") else "none")
    return {"server": server(st), "stage": stage, "name": st.get("name") or "", "request_id": st.get("request_id") or "",
            "recipient_id": st.get("recipient_id") or "", "has_key": os.path.exists(PRIV_PATH),
            "api_key_tail": (st.get("api_key") or "")[-4:], "review_note": st.get("review_note") or "",
            "claim_expires_at": st.get("claim_expires_at") or "",
            "totp_secret": st.get("totp_secret") or "" if stage == "claimed" else "",
            "otpauth_uri": st.get("otpauth_uri") or "" if stage == "claimed" else "",
            "keys": st.get("keys") or []}


# ---------- đăng ký ----------

def set_server(url):
    st = load_state()
    st["server"] = check_server(url)
    save_state(st)
    return st["server"]


def request(name, contact, reason, organization="", requested_keys="", url=None):
    st = load_state()
    if st.get("request_id") and st.get("stage") not in ("rejected",):
        raise KVError("đã gửi yêu cầu %s — bấm Gỡ kết nối nếu muốn gửi lại" % st["request_id"])
    if not (name or "").strip() or not (contact or "").strip() or not (reason or "").strip():
        raise KVError("cần tên, liên hệ và lý do")
    st = {"server": check_server(url or server(st))}
    pub = ensure_key()
    res = call("POST", "/api/v1/access-requests", {
        "name": name.strip(), "contact": contact.strip(), "organization": (organization or "").strip(),
        "reason": reason.strip(), "requested_keys": (requested_keys or "").strip(), "public_key": pub}, st=st)
    st.update({"request_id": res["request_id"], "claim_token": res["claim_token"], "name": name.strip(),
               "stage": "pending"})
    save_state(st)
    return "Đã gửi yêu cầu %s — chờ quản trị viên duyệt" % res["request_id"]


def refresh_status():
    st = load_state()
    if st.get("stage") in ("claimed", "active"):
        return summary()
    res = call("GET", "/api/v1/access-requests/%s" % st.get("request_id"), headers=_claim_hdr(st), st=st)
    st["stage"] = {"approved": "approved", "rejected": "rejected", "claimed": "claimed_elsewhere"}.get(
        res.get("status"), "pending")
    st["review_note"] = res.get("review_note") or ""
    st["claim_expires_at"] = res.get("claim_expires_at") or ""
    save_state(st)
    return summary()


def claim():
    st = load_state()
    res = call("POST", "/api/v1/access-requests/%s/claim" % st.get("request_id"), {}, headers=_claim_hdr(st), st=st)
    if res.get("private_key_pem"):  # server tự sinh khoá (yêu cầu gửi không kèm public key)
        _write_private(PRIV_PATH, res["private_key_pem"].encode())
    elif res.get("public_key") and res["public_key"] != ensure_key():
        raise KVError("public key trên server không khớp private key của máy này — huỷ")
    # Secret TOTP giữ tạm tới khi kích hoạt xong (để trang hiện lại nếu tải lại), kích hoạt xong thì xoá.
    st.update({"api_key": res["api_key"], "recipient_id": res.get("recipient_id") or "", "stage": "claimed",
               "totp_secret": res.get("totp_secret") or "", "otpauth_uri": res.get("otpauth_uri") or "",
               "keys": [_brief(k) for k in res.get("granted_keys") or []]})
    save_state(st)
    return summary()


def activate(otp):
    st = load_state()
    call("POST", "/api/v1/access-requests/%s/activate" % st.get("request_id"), {"otp": _otp(otp)},
         headers=_claim_hdr(st), st=st)
    st.update({"stage": "active", "activated": True})
    st.pop("totp_secret", None)
    st.pop("otpauth_uri", None)
    save_state(st)
    return "Đã kích hoạt KeyVault"


def import_key(api_key, priv_pem=None, url=None):
    """Cách B: quản trị viên đã đăng ký giúp (gửi API key). priv_pem: khoá riêng có sẵn (PEM), bỏ trống = khoá máy này."""
    api_key = (api_key or "").strip()
    if not api_key.startswith("kv_"):
        raise KVError("API key KeyVault phải dạng kv_…")
    st = load_state()
    if url:
        st["server"] = check_server(url)
    if priv_pem and priv_pem.strip():
        _write_private(PRIV_PATH + ".new", priv_pem.strip().encode() + b"\n")
        try:
            load_private_key(PRIV_PATH + ".new")
        except KVError:
            os.remove(PRIV_PATH + ".new")
            raise
        os.replace(PRIV_PATH + ".new", PRIV_PATH)
    ensure_key()
    st.update({"api_key": api_key, "stage": "active", "activated": True})
    for k in ("request_id", "claim_token", "totp_secret", "otpauth_uri"):
        st.pop(k, None)
    save_state(st)
    keys()  # kiểm tra API key ngay
    return "Đã lưu API key KeyVault"


def reset():
    """Gỡ kết nối: xoá API key / yêu cầu. Giữ private key (đổi tên .old) phòng còn cần."""
    if os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)
    if os.path.exists(PRIV_PATH):
        os.replace(PRIV_PATH, PRIV_PATH + ".old")
    return "Đã gỡ kết nối KeyVault"


# ---------- lấy key ----------

def _brief(k):
    return {"id": k.get("id"), "name": k.get("name") or "", "size": k.get("size_bytes"),
            "fingerprint": k.get("fingerprint") or "", "description": k.get("description") or ""}


def keys():
    st = load_state()
    out = [_brief(k) for k in call("GET", "/api/v1/keys", api_key=_api_key(st), st=st) or []]
    st["keys"] = out
    save_state(st)
    return out


def _otp(otp):
    otp = str(otp or "").strip().replace(" ", "")
    if not otp.isdigit() or len(otp) != 6:
        raise KVError("mã OTP gồm 6 chữ số (Google Authenticator, mục KeyVault mang tên máy này)")
    return otp


def fetch(key_id, otp):
    """Lấy và giải mã một key. Trả bytes. Mỗi mã OTP chỉ dùng được một lần."""
    st = load_state()
    otp = _otp(otp)
    key_id = str(key_id or "").strip()
    if not key_id:
        raise KVError("chọn key cần lấy")
    api_key = _api_key(st)
    priv = load_private_key()
    created = call("POST", "/api/v1/exports", {"key_id": key_id}, api_key=api_key, st=st)
    aad = "kv-export-v1|%s|%s|%s" % (created["export_id"], key_id, created["recipient_id"])
    if created.get("aad") != aad or created.get("key_id") != key_id:
        raise KVError("phản hồi KeyVault không khớp yêu cầu — huỷ")
    unlocked = call("POST", "/api/v1/exports/%s/unlock" % created["export_id"], {"otp": otp}, api_key=api_key, st=st)
    if unlocked.get("aad") != aad:
        raise KVError("AAD không khớp — huỷ")
    return decrypt(priv, created, unlocked, aad.encode())


def fetch_text(key_id, otp):
    try:
        return fetch(key_id, otp).decode("utf-8").strip()
    except UnicodeDecodeError:
        raise KVError("key này không phải văn bản (không dùng làm token được)")


# ---------- dòng lệnh ----------

def main():
    ap = argparse.ArgumentParser(description="Lấy key từ KeyVault cho op-watch")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("request")
    s.add_argument("--name", required=True)
    s.add_argument("--contact", required=True)
    s.add_argument("--reason", required=True)
    s.add_argument("--organization", default="")
    s.add_argument("--requested-keys", default="")
    s.add_argument("--server")
    sub.add_parser("status")
    sub.add_parser("claim")
    s = sub.add_parser("activate")
    s.add_argument("--otp", required=True)
    s = sub.add_parser("import")
    s.add_argument("--api-key", required=True)
    s.add_argument("--priv", help="file private key có sẵn (PEM X25519)")
    s.add_argument("--server")
    sub.add_parser("pubkey")
    sub.add_parser("keys")
    s = sub.add_parser("fetch")
    s.add_argument("--key-id", required=True)
    s.add_argument("--otp")
    s.add_argument("--profile", help="ghi thành token của gói Claude này (không in ra màn hình)")
    s.add_argument("--out", help="ghi ra file (quyền 600)")
    s = sub.add_parser("test", help="lấy key + thử gọi Claude bằng token đó (không lưu, không in token)")
    s.add_argument("--key-id", required=True)
    s.add_argument("--otp")
    sub.add_parser("reset")
    a = ap.parse_args()
    try:
        if a.cmd == "request":
            print(request(a.name, a.contact, a.reason, a.organization, a.requested_keys, a.server))
        elif a.cmd == "status":
            sm = refresh_status() if load_state().get("request_id") else summary()
            print(json.dumps({k: v for k, v in sm.items() if k not in ("totp_secret", "otpauth_uri")},
                             ensure_ascii=False, indent=2))
        elif a.cmd == "claim":
            sm = claim()
            print("Thêm vào Google Authenticator → + → Nhập khóa thiết lập, secret:", sm["totp_secret"])
            print("Rồi chạy: python3 keyvault.py activate --otp <6 số>")
        elif a.cmd == "activate":
            print(activate(a.otp))
        elif a.cmd == "import":
            print(import_key(a.api_key, open(a.priv).read() if a.priv else None, a.server))
        elif a.cmd == "pubkey":
            print(ensure_key())
        elif a.cmd == "keys":
            for k in keys():
                print("%s  %-30s %6s B  fp=%s  %s" % (k["id"], k["name"], k["size"], k["fingerprint"], k["description"]))
        elif a.cmd == "fetch":
            otp = a.otp or input("Mã OTP: ")
            if a.profile:
                import op_dashboard
                print(op_dashboard.kv_fetch_profile(a.profile, a.key_id, otp))
            else:
                value = fetch(a.key_id, otp)
                if a.out:
                    _write_private(a.out, value)
                    print("Đã lưu %d byte vào %s" % (len(value), a.out))
                else:
                    sys.stdout.buffer.write(value)
        elif a.cmd == "test":
            import op_dashboard
            try:
                print(op_dashboard.kv_test_key(a.key_id, a.otp or input("Mã OTP: "))["message"])
            except ValueError as ex:
                sys.exit("Lỗi: %s" % ex)
        elif a.cmd == "reset":
            print(reset())
        else:
            ap.print_help()
    except KVError as ex:
        sys.exit("Lỗi: %s" % ex)


if __name__ == "__main__":
    main()
