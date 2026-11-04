#!/usr/bin/env python3
"""
tg_watch: userbot Telegram (Telethon) đăng nhập bằng tài khoản của mình, chỉ nghe chat riêng với các tester trong
TG_TESTERS, lưu tin nhắn + ảnh/file và gom thành yêu cầu chờ duyệt (xem tg_requests.py). Kèm các việc nền:
gom tin quá hạn → Chờ duyệt, theo dõi bản nháp AI → tạo task, gửi phản hồi cho tester (outbox), nhập tin cũ, đồng bộ
trạng thái work package.

  Đăng nhập: trang theo dõi → Telegram → ⚙ Cài đặt (số điện thoại → mã → mật khẩu 2 bước); service chưa đăng nhập thì
  chờ lệnh đăng nhập từ trang. Hoặc trên terminal:
  python3 tg_watch.py --login      # nhập số điện thoại, mã xác nhận, mật khẩu 2 bước (nếu có)
  python3 tg_watch.py --dialogs    # liệt kê chat riêng gần đây (lấy ID / @username cho TG_TESTERS)
  python3 tg_watch.py              # chạy liên tục (service op-telegram)

Cần: pip install --user telethon; TG_API_ID / TG_API_HASH lấy ở https://my.telegram.org → API development tools.
File phiên (TG_SESSION, quyền 600) = quyền vào tài khoản Telegram của mình: không chia sẻ, không commit.
"""
import argparse
import json
import asyncio
import logging
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402
import tg_requests as tr  # noqa: E402

try:
    from telethon import TelegramClient, errors, events
    from telethon.tl.types import User
except ImportError:  # pragma: no cover
    sys.exit("Thiếu Telethon: python3 -m pip install --user telethon")

log = logging.getLogger("tg_watch")
SESSION = envconf.path("TG_SESSION", "~/.config/op-watch/telegram")  # Telethon thêm đuôi .session
# Cấu hình còn lại đọc qua tr.settings() (telegram.json do trang theo dõi ghi > .env), đổi là áp ngay; API id / hash và
# danh sách tester chỉ đọc lúc khởi động (trang theo dõi tự khởi động lại op-telegram khi đổi các khoá này).


def name_of(ent):
    name = " ".join(x for x in (getattr(ent, "first_name", None), getattr(ent, "last_name", None)) if x)
    uname = getattr(ent, "username", None)
    if name and uname:
        return "%s (@%s)" % (name, uname)
    return name or ("@" + uname if uname else str(ent.id))


def client(wait=False):
    """wait: chưa đặt API id / hash thì chờ (service) tới khi đặt trên trang rồi chạy lại tiến trình."""
    st = tr.settings()
    if not st["api_id"] or not st["api_hash"]:
        msg = "chưa đặt API id / hash (trang theo dõi → Telegram → ⚙ Cài đặt)"
        if not wait:
            sys.exit("Chưa đặt API id / hash: trang theo dõi → Telegram → ⚙ Cài đặt, hoặc TG_API_ID / TG_API_HASH"
                     " trong .env (https://my.telegram.org → API development tools)")
        log.error(msg + " — chờ cấu hình")
        while not (tr.settings()["api_id"] and tr.settings()["api_hash"]):
            tr.set_meta("watcher", {"at": tr.now(), "pid": os.getpid(), "testers": [], "error": msg})
            time.sleep(5)
        restart_self()
    os.makedirs(os.path.dirname(SESSION), exist_ok=True)
    return TelegramClient(SESSION, st["api_id"], st["api_hash"])


def restart_self():
    """Chạy lại tiến trình (cùng PID, systemd không coi là lỗi) — sau khi đổi cấu hình / đăng xuất."""
    log.info("chạy lại tg_watch.py")
    logging.shutdown()
    os.execv(sys.executable, [sys.executable, "-u", os.path.abspath(__file__)] + sys.argv[1:])


def mask_phone(p):
    p = re.sub(r"[^\d+]", "", p or "")
    return p[:4] + "•" * max(0, len(p) - 6) + p[-2:] if len(p) > 6 else p


def secure_session():
    p = SESSION + ".session"
    if os.path.exists(p):
        os.chmod(p, 0o600)


class Watcher:
    def __init__(self, c):
        self.c = c
        self.testers = {}  # chat_id (= user id) → tên
        self.me = ""

    async def download(self, msg):
        if not msg.media:
            return None, None
        kind = "ảnh" if msg.photo else "video" if msg.video else "ghi âm" if msg.voice else "tệp" if msg.document else None
        if kind is None:
            return None, "media khác"
        size = getattr(msg.file, "size", 0) or 0
        if size > tr.settings()["max_media_mb"] * 1024 * 1024:
            return None, "%s (quá lớn, không tải)" % kind
        folder = os.path.join(tr.media_dir(), str(msg.chat_id))
        os.makedirs(folder, exist_ok=True)
        fname = "%s_%s" % (msg.id, msg.file.name) if getattr(msg.file, "name", None) else "%s%s" % (
            msg.id, getattr(msg.file, "ext", "") or "")
        path = await msg.download_media(file=os.path.join(folder, fname.replace("/", "_")))
        return ("%s/%s" % (msg.chat_id, os.path.basename(path)) if path else None), kind

    async def store(self, msg, live):
        if msg.chat_id not in self.testers:
            return
        if live and not msg.out and (msg.message or "").strip():
            # Trả lời câu hỏi xin ý kiến (ticket UNREVIEWED) → comment lên ticket, không gom thành yêu cầu.
            try:
                handled = await self.run_blocking(tr.handle_ask_reply, msg.chat_id, msg.reply_to_msg_id, msg.message,
                                                  self.testers[msg.chat_id])
            except Exception:  # noqa: BLE001
                log.exception("lỗi ghi trả lời xin ý kiến")
                handled = False
            if handled:
                tr.ingest(msg.chat_id, msg.id, True, self.testers[msg.chat_id], msg.message or "", msg.date, None, None,
                          msg.reply_to_msg_id, False)
                log.info("tester %s trả lời câu hỏi xin ý kiến", self.testers[msg.chat_id])
                return
        media, kind = await self.download(msg)
        rid = tr.ingest(msg.chat_id, msg.id, not msg.out,
                        self.testers[msg.chat_id] if not msg.out else (self.me or "Tôi"),
                        msg.message or "", msg.date, media, kind, msg.reply_to_msg_id, live)
        if rid and live:
            log.info("tin mới của %s → yêu cầu #%s", self.testers[msg.chat_id], rid)

    async def on_new(self, event):
        try:
            await self.store(event.message, True)
        except Exception:  # noqa: BLE001
            log.exception("lỗi xử lý tin nhắn")

    async def on_edit(self, event):
        tr.edit_message(event.chat_id, event.message.id, event.message.message or "")

    def heartbeat(self, error=None):
        tr.set_meta("watcher", {"me": self.me, "testers": [{"id": k, "name": v} for k, v in self.testers.items()],
                                "at": tr.now(), "pid": os.getpid(), "error": error})

    async def run_blocking(self, fn, *args):
        return await asyncio.get_event_loop().run_in_executor(None, fn, *args)

    async def background(self):
        tick = 10 ** 9  # đồng bộ ngay lượt đầu
        while True:
            try:
                tr.finalize_due()
                for a in tr.pending_asks():
                    try:
                        sent = await self.c.send_message(a["chat_id"], tr.ask_text(a))
                        tr.mark_ask_sent(a["id"], sent.id)
                        log.info("đã gửi câu hỏi xin ý kiến %s", a["display_id"])
                    except Exception as ex:  # noqa: BLE001
                        tr.mark_ask_sent(a["id"], error=str(ex))
                for o in tr.pending_outbox():
                    try:
                        await self.c.send_message(o["chat_id"], o["text"], reply_to=o["reply_to"])
                        tr.mark_outbox(o["id"])
                    except Exception as ex:  # noqa: BLE001
                        tr.mark_outbox(o["id"], str(ex)[:300])
                for cmd in tr.pending_commands():
                    args = json.loads(cmd["args"] or "{}")
                    if cmd["kind"] == "logout":
                        tr.finish_command(cmd["id"], "đăng xuất")
                        log.info("đăng xuất Telegram theo yêu cầu từ trang theo dõi")
                        await self.c.log_out()
                        tr.set_meta("login", {"step": "phone", "at": tr.now(), "message": "đã đăng xuất"})
                        await self.c.disconnect()
                        restart_self()
                    if cmd["kind"].startswith("login_"):
                        tr.finish_command(cmd["id"], "đã đăng nhập sẵn")
                        continue
                    if cmd["kind"] == "import" and args.get("chat_id") in self.testers:
                        n = 0
                        async for m in self.c.iter_messages(args["chat_id"], limit=args.get("limit", 200)):
                            await self.store(m, False)
                            n += 1
                        tr.finish_command(cmd["id"], "đã đọc %d tin" % n)
                    else:
                        tr.finish_command(cmd["id"], "bỏ qua")
                tr.triage_due()  # AI lọc việc (luồng nền riêng, không chặn vòng này)
                done = await self.run_blocking(tr.advance_drafts)
                for line in done:
                    log.info("tạo task: %s", line)
                if tick >= tr.settings()["sync_minutes"] * 60:
                    tick = 0
                    await self.run_blocking(tr.sync_wp_status)
                self.heartbeat()
            except Exception as ex:  # noqa: BLE001
                log.exception("lỗi việc nền")
                self.heartbeat(str(ex)[:300])
            await asyncio.sleep(10)
            tick += 10

    def login_state(self, step, **extra):
        tr.set_meta("login", dict({"step": step, "at": tr.now()}, **extra))

    async def login_flow(self):
        """Chưa đăng nhập: chờ lệnh từ trang theo dõi (login_phone → login_code → login_password nếu có 2 bước).
        Mã / mật khẩu chỉ đi qua hàng lệnh và bị xoá ngay khi đọc (tr.pending_commands / finish_command)."""
        phone = code_hash = None
        step = "phone"
        self.login_state(step)
        while True:
            self.heartbeat("chưa đăng nhập Telegram — đăng nhập ở ⚙ Cài đặt trên trang theo dõi")
            for cmd in tr.pending_commands():
                args = json.loads(cmd["args"] or "{}")
                tr.finish_command(cmd["id"], cmd["kind"])  # xoá args (mã / mật khẩu) ngay
                kind = cmd["kind"]
                try:
                    if kind == "login_cancel":
                        phone = code_hash = None
                        step = "phone"
                        self.login_state(step, message="đã huỷ")
                    elif kind == "login_phone":
                        phone = re.sub(r"[^\d+]", "", args.get("phone") or "")
                        self.login_state("sending", phone=mask_phone(phone))
                        sent = await self.c.send_code_request(phone)
                        code_hash = sent.phone_code_hash
                        via = type(sent.type).__name__.replace("SentCodeType", "")
                        step = "code"
                        self.login_state(step, phone=mask_phone(phone),
                                         message="đã gửi mã qua %s" % ("ứng dụng Telegram" if via == "App" else via))
                    elif kind == "login_code" and step == "code":
                        self.login_state("checking", phone=mask_phone(phone))
                        try:
                            await self.c.sign_in(phone, str(args.get("code") or "").strip(), phone_code_hash=code_hash)
                        except errors.SessionPasswordNeededError:
                            step = "password"
                            self.login_state(step, phone=mask_phone(phone), message="tài khoản bật xác minh 2 bước")
                    elif kind == "login_password" and step == "password":
                        self.login_state("checking", phone=mask_phone(phone))
                        await self.c.sign_in(password=args.get("password") or "")
                    else:
                        self.login_state(step, phone=mask_phone(phone), error="bước không hợp lệ (%s) — làm lại" % kind)
                except errors.PhoneCodeInvalidError:
                    self.login_state(step, phone=mask_phone(phone), error="mã không đúng")
                except errors.PhoneCodeExpiredError:
                    step = "phone"
                    self.login_state(step, error="mã đã hết hạn — gửi lại mã")
                except errors.PasswordHashInvalidError:
                    self.login_state(step, phone=mask_phone(phone), error="mật khẩu 2 bước không đúng")
                except errors.PhoneNumberInvalidError:
                    step = "phone"
                    self.login_state(step, error="số điện thoại không hợp lệ (dạng +84…)")
                except errors.FloodWaitError as ex:
                    self.login_state(step, phone=mask_phone(phone),
                                     error="Telegram yêu cầu chờ %d giây rồi thử lại" % ex.seconds)
                except Exception as ex:  # noqa: BLE001
                    log.exception("đăng nhập lỗi")
                    self.login_state(step if step != "sending" else "phone", phone=mask_phone(phone),
                                     error=str(ex)[:300])
                if await self.c.is_user_authorized():
                    me = await self.c.get_me()
                    self.login_state("done", me=name_of(me), message="đã đăng nhập")
                    log.info("đăng nhập Telegram từ trang theo dõi: %s", name_of(me))
                    return
            await asyncio.sleep(2)

    async def start(self):
        await self.c.connect()
        if not await self.c.is_user_authorized():
            await self.login_flow()
        secure_session()
        self.me = name_of(await self.c.get_me())
        self.login_state("done", me=self.me)
        for ref in tr.settings()["testers"]:
            try:
                ent = await self.c.get_entity(int(ref) if ref.lstrip("-").isdigit() else ref)
                self.testers[ent.id] = name_of(ent)
            except Exception as ex:  # noqa: BLE001
                log.error("không tìm thấy tester %s: %s", ref, ex)
        if not self.testers:
            self.heartbeat("chưa có tester nào (⚙ Cài đặt → Tester) hoặc không tìm thấy tester")
            log.error("danh sách tester trống hoặc không tìm thấy tester nào — chỉ chạy việc nền")
        else:
            chats = list(self.testers)
            self.c.add_event_handler(self.on_new, events.NewMessage(chats=chats))
            self.c.add_event_handler(self.on_edit, events.MessageEdited(chats=chats))
            log.info("đăng nhập %s, nghe chat với: %s", self.me, ", ".join(self.testers.values()))
        self.heartbeat()
        await self.background()


async def login():
    c = client()
    await c.start()
    secure_session()
    me = await c.get_me()
    print("\nĐã đăng nhập: %s (id=%s). File phiên: %s.session (quyền 600)" % (name_of(me), me.id, SESSION))
    await dialogs(c)
    await c.disconnect()


async def dialogs(c=None):
    own = c is None
    if own:
        c = client()
        await c.connect()
        if not await c.is_user_authorized():
            sys.exit("Chưa đăng nhập: python3 tg_watch.py --login")
    print("\nChat riêng gần đây (điền ID hoặc @username vào ⚙ Cài đặt → Tester trên trang theo dõi, hoặc TG_TESTERS):")
    async for d in c.iter_dialogs(limit=80):
        if isinstance(d.entity, User) and not d.entity.bot and not d.entity.is_self:
            print("  %14s  %s" % (d.entity.id, name_of(d.entity)))
    if own:
        await c.disconnect()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--login", action="store_true", help="đăng nhập Telegram (tương tác)")
    ap.add_argument("--dialogs", action="store_true", help="liệt kê chat riêng gần đây")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    loop = asyncio.get_event_loop()
    if args.login:
        return loop.run_until_complete(login())
    if args.dialogs:
        return loop.run_until_complete(dialogs())
    w = Watcher(client(wait=True))
    try:
        loop.run_until_complete(w.start())
    finally:
        loop.run_until_complete(w.c.disconnect())


if __name__ == "__main__":
    main()
