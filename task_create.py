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
import repos as R  # noqa: E402
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
{repo_refs}
- Môi trường test: web {fe_url}, API `$BASE` = `{api_url}`
Chỉ tra cứu đủ để ghi chính xác; không cần đọc lan man.

## Ticket mẫu của tester (bắt chước bố cục, cách đặt tiêu đề, giọng văn)
{examples}

## Quy tắc
- Tiêu đề: `{title_template}` (bug); việc không phải lỗi
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


def load(project=None):
    """(cfg, acfg, dcfg) theo dự án (mã / tiền tố; trống = dự án mặc định) — tạo task vào dự án OP đó."""
    import projects as P
    cfg = op_watch.load_config(argparse.Namespace(config=None, interval=None, on_change=None, no_notify=True))
    acfg = dict(af.AUTOFIX_DEFAULTS)
    acfg.update(cfg.get("autofix") or {})
    proj = (P.get(cfg, project) if project else None) or P.default(cfg)
    cfg, acfg = P.scope(cfg, acfg, proj)
    return cfg, acfg, envconf.deploy_config(proj["key"])


def draft_project(d):
    """Dự án của bản nháp (chọn lúc tạo, lưu trong request.json → opts.project)."""
    try:
        with open(os.path.join(d, "request.json"), encoding="utf-8") as f:
            return (json.load(f).get("opts") or {}).get("project") or None
    except (OSError, ValueError):
        return None


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
    import projects as P
    _, acfg, _ = load()
    cache = os.path.join(os.path.expanduser(acfg["root"]), ".task-examples%s.json" % (
        "" if (P.current(cfg) or {}).get("_legacy", True) else "-" + P.current(cfg)["key"]))
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


def _file_hash(path):
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _save_files(d, files, existing=()):
    """Ghi file (base64 từ dashboard) vào <draft>/files/, tên không trùng file đã có. Giới hạn số file / tổng dung
    lượng tính cả file đã đính kèm trước đó. Kiểm tra hết rồi mới ghi (lỗi thì không để lại file dở).
    File trùng nội dung với file đã có (hoặc file khác trong cùng lượt) thì KHÔNG lưu thêm bản mới — vd bấm "Soạn lại"
    nhiều lần, form gửi lại cùng file → trước đây sinh a_1, a_2… và đính kèm trùng lên ticket.
    Trả (saved, renamed): saved = các file mới thật sự ghi; renamed = tên gốc → tên đang lưu (file mới hoặc file cũ
    trùng nội dung)."""
    import base64
    import hashlib
    existing = list(existing or [])
    used, total = {x["name"] for x in existing}, sum(int(x.get("size") or 0) for x in existing)
    by_hash = {}
    for x in existing:
        h = _file_hash(os.path.join(d, "files", x["name"]))
        if h:
            by_hash.setdefault(h, x["name"])
    items, renamed = [], {}
    for f in files:
        data = base64.b64decode(f.get("data") or "")
        h = hashlib.sha256(data).hexdigest()
        if h in by_hash:
            renamed[f.get("name")] = by_hash[h]
            continue
        total += len(data)
        if total > MAX_BYTES:
            raise ValueError("tổng dung lượng file vượt %d MB" % (MAX_BYTES // 1024 // 1024))
        name = safe_name(f.get("name"), used)
        by_hash[h] = name
        renamed[f.get("name")] = name
        items.append((name, data, f.get("type") or ""))
    if len(existing) + len(items) > MAX_FILES:
        raise ValueError("tối đa %d file (đã có %d)" % (MAX_FILES, len(existing)))
    os.makedirs(os.path.join(d, "files"), exist_ok=True)
    saved = []
    for name, data, ctype in items:
        with open(os.path.join(d, "files", name), "wb") as out:
            out.write(data)
        saved.append({"name": name, "size": len(data), "type": ctype})
    return saved, renamed


def start_draft(prompt, files, opts=None):
    """Lưu yêu cầu + file, chạy Claude soạn nền. Trả id bản nháp (theo dõi bằng get_draft)."""
    prompt = (prompt or "").strip()
    if not prompt and not files:
        raise ValueError("cần mô tả task hoặc file đính kèm")
    if len(files) > MAX_FILES:
        raise ValueError("tối đa %d file" % MAX_FILES)
    cfg, acfg, dcfg = load((opts or {}).get("project"))
    draft_id = datetime.now().strftime("%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    d = os.path.join(drafts_root(acfg), draft_id)
    os.makedirs(os.path.join(d, "files"))
    saved, _ = _save_files(d, files)
    with open(os.path.join(d, "request.json"), "w", encoding="utf-8") as f:
        json.dump({"prompt": prompt, "files": saved, "opts": opts or {}}, f, ensure_ascii=False, indent=1)
    set_state(d, state="drafting", files=saved, prompt=prompt, opts=opts or {})
    threading.Thread(target=_run_draft, args=(d,), daemon=True).start()
    return draft_id


REFINE_NOTE = """

## Soạn lại (lần {round}) — người dùng đã trả lời các điểm AI gợi ý bổ sung
Bản nháp hiện tại (người dùng có thể đã sửa tay — giữ phần họ sửa, chỉ cập nhật theo thông tin mới):
### Tiêu đề
{subject}
### Mô tả
{description}

### Thông tin người dùng bổ sung (câu hỏi → trả lời; coi là ĐÚNG)
{answers}

Viết lại tiêu đề + mô tả cho đầy đủ, đưa thông tin trên vào đúng mục (bước thực hiện, kết quả mong đợi, dữ liệu / tài
khoản…), bỏ các `[Cần bổ sung]` đã có câu trả lời. "questions" chỉ còn các điểm CHƯA được trả lời và thật sự cần — đủ
rồi thì để [].
"""


def _account_passwords(dcfg):
    """{tên: mật khẩu|""} các tài khoản test đã khai báo: tài khoản server (deploy.json / tab Dự án "accounts")
    + tài khoản tester trong qa_ticket_accounts (mật khẩu lấy từ bộ nhớ soar_api nếu đã từng dùng)."""
    out = {}
    for name, v in (dcfg.get("accounts") or {}).items():
        out[name] = (v or {}).get("password") or "" if isinstance(v, dict) else ""
    try:
        with open(os.path.expanduser("~/.cache/op-watch/qa_accounts.json"), encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    for name in dcfg.get("qa_ticket_accounts") or []:
        out.setdefault(name, cache.get(name) or "")
    return out


def known_accounts(draft_id=None):
    """Danh sách tài khoản cho ô chọn trên dashboard — KHÔNG trả mật khẩu (chỉ báo đã lưu hay chưa)."""
    if draft_id:
        _, acfg, _ = load()
        _, _, dcfg = load(draft_project(os.path.join(drafts_root(acfg), draft_id)))
    else:
        _, _, dcfg = load()
    server = set((dcfg.get("accounts") or {}).keys())
    return [{"user": n, "has_password": bool(p), "source": "server" if n in server else "tester"}
            for n, p in _account_passwords(dcfg).items()]


def _expand_accounts(d, answers):
    """answers[i]["accounts"] (tên đã chọn) → nối "tài khoản / mật khẩu" vào câu trả lời (mật khẩu lấy ở server)."""
    names = [n for x in answers for n in (x.get("accounts") or [])]
    if not names:
        return answers
    _, _, dcfg = load(draft_project(d))
    pw = _account_passwords(dcfg)
    for x in answers:
        sel = [n for n in (x.get("accounts") or []) if n in pw]
        if sel:
            lines = ["- %s / %s" % (n, pw[n] or "[Cần bổ sung mật khẩu]") for n in sel]
            x["a"] = ((x.get("a") or "").strip() + "\nTài khoản test đã chọn (tài khoản / mật khẩu):\n"
                      + "\n".join(lines)).strip()
    return answers


def refine_draft(draft_id, answers, subject="", description="", files=None):
    """Người dùng trả lời các câu AI gợi ý bổ sung → AI soạn lại bản nháp (chạy nền, giữ file đính kèm).
    answers: [{"q": câu hỏi, "a": trả lời, "files": [tên file trong files gửi kèm]}] — chỉ các câu có nhập; cộng dồn
    qua các lần soạn lại. files: file bổ sung [{"name","type","data"(base64)}] — thêm vào danh sách file của bản nháp
    (AI đọc khi soạn lại, đính kèm lên ticket khi tạo)."""
    st = get_draft(draft_id)
    if st.get("state") == "drafting":
        raise ValueError("AI đang soạn, chờ xong rồi soạn lại")
    if st.get("state") == "created":
        raise ValueError("bản nháp này đã tạo task")
    files = list(files or [])
    answers = [{"q": str(x.get("q") or "").strip()[:500], "a": str(x.get("a") or "").strip()[:2000],
                "accounts": [str(n) for n in (x.get("accounts") or [])][:20],
                "files": [str(n) for n in (x.get("files") or [])][:MAX_FILES]}
               for x in answers or [] if str(x.get("a") or "").strip() or x.get("accounts") or x.get("files")]
    if not answers and not files:
        raise ValueError("chưa nhập thông tin hoặc đính kèm file cho câu nào")
    _, acfg, _ = load()
    d = os.path.join(drafts_root(acfg), draft_id)
    if files:
        rq_path = os.path.join(d, "request.json")
        req = json.load(open(rq_path, encoding="utf-8"))
        saved, renamed = _save_files(d, files, req.get("files"))  # tên gốc → tên đang lưu (file trùng nội dung: tên cũ)
        req["files"] = (req.get("files") or []) + saved
        with open(rq_path, "w", encoding="utf-8") as f:
            json.dump(req, f, ensure_ascii=False, indent=1)
        set_state(d, files=req["files"])
        claimed = set()
        for x in answers:
            names = [renamed[n] for n in x["files"] if n in renamed]
            claimed.update(names)
            if names:
                x["a"] = (x["a"] + "\nFile bổ sung cho ý này (đã thêm vào danh sách file đính kèm, đọc kỹ): "
                          + ", ".join(names)).strip()
        rest = [x["name"] for x in saved if x["name"] not in claimed]
        if rest:
            answers.append({"q": "", "a": "Người dùng đính kèm thêm file (đã thêm vào danh sách file đính kèm, đọc "
                                         "kỹ): " + ", ".join(rest), "accounts": []})
    answers = [{"q": x["q"], "a": x["a"]} for x in _expand_accounts(d, answers)]
    path = os.path.join(d, "refine.json")
    data = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {"round": 0, "answers": []}
    data["round"] += 1
    seen = {(x.get("q"), x.get("a")) for x in data["answers"]}  # bấm soạn lại nhiều lần: form gửi lại cùng câu trả lời
    data["answers"] += [x for x in answers if (x["q"], x["a"]) not in seen and not seen.add((x["q"], x["a"]))]
    cur = st.get("draft") or {}
    data["subject"] = (subject or cur.get("subject") or "").strip()
    data["description"] = (description or cur.get("description") or "").strip()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    if os.path.exists(os.path.join(d, "draft.json")):
        os.replace(os.path.join(d, "draft.json"), os.path.join(d, "draft-%d.json" % (data["round"] - 1)))
    set_state(d, state="drafting", refine_round=data["round"], error=None)
    threading.Thread(target=_run_draft, args=(d,), daemon=True).start()
    return data["round"]


def _file_line(d, x):
    """Một dòng liệt kê file đính kèm cho prompt. Claude headless không đọc được .docx → chuyển sẵn sang
    markdown ở <draft>/text/<tên>.md (ngoài files/ nên không bị upload lên ticket) và trỏ prompt tới đó."""
    src = os.path.join(d, "files", x["name"])
    line = "- %s (%s, %d byte)" % (src, x["type"] or "?", x["size"])
    if not x["name"].lower().endswith(".docx"):
        return line
    try:
        import docx_text
        os.makedirs(os.path.join(d, "text"), exist_ok=True)
        md = os.path.join(d, "text", x["name"] + ".md")
        with open(md, "w", encoding="utf-8") as f:
            f.write(docx_text.convert(src))
        return line + " — file Word, ĐỌC nội dung đã chuyển sang markdown: %s" % md
    except Exception as ex:  # docx hỏng / không phải zip: để Claude báo không đọc được như cũ
        return line + " — không chuyển được sang văn bản (%s)" % str(ex)[:120]


def _run_draft(d):
    try:
        cfg, acfg, dcfg = load(draft_project(d))
        req = json.load(open(os.path.join(d, "request.json"), encoding="utf-8"))
        api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
        files = "\n".join(_file_line(d, x) for x in req["files"]) or "(không có)"
        out = os.path.join(d, "draft.json")
        import projects as P
        prompt = PROMPT.format(project_name=P.name(acfg), title_template=P.title_template(acfg), prompt=req["prompt"] or "(xem file đính kèm)", files=files,
                               repo_refs=R.prompt_refs(acfg), fe_url=dcfg.get("fe_url", ""),
                               api_url=dcfg.get("api_url", ""), out=out,
                               examples=examples(api, cfg, dcfg.get("qa_user_id") or 0))
        rp = os.path.join(d, "refine.json")
        if os.path.exists(rp):
            ref = json.load(open(rp, encoding="utf-8"))
            prompt += REFINE_NOTE.format(round=ref["round"], subject=ref.get("subject") or "", description=(
                ref.get("description") or "")[:12000], answers="\n".join("- %s → %s" % (a["q"] or "(bổ sung)", a["a"])
                                                                         for a in ref["answers"]))
        with open(os.path.join(d, "prompt.md"), "w", encoding="utf-8") as f:
            f.write(prompt)
        scope = lambda p: "//" + p.lstrip("/") + "/**"  # noqa: E731
        paths = [r["path"] for r in R.repos(acfg)]
        allowed = ["Read(%s)" % scope(d)] + ["Read(%s)" % scope(x) for x in paths] + [
                   "Glob", "Grep", "Write(//%s)" % out.lstrip("/"), "Edit(//%s)" % out.lstrip("/")]
        cmd = [acfg["claude"], "-p", prompt, "--permission-mode", "dontAsk", "--max-budget-usd", "1.5",
               "--output-format", "json", "--add-dir"] + paths + [
               "--allowedTools"] + allowed + ["--disallowedTools", "Bash", "WebFetch", "WebSearch"]
        if acfg.get("model"):
            cmd += ["--model", acfg["model"]]
        res, prof = af.claude_oneshot(acfg, cmd, os.path.join(d, "claude.json"), os.path.join(d, "claude.err"),
                                      cwd=d, timeout=600)  # gói bị vô hiệu → tự dùng gói khác
        code = res.returncode
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
    cfg, acfg, dcfg = load((st.get("opts") or {}).get("project"))
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
    uploaded, errors, by_hash = {}, [], {}
    for x in st.get("files") or []:
        h = _file_hash(os.path.join(d, "files", x["name"]))
        if h and h in by_hash:  # cùng nội dung với file đã đính kèm (bản nháp cũ còn a_1, a_2…) → không đính kèm lại
            uploaded[x["name"]] = uploaded[by_hash[h]]
            continue
        try:
            att = upload_attachment(api, wp["id"], os.path.join(d, "files", x["name"]), x["name"], x.get("type"))
            uploaded[x["name"]] = att["id"]
            if h:
                by_hash[h] = x["name"]
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
            "attachments": len(set(uploaded.values())), "errors": errors}


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
