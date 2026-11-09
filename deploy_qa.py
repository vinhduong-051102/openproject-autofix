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
4. Đưa image mới vào chạy theo DEPLOY_STRATEGY (mặc định bluegreen: container tạm <tên>-next chạy bản mới, kiểm tra
   sống, nginx trỏ sang, tạo lại container chính, trỏ về, xoá tạm — bản mới không lên thì giữ bản cũ; xem rollout()),
   chờ FE đúng version + API sống.
5. QA từng ticket (của mình, đang Resolved) có commit trong bản deploy: Claude test trên server qua soar_api.py.
   ĐẠT → comment bằng chứng + chuyển người phụ trách sang QA (QA_USER_NAME). KHÔNG ĐẠT / KHÔNG TỰ TEST ĐƯỢC → chỉ báo.
  KHÔNG RÕ YÊU CẦU → không test, comment điểm chưa rõ + câu hỏi, chuyển UNREVIEWED.

Cấu hình server + tài khoản test: biến DEPLOY_* / QA_* trong .env và/hoặc ~/.config/op-watch/deploy.json (chmod 600).
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
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
import projects as P  # noqa: E402

_PROJ = {}  # dự án đang deploy / QA (load ghi) — để ghi tên dự án vào thông báo khi có nhiều dự án


def _label(text):
    """"**DEPLOY 86** msg" → "**DEPLOY 86** [Dự án] msg" khi khai báo nhiều dự án (tag giữ nguyên cho trang theo dõi)."""
    if not _PROJ.get("multi") or not _PROJ.get("name"):
        return text
    m = re.match(r"^(\*\*[^*]+\*\*|\[[^\]]+\]) ?", text)
    return ("%s [%s] %s" % (m.group(1), _PROJ["name"], text[m.end():])) if m else "[%s] %s" % (_PROJ["name"], text)


def inbox(cfg, text):
    af.inbox(cfg, _label(text))


def notify(title, body):
    af.notify(_label(title), body)

DEPLOY_CONFIG = envconf.DEPLOY_CONFIG
SOAR_API = os.path.join(os.path.dirname(os.path.abspath(__file__)), "soar_api.py")
UI_CHECK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_check.py")
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
    # compose up lỗi (vd playbook chưa kịp healthy khi cả stack recreate → nginx/bootstrap chưa được start):
    # chờ rồi chạy lại "docker compose up -d" (không recreate) tối đa N lần.
    "compose_retry_tries": 3,
    "compose_retry_wait_seconds": 45,
    # QA bằng Gemini (agy, chỉ đọc): mỗi vòng Gemini trả JSON các request cần gọi, tool gọi qua soar_api.py rồi gửi
    # kết quả lại. "claude" = Claude headless tự gọi soar_api.py (tốn hạn mức Claude).
    "qa_engine": "gemini",
    "qa_rounds": 6,
    # tài khoản tester được phép dùng khi ticket ghi sẵn tên + mật khẩu (vd tài khoản tenancy con) — đặt trong
    # deploy.json "qa_ticket_accounts" hoặc QA_TICKET_ACCOUNTS (cách nhau dấu phẩy) trong .env
    "qa_ticket_accounts": [],
    # trạng thái OpenProject (id) của ticket giao cho mình cũng được QA lại trên server; ĐẠT → chuyển Resolved + người QA
    # (vd [98] = Review). Đặt trong deploy.json "qa_review_status_ids" hoặc QA_REVIEW_STATUSES=98 trong .env
    "qa_review_status_ids": [],
    "qa_model": "gemini-3.8-flash-medium",
    "qa_calls_per_round": 8,
    # Giới hạn MỖI lần gọi Gemini khi QA (một vòng bình thường ~1 phút). Hết giờ thì thử lại vòng đó 1 lần —
    # trước đây dùng chung GEMINI_TIMEOUT (900s) và treo là bỏ luôn ticket.
    "qa_call_timeout": 240,
    "be_container": "soar",
    "qa_parallel": 3,  # số ticket QA (Gemini) chạy cùng lúc trên server 86
    # Request gặp 502/503/504 / lỗi kết nối: chờ server lên lại tối đa chừng này phút rồi gọi lại.
    "server_retry_minutes": 10,
    # QA trên server KHÔNG ĐẠT → Claude (autofix) kiểm chứng báo cáo QA rồi sửa tiếp; quá số lần này vẫn không đạt
    # thì chuyển UNREVIEWED để người xem.
    "qa_fail_autofix_max": 2,
    # DELETE chỉ cho các đường dẫn khớp (dữ liệu QA tự tạo khi test, ví dụ huỷ báo cáo kết quả AI của cảnh báo).
    "qa_delete_allow": [r"^/api/v1/alerts/[^/?]+/ai-feedback/?$"],
    # ---- Cách đưa image mới vào chạy (rollout, xem README "Deploy không dừng trang") ----
    # bluegreen: dựng container tạm <container>-next chạy image mới → kiểm tra sống → nginx trỏ sang tạm → tạo lại
    #            container chính bằng image mới → nginx trỏ lại chính → xoá tạm. Lỗi trước khi chuyển: xoá tạm, trả
    #            image cũ vào docker-compose.yml, trang giữ bản cũ. Server chưa tách upstream nginx → tự dùng "service".
    # service:   chỉ tạo lại service đổi image (--no-deps) rồi reload nginx — trang dừng vài chục giây / ~1 phút (BE).
    # recreate:  cách cũ, docker compose up -d --force-recreate CẢ stack (dễ sập cả trang nếu playbook unhealthy).
    "deploy_strategy": "bluegreen",
    "bg_parts": ["fe", "be"],          # phần nào dùng bluegreen (phần còn lại dùng service)
    "nginx_container": "soar-https-nginx",
    "nginx_service": "soar-https-nginx",
    "fe_container": "soar-fe",         # container_name của fe_service (BE: be_container)
    "bg_fe_upstream": "soar_frontend_4443",
    "bg_be_upstream": "soar_backend_4443",
    # File upstream riêng trong thư mục mount vào /etc/nginx/conf.d (đường dẫn tương đối DEPLOY_BASE); deploy ghi đè.
    "bg_fe_upstream_file": "nginx-config/op-watch-upstream-fe.conf",
    "bg_be_upstream_file": "nginx-config/op-watch-upstream-be.conf",
    "bg_fe_port": 3000,
    "bg_be_port": 8888,
    "bg_next_suffix": "-next",
    "bg_health_minutes": 10,           # chờ container tạm / chính sống tối đa N phút
    "bg_drain_seconds": 15,            # sau khi trỏ nginx về chính: chờ request dở trên container tạm xong rồi mới xoá
    "bg_be_health_path": "/actuator/health",
    # BE tạm nhận thêm alias mạng = be_container ("soar") để service nội bộ (playbook gọi http://soar:8888) không lỗi
    # trong lúc container chính tạo lại.
    "bg_be_aliases": True,
    # BE có migration Liquibase phá cấu trúc (drop / rename cột, bảng…): fallback = dùng "service" cho BE (dừng bản
    # cũ trước khi bản mới migrate); ignore = vẫn bluegreen (bản cũ có thể lỗi SQL vài phút trong lúc chạy song song).
    "bg_be_destructive": "fallback",
}

# Biến .env cho phần rollout (đọc thẳng ở đây, không cần sửa envconf.DEPLOY_ENV).
ROLLOUT_ENV = {
    "DEPLOY_STRATEGY": ("deploy_strategy", "str"),
    "DEPLOY_BLUEGREEN_PARTS": ("bg_parts", "list"),
    "DEPLOY_NGINX_CONTAINER": ("nginx_container", "str"),
    "DEPLOY_NGINX_SERVICE": ("nginx_service", "str"),
    "DEPLOY_FE_CONTAINER": ("fe_container", "str"),
    "DEPLOY_BG_FE_UPSTREAM": ("bg_fe_upstream", "str"),
    "DEPLOY_BG_BE_UPSTREAM": ("bg_be_upstream", "str"),
    "DEPLOY_BG_FE_UPSTREAM_FILE": ("bg_fe_upstream_file", "str"),
    "DEPLOY_BG_BE_UPSTREAM_FILE": ("bg_be_upstream_file", "str"),
    "DEPLOY_BG_FE_PORT": ("bg_fe_port", "int"),
    "DEPLOY_BG_BE_PORT": ("bg_be_port", "int"),
    "DEPLOY_BG_NEXT_SUFFIX": ("bg_next_suffix", "str"),
    "DEPLOY_BG_HEALTH_MINUTES": ("bg_health_minutes", "int"),
    "DEPLOY_BG_DRAIN_SECONDS": ("bg_drain_seconds", "int"),
    "DEPLOY_BG_BE_HEALTH_PATH": ("bg_be_health_path", "str"),
    "DEPLOY_BG_BE_ALIASES": ("bg_be_aliases", "bool"),
    "DEPLOY_BG_BE_DESTRUCTIVE": ("bg_be_destructive", "str"),
}
STRATEGIES = ("bluegreen", "service", "recreate")

FAIL_COUNT = os.path.join(envconf.autofix_root(), "qa-fail-count.json")
# Rule rút ra từ các lần Claude kết luận QA SAI (op_autofix.report_qa_wrong ghi thêm); đưa vào prompt mọi lần QA sau.
QA_RULES = os.path.join(envconf.autofix_root(), "qa-rules.md")
QA_TOOL_ERROR = "(lỗi công cụ QA — QA lại lượt sau)"


def load_qa_rules(limit=6000):
    """Nội dung qa-rules.md (giữ phần mới nhất nếu quá dài) để chèn vào prompt QA."""
    try:
        with open(QA_RULES, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return "(chưa có)"
    if len(text) > limit:
        text = "…\n" + text[-limit:].split("\n", 1)[-1]
    return text or "(chưa có)"


REPEAT_RX = re.compile(r"\{\{rep:(.{1,20}?):(\d{1,6})\}\}", re.S)


def expand_repeat(text, notes):
    """{{rep:<chuỗi>:<số lần>}} → chuỗi lặp đúng số lần (model tự gõ chuỗi dài hay đếm sai). Ghi chú độ dài vào notes."""
    if not text or "{{rep:" not in text:
        return text

    def sub(m):
        n = min(int(m.group(2)), 200000)
        out = m.group(1) * n
        notes.append("{{rep:%s:%s}} → %d ký tự" % (m.group(1), m.group(2), len(out)))
        return out
    return REPEAT_RX.sub(sub, text)
# Có file này = đang QA trên server: đợt test lại bằng Gemini (op_autofix --review) chờ, nhường agy cho QA.
QA_ACTIVE = os.path.join(envconf.autofix_root(), ".qa-active")
AUTOFIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "op_autofix.py")

PENDING = os.path.join(envconf.autofix_root(), "qa-pending.json")
# Log docker compose (deploy server) ghi từng dòng trong lúc chạy để dashboard theo dõi trực tiếp.
COMPOSE_LIVE = os.path.join(envconf.autofix_root(), ".compose-live.log")
COMPOSE_STATE = os.path.join(envconf.autofix_root(), ".compose-live.json")
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
Giao diện thật (trình duyệt, tự đăng nhập tài khoản trong ticket): ghi bước JSON vào {qa_dir}/ui/<tên>.json rồi
`python3 {ui_check} {qa_dir}/ui/<tên>.json` — hướng dẫn bước ở đầu file {ui_check}; ảnh ra {qa_dir}/ui/<tên>-shots/
(xem bằng Read), chụp có khoanh đỏ vùng cần chú ý: {{"screenshot": "ten", "mark": ["css"], "note": "…"}}.

## Làm
1. Tái hiện đúng các bước trong ticket (Reopened thì theo comment tester gần nhất), so với kết quả mong đợi.
   Thử thêm 1-2 trường hợp lân cận dễ hỏng.
2. Bug chỉ thấy trên giao diện (màu, focus, nhãn trình đọc màn hình, bố cục…): kiểm tra bundle FE đã deploy
   (index.html → file /assets/*.js liên quan) có đúng thay đổi trong diff không. Không đủ chắc → KHÔNG TỰ TEST ĐƯỢC.
3. AN TOÀN — môi trường dùng chung với tester: không xoá / sửa dữ liệu không do mình tạo; dữ liệu test đặt tên bắt đầu
   `QA_AUTO_` và dọn lại nếu API cho phép; đổi cấu hình hệ thống thì trả lại giá trị cũ ngay sau khi test.
   Không gọi host khác. Tài khoản: mặc định của soar_api; tài khoản test QA_AUTO_* tự tạo; hoặc tài khoản tester ghi
   trong ticket nếu tên thuộc [{ticket_accounts}] (`--as <tên> --password '<mật khẩu trong ticket>'` lần đầu) — KHÔNG
   đổi mật khẩu / thông tin / trạng thái của tài khoản tester, chỉ sửa / xoá dữ liệu do mình tạo.

4. Ticket KHÔNG RÕ RÀNG — đọc cả mô tả, comment, diff mà vẫn không xác định được cần kiểm tra gì: không có bước tái hiện
   lẫn kết quả mong đợi, mô tả mâu thuẫn với comment, hoặc không biết màn hình / API / dữ liệu nào → KHÔNG test (không
   gọi request), kết luận KHÔNG RÕ YÊU CẦU; phần kết quả nêu cụ thể điểm nào chưa rõ và 1-3 câu hỏi cần tester / BA trả lời.
   Chỉ dùng khi thật sự không suy ra được; thiếu vài chi tiết nhưng vẫn kiểm tra được theo diff + ticket thì vẫn test.

## Báo cáo (bắt buộc)
Ghi {qa_dir}/qa.md bằng tiếng Việt. Dòng đầu đúng một trong: `QA: ĐẠT` | `QA: KHÔNG ĐẠT` | `QA: KHÔNG TỰ TEST ĐƯỢC` |
`QA: KHÔNG RÕ YÊU CẦU`
## Kết quả — ngắn gọn cho tester: đã test gì, trên bản nào, kết quả (dùng làm comment ticket)
## Bằng chứng — từng request → mã HTTP + phần response quan trọng
## Ghi chú — dữ liệu đã tạo / đã dọn, điều chưa kiểm tra được
## Ảnh minh chứng — mỗi ảnh một dòng `- SAU: <đường dẫn ảnh> — <chú thích cho tester>` (ảnh chụp server, có khoanh
vùng) và `- TRƯỚC: {qa_dir}/ticket/images/<id>.png — <chú thích>` (ảnh gốc của tester, nếu có); tối đa 6, không có thì bỏ.
"""

GEMINI_QA_PROMPT = """KHÔNG dùng bất kỳ công cụ nào (không chạy lệnh, không đọc file, không gọi mạng). Một chương trình khác gọi
API giúp bạn và gửi lại kết quả ở vòng sau. Việc của bạn chỉ là trả về JSON đúng schema.

Bạn là QA, kiểm tra lại bug {display_id} trên MÔI TRƯỜNG TEST THẬT (server 86) sau khi bản sửa đã deploy ({deployed}).
Làm ĐÚNG như mô tả trong ticket: tái hiện các bước tái hiện (ticket Reopened thì theo comment tester gần nhất) và so với
KẾT QUẢ MONG ĐỢI của ticket. Không mở rộng sang việc khác ngoài ticket.

Gọi API: mỗi phần tử `calls` gồm
- method: GET | POST | PUT | PATCH | UPLOAD (multipart) | SEARCH | UI | DELETE (chỉ cho: {delete_allow})
- UI: thao tác GIAO DIỆN thật bằng trình duyệt (tự đăng nhập tài khoản ghi trong ticket; không có thì tài khoản QA,
  hoặc account=<tên> như API). target = "/" ; body = chuỗi JSON mảng các bước:
  {{"goto": "/duong-dan"}}, {{"click": "text=Lưu"}} (selector Playwright: text=…, css, role=button[name="…"]),
  {{"fill": ["css", "chữ"]}}, {{"press": "Enter"}}, {{"select": ["css", "giá trị"]}}, {{"hover": "css"}},
  {{"wait": 1500}} | {{"wait": "text=Đã lưu"}}, {{"text": "css"}} (đọc chữ), {{"style": ["css", ["color", "font-size"]]}}
  (CSS tính toán), {{"lang": "en"}}, {{"screenshot": "ten", "mark": ["css vùng cần chú ý"], "note": "nhãn ngắn"}}
  (chụp + khoanh đỏ vùng cần chú ý). Màn hình đăng nhập / trang công khai: body = {{"login": false, "steps": [...]}}.
  Kết quả trả về: từng bước OK / LỖI, chữ / CSS đọc được, đường dẫn ảnh. Bạn KHÔNG xem được ảnh: kết luận dựa trên
  chữ / CSS đọc được; ảnh dùng làm minh chứng cho tester. Nút xoá / đăng xuất bị chặn. Tối đa 2 request UI mỗi vòng.
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
  một tài khoản test QA_AUTO_* vừa tạo, hoặc tài khoản tester ghi trong ticket (sau đó tool nhớ, chỉ cần account)
- why: gọi để kiểm tra gì
- parallel: true trên các request của CÙNG một vòng → các request đó được gửi đồng thời (test tranh chấp / gửi trùng)
Tối đa {max_calls} request mỗi vòng; response dài bị cắt.
CHUỖI DÀI / LẶP (test giới hạn độ dài…): KHÔNG tự gõ chuỗi dài (dễ đếm sai). Viết `{{{{rep:<chuỗi>:<số lần>}}}}` trong
body / target / file_name / file_content, công cụ thay bằng chuỗi lặp đúng số lần và ghi lại độ dài thực gửi. Ví dụ tên
256 ký tự: `"name":"{{{{rep:A:256}}}}"`; "QA_AUTO_" + 248 ký tự: `"QA_AUTO_{{{{rep:A:248}}}}"`.

Tài khoản: mặc định QA_superadmin (FULL quyền).
TÀI KHOẢN TESTER TRONG TICKET — ƯU TIÊN: ticket ghi sẵn tài khoản tester (tên + mật khẩu, kể cả mật khẩu chỉ ghi ở
ticket/comment khác cùng tài khoản) và tên đó thuộc danh sách được phép [{ticket_accounts}] → gọi thẳng bằng tài khoản
đó: account="<tên>", password="<mật khẩu trong ticket>" ở lần gọi đầu. Đặc biệt bug cách ly tenancy (tenancy con không
tạo được tài khoản QA_AUTO_* đăng nhập được). Tài khoản tester dùng chung với người thật: KHÔNG đổi mật khẩu, thông tin,
quyền, trạng thái của chính các tài khoản đó; chỉ sửa / xoá dữ liệu do mình tạo trong lần test (tên bắt đầu QA_).
Mật khẩu sai / tài khoản không có trong danh sách → dùng cách tạo tài khoản QA_AUTO_* bên dưới.
Bug về phân quyền / phạm vi dữ liệu / loại tài khoản (khách hàng, phòng ban khác, thiếu quyền…) mà ticket không có tài
khoản dùng được thì TỰ TẠO tài khoản test phù hợp bằng QA_superadmin rồi gọi lại bằng tài khoản đó:
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

Lỗi thấy trên giao diện (màu, vị trí, chữ, icon, focus…): ƯU TIÊN dùng UI làm đúng các bước của ticket, đọc chữ / CSS
của vùng liên quan và chụp ảnh có khoanh vùng đó; kết hợp SEARCH tìm chuỗi đặc trưng của diff trong mã FE đang chạy.
Đúng kết quả mong đợi → ĐẠT; sai → KHÔNG ĐẠT; chỉ khi không làm được cả UI lẫn SEARCH mới KHÔNG TỰ TEST ĐƯỢC.

ẢNH MINH CHỨNG (shots, khi kết thúc; tối đa 6): ảnh chụp từ request UI (đường dẫn trong kết quả) với kind "SAU" và
note ngắn cho tester (vùng khoanh đỏ là gì, đúng / sai thế nào); kèm ảnh gốc của tester trong ticket (dạng
`ticket/images/<id>.png`, ảnh `images/<id>.png` trong mô tả / comment) với kind "TRƯỚC" nếu có. Không có ảnh thì bỏ trống.

Ticket KHÔNG RÕ RÀNG — đọc cả mô tả, comment, diff mà vẫn không xác định được cần kiểm tra gì: không có bước tái hiện
lẫn kết quả mong đợi, mô tả mâu thuẫn với comment, hoặc không biết màn hình / API / dữ liệu nào → KHÔNG test (không
gọi request), kết luận KHÔNG RÕ YÊU CẦU; phần kết quả nêu cụ thể điểm nào chưa rõ và 1-3 câu hỏi cần tester / BA trả lời.
Chỉ dùng khi thật sự không suy ra được; thiếu vài chi tiết nhưng vẫn kiểm tra được theo diff + ticket thì vẫn test.

Kết thúc (done=true) khi đã đủ bằng chứng, với verdict ĐẠT | KHÔNG ĐẠT | KHÔNG TỰ TEST ĐƯỢC | KHÔNG RÕ YÊU CẦU và
result (ngắn gọn cho tester: đã test gì, kết quả), evidence (request → mã HTTP + phần response quan trọng),
notes (dữ liệu đã tạo / chưa kiểm tra được). Chưa xong: done=false, verdict=CHƯA, calls = các request tiếp theo.

# RULE TỪ CÁC LẦN QA SAI TRƯỚC (BẮT BUỘC TUÂN THEO)
{qa_rules}

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
        "verdict": {"type": "string", "enum": ["ĐẠT", "KHÔNG ĐẠT", "KHÔNG TỰ TEST ĐƯỢC", "KHÔNG RÕ YÊU CẦU", "CHƯA"]},
        "result": {"type": "string"}, "evidence": {"type": "string"}, "notes": {"type": "string"},
        "shots": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"}, "kind": {"type": "string", "enum": ["TRƯỚC", "SAU"]},
            "note": {"type": "string"}}, "required": ["path", "kind"]}},
    },
    "required": ["done", "verdict"],
}


def clean_output(data):
    """Output của soar_api.py (bytes) → chuỗi an toàn để đưa vào prompt Gemini: bỏ ký tự điều khiển / null
    (null làm lệnh gọi Gemini lỗi "embedded null byte"); body nhị phân (vd file xlsx export) → chỉ ghi kích thước."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")
    head, sep, body = text.partition("\n\n")
    if not sep:
        head, body = "", text
    bad = sum(1 for c in body if c == "\ufffd" or (ord(c) < 32 and c not in "\n\r\t"))
    if body and ("\x00" in body or bad > max(8, len(body) // 20)):
        body = "[nội dung nhị phân ~%d byte — đã lược, không đưa vào prompt; bắt đầu bằng %r]" % (
            len(body.encode("utf-8", "replace")), body[:8].encode("utf-8", "replace")[:8])
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", head + sep + body)


def ui_call(qa_dir, call):
    """Request UI của QA: các bước giao diện (JSON) → ui_check.py (trình duyệt thật). Trả kết quả (đã cắt)."""
    try:
        steps = json.loads(call.get("body") or "[]")
    except ValueError as ex:
        return "UI: body không phải JSON hợp lệ (%s)" % ex
    ui_dir = os.path.join(qa_dir, "ui")
    os.makedirs(ui_dir, exist_ok=True)
    path = os.path.join(ui_dir, "ui-%d.json" % (len([f for f in os.listdir(ui_dir) if f.endswith(".json")]) + 1))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(steps, f, ensure_ascii=False, indent=1)
    cmd = ["python3", UI_CHECK, path, "--ticket", os.path.join(qa_dir, "ticket", "ticket.md")]
    if call.get("account"):
        cmd += ["--as", call["account"]] + (["--password", call["password"]] if call.get("password") else [])
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=420,
                             stdin=subprocess.DEVNULL, universal_newlines=True, errors="replace").stdout
    except subprocess.TimeoutExpired:
        return "UI: HẾT THỜI GIAN (420s)"
    out = out.replace(qa_dir + "/", "")  # đường dẫn ảnh tương đối thư mục QA (dùng lại trong shots)
    return out if len(out) <= 4000 else out[:4000] + "\n… (cắt, tổng %d ký tự)" % len(out)


def soar_call(qa_dir, call, dcfg=None):
    """Một request Gemini yêu cầu → chạy soar_api.py. Trả chuỗi kết quả (đã cắt)."""
    method = (call.get("method") or "GET").upper()
    if method == "SEARCH" and dcfg:
        return bundle_search(dcfg, call.get("target", ""))
    if method == "UI":
        return ui_call(qa_dir, call)
    if method == "DELETE":
        if not dcfg or not any(re.search(rx, call.get("target", "")) for rx in dcfg.get("qa_delete_allow", [])):
            return "BỊ CHẶN: DELETE chỉ cho đường dẫn khớp %s" % (dcfg or {}).get("qa_delete_allow")
    elif method not in ("GET", "POST", "PUT", "PATCH", "UPLOAD"):
        return "BỊ CHẶN: chỉ cho GET / POST / PUT / PATCH / UPLOAD / SEARCH / UI / DELETE (giới hạn)"
    rep_notes = []
    call = dict(call)
    for k in ("target", "body", "file_name", "file_content"):
        if isinstance(call.get(k), str):
            call[k] = expand_repeat(call[k], rep_notes)
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
            return clean_output(subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                               timeout=120, stdin=subprocess.DEVNULL).stdout)
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
    out = ("(công cụ đã mở rộng: %s)\n" % "; ".join(rep_notes) if rep_notes else "") + sent_note + out
    return out if len(out) <= 4000 else out[:4000] + "\n… (cắt, tổng %d ký tự)" % len(out)


def fe_bundle(dcfg):
    """
    Tải index.html + mọi asset JS/CSS của FE đang chạy (theo import trong code), lưu đệm theo version + dấu index.html.
    Hai bản build khác nhau có thể trùng số version (người khác deploy tay mà không push version-tags.json): tên asset
    có hash nội dung nên index.html khác nhau → không dùng nhầm bộ đệm của bản cũ.
    """
    html = urllib.request.urlopen(dcfg["fe_url"] + "/", timeout=30).read().decode("utf-8", "replace")
    m = re.search(r'<meta name="version" content="([^"]*)"', html)
    cache = os.path.join(envconf.autofix_root(), "fe-bundle-%s-%s" % (
        m.group(1) if m else "unknown", hashlib.sha1(html.encode("utf-8")).hexdigest()[:8]))
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
    if not diff.strip():
        diff = "(Không có commit sửa nào gắn mã ticket — QA kiểm tra hành vi hiện tại trên server theo mô tả + comment của ticket: đã đúng kết quả mong đợi → ĐẠT, chưa đúng → KHÔNG ĐẠT.)"
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
            ticket_accounts=", ".join(dcfg.get("qa_ticket_accounts") or []) or "trống — không dùng",
            round=rnd, rounds=rounds, ticket=ticket, diff=diff, qa_rules=load_qa_rules(),
            last_round=" ĐÂY LÀ VÒNG CUỐI: phải done=true và kết luận." if last else "",
            transcript="\n\n".join(transcript)[-60000:] or "(chưa gọi gì)")
        prompt = prompt.replace("\x00", "")
        call_timeout = max(60, int(dcfg.get("qa_call_timeout") or 240))
        cmd = [os.path.expanduser(acfg["gemini_cmd"]), "-p", prompt, "--mode", "plan", "--output-format", "json",
               "--json-schema", schema, "--print-timeout", "%ds" % (call_timeout - 15)]
        if dcfg.get("qa_model") or acfg.get("gemini_model"):
            cmd += ["--model", dcfg.get("qa_model") or acfg["gemini_model"]]
        answer, diag = {}, []
        for attempt in (1, 2):
            # Gemini trả rỗng / JSON hỏng (thường do lỗi tạm của CLI / hạn mức): ghi stderr rồi thử lại vòng đó 1 lần.
            try:
                res = subprocess.run(cmd, cwd=qa_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     universal_newlines=True, errors="replace", timeout=call_timeout,
                                     stdin=subprocess.DEVNULL)
                line = [l for l in res.stdout.splitlines() if l.startswith("{")]
                try:
                    answer = (json.loads(line[-1]).get("structured_output") or {}) if line else {}
                except ValueError:
                    answer = {}
                if not answer:
                    diag.append({"attempt": attempt, "rc": res.returncode, "stderr": res.stderr[-1500:],
                                 "stdout_tail": res.stdout[-500:]})
            except subprocess.TimeoutExpired:
                # Gemini treo (không trả gì): thử lại vòng này 1 lần; vẫn treo thì là lỗi công cụ (QA lại lượt sau).
                diag.append({"attempt": attempt, "error": "Gemini không trả lời sau %ds" % call_timeout})
                if attempt == 1:
                    log("%s: Gemini không trả lời vòng %d sau %ds → thử lại" % (display_id, rnd, call_timeout))
                    continue
                answer = {"done": True, "verdict": "KHÔNG TỰ TEST ĐƯỢC",
                          "result": "Gemini lỗi: không trả lời sau %ds (vòng %d, đã thử lại)" % (call_timeout, rnd)}
                break
            except (OSError, ValueError) as ex:
                diag.append({"attempt": attempt, "error": str(ex)[:300]})
                answer = {"done": True, "verdict": "KHÔNG TỰ TEST ĐƯỢC", "result": "Gemini lỗi: %s" % ex}
                break
            if answer:
                break
            if attempt == 1:
                log("%s: Gemini không trả kết quả vòng %d (mã %s) → thử lại" % (display_id, rnd, diag[-1].get("rc")))
                time.sleep(20)
        with open(os.path.join(qa_dir, "gemini-rounds.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"round": rnd, "answer": answer, "diag": diag} if diag else
                               {"round": rnd, "answer": answer}, ensure_ascii=False) + "\n")
        if not answer:
            err = next((d.get("stderr") or d.get("error") for d in reversed(diag) if d.get("stderr") or d.get("error")), "")
            answer = {"done": True, "verdict": "KHÔNG TỰ TEST ĐƯỢC",
                      "result": "Gemini không trả kết quả (vòng %d, đã thử lại)%s" % (
                          rnd, ": " + err.strip().splitlines()[-1][:300] if err.strip() else "")}
        if answer.get("done") or not answer.get("calls"):
            break
        calls, n_ui = [], 0
        for c in answer["calls"][:dcfg["qa_calls_per_round"]]:
            if str(c.get("method") or "").upper() == "UI":
                n_ui += 1
                if n_ui > 2:
                    continue
                c = dict(c, parallel=False)  # trình duyệt: chạy lần lượt
            calls.append(c)
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
    verdict = answer.get("verdict") if answer.get("verdict") in ("ĐẠT", "KHÔNG ĐẠT", "KHÔNG TỰ TEST ĐƯỢC",
                                                                 "KHÔNG RÕ YÊU CẦU") \
        else "KHÔNG TỰ TEST ĐƯỢC"
    shots = []
    for sh in (answer.get("shots") or [])[:6]:
        p = str(sh.get("path") or "").strip().strip("`")
        if p and os.path.isfile(p if os.path.isabs(p) else os.path.join(qa_dir, p)):
            shots.append("- %s: %s — %s" % ("TRƯỚC" if sh.get("kind") == "TRƯỚC" else "SAU", p,
                                             str(sh.get("note") or "").replace("\n", " ")[:300]))
    text = "QA: %s\n\n_QA bằng Gemini (%s), %d request_\n\n## Kết quả\n%s\n\n## Bằng chứng\n%s\n\n## Ghi chú\n%s\n\n" \
           "%s## Toàn bộ request\n%s\n" % (
               verdict, dcfg.get("qa_model") or acfg.get("gemini_model") or "agy", len(transcript), answer.get("result", ""),
               answer.get("evidence", ""), answer.get("notes", ""),
               ("## Ảnh minh chứng\n%s\n\n" % "\n".join(shots)) if shots else "", "\n\n".join(transcript))
    with open(os.path.join(qa_dir, "qa.md"), "w", encoding="utf-8") as f:
        f.write(text)
    return text


QA_TOOLS = ["Read", "Glob", "Grep", "Bash(python3 %s*)" % SOAR_API, "Bash(python3 %s *)" % UI_CHECK, "Bash(ls *)", "Bash(head *)", "Bash(tail *)",
            "Bash(grep *)", "Bash(wc *)", "Bash(cat *)"]


def log(msg):
    print("[%s] [deploy-qa] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def load_all():
    cfg = op_watch.load_config(argparse.Namespace(config=None, interval=None, on_change=None, no_notify=False))
    return cfg, P.projects(cfg)


def load(proj=None):
    """(cfg, acfg, dcfg) theo dự án `proj` (mặc định dự án đầu): repo, server test, tài khoản QA của dự án đó."""
    cfg, items = load_all()
    if isinstance(proj, str):
        proj = P.get(cfg, proj)
    proj = proj or items[0]
    acfg = dict(af.AUTOFIX_DEFAULTS)
    acfg.update(cfg.get("autofix") or {})
    cfg, acfg = P.scope(cfg, acfg, proj)
    os.environ["OP_PROJECT_KEY"] = proj["key"]  # soar_api / ui_check (tiến trình con) dùng server của dự án
    _PROJ.update(name=proj["name"], key=proj["key"], multi=len(items) > 1)
    deploy_repos(acfg)
    dcfg = dict(DEFAULTS)
    dcfg.update(envconf.overlay(envconf.deploy_config(proj["key"]), ROLLOUT_ENV))
    return cfg, acfg, dcfg


def deploy_targets(only_tickets=None, project=None):
    """Dự án tham gia deploy / QA: có repo deploy khác "none" hoặc bật QA. only_tickets → chỉ dự án của các ticket đó.
    Trả [(dự án, [ticket của dự án] hoặc None)]."""
    cfg, items = load_all()
    if project:
        items = [p for p in items if project in (p["key"], p["prefix"])]
        if not items:
            raise SystemExit("không có dự án %s" % project)
    if only_tickets:
        groups = {}
        for t in only_tickets:
            groups.setdefault(P.by_ticket(cfg, t)["key"], []).append(t)
        return [(p, groups[p["key"]]) for p in items if p["key"] in groups]
    import repos as R
    out = []
    for p in items:
        rs = R.repos(P.scope_acfg(cfg.get("autofix") or {}, p)) if (p["repos"] or p["_legacy"]) else []
        if p["qa"] or any(r["deploy"] != "none" for r in rs):
            out.append((p, None))
    return out


def qa_enabled(acfg, dcfg):
    """Dự án có QA tự động trên server test không (bật qa + có fe_url / api_url)."""
    return bool(P.current(acfg) and P.current(acfg)["qa"] and dcfg.get("fe_url") and dcfg.get("api_url"))


def deploy_repos(acfg):
    """Deploy theo KIỂU, không theo tên repo: repo có "deploy": "fe-bundle" → phần FE (build bundle tĩnh), "be-image" →
    phần BE (build Docker image từ release branch). Ghi vào các khoá fe_* / be_* để logic deploy + QA dùng như cũ;
    không có repo cho một kiểu thì khoá repo = None (bỏ qua phần đó). Repo "deploy": "none" không tham gia deploy."""
    import repos as R
    for prefix, kind in (("fe", "fe-bundle"), ("be", "be-image")):
        r = next((x for x in R.repos(acfg) if x["deploy"] == kind), None)
        acfg[prefix + "_repo"] = r["path"] if r else None
        acfg[prefix + "_branch"] = r["branch"] if r else ""
        acfg[prefix + "_upstream"] = r["upstream"] if r else ""
        acfg[prefix + "_label"] = r["label"] if r else prefix.upper()
        if prefix == "be":
            acfg["be_subdir"] = (r or {}).get("subdir") or ""
    acfg["custom_deploy"] = [x for x in R.repos(acfg) if x["deploy"] == "command"]
    return acfg


# ---- repo deploy "command": lệnh deploy tự khai báo (Cấu hình → Danh sách repo) ---------------------------------------
# Mốc "đã deploy" của từng repo = commit cuối deploy thành công, lưu ở <root>/deploy-state.json. Lần đầu chưa có mốc thì
# lấy origin/<deploy_ref> hiện tại làm mốc (không deploy lại toàn bộ lịch sử).

def _state_path(acfg):
    return os.path.join(os.path.expanduser(acfg["root"]), "deploy-state.json")


def deploy_state(acfg, key=None, sha=None):
    try:
        with open(_state_path(acfg), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if key and sha:
        st[key] = {"sha": sha, "at": datetime.now().isoformat(timespec="seconds")}
        with open(_state_path(acfg) + ".tmp", "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1)
        os.replace(_state_path(acfg) + ".tmp", _state_path(acfg))
    return st


def custom_pending(acfg, repo):
    """Commit mới trên origin/<deploy_ref> sau mốc đã deploy của repo."""
    ref = "origin/" + repo["deploy_ref"]
    af.run(["git", "fetch", "-q", "origin", repo["deploy_ref"]], cwd=repo["path"], check=False)
    mark = (deploy_state(acfg).get(repo["key"]) or {}).get("sha")
    if not mark:
        head = git_lines(repo["path"], "rev-parse", ref)
        if head:
            deploy_state(acfg, repo["key"], head[0])
            log("%s: chưa có mốc deploy — lấy %s (%s) làm mốc, lần sau chỉ deploy commit mới" % (repo["label"], ref, head[0][:9]))
        return []
    return [tuple(l.split(" ", 1)) for l in git_lines(repo["path"], "log", "--no-merges", "--format=%H %s",
                                                       "%s..%s" % (mark, ref)) if " " in l]


def custom_contains(acfg, repo, hashes):
    """Bản đã deploy của repo (mốc deploy-state) chứa các commit chưa. None = chưa có mốc."""
    mark = (deploy_state(acfg).get(repo["key"]) or {}).get("sha")
    if not mark:
        return None
    return all(subprocess.run(["git", "merge-base", "--is-ancestor", h, mark], cwd=repo["path"],
                              stderr=subprocess.DEVNULL).returncode == 0 for h in hashes)


def custom_build_key(acfg):
    """Phần thêm vào khoá bản build (lịch sử QA) theo mốc deploy của các repo "command"."""
    st = deploy_state(acfg)
    return "".join("|%s:%s" % (r["key"], ((st.get(r["key"]) or {}).get("sha") or "")[:9]) for r in acfg.get("custom_deploy") or [])


def deploy_custom(acfg, dcfg, repo, root, logf):
    """Chạy các lệnh deploy của repo trong worktree mới của origin/<deploy_ref>; xong (và health OK) thì ghi mốc."""
    import repos as R
    work = os.path.join(root, "deploy-" + repo["key"])
    ref = repo["deploy_ref"]
    fresh_worktree(repo["path"], work, "origin/" + ref)
    try:
        R.link_shared(repo, work)
        err = R.run_setup(repo, work, af.build_env(acfg), logf.name if logf else None)
        if err:
            raise RuntimeError(err)
        sha = af.run(["git", "rev-parse", "HEAD"], cwd=work).strip()
        env = dict(af.build_env(acfg), DEPLOY_HOST=dcfg.get("host") or "", DEPLOY_USER=dcfg.get("user") or "",
                   DEPLOY_BASE=dcfg.get("base") or "", SSHPASS=dcfg.get("password") or "", DEPLOY_REF=ref, DEPLOY_SHA=sha)
        # Cả ô "Lệnh deploy" là MỘT script bash (set -e: dòng lỗi thì dừng) — heredoc / lệnh nhiều dòng chạy đúng.
        script = "set -e\n" + "\n".join(repo["deploy_cmd"])
        script = R.fill(script, repo, work).replace("{ref}", ref).replace("{sha}", sha).replace(
            "{host}", dcfg.get("host") or "").replace("{user}", dcfg.get("user") or "").replace("{base}", dcfg.get("base") or "")
        sh(["bash", "-c", script], cwd=work, logf=logf, env=env, timeout=5400)
        url = repo.get("deploy_health")
        if url:
            end = time.time() + 60 * float(dcfg.get("wait_minutes", 10))
            while True:
                try:
                    with urllib.request.urlopen(url, timeout=15) as res:
                        if 200 <= res.status < 300:
                            break
                except (urllib.error.URLError, OSError, ValueError):
                    pass
                if time.time() > end:
                    raise RuntimeError("%s: %s không trả 2xx sau deploy" % (repo["label"], url))
                time.sleep(10)
        deploy_state(acfg, repo["key"], sha)
        return sha[:9]
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", work], cwd=repo["path"], check=False)


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


def compose_image_cmd(dcfg, service):
    """Lệnh in image hiện ghi trong docker-compose.yml cho service (khối "  <service>:" tới service kế tiếp)."""
    return ("cd %s && sed -n '/^  %s:/,/^  [a-z]/ s|^[[:space:]]*image:[[:space:]]*||p' docker-compose.yml | head -1"
            % (dcfg["base"], service))


def compose_image(dcfg, service):
    """Image hiện tại của service trong docker-compose.yml (để trả lại nếu bản mới không lên). Lỗi → None."""
    try:
        return remote(dcfg, compose_image_cmd(dcfg, service), timeout=60).strip() or None
    except (RuntimeError, subprocess.TimeoutExpired):
        return None


def set_compose_image(dcfg, service, image, logf):
    """Backup docker-compose.yml rồi đổi dòng image: trong khối của service."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    remote(dcfg, "cd %s && cp docker-compose.yml docker-compose.yml.bak-%s-%s && "
                 "sed -i '/^  %s:/,/^  [a-z]/ s|^\\(\\s*image:\\s*\\).*|\\1%s|' docker-compose.yml && "
                 "sed -n '/^  %s:/,/image:/p' docker-compose.yml"
           % (dcfg["base"], stamp, service, service, image, service), logf)


def git_lines(repo, *args):
    if not repo:  # không có repo cho kiểu deploy này
        return []
    return af.run(["git"] + list(args), cwd=repo, check=False).strip().splitlines()


def pending(acfg, dcfg):
    """Commit mới chưa deploy: {"FE": [(hash, subject)], "BE": [...]}, cùng mốc so sánh."""
    fe, be = acfg["fe_repo"], acfg["be_repo"]
    if fe:
        af.run(["git", "fetch", "-q", "origin", acfg["fe_upstream"]], cwd=fe)
    if be:
        af.run(["git", "fetch", "-q", "origin", acfg["be_upstream"], dcfg["be_release_branch"]], cwd=be)
    dev = "origin/" + acfg["fe_upstream"]
    last = git_lines(fe, "log", "-1", "--format=%H", "-i", "--grep=update version", dev)
    rng = "%s..%s" % (last[0], dev) if last else dev
    fe_commits = [tuple(l.split(" ", 1)) for l in git_lines(fe, "log", "--no-merges", "--format=%H %s", rng)
                  if " " in l and "update version" not in l.lower()]
    be_commits = [tuple(l.split(" ", 1)) for l in git_lines(
        be, "log", "--no-merges", "--format=%H %s",
        "origin/%s..origin/%s" % (dcfg["be_release_branch"], acfg["be_upstream"])) if " " in l]
    out = {"FE": fe_commits, "BE": be_commits}
    for r in acfg.get("custom_deploy") or []:
        try:
            out[r["label"]] = custom_pending(acfg, r)
        except Exception as ex:  # noqa: BLE001 — một repo lỗi không chặn phần khác
            log("%s: không đọc được commit chưa deploy: %s" % (r["label"], ex))
            out[r["label"]] = []
    return out


def fresh_worktree(repo, path, ref):
    if os.path.isdir(path):
        subprocess.run(["git", "worktree", "remove", "--force", path], cwd=repo, check=False)
        shutil.rmtree(path, ignore_errors=True)
    af.run(["git", "worktree", "prune"], cwd=repo, check=False)
    af.run(["git", "worktree", "add", "-q", "--detach", path, ref], cwd=repo)


def deploy_fe(acfg, dcfg, root, logf, changes=None):
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
        new_image = "%s:%s" % (dcfg["fe_image"], version)
        old_image = compose_image(dcfg, dcfg["fe_service"])
        set_compose_image(dcfg, dcfg["fe_service"], new_image, logf)
        if changes is not None:
            changes.append({"part": "fe", "service": dcfg["fe_service"], "container": dcfg["fe_container"],
                            "old_image": old_image, "new_image": new_image, "version": version})
        return version
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", work], cwd=repo, check=False)


def deploy_be(acfg, dcfg, root, logf, changes=None):
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
        # Thông tin bản build cho GET /api/v1/system/version (như docker/dev/build-image.sh): không truyền thì BE báo
        # version "dev", commit / nhánh "unknown".
        commit = af.run(["git", "rev-parse", "--short", "HEAD"], cwd=work).strip()
        sh(["docker", "build", "-t", image, "-f", "docker/dev/Dockerfile",
            "--build-arg", envconf.get("DEPLOY_VERSION_ARG", "SOAR_VERSION") + "=" + tag, "--build-arg", "GIT_COMMIT=" + commit,
            "--build-arg", "GIT_BRANCH=" + release, "."], cwd=module, logf=logf, timeout=5400)
        tar = os.path.join(root, "%s.tar" % dcfg["be_image"])
        sh(["docker", "save", "-o", tar, image], logf=logf)
        upload(dcfg, tar, "%s/images/%s.tar" % (dcfg["base"], dcfg["be_image"]), logf)
        os.remove(tar)
        remote(dcfg, "cd %s && docker load -i images/%s.tar" % (dcfg["base"], dcfg["be_image"]), logf)
        # Migration Liquibase trong bản này (so với nhánh release trước khi merge): có thay đổi phá cấu trúc thì
        # bluegreen chạy song song bản cũ với schema mới là rủi ro → rollout() quyết định theo bg_be_destructive.
        db_dir = os.path.join(acfg["be_subdir"], "src", "main", "resources", "db")
        mig = af.run(["git", "diff", "origin/%s..HEAD" % release, "--", db_dir], cwd=work, check=False)
        old_image = compose_image(dcfg, dcfg["be_service"])
        set_compose_image(dcfg, dcfg["be_service"], image, logf)
        if changes is not None:
            changes.append({"part": "be", "service": dcfg["be_service"], "container": dcfg["be_container"],
                            "old_image": old_image, "new_image": image, "version": tag,
                            "migrations": bool(mig.strip()), "destructive": destructive_migrations(mig)})
        return tag
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", work], cwd=repo, check=False)


def _compose_state(**kw):
    try:
        with open(COMPOSE_STATE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.update(kw, updated_at=datetime.now().isoformat(timespec="seconds"))
    tmp = COMPOSE_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, COMPOSE_STATE)


def remote_stream(dcfg, command, logf=None, timeout=1800):
    """Như remote() nhưng đọc output từng dòng và ghi ngay vào COMPOSE_LIVE (dashboard đọc trực tiếp).
    Mã != 0 → RuntimeError kèm đuôi output (giống sh()); quá timeout → kill + TimeoutExpired."""
    env = dict(os.environ, SSHPASS=dcfg["password"])
    cmd = ["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20",
           "%s@%s" % (dcfg["user"], dcfg["host"]), command]
    return _stream(cmd, command, env, logf, timeout)


def _stream(cmd, label, env=None, logf=None, timeout=1800):
    started = time.time()
    out = []
    with open(COMPOSE_LIVE, "a", encoding="utf-8") as live:
        live.write("\n===== %s  $ %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), label))
        live.flush()
        _compose_state(running=True, command=label)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                                env=env, stdin=subprocess.DEVNULL, bufsize=1, start_new_session=True)

        def kill():  # cả nhóm tiến trình (sshpass → ssh), không thì ssh con vẫn giữ pipe và vòng đọc không dừng
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        timer = threading.Timer(timeout, kill)
        timer.start()
        try:
            for line in proc.stdout:
                out.append(line)
                live.write(line)
                live.flush()
            code = proc.wait()
        finally:
            timer.cancel()
        timed_out = time.time() - started >= timeout and code != 0
        live.write("===== %s  %s (%.0fs)\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                              "HẾT GIỜ" if timed_out else "mã thoát %s" % code, time.time() - started))
        live.flush()
    _compose_state(last_code=code)
    text = "".join(out)
    if logf:
        logf.write("\n$ %s\n%s" % (" ".join(cmd), text))
        logf.flush()
    if timed_out:
        raise subprocess.TimeoutExpired(cmd, timeout, output=text)
    if code != 0:
        raise RuntimeError("%s → mã %s: %s" % (cmd[0], code, text.strip()[-400:]))
    return text


def compose_up(dcfg, logf=None):
    """docker compose up -d --force-recreate. Lỗi (thường là service healthcheck chưa kịp healthy lúc cả stack
    khởi động lại → compose bỏ ngang, các service phụ thuộc như nginx chưa start) thì chờ rồi chạy lại
    "docker compose up -d" (không recreate) để start nốt phần còn thiếu. Hết lượt vẫn lỗi → ném lỗi cuối."""
    with open(COMPOSE_LIVE, "w", encoding="utf-8") as live:
        live.write("# deploy %s@%s:%s — bắt đầu %s\n" % (dcfg["user"], dcfg["host"], dcfg["base"],
                                                       datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    _compose_state(running=True, started_at=datetime.now().isoformat(timespec="seconds"), last_code=None,
                   result=None, host=dcfg["host"])
    try:
        _compose_retry(dcfg, logf)
        result = "ok"
    except Exception:
        result = "error"
        raise
    finally:
        with open(COMPOSE_LIVE, "a", encoding="utf-8") as live:
            live.write("\n# kết thúc %s — %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                                  "THÀNH CÔNG" if result == "ok" else "LỖI"))
        _compose_state(running=False, result=result, finished_at=datetime.now().isoformat(timespec="seconds"))


def _compose_retry(dcfg, logf=None):
    try:
        remote_stream(dcfg, "cd %s && docker compose up -d --force-recreate" % dcfg["base"], logf)
        return
    except (RuntimeError, subprocess.TimeoutExpired) as ex:
        last = ex
    tries, wait = int(dcfg.get("compose_retry_tries", 3)), int(dcfg.get("compose_retry_wait_seconds", 45))
    for i in range(1, tries + 1):
        log("compose up lỗi (%s) → chờ %ds rồi chạy lại lần %d/%d"
            % (str(last).splitlines()[-1][:200], wait, i, tries))
        note = "\n# compose up lỗi → chờ %ds, chạy lại lần %d/%d\n" % (wait, i, tries)
        if logf:
            logf.write(note)
            logf.flush()
        with open(COMPOSE_LIVE, "a", encoding="utf-8") as live:
            live.write(note)
        time.sleep(wait)
        try:
            remote_stream(dcfg, "cd %s && docker compose up -d" % dcfg["base"], logf)
            log("compose up chạy lại lần %d thành công" % i)
            return
        except (RuntimeError, subprocess.TimeoutExpired) as ex:
            last = ex
    raise last


# ==== Rollout: đưa image mới vào chạy mà không dừng trang (blue-green qua container tạm) ===========================
# Luồng bluegreen cho một phần (FE hoặc BE), docker-compose.yml đã ghi image mới (deploy_fe / deploy_be):
#   0. preflight: nginx chạy, upstream <name> chỉ khai báo trong file riêng bg_*_upstream_file, container chính chạy
#      → không đạt thì dùng "service" (dừng ngắn) cho phần đó.
#   1. nginx trỏ chắc chắn về bản chính (dọn lần deploy dở trước), xoá <container>-next cũ nếu còn.
#   2. docker compose run -d --no-deps --name <container>-next <service>: cùng env_file / volume / mạng compose,
#      KHÔNG publish cổng host → không đụng bản đang chạy.
#   3. kiểm tra bản tạm qua mạng nội bộ (curl từ container nginx): FE trả index.html đúng version; BE
#      /actuator/health = 200 và /api/v1/ping < 500. BE: gắn alias mạng "soar" cho bản tạm (service nội bộ gọi
#      http://soar:8888) rồi kiểm tra lại.
#   4. ghi upstream → <container>-next, nginx -t, nginx -s reload (graceful); kiểm tra URL công khai.
#      Lỗi ở 2-4: nginx về bản chính, xoá bản tạm, trả image cũ vào docker-compose.yml → trang giữ bản cũ.
#   5. docker compose up -d --no-deps --force-recreate <service>: container chính (đúng tên, cổng host, nhãn compose)
#      chạy image mới; trong lúc này trang chạy trên bản tạm. Kiểm tra bản chính qua tên service compose.
#      Lỗi: GIỮ bản tạm phục vụ trang (đặt restart unless-stopped), báo lỗi để người xem.
#   6. upstream → tên service compose (chỉ container chính có alias này), reload, chờ drain, xoá bản tạm.

class RolloutError(RuntimeError):
    pass


def q(s):
    return shlex.quote(str(s))


def part_cfg(dcfg, ch):
    """Thông số bluegreen của một thay đổi {"part": "fe"|"be", "service", "container", ...}."""
    p = ch["part"]
    return {
        "upstream": dcfg["bg_%s_upstream" % p],
        "file": dcfg["bg_%s_upstream_file" % p],
        "port": int(dcfg["bg_%s_port" % p]),
        "next": ch["container"] + dcfg.get("bg_next_suffix", "-next"),
        # Host cho upstream của bản chính: tên service compose (alias chỉ container do compose up tạo có; container
        # tạm tạo bằng compose run không có) → khi bản tạm còn alias "soar" nginx vẫn chỉ trỏ đúng bản chính.
        "main_host": ch["service"],
    }


def upstream_conf(name, host, port, keepalive=64):
    return ("# Quản lý bởi op-watch deploy_qa.py (deploy bluegreen) — đừng sửa tay, deploy sẽ ghi đè.\n"
            "upstream %s {\n    server %s:%d;\n    keepalive %d;\n}\n" % (name, host, int(port), keepalive))


def nginx_reload_cmd(dcfg):
    n = q(dcfg["nginx_container"])
    return "docker exec %s nginx -t && docker exec %s nginx -s reload" % (n, n)


def cmd_write_upstream(dcfg, ch, host):
    """Ghi file upstream trỏ host, nginx -t rồi reload; nginx -t lỗi → trả file cũ (.prev) và thoát mã 1."""
    pc = part_cfg(dcfg, ch)
    f = q(pc["file"])
    return ("cd %s && printf '%%s' %s > %s.tmp && { [ ! -f %s ] || cp %s %s.prev; } && mv %s.tmp %s && "
            "{ %s || { echo 'nginx -t / reload lỗi → trả upstream cũ' >&2; [ ! -f %s.prev ] || mv %s.prev %s; "
            "%s; exit 1; }; }"
            % (dcfg["base"], q(upstream_conf(pc["upstream"], host, pc["port"])), f, f, f, f, f, f,
               nginx_reload_cmd(dcfg), f, f, f, nginx_reload_cmd(dcfg)))


def cmd_preflight(dcfg, ch):
    """In "PREFLIGHT:OK" hoặc "PREFLIGHT: <lý do>…" (chỉ đọc)."""
    pc = part_cfg(dcfg, ch)
    rx = q(r"^[[:space:]]*upstream[[:space:]]+%s[[:space:]]*\{" % pc["upstream"])
    f, n, c = q(pc["file"]), q(dcfg["nginx_container"]), q(ch["container"])
    confdir = q(os.path.dirname(pc["file"]) or ".")
    return ("cd %s || { echo 'PREFLIGHT: không vào được thư mục compose'; exit 0; }; r=''; "
            "[ \"$(docker inspect -f '{{.State.Running}}' %s 2>/dev/null)\" = true ] || r=\"$r nginx-%s-không-chạy\"; "
            "[ -f %s ] || r=\"$r thiếu-file-%s\"; "
            "k=$(grep -lE %s %s/*.conf 2>/dev/null | wc -l); "
            "{ [ \"$k\" = 1 ] && grep -qE %s %s; } || r=\"$r upstream-%s-khai-báo-ở-$k-file-.conf(cần-đúng-1:-%s)\"; "
            "[ \"$(docker inspect -f '{{.State.Running}}' %s 2>/dev/null)\" = true ] || r=\"$r container-%s-không-chạy\"; "
            "echo \"PREFLIGHT:${r:-OK}\""
            % (dcfg["base"], n, dcfg["nginx_container"], f, pc["file"], rx, confdir, rx, f, pc["upstream"],
               pc["file"], c, ch["container"]))


def parse_preflight(out):
    """→ (ok, lý do)."""
    m = re.search(r"PREFLIGHT:(.*)", out or "")
    if not m:
        return False, "không đọc được kết quả preflight"
    why = m.group(1).strip()
    return why == "OK", why


def cmd_next_start(dcfg, ch):
    pc = part_cfg(dcfg, ch)
    return ("cd %s && docker rm -f %s >/dev/null 2>&1; docker compose run -d --no-deps --name %s %s"
            % (dcfg["base"], q(pc["next"]), q(pc["next"]), q(ch["service"])))


def cmd_next_net(dcfg, ch, aliases, only_if_service_alias=False):
    """Nối lại bản tạm vào mạng compose với đúng các alias cho trước (docker tự thêm tên container).
    - BE: aliases=[be_container] ("soar") để service nội bộ gọi http://soar:8888 vẫn có bản trả lời trong lúc
      container chính tạo lại. Chạy SAU khi bản tạm đã kiểm tra sống và TRƯỚC khi nginx trỏ sang (nối lại đổi IP).
    - only_if_service_alias: chỉ làm khi bản tạm lỡ có alias = tên service compose (alias dành riêng cho bản chính,
      upstream "bản chính" dùng tên này)."""
    pc = part_cfg(dcfg, ch)
    nx = q(pc["next"])
    cond = ""
    if only_if_service_alias:
        cond = ("docker inspect -f '{{range .NetworkSettings.Networks}}{{.Aliases}} {{end}}' %s | grep -qw %s || "
                "{ echo 'bản tạm không có alias %s'; exit 0; }; " % (nx, q(ch["service"]), ch["service"]))
    return ("set -e; %sNETS=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' %s); "
            "set -- $NETS; docker network disconnect \"$1\" %s; docker network connect %s\"$1\" %s; "
            "echo \"%s nối lại mạng $1, alias: %s\""
            % (cond, nx, nx, "".join("--alias %s " % q(a) for a in aliases), nx, pc["next"],
               ",".join(aliases) or "(chỉ tên container)"))


def cmd_probe(dcfg, ch, host, container):
    """Một lần kiểm tra (chỉ đọc): in "PROBE state=<trạng thái container> ok=<0|1> detail=<…>"."""
    pc = part_cfg(dcfg, ch)
    n = q(dcfg["nginx_container"])
    base = "http://%s:%d" % (host, pc["port"])
    head = "st=$(docker inspect -f '{{.State.Status}}' %s 2>/dev/null) || st=missing; st=${st:-missing}; " % q(container)
    if ch["part"] == "fe":
        return (head + "body=$(docker exec %s curl -s --max-time 10 %s 2>/dev/null); "
                "case \"$body\" in *%s*) ok=1;; *) ok=0;; esac; "
                "echo \"PROBE state=$st ok=$ok detail=index.html-${#body}-byte\""
                % (n, q(base + "/"), q('content="%s"' % ch["version"])))
    return (head + "h=$(docker exec %s curl -s -o /dev/null -w '%%{http_code}' --max-time 10 %s 2>/dev/null); "
            "p=$(docker exec %s curl -s -o /dev/null -w '%%{http_code}' --max-time 10 %s 2>/dev/null); "
            "ok=0; [ \"$h\" = 200 ] && [ \"${p:-000}\" -ge 200 ] 2>/dev/null && [ \"$p\" -lt 500 ] && ok=1; "
            "echo \"PROBE state=$st ok=$ok detail=health-$h-ping-$p\""
            % (n, q(base + dcfg.get("bg_be_health_path", "/actuator/health")), n, q(base + "/api/v1/ping")))


def parse_probe(out):
    """→ (trạng thái container, ok, chi tiết)."""
    m = re.search(r"PROBE state=(\S*) ok=(\d) detail=(\S*)", out or "")
    if not m:
        return "unknown", False, (out or "").strip()[-120:]
    return m.group(1), m.group(2) == "1", m.group(3)


def cmd_recreate_main(dcfg, ch):
    return "cd %s && docker compose up -d --no-deps --force-recreate %s" % (dcfg["base"], q(ch["service"]))


def cmd_service_up(dcfg, services):
    """Strategy service: chỉ tạo lại các service đổi image, rồi reload nginx (nginx phân giải tên container lúc
    nạp cấu hình → container mới đổi IP mà không reload thì 502). nginx không chạy → start riêng nó (--no-deps)."""
    n = q(dcfg["nginx_container"])
    return ("cd %s && docker compose up -d --no-deps --force-recreate %s && "
            "if [ \"$(docker inspect -f '{{.State.Running}}' %s 2>/dev/null)\" = true ]; then %s; "
            "else docker compose up -d --no-deps %s; fi"
            % (dcfg["base"], " ".join(q(s) for s in services), n, nginx_reload_cmd(dcfg), q(dcfg["nginx_service"])))


DESTRUCTIVE_RX = re.compile(
    r"<\s*(dropColumn|dropTable|renameColumn|renameTable|dropView|modifyDataType|addNotNullConstraint|"
    r"dropNotNullConstraint|dropForeignKeyConstraint|dropUniqueConstraint|dropPrimaryKey|dropSequence|mergeColumns)\b"
    r"|\b(DROP\s+(TABLE|COLUMN|VIEW|CONSTRAINT|INDEX)|RENAME\s+(TO|COLUMN)|ALTER\s+COLUMN\s+\S+\s+(TYPE|SET\s+NOT\s+NULL)"
    r"|TRUNCATE\b)", re.I)


def destructive_migrations(diff_text):
    """Dòng thêm mới (+) trong diff thư mục db/ có thay đổi schema không tương thích ngược → danh sách (tối đa 10)."""
    out = []
    for line in (diff_text or "").splitlines():
        if line.startswith("+") and not line.startswith("+++") and DESTRUCTIVE_RX.search(line):
            out.append(line[1:].strip()[:160])
            if len(out) >= 10:
                break
    return out


def _live_note(text, logf=None):
    with open(COMPOSE_LIVE, "a", encoding="utf-8") as live:
        live.write("# %s %s\n" % (datetime.now().strftime("%H:%M:%S"), text))
    if logf:
        logf.write("\n# %s\n" % text)
        logf.flush()
    log(text)


class Runner:
    """Chạy lệnh trên server (ghi compose-live cho dashboard) hoặc, khi dry=True, chỉ in lệnh sẽ chạy."""

    def __init__(self, dcfg, logf=None, dry=False):
        self.dcfg, self.logf, self.dry, self.plan = dcfg, logf, dry, []

    def run(self, command, timeout=1800, quiet=False, fake=""):
        """quiet: lệnh đọc / kiểm tra lặp lại — không stream vào compose-live (tránh làm rối log)."""
        if self.dry:
            self.plan.append(command)
            print("$ " + command)
            return fake
        if quiet:
            return remote(self.dcfg, command, self.logf, timeout=timeout)
        return remote_stream(self.dcfg, command, self.logf, timeout=timeout)

    def note(self, text):
        if self.dry:
            print("# " + text)
        else:
            _live_note(text, self.logf)

    def sleep(self, seconds):
        if not self.dry:
            time.sleep(seconds)


def wait_healthy(rn, ch, host, container, minutes):
    """Kiểm tra lặp tới khi ok / hết giờ / container đã thoát. → (ok, chi tiết lần cuối)."""
    deadline, started, last, n = time.time() + minutes * 60, time.time(), "", 0
    while True:
        try:
            out = rn.run(cmd_probe(rn.dcfg, ch, host, container), timeout=90, quiet=True,
                         fake="PROBE state=running ok=1 detail=dry-run")
        except (RuntimeError, subprocess.TimeoutExpired) as ex:
            out = str(ex)
        st, ok, detail = parse_probe(out)
        last = "%s state=%s %s" % (host, st, detail)
        n += 1
        if ok:
            rn.note("%s sống sau %.0fs (%s)" % (host, time.time() - started, detail))
            return True, last
        if st in ("exited", "dead", "missing"):
            return False, last
        if n == 1 or n % 6 == 0:
            rn.note("chờ %s sống… %s (%.0fs)" % (host, last, time.time() - started))
        if time.time() >= deadline:
            return False, last
        rn.sleep(10)


def public_ok(dcfg, ch, minutes=2):
    """Sau khi nginx trỏ sang bản tạm: URL công khai trả đúng (FE version / API < 500)."""
    deadline = time.time() + minutes * 60
    while True:
        try:
            if ch["part"] == "fe":
                html = urllib.request.urlopen(dcfg["fe_url"] + "/", timeout=15).read().decode("utf-8", "replace")
                if ('content="%s"' % ch["version"]) in html:
                    return True
            else:
                try:
                    urllib.request.urlopen(dcfg["api_url"] + "/api/v1/ping", timeout=15)
                    return True
                except urllib.error.HTTPError as ex:
                    if ex.code < 500:
                        return True
        except OSError:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(10)


def _restore_image(rn, ch):
    """Trả image cũ vào docker-compose.yml (bản mới không lên — compose up sau này không kéo nhầm bản lỗi)."""
    if not ch.get("old_image"):
        rn.note("không biết image cũ của %s — docker-compose.yml vẫn ghi %s, cần sửa tay" % (ch["service"],
                                                                                         ch["new_image"]))
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    try:
        rn.run("cd %s && cp docker-compose.yml docker-compose.yml.bak-%s-rollback-%s && "
               "sed -i '/^  %s:/,/^  [a-z]/ s|^\\(\\s*image:\\s*\\).*|\\1%s|' docker-compose.yml"
               % (rn.dcfg["base"], stamp, ch["service"], ch["service"], ch["old_image"]), timeout=60)
        rn.note("docker-compose.yml: %s trả lại image cũ %s" % (ch["service"], ch["old_image"]))
    except (RuntimeError, subprocess.TimeoutExpired) as ex:
        rn.note("trả image cũ lỗi: %s" % str(ex)[:200])


def _abort_next(rn, ch, pc, point_main):
    """Bản mới không đạt: (nếu đã chuyển) nginx về bản chính, xoá bản tạm, trả image cũ."""
    if point_main:
        try:
            rn.run(cmd_write_upstream(rn.dcfg, ch, pc["main_host"]), timeout=120)
        except (RuntimeError, subprocess.TimeoutExpired) as ex:
            rn.note("CẢNH BÁO: trỏ nginx về bản chính lỗi: %s" % str(ex)[:200])
    try:
        rn.run("docker logs --tail 60 %s 2>&1; docker rm -f %s" % (q(pc["next"]), q(pc["next"])), timeout=120)
    except (RuntimeError, subprocess.TimeoutExpired):
        pass
    _restore_image(rn, ch)


def bluegreen_part(rn, ch):
    """Rollout bluegreen một phần. → "ok" | "fallback" (server chưa sẵn sàng, chưa đụng gì). Lỗi → RolloutError."""
    dcfg = rn.dcfg
    pc = part_cfg(dcfg, ch)
    minutes = float(dcfg.get("bg_health_minutes", 10))
    ok, why = parse_preflight(rn.run(cmd_preflight(dcfg, ch), timeout=60, quiet=True, fake="PREFLIGHT:OK"))
    if not ok:
        rn.note("bluegreen %s: server chưa sẵn sàng (%s) → dùng cách 'service'" % (ch["part"].upper(), why))
        return "fallback"
    rn.note("bluegreen %s: %s → %s qua container tạm %s" % (ch["part"].upper(), ch.get("old_image"),
                                                              ch["new_image"], pc["next"]))
    # 1. trạng thái sạch: trang chính trỏ bản chính, không còn bản tạm của lần trước
    rn.run(cmd_write_upstream(dcfg, ch, pc["main_host"]), timeout=120)
    # 2-3. dựng + kiểm tra bản tạm
    try:
        rn.run(cmd_next_start(dcfg, ch), timeout=600)
        good, last = wait_healthy(rn, ch, pc["next"], pc["next"], minutes)
        if good:
            if ch["part"] == "be" and dcfg.get("bg_be_aliases", True):
                rn.run(cmd_next_net(dcfg, ch, [ch["container"]]), timeout=120)
            else:
                rn.run(cmd_next_net(dcfg, ch, [], only_if_service_alias=True), timeout=120)
            good, last = wait_healthy(rn, ch, pc["next"], pc["next"], 2)
    except (RuntimeError, subprocess.TimeoutExpired) as ex:
        good, last = False, str(ex).splitlines()[-1][:200] if str(ex) else repr(ex)
    if not good:
        _abort_next(rn, ch, pc, point_main=False)
        raise RolloutError("%s bản mới %s không lên (%s) — đã xoá container tạm, trang GIỮ bản cũ %s"
                           % (ch["part"].upper(), ch["new_image"], last, ch.get("old_image")))
    # 4. chuyển trang chính sang bản tạm
    try:
        rn.run(cmd_write_upstream(dcfg, ch, pc["next"]), timeout=120)
    except (RuntimeError, subprocess.TimeoutExpired) as ex:
        _abort_next(rn, ch, pc, point_main=True)
        raise RolloutError("%s: nginx không chuyển được sang bản tạm (%s) — trang GIỮ bản cũ"
                           % (ch["part"].upper(), str(ex).splitlines()[-1][:200]))
    if not rn.dry and not public_ok(dcfg, ch):
        _abort_next(rn, ch, pc, point_main=True)
        raise RolloutError("%s: sau khi chuyển nginx, URL công khai không trả đúng bản mới — đã trỏ lại bản cũ"
                           % ch["part"].upper())
    rn.note("%s: trang chính đang chạy bản tạm %s (%s)" % (ch["part"].upper(), pc["next"], ch["new_image"]))
    # 5. tạo lại container chính bằng image mới (trang vẫn phục vụ từ bản tạm)
    try:
        rn.run(cmd_recreate_main(dcfg, ch), timeout=900)
        good, last = wait_healthy(rn, ch, pc["main_host"], ch["container"], minutes)
    except (RuntimeError, subprocess.TimeoutExpired) as ex:
        good, last = False, str(ex).splitlines()[-1][:200] if str(ex) else repr(ex)
    if not good:
        try:
            rn.run("docker update --restart unless-stopped %s" % q(pc["next"]), timeout=60)
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        raise RolloutError("%s: container chính %s chưa lên với image mới (%s) — trang VẪN CHẠY bằng container tạm "
                           "%s (bản mới). Cần xem tay: sửa %s rồi trỏ %s về %s:%d, nginx -s reload, docker rm -f %s"
                           % (ch["part"].upper(), ch["container"], last, pc["next"], ch["container"], pc["file"],
                              pc["main_host"], pc["port"], pc["next"]))
    # 6. trỏ về bản chính, drain, xoá bản tạm
    rn.run(cmd_write_upstream(dcfg, ch, pc["main_host"]), timeout=120)
    rn.sleep(int(dcfg.get("bg_drain_seconds", 15)))
    try:
        # reload lần nữa sau khi xoá: nginx phân giải tên lúc nạp cấu hình, không giữ IP của bản tạm đã xoá
        rn.run("docker rm -f %s && %s" % (q(pc["next"]), nginx_reload_cmd(dcfg)), timeout=120)
    except (RuntimeError, subprocess.TimeoutExpired) as ex:
        rn.note("xoá %s / reload nginx lỗi (%s) — xem tay" % (pc["next"], str(ex)[:120]))
    rn.note("%s: xong, trang chính chạy %s trên %s" % (ch["part"].upper(), ch["new_image"], ch["container"]))
    return "ok"


def plan_parts(dcfg, changes, strategy=None):
    """→ [(change, cách)] với cách ∈ bluegreen | service; strategy recreate → [] (rollout chạy compose cũ)."""
    strategy = strategy or dcfg.get("deploy_strategy") or "bluegreen"
    if strategy not in STRATEGIES:
        raise RolloutError("DEPLOY_STRATEGY=%s không hợp lệ (%s)" % (strategy, "|".join(STRATEGIES)))
    if strategy == "recreate":
        return []
    parts = [p.lower() for p in (dcfg.get("bg_parts") or [])]
    out = []
    for ch in changes:
        how = "bluegreen" if strategy == "bluegreen" and ch["part"] in parts else "service"
        if how == "bluegreen" and ch["part"] == "be" and ch.get("destructive") \
                and dcfg.get("bg_be_destructive", "fallback") != "ignore":
            how = "service"
        out.append((ch, how))
    return out


def rollout(dcfg, changes, logf=None, dry=False, strategy=None):
    """Đưa các image mới (changes từ deploy_fe / deploy_be) vào chạy theo DEPLOY_STRATEGY. Ghi compose-live như
    compose_up để dashboard theo dõi. Lỗi → ném lỗi (run_all báo DEPLOY 86 LỖI, không QA)."""
    strategy = strategy or dcfg.get("deploy_strategy") or "bluegreen"
    if strategy == "recreate":
        if dry:
            print("$ cd %s && docker compose up -d --force-recreate   # strategy recreate (cách cũ)" % dcfg["base"])
            return
        compose_up(dcfg, logf)
        return
    plan = plan_parts(dcfg, changes, strategy)
    rn = Runner(dcfg, logf, dry)
    if not dry:
        with open(COMPOSE_LIVE, "w", encoding="utf-8") as live:
            live.write("# deploy %s@%s:%s — %s — bắt đầu %s\n" % (
                dcfg["user"], dcfg["host"], dcfg["base"], strategy, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        _compose_state(running=True, started_at=datetime.now().isoformat(timespec="seconds"), last_code=None,
                       result=None, host=dcfg["host"])
    result, errors = "error", []
    try:
        service_list = []
        for ch, how in plan:
            if how == "service" and ch["part"] == "be" and ch.get("destructive") and strategy == "bluegreen":
                rn.note("BE có migration phá cấu trúc → không chạy song song bản cũ / mới, dùng 'service': %s"
                        % "; ".join(ch["destructive"][:3]))
            if how == "bluegreen":
                try:
                    if bluegreen_part(rn, ch) == "ok":
                        continue
                except RolloutError as ex:  # phần này giữ bản cũ / bản tạm; vẫn làm tiếp phần còn lại
                    rn.note("LỖI: %s" % ex)
                    errors.append(str(ex))
                    continue
            service_list.append(ch["service"])
        if service_list:
            rn.note("tạo lại (dừng ngắn): %s" % ", ".join(service_list))
            rn.run(cmd_service_up(dcfg, service_list), timeout=1800)
        if errors:
            raise RolloutError(" | ".join(errors))
        result = "ok"
    finally:
        if not dry:
            with open(COMPOSE_LIVE, "a", encoding="utf-8") as live:
                live.write("\n# kết thúc %s — %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                                      "THÀNH CÔNG" if result == "ok" else "LỖI"))
            _compose_state(running=False, result=result, finished_at=datetime.now().isoformat(timespec="seconds"))


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


def soar_server(dcfg):
    """Server kiểu SOAR (deploy.json: ping /api/v1/ping, version FE trong meta, container BE qua ssh)?"""
    return dcfg.get("_inherit", True)


def ping_url(dcfg):
    """URL kiểm tra API sống: ping_path của dự án, kiểu SOAR /api/v1/ping, còn lại gốc API (trả < 500 là sống)."""
    path = dcfg.get("ping_path") or ("/api/v1/ping" if soar_server(dcfg) else "/")
    return dcfg["api_url"].rstrip("/") + "/" + path.lstrip("/")


def wait_up(dcfg, fe_version, minutes=None, revive=True):
    """Chờ FE trả đúng version (nếu deploy FE) và API trả lời (401 khi chưa đăng nhập = đã sống).
    BE thoát giữa chừng thì start lại (tối đa be_restart_tries lần)."""
    deadline = time.time() + (dcfg["wait_minutes"] if minutes is None else minutes) * 60
    tries, last_check = int(dcfg.get("be_restart_tries", 3)), 0
    while time.time() < deadline:
        if revive and soar_server(dcfg) and tries > 0 and time.time() - last_check >= 30:
            last_check = time.time()
            if revive_be(dcfg):
                tries -= 1
        ok_fe = ok_api = False
        try:
            html = urllib.request.urlopen(dcfg["fe_url"] + "/", timeout=15).read().decode("utf-8", "replace")
            ok_fe = not fe_version or ('content="%s"' % fe_version) in html
        except OSError:
            pass
        ok_api = api_alive(dcfg)
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
        # không có meta version (app khác SOAR): dấu = băm nội dung trang (đổi khi deploy bản build mới)
        fp["fe"] = m.group(1) if m else (None if soar_server(dcfg) else "html-" + hashlib.sha1(html.encode()).hexdigest()[:10])
    except OSError:
        pass
    if not soar_server(dcfg) or not dcfg.get("host"):
        return fp
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
        urllib.request.urlopen(ping_url(dcfg), timeout=15)
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


QA_TOOL_RETRY_MAX = 3  # lỗi công cụ QA (Gemini treo / không trả kết quả) trên cùng một bản build: thử lại tối đa


def tool_errors(display_id, build, add=False):
    """Số lần QA lỗi công cụ của ticket trên bản build (add=True: cộng thêm 1). Bản build khác thì đếm lại từ 0."""
    path = os.path.join(envconf.autofix_root(), "qa-tool-errors.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    cur = data.get(display_id) or {}
    n = cur.get("n", 0) if cur.get("build") == build else 0
    if add:
        n += 1
        data[display_id] = {"build": build, "n": n, "at": datetime.now().isoformat(timespec="seconds")}
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)
    return n


def _merged_pending(waiting, done, remaining, retry):
    """Hàng chờ mới = (file hiện tại ∪ hàng chờ lúc bắt đầu) − đã QA xong + còn lại + cần thử lại. Đọc lại file thay vì
    ghi đè bằng bản đọc lúc bắt đầu: lượt QA khác (nút bấm / lượt kiểm tra) có thể đã thêm ticket trong lúc chạy."""
    return (set(load_pending()) | set(waiting)) - set(done) | set(remaining) | set(retry)


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
            cr = next((r for r in acfg.get("custom_deploy") or [] if r["label"] == label), None)
            if cr:
                inside = custom_contains(acfg, cr, [h])
                how = "so với commit đã deploy gần nhất của %s (%s)" % (label, cr["deploy_ref"])
            elif label == "FE":
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
    reason = "## Cần quyết định\nQA tự động trên server test KHÔNG ĐẠT / KHÔNG TỰ TEST ĐƯỢC %d lần liên tiếp " \
             "(autofix đã kiểm chứng / sửa lại %d lần). Cần người xem.\n\n%s" % (
                 n, n - 1, af.report_section(qa_text, "Kết quả")[:3000])
    af.mark_for_review(api, wp["id"], acfg["review_status_id"], reason, os.path.basename(os.path.dirname(qa_path)))
    return " → quá %d lần, đã chuyển UNREVIEWED" % dcfg["qa_fail_autofix_max"]


def qa_ticket(api, cfg, acfg, dcfg, me_id, display_id, commits, deployed, guard=None):
    """Bọc _qa_ticket: ghi thời gian QA trên server lên ticket (Testing)."""
    t0 = time.time()
    verdict, path = _qa_ticket(api, cfg, acfg, dcfg, me_id, display_id, commits, deployed, guard)
    if verdict:  # "" = không phải ticket của mình / không ở trạng thái QA → không ghi
        af.log_time(api, dict(af.AUTOFIX_DEFAULTS, **acfg), display_id, time.time() - t0, "test",
                    "[QA tự động] Gemini QA trên server test (%s) — %s" % (deployed, verdict[:120]))
    return verdict, path


def _qa_ticket(api, cfg, acfg, dcfg, me_id, display_id, commits, deployed, guard=None):
    """QA một ticket trên server. Trả (kết luận, đường dẫn qa.md)."""
    wp = api.get("/api/v3/work_packages/%s" % display_id)
    assignee = (wp["_links"].get("assignee") or {}).get("href", "")
    status_id = wp["_links"]["status"]["href"].rstrip("/").split("/")[-1]
    review = status_id in {str(x) for x in dcfg.get("qa_review_status_ids") or []}
    if not assignee.endswith("/%s" % me_id) or (status_id != str(acfg["resolved_status_id"]) and not review):
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
    notify("[QA SERVER] %s" % display_id, "Đang test trên server 86 (%s): %s" % (
        dcfg["qa_engine"], wp.get("subject", "")))
    if dcfg["qa_engine"] == "gemini":
        gemini_qa(acfg, dcfg, qa_dir, display_id, deployed, guard)
    else:
        prompt = QA_PROMPT.format(display_id=display_id, subject=wp.get("subject", ""), deployed=deployed,
                                  qa_dir=qa_dir, soar_api=SOAR_API, fe_url=dcfg["fe_url"], ui_check=UI_CHECK,
                                  ticket_accounts=", ".join(dcfg.get("qa_ticket_accounts") or []) or "trống — không dùng")
        prompt += "\n\n# RULE TỪ CÁC LẦN QA SAI TRƯỚC (BẮT BUỘC TUÂN THEO)\n" + load_qa_rules()
        with open(os.path.join(qa_dir, "prompt.md"), "w", encoding="utf-8") as f:
            f.write(prompt)
        scope = "/" + qa_dir + "/**"
        cmd = [acfg["claude"], "-p", prompt, "--permission-mode", "dontAsk",
               "--max-budget-usd", str(acfg["max_budget_usd"]), "--output-format", "stream-json", "--verbose",
               "--allowedTools"] + QA_TOOLS + ["Edit(%s)" % scope, "Write(%s)" % scope] + \
              ["--disallowedTools"] + af.DISALLOWED_TOOLS
        if acfg.get("model"):
            cmd += ["--model", acfg["model"]]
        try:  # gói bị vô hiệu → tự chạy lại bằng gói khác
            af.claude_oneshot(acfg, cmd, os.path.join(qa_dir, "claude.jsonl"), os.path.join(qa_dir, "claude.err"),
                              prefer=acfg.get("_profile"), cwd=qa_dir)
        except RuntimeError as ex:
            log("%s: %s" % (display_id, ex))
    path = os.path.join(qa_dir, "qa.md")
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    m = re.search(r"QA:\s*(KHÔNG RÕ YÊU CẦU|KHÔNG ĐẠT|ĐẠT|KHÔNG TỰ TEST ĐƯỢC)", text[:200])
    verdict = m.group(1) if m else "KHÔNG CÓ BÁO CÁO"
    # Server đổi trong lúc test → kết quả không đáng tin, không comment / không chuyển QA.
    change = guard() if guard else None
    if change:
        raise ServerChanged(change)
    if verdict == "ĐẠT":
        body = af.report_section(text, "Kết quả") or "Đã kiểm tra đạt."
        comment = "**[QA tự động] Đã deploy lên môi trường test (%s) và kiểm tra lại: ĐẠT** — %schuyển %s.\n\n%s" % (
            deployed, "chuyển Resolved, " if review else "", dcfg["qa_user_name"], af.for_tester(body)[:3000])
        comment += "\n".join(af.evidence_lines(api, wp["id"], text, qa_dir))
        af.op_write(api, "POST", "/api/v3/work_packages/%s/activities" % wp["id"], {"comment": {"raw": comment}})
        lock_version = api.get("/api/v3/work_packages/%s" % wp["id"])["lockVersion"]
        links = {"assignee": {"href": "/api/v3/users/%s" % dcfg["qa_user_id"]}}
        if review:  # ticket đang Review: QA đạt → Resolved
            links["status"] = {"href": "/api/v3/statuses/%s" % acfg["resolved_status_id"]}
        af.op_write(api, "PATCH", "/api/v3/work_packages/%s" % wp["id"], {"lockVersion": lock_version, "_links": links})
        verdict += (" → Resolved, đã chuyển " if review else " → đã chuyển ") + dcfg["qa_user_name"]
        fail_count(display_id, 0)
    elif verdict == "KHÔNG ĐẠT":
        verdict += handle_fail(api, acfg, dcfg, wp, display_id, path, text, commits)
    elif verdict == "KHÔNG TỰ TEST ĐƯỢC":
        result = af.report_section(text, "Kết quả")
        if re.match(r"(Gemini lỗi|Gemini không trả kết quả)", result):
            # Lỗi của công cụ QA, không phải ticket: không giao Claude, không ghi qa-history → lượt sau QA lại.
            verdict += " " + QA_TOOL_ERROR
        else:
            # Claude kiểm chứng: QA có thể tự test được không (QA SAI → rule cho lần sau), hay cần tester test tay.
            verdict += handle_fail(api, acfg, dcfg, wp, display_id, path, text, commits)
    elif verdict == "KHÔNG RÕ YÊU CẦU":
        # Không test: comment điểm chưa rõ + câu hỏi, chuyển UNREVIEWED (vẫn giao cho mình) để người quyết định.
        body = af.report_section(text, "Kết quả") or "QA tự động không xác định được cần kiểm tra gì."
        comment = "**[QA tự động] Chưa test trên môi trường test (%s): yêu cầu chưa rõ** — chuyển UNREVIEWED.\n\n%s" % (
            deployed, af.for_tester(body)[:3000])
        comment += "\n".join(af.evidence_lines(api, wp["id"], text, qa_dir))
        af.op_write(api, "POST", "/api/v3/work_packages/%s/activities" % wp["id"], {"comment": {"raw": comment}})
        lock_version = api.get("/api/v3/work_packages/%s" % wp["id"])["lockVersion"]
        af.op_write(api, "PATCH", "/api/v3/work_packages/%s" % wp["id"], {
            "lockVersion": lock_version,
            "_links": {"status": {"href": "/api/v3/statuses/%s" % acfg["review_status_id"]}}})
        verdict += " → đã chuyển UNREVIEWED"
    report_dir = os.path.expanduser(acfg.get("report_dir") or "")
    if report_dir:
        os.makedirs(report_dir, exist_ok=True)
        shutil.copy(path, os.path.join(report_dir, os.path.basename(qa_dir) + ".md")) if text else None
    inbox(cfg, "**QA SERVER** %s — %s · %s" % (display_id, verdict, path))
    notify("[QA SERVER] %s: %s" % (display_id, verdict), wp.get("subject", ""))
    return verdict, path


QA_HISTORY = os.path.join(envconf.autofix_root(), "qa-history.json")


def resolved_mine(api, cfg, acfg, dcfg=None):
    """Ticket đang Resolved (hoặc trạng thái qa_review_status_ids, vd Review) và giao cho mình: ứng viên QA dù không còn
    trong hàng chờ / đợt deploy mới."""
    ids = [str(acfg["resolved_status_id"])] + [str(x) for x in ((dcfg or {}).get("qa_review_status_ids") or [])]
    wps = api.my_work_packages(cfg["project"], ids)
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


def review_queue(api=None):
    """Hàng QA của mọi dự án bật QA (gộp)."""
    out = {"items": [], "build": "", "server_up": False, "at": time.time()}
    for proj, _ in deploy_targets():
        if not proj["qa"]:
            continue
        r = _review_queue(api, proj)
        for it in r["items"]:
            it["project"] = proj["key"]
        out["items"] += r["items"]
        out["build"] = out["build"] or r["build"]
        out["server_up"] = out["server_up"] or r["server_up"]
    return out


def _review_queue(api=None, proj=None):
    """
    Ticket giao cho mình ở trạng thái qa_review_status_ids (vd Review) + hàng chờ QA: tình trạng từng ticket cho trang
    theo dõi. Mỗi mục: display_id, wp_id, subject, status, state (waiting | no_commit | not_deployed | done), note,
    last (kết quả QA gần nhất), build (bản đang chạy).
    """
    cfg, acfg, dcfg = load(proj)
    api = api or op_watch.OpenProject(cfg["url"], cfg["api_key"])
    review_ids = [str(x) for x in dcfg.get("qa_review_status_ids") or []]
    wps = api.my_work_packages(cfg["project"], review_ids) if review_ids else []
    queued = set(load_pending())
    fp = fingerprint(dcfg)
    build = "%s|%s" % (fp.get("fe"), fp.get("be")) + custom_build_key(acfg)
    out = []
    for w in wps:
        t = w.get("displayId") or "WP-%s" % w["id"]
        last = qa_history(t) or {}
        fe_h = [l.split()[0] for l in git_lines(acfg["fe_repo"], "log", "--no-merges", "-F", "--grep=[%s]" % t,
                                                 "--format=%H", "origin/" + acfg["fe_upstream"])]
        be_h = [l.split()[0] for l in git_lines(acfg["be_repo"], "log", "--no-merges", "-F", "--grep=[%s]" % t,
                                                 "--format=%H", "origin/" + acfg["be_upstream"])]
        if last.get("build") == build:
            state, note = "done", "đã QA trên bản này: %s" % last.get("verdict")
        elif not fe_h and not be_h:
            state, note = "no_commit", "chưa có commit [%s] trên %s / %s" % (t, acfg["fe_upstream"], acfg["be_upstream"])
        elif (fe_h and fe_contains(acfg, fp.get("fe"), fe_h) is not True) or \
                (be_h and be_contains(acfg, fp.get("be"), be_h) is not True):
            state, note = "not_deployed", "bản đang chạy chưa có bản sửa — chờ đợt merge + deploy"
        else:
            state, note = "waiting", "chờ lượt kiểm tra kế tiếp (tối đa 10 phút)"
        out.append({"display_id": t, "wp_id": w["id"], "subject": w.get("subject", ""),
                    "status": (w["_links"].get("status") or {}).get("title"), "state": state, "note": note,
                    "fe_commits": len(fe_h), "be_commits": len(be_h), "last": last, "queued": t in queued})
    seen = {x["display_id"] for x in out}
    for t in sorted(queued - seen):
        out.append({"display_id": t, "wp_id": None, "subject": "", "status": "hàng chờ QA", "state": "waiting",
                    "note": "trong hàng chờ QA (đợt deploy trước chưa test xong)", "last": qa_history(t) or {},
                    "queued": True})
    return {"items": out, "build": build, "server_up": bool(fp.get("fe")), "at": time.time()}


def review_watch():
    for proj, _ in deploy_targets():
        if proj["qa"]:
            try:
                _review_watch(proj)
            except Exception as ex:  # noqa: BLE001 — dự án này lỗi không chặn dự án khác
                log("review-watch %s lỗi: %s" % (proj["name"], ex))


def _review_watch(proj):
    """
    op-watch gọi định kỳ (on_tick): ticket giao cho mình vừa chuyển sang trạng thái qa_review_status_ids (vd Review) mà
    chưa QA trên bản đang chạy → QA ngay (không deploy). Đang có merge / QA khác, hoặc server không trả lời → bỏ qua lượt
    này, không báo lỗi (lượt sau / đợt merge theo lịch sẽ làm).
    """
    cfg, acfg, dcfg = load(proj)
    if not dcfg.get("qa_review_status_ids") or not qa_enabled(acfg, dcfg):
        return
    root = os.path.expanduser(acfg["root"])
    os.makedirs(root, exist_ok=True)
    lk = open(os.path.join(root, ".review-watch.lock"), "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    if os.path.exists(QA_ACTIVE) or subprocess.run(["systemctl", "--user", "is-active", "--quiet", "op-merge.service"]
                                                   ).returncode == 0:
        return
    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    review_wps = api.my_work_packages(cfg["project"], [str(x) for x in dcfg["qa_review_status_ids"]])
    ids = [w.get("displayId") for w in review_wps]
    queued = load_pending()  # hàng chờ QA (vd lỗi công cụ QA lượt trước) — QA lại, không đợi đợt deploy sau
    ids = sorted(set(ids) | set(queued))
    if not ids or not wait_up(dcfg, None, minutes=0.3, revive=False):
        return
    fp = fingerprint(dcfg)
    if not fp.get("fe"):
        return
    build = "%s|%s" % (fp.get("fe"), fp.get("be"))
    root_status = os.path.join(root, ".status")
    review = set()
    for w in review_wps:
        did = w.get("displayId")
        if not did or _autofix_busy(root_status, did):
            continue  # autofix đang sửa ticket này (vd sau QA không đạt) — QA khi xong
        last = qa_history(did) or {}
        if last.get("build") != build:
            review.add(did)  # chưa QA trên bản này
        elif entered_status_after(api, w, last.get("at")):
            review.add(did)  # chuyển sang Review SAU lần QA trước (vd mình thêm hướng dẫn rồi mở lại)
    cands = sorted(review | {t for t in queued if (qa_history(t) or {}).get("build") != build
                             and tool_errors(t, build) < QA_TOOL_RETRY_MAX})
    if cands:
        log("ticket Review / hàng chờ cần QA trên bản đang chạy: %s" % ", ".join(cands))
        run_one(proj, only=cands, auto_only=True, review=review)


def _autofix_busy(status_dir, display_id):
    try:
        with open(os.path.join(status_dir, display_id + ".json"), encoding="utf-8") as f:
            st = json.load(f)
        return st.get("state") in af.ACTIVE_STATES and af.status_alive(st)  # trạng thái cũ của tiến trình đã chết thì bỏ qua
    except (OSError, ValueError):
        return False


def entered_status_after(api, wp, at_local):
    """Ticket chuyển sang trạng thái hiện tại SAU thời điểm at_local (ISO giờ máy, lúc QA lần trước)?"""
    if not at_local:
        return True
    try:
        since = datetime.fromisoformat(at_local).astimezone()
    except ValueError:
        return False
    title = ((wp.get("_links") or {}).get("status") or {}).get("title") or ""
    for act in reversed(api.activities(wp["id"])):
        for d in act.get("details") or []:
            if re.search(r"Status changed from .+ to %s\s*$" % re.escape(title), d.get("raw") or ""):
                when = datetime.fromisoformat(act["createdAt"].replace("Z", "+00:00"))
                return when > since
    return False


def deploy_only(cfg, acfg, dcfg, dry_run=False, qa_requested=False):
    """Dự án không QA tự động: chỉ deploy các repo có commit mới (deploy "command" / fe-bundle / be-image)."""
    if qa_requested:
        log("dự án %s chưa bật QA tự động (Cấu hình → Dự án) — bỏ qua QA" % P.current(acfg)["name"])
        return
    root = os.path.expanduser(acfg["root"])
    os.makedirs(root, exist_ok=True)
    commits = pending(acfg, dcfg)
    log("chưa deploy: %s" % (", ".join("%s %d commit" % (k, len(v)) for k, v in commits.items()) or "không có repo deploy"))
    if dry_run or not any(commits.values()):
        return
    logf = open(os.path.join(root, "deploy-%s-%s.log" % (P.current(acfg)["key"], datetime.now().strftime("%m%d-%H%M"))),
                "w", encoding="utf-8")
    parts = []
    try:
        changes = []
        if commits.get("FE"):
            parts.append("FE %s" % deploy_fe(acfg, dcfg, root, logf, changes))
        if commits.get("BE"):
            parts.append("BE %s:%s" % (dcfg["be_image"], deploy_be(acfg, dcfg, root, logf, changes)))
        if changes:
            rollout(dcfg, changes, logf)
        for r in acfg.get("custom_deploy") or []:
            if commits.get(r["label"]):
                parts.append("%s %s" % (r["label"], deploy_custom(acfg, dcfg, r, root, logf)))
    except Exception as ex:  # noqa: BLE001
        msg = "deploy lỗi: %s (log %s)" % (str(ex).splitlines()[0][:300], logf.name)
        log(msg)
        inbox(cfg, "**DEPLOY 86 LỖI** " + msg)
        notify("[DEPLOY 86 LỖI]", msg)
        return
    msg = "đã deploy %s (dự án không QA tự động)" % ", ".join(parts)
    log(msg)
    inbox(cfg, "**DEPLOY 86** " + msg)
    notify("[DEPLOY 86]", msg)


def run_all(dry_run=False, only=None, auto_only=False, force=False, review=None, project=None):
    """Deploy + QA lần lượt từng dự án (merge_to_dev gọi sau merge; --qa chỉ các dự án của ticket được chọn)."""
    for proj, tickets in deploy_targets(only, project):
        try:
            run_one(proj, dry_run, tickets if only else None, auto_only, force, review)
        except Exception as ex:  # noqa: BLE001 — dự án này lỗi vẫn deploy dự án khác
            log("dự án %s: deploy / QA lỗi: %s" % (proj["name"], ex))
            inbox(load(proj)[0], "**DEPLOY 86 LỖI** %s" % str(ex).splitlines()[0][:300])


def run_one(proj, dry_run=False, only=None, auto_only=False, force=False, review=None):
    cfg, acfg, dcfg = load(proj)
    if len(load_all()[1]) > 1:
        log("— dự án %s (OP %s)" % (proj["name"], proj["op_project"]))
    if not qa_enabled(acfg, dcfg):
        return deploy_only(cfg, acfg, dcfg, dry_run, bool(only))
    root = os.path.expanduser(acfg["root"])
    os.makedirs(root, exist_ok=True)
    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    me_id = api.me()["id"]
    deployed_parts, commits = [], pending(acfg, dcfg)
    waiting = load_pending()
    auto = set()  # ticket lấy từ OpenProject (Resolved, giao cho mình) — chỉ QA khi bản đang chạy đã có bản sửa
    if only:
        tickets = list(only)
        if auto_only:  # review_watch: cùng điều kiện như ticket lấy từ OpenProject (có bản sửa trên server, chưa QA)
            auto = set(only)
    else:
        new = sorted({t for items in commits.values() for _, s in items for t in re.findall(r"\[(%s)\]" % envconf.TICKET_RX, s)})
        try:
            auto = set(resolved_mine(api, cfg, acfg, dcfg)) - set(new) - set(waiting)
        except Exception as ex:  # noqa: BLE001 — không đọc được OpenProject thì vẫn QA như cũ
            log("không lấy được danh sách Resolved: %s" % ex)
        tickets = sorted(set(new) | set(waiting) | auto)
        log("chưa deploy: %s · ticket mới: %s · hàng chờ QA: %s · Resolved/Review chưa QA: %s" % (
            ", ".join("%s %d commit" % (k, len(v)) for k, v in commits.items()), ", ".join(new) or "không có", ", ".join(waiting) or "trống",
            ", ".join(sorted(auto)) or "không có"))
        if dry_run or not tickets:
            return
    fe_version = None
    if not only and any(commits.values()):
        logf = open(os.path.join(root, "deploy-%s.log" % datetime.now().strftime("%m%d-%H%M")), "w", encoding="utf-8")
        notify("[DEPLOY 86]", "Bắt đầu deploy: " + ", ".join("%s %d commit" % (k, len(v)) for k, v in commits.items() if v))
        try:
            changes = []
            if commits["FE"]:
                fe_version = deploy_fe(acfg, dcfg, root, logf, changes)
                deployed_parts.append("FE %s" % fe_version)
            if commits["BE"]:
                deployed_parts.append("BE %s:%s" % (dcfg["be_image"], deploy_be(acfg, dcfg, root, logf, changes)))
            if changes:
                rollout(dcfg, changes, logf)
            for r in acfg.get("custom_deploy") or []:
                if commits.get(r["label"]):
                    deployed_parts.append("%s %s" % (r["label"], deploy_custom(acfg, dcfg, r, root, logf)))
        except Exception as ex:  # noqa: BLE001
            msg = "deploy lỗi: %s (log %s)" % (str(ex).splitlines()[0][:300], logf.name)
            log(msg)
            save_pending(tickets, "deploy lỗi")
            inbox(cfg, "**DEPLOY 86 LỖI** " + msg)
            notify("[DEPLOY 86 LỖI]", msg)
            return
        if not wait_up(dcfg, fe_version):
            msg = "server chưa lên sau %d phút (%s) — chưa QA" % (dcfg["wait_minutes"], ", ".join(deployed_parts))
            save_pending(tickets, "server chưa lên sau deploy")
            inbox(cfg, "**DEPLOY 86 LỖI** " + msg)
            notify("[DEPLOY 86 LỖI]", msg)
            return
        msg = "đã deploy %s · QA %d ticket" % (", ".join(deployed_parts), len(tickets))
        log(msg)
        inbox(cfg, "**DEPLOY 86** " + msg)
        notify("[DEPLOY 86]", msg)

    if not deployed_parts and not wait_up(dcfg, None, minutes=dcfg.get("alive_wait_minutes", 5)):
        msg = "server không trả lời (FE %s / API %s) — chưa QA, giữ hàng chờ" % (dcfg["fe_url"], dcfg["api_url"])
        log(msg)
        save_pending(tickets, "server không sống")
        inbox(cfg, "**DEPLOY 86 LỖI** " + msg)
        notify("[DEPLOY 86 LỖI]", msg)
        return
    baseline = fingerprint(dcfg)
    if not baseline.get("fe"):
        msg = "không đọc được version FE trên %s — chưa QA, giữ hàng chờ" % dcfg["fe_url"]
        log(msg)
        save_pending(tickets, "không đọc được version FE")
        inbox(cfg, "**DEPLOY 86 LỖI** " + msg)
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
                 waiting, remaining, done, auto=auto, force=set(only or []) if force else set(), review=set(review or []))
    finally:
        if os.path.exists(QA_ACTIVE):
            os.remove(QA_ACTIVE)


def _qa_loop(api, cfg, acfg, dcfg, me_id, tickets, commits, deployed_parts, deployed, baseline, guard, lock,
             waiting, remaining, done, auto=(), force=(), review=()):
    """QA các ticket, tối đa qa_parallel ticket cùng lúc (Gemini); server đổi giữa chừng thì dừng tất cả."""
    mu = threading.Lock()
    stop = {"msg": None}
    retry = set()  # lỗi công cụ QA → giữ hàng chờ cho lượt kiểm tra sau

    def one(display_id):
        if stop["msg"]:
            return
        hashes = [(label, repo, [h for h, s in commits[label] if "[%s]" % display_id in s] or
                   [l.split()[0] for l in git_lines(repo, "log", "--no-merges", "-F", "--grep=[%s]" % display_id,
                                                    "--format=%H", "origin/" + up)])
                  for label, repo, up in [("FE", acfg["fe_repo"], acfg["fe_upstream"]),
                                          ("BE", acfg["be_repo"], acfg["be_upstream"])] +
                  [(r["label"], r["path"], r["deploy_ref"]) for r in acfg.get("custom_deploy") or []]]
        hashes = [x for x in hashes if x[2]]
        fe_hashes = [h for label, _, hs in hashes if label == "FE" for h in hs]
        be_hashes = [h for label, _, hs in hashes if label == "BE" for h in hs]
        build = "%s|%s" % (baseline.get("fe"), baseline.get("be")) + custom_build_key(acfg)
        cust = {r["label"]: r for r in acfg.get("custom_deploy") or []}
        cust_state = [custom_contains(acfg, cust[label], hs) for label, _, hs in hashes if label in cust]
        if display_id in force:
            # QA tay từ trang theo dõi: bỏ điều kiện commit / bản đang chạy có bản sửa (vd ticket không cần sửa code).
            log("%s: QA theo yêu cầu (bỏ qua điều kiện commit: FE %d, BE %d commit)"
                % (display_id, len(fe_hashes), len(be_hashes)))
        elif display_id in review:
            # Ticket vừa được chuyển sang Review (sau lần QA trước, có thể kèm hướng dẫn mới): QA lại kể cả cùng bản
            # build. Không có commit sửa → QA hành vi hiện tại (như bấm QA trên server); có commit mà bản đang chạy
            # chưa có → đợi deploy.
            if (fe_hashes and fe_contains(acfg, baseline["fe"], fe_hashes) is False) or \
                    (be_hashes and be_contains(acfg, baseline["be"], be_hashes) is False) or False in cust_state:
                log("%s: vào Review nhưng bản đang chạy chưa có bản sửa — đợi deploy" % display_id)
                with mu:
                    remaining.remove(display_id)
                return
            log("%s: vừa chuyển Review — QA lại (FE %d, BE %d commit)" % (display_id, len(fe_hashes), len(be_hashes)))
        elif display_id in auto:
            # Ticket Resolved lấy từ OpenProject: chỉ QA khi chắc bản đang chạy có bản sửa, và chưa QA trên bản này.
            def drop(why):
                log("%s: %s — chưa QA" % (display_id, why))
                with mu:
                    remaining.remove(display_id)
            if not hashes:
                return drop("không thấy commit [%s] trên %s / %s" % (display_id, acfg["fe_upstream"], acfg["be_upstream"]))
            if (fe_hashes and fe_contains(acfg, baseline["fe"], fe_hashes) is not True) or \
                    (be_hashes and be_contains(acfg, baseline["be"], be_hashes) is not True) or \
                    any(x is not True for x in cust_state):
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
        elif False in cust_state:
            log("%s: repo deploy tuỳ chỉnh chưa deploy bản sửa — để lại hàng chờ" % display_id)
            return
        slot = af.acquire_slot(os.path.expanduser(acfg["root"]), acfg) if dcfg["qa_engine"] != "gemini" else None
        try:
            verdict, path = qa_ticket(api, cfg, acfg, dcfg, me_id, display_id, hashes, deployed, guard)
            log("%s: %s" % (display_id, verdict or "bỏ qua (không phải của mình / không ở Resolved)"))
            if verdict and QA_TOOL_ERROR not in verdict:
                qa_history(display_id, build, verdict)
            elif verdict:
                # Lỗi công cụ QA: giữ trong hàng chờ để lượt kiểm tra 10 phút (review_watch) QA lại, tối đa
                # QA_TOOL_RETRY_MAX lần trên bản build này (trước đây ticket rời hàng chờ, phải đợi đợt deploy sau).
                n = tool_errors(display_id, build, add=True)
                if n < QA_TOOL_RETRY_MAX:
                    with mu:
                        retry.add(display_id)
                    log("%s: lỗi công cụ QA lần %d/%d — giữ hàng chờ, QA lại lượt kiểm tra sau"
                        % (display_id, n, QA_TOOL_RETRY_MAX))
                else:
                    inbox(cfg, "**QA SERVER LỖI CÔNG CỤ** %s — %d lần trên bản %s, ngừng thử (QA lại ở đợt deploy sau"
                                  " hoặc bấm QA trên server)" % (display_id, n, build))
        except ServerChanged as ex:
            with mu:
                if not stop["msg"]:
                    stop["msg"] = "%s (%s)" % (display_id, ex)
            return
        except Exception as ex:  # noqa: BLE001
            log("%s: QA lỗi %s" % (display_id, ex))
            inbox(cfg, "**QA SERVER LỖI** %s — %s" % (display_id, str(ex).splitlines()[0][:300]))
        finally:
            if slot:
                fcntl.flock(slot, fcntl.LOCK_UN)
        with mu:
            remaining.remove(display_id)
            done.add(display_id)
            save_pending(_merged_pending(waiting, done, remaining, retry), "đang QA")

    # Số ticket QA cùng lúc đọc lại từ cấu hình mỗi vài giây: đổi trên trang theo dõi thì áp dụng ngay cho các ticket
    # còn đang xếp hàng trong lượt này (ticket đang test vẫn chạy tiếp).
    gate = threading.Condition()
    active = {"n": 0}

    def limit():
        try:
            return max(1, int(envconf.deploy_config().get("qa_parallel") or dcfg.get("qa_parallel") or 1))
        except (ValueError, TypeError):
            return max(1, int(dcfg.get("qa_parallel") or 1))

    def gated(display_id):
        with gate:
            while active["n"] >= limit() and not stop["msg"]:
                gate.wait(3)
            active["n"] += 1
        try:
            one(display_id)
        finally:
            with gate:
                active["n"] -= 1
                gate.notify_all()

    with ThreadPoolExecutor(max_workers=min(10, max(1, len(tickets)))) as pool:
        list(pool.map(gated, list(tickets)))
    if stop["msg"]:
        msg = "server 86 thay đổi trong lúc QA %s — tạm dừng, %d ticket vào hàng chờ: %s" % (
            stop["msg"], len(remaining), ", ".join(remaining))
        log(msg)
        save_pending(_merged_pending(waiting, done, remaining, retry), stop["msg"])
        inbox(cfg, "**QA SERVER TẠM DỪNG** " + msg)
        notify("[QA SERVER] tạm dừng", "Server 86 vừa bị cập nhật / restart — QA tiếp ở lần chạy sau")
        return
    save_pending(_merged_pending(waiting, done, remaining, retry), "lỗi công cụ QA — thử lại" if retry else "")

def print_rollout_plan(parts, strategy=None, destructive=False):
    """--rollout-plan: in lệnh sẽ chạy (dry-run, không ssh). Mật khẩu không in."""
    dcfg = dict(DEFAULTS)
    dcfg.update(envconf.overlay(envconf.deploy_config(), ROLLOUT_ENV))
    dcfg.setdefault("base", "~/soar_v2.0")
    changes = []
    if "fe" in parts:
        changes.append({"part": "fe", "service": dcfg["fe_service"], "container": dcfg["fe_container"],
                        "old_image": "%s:<bản-cũ>" % dcfg["fe_image"], "new_image": "%s:<VERSION>" % dcfg["fe_image"],
                        "version": "<VERSION>"})
    if "be" in parts:
        changes.append({"part": "be", "service": dcfg["be_service"], "container": dcfg["be_container"],
                        "old_image": "%s:<bản-cũ>" % dcfg["be_image"], "new_image": "%s:<TAG>" % dcfg["be_image"],
                        "version": "<TAG>", "destructive": ["<dropColumn ...> (giả định)"] if destructive else []})
    strategy = strategy or dcfg.get("deploy_strategy") or "bluegreen"
    print("# strategy=%s, server %s:%s (dry-run: preflight / kiểm tra sống coi như đạt)"
          % (strategy, dcfg.get("host", "?"), dcfg["base"]))
    for ch, how in plan_parts(dcfg, changes, strategy):
        print("# %s → %s" % (ch["part"].upper(), how))
    rollout(dcfg, changes, dry=True, strategy=strategy)


def main():
    parser = argparse.ArgumentParser(description="Deploy lên server test 86 rồi QA tự động từng ticket.")
    parser.add_argument("--dry-run", action="store_true", help="chỉ liệt kê commit / ticket chưa deploy")
    parser.add_argument("--qa", nargs="+", metavar="TICKET", help="chỉ QA các ticket này trên bản đang chạy")
    parser.add_argument("--force", action="store_true",
                        help="cùng --qa: QA kể cả khi chưa có commit / bản đang chạy chưa có bản sửa")
    parser.add_argument("--review-watch", action="store_true",
                        help="QA ngay ticket của mình ở trạng thái qa_review_status_ids chưa QA trên bản đang chạy "
                             "(op-watch gọi định kỳ)")
    parser.add_argument("--resume", action="store_true",
                        help="QA tiếp hàng chờ (ticket dở do server bị cập nhật); có commit mới thì deploy trước")
    parser.add_argument("--rollout-plan", nargs="*", choices=["fe", "be"], metavar="fe|be",
                        help="chỉ IN các lệnh rollout (bluegreen / service / recreate) sẽ chạy trên server cho FE/BE "
                             "(mặc định cả hai) với image giả định — không ssh, không build, không đổi gì")
    parser.add_argument("--strategy", choices=STRATEGIES, help="cùng --rollout-plan: thử cách khác DEPLOY_STRATEGY")
    parser.add_argument("--project", help="chỉ deploy / QA dự án này (mã hoặc tiền tố, xem Cấu hình → Dự án)")
    parser.add_argument("--destructive", action="store_true",
                        help="cùng --rollout-plan: giả định BE có migration phá cấu trúc")
    args = parser.parse_args()
    if args.rollout_plan is not None:
        print_rollout_plan(args.rollout_plan or ["fe", "be"], args.strategy, args.destructive)
        return
    if args.review_watch:
        review_watch()
        return
    run_all(dry_run=args.dry_run, only=args.qa, force=bool(args.force and args.qa), project=args.project)


if __name__ == "__main__":
    main()
