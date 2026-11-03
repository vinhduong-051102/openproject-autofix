#!/usr/bin/env python3
"""
task_create: tạo task OpenProject từ trang theo dõi.

  1. draft(): người dùng nhập mô tả ngắn + file liên quan (ảnh chụp, log…). Claude (headless, chỉ đọc file đính kèm và
     mã FE/BE để lấy đúng tên màn hình / đường dẫn / API) soạn tiêu đề + mô tả theo mẫu ticket của tester → draft.json.
  2. create(): người dùng xem / sửa bản nháp rồi tạo work package trạng thái New, upload file đính kèm, thay ảnh
     `attachment:<tên file>` trong mô tả bằng link file đã upload.

Bản nháp lưu ở <autofix root>/task-drafts/<id>/ (files/, prompt.md, draft.json, state.json, claude.json).
"""
import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402
import op_autofix as af  # noqa: E402
import op_watch  # noqa: E402

MAX_FILES = 10
MAX_BYTES = 20 * 1024 * 1024
TYPE_BUG = 7
STATUS_NEW = 1

PROMPT = """Bạn là tester dự án {project_name}. Viết một ticket OpenProject (tiếng Việt) từ mô tả của người dùng và các file
đính kèm, ĐÚNG phong cách các ticket mẫu của tester bên dưới. Chạy nền, không hỏi lại.

## Yêu cầu của người dùng
{prompt}

## File đính kèm (đọc hết, ảnh thì xem bằng Read)
{files}

## Nguồn tham khảo (chỉ đọc)
- Mã FE: {fe_repo} (route / menu / nhãn i18n trong src/) — dùng để ghi ĐÚNG đường dẫn màn hình, tên menu, tên tab, nhãn
- Mã BE: {be_repo} — dùng để ghi đúng API (method + path) nếu ticket liên quan API
- Môi trường test: web {fe_url}, API `$BASE` = `{api_url}`
Chỉ tra cứu đủ để ghi chính xác; không cần đọc lan man.

## Ticket mẫu của tester (bắt chước bố cục, cách đặt tiêu đề, giọng văn)
{examples}

## Quy tắc
- Tiêu đề: `TCVN - <Phân hệ> - <Màn hình / chức năng> - Lỗi: <mô tả lỗi ngắn gọn, cụ thể>` (bug); việc không phải lỗi
  (yêu cầu mới / cải tiến) thì `<Phân hệ> - <Màn hình> - <nội dung>` và type "Task".
- Mô tả (markdown) theo thứ tự: Link (đường dẫn web cụ thể), Tài khoản (nếu biết; mật khẩu CHỈ ghi khi người dùng đưa,
  không thì `[Cần bổ sung]`), API (nếu liên quan), **ĐIỀU KIỆN BAN ĐẦU:**, **CÁC BƯỚC THỰC HIỆN:** (đánh số, cụ thể từng
  thao tác), **KẾT QUẢ MONG ĐỢI:**, **KẾT QUẢ THỰC TẾ:**, **MINH CHỨNG:** (nếu có file).
- Ảnh đính kèm chèn vào mục MINH CHỨNG bằng `![<tên file>](attachment:<tên file>)` (đúng tên file trong danh sách).
  File không phải ảnh thì ghi "Xem file đính kèm `<tên file>`".
- Không bịa dữ liệu không có trong yêu cầu / file / mã nguồn; chỗ chưa biết ghi `[Cần bổ sung]`.
- Kết thúc bằng dòng `***Anh/Em xem và xử lý giúp nhé.***` như mẫu.

## Kết quả (bắt buộc)
Ghi file {out} (JSON, UTF-8) đúng dạng:
{{"subject": "...", "description": "...(markdown)...", "type": "Bug" | "Task", "priority": "High" | "Medium" | "Low" | "Immediate",
  "questions": ["điểm còn thiếu người dùng nên bổ sung", ...]}}
"""


def load():
    cfg = op_watch.load_config(argparse.Namespace(config=None, interval=None, on_change=None, no_notify=True))
    acfg = dict(af.AUTOFIX_DEFAULTS)
    acfg.update(cfg.get("autofix") or {})
    return cfg, acfg, envconf.deploy_config()


def drafts_root(acfg):
    path = os.path.join(os.path.expanduser(acfg["root"]), "task-drafts")
    os.makedirs(path, exist_ok=True)
    return path


def safe_name(name, used):
    base = re.sub(r"[^\w.\-]+", "_", os.path.basename(name or "file"), flags=re.UNICODE).strip("._") or "file"
    base = base[:120]
    out, i = base, 1
    while out in used:
        stem, dot, ext = base.rpartition(".")
        out = "%s_%d.%s" % (stem, i, ext) if dot else "%s_%d" % (base, i)
        i += 1
    used.add(out)
    return out


def examples(api, cfg, qa_user_id, n=2):
    """Mô tả vài ticket gần nhất do tester (người QA) tạo — làm mẫu (lưu đệm 1 ngày)."""
    _, acfg, _ = load()
    cache = os.path.join(os.path.expanduser(acfg["root"]), ".task-examples.json")
    try:
        if time.time() - os.path.getmtime(cache) < 86400:
            with open(cache, encoding="utf-8") as f:
                return json.load(f)["text"]
    except (OSError, ValueError, KeyError):
        pass
    filters = [{"author": {"operator": "=", "values": [str(qa_user_id)]}}, {"status": {"operator": "*", "values": []}}]
    data = api.get("/api/v3/projects/%s/work_packages" % cfg["project"], {
        "filters": json.dumps(filters), "sortBy": json.dumps([["id", "desc"]]), "pageSize": 12})
    picked = []
    for w in data.get("_embedded", {}).get("elements", []):
        raw = ((w.get("description") or {}).get("raw") or "").strip()
        if 800 < len(raw) < 6000 and "KẾT QUẢ MONG ĐỢI" in raw:
            raw = re.sub(r"\(/api/v3/attachments/\d+/content\)", "(attachment:anh.png)", raw)
            picked.append("### Tiêu đề: %s\n\n%s" % (w.get("subject", ""), raw[:3500]))
        if len(picked) >= n:
            break
    text = "\n\n---\n\n".join(picked) or "(chưa lấy được ticket mẫu)"
    with open(cache, "w", encoding="utf-8") as f:
        json.dump({"text": text}, f, ensure_ascii=False)
    return text


def set_state(d, **fields):
    path = os.path.join(d, "state.json")
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    data.update(fields, updated_at=datetime.now().isoformat(timespec="seconds"))
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)
    return data


def start_draft(prompt, files, opts=None):
    """Lưu yêu cầu + file, chạy Claude soạn nền. Trả id bản nháp (theo dõi bằng get_draft)."""
    prompt = (prompt or "").strip()
    if not prompt and not files:
        raise ValueError("cần mô tả task hoặc file đính kèm")
    if len(files) > MAX_FILES:
        raise ValueError("tối đa %d file" % MAX_FILES)
    cfg, acfg, dcfg = load()
    draft_id = datetime.now().strftime("%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    d = os.path.join(drafts_root(acfg), draft_id)
    os.makedirs(os.path.join(d, "files"))
    import base64
    used, saved, total = set(), [], 0
    for f in files:
        data = base64.b64decode(f.get("data") or "")
        total += len(data)
        if total > MAX_BYTES:
            raise ValueError("tổng dung lượng file vượt %d MB" % (MAX_BYTES // 1024 // 1024))
        name = safe_name(f.get("name"), used)
        with open(os.path.join(d, "files", name), "wb") as out:
            out.write(data)
        saved.append({"name": name, "size": len(data), "type": f.get("type") or ""})
    with open(os.path.join(d, "request.json"), "w", encoding="utf-8") as f:
        json.dump({"prompt": prompt, "files": saved, "opts": opts or {}}, f, ensure_ascii=False, indent=1)
    set_state(d, state="drafting", files=saved, prompt=prompt, opts=opts or {})
    threading.Thread(target=_run_draft, args=(d,), daemon=True).start()
    return draft_id


def _run_draft(d):
    try:
        cfg, acfg, dcfg = load()
        req = json.load(open(os.path.join(d, "request.json"), encoding="utf-8"))
        api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
        files = "\n".join("- %s/files/%s (%s, %d byte)" % (d, x["name"], x["type"] or "?", x["size"])
                          for x in req["files"]) or "(không có)"
        out = os.path.join(d, "draft.json")
        prompt = PROMPT.format(project_name="SOAR", prompt=req["prompt"] or "(xem file đính kèm)", files=files,
                               fe_repo=acfg["fe_repo"], be_repo=acfg["be_repo"], fe_url=dcfg.get("fe_url", ""),
                               api_url=dcfg.get("api_url", ""), out=out,
                               examples=examples(api, cfg, dcfg.get("qa_user_id") or 0))
        with open(os.path.join(d, "prompt.md"), "w", encoding="utf-8") as f:
            f.write(prompt)
        root = os.path.expanduser(acfg["root"])
        prof, why = af.choose_profile(root, acfg)
        if prof is None:
            raise RuntimeError("không có gói Claude dùng được (%s)" % why)
        af.use_profile(acfg, prof)
        scope = lambda p: "//" + p.lstrip("/") + "/**"  # noqa: E731
        allowed = ["Read(%s)" % scope(d), "Read(%s)" % scope(acfg["fe_repo"]), "Read(%s)" % scope(acfg["be_repo"]),
                   "Glob", "Grep", "Write(//%s)" % out.lstrip("/"), "Edit(//%s)" % out.lstrip("/")]
        cmd = [acfg["claude"], "-p", prompt, "--permission-mode", "dontAsk", "--max-budget-usd", "1.5",
               "--output-format", "json", "--add-dir", acfg["fe_repo"], acfg["be_repo"],
               "--allowedTools"] + allowed + ["--disallowedTools", "Bash", "WebFetch", "WebSearch"]
        if acfg.get("model"):
            cmd += ["--model", acfg["model"]]
        with open(os.path.join(d, "claude.json"), "w", encoding="utf-8") as so, \
                open(os.path.join(d, "claude.err"), "w", encoding="utf-8") as se:
            code = subprocess.run(cmd, cwd=d, stdout=so, stderr=se, stdin=subprocess.DEVNULL,
                                  env=af.build_env(acfg), timeout=600).returncode
        if not os.path.exists(out):
            raise RuntimeError("Claude không ghi bản nháp (mã %s, xem %s/claude.err)" % (code, d))
        draft = json.load(open(out, encoding="utf-8"))
        if not draft.get("subject") or not draft.get("description"):
            raise RuntimeError("bản nháp thiếu tiêu đề / mô tả")
        set_state(d, state="ready", draft=draft, profile=prof["name"])
    except Exception as ex:  # noqa: BLE001
        set_state(d, state="error", error=str(ex)[:500])


def get_draft(draft_id):
    _, acfg, _ = load()
    if not re.match(r"^[\w-]+$", draft_id or ""):
        raise ValueError("id bản nháp không hợp lệ")
    path = os.path.join(drafts_root(acfg), draft_id, "state.json")
    if not os.path.exists(path):
        raise ValueError("không có bản nháp này")
    with open(path, encoding="utf-8") as f:
        return dict(json.load(f), id=draft_id)


def upload_attachment(api, wp_id, path, name, ctype):
    boundary = uuid.uuid4().hex
    meta = json.dumps({"fileName": name}).encode()
    with open(path, "rb") as f:
        data = f.read()
    body = b"".join([
        b"--" + boundary.encode() + b"\r\nContent-Disposition: form-data; name=\"metadata\"\r\n"
        b"Content-Type: application/json\r\n\r\n" + meta + b"\r\n",
        b"--" + boundary.encode() + b"\r\nContent-Disposition: form-data; name=\"file\"; filename=\""
        + name.encode("utf-8") + b"\"\r\nContent-Type: " + (ctype or "application/octet-stream").encode() + b"\r\n\r\n"
        + data + b"\r\n--" + boundary.encode() + b"--\r\n"])
    req = urllib.request.Request(api.url + "/api/v3/work_packages/%s/attachments" % wp_id, data=body, method="POST",
                                 headers=dict(api.headers, **{"Content-Type": "multipart/form-data; boundary=" + boundary}))
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def create(draft_id, subject, description, type_name="Bug", priority_name="High", assignee="me"):
    """Tạo work package trạng thái New từ bản nháp (đã sửa). assignee: "me" | "none" | id người dùng."""
    st = get_draft(draft_id)
    if st.get("state") == "created":
        raise ValueError("bản nháp này đã tạo task %s" % st.get("display_id"))
    subject = (subject or "").strip()
    if not subject:
        raise ValueError("cần tiêu đề")
    cfg, acfg, dcfg = load()
    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    types = {t["name"]: t["id"] for t in api.get("/api/v3/projects/%s/types" % cfg["project"])["_embedded"]["elements"]}
    prios = {p["name"]: p["id"] for p in api.get("/api/v3/priorities")["_embedded"]["elements"]}
    links = {"type": {"href": "/api/v3/types/%s" % types.get(type_name, TYPE_BUG)},
             "status": {"href": "/api/v3/statuses/%s" % STATUS_NEW}}
    if priority_name in prios:
        links["priority"] = {"href": "/api/v3/priorities/%s" % prios[priority_name]}
    if assignee == "me":
        links["assignee"] = {"href": "/api/v3/users/%s" % api.me()["id"]}
    elif assignee not in (None, "", "none"):
        links["assignee"] = {"href": "/api/v3/users/%s" % int(assignee)}
    wp = af.op_write(api, "POST", "/api/v3/projects/%s/work_packages" % cfg["project"], {
        "subject": subject, "description": {"format": "markdown", "raw": description or ""}, "_links": links})
    d = os.path.join(drafts_root(acfg), draft_id)
    uploaded, errors = {}, []
    for x in st.get("files") or []:
        try:
            att = upload_attachment(api, wp["id"], os.path.join(d, "files", x["name"]), x["name"], x.get("type"))
            uploaded[x["name"]] = att["id"]
        except Exception as ex:  # noqa: BLE001
            errors.append("%s: %s" % (x["name"], ex))
    raw = description or ""
    new_raw = re.sub(r"\(attachment:([^)\s]+)\)",
                     lambda m: "(/api/v3/attachments/%s/content)" % uploaded[m.group(1)] if m.group(1) in uploaded
                     else m.group(0), raw)
    if new_raw != raw:
        lock = api.get("/api/v3/work_packages/%s" % wp["id"])["lockVersion"]
        af.op_write(api, "PATCH", "/api/v3/work_packages/%s" % wp["id"], {
            "lockVersion": lock, "description": {"format": "markdown", "raw": new_raw}})
    display_id = wp.get("displayId") or "WP-%s" % wp["id"]
    set_state(d, state="created", wp_id=wp["id"], display_id=display_id, upload_errors=errors)
    af.inbox(cfg, "**TẠO TASK** %s — %s" % (display_id, subject))
    return {"wp_id": wp["id"], "display_id": display_id, "url": "%s/work_packages/%s" % (cfg["url"], wp["id"]),
            "attachments": len(uploaded), "errors": errors}


def list_drafts(limit=8):
    _, acfg, _ = load()
    root = drafts_root(acfg)
    out = []
    for name in sorted(os.listdir(root), reverse=True)[:limit]:
        try:
            st = get_draft(name)
        except ValueError:
            continue
        out.append({"id": name, "state": st.get("state"), "subject": (st.get("draft") or {}).get("subject", ""),
                    "prompt": (st.get("prompt") or "")[:120], "display_id": st.get("display_id"),
                    "updated_at": st.get("updated_at")})
    return out
