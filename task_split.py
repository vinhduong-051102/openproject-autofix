#!/usr/bin/env python3
"""
task_split: tách một việc lớn (ticket OpenProject) thành các việc con nhỏ, làm được độc lập.

1. start_split(SOAR-x): tải ticket (mô tả + lịch sử + ảnh) và đọc file đính kèm (zip / tar / xlsx / docx / text), Claude
   đọc thêm mã nguồn các repo (chỉ đọc) → đề xuất danh sách việc con (split.json), chạy nền.
2. Người dùng xem / sửa / bỏ chọn trên trang theo dõi (popup ticket → "✂ Tách việc").
3. create_children(id, tasks): tạo từng việc con trạng thái New, task cha = ticket gốc; tạo lại thì bỏ qua việc đã tạo.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402
import op_autofix as af  # noqa: E402
import op_watch  # noqa: E402
import repos as R  # noqa: E402
import task_create  # noqa: E402

MAX_TASKS = 30
ATT_MAX = 30 * 1024 * 1024

PROMPT = """Bạn là trưởng nhóm dev dự án {project_name}. Tách việc LỚN dưới đây thành các VIỆC CON nhỏ để giao riêng
(autofix / dev làm từng việc độc lập, tester kiểm tra từng việc). CHỈ ĐỌC, không sửa file nào ngoài file kết quả.

## Việc cần tách
Ticket {display_id} — đọc {d}/ticket/ticket.md (mô tả + lịch sử; ảnh trong {d}/ticket/images/).
Nội dung file đính kèm đã trích ra chữ: {docs}
{hint}
## Mã nguồn (chỉ đọc, để chia việc theo đúng phần code / màn hình / API)
{repo_refs}

## Cách chia
- Mỗi việc con = MỘT thay đổi làm và kiểm tra được độc lập (một lỗi, một chức năng nhỏ, một API). Gộp các ý rất nhỏ
  cùng một chỗ code / cùng một màn hình thành một việc; không chia vụn tới mức một việc chỉ đổi một chữ ở chỗ khác nhau.
- Việc lớn là danh sách lỗi (vd bộ báo cáo test) thì thường mỗi lỗi một việc con; lỗi trùng nguyên nhân thì gộp.
- Tối đa {max_tasks} việc. Xếp theo ưu tiên (Major / chặn trước).
- Tiêu đề theo mẫu: {title_template} — ngắn, nêu đúng màn hình / chức năng và lỗi.
- Mô tả (markdown, tiếng Việt) TỰ ĐỦ để làm mà không cần mở ticket cha: chép phần môi trường / tài khoản / dữ liệu test
  liên quan từ ticket cha, nội dung lỗi chi tiết từ file đính kèm (bước tái hiện, kết quả mong đợi, thực tế, bằng chứng
  API nếu có), gợi ý vị trí code nếu đọc được (file / hàm). Cuối mô tả ghi "Thuộc việc: {display_id}".
- Không bịa: thông tin thiếu thì ghi rõ "[Cần bổ sung] …".
- type: "Bug" nếu là lỗi, "Task" nếu là việc khác (xác nhận, cấu hình…). priority: "Immediate" | "High" | "Normal" | "Low".

## Kết quả
Ghi file {out} (JSON, UTF-8), không in ra màn hình:
{{"summary": "1–2 câu: chia theo cách nào", "notes": "điều cần người dùng xem lại (nếu có)",
  "tasks": [{{"subject": "", "type": "Bug", "priority": "High", "ref": "mã / số thứ tự trong việc cha, vd BUG 001",
              "description": "markdown"}}]}}
"""


def splits_root(acfg):
    d = os.path.join(os.path.expanduser(acfg["root"]), ".splits")
    os.makedirs(d, exist_ok=True)
    return d


def _dir(acfg, split_id):
    if not re.match(r"^[\w-]+$", split_id or ""):
        raise ValueError("id không hợp lệ")
    d = os.path.join(splits_root(acfg), split_id)
    if not os.path.isdir(d):
        raise ValueError("không có lượt tách này")
    return d


def get_split(split_id):
    _, acfg, _ = task_create.load()
    with open(os.path.join(_dir(acfg, split_id), "state.json"), encoding="utf-8") as f:
        return dict(json.load(f), id=split_id)


def latest_for(display_id):
    """Lượt tách gần nhất của ticket (để mở lại khi đóng popup)."""
    _, acfg, _ = task_create.load()
    root = splits_root(acfg)
    for name in sorted(os.listdir(root), reverse=True):
        if name.startswith(display_id + "-"):
            try:
                return get_split(name)
            except (ValueError, OSError):
                continue
    return None


def start_split(display_id, hint=""):
    display_id = (display_id or "").strip()
    if not re.match(r"^%s$" % envconf.TICKET_RX, display_id) and not re.match(r"^\d+$", display_id):
        raise ValueError("mã ticket không hợp lệ: %s" % display_id)
    _, acfg, _ = task_create.load()
    cur = latest_for(display_id)
    if cur and cur.get("state") == "running":
        return cur["id"]
    split_id = "%s-%s-%s" % (display_id, datetime.now().strftime("%m%d-%H%M%S"), secrets.token_hex(2))
    d = os.path.join(splits_root(acfg), split_id)
    os.makedirs(d)
    task_create.set_state(d, state="running", display_id=display_id, hint=(hint or "").strip()[:4000],
                          started_at=datetime.now().isoformat(timespec="seconds"))
    threading.Thread(target=_run, args=(d, display_id, (hint or "").strip()), daemon=True).start()
    return split_id


def _attachments(api, wp, d):
    """Tải file đính kèm không phải ảnh, trích chữ (zip / tar / xlsx / docx / text) vào docs/. Trả danh sách mô tả."""
    import tg_requests
    os.makedirs(os.path.join(d, "att"), exist_ok=True)
    os.makedirs(os.path.join(d, "docs"), exist_ok=True)
    res = api.get("/api/v3/work_packages/%s/attachments" % wp["id"])
    lines, used = [], set()
    for a in (res.get("_embedded") or {}).get("elements") or []:
        ctype = ((a.get("contentType") or "") if isinstance(a.get("contentType"), str) else "")
        if ctype.startswith("image/") or (a.get("fileSize") or 0) > ATT_MAX:
            continue
        name = task_create.safe_name(a.get("fileName") or "file", used)
        path = os.path.join(d, "att", name)
        req = op_watch.urllib.request.Request(api.url + "/api/v3/attachments/%s/content" % a["id"], headers=api.headers)
        try:
            with op_watch.urllib.request.urlopen(req, timeout=120) as resp, open(path, "wb") as f:
                shutil.copyfileobj(resp, f)
        except (op_watch.urllib.error.URLError, OSError) as ex:
            lines.append("- %s: không tải được (%s)" % (name, ex))
            continue
        docs, notes = tg_requests.extract_docs(path)
        for i, x in enumerate(docs):
            out = os.path.join(d, "docs", "%s__%02d__%s.txt" % (name, i, re.sub(r"[^\w.-]+", "_", x["name"])[-80:]))
            with open(out, "w", encoding="utf-8") as f:
                f.write(x["text"])
            lines.append("- %s → %s (%d ký tự)" % (name if x["name"] == name else "%s / %s" % (name, x["name"]),
                                                 out, len(x["text"])))
        lines += ["  (%s)" % n for n in notes[:5]]
    return lines


def _run(d, display_id, hint):
    try:
        cfg, acfg, _ = task_create.load()
        acfg = dict(af.AUTOFIX_DEFAULTS, **acfg)
        api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
        wp = af.fetch_ticket(api, display_id, os.path.join(d, "ticket"))
        docs = _attachments(api, wp, d)
        task_create.set_state(d, wp_id=wp["id"], subject=wp.get("subject"), docs=len(docs))
        out = os.path.join(d, "split.json")
        prompt = PROMPT.format(project_name=envconf.PROJECT_NAME, display_id=wp.get("displayId") or display_id, d=d,
                               docs=("\n" + "\n".join(docs)) if docs else "(không có)",
                               hint=("\n## Ghi chú của người dùng khi tách\n%s\n" % hint) if hint else "",
                               repo_refs=R.prompt_refs(acfg), max_tasks=MAX_TASKS,
                               title_template=envconf.TASK_TITLE_TEMPLATE, out=out)
        with open(os.path.join(d, "prompt.md"), "w", encoding="utf-8") as f:
            f.write(prompt)
        prof, why = af.choose_profile(os.path.expanduser(acfg["root"]), acfg)
        if prof is None:
            raise RuntimeError("không có gói Claude dùng được (%s)" % why)
        af.use_profile(acfg, prof)
        scope = lambda p: "//" + p.lstrip("/") + "/**"  # noqa: E731
        paths = [r["path"] for r in R.repos(acfg)]
        allowed = ["Read(%s)" % scope(d)] + ["Read(%s)" % scope(x) for x in paths] + [
            "Glob", "Grep", "Write(//%s)" % out.lstrip("/"), "Edit(//%s)" % out.lstrip("/")]
        cmd = [acfg["claude"], "-p", prompt, "--permission-mode", "dontAsk", "--max-budget-usd", "3",
               "--output-format", "json", "--add-dir"] + paths + [
            "--allowedTools"] + allowed + ["--disallowedTools", "Bash", "WebFetch", "WebSearch"]
        if acfg.get("model"):
            cmd += ["--model", acfg["model"]]
        with open(os.path.join(d, "claude.json"), "w", encoding="utf-8") as so, \
                open(os.path.join(d, "claude.err"), "w", encoding="utf-8") as se:
            code = subprocess.run(cmd, cwd=d, stdout=so, stderr=se, stdin=subprocess.DEVNULL,
                                  env=af.build_env(acfg), timeout=1200).returncode
        if not os.path.exists(out):
            raise RuntimeError("Claude không ghi kết quả (mã %s, xem %s/claude.err)" % (code, d))
        res = json.load(open(out, encoding="utf-8"))
        tasks = [t for t in res.get("tasks") or [] if isinstance(t, dict) and (t.get("subject") or "").strip()]
        if not tasks:
            raise RuntimeError("AI không đề xuất việc con nào")
        task_create.set_state(d, state="ready", summary=res.get("summary") or "", notes=res.get("notes") or "",
                              tasks=tasks[:MAX_TASKS], profile=prof["name"])
    except Exception as ex:  # noqa: BLE001
        task_create.set_state(d, state="error", error=str(ex)[:500])


def create_children(split_id, tasks, assignee="me"):
    """Tạo các việc con (danh sách đã sửa, chỉ phần được chọn). Việc đã tạo (cùng vị trí idx) thì bỏ qua."""
    st = get_split(split_id)
    if st.get("state") not in ("ready", "created"):
        raise ValueError("lượt tách chưa xong")
    tasks = [t for t in tasks or [] if isinstance(t, dict) and (t.get("subject") or "").strip()]
    if not tasks:
        raise ValueError("chưa chọn việc con nào")
    if len(tasks) > MAX_TASKS:
        raise ValueError("tối đa %d việc con" % MAX_TASKS)
    cfg, acfg, _ = task_create.load()
    d = _dir(acfg, split_id)
    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    types = {t["name"]: t["id"] for t in api.get("/api/v3/projects/%s/types" % cfg["project"])["_embedded"]["elements"]}
    prios = {p["name"]: p["id"] for p in api.get("/api/v3/priorities")["_embedded"]["elements"]}
    me = api.me()["id"] if assignee == "me" else None
    created = dict(st.get("created") or {})
    made, errors = [], []
    for t in tasks:
        key = str(t.get("idx"))
        if key in created:
            continue
        links = {"type": {"href": "/api/v3/types/%s" % types.get(t.get("type") or "Bug", task_create.TYPE_BUG)},
                 "status": {"href": "/api/v3/statuses/%s" % task_create.STATUS_NEW},
                 "parent": {"href": "/api/v3/work_packages/%s" % st["wp_id"]}}
        if t.get("priority") in prios:
            links["priority"] = {"href": "/api/v3/priorities/%s" % prios[t["priority"]]}
        if me:
            links["assignee"] = {"href": "/api/v3/users/%s" % me}
        elif assignee not in (None, "", "none"):
            links["assignee"] = {"href": "/api/v3/users/%s" % int(assignee)}
        try:
            wp = af.op_write(api, "POST", "/api/v3/projects/%s/work_packages" % cfg["project"], {
                "subject": t["subject"].strip()[:255],
                "description": {"format": "markdown", "raw": t.get("description") or ""}, "_links": links})
        except Exception as ex:  # noqa: BLE001 — một việc lỗi không chặn việc khác; tạo lại sẽ thử tiếp
            errors.append("%s: %s" % (t["subject"][:60], str(ex)[:200]))
            continue
        created[key] = {"wp_id": wp["id"], "display_id": wp.get("displayId") or "WP-%s" % wp["id"],
                        "subject": t["subject"]}
        made.append(created[key])
        task_create.set_state(d, created=created)  # ghi ngay: lỗi giữa chừng không tạo trùng khi bấm lại
    task_create.set_state(d, state="created", created=created, errors=errors)
    if made:
        af.inbox(cfg, "**TÁCH VIỆC** %s → %d việc con: %s" % (
            st.get("display_id"), len(made), ", ".join(x["display_id"] for x in made)))
    return {"created": made, "errors": errors, "total": len(created)}


if __name__ == "__main__":
    import argparse
    import time
    p = argparse.ArgumentParser(description="Tách việc lớn thành việc con (chỉ đề xuất, không tạo trên OpenProject)")
    p.add_argument("ticket")
    p.add_argument("--hint", default="")
    a = p.parse_args()
    sid = start_split(a.ticket, a.hint)
    print("lượt tách", sid)
    while get_split(sid).get("state") == "running":
        time.sleep(5)
    s = get_split(sid)
    print(json.dumps({k: s.get(k) for k in ("state", "error", "summary", "notes")}, ensure_ascii=False, indent=1))
    for i, t in enumerate(s.get("tasks") or []):
        print("%2d. [%s/%s] %s (%s)" % (i + 1, t.get("type"), t.get("priority"), t.get("subject"), t.get("ref") or ""))
