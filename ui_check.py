#!/usr/bin/env python3
"""
ui_check: mở giao diện SOAR trên server test bằng trình duyệt thật (Playwright + Google Chrome của máy), đăng nhập bằng
tài khoản ghi trong ticket, chạy các bước trong một file JSON, chụp màn hình để Claude xem lại (Read file PNG).

    ui_check.py <run_dir>/ui/steps.json                    # tài khoản lấy từ <run_dir>/ticket/ticket.md
    ui_check.py steps.json --ticket path/ticket.md
    ui_check.py steps.json --user admint --password 'x' --otp 123456

File bước (JSON, mảng; mỗi phần tử một bước):
    {"goto": "/alerts"}                         đường dẫn trên FE (hoặc URL đầy đủ cùng máy chủ FE)
    {"click": "text=Lưu"}                       selector Playwright (text=…, role=button[name="…"], css…)
    {"fill": ["input[name=q]", "abc"]}          nhập chữ
    {"press": "Enter"}                          phím
    {"select": ["select#x", "value"]}           chọn option
    {"hover": "css…"}
    {"wait": 1500} | {"wait": "text=Đã lưu"}    chờ (ms, tối đa 30000) hoặc chờ selector hiện
    {"screenshot": "ten"}                       chụp cả trang → <out>/ten.png ({"screenshot": "ten", "selector": "css…"}: chụp 1 phần tử)
    {"screenshot": "ten", "mark": ["css…", …], "note": "Vùng lỗi"}   khoanh đỏ các vùng cần chú ý (kèm nhãn) rồi chụp
    {"text": "css…"}                            in chữ của phần tử (tối đa 20 phần tử)
    {"style": ["css…", ["color", "background-color", "font-size"]]}   in CSS tính toán
    {"theme": "dark"|"light"}                    đổi theme qua localStorage rồi tải lại (nếu FE hỗ trợ)
    {"lang": "vi"|"en"}
    {"frame": "iframe"}                         các bước sau thao tác TRONG iframe (vd ứng dụng FLOW nhúng ở trang Kịch bản);
                                                {"frame": ""} quay lại trang chính
Dạng object {"login": false, "steps": [...]}: không đăng nhập (xem màn hình đăng nhập / trang công khai).

Giới hạn an toàn: chỉ mở trang trên máy chủ FE của server test (DEPLOY_FE_URL hoặc Link trong ticket nếu cùng máy chủ);
không bấm phần tử có chữ xoá / delete / đăng xuất / logout / reset; tối đa 60 bước, mỗi lần chạy 5 phút.
Lưu ý: đăng nhập mới có thể đăng xuất phiên trình duyệt đang mở của cùng tài khoản; phiên đăng nhập được lưu lại
($OP_WATCH_CACHE_DIR/ui-<tài khoản>.json) để các lần sau không phải đăng nhập lại.
"""
import argparse
import fcntl
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402

DENY_CLICK = re.compile(r"xo[áa]|x[óo]a|delete|remove|đăng xuất|dang xuat|log ?out|sign ?out|reset|huỷ kích hoạt|vô hiệu",
                        re.I)
MAX_STEPS = 60
DEADLINE = 300


def ticket_account(path):
    """(link, tài khoản, mật khẩu, otp) ghi trong phần mô tả ticket.md (dạng `Tài khoản: **x**`)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return "", "", "", ""
    desc = text.split("## Lịch sử")[0]

    def field(*labels):
        for src in (desc, text):
            for lb in labels:
                m = re.search(r"%s\s*[:：]\s*([^\n]*)" % lb, src, re.I)
                if not m:
                    continue
                val = m.group(1).strip()
                # Còn để trống trong ticket: [Cần bổ sung], (chưa có), TBD, … → coi như không có
                if re.match(r"^\W*(cần bổ sung|can bo sung|chưa có|chua co|tbd|n/?a|\.\.\.|…)", val, re.I) \
                        or re.match(r"^\[(?!https?:)[^\]]*\]\s*$", val):
                    continue
                link = re.search(r"https?://[^\s)\]]+", val)
                if link and lb.lower().startswith("link"):
                    return link.group(0)
                tok = re.search(r"[^\s*`\[\]]+", val)
                if tok:
                    return tok.group(0).strip("'\".,;")
        return ""

    return (field(r"Link"), field(r"Tài khoản", r"Tai khoan", r"Username", r"User"),
            field(r"Mật khẩu", r"Mat khau", r"Password"), field(r"OTP"))


def fe_base(link=""):
    cfg = envconf.deploy_config()
    fe = (cfg.get("fe_url") or "").rstrip("/")
    if not fe:
        sys.exit("Thiếu DEPLOY_FE_URL (.env) — không biết giao diện server test ở đâu")
    if link and urlparse(link).netloc and urlparse(link).netloc != urlparse(fe).netloc:
        print("Lưu ý: link trong ticket (%s) khác server test (%s) — dùng server test" % (link, fe))
    return fe


def ui_auth():
    """Trang / ô đăng nhập trên giao diện của dự án (tab Dự án → Đăng nhập; trống = mặc định kiểu SOAR, hợp
    với đa số app: ô name=username / email, ô mật khẩu, nút submit)."""
    a = (envconf.deploy_config().get("auth") or {})
    return {"path": a.get("ui_login_path") or "/login",
            "user": a.get("ui_user") or 'input[name="username"], input[name="email"], input[type="email"]',
            "password": a.get("ui_password") or 'input[type="password"]',
            "submit": a.get("ui_submit") or 'button[type="submit"]',
            "none": a.get("kind") == "none"}


def on_login(url):
    return ui_auth()["path"].rstrip("/") in url


def state_path(user):
    os.makedirs(envconf.CACHE_DIR, exist_ok=True)
    proj = os.environ.get("OP_PROJECT_KEY") or ""
    scope = "" if not proj or (envconf.deploy_config().get("auth") or {}).get("kind", "soar") == "soar" else proj + "-"
    return os.path.join(envconf.CACHE_DIR, "ui-%s%s.json" % (scope, re.sub(r"[^\w.-]", "_", user)))


def login(page, fe, user, password, otp):
    ua = ui_auth()
    page.goto(fe + ua["path"], wait_until="networkidle", timeout=60000)
    page.locator(ua["user"]).first.fill(user)
    page.locator(ua["password"]).first.fill(password)
    page.locator(ua["submit"]).first.click()
    deadline = time.time() + 30
    while time.time() < deadline:
        page.wait_for_timeout(500)
        if not on_login(page.url):
            return "đăng nhập %s xong" % user
        otp_box = page.query_selector('input[autocomplete="one-time-code"], input[data-input-otp]')
        if otp_box and otp_box.is_visible():
            if not otp:
                raise RuntimeError("màn hình hỏi OTP nhưng ticket không ghi OTP (--otp)")
            otp_box.click()
            page.keyboard.type(otp, delay=60)
            page.wait_for_timeout(2500)
            if not on_login(page.url):
                return "đăng nhập %s (OTP) xong" % user
    err = page.query_selector("[role=alert], .text-destructive, [data-sonner-toast], li[role=status]")
    raise RuntimeError("đăng nhập %s không thành công%s" % (user, (": " + err.inner_text()[:200]) if err else ""))


def mark(page, selectors, scope=None, note=""):
    """Khoanh đỏ (viền + nhãn) các phần tử khớp selector Playwright; trả số vùng tìm thấy."""
    found = 0
    for k, sel in enumerate(selectors):
        loc = (scope or page).locator(sel)
        for j in range(min(loc.count(), 5)):
            box = loc.nth(j).bounding_box()
            if not box:
                continue
            found += 1
            page.evaluate("""([b, label]) => {
                const d = document.createElement('div');
                d.setAttribute('data-ui-check-mark', '1');
                Object.assign(d.style, {position: 'absolute', left: (b.x + scrollX - 4) + 'px', top: (b.y + scrollY - 4) + 'px',
                  width: (b.width + 8) + 'px', height: (b.height + 8) + 'px', border: '3px solid #ff1f3d',
                  borderRadius: '6px', boxShadow: '0 0 0 2px rgba(255,255,255,.7)', zIndex: 2147483647,
                  pointerEvents: 'none', boxSizing: 'border-box'});
                if (label) {
                  const t = document.createElement('span');
                  t.textContent = label;
                  Object.assign(t.style, {position: 'absolute', left: '-3px', bottom: '100%', marginBottom: '3px',
                    background: '#ff1f3d', color: '#fff', font: '600 12px/16px sans-serif', padding: '2px 6px',
                    borderRadius: '4px', whiteSpace: 'nowrap'});
                  d.appendChild(t);
                }
                document.body.appendChild(d);
            }""", [box, note if (k == 0 and j == 0) else ""])
    return found


def same_host(url, fe):
    return urlparse(url).netloc == urlparse(fe).netloc


def run_steps(page, fe, steps, out, relogin=None):
    results = []
    t0 = time.time()
    scope = page  # trang chính hoặc iframe (bước "frame")
    for i, st in enumerate(steps[:MAX_STEPS], 1):
        if time.time() - t0 > DEADLINE:
            results.append("%d. dừng: quá %d giây" % (i, DEADLINE))
            break
        if not isinstance(st, dict) or len(st) < 1:
            results.append("%d. bỏ qua bước không hợp lệ: %r" % (i, st))
            continue
        kind = next(iter(st))
        arg = st[kind]
        try:
            if kind == "goto":
                url = urljoin(fe + "/", str(arg).lstrip("/")) if not str(arg).startswith("http") else str(arg)
                if not same_host(url, fe):
                    raise ValueError("chỉ mở trang trên %s" % fe)
                page.goto(url, wait_until="networkidle", timeout=60000)
                if relogin and on_login(page.url) and not on_login(url):
                    # phiên đã lưu hết hạn (server đổi bản / đăng nhập nơi khác): đăng nhập lại rồi mở lại trang
                    relogin()
                    page.goto(url, wait_until="networkidle", timeout=60000)
                msg = "mở %s → %s" % (url, page.url)
            elif kind == "click":
                el = scope.locator(str(arg)).first
                el.wait_for(state="visible", timeout=10000)
                label = (el.inner_text(timeout=2000) or "") + " " + (el.get_attribute("aria-label") or "") + " " + \
                    (el.get_attribute("title") or "")
                if DENY_CLICK.search(label) or DENY_CLICK.search(str(arg)):
                    raise ValueError("không bấm nút có thể xoá / đăng xuất (%s)" % label.strip()[:80])
                el.click(timeout=10000)
                page.wait_for_timeout(600)
                msg = "bấm %s" % arg
            elif kind == "fill":
                sel, val = arg
                scope.locator(sel).first.fill(str(val), timeout=10000)
                msg = "nhập %s" % sel
            elif kind == "press":
                page.keyboard.press(str(arg))
                page.wait_for_timeout(400)
                msg = "phím %s" % arg
            elif kind == "select":
                sel, val = arg
                scope.locator(sel).first.select_option(str(val), timeout=10000)
                msg = "chọn %s = %s" % (sel, val)
            elif kind == "hover":
                scope.locator(str(arg)).first.hover(timeout=10000)
                msg = "rê chuột %s" % arg
            elif kind == "wait":
                if isinstance(arg, (int, float)):
                    page.wait_for_timeout(min(int(arg), 30000))
                    msg = "chờ %d ms" % min(int(arg), 30000)
                else:
                    scope.locator(str(arg)).first.wait_for(state="visible", timeout=30000)
                    msg = "đã thấy %s" % arg
            elif kind == "screenshot":
                name = re.sub(r"[^\w.-]", "_", str(arg))[:60] or "shot"
                path = os.path.join(out, "%02d-%s.png" % (i, name))
                marks = st.get("mark") or []
                marks = [marks] if isinstance(marks, str) else list(marks)[:10]
                found = mark(page, marks, scope=scope, note= str(st.get("note") or "")) if marks else 0
                try:
                    if st.get("selector"):
                        scope.locator(st["selector"]).first.screenshot(path=path, timeout=10000)
                    else:
                        page.screenshot(path=path, full_page=bool(st.get("full", False)))
                finally:
                    if marks:
                        page.evaluate("() => document.querySelectorAll('[data-ui-check-mark]').forEach(e => e.remove())")
                msg = "ảnh %s%s" % (path, (" · khoanh %d/%d vùng" % (found, len(marks))) if marks else "")
            elif kind == "text":
                els = scope.locator(str(arg))
                n = min(els.count(), 20)
                texts = [els.nth(k).inner_text(timeout=3000).strip().replace("\n", " ⏎ ")[:300] for k in range(n)]
                msg = "chữ %s (%d): %s" % (arg, els.count(), json.dumps(texts, ensure_ascii=False))
            elif kind == "style":
                sel, props = arg
                vals = scope.locator(sel).first.evaluate(
                    "(el, ps) => { const c = getComputedStyle(el); return Object.fromEntries(ps.map(p => [p, c.getPropertyValue(p)])); }",
                    list(props)[:20])
                msg = "CSS %s: %s" % (sel, json.dumps(vals, ensure_ascii=False))
            elif kind == "frame":
                if arg:
                    page.locator(str(arg)).first.wait_for(state="attached", timeout=30000)
                    scope = page.frame_locator(str(arg)).first
                    msg = "vào iframe %s" % arg
                else:
                    scope = page
                    msg = "về trang chính"
            elif kind in ("theme", "lang"):
                key = {"theme": ["theme", "vite-ui-theme"], "lang": ["i18nextLng", "language"]}[kind]
                page.evaluate("([ks, v]) => ks.forEach(k => localStorage.setItem(k, v))", [key, str(arg)])
                page.reload(wait_until="networkidle", timeout=60000)
                msg = "%s = %s (tải lại)" % (kind, arg)
            else:
                raise ValueError("không hiểu bước '%s'" % kind)
            results.append("%d. OK %s" % (i, msg))
        except Exception as ex:  # noqa: BLE001 — ghi lỗi từng bước, chụp ảnh lúc lỗi rồi chạy tiếp
            path = os.path.join(out, "%02d-loi.png" % i)
            try:
                page.screenshot(path=path)
            except Exception:  # noqa: BLE001
                path = "(không chụp được)"
            results.append("%d. LỖI %s %r: %s · ảnh lúc lỗi %s" % (i, kind, arg, str(ex).splitlines()[0][:300], path))
    if len(steps) > MAX_STEPS:
        results.append("bỏ %d bước sau bước %d (tối đa %d)" % (len(steps) - MAX_STEPS, MAX_STEPS, MAX_STEPS))
    return results


def main():
    ap = argparse.ArgumentParser(description="Chạy các bước trên giao diện server test, chụp màn hình.")
    ap.add_argument("steps", help="file JSON các bước")
    ap.add_argument("--ticket", help="ticket.md để lấy tài khoản (mặc định <thư mục lượt chạy>/ticket/ticket.md)")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--otp")
    ap.add_argument("--as", dest="account",
                    help="tài khoản trong deploy.json / QA_AUTO_* / qa_ticket_accounts (mật khẩu soar_api đã nhớ)")
    ap.add_argument("--out", help="thư mục ảnh (mặc định cạnh file bước: <tên>-shots/)")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    args = ap.parse_args()

    steps_path = os.path.abspath(args.steps)
    with open(steps_path, encoding="utf-8") as f:
        steps = json.load(f)
    do_login = True
    if isinstance(steps, dict):
        do_login = steps.get("login", True) is not False
    if ui_auth()["none"]:  # app không cần đăng nhập (tab Dự án → Đăng nhập: không đăng nhập)
        do_login = False
        steps = steps.get("steps") or []
    ticket = args.ticket
    if not ticket:
        d = os.path.dirname(steps_path)
        for cand in (os.path.join(d, "ticket", "ticket.md"), os.path.join(os.path.dirname(d), "ticket", "ticket.md")):
            if os.path.exists(cand):
                ticket = cand
                break
    link, t_user, t_pass, t_otp = ticket_account(ticket) if ticket else ("", "", "", "")
    user, password, otp = args.user or t_user, args.password or t_pass, args.otp or t_otp
    source = "tham số" if args.user else "từ ticket"
    if do_login and (args.account or not (user and password)):
        # --as, hoặc ticket không ghi tài khoản → tài khoản QA của soar_api (deploy.json, mặc định default_account)
        import soar_api
        dcfg = envconf.deploy_config()
        user, password = soar_api.credentials(dcfg, args.account, args.password if args.account else None)
        otp = args.otp or str(dcfg.get("otp") or dcfg.get("app_otp") or t_otp or "")
        source = "tài khoản QA (deploy.json)"
    if do_login and (not user or not password):
        sys.exit("Không có tài khoản: ticket không ghi `Tài khoản:` / `Mật khẩu:` (truyền --user / --password)")
    fe = fe_base(link)
    out = args.out or os.path.splitext(steps_path)[0] + "-shots"
    os.makedirs(out, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Thiếu Playwright: python3 -m pip install --user playwright")
    print("Server test: %s · %s" % (fe, ("tài khoản: %s (%s)" % (user, source))
                                    if do_login else "không đăng nhập"))
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)

        def session(user, password, otp):
            """(context, page, storage_state path, lock) đã đăng nhập (hoặc chưa, khi do_login=False)."""
            lock = None
            if do_login:  # mỗi tài khoản một phiên: lượt khác cùng tài khoản chờ (đăng nhập lại sẽ đá phiên đang test)
                lock = open(state_path(user) + ".lock", "w")
                fcntl.flock(lock, fcntl.LOCK_EX)
            sp = state_path(user) if do_login else ""
            ctx = browser.new_context(ignore_https_errors=True, viewport={"width": args.width, "height": args.height},
                                      storage_state=sp if sp and os.path.exists(sp) else None, locale="vi-VN")
            page = ctx.new_page()

            def guard(route):
                req = route.request
                if req.is_navigation_request() and req.frame == page.main_frame and not same_host(req.url, fe):
                    return route.abort()
                return route.continue_()

            page.route("**/*", guard)
            page.goto(fe + "/", wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(1500)  # app SPA kiểm tra token rồi mới chuyển về trang đăng nhập
            try:
                if do_login and on_login(page.url):
                    print(login(page, fe, user, password, otp))
                    ctx.storage_state(path=sp)
                    os.chmod(sp, 0o600)
                elif do_login:
                    print("dùng lại phiên đăng nhập đã lưu của %s" % user)
            except Exception:
                ctx.close()
                if lock:
                    lock.close()
                raise
            return ctx, page, sp, lock

        cred = [user, password, otp]
        try:
            ctx, page, sp, lock = session(user, password, otp)
        except RuntimeError as ex:
            if source != "từ ticket":
                raise
            # Tài khoản trong ticket không đăng nhập được (sai / thiếu mật khẩu, OTP…) → tài khoản QA của deploy.json
            import soar_api
            dcfg = envconf.deploy_config()
            qa_user, qa_pass = soar_api.credentials(dcfg, None)
            print("%s → thử bằng tài khoản QA %s" % (ex, qa_user))
            cred = [qa_user, qa_pass, str(dcfg.get("app_otp") or otp or "")]
            ctx, page, sp, lock = session(*cred)

        def relogin():
            print("phiên đã lưu hết hạn → " + login(page, fe, *cred))
            if sp:
                ctx.storage_state(path=sp)
                os.chmod(sp, 0o600)
        for line in run_steps(page, fe, steps, out, relogin if do_login else None):
            print(line)
        if sp and not on_login(page.url):
            ctx.storage_state(path=sp)
        browser.close()
    print("Ảnh: %s (xem bằng Read)" % out)


if __name__ == "__main__":
    main()
