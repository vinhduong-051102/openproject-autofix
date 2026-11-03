#!/usr/bin/env python3
"""
deploy_qa: deploy code mới lên server test (DEPLOY_HOST / DEPLOY_FE_URL trong .env) rồi QA tự động từng ticket trên server thật.
Chạy sau mỗi lần merge_to_dev (12h / 18h), hoặc tay: deploy_qa.py [--dry-run] [--qa SOAR-x ...].

1. Có gì mới: FE = commit trên origin/dev-ss sau commit "update version" gần nhất; BE = commit của origin/develop-ss
   chưa có trong origin/release-ss/lastest-k8s.
2. FE: worktree dev-ss → ./build-and-deploy.sh dev → commit "update version <VERSION>" lên dev-ss → soar-fe-ss.tar lên
   ~/soar_v2.0/images → docker load → đổi image service soar-fe-nginx trong docker-compose.yml (có backup).
3. BE: worktree release-ss/lastest-k8s → merge origin/develop-ss, push → docker build soar-be-ss-lastest-k8s:<ngày><a..z>
   → tar lên images → docker load → đổi image service service-product-content (có backup).
4. docker compose up -d --force-recreate, chờ FE đúng version + API sống.
5. QA từng ticket (của mình, đang Resolved) có commit trong bản deploy: Claude test trên server qua soar_api.py.
   ĐẠT → comment bằng chứng + chuyển người phụ trách sang QA (QA_USER_NAME). KHÔNG ĐẠT / KHÔNG TỰ TEST ĐƯỢC → chỉ báo.

Cấu hình server + tài khoản test: biến DEPLOY_* / QA_* trong .env và/hoặc ~/.config/op-watch/deploy.json (chmod 600).
"""

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import op_autofix as af  # noqa: E402
import op_watch  # noqa: E402

import envconf  # noqa: E402

DEPLOY_CONFIG = envconf.DEPLOY_CONFIG
SOAR_API = os.path.join(os.path.dirname(os.path.abspath(__file__)), "soar_api.py")
DEFAULTS = {
    "fe_service": "soar-fe-nginx",
    "be_service": "service-product-content",
    "fe_image": "soar-fe-ss",
    "be_image": "soar-be-ss-lastest-k8s",
    "be_release_branch": "release-ss/lastest-k8s",
    "qa_user_id": 0,           # id người QA trên OpenProject — đặt QA_USER_ID trong .env
    "qa_user_name": "QA",
    "wait_minutes": 15,
    "alive_wait_minutes": 5,   # chưa deploy gì: chờ server sống tối đa N phút trước khi QA
    "be_restart_tries": 3,     # BE thoát sau compose up (DB chưa sẵn sàng) → docker start lại tối đa N lần
    # QA bằng Gemini (agy, chỉ đọc): mỗi vòng Gemini trả JSON các request cần gọi, tool gọi qua soar_api.py rồi gửi
    # kết quả lại. "claude" = Claude headless tự gọi soar_api.py (tốn hạn mức Claude).
    "qa_engine": "gemini",
    "qa_rounds": 6,
    "qa_model": "gemini-3.8-flash-medium",
    "qa_calls_per_round": 8,
    "be_container": "soar",
    "qa_parallel": 3,  # số ticket QA (Gemini) chạy cùng lúc trên server 86
    # Request gặp 502/503/504 / lỗi kết nối: chờ server lên lại tối đa chừng này phút rồi gọi lại.
    "server_retry_minutes": 10,
    # QA trên server KHÔNG ĐẠT → Claude (autofix) kiểm chứng báo cáo QA rồi sửa tiếp; quá số lần này vẫn không đạt
    # thì chuyển UNREVIEWED để người xem.
    "qa_fail_autofix_max": 2,
    # DELETE chỉ cho các đường dẫn khớp (dữ liệu QA tự tạo khi test, ví dụ huỷ báo cáo kết quả AI của cảnh báo).
    "qa_delete_allow": [r"^/api/v1/alerts/[^/?]+/ai-feedback/?$"],
}

FAIL_COUNT = os.path.join(envconf.autofix_root(), "qa-fail-count.json")
# Có file này = đang QA trên server: đợt test lại bằng Gemini (op_autofix --review) chờ, nhường agy cho QA.
QA_ACTIVE = os.path.join(envconf.autofix_root(), ".qa-active")
AUTOFIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "op_autofix.py")

PENDING = os.path.join(envconf.autofix_root(), "qa-pending.json")
TRANSIENT = re.compile(r"^HTTP 50[234]\b|URLError|Connection refused|Connection reset|timed out|RemoteDisconnected",
                       re.M)


class ServerChanged(Exception):
    """Server 86 bị người khác deploy / restart / không phản hồi trong lúc QA: kết quả ticket đang test không dùng."""

QA_PROMPT = """Bạn là QA tự động, kiểm tra lại bug {display_id} — {subject} trên MÔI TRƯỜNG TEST THẬT (server 86) sau khi
bản sửa đã được deploy ({deployed}). Chạy nền, không có người theo dõi.

## Tài liệu
- Ticket (mô tả, bước tái hiện, comment, kết quả mong đợi): {qa_dir}/ticket/ticket.md ; ảnh: {qa_dir}/ticket/images/
- Bản sửa (diff các commit): {qa_dir}/diff.md

## Công cụ gọi server (DUY NHẤT)
`python3 {soar_api} <METHOD> <đường dẫn API | URL FE> [body JSON | @file]` — tự đăng nhập tài khoản QA mặc định.
Ví dụ: `python3 {soar_api} GET /api/v1/...`, `python3 {soar_api} POST /api/v1/... '{{"a":1}}'`,
`python3 {soar_api} UPLOAD /api/v1/file-attachments/upload file=@{qa_dir}/a.txt type=1 caseId=5`,
`python3 {soar_api} --header 'Accept-Language: en' GET ...`, FE tĩnh: `python3 {soar_api} GET {fe_url}/index.html`.
Chạy `python3 {soar_api}` không tham số để xem hướng dẫn. File test tạm: ghi trong {qa_dir}.

## Làm
1. Tái hiện đúng các bước trong ticket (Reopened thì theo comment tester gần nhất), so với kết quả mong đợi.
   Thử thêm 1-2 trường hợp lân cận dễ hỏng.
2. Bug chỉ thấy trên giao diện (màu, focus, nhãn trình đọc màn hình, bố cục…): kiểm tra bundle FE đã deploy
   (index.html → file /assets/*.js liên quan) có đúng thay đổi trong diff không. Không đủ chắc → KHÔNG TỰ TEST ĐƯỢC.
3. AN TOÀN — môi trường dùng chung với tester: không xoá / sửa dữ liệu không do mình tạo; dữ liệu test đặt tên bắt đầu
   `QA_AUTO_` và dọn lại nếu API cho phép; đổi cấu hình hệ thống thì trả lại giá trị cũ ngay sau khi test.
   Không gọi host khác, không dùng tài khoản khác.

## Báo cáo (bắt buộc)
Ghi {qa_dir}/qa.md bằng tiếng Việt. Dòng đầu đúng một trong: `QA: ĐẠT` | `QA: KHÔNG ĐẠT` | `QA: KHÔNG TỰ TEST ĐƯỢC`
## Kết quả — ngắn gọn cho tester: đã test gì, trên bản nào, kết quả (dùng làm comment ticket)
## Bằng chứng — từng request → mã HTTP + phần response quan trọng
## Ghi chú — dữ liệu đã tạo / đã dọn, điều chưa kiểm tra được
"""

GEMINI_QA_PROMPT = """KHÔNG dùng bất kỳ công cụ nào (không chạy lệnh, không đọc file, không gọi mạng). Một chương trình khác gọi
API giúp bạn và gửi lại kết quả ở vòng sau. Việc của bạn chỉ là trả về JSON đúng schema.

Bạn là QA, kiểm tra lại bug {display_id} trên MÔI TRƯỜNG TEST THẬT (server 86) sau khi bản sửa đã deploy ({deployed}).
Làm ĐÚNG như mô tả trong ticket: tái hiện các bước tái hiện (ticket Reopened thì theo comment tester gần nhất) và so với
KẾT QUẢ MONG ĐỢI của ticket. Không mở rộng sang việc khác ngoài ticket.

Gọi API: mỗi phần tử `calls` gồm
- method: GET | POST | PUT | PATCH | UPLOAD (multipart) | SEARCH | DELETE (chỉ cho: {delete_allow})
- SEARCH: tìm trong TOÀN BỘ mã FE đang chạy (index.html + mọi file /assets/*.js, *.css đã tải sẵn); target = đoạn
  chữ hoặc regex (Python) cần tìm, ví dụ một chuỗi i18n, tên class, aria-label, mã màu trong diff. Trả các đoạn khớp
  kèm tên file và ngữ cảnh. Dùng SEARCH để kiểm tra lỗi giao diện thay vì GET file bundle (file rất lớn, bị cắt).
- target: đường dẫn API bắt đầu bằng `/api/...` (server {api_url}, đã đăng nhập tài khoản QA mặc định) hoặc URL FE đầy
  đủ {fe_url}/... (index.html, /assets/*.js để kiểm tra mã giao diện đã deploy)
- body: JSON dạng chuỗi (POST/PUT/PATCH); với UPLOAD: các trường form, mỗi dòng `ten=gia_tri`
- header: tuỳ chọn, một header dạng `Accept-Language: en`
- file_name + file_content: (UPLOAD) tên và nội dung file văn bản sẽ gửi ở trường `file`. file_name được gửi NGUYÊN
  VĂN (kể cả "/", "\\", "<", Unicode); file_content "" = file rỗng 0 byte; bỏ trường file_content = nội dung mặc định
- account: tuỳ chọn, gọi bằng tài khoản nào (mặc định QA_superadmin — FULL quyền); password: chỉ khi lần đầu dùng
  một tài khoản test QA_AUTO_* vừa tạo (sau đó tool nhớ, chỉ cần account)
- why: gọi để kiểm tra gì
- parallel: true trên các request của CÙNG một vòng → các request đó được gửi đồng thời (test tranh chấp / gửi trùng)
Tối đa {max_calls} request mỗi vòng; response dài bị cắt.

Tài khoản: mặc định QA_superadmin (FULL quyền). Bug về phân quyền / phạm vi dữ liệu / loại tài khoản (khách hàng, phòng
ban khác, thiếu quyền…) thì TỰ TẠO tài khoản test phù hợp bằng QA_superadmin rồi gọi lại bằng tài khoản đó:
1. Phòng ban + vai trò: POST /api/v1/users/list/roles/departments?department_id=<id> body `{{}}` (vai trò của phòng ban);
   danh sách user để biết phòng ban: POST /api/v1/users/filter body `{{"page":0,"size":20}}`; khách hàng: GET /api/v1/customers/list
2. Tạo: POST /api/v1/users body (snake_case) `{{"username":"QA_AUTO_<mô_tả>","full_name":"QA AUTO …",
   "email":"qa_auto_<mô_tả>@example.com","phone_number":"09<8 số ngẫu nhiên>","type":0,"is_active":1,"status":0,
   "role_operation_ids":[],"departments":[{{"department_id":<id>,"role_id":[<id>],"is_primary":1}}]}}`
   — type: 0 nội bộ, 1 đối tác, 2 khách hàng; status PHẢI là 0 (1 = khoá); username / email / phone không trùng.
   Đã có tài khoản QA_AUTO_* phù hợp (tìm bằng /api/v1/users/filter) thì dùng lại.
3. Lấy mật khẩu: PUT /api/v1/users/reset-password/<id> → data.new_password
4. Gọi bằng tài khoản đó: account="QA_AUTO_…", password=<new_password> (lần đầu).
Không xoá tài khoản (để dùng lại); ghi các tài khoản đã tạo vào notes.

An toàn — môi trường dùng chung với tester: không sửa dữ liệu không do mình tạo; dữ liệu test đặt tên bắt đầu `QA_AUTO_`;
đổi cấu hình thì gọi trả lại giá trị cũ ngay trong vòng sau.

Lỗi chỉ thấy trên giao diện (màu, focus, nhãn trình đọc màn hình…): dùng SEARCH tìm các chuỗi / đoạn code đặc trưng
mà diff thêm vào (i18n, aria-label, class, hằng số) trong mã FE đang chạy; thấy đủ thay đổi của diff → ĐẠT (ghi rõ là
kiểm tra mã đã deploy); thiếu → KHÔNG ĐẠT; chỉ khi không xác định được chuỗi đặc trưng nào mới KHÔNG TỰ TEST ĐƯỢC.

Kết thúc (done=true) khi đã đủ bằng chứng, với verdict ĐẠT | KHÔNG ĐẠT | KHÔNG TỰ TEST ĐƯỢC và
result (ngắn gọn cho tester: đã test gì, kết quả), evidence (request → mã HTTP + phần response quan trọng),
notes (dữ liệu đã tạo / chưa kiểm tra được). Chưa xong: done=false, verdict=CHƯA, calls = các request tiếp theo.

Vòng {round}/{rounds}.{last_round}

# TICKET
{ticket}

# BẢN SỬA (diff)
{diff}

# CÁC REQUEST ĐÃ GỌI VÀ KẾT QUẢ
{transcript}
"""

QA_SCHEMA = {
    "type": "object",
    "properties": {
        "done": {"type": "boolean"},
        "calls": {"type": "array", "items": {"type": "object", "properties": {
            "method": {"type": "string"}, "target": {"type": "string"}, "body": {"type": "string"},
            "header": {"type": "string"}, "file_name": {"type": "string"}, "file_content": {"type": "string"},
            "account": {"type": "string"}, "password": {"type": "string"}, "parallel": {"type": "boolean"},
            "why": {"type": "string"}}, "required": ["method", "target"]}},
        "verdict": {"type": "string", "enum": ["ĐẠT", "KHÔNG ĐẠT", "KHÔNG TỰ TEST ĐƯỢC", "CHƯA"]},
        "result": {"type": "string"}, "evidence": {"type": "string"}, "notes": {"type": "string"},
    },
    "required": ["done", "verdict"],
}


def soar_call(qa_dir, call, dcfg=None):
    """Một request Gemini yêu cầu → chạy soar_api.py. Trả chuỗi kết quả (đã cắt)."""
    method = (call.get("method") or "GET").upper()
    if method == "SEARCH" and dcfg:
        return bundle_search(dcfg, call.get("target", ""))
    if method == "DELETE":
        if not dcfg or not any(re.search(rx, call.get("target", "")) for rx in dcfg.get("qa_delete_allow", [])):
            return "BỊ CHẶN: DELETE chỉ cho đường dẫn khớp %s" % (dcfg or {}).get("qa_delete_allow")
    elif method not in ("GET", "POST", "PUT", "PATCH", "UPLOAD"):
        return "BỊ CHẶN: chỉ cho GET / POST / PUT / PATCH / UPLOAD / SEARCH / DELETE (giới hạn)"
    cmd = ["python3", SOAR_API]
    if call.get("account"):
        cmd += ["--as", call["account"]]
        if call.get("password"):
            cmd += ["--password", call["password"]]
    if call.get("header"):
        cmd += ["--header", call["header"]]
    cmd += [method, call.get("target", "")]
    body = call.get("body") or ""
    sent_note = ""
    if method == "UPLOAD":
        if call.get("file_name") or "file_content" in call:
            # Tên file gửi đi giữ NGUYÊN VĂN như Gemini đưa (không basename: test tên chứa "/" phải tới được server);
            # file trên đĩa đặt tên riêng. file_content "" = file 0 byte; chỉ khi không có trường này mới dùng mặc định.
            name = call.get("file_name") or "QA_AUTO_file.txt"
            content = call["file_content"] if call.get("file_content") is not None else "QA_AUTO test"
            up = os.path.join(qa_dir, "upload")
            os.makedirs(up, exist_ok=True)
            path = os.path.join(up, "file-%d%s" % (len(os.listdir(up)) + 1,
                                                  os.path.splitext(name.replace("\\", "/").split("/")[-1])[1][:10]))
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            cmd.append("file=@%s;filename=%s" % (path, name))
            sent_note = "(công cụ đã gửi trường file: filename=%s, %d byte)\n" % (
                json.dumps(name, ensure_ascii=False), len(content.encode("utf-8")))
        cmd += [line.strip() for line in body.splitlines() if "=" in line]
    elif body:
        cmd.append(body)
    def once():
        try:
            return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                                  timeout=120, stdin=subprocess.DEVNULL).stdout
        except subprocess.TimeoutExpired:
            return "HẾT THỜI GIAN (120s) timed out"

    out = once()
    if dcfg and TRANSIENT.search(out[:3000]):
        # Server đang khởi động lại (có thể người khác deploy): chờ lên lại rồi gọi lại, không đưa lỗi tạm cho Gemini.
        deadline = time.time() + dcfg["server_retry_minutes"] * 60
        while time.time() < deadline and not api_alive(dcfg):
            time.sleep(15)
        out = once()
        if TRANSIENT.search(out[:3000]):
            raise ServerChanged("server không phản hồi (%s)" % out.strip().splitlines()[0][:120])
    out = sent_note + out
    return out if len(out) <= 4000 else out[:4000] + "\n… (cắt, tổng %d ký tự)" % len(out)


def fe_bundle(dcfg):
    """Tải index.html + mọi asset JS/CSS của FE đang chạy (theo import trong code), lưu đệm theo version."""
    html = urllib.request.urlopen(dcfg["fe_url"] + "/", timeout=30).read().decode("utf-8", "replace")
    m = re.search(r'<meta name="version" content="([^"]*)"', html)
    cache = os.path.join(envconf.autofix_root(), "fe-bundle-%s" % (m.group(1) if m else "unknown"))
    done_flag = os.path.join(cache, ".complete")
    if not os.path.exists(done_flag):
        os.makedirs(cache, exist_ok=True)
        with open(os.path.join(cache, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        queue, seen = re.findall(r'(?:src|href)="/?(assets/[^"]+\.(?:js|css))"', html), set()
        while queue:
            rel = queue.pop()
            if rel in seen:
                continue
            seen.add(rel)
            try:
                text = urllib.request.urlopen("%s/%s" % (dcfg["fe_url"], rel), timeout=60).read().decode(
                    "utf-8", "replace")
            except OSError:
                continue
            with open(os.path.join(cache, os.path.basename(rel)), "w", encoding="utf-8") as f:
                f.write(text)
            queue += ["assets/" + n for n in re.findall(r'["\'(/]([\w.-]+\.(?:js|css))["\')]', text)
                      if "assets/" + n not in seen]
        open(done_flag, "w").close()
    return cache


def bundle_search(dcfg, pattern, limit=12, ctx=200):
    """SEARCH của Gemini: tìm pattern (regex, lỗi regex thì tìm chữ thường) trong mã FE đang chạy."""
    if not pattern:
        return "SEARCH cần target"
    try:
        cache = fe_bundle(dcfg)
    except OSError as ex:
        return "không tải được mã FE: %s" % ex
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    hits = []
    for name in sorted(os.listdir(cache)):
        if name.startswith("."):
            continue
        with open(os.path.join(cache, name), encoding="utf-8", errors="replace") as f:
            text = f.read()
        for m in rx.finditer(text):
            hits.append("[%s @%d] …%s…" % (name, m.start(), text[max(0, m.start() - ctx):m.end() + ctx]))
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    files = len([n for n in os.listdir(cache) if not n.startswith(".")])
    return "SEARCH %r trong %d file (%s): %d kết quả%s\n%s" % (
        pattern, files, os.path.basename(cache), len(hits), " (đã dừng ở %d)" % limit if len(hits) >= limit else "",
        "\n\n".join(hits) or "không thấy")


def gemini_qa(acfg, dcfg, qa_dir, display_id, deployed, guard=None):
    """QA bằng Gemini nhiều vòng; ghi qa.md. Trả nội dung qa.md."""
    with open(os.path.join(qa_dir, "ticket", "ticket.md"), encoding="utf-8") as f:
        ticket = f.read()[-20000:]
    with open(os.path.join(qa_dir, "diff.md"), encoding="utf-8") as f:
        diff = f.read()[:30000]
    schema = os.path.join(qa_dir, "qa-schema.json")
    with open(schema, "w", encoding="utf-8") as f:
        json.dump(QA_SCHEMA, f, ensure_ascii=False)
    transcript, answer = [], {}
    rounds = dcfg["qa_rounds"]
    for rnd in range(1, rounds + 1):
        change = guard() if guard else None
        if change:
            raise ServerChanged(change)
        last = rnd == rounds
        prompt = GEMINI_QA_PROMPT.format(
            display_id=display_id, deployed=deployed, api_url=dcfg["api_url"], fe_url=dcfg["fe_url"],
            max_calls=dcfg["qa_calls_per_round"], delete_allow=", ".join(dcfg.get("qa_delete_allow", [])) or "không",
            round=rnd, rounds=rounds, ticket=ticket, diff=diff,
            last_round=" ĐÂY LÀ VÒNG CUỐI: phải done=true và kết luận." if last else "",
            transcript="\n\n".join(transcript)[-60000:] or "(chưa gọi gì)")
        cmd = [os.path.expanduser(acfg["gemini_cmd"]), "-p", prompt, "--mode", "plan", "--output-format", "json",
               "--json-schema", schema, "--print-timeout", "%ds" % (acfg["gemini_timeout"] - 30)]
        if dcfg.get("qa_model") or acfg.get("gemini_model"):
            cmd += ["--model", dcfg.get("qa_model") or acfg["gemini_model"]]
        try:
            res = subprocess.run(cmd, cwd=qa_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 universal_newlines=True, timeout=acfg["gemini_timeout"], stdin=subprocess.DEVNULL)
            line = [l for l in res.stdout.splitlines() if l.startswith("{")]
            answer = (json.loads(line[-1]).get("structured_output") or {}) if line else {}
        except (OSError, subprocess.TimeoutExpired, ValueError) as ex:
            answer = {"done": True, "verdict": "KHÔNG TỰ TEST ĐƯỢC", "result": "Gemini lỗi: %s" % ex}
        with open(os.path.join(qa_dir, "gemini-rounds.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"round": rnd, "answer": answer}, ensure_ascii=False) + "\n")
        if not answer:
            answer = {"done": True, "verdict": "KHÔNG TỰ TEST ĐƯỢC", "result": "Gemini không trả kết quả (vòng %d)" % rnd}
        if answer.get("done") or not answer.get("calls"):
            break
        calls = answer["calls"][:dcfg["qa_calls_per_round"]]
        parallel = [c for c in calls if c.get("parallel")]
        results = {}
        if len(parallel) > 1:
            # Gửi đồng thời (test tranh chấp): mỗi request một luồng, cùng xuất phát.
            import threading
            start = threading.Barrier(len(parallel))

            def worker(i, c):
                start.wait()
                try:
                    results[i] = soar_call(qa_dir, c, dcfg)
                except ServerChanged as ex:
                    results[i] = ex
            threads = [threading.Thread(target=worker, args=(calls.index(c), c)) for c in parallel]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            for v in results.values():
                if isinstance(v, ServerChanged):
                    raise v
        for i, call in enumerate(calls):
            out = results[i] if i in results else soar_call(qa_dir, call, dcfg)
            if call.get("parallel") and len(parallel) > 1:
                out = "[gửi đồng thời cùng %d request]\n%s" % (len(parallel), out)
            transcript.append("## %s%s %s%s\n(%s)\n```\n%s\n```" % (
                "[%s] " % call["account"] if call.get("account") else "", call.get("method"), call.get("target"), (" body: " + call["body"][:500]) if call.get("body") else "",
                call.get("why", ""), out))
    verdict = answer.get("verdict") if answer.get("verdict") in ("ĐẠT", "KHÔNG ĐẠT", "KHÔNG TỰ TEST ĐƯỢC") \
        else "KHÔNG TỰ TEST ĐƯỢC"
    text = "QA: %s\n\n_QA bằng Gemini (%s), %d request_\n\n## Kết quả\n%s\n\n## Bằng chứng\n%s\n\n## Ghi chú\n%s\n\n" \
           "## Toàn bộ request\n%s\n" % (
               verdict, dcfg.get("qa_model") or acfg.get("gemini_model") or "agy", len(transcript), answer.get("result", ""),
               answer.get("evidence", ""), answer.get("notes", ""), "\n\n".join(transcript))
    with open(os.path.join(qa_dir, "qa.md"), "w", encoding="utf-8") as f:
        f.write(text)
    return text


QA_TOOLS = ["Read", "Glob", "Grep", "Bash(python3 %s*)" % SOAR_API, "Bash(ls *)", "Bash(head *)", "Bash(tail *)",
            "Bash(grep *)", "Bash(wc *)", "Bash(cat *)"]


def log(msg):
    print("[%s] [deploy-qa] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def load():
    cfg = op_watch.load_config(argparse.Namespace(config=None, interval=None, on_change=None, no_notify=False))
    acfg = dict(af.AUTOFIX_DEFAULTS)
    acfg.update(cfg.get("autofix") or {})
    dcfg = dict(DEFAULTS)
    dcfg.update(envconf.deploy_config())
    return cfg, acfg, dcfg


def sh(cmd, cwd=None, logf=None, env=None, timeout=3600):
    """Chạy lệnh, ghi vào log; lỗi → RuntimeError kèm đuôi output."""
    res = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                         env=env, timeout=timeout, stdin=subprocess.DEVNULL)
    if logf:
        logf.write("\n$ %s\n%s" % (" ".join(cmd) if isinstance(cmd, list) else cmd, res.stdout))
        logf.flush()
    if res.returncode != 0:
        raise RuntimeError("%s → mã %s: %s" % (cmd[0] if isinstance(cmd, list) else cmd, res.returncode,
                                                res.stdout.strip()[-400:]))
    return res.stdout


def remote(dcfg, command, logf=None, timeout=1800):
    env = dict(os.environ, SSHPASS=dcfg["password"])
    return sh(["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20",
               "%s@%s" % (dcfg["user"], dcfg["host"]), command], logf=logf, env=env, timeout=timeout)


def upload(dcfg, local, remote_path, logf=None):
    env = dict(os.environ, SSHPASS=dcfg["password"])
    sh(["sshpass", "-e", "scp", "-q", "-o", "StrictHostKeyChecking=accept-new", local,
        "%s@%s:%s" % (dcfg["user"], dcfg["host"], remote_path)], logf=logf, env=env)


def set_compose_image(dcfg, service, image, logf):
    """Backup docker-compose.yml rồi đổi dòng image: trong khối của service."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    remote(dcfg, "cd %s && cp docker-compose.yml docker-compose.yml.bak-%s-%s && "
                 "sed -i '/^  %s:/,/^  [a-z]/ s|^\\(\\s*image:\\s*\\).*|\\1%s|' docker-compose.yml && "
                 "sed -n '/^  %s:/,/image:/p' docker-compose.yml"
           % (dcfg["base"], stamp, service, service, image, service), logf)


def git_lines(repo, *args):
    return af.run(["git"] + list(args), cwd=repo, check=False).strip().splitlines()


def pending(acfg, dcfg):
    """Commit mới chưa deploy: {"FE": [(hash, subject)], "BE": [...]}, cùng mốc so sánh."""
    fe, be = acfg["fe_repo"], acfg["be_repo"]
    af.run(["git", "fetch", "-q", "origin", acfg["fe_upstream"]], cwd=fe)
    af.run(["git", "fetch", "-q", "origin", acfg["be_upstream"], dcfg["be_release_branch"]], cwd=be)
    dev = "origin/" + acfg["fe_upstream"]
    last = git_lines(fe, "log", "-1", "--format=%H", "-i", "--grep=update version", dev)
    rng = "%s..%s" % (last[0], dev) if last else dev
    fe_commits = [tuple(l.split(" ", 1)) for l in git_lines(fe, "log", "--no-merges", "--format=%H %s", rng)
                  if " " in l and "update version" not in l.lower()]
    be_commits = [tuple(l.split(" ", 1)) for l in git_lines(
        be, "log", "--no-merges", "--format=%H %s",
        "origin/%s..origin/%s" % (dcfg["be_release_branch"], acfg["be_upstream"])) if " " in l]
    return {"FE": fe_commits, "BE": be_commits}


def fresh_worktree(repo, path, ref):
    if os.path.isdir(path):
        subprocess.run(["git", "worktree", "remove", "--force", path], cwd=repo, check=False)
        shutil.rmtree(path, ignore_errors=True)
    af.run(["git", "worktree", "prune"], cwd=repo, check=False)
    af.run(["git", "worktree", "add", "-q", "--detach", path, ref], cwd=repo)


def deploy_fe(acfg, dcfg, root, logf):
    repo, dev = acfg["fe_repo"], acfg["fe_upstream"]
    work = os.path.join(root, "deploy-fe")
    fresh_worktree(repo, work, "origin/" + dev)
    try:
        os.symlink(os.path.join(repo, "node_modules"), os.path.join(work, "node_modules"))
        shutil.copy(os.path.join(repo, ".env.deploy"), os.path.join(work, ".env.deploy"))
        tar = os.path.join(work, "%s.tar" % dcfg["fe_image"])
        try:
            sh(["bash", "./build-and-deploy.sh", "dev"], cwd=work, logf=logf, env=af.build_env(acfg))
        except RuntimeError as ex:
            # Bước cuối của script là deploy sang host cũ (.168) bằng ssh key: lỗi ở đó không chặn deploy lên 86
            # nếu image tar + version đã tạo xong.
            if not os.path.exists(tar):
                raise
            log("FE: build-and-deploy.sh lỗi ở bước sau khi tạo image (%s) — vẫn deploy lên 86" % str(ex)[:200])
        with open(os.path.join(work, "build_dev", "version-tags.json"), encoding="utf-8") as f:
            version = json.load(f)["SS"]["dev"]["version"]
        af.run(["git", "add", "-A", "build_dev/version-tags.json", "releases"], cwd=work)
        af.run(["git", "commit", "-q", "-m", "update version %s" % version], cwd=work)
        af.push(work, dev)
        upload(dcfg, tar, "%s/images/%s.tar" % (dcfg["base"], dcfg["fe_image"]), logf)
        remote(dcfg, "cd %s && docker load -i images/%s.tar" % (dcfg["base"], dcfg["fe_image"]), logf)
        set_compose_image(dcfg, dcfg["fe_service"], "%s:%s" % (dcfg["fe_image"], version), logf)
        return version
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", work], cwd=repo, check=False)


def deploy_be(acfg, dcfg, root, logf):
    repo, release = acfg["be_repo"], dcfg["be_release_branch"]
    work = os.path.join(root, "deploy-be")
    fresh_worktree(repo, work, "origin/" + release)
    try:
        res = subprocess.run(["git", "merge", "-q", "-m", "merge %s vào %s" % (acfg["be_upstream"], release.split("/")[-1]),
                              "origin/" + acfg["be_upstream"]], cwd=work, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, universal_newlines=True)
        if res.returncode != 0:
            af.resolve_conflicts(acfg, work, "merge %s vào %s" % (acfg["be_upstream"], release))
        af.push(work, release)
        day = datetime.now().strftime("%Y%m%d")
        used = set(remote(dcfg, "docker images --format '{{.Tag}}' %s" % dcfg["be_image"]).split())
        tag = next(day + c for c in "abcdefghijklmnopqrstuvwxyz" if day + c not in used)
        image = "%s:%s" % (dcfg["be_image"], tag)
        # Nhớ image này build từ commit nào (deploy-check so commit sửa với đúng bản đang chạy).
        built = os.path.join(os.path.expanduser(acfg["root"]), "be-images.json")
        try:
            with open(built, encoding="utf-8") as f:
                images = json.load(f)
        except (OSError, ValueError):
            images = {}
        images[image] = af.run(["git", "rev-parse", "HEAD"], cwd=work).strip()
        with open(built, "w", encoding="utf-8") as f:
            json.dump(images, f, indent=1)
        module = os.path.join(work, acfg["be_subdir"])
        sh(["docker", "build", "-t", image, "-f", "docker/dev/Dockerfile", "."], cwd=module, logf=logf, timeout=5400)
        tar = os.path.join(root, "%s.tar" % dcfg["be_image"])
        sh(["docker", "save", "-o", tar, image], logf=logf)
        upload(dcfg, tar, "%s/images/%s.tar" % (dcfg["base"], dcfg["be_image"]), logf)
        os.remove(tar)
        remote(dcfg, "cd %s && docker load -i images/%s.tar" % (dcfg["base"], dcfg["be_image"]), logf)
        set_compose_image(dcfg, dcfg["be_service"], image, logf)
        return tag
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", work], cwd=repo, check=False)


def revive_be(dcfg, logf=None):
    """BE thoát (vd. lúc compose up Postgres chưa nhận kết nối → Liquibase lỗi, exit 1) thì start lại.
    Trả về True nếu đã start lại."""
    try:
        st = remote(dcfg, "docker inspect -f '{{.State.Status}}' %s" % dcfg["be_container"], timeout=60).strip()
    except (RuntimeError, subprocess.TimeoutExpired):
        return False
    if st not in ("exited", "dead", "created"):
        return False
    log("BE container %s đang %s → docker start lại" % (dcfg["be_container"], st))
    try:
        remote(dcfg, "docker start %s" % dcfg["be_container"], logf, timeout=120)
        return True
    except (RuntimeError, subprocess.TimeoutExpired) as ex:
        log("docker start %s lỗi: %s" % (dcfg["be_container"], ex))
        return False


def wait_up(dcfg, fe_version, minutes=None, revive=True):
    """Chờ FE trả đúng version (nếu deploy FE) và API trả lời (401 khi chưa đăng nhập = đã sống).
    BE thoát giữa chừng thì start lại (tối đa be_restart_tries lần)."""
    deadline = time.time() + (dcfg["wait_minutes"] if minutes is None else minutes) * 60
    tries, last_check = int(dcfg.get("be_restart_tries", 3)), 0
    while time.time() < deadline:
        if revive and tries > 0 and time.time() - last_check >= 30:
            last_check = time.time()
            if revive_be(dcfg):
                tries -= 1
        ok_fe = ok_api = False
        try:
            html = urllib.request.urlopen(dcfg["fe_url"] + "/", timeout=15).read().decode("utf-8", "replace")
            ok_fe = not fe_version or ('content="%s"' % fe_version) in html
        except OSError:
            pass
        try:
            urllib.request.urlopen(dcfg["api_url"] + "/api/v1/ping", timeout=15)
            ok_api = True
        except urllib.error.HTTPError as ex:
            ok_api = ex.code < 500
        except OSError:
            pass
        if ok_fe and ok_api:
            return True
        time.sleep(15)
    return False


def fingerprint(dcfg):
    """Dấu bản đang chạy: version FE (meta index.html) + image và thời điểm khởi động container BE."""
    fp = {"fe": None, "be": None, "be_started": None}
    try:
        html = urllib.request.urlopen(dcfg["fe_url"] + "/", timeout=20).read().decode("utf-8", "replace")
        m = re.search(r'<meta name="version" content="([^"]*)"', html)
        fp["fe"] = m.group(1) if m else None
    except OSError:
        pass
    try:
        out = remote(dcfg, "docker inspect -f '{{.Config.Image}}|{{.State.StartedAt}}' %s" % dcfg["be_container"],
                     timeout=60).strip()
        fp["be"], _, fp["be_started"] = out.partition("|")
    except (RuntimeError, subprocess.TimeoutExpired):
        pass
    return fp


def describe_change(old, new):
    parts = []
    if old.get("fe") != new.get("fe"):
        parts.append("FE %s → %s" % (old.get("fe"), new.get("fe")))
    if old.get("be") != new.get("be"):
        parts.append("BE %s → %s" % (old.get("be"), new.get("be")))
    elif old.get("be_started") != new.get("be_started"):
        parts.append("BE khởi động lại")
    return ", ".join(parts)


def api_alive(dcfg):
    try:
        urllib.request.urlopen(dcfg["api_url"] + "/api/v1/ping", timeout=15)
        return True
    except urllib.error.HTTPError as ex:
        return ex.code < 500
    except OSError:
        return False


def load_pending():
    try:
        with open(PENDING, encoding="utf-8") as f:
            return json.load(f).get("tickets", [])
    except (OSError, ValueError):
        return []


def save_pending(tickets, reason=""):
    os.makedirs(os.path.dirname(PENDING), exist_ok=True)
    with open(PENDING, "w", encoding="utf-8") as f:
        json.dump({"tickets": sorted(set(tickets)), "reason": reason,
                   "updated": datetime.now().isoformat(timespec="seconds")}, f, ensure_ascii=False, indent=1)


def fe_contains(acfg, fe_version, fe_commits):
    """Bản FE đang chạy (commit "update version X" trên dev-ss) đã chứa các commit sửa chưa. None = không xác định."""
    if not fe_version:
        return None
    repo = acfg["fe_repo"]
    tag = git_lines(repo, "log", "-1", "--format=%H", "-F", "--grep=update version %s" % fe_version,
                    "origin/" + acfg["fe_upstream"])
    if not tag:
        tag = git_lines(repo, "log", "-1", "--format=%H", "-F", "--grep=%s" % fe_version, "--grep=version",
                        "--all-match", "origin/" + acfg["fe_upstream"])
    if not tag:
        return None
    return all(subprocess.run(["git", "merge-base", "--is-ancestor", h, tag[0]], cwd=repo).returncode == 0
               for h in fe_commits)


def fail_count(display_id, delta=None):
    """Số lần QA server KHÔNG ĐẠT liên tiếp của ticket; delta=None đọc, 0 đặt lại, 1 tăng."""
    try:
        with open(FAIL_COUNT, encoding="utf-8") as f:
            counts = json.load(f)
    except (OSError, ValueError):
        counts = {}
    if delta is not None:
        counts[display_id] = 0 if delta == 0 else counts.get(display_id, 0) + delta
        with open(FAIL_COUNT, "w", encoding="utf-8") as f:
            json.dump(counts, f, indent=1)
    return counts.get(display_id, 0)


def deploy_check(acfg, dcfg, commits, path):
    """
    Ghi deploy-check.md cạnh qa.md: bản đang chạy trên server và từng commit sửa đã có trong bản đó chưa — để Claude
    kiểm chứng QA_FAIL dựa trên sự thật, không đoán "server chưa có bản sửa".
    """
    fp = fingerprint(dcfg)
    lines = ["# Bản đang chạy trên server 86 lúc QA", "",
             "- FE: `%s`" % fp.get("fe"), "- BE image: `%s` (container khởi động %s)" % (fp.get("be"), fp.get("be_started")),
             "", "## Commit sửa của ticket có trong bản đang chạy không", ""]
    release = "origin/" + dcfg["be_release_branch"]
    for label, repo, hashes in commits:
        for h in hashes:
            if label == "FE":
                inside = fe_contains(acfg, fp.get("fe"), [h])
                how = "so với commit `update version %s` trên %s" % (fp.get("fe"), acfg["fe_upstream"])
            else:
                try:
                    with open(os.path.join(os.path.expanduser(acfg["root"]), "be-images.json"), encoding="utf-8") as f:
                        src = json.load(f).get(fp.get("be") or "")
                except (OSError, ValueError):
                    src = None
                if src:
                    inside = subprocess.run(["git", "merge-base", "--is-ancestor", h, src], cwd=repo).returncode == 0
                    how = "image `%s` build từ commit `%s`" % (fp.get("be"), src[:9])
                else:
                    af.run(["git", "fetch", "-q", "origin", dcfg["be_release_branch"]], cwd=repo, check=False)
                    inside = None if subprocess.run(["git", "merge-base", "--is-ancestor", h, release],
                                                    cwd=repo).returncode == 0 else False
                    how = ("không rõ image `%s` build từ commit nào (không do deploy_qa build); nhánh %s %s commit này"
                           % (fp.get("be"), dcfg["be_release_branch"], "đã có" if inside is None else "CHƯA có"))
            word = "CÓ" if inside else ("KHÔNG RÕ" if inside is None else "CHƯA")
            lines.append("- %s `%s`: **%s** — %s" % (label, h[:9], word, how))
    lines += ["", "## Công cụ QA", "",
              "Gemini gọi API qua `soar_api.py`; mỗi kết quả request trong qa-rounds.jsonl bắt đầu bằng dòng mô tả đúng",
              "thứ đã gửi (vd `(công cụ đã gửi trường file: filename=\"…\", N byte)`). So request THỰC GỬI với ý định của",
              "QA: nếu công cụ gửi khác (tên / nội dung / tham số bị đổi) thì lỗi ở công cụ QA, không phải ở code."]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def handle_fail(api, acfg, dcfg, wp, display_id, qa_path, qa_text, commits=()):
    """QA KHÔNG ĐẠT: giao Claude kiểm chứng + sửa (chạy nền, xếp hàng chung autofix), hoặc UNREVIEWED nếu quá số lần."""
    try:
        deploy_check(acfg, dcfg, commits, os.path.join(os.path.dirname(qa_path), "deploy-check.md"))
    except Exception as ex:  # noqa: BLE001 — thiếu thông tin này vẫn giao Claude
        log("deploy-check lỗi: %s" % ex)
    n = fail_count(display_id, 1)
    if n <= dcfg["qa_fail_autofix_max"]:
        log_path = os.path.join(os.path.dirname(qa_path), "autofix.log")
        with open(log_path, "a", encoding="utf-8") as out:
            subprocess.Popen(["python3", AUTOFIX, "--wp", str(wp["id"]), "--event", "QA_FAIL", "--qa-report", qa_path],
                             stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        return " → Claude đang kiểm chứng + sửa (lần %d/%d)" % (n, dcfg["qa_fail_autofix_max"])
    reason = "## Cần quyết định\nQA tự động trên server test KHÔNG ĐẠT %d lần liên tiếp (autofix đã sửa lại %d lần). " \
             "Cần người xem.\n\n%s" % (n, n - 1, af.report_section(qa_text, "Kết quả")[:3000])
    af.mark_for_review(api, wp["id"], acfg["review_status_id"], reason, os.path.basename(os.path.dirname(qa_path)))
    return " → quá %d lần, đã chuyển UNREVIEWED" % dcfg["qa_fail_autofix_max"]


def qa_ticket(api, cfg, acfg, dcfg, me_id, display_id, commits, deployed, guard=None):
    """QA một ticket trên server. Trả (kết luận, đường dẫn qa.md)."""
    wp = api.get("/api/v3/work_packages/%s" % display_id)
    assignee = (wp["_links"].get("assignee") or {}).get("href", "")
    status_id = wp["_links"]["status"]["href"].rstrip("/").split("/")[-1]
    if not assignee.endswith("/%s" % me_id) or status_id != str(acfg["resolved_status_id"]):
        return "", ""
    root = os.path.expanduser(acfg["root"])
    qa_dir = os.path.join(root, "%s-qa-%s" % (display_id, datetime.now().strftime("%m%d-%H%M")))
    os.makedirs(qa_dir, exist_ok=True)
    wp = af.fetch_ticket(api, wp["id"], os.path.join(qa_dir, "ticket"))
    with open(os.path.join(qa_dir, "diff.md"), "w", encoding="utf-8") as f:
        for label, repo, hashes in commits:
            for h in hashes:
                f.write("# %s %s\n\n```diff\n%s\n```\n\n" % (label, h[:8], af.run(["git", "show", h], cwd=repo,
                                                                                   check=False)[:40000]))
    af.notify("[QA SERVER] %s" % display_id, "Đang test trên server 86 (%s): %s" % (
        dcfg["qa_engine"], wp.get("subject", "")))
    if dcfg["qa_engine"] == "gemini":
        gemini_qa(acfg, dcfg, qa_dir, display_id, deployed, guard)
    else:
        prompt = QA_PROMPT.format(display_id=display_id, subject=wp.get("subject", ""), deployed=deployed,
                                  qa_dir=qa_dir, soar_api=SOAR_API, fe_url=dcfg["fe_url"])
        with open(os.path.join(qa_dir, "prompt.md"), "w", encoding="utf-8") as f:
            f.write(prompt)
        scope = "/" + qa_dir + "/**"
        cmd = [acfg["claude"], "-p", prompt, "--permission-mode", "dontAsk",
               "--max-budget-usd", str(acfg["max_budget_usd"]), "--output-format", "stream-json", "--verbose",
               "--allowedTools"] + QA_TOOLS + ["Edit(%s)" % scope, "Write(%s)" % scope] + \
              ["--disallowedTools"] + af.DISALLOWED_TOOLS
        if acfg.get("model"):
            cmd += ["--model", acfg["model"]]
        with open(os.path.join(qa_dir, "claude.jsonl"), "w", encoding="utf-8") as out:
            subprocess.run(cmd, cwd=qa_dir, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           env=af.build_env(acfg))
    path = os.path.join(qa_dir, "qa.md")
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    m = re.search(r"QA:\s*(KHÔNG ĐẠT|ĐẠT|KHÔNG TỰ TEST ĐƯỢC)", text[:200])
    verdict = m.group(1) if m else "KHÔNG CÓ BÁO CÁO"
    # Server đổi trong lúc test → kết quả không đáng tin, không comment / không chuyển QA.
    change = guard() if guard else None
    if change:
        raise ServerChanged(change)
    if verdict == "ĐẠT":
        body = af.report_section(text, "Kết quả") or "Đã kiểm tra đạt."
        comment = "**[QA tự động] Đã deploy lên môi trường test (%s) và kiểm tra lại: ĐẠT** — chuyển %s.\n\n%s" % (
            deployed, dcfg["qa_user_name"], af.for_tester(body)[:3000])
        af.op_write(api, "POST", "/api/v3/work_packages/%s/activities" % wp["id"], {"comment": {"raw": comment}})
        lock_version = api.get("/api/v3/work_packages/%s" % wp["id"])["lockVersion"]
        af.op_write(api, "PATCH", "/api/v3/work_packages/%s" % wp["id"], {
            "lockVersion": lock_version, "_links": {"assignee": {"href": "/api/v3/users/%s" % dcfg["qa_user_id"]}}})
        verdict += " → đã chuyển " + dcfg["qa_user_name"]
        fail_count(display_id, 0)
    elif verdict == "KHÔNG ĐẠT":
        verdict += handle_fail(api, acfg, dcfg, wp, display_id, path, text, commits)
    report_dir = os.path.expanduser(acfg.get("report_dir") or "")
    if report_dir:
        os.makedirs(report_dir, exist_ok=True)
        shutil.copy(path, os.path.join(report_dir, os.path.basename(qa_dir) + ".md")) if text else None
    af.inbox(cfg, "**QA SERVER** %s — %s · %s" % (display_id, verdict, path))
    af.notify("[QA SERVER] %s: %s" % (display_id, verdict), wp.get("subject", ""))
    return verdict, path


QA_HISTORY = os.path.join(envconf.autofix_root(), "qa-history.json")


def resolved_mine(api, cfg, acfg):
    """Ticket đang Resolved và giao cho mình: ứng viên QA dù không còn trong hàng chờ / đợt deploy mới."""
    wps = api.my_work_packages(cfg["project"], [str(acfg["resolved_status_id"])])
    return sorted(w.get("displayId") or "WP-%s" % w["id"] for w in wps)


def be_contains(acfg, be_image, hashes):
    """Image BE đang chạy (build từ commit ghi trong be-images.json) đã chứa các commit sửa chưa. None = không rõ."""
    try:
        with open(os.path.join(os.path.expanduser(acfg["root"]), "be-images.json"), encoding="utf-8") as f:
            src = json.load(f).get(be_image or "")
    except (OSError, ValueError):
        src = None
    if not src:
        return None
    return all(subprocess.run(["git", "merge-base", "--is-ancestor", h, src], cwd=acfg["be_repo"]).returncode == 0
               for h in hashes)


def qa_history(display_id=None, build=None, verdict=None):
    """Kết quả QA gần nhất theo bản build (fe|be). Có build + verdict thì ghi; không thì trả bản ghi của ticket."""
    try:
        with open(QA_HISTORY, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if display_id and verdict is not None:
        data[display_id] = {"build": build, "verdict": verdict, "at": datetime.now().isoformat(timespec="seconds")}
        with open(QA_HISTORY + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(QA_HISTORY + ".tmp", QA_HISTORY)
    return data.get(display_id) if display_id else data


def run_all(dry_run=False, only=None):
    cfg, acfg, dcfg = load()
    root = os.path.expanduser(acfg["root"])
    os.makedirs(root, exist_ok=True)
    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    me_id = api.me()["id"]
    deployed_parts, commits = [], pending(acfg, dcfg)
    waiting = load_pending()
    auto = set()  # ticket lấy từ OpenProject (Resolved, giao cho mình) — chỉ QA khi bản đang chạy đã có bản sửa
    if only:
        tickets = list(only)
    else:
        new = sorted({t for items in commits.values() for _, s in items for t in re.findall(r"\[(SOAR-\d+)\]", s)})
        try:
            auto = set(resolved_mine(api, cfg, acfg)) - set(new) - set(waiting)
        except Exception as ex:  # noqa: BLE001 — không đọc được OpenProject thì vẫn QA như cũ
            log("không lấy được danh sách Resolved: %s" % ex)
        tickets = sorted(set(new) | set(waiting) | auto)
        log("chưa deploy: FE %d commit, BE %d commit · ticket mới: %s · hàng chờ QA: %s · Resolved chưa QA: %s" % (
            len(commits["FE"]), len(commits["BE"]), ", ".join(new) or "không có", ", ".join(waiting) or "trống",
            ", ".join(sorted(auto)) or "không có"))
        if dry_run or not tickets:
            return
    fe_version = None
    if not only and (commits["FE"] or commits["BE"]):
        logf = open(os.path.join(root, "deploy-%s.log" % datetime.now().strftime("%m%d-%H%M")), "w", encoding="utf-8")
        af.notify("[DEPLOY 86]", "Bắt đầu deploy: FE %d commit, BE %d commit" % (len(commits["FE"]), len(commits["BE"])))
        try:
            if commits["FE"]:
                fe_version = deploy_fe(acfg, dcfg, root, logf)
                deployed_parts.append("FE %s" % fe_version)
            if commits["BE"]:
                deployed_parts.append("BE %s:%s" % (dcfg["be_image"], deploy_be(acfg, dcfg, root, logf)))
            remote(dcfg, "cd %s && docker compose up -d --force-recreate" % dcfg["base"], logf)
        except Exception as ex:  # noqa: BLE001
            msg = "deploy lỗi: %s (log %s)" % (str(ex).splitlines()[0][:300], logf.name)
            log(msg)
            save_pending(tickets, "deploy lỗi")
            af.inbox(cfg, "**DEPLOY 86 LỖI** " + msg)
            af.notify("[DEPLOY 86 LỖI]", msg)
            return
        if not wait_up(dcfg, fe_version):
            msg = "server chưa lên sau %d phút (%s) — chưa QA" % (dcfg["wait_minutes"], ", ".join(deployed_parts))
            save_pending(tickets, "server chưa lên sau deploy")
            af.inbox(cfg, "**DEPLOY 86 LỖI** " + msg)
            af.notify("[DEPLOY 86 LỖI]", msg)
            return
        msg = "đã deploy %s · QA %d ticket" % (", ".join(deployed_parts), len(tickets))
        log(msg)
        af.inbox(cfg, "**DEPLOY 86** " + msg)
        af.notify("[DEPLOY 86]", msg)

    if not deployed_parts and not wait_up(dcfg, None, minutes=dcfg.get("alive_wait_minutes", 5)):
        msg = "server không trả lời (FE %s / API %s) — chưa QA, giữ hàng chờ" % (dcfg["fe_url"], dcfg["api_url"])
        log(msg)
        save_pending(tickets, "server không sống")
        af.inbox(cfg, "**DEPLOY 86 LỖI** " + msg)
        af.notify("[DEPLOY 86 LỖI]", msg)
        return
    baseline = fingerprint(dcfg)
    if not baseline.get("fe"):
        msg = "không đọc được version FE trên %s — chưa QA, giữ hàng chờ" % dcfg["fe_url"]
        log(msg)
        save_pending(tickets, "không đọc được version FE")
        af.inbox(cfg, "**DEPLOY 86 LỖI** " + msg)
        return
    deployed = ", ".join(deployed_parts) or "bản đang chạy: FE %s, BE %s" % (baseline["fe"], baseline["be"])
    log("dấu server: %s" % baseline)

    def guard():
        return describe_change(baseline, fingerprint(dcfg))

    lock = open(os.path.join(root, ".run.lock"), "w")
    remaining, done = list(tickets), set()
    open(QA_ACTIVE, "w").close()
    try:
        _qa_loop(api, cfg, acfg, dcfg, me_id, tickets, commits, deployed_parts, deployed, baseline, guard, lock,
                 waiting, remaining, done, auto=auto)
    finally:
        if os.path.exists(QA_ACTIVE):
            os.remove(QA_ACTIVE)


def _qa_loop(api, cfg, acfg, dcfg, me_id, tickets, commits, deployed_parts, deployed, baseline, guard, lock,
             waiting, remaining, done, auto=()):
    """QA các ticket, tối đa qa_parallel ticket cùng lúc (Gemini); server đổi giữa chừng thì dừng tất cả."""
    mu = threading.Lock()
    stop = {"msg": None}

    def one(display_id):
        if stop["msg"]:
            return
        hashes = [(label, repo, [h for h, s in commits[label] if "[%s]" % display_id in s] or
                   [l.split()[0] for l in git_lines(repo, "log", "--no-merges", "-F", "--grep=[%s]" % display_id,
                                                    "--format=%H", "origin/" + up)])
                  for label, repo, up in (("FE", acfg["fe_repo"], acfg["fe_upstream"]),
                                          ("BE", acfg["be_repo"], acfg["be_upstream"]))]
        hashes = [x for x in hashes if x[2]]
        fe_hashes = [h for label, _, hs in hashes if label == "FE" for h in hs]
        be_hashes = [h for label, _, hs in hashes if label == "BE" for h in hs]
        build = "%s|%s" % (baseline.get("fe"), baseline.get("be"))
        if display_id in auto:
            # Ticket Resolved lấy từ OpenProject: chỉ QA khi chắc bản đang chạy có bản sửa, và chưa QA trên bản này.
            def drop(why):
                log("%s: %s — chưa QA" % (display_id, why))
                with mu:
                    remaining.remove(display_id)
            if not hashes:
                return drop("không thấy commit [%s] trên %s / %s" % (display_id, acfg["fe_upstream"], acfg["be_upstream"]))
            if (fe_hashes and fe_contains(acfg, baseline["fe"], fe_hashes) is not True) or \
                    (be_hashes and be_contains(acfg, baseline["be"], be_hashes) is not True):
                return drop("bản đang chạy (FE %s, BE %s) chưa chắc có bản sửa" % (baseline["fe"], baseline["be"]))
            last = qa_history(display_id)
            if last and last.get("build") == build:
                return drop("đã QA trên bản này (%s)" % last.get("verdict"))
        elif not hashes:
            # Ticket trong hàng chờ nhưng commit sửa chưa vào dev-ss / develop-ss (vd. còn trên nhánh cá nhân):
            # server chắc chắn chưa có bản sửa → không QA, để lại hàng chờ tới đợt merge + deploy sau.
            log("%s: chưa thấy commit [%s] trên %s / %s — để lại hàng chờ"
                % (display_id, display_id, acfg["fe_upstream"], acfg["be_upstream"]))
            return
        elif fe_hashes and fe_contains(acfg, baseline["fe"], fe_hashes) is False:
            log("%s: bản FE %s trên server chưa có bản sửa — để lại hàng chờ" % (display_id, baseline["fe"]))
            return
        elif be_hashes and be_contains(acfg, baseline["be"], be_hashes) is False:
            log("%s: bản BE %s trên server chưa có bản sửa — để lại hàng chờ" % (display_id, baseline["be"]))
            return
        slot = af.acquire_slot(os.path.expanduser(acfg["root"]), acfg) if dcfg["qa_engine"] != "gemini" else None
        try:
            verdict, path = qa_ticket(api, cfg, acfg, dcfg, me_id, display_id, hashes, deployed, guard)
            log("%s: %s" % (display_id, verdict or "bỏ qua (không phải của mình / không ở Resolved)"))
            if verdict:
                qa_history(display_id, build, verdict)
        except ServerChanged as ex:
            with mu:
                if not stop["msg"]:
                    stop["msg"] = "%s (%s)" % (display_id, ex)
            return
        except Exception as ex:  # noqa: BLE001
            log("%s: QA lỗi %s" % (display_id, ex))
            af.inbox(cfg, "**QA SERVER LỖI** %s — %s" % (display_id, str(ex).splitlines()[0][:300]))
        finally:
            if slot:
                fcntl.flock(slot, fcntl.LOCK_UN)
        with mu:
            remaining.remove(display_id)
            done.add(display_id)
            save_pending(set(waiting) - done | set(remaining), "đang QA")

    workers = max(1, int(dcfg.get("qa_parallel") or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, list(tickets)))
    if stop["msg"]:
        msg = "server 86 thay đổi trong lúc QA %s — tạm dừng, %d ticket vào hàng chờ: %s" % (
            stop["msg"], len(remaining), ", ".join(remaining))
        log(msg)
        save_pending(set(waiting) - done | set(remaining), stop["msg"])
        af.inbox(cfg, "**QA SERVER TẠM DỪNG** " + msg)
        af.notify("[QA SERVER] tạm dừng", "Server 86 vừa bị cập nhật / restart — QA tiếp ở lần chạy sau")
        return
    save_pending(set(waiting) - done | set(remaining), "")

def main():
    parser = argparse.ArgumentParser(description="Deploy lên server test 86 rồi QA tự động từng ticket.")
    parser.add_argument("--dry-run", action="store_true", help="chỉ liệt kê commit / ticket chưa deploy")
    parser.add_argument("--qa", nargs="+", metavar="TICKET", help="chỉ QA các ticket này trên bản đang chạy")
    parser.add_argument("--resume", action="store_true",
                        help="QA tiếp hàng chờ (ticket dở do server bị cập nhật); có commit mới thì deploy trước")
    args = parser.parse_args()
    run_all(dry_run=args.dry_run, only=args.qa)


if __name__ == "__main__":
    main()
