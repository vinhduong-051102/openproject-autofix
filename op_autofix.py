#!/usr/bin/env python3
"""
op-autofix: chạy Claude Code (headless) để tự sửa một bug OpenProject tới bước COMMIT — không push, không
comment, không đổi trạng thái. Được op-watch gọi qua "on_change" khi có sự kiện NEW / REOPENED.

Một lượt chạy:
  1. Khoá (mỗi lúc chỉ một phiên; phiên sau xếp hàng chờ). Bỏ qua nếu ticket đó đang được xử lý.
  2. Tải ticket: mô tả, comment, ảnh đính kèm → ~/autofix/<SOAR-xxxx>-<giờ>/ticket/ (Claude không cần mạng / API key).
  3. Tạo worktree riêng: fe/ (nhánh auto/<SOAR-xxxx>-<giờ> từ FE_BRANCH + merge origin/FE_UPSTREAM) và
     be/ (từ BE_BRANCH + merge origin/BE_UPSTREAM). Xung đột chỉ ở danh sách include Liquibase (master.xml)
     → tự gộp cả hai bên; xung đột khác → bỏ merge, sửa trên nhánh của mình và ghi chú trong prompt / báo cáo.
  4. Chạy `claude -p` với quyền tối thiểu (dontAsk + danh sách cho phép, chặn push / mạng), giới hạn chi phí.
  5. Claude ghi report.md; script ghi 1 dòng vào inbox + thông báo desktop.

Dùng:
  op_autofix.py --wp 72107                 # chạy cho work package id 72107
  op_autofix.py --wp 72107 --dry-run       # chỉ chuẩn bị ticket + worktree + prompt, không gọi Claude
  op_autofix.py --clean SOAR-2789-1030-1205  # xoá worktree + nhánh auto của một lượt chạy
  op_autofix.py --status                   # các lượt đang chờ / đang chạy / vừa xong + thao tác gần nhất
  op_autofix.py --follow [SOAR-2789]       # xem trực tiếp từng thao tác của phiên đang chạy
"""

import argparse
import fcntl
import getpass
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402
import repos as R  # noqa: E402  danh sách repo (cấu hình động; mặc định FE + BE như trước)  (nạp .env)
import op_watch  # noqa: E402  (dùng chung cấu hình + client OpenProject)

# systemd có thể khởi động service trước khi PATH người dùng có ~/.local/bin (claude, agy): thêm vào đầu PATH.
for _bin in reversed(envconf.extra_path()):
    if _bin not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")

AUTOFIX_DEFAULTS = {
    "enabled": True,
    "events": ["NEW", "REOPENED"],
    "root": "~/autofix",
    # Repo + nhánh: đặt trong .env (FE_REPO, FE_BRANCH…) — giá trị dưới chỉ là mặc định khi chưa cấu hình.
    "fe_repo": os.path.expanduser("~/soar-fe"),
    "fe_branch": "dev-ss-%s" % getpass.getuser(),
    "fe_upstream": "dev-ss",
    "be_repo": os.path.expanduser("~/SOAR_BE"),
    "be_branch": "develop-ss-%s" % getpass.getuser(),
    "be_upstream": "develop-ss",
    "be_subdir": "SOAR",
    # Danh sách repo (xem repos.py). None = dựng FE + BE từ các khoá fe_* / be_* ở trên.
    "repos": None,
    "max_budget_usd": 5,
    # Hạn mức 5 giờ của gói đã dùng quá mức này thì hoãn (không chạy Claude), để dành cho làm việc trực tiếp.
    "pause_above_utilization": 0.8,
    # Kết quả CẦN QUYẾT ĐỊNH: comment lý do lên ticket và chuyển sang trạng thái này (94 = UNREVIEWED) để chờ duyệt.
    # Đặt null để không đụng vào OpenProject.
    "review_status_id": 94,
    "model": "",
    "claude": "claude",
    # Chỉ tự sửa ticket có số SOAR-<n> từ mức này trở lên (ticket cũ hơn bỏ qua; chạy tay MANUAL vẫn được).
    "min_ticket_number": 1900,
    # Số lượt autofix / test lại (Claude + Gemini) chạy cùng lúc. Mỗi lượt có worktree riêng; fetch / tạo worktree /
    # push vào nhánh chung được khoá riêng nên không giẫm lên nhau.
    "max_parallel": 3,
    "node_bin": "~/.nvm/versions/node/v22.23.4/bin",
    # Kết quả ĐÃ SỬA: tool tự build / test lại, push lên fe_branch / be_branch, comment + chuyển trạng thái này
    # (95 = Resolved). Claude trong lượt chạy vẫn không được push; chỉ tool làm sau khi tự kiểm tra lại.
    "auto_ship": True,
    "resolved_status_id": 95,
    # Trường tuỳ chỉnh (kiểu User) trên ticket ghi người làm: autofix nhận ticket thì điền mình vào (trống mới điền,
    # đã có dev khác thì giữ nguyên trừ khi dev_field_overwrite). Để trống tên trường = tắt.
    "dev_field": "Dev",
    "dev_field_overwrite": False,
    # Ghi giờ làm (Spent time) lên ticket: thời gian Claude sửa → Development, kiểm tra + Gemini test lại + QA server →
    # Testing (tên activity trên OpenProject). set_dates: điền Start date / Finish date nếu đang trống.
    "log_time": True,
    "time_activity_dev": "Development",
    "time_activity_test": "Testing",
    "set_dates": True,
    # Gemini (Antigravity CLI `agy`, chế độ plan = chỉ đọc) kiểm tra lại bản sửa sau khi build/test qua, trước khi push.
    # KHÔNG ĐẠT → không push (gemini_block_on_fail). Gemini lỗi / hết hạn mức → vẫn push, ghi chú trong báo cáo.
    "gemini_enabled": True,
    "gemini_cmd": "~/.local/bin/agy",
    "gemini_model": "gemini-3.8-flash-high",
    "gemini_timeout": 900,
    "gemini_block_on_fail": True,
    # Gemini kết luận thế này → Claude xem lại từng ý, tự quyết sửa thêm / giữ nguyên / cần người quyết định.
    "followup_on": ["KHÔNG ĐẠT", "CẦN XEM"],
    # Hạn mức 7 ngày đã dùng quá mức này thì bỏ bước Claude xem lại (giữ hạn mức cho làm việc trực tiếp).
    "followup_skip_above_7d": 0.95,
    # Bỏ bước Claude xem lại vì hết hạn mức → ticket vào hàng chờ và chuyển trạng thái này (97 = TO DO) cho tới khi
    # Claude xem lại xong (khi đó chuyển Resolved, hoặc UNREVIEWED nếu cần quyết định). null = không đổi trạng thái.
    "followup_status_id": 97,
    # Báo cáo tổng hợp mỗi lượt (Claude + build/test + Gemini) để mở nhanh.
    "report_dir": "~/Downloads/autofix-reports",
    # Các gói Claude: mỗi gói một thư mục cấu hình đã /login bằng tài khoản riêng (CLAUDE_CONFIG_DIR).
    # Gói đang dùng do trang theo dõi (op_dashboard.py) chọn, lưu ở <root>/.profile.json.
    "profiles": [{"name": "Gói chính", "config_dir": "~/.claude"}],
    # Gói đang dùng chạm ngưỡng (5 giờ ≥ pause_above_utilization, 7 ngày ≥ switch_above_7d, hoặc bị chặn) thì tự
    # chuyển sang gói khác còn hạn mức; tắt thì hoãn như cũ.
    "auto_switch": True,
    "switch_above_7d": 0.95,
    # CẦN QUYẾT ĐỊNH với NGƯỜI QUYẾT: DEV (agent cần hỗ trợ) → tool tự chạy thêm một lượt "Claude hỗ trợ": tải mọi file
    # đính kèm, ticket liên quan, cho gọi GET API server test (soar_api.py), rồi Claude gỡ vướng và sửa tiếp. Vẫn kẹt
    # mới chuyển UNREVIEWED. Trang theo dõi có nút chạy lại bước này kèm ghi chú (op_autofix.py --assist RUN).
    "assist_auto": True,
    "assist_max_related": 8,
}

ALLOWED_TOOLS = [
    "Read", "Glob", "Grep",
    "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git show*)", "Bash(git add*)",
    "Bash(git commit*)", "Bash(git branch --show-current)", "Bash(git rev-parse*)", "Bash(git blame*)",
    "Bash(git grep*)", "Bash(npm run build*)", "Bash(npm run lint*)", "Bash(npx tsc*)", "Bash(npx eslint*)",
    "Bash(mvn *)", "Bash(cd *)", "Bash(ls *)", "Bash(grep *)", "Bash(wc *)", "Bash(head *)", "Bash(tail *)",
    # Prompt bảo dùng `git -C <worktree> …` nên dạng -C cũng phải được phép (không có thì add/commit/rev-parse bị chặn).
    "Bash(git -C* status*)", "Bash(git -C* diff*)", "Bash(git -C* log*)", "Bash(git -C* show*)", "Bash(git -C* add*)",
    "Bash(git -C* commit*)", "Bash(git -C* rev-parse*)", "Bash(git -C* blame*)", "Bash(git -C* grep*)",
]
DISALLOWED_TOOLS = [
    # Dạng `git -C <dir> <lệnh>` cũng phải chặn (quy tắc cho phép git -C bên dưới chỉ để đọc lịch sử).
    "Bash(git -C* push*)", "Bash(git -C* fetch*)", "Bash(git -C* pull*)", "Bash(git -C* remote*)",
    "Bash(git -C* checkout*)", "Bash(git -C* switch*)", "Bash(git -C* branch*)", "Bash(git -C* reset*)",
    "Bash(git -C* worktree*)",
    "Bash(git push*)", "Bash(git remote*)", "Bash(git fetch*)", "Bash(git pull*)", "Bash(git reset --hard*)",
    "Bash(git checkout*)", "Bash(git switch*)", "Bash(git worktree*)",
    "Bash(curl*)", "Bash(wget*)", "Bash(ssh*)", "Bash(scp*)", "Bash(rm -rf*)", "Bash(docker*)",
    "WebFetch", "WebSearch",
]

PROMPT = """Bạn đang tự động sửa một bug OpenProject của dự án {project_name}, chạy nền không có người theo dõi.

## Ticket
{display_id} (#{wp_id}, trạng thái {status}, sự kiện {event}) — {subject}
Toàn bộ mô tả + comment: {run_dir}/ticket/ticket.md ; ảnh đính kèm: {run_dir}/ticket/images/ (đọc bằng Read).
Có mục "Thiết kế Figma" trong ticket.md thì xem ảnh frame ({run_dir}/ticket/figma/) và làm ĐÚNG thông số ghi kèm (kích thước,
khoảng cách, màu, font) — dùng token / biến có sẵn của dự án gần nhất với giá trị đó, không hard-code nếu đã có token tương ứng.
Đọc hết comment: ticket Reopened thì comment gần nhất của tester là yêu cầu cần làm.
Nếu đã có comment **[Cần duyệt]** (autofix lần trước nêu các phương án) và SAU đó có comment của {assignee}
(người được giao) chọn phương án / đưa hướng xử lý, thì đó là QUYẾT ĐỊNH ĐÃ DUYỆT: làm đúng theo quyết định đó, không
hỏi lại, không kết luận "CẦN QUYẾT ĐỊNH" cho cùng vấn đề nữa (chỉ khi quyết định đó tự nó gây vấn đề mới).

## Code (đã sẵn sàng, đã merge code mới nhất của nhánh dev chung){merge_note}
{code_repos}
Chỉ sửa trong {n_repos} thư mục này. Không tạo nhánh / worktree khác, không checkout, không fetch/pull/push.

## Làm
1. Tìm nguyên nhân gốc trong code (không đoán). Ticket nhắc tới API/màn hình nào thì tìm đúng chỗ đó.
2. Sửa tối thiểu, đúng phạm vi ticket, theo phong cách code xung quanh{repo_rules}.
3. Kiểm tra: {repo_checks}.
   KHÔNG dùng `cd … && …` (bị chặn): git luôn dùng {git_dirs}, Maven dùng `-f`.
   {cwd_note}
   Một lệnh bị từ chối quyền KHÔNG có nghĩa là Bash bị cấm — đổi sang đúng dạng lệnh được phép ở trên rồi làm tiếp.
{bash_rules}
   Vẫn không chạy được bước kiểm tra nào thì VẪN commit phần đã sửa (công cụ tự build + test lại trước khi push) và
   ghi rõ trong báo cáo; KHÔNG kết luận KHÔNG SỬA ĐƯỢC chỉ vì không chạy được build / test.
4. TỰ KIỂM TRA HỒI QUY (bắt buộc, trước khi commit) — Gemini sau đó CHỈ test lại ticket, không rà hồi quy thay bạn:
   - Với mỗi hàm / component / hook / API / DTO / câu truy vấn / hằng số / key i18n / CSS dùng chung đã đổi: tìm MỌI chỗ
     dùng (`git -C <worktree> grep -n …`, Grep) — FE: các màn hình / component khác dùng chung, route; BE: caller,
     controller, service, mapper, test, chỗ đọc cùng bảng / cột.
   - Từng chỗ: hành vi cũ còn đúng không (dữ liệu vào / ra, mã lỗi, định dạng, phân quyền, giao diện sáng / tối). Chỗ
     nào phải đổi theo cho đúng thì sửa luôn (vẫn trong phạm vi ticket); chỗ nào đổi hành vi ngoài ticket thì KHÔNG sửa,
     ghi vào "Cần quyết định / rủi ro".
   - Chạy test của mọi lớp bị ảnh hưởng, không chỉ test của file đã sửa (BE: `-Dtest='ATest,BTest,…'` gồm test của các
     caller / controller liên quan; FE: `npm run build` + tsc). Test lỗi sẵn từ trước (không do bản sửa) thì ghi rõ.
5. Commit trên nhánh hiện tại của worktree (`git -C <worktree> add …` rồi `git -C <worktree> commit …`), mỗi ticket một
   commit mỗi repo, message:
   `[{display_id}] - {subject}` + dòng trống + mô tả ngắn tiếng Việt không dấu + dòng trống +
   `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Không được
- Không push, không gọi mạng, không sửa OpenProject, không đăng nhập bằng tài khoản của người khác.
- Nếu ticket cần một QUYẾT ĐỊNH (trái với code/thiết kế người khác cố ý làm, đổi hành vi sản phẩm, thiếu thông tin
  để tái hiện, cần dữ liệu môi trường thật) thì KHÔNG commit phần đó — chỉ ghi rõ vào báo cáo.

## Báo cáo (bắt buộc, kể cả khi không sửa được)
Ghi {run_dir}/report.md bằng tiếng Việt:
- Dòng đầu: `KẾT QUẢ: ĐÃ SỬA` | `KẾT QUẢ: CẦN QUYẾT ĐỊNH` | `KẾT QUẢ: KHÔNG SỬA ĐƯỢC` (| `KẾT QUẢ: QA SAI` | `KẾT QUẢ: CẦN TEST TAY` khi có mục QA bên dưới)
- Nguyên nhân; Đã sửa (file, cách sửa); Commit (repo + hash); Kiểm tra (lệnh + kết quả thật);
  `## Kiểm tra hồi quy` — các chỗ dùng đã rà (file / màn hình / API → còn đúng / đã sửa theo / cần quyết định) và test
  đã chạy cho phần bị ảnh hưởng (lệnh + kết quả);
  Cần quyết định / rủi ro;
{decider_rules}
  `## Comment cho tester` — đoạn này được đăng nguyên văn lên ticket cho TESTER (không phải dev) đọc: tiếng Việt, ngắn,
  nói theo màn hình / thao tác / kết quả nhìn thấy. Dòng đầu bắt buộc `**Nguyên nhân lỗi:** …` (1–2 câu: vì sao bị lỗi,
  nói theo hiện tượng — vd "hệ thống chưa kiểm tra quyền xem trước khi trả số liệu nên tài khoản thiếu quyền thấy số 0"),
  sau đó đã sửa gì, test lại thế nào, lưu ý. KHÔNG tên file, class, hàm,
  biến, commit, stack trace hay giải thích code. API thì chỉ nêu endpoint + mã HTTP + thông báo trả về.
"""


RESUME_NOTE = """

## Chạy tiếp (lượt trước bị dừng giữa chừng vì hết hạn mức)
Worktree có thể đã có thay đổi / commit dở của lượt trước: xem `git -C <worktree> status` và
`git -C <worktree> log --oneline -3` trước, làm tiếp từ đó (không làm lại từ đầu, không commit trùng).
"""


INTERRUPTED_NOTE = """

## Chạy tiếp (lượt trước bị ngắt giữa chừng — máy / dịch vụ khởi động lại)
Worktree có thể đã có thay đổi / commit dở của lượt trước: xem `git -C <worktree> status` và
`git -C <worktree> log --oneline -3` trước, làm tiếp từ đó (không làm lại từ đầu, không commit trùng). Thay đổi chưa
commit có thể chưa xong (bị cắt ngang khi đang sửa): đọc lại cho chắc trước khi dùng. report.md (nếu có) là của lượt
bị ngắt — ghi lại cho đúng kết quả cuối cùng.
"""


QA_FAIL_NOTE = """

## QA trên server test kết luận {qa_verdict} (lý do lượt chạy này)
Bản sửa trước của ticket đã deploy lên server test và QA tự động (Gemini) kết luận {qa_verdict}.
- Báo cáo QA: {qa_report}
- Từng request QA đã gửi + response thật: {qa_rounds}
- Bản đang chạy trên server + commit sửa đã có trong bản đó chưa (kiểm tra bằng git, đây là SỰ THẬT): {deploy_check}
1. Kiểm chứng báo cáo QA với ticket + code (`git -C <worktree> log --grep='[{display_id}]'` để xem các commit đã sửa):
   QA có test đúng yêu cầu ticket, đúng chỗ, hiểu đúng kết quả mong đợi không? QA có thể sai.
2. Xem request QA THỰC GỬI trong file request (dòng đầu mỗi kết quả mô tả đúng thứ công cụ đã gửi): công cụ gửi khác ý
   định của QA (tên file, nội dung, tham số bị đổi / cắt) → lỗi ở công cụ QA.
3. KHÔNG được kết luận "server chưa có bản sửa / chưa deploy" khi file deploy-check ghi commit sửa là **CÓ**. Khi đó
   kết quả sai trên server phải có nguyên nhân khác: bản sửa chưa phủ hết luồng thật (nhánh code khác, lớp khác,
   cấu hình server, dữ liệu cũ), hoặc công cụ QA gửi sai. Chỉ kết luận do môi trường khi deploy-check ghi CHƯA.
4. QA đúng → sửa phần còn thiếu / sai, commit như hướng dẫn ở trên; report.md dòng đầu `KẾT QUẢ: ĐÃ SỬA`.
5. QA sai (test sai cách, hiểu sai ticket, công cụ QA gửi sai, deploy-check ghi CHƯA) → KHÔNG sửa; report.md dòng
   đầu `KẾT QUẢ: QA SAI`, nêu rõ nguyên nhân thuộc loại nào + bằng chứng (request thực gửi / dòng deploy-check), kèm đề
   xuất cách test đúng cho tester.
6. Phần cần người quyết định → `KẾT QUẢ: CẦN QUYẾT ĐỊNH` như thường.
7. QA kết luận KHÔNG TỰ TEST ĐƯỢC: xét QA có tự test được không — tài khoản / quyền khác, endpoint đúng (đối chiếu
   controller trong code), bật cấu hình rồi trả lại, tạo dữ liệu QA_AUTO_*, SEARCH mã FE đã deploy… Làm được mà QA bỏ
   qua → `KẾT QUẢ: QA SAI` (kèm cách test đúng). Đọc code thấy bản sửa còn thiếu → sửa như bước 4. Thật sự cần người
   (thao tác giao diện / hệ thống ngoài / dữ liệu không tạo được qua API) → `KẾT QUẢ: CẦN TEST TAY`, mục
   `## Comment cho tester` ghi bước test tay + kết quả mong đợi, kèm `## Người quyết định` + `## Câu hỏi cho tester`
   như hướng dẫn ở phần Báo cáo (ai test tay được). KHÔNG sửa code khi chỉ là không test được.
8. Khi `KẾT QUẢ: QA SAI`: thêm mục `## Rule cho QA` gồm 1–3 gạch đầu dòng là rule TỔNG QUÁT để QA tự động không lặp
   lại lỗi này ở ticket khác (không nhắc mã ticket / id dữ liệu cụ thể; nêu cách làm đúng). QA tự động KHÔNG chạy lệnh
   được, chỉ gửi request API; chuỗi dài viết `{{{{rep:<chuỗi>:<số lần>}}}}`. Các rule đang có (đừng viết trùng):
{qa_rules}
"""


def write_rules(run_dir, acfg=None):
    scope = "/" + os.path.abspath(run_dir) + "/**"
    base = os.path.abspath(run_dir)
    keys = [r["key"] for r in R.repos(acfg or {})]
    # git của các worktree chạy bằng `git -C <worktree>` (lệnh dạng `cd … && …` không qua được kiểm tra quyền).
    # Chỉ đúng các worktree của lượt chạy; push/fetch/checkout/branch/reset… vẫn bị chặn ở DISALLOWED_TOOLS.
    read_git = ["Bash(git -C %s/%s %s*)" % (base, sub, cmd)
                for sub in keys for cmd in ("log", "show", "diff", "status", "blame", "add", "commit", "grep")]
    npm = ["Bash(npm --prefix %s/%s run %s*)" % (base, k, c) for k in keys for c in ("build", "lint")]
    return ["Edit(%s)" % scope, "Write(%s)" % scope] + read_git + npm + R.allowed_bash(acfg or {})


def log(msg):
    op_watch.log("[autofix] " + msg)


def run(cmd, cwd=None, check=True):
    res = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if check and res.returncode != 0:
        raise RuntimeError("%s → %s" % (" ".join(cmd), res.stdout.strip()[-800:]))
    return res.stdout


def notify(title, body):
    try:
        subprocess.run(["notify-send", "-a", "OpenProject", "-u", "normal", title, body], timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def inbox(cfg, line):
    if cfg.get("inbox"):
        with open(os.path.expanduser(cfg["inbox"]), "a", encoding="utf-8") as f:
            f.write("- [ ] %s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M"), line))


def for_tester(text):
    """Bỏ chi tiết dành cho dev khỏi nội dung đăng lên ticket: link / đường dẫn file, tên class đầy đủ, hash commit."""
    text = re.sub(r"\[([^\]]*)\]\((?:file://)?/[^)]*\)", r"\1", text)
    text = re.sub(r"`?(?:[\w.-]+/)*[\w-]+\.(?:java|tsx?|jsx?|py|xml|ya?ml|css|scss)\b(?::[\d-]+|#L[\d-]+)?`?", "", text)
    text = re.sub(r"`?\b(?:com|org|net)\.[a-z]\w*(?:\.[a-z]\w*)+\.([A-Z]\w+)`?", r"`\1`", text)
    text = re.sub(r"`?\b[0-9a-f]{7,40}\b`?(?=[\s,.)]|$)", "", text)
    text = re.sub(r"\((?:dev-ss|develop-ss)[\w-]*\)", "", text)
    text = re.sub(r"\(\s*[,;]?\s*\)", "", text)  # ngoặc rỗng còn lại sau khi bỏ tên file / hash
    text = re.sub(r"[ \t]+([,.)])", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def plain_md(text):
    """Bỏ link file / định dạng nhấn mạnh để đưa vào comment OpenProject; giữ `code`."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\*\*|(?<![\w`])[*_](?=\S)|(?<=\S)[*_](?![\w`])", "", text)
    return re.sub(r"\s+", " ", text).strip()


def first_sentence(text, limit=220):
    text = plain_md(text)
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    text = m.group(1) if m and len(m.group(1)) > 30 else text
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def summarize_gemini(text, limit=4, own_only=False):
    """
    Các lý do Gemini kết luận CẦN XEM / KHÔNG ĐẠT, mỗi lý do một dòng ngắn cho comment ticket.
    Ưu tiên mục "Tóm tắt lý do" Gemini tự viết; không có thì rút từ "Lỗi / rủi ro": mức độ + hậu quả (hoặc chi tiết).
    """
    own = report_section(text, "Tóm tắt")
    if own:
        items = [plain_md(re.sub(r"^\s*([-*]|\d+[.)])\s+", "", line)) for line in own.splitlines()
                 if re.match(r"^\s*([-*]|\d+[.)])\s+", line)]
        if items:
            return items[:limit]
    if own_only:
        return []
    body = report_section(text, "Lỗi")
    out = []
    for block in re.split(r"\n(?=\d+[.)]\s)", body):
        head = block.strip().splitlines()[0] if block.strip() else ""
        if not re.match(r"\d+[.)]\s", head):
            continue
        sev = re.search(r"Mức độ:\s*([^*\n]+)", head)
        parts = {}
        for m in re.finditer(r"^\s*[-*]\s*\*?([^*:\n]+):\*?\s*(.+)$", block, re.M):
            parts[m.group(1).strip().lower()] = m.group(2)
        detail = parts.get("hậu quả") or parts.get("chi tiết") or re.sub(r"^\d+[.)]\s*", "", head)
        line = first_sentence(detail)
        if sev:
            line = "%s: %s" % (plain_md(sev.group(1)), line)
        out.append(line)
    # Lý do chính Gemini ghi ở "Lý do kết luận / đánh dấu …:" — cùng dòng, hoặc gạch đầu dòng ngay sau đó.
    m = re.search(r"Lý do (?:kết luận|đánh dấu)[^:\n]*:\**[ \t]*(.*)\n?(?:[ \t]*[-*][ \t]*(.+))?", text)
    if m:
        main = m.group(1).strip() or (m.group(2) or "")
        if plain_md(main):
            out.insert(0, "Lý do chính: " + first_sentence(main.rstrip(":"), 260))
    return out[:limit]


def waiting_comment(run_dir, pushed):
    """Các dòng comment cho ticket chờ Claude xem lại: tóm tắt lý do Gemini thay vì chỉ ghi kết luận."""
    path = os.path.join(run_dir, "gemini.md")
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    m = re.search(r"KẾT LUẬN:\s*(KHÔNG ĐẠT|ĐẠT|CẦN XEM)", text[:500])
    verdict = {"KHÔNG ĐẠT": "chưa đạt", "CẦN XEM": "cần xem lại"}.get(m.group(1) if m else "", "cần xem lại")
    head = "**[Autofix] Chờ xem lại** — " if not pushed else "**Chờ xem lại** — "
    # Chỉ dùng "Tóm tắt lý do" Gemini viết cho tester; lượt cũ không có mục này thì không liệt kê chi tiết kỹ thuật.
    items = [for_tester(i) for i in summarize_gemini(text, own_only=True)]
    lines = [head + "AI kiểm tra lại và đánh giá bản sửa %s%s" % (verdict, ":" if items else ".")]
    lines += ["- " + item for item in items]
    lines += ["", "Bản sửa sẽ được rà lại %s(sửa thêm nếu cần), xong sẽ chuyển ticket về Resolved. "
              "Trong lúc chờ ticket để TO DO." % ("các điểm trên " if items else "")]
    if not pushed:
        lines[-1] = "Bản sửa chưa được đưa lên môi trường. " + lines[-1]
    return lines


def report_section(text, heading):
    """Nội dung một mục "## <heading>..." trong report.md (tới mục ## kế tiếp)."""
    m = re.search(r"^##\s*" + re.escape(heading) + r"[^\n]*\n(.*?)(?=^##\s|\Z)", text, re.S | re.M)
    return m.group(1).strip() if m else ""


_TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_TABLE_SEP = re.compile(r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$")


def op_markdown(text):
    """Chuẩn hoá markdown trước khi đăng comment lên OpenProject (báo cáo của Claude hay viết kiểu OP không hiểu):
    - Bảng nằm thụt lề trong mục danh sách → OP hiện nguyên dấu |: kéo bảng ra lề trái, có dòng trống trước / sau.
    - Đường dẫn chứa * (vd /api/v1/auth/**) → OP hiểu ** là chữ đậm: bọc trong `…` (ngoài đoạn code sẵn có)."""
    lines = (text or "").split("\n")
    out, i, fence = [], 0, False
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and _TABLE_ROW.match(line) and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            j = i
            while j < len(lines) and (_TABLE_ROW.match(lines[j]) or _TABLE_SEP.match(lines[j])):
                j += 1
            if out and out[-1].strip():
                out.append("")
            out.extend(l.strip() for l in lines[i:j])
            if j < len(lines) and lines[j].strip():
                out.append("")
            i = j
            continue
        out.append(line)
        i += 1
    text, res, fence = "\n".join(out), [], False
    for block in re.split(r"(```.*?```)", text, flags=re.S):
        if block.startswith("```"):
            res.append(block)
            continue
        parts = block.split("`")
        for k in range(0, len(parts), 2):  # chỉ phần ngoài `code`
            parts[k] = re.sub(r"(?<![\w`])(/[\w.{}:-]*(?:/[\w.{}:-]*)*\*[\w.*/{}:-]*)", r"`\1`", parts[k])
        res.append("`".join(parts))
    return "".join(res)


def op_write(api, method, path, body, normalize=True):
    """Ghi lên OpenProject. Comment (POST …/activities) được chuẩn hoá markdown, trừ khi normalize=False
    (bình luận người dùng tự gõ trên dashboard: giữ nguyên văn)."""
    if normalize and method == "POST" and path.endswith("/activities") and isinstance(
            ((body or {}).get("comment") or {}).get("raw"), str):
        body = dict(body, comment=dict(body["comment"], raw=op_markdown(body["comment"]["raw"])))
    req = op_watch.urllib.request.Request(api.url + path, data=json.dumps(body).encode("utf-8"), method=method,
                                          headers=dict(api.headers, **{"Content-Type": "application/json"}))
    with op_watch.urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def set_wp_status(api, wp_id, status_id):
    lock_version = api.get("/api/v3/work_packages/%s" % wp_id)["lockVersion"]
    op_write(api, "PATCH", "/api/v3/work_packages/%s" % wp_id, {
        "lockVersion": lock_version, "_links": {"status": {"href": "/api/v3/statuses/%s" % status_id}}})


def wp_status_id(api, wp_id):
    href = api.get("/api/v3/work_packages/%s" % wp_id)["_links"]["status"]["href"]
    return int(href.rstrip("/").split("/")[-1])


def mark_for_review(api, wp_id, status_id, report_text, run_name, run_dir=None):
    """Ticket cần người duyệt: comment lý do (mục "Cần quyết định" của báo cáo) + chuyển trạng thái chờ duyệt."""
    reason = report_section(report_text, "Cần quyết định") or report_section(report_text, "Nguyên nhân") \
        or "Xem báo cáo autofix %s." % run_name
    if len(reason) > 4000:
        reason = reason[:4000] + "\n…"
    decider, why, question = review_decider(report_text)
    comment = "**[Cần duyệt]** Ticket cần quyết định trước khi sửa:\n\n" + for_tester(reason)
    if decider:
        comment += "\n\n_Người quyết: %s%s_" % ("Tester — sẽ xin ý kiến qua Telegram" if decider == "tester" else "Dev",
                                               (" (" + plain_md(why) + ")") if why else "")
    comment += "\n".join(evidence_lines(api, wp_id, report_text, run_dir))
    op_write(api, "POST", "/api/v3/work_packages/%s/activities" % wp_id, {"comment": {"raw": comment}})
    w = api.get("/api/v3/work_packages/%s" % wp_id)
    op_write(api, "PATCH", "/api/v3/work_packages/%s" % wp_id, {
        "lockVersion": w["lockVersion"], "_links": {"status": {"href": "/api/v3/statuses/%s" % status_id}}})
    try:  # xin ý kiến tester qua Telegram (tg_requests) — lỗi ở đây không được làm hỏng lượt autofix
        import tg_requests
        tg_requests.propose_ask(w.get("displayId") or "WP-%s" % wp_id, int(wp_id), w.get("subject", ""), decider or "dev",
                                why, for_tester(question), for_tester(reason), "autofix", run_name)
    except Exception as ex:  # noqa: BLE001
        log("xin ý kiến qua Telegram lỗi: %s" % ex)


def review_decider(report_text):
    """(tester | dev | "", lý do, câu hỏi cho tester) từ mục "Người quyết định" / "Câu hỏi cho tester" của báo cáo."""
    sec = report_section(report_text, "Người quyết định")
    m = re.search(r"NGƯỜI QUYẾT:\s*(TESTER|DEV)", sec or report_text)
    if not m:
        return "", "", ""
    why = re.sub(r"^.*NGƯỜI QUYẾT:\s*(TESTER|DEV)\W*", "", sec, count=1, flags=re.S).strip() if sec else ""
    question = report_section(report_text, "Câu hỏi cho tester") if m.group(1) == "TESTER" else ""
    return m.group(1).lower(), why[:500], question[:2000]


_SCHEMA_KEYS = {}
_ME = {}


def dev_field_key(api, w, name):
    """Khoá customFieldN của trường kiểu User tên `name` trong schema của ticket (cache theo schema)."""
    href = (w.get("_links") or {}).get("schema", {}).get("href")
    if not href:
        return None
    if href not in _SCHEMA_KEYS:
        sch = api.get(href)
        _SCHEMA_KEYS[href] = next((k for k, v in sch.items() if k.startswith("customField") and isinstance(v, dict)
                                   and (v.get("name") or "").strip().lower() == name.strip().lower()
                                   and v.get("type") == "User" and v.get("writable")), None)
    return _SCHEMA_KEYS[href]


def set_dev(api, acfg, wp_id):
    """Điền mình vào trường Dev của ticket. Trả mô tả ngắn (không ném lỗi — chỉ là cập nhật phụ)."""
    name = (acfg.get("dev_field") or "").strip()
    if not name:
        return "tắt"
    try:
        w = api.get("/api/v3/work_packages/%s" % wp_id)
        key = dev_field_key(api, w, name)
        if not key:
            return "ticket không có trường %s" % name
        if "id" not in _ME:
            _ME["id"] = api.me()["id"]
        me = "/api/v3/users/%s" % _ME["id"]
        cur = ((w.get("_links") or {}).get(key) or {}).get("href")
        body, notes = {"lockVersion": w["lockVersion"]}, []
        today = datetime.now().strftime("%Y-%m-%d")
        if acfg.get("set_dates", True) and not w.get("startDate") and not (w.get("dueDate") and w["dueDate"] < today):
            body["startDate"] = today  # ngày bắt đầu làm (chỉ khi đang trống)
            notes.append("Start date = hôm nay")
        if cur == me:
            notes.insert(0, "đã là mình")
        elif cur and not acfg.get("dev_field_overwrite"):
            notes.insert(0, "giữ dev hiện có (%s)" % (((w.get("_links") or {}).get(key) or {}).get("title") or cur))
        else:
            body["_links"] = {key: {"href": me}}
            notes.insert(0, "đã điền %s = mình" % name)
        if len(body) > 1:
            op_write(api, "PATCH", "/api/v3/work_packages/%s" % w["id"], body)
        return ", ".join(notes)
    except Exception as ex:  # noqa: BLE001
        return "lỗi: %s" % str(ex).splitlines()[0][:200]


_TIME_ACTS = {}


def time_activity(api, wp_id, name):
    """href activity ghi giờ theo tên (lấy từ form time_entries — chỉ kiểm tra, không tạo bản ghi)."""
    if not _TIME_ACTS:
        form = op_write(api, "POST", "/api/v3/time_entries/form",
                        {"_links": {"entity": {"href": "/api/v3/work_packages/%s" % wp_id}}})
        vals = ((((form.get("_embedded") or {}).get("schema") or {}).get("activity") or {}).get("_embedded") or {}) \
            .get("allowedValues") or []
        _TIME_ACTS.update({(a.get("name") or "").lower(): a["_links"]["self"]["href"] for a in vals})
    return _TIME_ACTS.get((name or "").lower())


def log_time(api, acfg, wp_id, seconds, kind, comment):
    """Ghi giờ làm lên ticket (Spent time). kind: dev | test. Không ném lỗi — chỉ là cập nhật phụ."""
    if not acfg.get("log_time", True) or not wp_id or seconds is None or seconds < 30:
        return ""
    try:
        w = api.get("/api/v3/work_packages/%s" % wp_id)
        act = time_activity(api, w["id"], acfg.get("time_activity_dev" if kind == "dev" else "time_activity_test"))
        minutes = max(1, int(round(seconds / 60.0)))
        body = {"hours": "PT%dM" % minutes, "spentOn": datetime.now().strftime("%Y-%m-%d"),
                "comment": {"raw": comment[:250]},
                "_links": {"entity": {"href": "/api/v3/work_packages/%s" % w["id"]}}}
        if act:
            body["_links"]["activity"] = {"href": act}
        op_write(api, "POST", "/api/v3/time_entries", body)
        log("%s: ghi %d phút (%s) — %s" % (w.get("displayId") or wp_id, minutes, kind, comment[:80]))
        return "%d phút" % minutes
    except Exception as ex:  # noqa: BLE001
        log("ghi giờ làm lỗi (%s): %s" % (wp_id, str(ex).splitlines()[0][:200]))
        return ""


def set_finish_date(api, acfg, wp_id):
    """Bản sửa đã push + Resolved: điền Finish date = hôm nay nếu đang trống."""
    if not acfg.get("set_dates", True):
        return
    try:
        w = api.get("/api/v3/work_packages/%s" % wp_id)
        today = datetime.now().strftime("%Y-%m-%d")
        if w.get("dueDate") or (w.get("startDate") and w["startDate"] > today):
            return
        op_write(api, "PATCH", "/api/v3/work_packages/%s" % w["id"], {"lockVersion": w["lockVersion"], "dueDate": today})
    except Exception as ex:  # noqa: BLE001
        log("điền Finish date lỗi (%s): %s" % (wp_id, str(ex).splitlines()[0][:200]))



def assignee_name(api):
    """Tên người được giao (người duyệt quyết định) trong prompt: OP_ASSIGNEE_NAME, không đặt thì tên tài khoản API."""
    if envconf.ASSIGNEE_NAME:
        return envconf.ASSIGNEE_NAME
    if "name" not in _ME:
        try:
            me = api.me()
            _ME.update(id=me["id"], name=me.get("name") or "người được giao")
        except Exception:  # noqa: BLE001
            return "người được giao"
    return _ME["name"]

def make_trees(acfg, run_dir, branch):
    """Worktree cho mọi repo của lượt chạy (<run>/<key>) + thư mục dùng chung (vd node_modules). Trả ghi chú."""
    notes = []
    for r, d in R.trees(acfg, run_dir):
        notes.append(make_worktree(r["path"], r["branch"], r["upstream"], d, branch, acfg))
        R.link_shared(r, d)
        err = R.run_setup(r, d, build_env(acfg), os.path.join(run_dir, "setup.log"))
        if err:
            log(err)
            notes.append(err)
    return notes


def tree_commits(acfg, run_dir, display_id):
    """{nhãn repo: [commit ticket]} trên các worktree của lượt chạy."""
    return {r["label"]: ticket_commits(d, display_id) for r, d in R.trees(acfg, run_dir)}


def any_fix(acfg, run_dir):
    return any(fix_commits(d, r["branch"]) for r, d in R.trees(acfg, run_dir) if os.path.isdir(d))


def fetch_ticket(api, wp_id, out_dir, figma_designs=True):
    """Mô tả + toàn bộ comment/thay đổi + ảnh đính kèm, để Claude đọc offline."""
    os.makedirs(os.path.join(out_dir, "images"), exist_ok=True)
    wp = api.get("/api/v3/work_packages/%s" % wp_id)
    acts = api.activities(wp_id)
    image_ids = set()

    def localize(text):
        def repl(m):
            image_ids.add(m.group(1))
            return "![ảnh %s](images/%s.png)" % (m.group(1), m.group(1))
        return re.sub(r'<img[^>]*src="/api/v3/attachments/(\d+)/content"[^>]*>', repl, text or "")

    lines = ["# %s — %s" % (wp.get("displayId"), wp.get("subject")), "",
             "- Trạng thái: %s · Ưu tiên: %s · Tác giả: %s" % (
                 wp["_links"]["status"].get("title"), wp["_links"].get("priority", {}).get("title"),
                 (wp["_links"].get("author", {}).get("title") or "").strip()),
             "", "## Mô tả", "", localize((wp.get("description") or {}).get("raw")), "", "## Lịch sử"]
    for act in acts:
        who = api.user_name(act.get("_links", {}).get("user", {}).get("href", ""))
        details = [d.get("raw", "") for d in act.get("details", []) if d.get("raw")]
        comment = (act.get("comment") or {}).get("raw")
        if not comment and not details:
            continue
        lines += ["", "### %s — %s" % (act.get("createdAt", "")[:16].replace("T", " "), who)]
        lines += ["- " + d for d in details]
        if comment:
            lines += ["", localize(comment)]
    # Link Figma trong mô tả / comment → tải ảnh + thông số frame (figma.py, cần FIGMA_TOKEN) để Claude sửa theo.
    raw = "\n".join([(wp.get("description") or {}).get("raw") or ""] +
                    [(a.get("comment") or {}).get("raw") or "" for a in acts])
    try:
        import figma
        sec = "" if not figma_designs else figma.ticket_section(raw, os.path.join(out_dir, "figma"))
        if sec:
            lines.append(sec)
            log("%s: đã đọc thiết kế Figma (%d link)" % (wp.get("displayId"), len(figma.find_links(raw))))
    except Exception as ex:  # noqa: BLE001 — Figma lỗi không chặn lượt sửa
        log("đọc Figma lỗi: %s" % ex)
    with open(os.path.join(out_dir, "ticket.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    for att in image_ids:
        req = op_watch.urllib.request.Request(api.url + "/api/v3/attachments/%s/content" % att, headers=api.headers)
        try:
            with op_watch.urllib.request.urlopen(req, timeout=60) as resp, \
                    open(os.path.join(out_dir, "images", att + ".png"), "wb") as f:
                shutil.copyfileobj(resp, f)
        except (op_watch.urllib.error.URLError, OSError) as ex:
            log("không tải được ảnh %s: %s" % (att, ex))
    return wp


# Danh sách include Liquibase chỉ thêm dòng ở cuối: hai nhánh cùng thêm changelog thì giữ cả hai là đúng.
UNION_MERGE_FILES = re.compile(envconf.UNION_INCLUDE_RX)  # master.xml, master-prod.xml… (danh sách include)
# File thông báo i18n: mỗi ticket thường thêm khoá mới ở cuối → giữ dòng của cả hai bên (trùng khoá khác nội dung
# thì không tự gộp, để Claude gỡ).
UNION_PROPERTIES = re.compile(envconf.UNION_MERGE_RX)
CONFLICT_BLOCK = re.compile(r"<<<<<<< [^\n]*\n(.*?)(?:\|\|\|\|\|\|\| [^\n]*\n.*?)?=======\n(.*?)>>>>>>> [^\n]*\n",
                            re.S)


def build_env(acfg):
    """Node mặc định của máy (v8) không chạy được vite/tsc: đưa bản Node ở `node_bin` lên đầu PATH."""
    env = dict(os.environ)
    node_bin = os.path.expanduser(acfg.get("node_bin", ""))
    if node_bin and os.path.isdir(node_bin):
        env["PATH"] = node_bin + os.pathsep + env.get("PATH", "")
    # Dịch vụ systemd chỉ có PATH tối thiểu: thêm thư mục công cụ cài theo người dùng (bun, cargo, go, pnpm, deno…)
    # và EXTRA_PATH (.env) nếu có, để lệnh build / setup của repo tìm được như khi chạy tay trong terminal.
    have = env.get("PATH", "").split(os.pathsep)
    extra = envconf.extra_path() + [
        os.path.expanduser(x) for x in ("~/.bun/bin", "~/.local/share/pnpm", "~/.deno/bin", "~/.cargo/bin", "~/go/bin",
                                        "~/.npm-global/bin", "~/.local/bin")]
    add = [x for x in extra if os.path.isdir(x) and x not in have]
    if add:
        env["PATH"] = os.pathsep.join(have + add) if not node_bin else env["PATH"] + os.pathsep + os.pathsep.join(add)
    # Gói Claude: lượt chạy đã chọn gói (use_profile) thì dùng gói đó, không thì gói đang chọn trên trang theo dõi.
    if "_config_dir" in acfg:
        config_dir, token_file = acfg["_config_dir"], acfg.get("_token_file")
    else:
        order = profile_order(os.path.expanduser(acfg["root"]), acfg)[0]
        prof = order[0] if order else {}
        config_dir, token_file = prof.get("config_dir", ""), prof.get("token_file")
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    # Thư mục mặc định thì không đặt biến: CLAUDE_CONFIG_DIR=~/.claude đọc ~/.claude/.claude.json chứ không phải
    # ~/.claude.json như khi chạy bình thường.
    if config_dir and os.path.realpath(config_dir) != os.path.realpath(os.path.expanduser("~/.claude")):
        env["CLAUDE_CONFIG_DIR"] = config_dir
    # Gói thêm bằng `claude setup-token`: token của gói được ưu tiên hơn tài khoản đăng nhập trong thư mục cấu hình.
    token = read_token(token_file)
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


CONFLICT_MARKER = re.compile(r"^(<<<<<<< |>>>>>>> |=======$)", re.M)

CONFLICT_PROMPT = """Bạn đang gỡ xung đột merge git, chạy nền không có người theo dõi.
Worktree: {work} — đang giữa lệnh merge: {what}.
File còn xung đột: {files}

Việc cần làm:
1. Với từng file: đọc toàn bộ đoạn <<<<<<< / ======= / >>>>>>>, xem lịch sử hai phía
   (`git -C {work} log --oneline -5 HEAD -- <file>`, `git -C {work} log --oneline -5 MERGE_HEAD -- <file>`,
   `git -C {work} diff`, `git -C {work} show <commit>`) để hiểu mỗi bên đổi gì và vì sao.
2. Sửa file sao cho GIỮ ĐỦ thay đổi của CẢ HAI phía (gộp logic, không bỏ bên nào), code phải biên dịch được.
   Xoá hết dấu xung đột. Không sửa file nào khác ngoài các file trên.
3. Không git add / commit / merge / checkout / reset — script sẽ tự kiểm tra và commit.
4. Nếu hai phía mâu thuẫn thật về ý đồ (không thể gộp mà không chọn bỏ một bên) → KHÔNG đoán: để nguyên file đó.

Ghi {report} bằng tiếng Việt. Dòng đầu đúng một trong:
`KẾT QUẢ: ĐÃ GỠ` | `KẾT QUẢ: KHÔNG GỠ ĐƯỢC`
Sau đó: từng file → mỗi phía đổi gì → đã gộp thế nào (hoặc vì sao không gộp được).
"""



OFFLINE_MISS = "in offline mode and the artifact"


def run_build(cmd, cwd, out, acfg, timeout=1800):
    """Chạy lệnh build, ghi log vào out. mvn -o thiếu dependency mới (chưa có trong ~/.m2) → chạy lại online một lần."""
    res = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         env=build_env(acfg), timeout=timeout, universal_newlines=True, errors="replace")
    out.write(res.stdout)
    out.flush()
    if res.returncode != 0 and cmd[:1] == ["mvn"] and "-o" in cmd and OFFLINE_MISS in res.stdout:
        online = [c for c in cmd if c != "-o"]
        out.write("\n# thiếu dependency trong ~/.m2 → chạy lại online để tải\n$ %s\n" % " ".join(online))
        out.flush()
        res = subprocess.run(online, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             env=build_env(acfg), timeout=timeout)
    return res.returncode

def union_properties(text):
    """Gộp các khối xung đột của file .properties: giữ dòng của cả hai bên, bỏ dòng trùng. None nếu cùng một khoá mà
    hai bên đặt giá trị khác nhau (không đoán được bên nào đúng)."""
    clash = []

    def block(m):
        out, seen = [], {}
        for line in (m.group(1) + m.group(2)).splitlines(True):
            key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith(("#", "!")) else None
            if key:
                if key in seen:
                    if seen[key] != line.strip():
                        clash.append(key)
                    continue
                seen[key] = line.strip()
            elif line in out and line.strip():
                continue
            out.append(line)
        return "".join(out)

    merged = CONFLICT_BLOCK.sub(block, text)
    return None if clash or CONFLICT_MARKER.search(merged) else merged


def resolve_conflicts(acfg, work, what):
    """
    Gọi ngay sau khi `git merge` trong `work` bị xung đột. Xung đột chỉ ở db/master*.xml → giữ include của cả hai bên;
    còn lại → Claude headless gỡ (nếu acfg có và còn gói dùng được). Gỡ xong thì commit merge, trả ghi chú.
    Không gỡ được → merge --abort và raise RuntimeError.
    """
    conflicted = run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=work).split()
    if not conflicted:
        subprocess.run(["git", "merge", "--abort"], cwd=work, check=False)
        raise RuntimeError("%s lỗi (không có file xung đột)" % what)
    union = []
    for f in conflicted:
        if not (UNION_MERGE_FILES.search(f) or UNION_PROPERTIES.search(f)):
            continue
        file_path = os.path.join(work, f)
        try:
            with open(file_path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue  # file bị xoá ở một phía / không đọc được: để Claude gỡ
        merged = union_properties(text) if UNION_PROPERTIES.search(f) else CONFLICT_BLOCK.sub(r"\1\2", text)
        if merged is None:
            continue
        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write(merged)
        union.append(f)
    if union:
        run(["git", "add"] + union, cwd=work)
    rest = [f for f in conflicted if f not in union]
    notes = ["tự gộp %s (giữ dòng của cả hai nhánh)" % ", ".join(union)] if union else []
    if rest:
        fail = None
        if not acfg or not acfg.get("claude_resolve_conflicts", True):
            fail = "chưa bật gỡ xung đột bằng Claude"
        else:
            root = os.path.expanduser(acfg["root"])
            prof, why = choose_profile(root, acfg, acfg.get("_profile"))
            if prof is None:
                fail = "không có gói Claude dùng được (%s)" % why
        report_dir = None
        if not fail:
            sub = dict(acfg)
            use_profile(sub, prof)
            report_dir = os.path.join(root, "conflict-%s" % time.strftime("%m%d-%H%M%S"))
            os.makedirs(report_dir, exist_ok=True)
            report = os.path.join(report_dir, "report.md")
            prompt = CONFLICT_PROMPT.format(work=work, what=what, files=", ".join(rest), report=report)
            wscope, rscope = "/" + os.path.abspath(work) + "/**", "/" + os.path.abspath(report_dir) + "/**"
            allowed = ["Read", "Glob", "Grep", "Edit(%s)" % wscope, "Edit(%s)" % rscope, "Write(%s)" % rscope] + [
                "Bash(git -C %s %s*)" % (work, c) for c in ("log", "show", "diff", "status", "blame")]
            cmd = [acfg["claude"], "-p", prompt, "--permission-mode", "dontAsk",
                   "--max-budget-usd", str(acfg["max_budget_usd"]), "--output-format", "stream-json", "--verbose",
                   "--add-dir", report_dir, "--allowedTools"] + allowed + ["--disallowedTools"] + DISALLOWED_TOOLS
            if acfg.get("model"):
                cmd += ["--model", acfg["model"]]
            log("%s: xung đột %s → Claude gỡ (gói %s, %s)" % (what, ", ".join(rest), prof["name"], report_dir))
            notify("[GỠ XUNG ĐỘT]", "%s: %s → Claude đang gỡ" % (what, ", ".join(rest)))
            with open(os.path.join(report_dir, "claude.jsonl"), "w", encoding="utf-8") as out, \
                    open(os.path.join(report_dir, "claude.err"), "w", encoding="utf-8") as err:
                subprocess.run(cmd, cwd=work, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                               env=build_env(sub), timeout=3600)
            first = ""
            if os.path.exists(report):
                with open(report, encoding="utf-8") as fh:
                    first = fh.readline().strip()
            left = []
            for f in rest:
                try:
                    with open(os.path.join(work, f), encoding="utf-8", errors="replace") as fh:
                        if CONFLICT_MARKER.search(fh.read()):
                            left.append(f)
                except OSError:
                    pass  # file bị xoá ở một phía: để git quyết định bên dưới
            if "ĐÃ GỠ" not in first:
                fail = "Claude không gỡ được (%s)" % (first or "không có report")
            elif left:
                fail = "Claude báo đã gỡ nhưng còn dấu xung đột ở %s" % ", ".join(left)
            else:
                res = subprocess.run(["git", "add", "-A", "--"] + rest, cwd=work)
                if res.returncode != 0:
                    fail = "git add sau khi gỡ lỗi"
        if fail:
            subprocess.run(["git", "merge", "--abort"], cwd=work, check=False)
            raise RuntimeError("%s xung đột: %s — %s%s" % (what, ", ".join(rest), fail,
                                                          " (xem %s)" % report_dir if report_dir else ""))
        notes.append("Claude gỡ %s (%s/report.md)" % (", ".join(rest), report_dir))
    if run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=work).split():
        subprocess.run(["git", "merge", "--abort"], cwd=work, check=False)
        raise RuntimeError("%s: vẫn còn file xung đột sau khi gỡ" % what)
    run(["git", "commit", "--no-edit", "-q"], cwd=work)
    note = "%s: %s" % (what, "; ".join(notes))
    log(note)
    return note


def current_max_parallel(acfg):
    """max_parallel mới nhất (trang theo dõi có thể đổi trong lúc lượt chạy đang xếp hàng)."""
    try:
        cfg = op_watch.load_config(argparse.Namespace(config=None, interval=None, on_change=None, no_notify=True))
        return max(1, int((cfg.get("autofix") or {}).get("max_parallel") or acfg.get("max_parallel") or 1))
    except (SystemExit, OSError, ValueError, TypeError):
        return max(1, int(acfg.get("max_parallel") or 1))


def acquire_slot(root, acfg, wait=True):
    """
    Giữ một suất chạy (file lock .run-slot-N.lock). Số suất = max_parallel đọc lại mỗi lần thử, nên đổi số agent trên
    trang theo dõi áp dụng ngay cho các lượt đang xếp hàng. Đếm số suất đang bị giữ thật (kể cả suất có số thứ tự ≥ số
    mới khi vừa giảm) — đang chạy đủ số mới thì chờ. wait=False: hết suất thì trả None ngay.
    """
    while True:
        n = current_max_parallel(acfg)
        existing = [int(m.group(1)) for m in (re.match(r"^\.run-slot-(\d+)\.lock$", f) for f in os.listdir(root)) if m]
        held, mine = 0, None
        for i in range(max([n - 1] + existing) + 1):
            fh = open(os.path.join(root, ".run-slot-%d.lock" % i), "w")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                held += 1
                continue
            if mine is None and i < n:
                mine = fh
            else:
                fh.close()  # đóng file = nhả khoá
        if mine is not None and held < n:
            return mine
        if mine is not None:
            mine.close()
        if not wait:
            return None
        time.sleep(5)


class named_lock:
    """Khoá ngắn quanh thao tác git dùng chung (fetch, worktree add, push) giữa các lượt chạy song song."""

    def __init__(self, name):
        root = envconf.autofix_root()
        os.makedirs(root, exist_ok=True)
        self.path = os.path.join(root, ".lock-%s" % re.sub(r"[^\w.-]", "_", name))

    def __enter__(self):
        self.fh = open(self.path, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def make_worktree(repo, base_branch, upstream, path, branch, acfg=None):
    """
    Worktree mới trên nhánh auto/... từ origin/<base_branch>, merge origin/<upstream> (pull trước khi sửa).
    Trả về ghi chú khi không merge được (vẫn sửa được trên nhánh của mình), rỗng nếu merge xong.
    """
    with named_lock("git-" + os.path.basename(repo.rstrip("/"))):
        run(["git", "fetch", "-q", "origin", base_branch, upstream], cwd=repo)
        run(["git", "worktree", "add", "-q", "-b", branch, path, "origin/" + base_branch], cwd=repo)
    res = subprocess.run(["git", "merge", "-q", "-m", "Merge branch '%s' into %s" % (upstream, base_branch),
                          "origin/" + upstream], cwd=path,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if res.returncode == 0:
        return ""
    try:
        resolve_conflicts(acfg, path, "merge origin/%s vào %s" % (upstream, base_branch))
        return ""
    except RuntimeError as ex:
        conflicted = [str(ex)]
    note = "chưa merge được origin/%s vào %s (xung đột: %s) — đang sửa trên %s chưa có code mới nhất" % (
        upstream, base_branch, conflicted[0][:400], base_branch)
    log(note)
    return note


def fix_commits(repo_dir, base):
    """Commit sửa của lượt chạy: nhánh chính sau origin/<base>, bỏ merge (không tính commit kéo từ nhánh chung).
    Loại cả commit đã có trên bất kỳ nhánh nào của origin: merge origin/<upstream> vào nhánh cá nhân đang chậm hơn là
    fast-forward (không có merge commit) nên --first-parent vẫn đi qua commit của nhánh chung."""
    out = run(["git", "rev-list", "--first-parent", "--no-merges", "HEAD", "^origin/%s" % base, "--not", "--remotes=origin"],
              cwd=repo_dir, check=False)
    return out.split()


def surefire_failures(module_dir):
    """{(lớp test, tên test)} bị lỗi / fail theo target/surefire-reports/TEST-*.xml (None nếu không có báo cáo)."""
    import xml.etree.ElementTree as ET
    files = glob.glob(os.path.join(module_dir, "target", "surefire-reports", "TEST-*.xml"))
    if not files:
        return None
    out = set()
    for f in files:
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for tc in root.iter("testcase"):
            if tc.find("error") is not None or tc.find("failure") is not None:
                out.add((tc.get("classname") or "", tc.get("name") or ""))
    return out


def preexisting_test_failures(acfg, repo_dir, commits, tests, log_path, repo=None):
    """
    Test BE lỗi khi kiểm tra bản sửa: chạy lại đúng các lớp test đó trên commit ngay trước bản sửa. Mọi test lỗi ở bản
    sửa cũng lỗi ở bản trước → lỗi có sẵn trên nhánh chung (vd thiếu bean trong WebMvcTest), không chặn push. Trả ghi
    chú (chuỗi) nếu là lỗi có sẵn, rỗng nếu có test lỗi do bản sửa (hoặc không so sánh được).
    """
    repo = repo or R.find(acfg, "BE") or {"key": "be", "subdir": acfg.get("be_subdir") or "", "test": []}
    module = R.module_dir(repo, repo_dir)
    fix_fail = surefire_failures(module)
    if not fix_fail:
        return ""
    oldest = next((c for c in commits if all(subprocess.run(["git", "merge-base", "--is-ancestor", c, o], cwd=repo_dir)
                                               .returncode == 0 for o in commits)), None)
    if not oldest:
        return ""
    base_dir = os.path.join(os.path.dirname(log_path), ".verify-base-%s" % repo["key"])
    shutil.rmtree(base_dir, ignore_errors=True)
    try:
        run(["git", "worktree", "add", "-q", "--detach", base_dir, oldest + "^"], cwd=repo_dir)
        classes = sorted({c.rsplit(".", 1)[-1] for c, _ in fix_fail} & set(tests)) or sorted(tests)
        cmd = (R.commands(repo, "test", base_dir, ",".join(classes)) or [[]])[0]
        if not cmd:
            return ""
        with open(log_path, "a", encoding="utf-8") as out:
            out.write("\n# Test lỗi ở bản sửa — chạy lại trên commit trước bản sửa (%s^) để xem có phải lỗi sẵn không\n"
                      "$ %s\n" % (oldest[:9], " ".join(cmd)))
            out.flush()
            run_build(cmd, base_dir, out, acfg)
        base_fail = surefire_failures(R.module_dir(repo, base_dir)) or set()
        new = fix_fail - base_fail
        with open(log_path, "a", encoding="utf-8") as out:
            out.write("# lỗi ở bản sửa: %d, lỗi sẵn ở bản trước: %d, lỗi mới do bản sửa: %s\n"
                      % (len(fix_fail), len(fix_fail & base_fail),
                         ", ".join("%s.%s" % x for x in sorted(new)) or "không có"))
        if new:
            return ""
        names = sorted({c.rsplit(".", 1)[-1] for c, _ in fix_fail})
        return "test %s lỗi sẵn trên nhánh chung, không do bản sửa (%d test: %s)" % (
            repo.get("label") or "BE", len(fix_fail), ", ".join(names))
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as ex:
        log("so sánh test với bản trước lỗi: %s" % ex)
        return ""
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", base_dir], cwd=repo_dir,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(base_dir, ignore_errors=True)


ANSI_RX = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ERR_RX = re.compile(r"\berror\b|\bERR!|\bFAIL(ED)?\b|✗|×", re.I)
NOISE_RX = re.compile(r"run failed|exited \(\d+\)|^\s*(Tasks|Failed|Cached|Time):", re.I)


def error_lines(text):
    """Dòng lỗi (đã bỏ màu / thời gian) để so giữa bản sửa và bản trước."""
    out = set()
    for line in ANSI_RX.sub("", text).splitlines():
        line = re.sub(r"\b\d+(\.\d+)?\s*m?s\b", "", line).strip()
        if line and ERR_RX.search(line) and not NOISE_RX.search(line):
            out.add(line)
    return out


def preexisting_errors(acfg, repo, repo_dir, commits, cmd, log_path, start, out):
    """build_compare "errors": lệnh lỗi ở bản sửa → chạy lại trên commit trước bản sửa (trong chính worktree, giữ
    phụ thuộc đã cài). Mọi dòng lỗi đều có sẵn ở bản trước → trả ghi chú (không chặn); có lỗi mới → "" (chặn)."""
    out.flush()
    with open(log_path, encoding="utf-8", errors="replace") as f:
        f.seek(start)
        fix_err = error_lines(f.read())
    if not fix_err or not commits:
        return ""
    oldest = next((c for c in commits if all(subprocess.run(["git", "merge-base", "--is-ancestor", c, o], cwd=repo_dir)
                                               .returncode == 0 for o in commits)), None)
    if not oldest or run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo_dir, check=False).strip():
        return ""
    head = run(["git", "symbolic-ref", "-q", "--short", "HEAD"], cwd=repo_dir, check=False).strip() or \
        run(["git", "rev-parse", "HEAD"], cwd=repo_dir).strip()
    base_log = log_path + ".base"
    try:
        run(["git", "checkout", "-q", "--detach", oldest + "^"], cwd=repo_dir)
        with open(base_log, "w", encoding="utf-8") as bout:
            run_build(cmd, repo_dir, bout, acfg)
        with open(base_log, encoding="utf-8", errors="replace") as f:
            base_err = error_lines(f.read())
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as ex:
        log("so lỗi với bản trước lỗi: %s" % ex)
        return ""
    finally:
        subprocess.run(["git", "checkout", "-q", head], cwd=repo_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    new = sorted(fix_err - base_err)
    out.write("\n# Lệnh lỗi ở bản sửa — chạy lại trên %s^ (trước bản sửa): %d dòng lỗi ở bản sửa, %d có sẵn, mới: %s\n"
              % (oldest[:9], len(fix_err), len(fix_err & base_err), "\n#   ".join([""] + new[:20]) if new else "không có"))
    out.flush()
    if new:
        return ""
    return "%s `%s`: %d lỗi có sẵn trên nhánh chung (không do bản sửa)" % (
        repo["label"], " ".join(cmd)[-60:], len(fix_err))


def verify(acfg, label, repo_dir, commits, log_path):
    """Tự kiểm tra lại (lệnh build / test của repo trong cấu hình), không dựa vào báo cáo của Claude. Trả lỗi (chuỗi)
    hoặc rỗng nếu qua."""
    repo = R.find(acfg, label)
    if repo is None:
        return ""
    tests = []
    if repo.get("test") and repo.get("test_files"):
        # Chỉ file của commit sửa (không tính commit kéo từ nhánh chung / nhánh mình qua merge).
        changed = set()
        for commit in commits:
            changed.update(run(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit],
                               cwd=repo_dir, check=False).split())
        rx = re.compile(repo["test_files"])
        tests = sorted({os.path.splitext(os.path.basename(f))[0] for f in changed
                        if rx.search("/" + f) and os.path.exists(os.path.join(repo_dir, f))})
    test_cmds = R.commands(repo, "test", repo_dir, ",".join(tests)) if tests else []
    cmds = R.commands(repo, "build", repo_dir) + test_cmds
    if cmds:  # worktree đã bị cleanup dọn nhẹ (xoá node_modules…) → cài lại trước khi build
        R.link_shared(repo, repo_dir)
        err = R.run_setup(repo, repo_dir, build_env(acfg), log_path)
        if err:
            return err
    with open(log_path, "a", encoding="utf-8") as out:
        for cmd in cmds:
            out.write("\n$ %s\n" % " ".join(cmd))
            out.flush()
            start = out.tell()
            code = run_build(cmd, repo_dir, out, acfg)
            if code != 0 and repo.get("build_compare") == "errors":
                note = preexisting_errors(acfg, repo, repo_dir, commits, cmd, log_path, start, out)
                if note:
                    acfg.setdefault("_verify_notes", []).append(note)
                    log(note)
                    continue
            if code != 0:
                if cmd in test_cmds and repo.get("test_compare") == "surefire":
                    note = preexisting_test_failures(acfg, repo_dir, commits, tests, log_path, repo)
                    if note:
                        acfg.setdefault("_verify_notes", []).append(note)
                        log(note)
                        continue
                return "%s `%s` lỗi (mã %s, xem %s)" % (label, " ".join(cmd[:3] if len(cmd) <= 4 else cmd[-2:]),
                                                       code, log_path)
    return ""


def push(repo_dir, base):
    """Push HEAD lên origin/<base>. Nhánh trên remote đã có commit mới → merge vào rồi push lại (không force)."""
    with named_lock("push-" + base):
        _push(repo_dir, base)


def _push(repo_dir, base):
    run(["git", "fetch", "-q", "origin", base], cwd=repo_dir)
    if subprocess.run(["git", "merge-base", "--is-ancestor", "origin/" + base, "HEAD"], cwd=repo_dir).returncode:
        res = subprocess.run(["git", "merge", "-q", "-m", "Merge branch '%s' of origin into %s" % (base, base),
                              "origin/" + base], cwd=repo_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True)
        if res.returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=repo_dir, check=False)
            raise RuntimeError("origin/%s có commit mới xung đột với bản sửa" % base)
    run(["git", "push", "-q", "origin", "HEAD:refs/heads/" + base], cwd=repo_dir)


def merge_remote(acfg, repo_dir, base, what):
    """
    Gộp origin/<base> mới (lượt chạy song song khác vừa push) vào HEAD, ngoài khoá push để không chặn lượt khác.
    Xung đột → resolve_conflicts (danh sách include Liquibase tự gộp, còn lại Claude gỡ). Trả ghi chú; "" nếu
    không cần gộp. Gỡ không được → RuntimeError (merge đã được huỷ).
    """
    run(["git", "fetch", "-q", "origin", base], cwd=repo_dir)
    if subprocess.run(["git", "merge-base", "--is-ancestor", "origin/" + base, "HEAD"], cwd=repo_dir).returncode == 0:
        return ""
    res = subprocess.run(["git", "merge", "-q", "-m", "Merge branch '%s' of origin into %s" % (base, base),
                          "origin/" + base], cwd=repo_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True)
    if res.returncode == 0:
        return "gộp origin/%s" % base
    return resolve_conflicts(acfg, repo_dir, what)


ALREADY_FIXED_NOTE = """

## LƯU Ý: lượt này KHÔNG có commit mới
Claude kết luận lỗi ĐÃ ĐƯỢC SỬA SẴN trong code hiện tại (bởi commit có từ trước, xem báo cáo của Claude và mục DIFF nếu
có). Hãy kiểm tra trên CODE HIỆN TẠI của worktree ({worktrees}): đi lại từng bước tái hiện của ticket và
đối chiếu từng kết quả mong đợi — ĐẠT chỉ khi code hiện tại thực sự đã đáp ứng, không dựa vào lời của Claude.
"""


def referenced_commits(report_text, dirs):
    """Commit Claude nêu trong báo cáo (lỗi đã được sửa sẵn): [(nhãn, worktree, [hash])] — chỉ hash có thật trong nhánh."""
    hashes = list(dict.fromkeys(re.findall(r"\b[0-9a-f]{7,40}\b", report_text)))
    out = []
    for label, d, _ in dirs:
        found = [h for h in hashes if subprocess.run(["git", "merge-base", "--is-ancestor", h, "HEAD"], cwd=d,
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0]
        if found:
            out.append((label, d, found[:5]))
    return out


EVIDENCE_RX = re.compile(r"^\s*[-*]\s*(TRƯỚC|SAU)\s*:\s*`?([^\s`]+\.(?:png|jpe?g|webp))`?\s*(?:[—–:-]+\s*(.*))?$", re.I)


def evidence_lines(api, wp_id, report_text, run_dir, limit=6):
    """Mục `## Ảnh minh chứng` của report.md (dòng `- TRƯỚC: <ảnh> — <chú thích>` / `- SAU: …`) → các dòng markdown
    chèn vào comment. Ảnh gốc của ticket (ticket/images/<id>.png) dùng lại file đính kèm sẵn có; ảnh khác (trong thư
    mục lượt chạy, ≤ 5 MB) được upload lên ticket. Lỗi upload chỉ bỏ ảnh đó."""
    sec = report_section(report_text or "", "Ảnh minh chứng")
    if not sec or not run_dir:
        return []
    root = os.path.realpath(run_dir)
    before, after = [], []
    for line in sec.splitlines():
        m = EVIDENCE_RX.match(line)
        if not m or len(before) + len(after) >= limit:
            continue
        kind, path, note = m.group(1).upper(), m.group(2), (m.group(3) or "").strip()
        path = os.path.realpath(path if os.path.isabs(path) else os.path.join(run_dir, path))
        if not path.startswith(root + os.sep) or not os.path.isfile(path) or os.path.getsize(path) > 5 * 1024 * 1024:
            log("ảnh minh chứng bỏ qua (ngoài thư mục lượt chạy / không có / quá 5 MB): %s" % path)
            continue
        g = re.fullmatch(re.escape(os.path.join(root, "ticket", "images")) + r"/(\d+)\.png", path)
        att = g.group(1) if g else ""
        if not att:
            try:
                import task_create
                ext = os.path.splitext(path)[1].lower()
                ctype = {".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
                name = "autofix-%s-%s" % ("truoc" if kind.startswith("TR") else "sau", os.path.basename(path))
                att = str(task_create.upload_attachment(api, wp_id, path, name, ctype).get("id") or "")
            except Exception as ex:  # noqa: BLE001
                log("upload ảnh minh chứng lỗi %s: %s" % (path, ex))
                continue
        if att:
            note = for_tester(note)[:300]
            (before if kind.startswith("TR") else after).append(
                "%s\n![%s](/api/v3/attachments/%s/content)" % (("_%s_" % note) if note else "", note or "ảnh", att))
    out = []
    for title, items in (("Trước khi sửa", before), ("Sau khi sửa", after)):
        if items:
            out += ["", "**Ảnh minh chứng — %s** (khung đỏ: vùng cần chú ý):" % title]
            for it in items:
                out += ["", it]
    return out


def comment_and_status(api, wp_id, lines, status_id):
    op_write(api, "POST", "/api/v3/work_packages/%s/activities" % wp_id, {"comment": {"raw": "\n".join(lines)}})
    if status_id:
        set_wp_status(api, wp_id, status_id)


def tester_body(report_text, *headings):
    for h in headings:
        body = report_section(report_text, h)
        if body and not re.fullmatch(r"[-*\s]*(Không có|Không)\.?[^\n]*", body, re.I):
            return for_tester(body)[:3000]
    return ""


def deliver_already_fixed(api, cfg, acfg, wp_id, display_id, wp, run_dir, fe_dir, be_dir, report_text):
    """
    ĐÃ SỬA mà không có commit mới (lỗi đã được sửa sẵn trong code hiện tại): Gemini kiểm tra code hiện tại theo
    ticket. ĐẠT → comment cho tester + Resolved; còn lại (hoặc Gemini tắt / không chạy được) → comment + UNREVIEWED.
    Trả mô tả.
    """
    dirs = tuple((r["label"], d, r["branch"]) for r, d in R.trees(acfg, run_dir))
    review_id = acfg.get("review_status_id")
    verdict = ""
    if acfg.get("gemini_enabled"):
        log("%s: không có commit mới (đã sửa sẵn) — Gemini kiểm tra code hiện tại" % display_id)
        notify("[GEMINI TEST] %s" % display_id, "Đã sửa sẵn — Gemini kiểm tra code hiện tại: " + wp.get("subject", ""))
        verdict, _ = gemini_review(acfg, run_dir, referenced_commits(report_text, dirs),
                                   note=ALREADY_FIXED_NOTE.format(run_dir=run_dir,
                                                                  worktrees=", ".join(d for _, d in R.trees(acfg, run_dir))))
        shown = verdict or "không chạy được"
        log("%s: Gemini %s" % (display_id, shown))
        inbox(cfg, "**GEMINI TEST** %s — %s (đã sửa sẵn) · %s" % (display_id, shown, os.path.join(run_dir, "gemini.md")))
    body = tester_body(report_text, "Comment cho tester", "Đề xuất")
    if verdict == "ĐẠT":
        lines = ["**[Autofix] Đã được sửa sẵn** — lỗi này đã được xử lý trong bản code hiện tại (bản sửa trước đó); "
                 "AI kiểm tra lại xác nhận đạt. Cần test trên bản deploy mới nhất."]
        if body:
            lines += ["", body]
        lines += evidence_lines(api, wp_id, report_text, run_dir)
        comment_and_status(api, wp_id, lines, acfg["resolved_status_id"])
        return "Gemini ĐẠT · đã sửa sẵn (không commit mới), comment + chuyển Resolved"
    gemini_text = open(os.path.join(run_dir, "gemini.md"), encoding="utf-8").read() \
        if verdict and os.path.exists(os.path.join(run_dir, "gemini.md")) else ""
    items = [for_tester(i) for i in summarize_gemini(gemini_text, own_only=True)] if gemini_text else []
    why = {"KHÔNG ĐẠT": "đánh giá chưa đạt", "CẦN XEM": "chưa xác nhận được"}.get(verdict, "chưa chạy được")
    lines = ["**[Cần duyệt]** Autofix kết luận lỗi đã được sửa sẵn trong code hiện tại (không có thay đổi mới), "
             "nhưng AI kiểm tra lại %s%s" % (why, ":" if items else ".")]
    lines += ["- " + i for i in items]
    if body:
        lines += ["", "**Nhận định của autofix:**", body]
    lines += evidence_lines(api, wp_id, report_text, run_dir)
    comment_and_status(api, wp_id, lines, review_id)
    return "Gemini %s · đã sửa sẵn (không commit mới) → %s" % (
        verdict or "không chạy được", "comment + chuyển UNREVIEWED" if review_id else "đã comment")


QA_RULES = os.path.join(envconf.autofix_root(), "qa-rules.md")


def read_qa_rules():
    try:
        with open(QA_RULES, encoding="utf-8") as f:
            return f.read().strip()[-6000:] or "(chưa có)"
    except OSError:
        return "(chưa có)"


def save_qa_rules(report_text, display_id):
    """Mục "## Rule cho QA" của báo cáo QA SAI → thêm vào qa-rules.md (bỏ dòng trùng). Trả số rule mới."""
    body = report_section(report_text, "Rule cho QA")
    rules = [re.sub(r"^\s*[-*\d.)]+\s*", "", l).strip() for l in body.splitlines()]
    rules = [r for r in rules if len(r) > 15]
    try:
        with open(QA_RULES, encoding="utf-8") as f:
            have = f.read()
    except OSError:
        have = "# Rule cho QA tự động (rút ra từ các lần Claude kết luận QA SAI)\n" \
               "# Sửa / xoá dòng sai trực tiếp trong file này. Được đưa vào prompt mọi lần QA sau.\n"
    norm = lambda x: re.sub(r"\W+", " ", x.lower()).strip()  # noqa: E731
    known = {norm(l.split("] ", 1)[-1]) for l in have.splitlines() if l.startswith("- ")}
    new = [r for r in rules if norm(r) not in known]
    if new:
        with open(QA_RULES + ".tmp", "w", encoding="utf-8") as f:
            f.write(have.rstrip("\n") + "\n" + "".join("- [%s %s] %s\n" % (
                time.strftime("%Y-%m-%d"), display_id, r) for r in new))
        os.replace(QA_RULES + ".tmp", QA_RULES)
    return len(new)


def report_manual_test(api, wp_id, report_text, run_name, status_id=None):
    """CẦN TEST TAY (QA tự động thật sự không test được): xử lý như ticket cần duyệt — comment bước test tay + người
    test (tester / dev), chuyển UNREVIEWED, đưa vào danh sách xin ý kiến (nhờ tester test tay qua Telegram)."""
    body = tester_body(report_text, "Comment cho tester", "Nguyên nhân") or "Xem báo cáo autofix %s." % run_name
    decider, why, question = review_decider(report_text)
    lines = ["**[Cần duyệt] Cần test tay** — QA tự động không tự test được trên server test, cần kiểm tra tay theo các "
             "bước dưới đây:", "", body]
    if decider:
        lines += ["", "_Người test: %s%s_" % ("Tester — sẽ nhờ qua Telegram" if decider == "tester" else "Dev",
                                              (" (" + plain_md(why) + ")") if why else "")]
    op_write(api, "POST", "/api/v3/work_packages/%s/activities" % wp_id, {"comment": {"raw": "\n".join(lines)}})
    w = api.get("/api/v3/work_packages/%s" % wp_id)
    if status_id:
        op_write(api, "PATCH", "/api/v3/work_packages/%s" % wp_id, {
            "lockVersion": w["lockVersion"], "_links": {"status": {"href": "/api/v3/statuses/%s" % status_id}}})
    try:
        import tg_requests
        tg_requests.propose_ask(w.get("displayId") or "WP-%s" % wp_id, int(wp_id), w.get("subject", ""),
                                decider or "tester", why, for_tester(question or body), for_tester(body), "autofix",
                                run_name, kind="manual_test")
    except Exception as ex:  # noqa: BLE001
        log("xin ý kiến (test tay) qua Telegram lỗi: %s" % ex)
    return "đã comment bước test tay%s" % (", chuyển UNREVIEWED" if status_id else "")


def report_qa_wrong(api, wp_id, display_id, report_text, run_name):
    """QA SAI (Claude kiểm chứng: QA tự động trên server test sai cách / công cụ gửi sai, code đúng):
    comment hướng dẫn test đúng cho tester lên ticket, rồi xoá kết quả QA của ticket trên bản build hiện tại để
    lượt QA kế tiếp test lại (Gemini đọc comment này trong ticket). Số lần QA không đạt KHÔNG reset — lặp lại quá
    qa_fail_autofix_max thì deploy_qa vẫn chuyển UNREVIEWED, không chạy Claude mãi."""
    # Chỉ đăng mục viết cho tester; mục "Nguyên nhân" là phân tích kỹ thuật cho dev (regex, file, annotation).
    guide = tester_body(report_text, "Comment cho tester") or tester_body(report_text, "Nguyên nhân") \
        or "Xem báo cáo autofix %s." % run_name
    lines = ["**[QA tự động] Kết quả \"KHÔNG ĐẠT\" trên server test trước đó chưa chính xác** — Claude kiểm chứng lại: "
             "nguyên nhân nằm ở cách QA tự động test, không phải ở bản sửa. QA tự động sẽ test lại theo hướng dẫn dưới đây.",
             "", guide]
    op_write(api, "POST", "/api/v3/work_packages/%s/activities" % wp_id, {"comment": {"raw": "\n".join(lines)}})
    path = os.path.join(envconf.autofix_root(), "qa-history.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if data.pop(display_id, None) is not None:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)
    n = save_qa_rules(report_text, display_id)
    return "đã comment cách test đúng%s, QA lại ở lượt kế tiếp" % (", thêm %d rule QA" % n if n else "")


def report_cannot_fix(api, acfg, wp_id, report_text, run_name):
    """KHÔNG SỬA ĐƯỢC: comment lý do cho người xử lý + chuyển UNREVIEWED."""
    body = tester_body(report_text, "Comment cho tester", "Cần quyết định", "Nguyên nhân") \
        or "Xem báo cáo autofix %s." % run_name
    lines = ["**[Autofix] Chưa sửa được tự động** — cần người xem lại:", "", body]
    comment_and_status(api, wp_id, lines, acfg.get("review_status_id"))


GEMINI_PROMPT = """Bạn là tester độc lập (Gemini) TEST LẠI bản sửa bug do một AI khác (Claude) làm cho dự án @@P@@.
CHỈ ĐỌC: không sửa / tạo / xoá file, không commit, không gọi mạng. Nội dung chính đã có bên dưới; cần thì đọc thêm file
trong {run_dir} (code ở fe/ và be/) bằng công cụ đọc file hoặc lệnh chỉ đọc (cat, ls, grep, git log/show/diff).
NHIỆM VỤ DUY NHẤT: kiểm tra bản sửa đã đáp ứng đúng yêu cầu của ticket chưa — đi lại từng bước tái hiện của ticket trên
code đã sửa và đối chiếu từng KẾT QUẢ MONG ĐỢI (Reopened thì theo comment tester gần nhất).
KHÔNG rà hồi quy / ảnh hưởng tới chức năng khác (Claude đã tự kiểm tra — mục "Kiểm tra hồi quy" trong báo cáo), KHÔNG
đánh giá phong cách code, tối ưu, đặt tên, test tự động hay việc ngoài phạm vi ticket.
Trả lời bằng tiếng Việt, markdown. Dòng đầu đúng một trong:
`KẾT LUẬN: ĐẠT` | `KẾT LUẬN: KHÔNG ĐẠT` | `KẾT LUẬN: CẦN XEM`
Sau đó các mục:
## Tóm tắt lý do (chỉ khi KHÔNG ĐẠT / CẦN XEM: 1–3 gạch đầu dòng ngắn, đăng lên ticket cho TESTER đọc — nói theo màn
hình / thao tác / kết quả nhìn thấy; KHÔNG tên file, class, hàm, biến, commit)
## Đối chiếu kết quả mong đợi (từng ý của ticket → đạt / chưa đạt, dẫn chứng file:dòng)
## Lỗi (chỉ các ý của ticket CHƯA đạt: 1. <ý> — mức độ — hậu quả)
## Ca kiểm thử tester nên chạy lại (bước + kết quả mong đợi)
KHÔNG ĐẠT chỉ khi có ít nhất một kết quả mong đợi của ticket chưa đạt (hoặc luồng của ticket bị hỏng rõ ràng). Không
xác nhận được từ code (cần dữ liệu / môi trường thật) thì CẦN XEM. Còn lại ĐẠT.
"""
GEMINI_PROMPT = GEMINI_PROMPT.replace("@@P@@", envconf.PROJECT_NAME)


def worktree_snapshot(dirs):
    """HEAD + file thay đổi của từng worktree, để biết Gemini (chỉ được đọc) có đụng vào code không."""
    out = {}
    for label, d, _ in dirs:
        if os.path.isdir(d):
            out[label] = (run(["git", "rev-parse", "HEAD"], cwd=d, check=False).strip(),
                          run(["git", "status", "--porcelain"], cwd=d, check=False))
    return out


def gemini_review(acfg, run_dir, items, note=""):
    """Gemini xem lại bản sửa (chỉ đọc). Trả (kết luận, nội dung); kết luận rỗng nếu Gemini không chạy được."""
    parts = [GEMINI_PROMPT.format(run_dir=run_dir) + note]
    for title, path in (("TICKET", "ticket/ticket.md"), ("BÁO CÁO CỦA CLAUDE", "report.md"),
                        ("KẾT QUẢ BUILD / TEST TỰ ĐỘNG", "verify.log")):
        full = os.path.join(run_dir, path)
        if os.path.exists(full):
            with open(full, encoding="utf-8", errors="replace") as f:
                parts += ["", "# " + title, f.read()[-30000:]]
    parts += ["", "# DIFF"]
    for label, repo_dir, commits in items:
        for commit in commits:
            parts += ["## " + label, run(["git", "show", commit], cwd=repo_dir, check=False)[:60000]]
    out_path = os.path.join(run_dir, "gemini.md")
    # Chạy nền không hỏi được quyền: thiếu quyền là agy dừng, không ra kết luận. Cho đọc file trong thư mục lượt chạy
    # và chạy lệnh (Gemini hay dùng git log / ls / head để xem code) nhưng trong sandbox; worktree được kiểm tra lại
    # sau khi Gemini xong (worktree_snapshot), có thay đổi thì không push.
    # Worktree fe/ be/ không có .git riêng (dữ liệu git ở <repo gốc>/.git/worktrees/…) và node_modules là symlink về
    # repo gốc, nên phải cho đọc cả hai repo gốc, không chỉ thư mục lượt chạy.
    readable = [run_dir] + [os.path.realpath(r["path"]) for r in R.repos(acfg)]
    perms = {"permissions": {"allow": ["read_file(%s/**)" % d for d in readable] + ["command(*)"]}}
    cmd = [os.path.expanduser(acfg["gemini_cmd"]), "-p", "\n".join(parts), "--mode", "plan", "--sandbox",
           "--settings", json.dumps(perms), "--print-timeout", "%ds" % (acfg["gemini_timeout"] - 30)]
    if acfg.get("gemini_model"):
        cmd += ["--model", acfg["gemini_model"]]
    for attempt in range(2):
        try:
            res = subprocess.run(cmd, cwd=run_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL, universal_newlines=True, timeout=acfg["gemini_timeout"])
            text, err = res.stdout.strip(), res.stderr.strip()
        except (OSError, subprocess.TimeoutExpired) as ex:
            text, err = "", str(ex)
        # Bị từ chối quyền (đọc file ngoài các thư mục cho phép…) thì agy dừng hẳn: chạy lại một lần, nhắc giới hạn.
        if attempt or "permission that headless mode cannot prompt for" not in (text + err):
            break
        log("Gemini bị từ chối quyền, chạy lại: %s" % (text + err)[-200:].replace("\n", " "))
        cmd[2] = "\n".join(parts) + "\n\nLƯU Ý: chỉ được đọc file trong các thư mục: %s. Không đọc file ở nơi khác." % (
            ", ".join(readable))
    m = re.search(r"KẾT LUẬN:\s*(KHÔNG ĐẠT|ĐẠT|CẦN XEM)", text[:500])
    if not m:
        text = "Gemini không trả kết luận.\n\n```\n%s\n```\n\n%s" % (err[-1500:], text[:3000])
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    return (m.group(1) if m else ""), text


FOLLOWUP_PROMPT = """Bạn là Claude, đã (hoặc AI trước đã) sửa bug {display_id} — {subject}. Chạy nền, không có người theo dõi.
Gemini (reviewer độc lập) vừa test lại bản sửa và kết luận **{gemini_verdict}**. Toàn bộ nhận xét: {run_dir}/gemini.md
Ticket: {run_dir}/ticket/ticket.md. Bản sửa hiện tại: {commits} (xem bằng `git -C <worktree> show <hash>`).
Worktree: {worktrees}; nhánh hiện tại của worktree đã chứa bản sửa.

## Làm
1. Xét TỪNG ý Gemini nêu (lỗi, rủi ro, test thiếu). Kiểm chứng trong code — Gemini có thể sai, đừng tin mù quáng.
2. Ý đúng, trong phạm vi ticket, sửa được an toàn → sửa tối thiểu, thêm/sửa test, kiểm tra
   ({repo_checks_short}), rồi commit bằng `git -C <worktree> add …` +
   `git -C <worktree> commit …`, message `[{display_id}] - {subject}` + dòng trống + mô tả ngắn tiếng Việt không dấu
   (bắt đầu bằng "Bo sung sau review Gemini: ") + dòng trống + `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
3. Ý sai / ngoài phạm vi / chỉ là gợi ý không cần thiết → KHÔNG sửa, nêu lý do.
4. Ý cần người quyết định (đổi hành vi sản phẩm, trái thiết kế người khác cố ý làm, cần dữ liệu môi trường thật) →
   KHÔNG sửa phần đó.
KHÔNG dùng `cd … && …`; git luôn `git -C`, Maven `-f`. Không push, không fetch/pull/checkout, không gọi mạng.
{bash_rules}

## Báo cáo (bắt buộc)
Ghi {run_dir}/followup.md bằng tiếng Việt. Dòng đầu đúng một trong:
`QUYẾT ĐỊNH: SỬA THÊM` (đã commit thêm) | `QUYẾT ĐỊNH: GIỮ NGUYÊN` (không cần sửa) | `QUYẾT ĐỊNH: CẦN QUYẾT ĐỊNH`
Các mục: ## Đánh giá từng ý của Gemini (ý → đúng/sai → xử lý); ## Đã sửa thêm (file, commit); ## Kiểm tra;
## Cần quyết định (chỉ khi có: các phương án + đề xuất);
{decider_rules}
## Comment cho tester (chỉ khi SỬA THÊM: đăng nguyên văn lên ticket cho TESTER đọc — dòng đầu `**Nguyên nhân lỗi:** …`
(1–2 câu theo hiện tượng: vì sao còn lỗi / vì sao phải sửa thêm), rồi đã sửa thêm gì nhìn từ màn hình / thao tác / kết
quả, test lại thế nào; KHÔNG tên file, class, hàm, biến, commit).
"""



# Ai quyết phần "Cần quyết định": tester trả lời được (hỏi qua Telegram) hay để dev (mình) quyết.
DECIDER_RULES = """  Khi kết quả là CẦN QUYẾT ĐỊNH (hoặc CẦN TEST TAY) thêm 2 mục ngay sau mục Cần quyết định:
  `## Người quyết định` — dòng đầu đúng một trong `NGƯỜI QUYẾT: TESTER` | `NGƯỜI QUYẾT: DEV`, dòng sau một câu lý do.
    TESTER khi tester (hoặc BA qua tester) trả lời được: hiểu ticket thế nào / hành vi đúng là phương án nào (nhìn thấy
    trên màn hình / API), kết quả mong đợi, thiếu bước tái hiện, dữ liệu / tài khoản test, xác nhận lỗi còn không.
    DEV khi là lựa chọn kỹ thuật / kiến trúc, ảnh hưởng tính năng khác / code người khác, rủi ro bảo mật, phạm vi /
    chi phí, cần lãnh đạo chốt, hoặc vấn đề của công cụ / môi trường dev.
    Riêng CẦN TEST TAY: TESTER khi tester tự test tay được trên môi trường test (giao diện, tài khoản test, dữ liệu
    tạo được trên màn hình); DEV khi cần quyền server / DB / log / công cụ của dev mới kiểm tra được.
  `## Câu hỏi cho tester` — chỉ khi NGƯỜI QUYẾT: TESTER: tin nhắn Telegram gửi tester, tối đa 6 dòng, ngôn ngữ của
    tester (không tên file / class / code), nêu vấn đề + các phương án đánh số 1, 2, 3 để tester trả lời bằng số, kèm
    phương án đề xuất. Riêng CẦN TEST TAY: nhờ tester test tay — các bước ngắn + kết quả mong đợi, hỏi kết quả
    "Đạt" / "Không đạt" (kèm hiện tượng nếu không đạt)."""

# Chế độ dontAsk: lệnh ghép chỉ chạy khi MỌI phần đều khớp ALLOWED_TOOLS — một phần lạ là cả lệnh bị từ chối.
BASH_RULES = """Quy tắc lệnh Bash (chạy không người duyệt, lệnh ngoài danh sách bị từ chối, không hỏi):
- Mỗi lần gọi Bash MỘT lệnh đơn: không gán biến (`B=…`), không nối `;` / `&&`, không heredoc, không `$(…)`,
  không `echo` / `cat` / `sed` / `python`. Chỉ được nối `| head` / `| tail` / `| grep`.
- Xem file / tìm kiếm dùng Read / Grep / Glob. Commit: ghi message bằng Write vào {run_dir}/commit-msg.txt rồi
  `git -C <worktree> commit -q -F {run_dir}/commit-msg.txt`.
- Một lệnh bị từ chối KHÔNG có nghĩa Bash bị cấm: tách thành lệnh đơn đúng dạng trên rồi chạy lại, rồi làm tiếp."""

VERIFY_FIX_PROMPT = """Bạn là Claude, đã sửa bug {display_id} — {subject}. Chạy nền, không có người theo dõi.
Bước KIỂM TRA TỰ ĐỘNG (build / test do script chạy lại, không dựa vào báo cáo) sau khi sửa bị LỖI:
{err}
Log đầy đủ: {run_dir}/verify.log (xem phần cuối). Ticket: {run_dir}/ticket/ticket.md. Báo cáo sửa: {run_dir}/report.md.
Worktree: {worktrees}; nhánh hiện tại đã chứa bản sửa.

## Làm
1. Đọc log, tìm nguyên nhân. Lỗi do bản sửa / test mới của lượt này → sửa tối thiểu: test viết sai (mock thiếu,
   strict stubbing, kỳ vọng sai) thì sửa test cho đúng hành vi ticket yêu cầu; code sai thì sửa code.
   KHÔNG xoá test, KHÔNG @Disabled / bỏ qua test, KHÔNG nới kỳ vọng chỉ để cho qua.
2. Chạy lại đúng lệnh bị lỗi ({repo_checks_short}). Qua rồi commit bằng
   `git -C <worktree> add …` + `git -C <worktree> commit …`, message `[{display_id}] - {subject}` + dòng trống +
   mô tả ngắn tiếng Việt không dấu (bắt đầu bằng "Sua loi kiem tra tu dong: ") + dòng trống +
   `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
3. Lỗi không do lượt này (test cũ lỗi sẵn trên nhánh chung, môi trường / mạng) → KHÔNG sửa, ghi rõ bằng chứng.
KHÔNG dùng `cd … && …`; git luôn `git -C`, Maven `-f`. Không push, không fetch/pull/checkout, không gọi mạng.
{bash_rules}

## Báo cáo (bắt buộc)
Ghi {run_dir}/verify-fix.md tiếng Việt. Dòng đầu đúng một trong: `SỬA KIỂM TRA: ĐÃ SỬA` | `SỬA KIỂM TRA: KHÔNG SỬA`.
Sau đó: ## Nguyên nhân; ## Đã sửa (file, commit); ## Kiểm tra (lệnh + kết quả).
"""


def claude_fix_verify(acfg, display_id, wp, run_dir, fe_dir, be_dir, err):
    """Kiểm tra tự động lỗi: Claude một lượt sửa trong worktree (commit). Trả ghi chú; "" nếu không chạy được."""
    root = os.path.expanduser(acfg["root"])
    prof, why = choose_profile(root, acfg, acfg.get("_profile"))
    if prof is None:
        return ""
    use_profile(acfg, prof)
    prompt = VERIFY_FIX_PROMPT.format(bash_rules=BASH_RULES.format(run_dir=run_dir),
                                      display_id=display_id, subject=wp.get("subject", ""), err=err[:2000],
                                      run_dir=run_dir, **R.prompt_kwargs(acfg, run_dir))
    with open(os.path.join(run_dir, "verify-fix-prompt.md"), "w", encoding="utf-8") as f:
        f.write(prompt)
    log("%s: kiểm tra tự động lỗi → Claude sửa (%s)" % (display_id, prof["name"]))
    notify("[AUTOFIX SỬA KIỂM TRA] %s" % display_id, err[:200])
    run_claude(acfg, prompt, run_dir, fe_dir, be_dir, append=True)
    path = os.path.join(run_dir, "verify-fix.md")
    first = open(path, encoding="utf-8").readline().strip() if os.path.exists(path) else ""
    return "Claude sửa lỗi kiểm tra: " + (first.replace("SỬA KIỂM TRA:", "").strip() or "không có báo cáo")


def claude_cmd(acfg, prompt, run_dir, be_dir=None):
    others = [d for _, d in R.trees(acfg, run_dir)[1:]]  # repo đầu là thư mục làm việc, các repo còn lại --add-dir
    cmd = [acfg["claude"], "-p", prompt,
           "--permission-mode", "dontAsk",
           "--max-budget-usd", str(acfg["max_budget_usd"]),
           # stream-json: ghi từng thao tác ngay khi xảy ra để --status / --follow xem được tiến trình.
           "--output-format", "stream-json", "--verbose",
           "--add-dir"] + others + [run_dir]
    # Mỗi quy tắc là một tham số riêng (quy tắc có khoảng trắng, không nối chuỗi được). Edit/Write để trống thì ghi
    # được mọi nơi trên máy — chỉ cho trong thư mục của lượt chạy (worktree + report).
    cmd += ["--allowedTools"] + ALLOWED_TOOLS + write_rules(run_dir, acfg) + ["--disallowedTools"] + DISALLOWED_TOOLS
    if acfg.get("_assist"):  # bước Claude hỗ trợ: được gọi GET API server test (chỉ đọc) qua soar_api.py
        cmd[cmd.index("--disallowedTools"):cmd.index("--disallowedTools")] = [
            "Bash(python3 %s GET *)" % SOAR_API, "Bash(python3 %s *)" % UI_CHECK]
    if acfg.get("model"):
        cmd += ["--model", acfg["model"]]
    return cmd


def run_claude(acfg, prompt, run_dir, fe_dir=None, be_dir=None, append=False):
    """Chạy Claude headless; thao tác ghi vào claude.jsonl (append cho lượt xem lại, --follow vẫn đọc được)."""
    mode = "a" if append else "w"
    with open(os.path.join(run_dir, "claude.jsonl"), mode, encoding="utf-8") as out, \
            open(os.path.join(run_dir, "claude.err"), mode, encoding="utf-8") as err:
        return subprocess.run(claude_cmd(acfg, prompt, run_dir), cwd=R.trees(acfg, run_dir)[0][1], stdout=out, stderr=err,
                              stdin=subprocess.DEVNULL, env=build_env(acfg)).returncode


def claude_followup(acfg, display_id, wp, run_dir, fe_dir, be_dir, gemini_verdict, items):
    """Claude xem lại nhận xét của Gemini và tự quyết. Trả (quyết định, nội dung followup.md)."""
    commits = "; ".join("%s %s" % (label, ", ".join(c[:8] for c in cs)) for label, _, cs in items)
    prompt = FOLLOWUP_PROMPT.format(
        bash_rules=BASH_RULES.format(run_dir=run_dir), decider_rules=DECIDER_RULES,
        display_id=display_id, subject=wp.get("subject", ""), gemini_verdict=gemini_verdict, run_dir=run_dir,
        commits=commits, **R.prompt_kwargs(acfg, run_dir))
    with open(os.path.join(run_dir, "followup-prompt.md"), "w", encoding="utf-8") as f:
        f.write(prompt)
    run_claude(acfg, prompt, run_dir, fe_dir, be_dir, append=True)
    path = os.path.join(run_dir, "followup.md")
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    m = re.search(r"QUYẾT ĐỊNH:\s*(SỬA THÊM|GIỮ NGUYÊN|CẦN QUYẾT ĐỊNH)", text[:300])
    return (m.group(1) if m else ""), text


def deliver(api, cfg, acfg, wp_id, display_id, wp, run_dir, fe_dir, be_dir, report_text, review_of=None,
            gemini_from=None):
    """Bọc _deliver: ghi thời gian kiểm tra + Gemini test lại + push (Testing); push xong → Finish date."""
    t0 = time.time()
    shipped, msg = False, ""
    try:
        shipped, msg = _deliver(api, cfg, acfg, wp_id, display_id, wp, run_dir, fe_dir, be_dir, report_text,
                                review_of, gemini_from)
        return shipped, msg
    finally:
        log_time(api, acfg, wp_id, time.time() - t0, "test",
                 "[Autofix] Kiểm tra build/test + Gemini test lại%s (%s) — %s" % (
                     " (test lại)" if review_of else "", os.path.basename(run_dir), (msg or "")[:120]))
        if shipped and "Resolved" in (msg or ""):
            set_finish_date(api, acfg, wp_id)


def _deliver(api, cfg, acfg, wp_id, display_id, wp, run_dir, fe_dir, be_dir, report_text, review_of=None,
             gemini_from=None):
    """
    Sau khi có bản sửa: build/test → Gemini test → (chưa đạt) Claude xem lại từng ý, tự quyết → push + OpenProject.
    review_of=None: lượt sửa mới — commit sửa là commit trong worktree, push xong chuyển Resolved.
    review_of={"FE": [hash], "BE": [...]}: test lại bản đã push trước đây — chỉ push khi Claude sửa thêm, không đổi
    trạng thái ticket (chỉ comment). Trả (đã push?, mô tả).
    gemini_from: thư mục có gemini.md của lượt trước — dùng lại kết luận đó, không chạy Gemini nữa (hàng chờ xem lại).
    """
    dirs = tuple((r["label"], d, r["branch"]) for r, d in R.trees(acfg, run_dir))
    subject = wp.get("subject", "")

    def current():
        items = []
        for label, d, b in dirs:
            commits = list((review_of or {}).get(label, [])) + fix_commits(d, b)
            if commits:
                items.append((label, d, commits))
        return items

    log_path = os.path.join(run_dir, "verify.log")

    def verify_all():
        # Kiểm tra phải chạy đúng trên code sẽ được push: còn thay đổi chưa commit (vd Claude sửa mà không commit
        # được) thì build / test có thể qua nhờ working tree trong khi commit push lên vẫn lỗi.
        for label, d, _ in dirs:
            dirty = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=d, check=False).strip()
            if dirty:
                files = ", ".join(line[3:] for line in dirty.splitlines()[:5])
                return "%s còn thay đổi chưa commit (%s)" % (label, files)
        for label, d, commits in current():
            err = verify(acfg, label, d, commits, log_path)
            if err:
                return err
        return ""

    fix_notes = []

    def verify_or_fix(check=None):
        """Kiểm tra lại; lỗi thì Claude một lượt sửa (mỗi lần giao tối đa một lượt) rồi kiểm tra lại."""
        check = check or verify_all
        err = check()
        if err and not fix_notes:
            note = claude_fix_verify(acfg, display_id, wp, run_dir, fe_dir, be_dir, err)
            fix_notes.append(note or "Claude sửa lỗi kiểm tra: không có gói dùng được")
            if note:
                err = check()
        return err

    def verify_failed(prefix, err):
        # Vẫn lỗi sau khi Claude sửa: không push; ticket → UNREVIEWED kèm comment để dev xem.
        msg = " · ".join([n for n in [prefix] + fix_notes if n] + ["chưa push: " + err])
        # Test lại bản đã push trước đây (review_of): bản cũ vẫn trên môi trường, chỉ phần sửa thêm chưa push —
        # không đổi trạng thái ticket.
        if review_of is None and acfg.get("review_status_id"):
            try:
                comment_and_status(api, wp_id, [
                    "**[Cần duyệt]** Autofix đã sửa nhưng bản sửa chưa qua kiểm tra tự động (build / test) nên chưa "
                    "được đưa lên môi trường. Cần dev xem lại."], acfg["review_status_id"])
                msg += " → đã comment, chuyển UNREVIEWED"
            except (op_watch.urllib.error.URLError, OSError, ValueError, KeyError) as ex:
                msg += " (chuyển UNREVIEWED lỗi: %s)" % ex
        return False, msg

    acfg["_verify_notes"] = []
    err = verify_or_fix()
    if err:
        return verify_failed("", err)
    notes, gemini_verdict, followup_text = list(acfg.get("_verify_notes") or []) + fix_notes, "", ""
    if gemini_from:
        src = os.path.join(gemini_from, "gemini.md")
        if os.path.realpath(src) != os.path.realpath(os.path.join(run_dir, "gemini.md")):
            shutil.copy(src, os.path.join(run_dir, "gemini.md"))
        with open(src, encoding="utf-8") as f:
            m = re.search(r"KẾT LUẬN:\s*(KHÔNG ĐẠT|ĐẠT|CẦN XEM)", f.read()[:500])
        gemini_verdict = m.group(1) if m else ""
        notes.append("Gemini %s (lượt trước)" % (gemini_verdict or "?"))
    elif acfg.get("gemini_enabled"):
        log("%s: Gemini test lại" % display_id)
        notify("[GEMINI TEST] %s" % display_id, "Build/test đã qua — Gemini đang test lại bản sửa: " + subject)
        before = worktree_snapshot(dirs)
        gemini_verdict, _ = gemini_review(acfg, run_dir, current())
        changed = [label for label, snap in worktree_snapshot(dirs).items() if before.get(label) != snap]
        if changed:
            msg = "Gemini đã thay đổi worktree %s (chỉ được đọc) — kiểm tra tay %s" % (", ".join(changed), run_dir)
            inbox(cfg, "**GEMINI CẢNH BÁO** %s — %s" % (display_id, msg))
            return False, "chưa push: " + msg
        shown = gemini_verdict or "không chạy được"
        notes.append("Gemini " + shown)
        log("%s: Gemini %s" % (display_id, shown))
        notify("[GEMINI TEST] %s: %s" % (display_id, shown), subject)
        inbox(cfg, "**GEMINI TEST** %s — %s · %s" % (display_id, shown, os.path.join(run_dir, "gemini.md")))

    root = os.path.expanduser(acfg["root"])
    skip, queued = "", False
    if gemini_verdict in acfg.get("followup_on", []):
        # Bước Claude xem lại dùng gói của lượt chạy; gói đó hết hạn mức thì đổi gói (nếu bật tự chuyển).
        limits = followup_limits(acfg)
        prof, why = choose_profile(root, limits, acfg.get("_profile"))
        if prof is None:
            skip = why
        else:
            if prof["name"] != acfg.get("_profile"):
                notes.append("Claude xem lại bằng " + prof["name"])
                set_status(root, display_id, profile=prof["name"])
            use_profile(acfg, prof)
    if skip:
        notes.append("bỏ Claude xem lại: " + skip)
        # Xếp hàng: op-watch gọi --followup-pending định kỳ, có gói còn hạn mức thì Claude xem lại lúc đó.
        blocked = gemini_verdict == "KHÔNG ĐẠT" and acfg.get("gemini_block_on_fail")
        queue_followup(root, display_id, wp_id, run_dir, gemini_verdict, skip,
                       pushed=review_of is not None or not blocked)
        queued = True
        todo = acfg.get("followup_status_id")
        notes.append("đã xếp hàng xem lại khi có hạn mức" + (" (ticket → TO DO)" if todo else ""))
        if blocked and todo:
            # Không push nên không có comment "Đã sửa": ghi ngắn lý do lên ticket rồi chuyển TO DO.
            try:
                op_write(api, "POST", "/api/v3/work_packages/%s/activities" % wp_id, {"comment": {"raw":
                         "\n".join(waiting_comment(run_dir, pushed=False))}})
                set_wp_status(api, wp_id, todo)
            except (op_watch.urllib.error.URLError, OSError, ValueError, KeyError) as ex:
                notes.append("chuyển TO DO lỗi: %s" % ex)
        if gemini_verdict == "KHÔNG ĐẠT" and acfg.get("gemini_block_on_fail"):
            return False, " · ".join(notes) + " → chưa push"
    elif gemini_verdict and gemini_verdict in acfg.get("followup_on", []):
        log("%s: Claude xem lại nhận xét Gemini" % display_id)
        notify("[CLAUDE REVIEW] %s" % display_id, "Gemini %s — Claude đang xem lại từng ý để quyết định sửa" %
               gemini_verdict)
        decision, followup_text = claude_followup(acfg, display_id, wp, run_dir, fe_dir, be_dir, gemini_verdict,
                                                  current())
        if decision:
            clear_followup(root, display_id)
        shown = decision or "không có quyết định"
        notes.append("Claude xem lại: " + shown)
        log("%s: Claude xem lại: %s" % (display_id, shown))
        notify("[CLAUDE REVIEW] %s: %s" % (display_id, shown), subject)
        inbox(cfg, "**CLAUDE REVIEW** %s — %s · %s" % (display_id, shown, os.path.join(run_dir, "followup.md")))
        if decision == "CẦN QUYẾT ĐỊNH":
            if acfg.get("review_status_id"):
                mark_for_review(api, wp_id, acfg["review_status_id"], followup_text, os.path.basename(run_dir), run_dir)
                return False, " · ".join(notes) + " → đã chuyển UNREVIEWED, chưa push phần mới"
            return False, " · ".join(notes) + " → chờ quyết định, chưa push"
        if decision == "SỬA THÊM":
            err = verify_or_fix()
            if err:
                return verify_failed(" · ".join(notes), err)
        elif not decision and gemini_verdict == "KHÔNG ĐẠT" and acfg.get("gemini_block_on_fail"):
            return False, " · ".join(notes) + " → chưa push"
    elif gemini_verdict == "KHÔNG ĐẠT" and acfg.get("gemini_block_on_fail"):
        return False, " · ".join(notes) + " → chưa push"

    to_push = [(label, d, b) for label, d, b in dirs if fix_commits(d, b)]
    if not to_push:
        if queued and acfg.get("followup_status_id"):
            try:
                op_write(api, "POST", "/api/v3/work_packages/%s/activities" % wp_id, {"comment": {"raw":
                         "\n".join(waiting_comment(run_dir, pushed=True))}})
                set_wp_status(api, wp_id, acfg["followup_status_id"])
                notes.append("ticket → TO DO")
            except (op_watch.urllib.error.URLError, OSError, ValueError, KeyError) as ex:
                notes.append("chuyển TO DO lỗi: %s" % ex)
        return False, " · ".join(notes + ["không có gì mới để push"])
    # Hash lấy trước khi push (sau push origin/<base> đã chứa chúng).
    hashes = {label: [h[:8] for h in fix_commits(d, b)] for label, d, b in to_push}
    pushed = []

    def unresolved(label, ex):
        # Gỡ xung đột không được: ticket → UNREVIEWED kèm comment, bản sửa nằm lại worktree chờ gộp tay.
        done = (" (đã push %s)" % ", ".join(pushed)) if pushed else ""
        msg = "push %s lỗi: %s%s" % (label, str(ex).splitlines()[0][:200], done)
        if acfg.get("review_status_id"):
            try:
                comment_and_status(api, wp_id, [
                    "**[Cần duyệt]** Autofix đã sửa và kiểm tra đạt, nhưng chưa đưa được bản sửa lên vì trùng chỗ với "
                    "một thay đổi khác vừa được đưa lên cùng lúc. Cần gộp tay rồi mới test được."], acfg["review_status_id"])
                msg += " → đã comment, chuyển UNREVIEWED"
            except (op_watch.urllib.error.URLError, OSError, ValueError, KeyError) as ex2:
                msg += " (chuyển UNREVIEWED lỗi: %s)" % ex2
        return False, " · ".join(notes + [msg])

    for label, d, b in to_push:
        for attempt in range(3):
            # Gộp origin mới ngoài khoá push (Claude gỡ xung đột nếu cần), có gộp thì build / test lại bản đã gộp.
            try:
                note = merge_remote(acfg, d, b, "merge origin/%s vào bản sửa %s" % (b, display_id))
            except (RuntimeError, OSError) as ex:
                return unresolved(label, ex)
            if note:
                notes.append(note)
                err = verify_or_fix(lambda: verify(acfg, label, d, fix_commits(d, b), log_path))
                if err:
                    return verify_failed(" · ".join(notes + ["sau khi gộp origin/%s" % b]), err)
            try:
                push(d, b)
                break
            except RuntimeError as ex:
                # Trong lúc build lại có lượt khác push tiếp và xung đột: gộp lại (tối đa 3 lần).
                if "xung đột" not in str(ex) or attempt == 2:
                    return unresolved(label, ex)
                log("%s: origin/%s lại có commit mới xung đột, gộp lại" % (display_id, b))
            except OSError as ex:
                done = (" (đã push %s)" % ", ".join(pushed)) if pushed else ""
                return False, " · ".join(notes + ["push %s lỗi: %s%s" % (label, str(ex).splitlines()[0][:200], done)])
        pushed.append(label)

    commit_line = ", ".join("%s commit %s (%s)" % (label, ", ".join("`%s`" % h for h in hashes[label]), b)
                            for label, _, b in to_push)
    # Comment viết cho tester (không phải dev): chỉ nội dung "Comment cho tester" của Claude, không commit / nguyên
    # nhân trong code / hạn mức. Chi tiết kỹ thuật nằm trong report.md + báo cáo tổng hợp.
    log("%s: push %s" % (display_id, commit_line))
    if review_of is None:
        lines = ["**[Autofix] Đã sửa** — đã qua kiểm tra tự động (build, test, AI kiểm tra lại)."]
        sections = ((report_text, ("Comment cho tester", "Đề xuất"), ""),
                    (followup_text, ("Comment cho tester",), "Bổ sung sau khi kiểm tra lại"))
    else:
        lines = ["**[Autofix] Bổ sung sau khi kiểm tra lại** — đã sửa thêm, qua kiểm tra tự động."]
        sections = ((followup_text, ("Comment cho tester",), ""),)
    for text, headings, title in sections:
        body = next((report_section(text, h) for h in headings if report_section(text, h)), "")
        if body and not re.fullmatch(r"[-*\s]*(Không có|Không)\.?[^\n]*", body, re.I):
            body = for_tester(body)[:3000]
            if not re.search(r"nguyên nhân", body[:400], re.I):
                # Báo cáo cũ / Claude quên dòng "Nguyên nhân lỗi": lấy câu đầu mục Nguyên nhân (đã bỏ chi tiết dev).
                cause = report_section(text, "Nguyên nhân")
                if cause and not re.fullmatch(r"[-*\s]*(Không có|Không)\.?[^\n]*", cause, re.I):
                    body = "**Nguyên nhân lỗi:** %s\n\n%s" % (first_sentence(for_tester(cause), 300), body)
            lines += [""] + (["**%s:**" % title] if title else []) + [body]
    lines += evidence_lines(api, wp_id, followup_text if review_of is not None else report_text, run_dir)
    if queued:
        lines += [""] + waiting_comment(run_dir, pushed=True)
    op_write(api, "POST", "/api/v3/work_packages/%s/activities" % wp_id, {"comment": {"raw": "\n".join(lines)}})
    if queued and acfg.get("followup_status_id"):
        # Đã push nhưng Claude chưa xem lại nhận xét Gemini: để TO DO, xem lại xong mới chuyển Resolved.
        set_wp_status(api, wp_id, acfg["followup_status_id"])
        return True, " · ".join(notes + ["đã push %s, chuyển TO DO chờ xem lại" % ", ".join(pushed)])
    if review_of is None:
        set_wp_status(api, wp_id, acfg["resolved_status_id"])
        return True, " · ".join(notes + ["đã push %s, chuyển Resolved" % ", ".join(pushed)])
    return True, " · ".join(notes + ["đã push phần sửa thêm %s, comment lên ticket" % ", ".join(pushed)])


def write_summary_report(acfg, display_id, run_name, wp, verdict, commits, run_dir, report_text):
    """Báo cáo tổng hợp ở report_dir (mặc định ~/Downloads/autofix-reports/<run>.md)."""
    out_dir = os.path.expanduser(acfg.get("report_dir") or "")
    if not out_dir:
        return ""
    os.makedirs(out_dir, exist_ok=True)
    sections = ["# %s — %s" % (display_id, wp.get("subject", "")), "",
                "- Ticket: %s/work_packages/%s" % (op_watch.DEFAULTS["url"], wp.get("id")),
                "- Lượt chạy: %s (%s)" % (run_name, datetime.now().strftime("%d/%m/%Y %H:%M")),
                "- Kết quả: **%s**" % verdict,
                "- Commit: %s" % (", ".join(commits) or "không có"),
                "- Thư mục: %s" % run_dir, ""]
    gemini_path = os.path.join(run_dir, "gemini.md")
    if os.path.exists(gemini_path):
        with open(gemini_path, encoding="utf-8") as f:
            sections += ["## Gemini kiểm tra lại", "", f.read().strip(), ""]
    followup_path = os.path.join(run_dir, "followup.md")
    if os.path.exists(followup_path):
        with open(followup_path, encoding="utf-8") as f:
            sections += ["## Claude xem lại nhận xét của Gemini", "", f.read().strip(), ""]
    sections += ["## Báo cáo sửa của Claude", "", report_text.strip() or "(không có)", ""]
    verify_path = os.path.join(run_dir, "verify.log")
    if os.path.exists(verify_path):
        with open(verify_path, encoding="utf-8", errors="replace") as f:
            sections += ["## Build / test tự động (cuối log)", "", "```", f.read()[-4000:].strip(), "```", ""]
    path = os.path.join(out_dir, run_name + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(sections))
    return path


def clean(acfg, run_name):
    root = os.path.expanduser(acfg["root"])
    run_dir = os.path.join(root, run_name)
    for r in R.repos(acfg):
        repo, path = r["path"], os.path.join(run_dir, r["key"])
        if os.path.isdir(path):
            subprocess.run(["git", "worktree", "remove", "--force", path], cwd=repo, check=False)
        subprocess.run(["git", "branch", "-D", "auto/" + run_name], cwd=repo, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shutil.rmtree(run_dir, ignore_errors=True)
    log("đã xoá %s" % run_name)


# =================================================================== tiến trình

def status_dir(root):
    path = os.path.join(root, ".status")
    os.makedirs(path, exist_ok=True)
    return path


def set_status(root, display_id, **fields):
    """~/autofix/.status/<SOAR-xxxx>.json: queued → preparing → running → done | error."""
    path = os.path.join(status_dir(root), display_id + ".json")
    # Khoá quanh đọc-sửa-ghi: tiến trình phục hồi (--recover) và tiến trình con nó vừa mở cùng ghi một file.
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        if fields.get("state") in ("done", "paused"):
            data.pop("recover_attempts", None)  # lượt đã kết thúc bình thường: đếm lại số lần bị ngắt từ đầu
        data.update(fields, display_id=display_id, updated_at=datetime.now().isoformat(timespec="seconds"))
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


def load_statuses(root):
    out = []
    for name in sorted(os.listdir(status_dir(root))):
        if name.endswith(".json"):
            try:
                with open(os.path.join(status_dir(root), name), encoding="utf-8") as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                pass
    return out


ACTIVE_STATES = ("queued", "preparing", "running", "shipping")


def boot_time():
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime "):
                return float(line.split()[1])
    return 0.0


def proc_start(pid):
    """Thời điểm (epoch) tiến trình pid bắt đầu; None nếu không còn."""
    try:
        with open("/proc/%d/stat" % int(pid)) as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return boot_time() + int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, TypeError, IndexError):
        return None


def status_alive(s):
    """
    Lượt trong trạng thái còn tiến trình chạy thật: PID còn, là op_autofix, và bắt đầu trước lần ghi trạng thái cuối
    (sau khi khởi động lại máy, PID cũ có thể đã thuộc về tiến trình khác).
    """
    pid = s.get("pid")
    start = proc_start(pid) if pid else None
    if start is None:
        return False
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as f:
            if b"op_autofix" not in f.read():
                return False
    except OSError:
        return False
    try:
        return start <= datetime.fromisoformat(s.get("updated_at") or "").timestamp() + 2
    except ValueError:
        return True


def describe(event):
    """Một dòng ngắn cho mỗi thao tác Claude trong luồng stream-json; None nếu không đáng in."""
    if event.get("type") == "assistant":
        lines = []
        for item in (event.get("message") or {}).get("content") or []:
            if item.get("type") == "tool_use":
                inp = item.get("input") or {}
                target = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                lines.append("%s %s" % (item.get("name"), str(target).replace("\n", " ")[:140]))
            elif item.get("type") == "text" and item.get("text", "").strip():
                lines.append("💬 " + item["text"].strip().replace("\n", " ")[:160])
        return lines
    if event.get("type") == "rate_limit_event":
        return []
    if event.get("type") == "result":
        return ["■ kết thúc: %s · %s lượt · ~%.2f USD" % (
            "lỗi" if event.get("is_error") else "xong", event.get("num_turns"), event.get("total_cost_usd") or 0)]
    return []


def read_events(path):
    events = []
    if os.path.exists(path):
        # errors="replace": file ghi dở (vd đĩa đầy) có byte hỏng → bỏ dòng đó, không làm hỏng cả tool (chọn gói Claude
        # đọc file này của lượt gần nhất; lỗi ở đây từng làm mọi lượt autofix chết ngay khi khởi động).
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
    return events


# =================================================================== gói Claude (nhiều tài khoản)

_LAST_LIMIT_CACHE = {}


def find_profile(acfg, name):
    """Gói theo tên (không có / không còn trong cấu hình → gói đầu tiên)."""
    profs = profiles(acfg)
    return next((p for p in profs if p["name"] == name), profs[0] if profs else None)


def profiles(acfg):
    """
    Các gói trong cấu hình. Hai kiểu gói:
      {"name", "config_dir"}  — thư mục đã /login (logged_in = có .credentials.json);
      {"name", "token_file"}  — token của `claude setup-token` lưu trong file (logged_in = file có token).
    Gói token chạy với thư mục cấu hình mặc định (~/.claude) để giữ settings, chỉ đổi tài khoản tính hạn mức.
    """
    out = []
    for p in acfg.get("profiles") or AUTOFIX_DEFAULTS["profiles"]:
        if p.get("token_file"):
            tf = os.path.expanduser(p["token_file"])
            out.append(dict(p, token_file=tf, config_dir=os.path.expanduser(p.get("config_dir") or "~/.claude"),
                            kind="token", logged_in=bool(read_token(tf))))
        else:
            d = os.path.expanduser(p["config_dir"])
            out.append(dict(p, config_dir=d, kind="dir",
                            logged_in=os.path.exists(os.path.join(d, ".credentials.json"))))
    return out


def read_token(path):
    if not path:
        return ""
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def profile_prefs(root):
    """<root>/.profile.json: {"active": tên gói, "auto_switch": bool} — trang theo dõi ghi, tool đọc."""
    path = os.path.join(root, ".profile.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_profile_prefs(root, **fields):
    data = profile_prefs(root)
    data.update(fields)
    path = os.path.join(root, ".profile.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)


def use_profile(acfg, prof):
    """Các lệnh Claude sau đó trong lượt chạy (build_env) dùng gói này."""
    acfg["_profile"], acfg["_config_dir"], acfg["_token_file"] = prof["name"], prof["config_dir"], prof.get("token_file")


def last_limit_event(path):
    """rate_limit_event cuối cùng trong một file stream-json (cache theo mtime: trang theo dõi gọi liên tục)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cached = _LAST_LIMIT_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    info = None
    for e in read_events(path):
        if e.get("type") == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
    _LAST_LIMIT_CACHE[path] = (mtime, info)
    return info


def profile_usage(root, acfg, name):
    """Hạn mức mới nhất của một gói: từ các lượt chạy bằng gói đó + lượt kiểm tra (--probe). Trả (info, mtime)."""
    default = (profiles(acfg) or [{}])[0].get("name")
    paths = [os.path.join(root, ".usage", name + ".jsonl")]
    for st in load_statuses(root):
        # Lượt chạy trước khi có nhiều gói (không ghi profile) là của gói đầu tiên.
        if st.get("run_dir") and (st.get("profile") or default) == name:
            paths.append(os.path.join(st["run_dir"], "claude.jsonl"))
    best = (None, 0)
    for path in paths:
        info = last_limit_event(path)
        if info:
            mtime = os.path.getmtime(path)
            if mtime > best[1]:
                best = (info, mtime)
    return best


def window_used(info, key):
    """Mức đã dùng (0..1) của cửa sổ five_hour / seven_day; đã qua giờ làm mới thì coi như 0."""
    w = ((info or {}).get("unifiedWindows") or {}).get(key) or {}
    if w.get("resetsAt") and time.time() > w["resetsAt"]:
        return 0.0
    return w.get("utilization") or 0.0


def profile_block(root, acfg, prof):
    """Lý do không dùng được gói (chuỗi rỗng = dùng được)."""
    if not prof["logged_in"]:
        return "%s chưa %s" % (prof["name"], "có token" if prof.get("kind") == "token" else "đăng nhập")
    info, _ = profile_usage(root, acfg, prof["name"])
    if not info:
        return ""
    if info.get("status") == "rejected" and (info.get("resetsAt") or 0) > time.time():
        return "%s đã hết hạn mức (làm mới %s)" % (
            prof["name"], datetime.fromtimestamp(info["resetsAt"]).strftime("%d/%m %H:%M"))
    # --ignore-limits (chạy tay từ trang theo dõi): bỏ qua ngưỡng tự đặt; gói đã bị chặn thật (rejected) thì vẫn không dùng.
    if acfg.get("_ignore_limits"):
        return ""
    lim = profile_limits(acfg, prof)
    seven = lim["followup_7d"] if acfg.get("_followup") else lim["seven_day"]
    for key, label, limit in (("five_hour", "5 giờ", lim["five_hour"]), ("seven_day", "7 ngày", seven)):
        used = window_used(info, key)
        if limit and used >= limit:
            return "%s: hạn mức %s đã dùng %d%% (ngưỡng %d%%)" % (prof["name"], label, used * 100, limit * 100)
    return ""


def profile_limits(acfg, prof):
    """
    Ngưỡng của một gói (0..1): `limits` riêng của gói trong config ({"five_hour", "seven_day", "followup_7d"}),
    thiếu khoá nào thì lấy ngưỡng chung (pause_above_utilization / switch_above_7d / followup_skip_above_7d).
    """
    own = prof.get("limits") or {}
    seven = own.get("seven_day", acfg.get("switch_above_7d"))
    return {"five_hour": own.get("five_hour", acfg.get("pause_above_utilization")), "seven_day": seven,
            "followup_7d": own.get("followup_7d", acfg.get("followup_skip_above_7d") or seven)}


def profile_remaining(root, acfg, prof):
    """Phần hạn mức còn lại (0..1) = 1 − max(đã dùng 5 giờ, đã dùng 7 ngày); None = chưa có dữ liệu."""
    info, _ = profile_usage(root, acfg, prof["name"])
    if not info:
        return None
    if info.get("status") == "rejected" and (info.get("resetsAt") or 0) > time.time():
        return 0.0
    return max(0.0, 1.0 - max(window_used(info, "five_hour"), window_used(info, "seven_day")))


def profile_order(root, acfg):
    """
    Thứ tự thử các gói. Trả (danh sách gói, chế độ):
      "priority" — đã đặt thứ tự ưu tiên trên trang theo dõi (.profile.json "priority"); gói chưa có trong danh sách
                   xếp cuối theo thứ tự cấu hình;
      "quota"    — chưa đặt gì: gói còn nhiều hạn mức nhất trước (gói chưa có dữ liệu hạn mức coi như còn đủ;
                   bằng nhau thì theo thứ tự cấu hình).
    """
    profs = profiles(acfg)
    names = [p["name"] for p in profs]
    prio = [n for n in (profile_prefs(root).get("priority") or []) if n in names]
    if prio:
        return [profs[names.index(n)] for n in prio] + [p for p in profs if p["name"] not in prio], "priority"

    def key(ip):
        i, p = ip
        r = profile_remaining(root, acfg, p)
        return (-(1.0 if r is None else r), i)
    return [p for _, p in sorted(enumerate(profs), key=key)], "quota"


def rename_profile_data(root, old, new):
    """Đổi tên gói trong dữ liệu của tool: hạn mức đã lưu, trạng thái lượt chạy, thứ tự ưu tiên."""
    usage = os.path.join(root, ".usage")
    if os.path.exists(os.path.join(usage, old + ".jsonl")):
        os.replace(os.path.join(usage, old + ".jsonl"), os.path.join(usage, new + ".jsonl"))
    for path in glob.glob(os.path.join(status_dir(root), "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if data.get("profile") == old:
            data["profile"] = new
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(path + ".tmp", path)
    prefs = profile_prefs(root)
    fields = {}
    if prefs.get("active") == old:
        fields["active"] = new
    if old in (prefs.get("priority") or []):
        fields["priority"] = [new if n == old else n for n in prefs["priority"]]
    if fields:
        save_profile_prefs(root, **fields)


def choose_profile(root, acfg, prefer=None, exclude=()):
    """
    Gói cho lượt chạy: gói `prefer` (chạy tay chỉ định gói) rồi tới các gói theo profile_order (thứ tự ưu tiên đã đặt,
    hoặc còn nhiều hạn mức nhất). Tự chuyển tắt thì chỉ thử gói đầu tiên.
    Trả (gói, ghi chú) — gói None khi không gói nào dùng được, ghi chú là lý do.
    """
    prefs = profile_prefs(root)
    auto = prefs.get("auto_switch", acfg.get("auto_switch", True))
    order = profile_order(root, acfg)[0]
    if prefer and any(p["name"] == prefer for p in order):
        order = [p for p in order if p["name"] == prefer] + [p for p in order if p["name"] != prefer]
    if not auto:
        order = order[:1]
    reasons = []
    for prof in order:
        if prof["name"] in exclude:
            continue
        why = profile_block(root, acfg, prof)
        if not why:
            note = "đổi sang %s (%s)" % (prof["name"], "; ".join(reasons)) if reasons else ""
            return prof, note
        reasons.append(why)
    if not auto and len(profiles(acfg)) > 1:
        reasons.append("tự chuyển gói đang tắt")
    return None, "; ".join(reasons) or "không có gói nào"


def hit_limit(path):
    """Phiên Claude bị dừng vì hết hạn mức gói (rate_limit_event rejected / kết quả lỗi nói về limit)?"""
    for e in read_events(path):
        if e.get("type") == "rate_limit_event" and (e.get("rate_limit_info") or {}).get("status") == "rejected":
            return True
        if e.get("type") == "result" and e.get("is_error") and re.search(
                r"usage limit|rate limit|limit reached|hit your limit", str(e.get("result") or ""), re.I):
            return True
    return False


def probe_profile(acfg, prof):
    """Gọi Claude một câu rất ngắn bằng gói này để lấy hạn mức hiện tại (ghi <root>/.usage/<gói>.jsonl)."""
    root = os.path.expanduser(acfg["root"])
    os.makedirs(os.path.join(root, ".usage"), exist_ok=True)
    sub = dict(acfg)
    use_profile(sub, prof)
    with open(os.path.join(root, ".usage", prof["name"] + ".jsonl"), "w", encoding="utf-8") as out:
        return subprocess.run([acfg["claude"], "-p", "Trả lời đúng một chữ: ok", "--output-format", "stream-json",
                               "--verbose", "--tools", ""],
                              cwd=root, stdout=out, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                              env=build_env(sub), timeout=180).returncode


def usage_line(info):
    if not info:
        return "chưa có dữ liệu (cần một lượt chạy hoặc --probe)"
    windows = info.get("unifiedWindows") or {}
    parts = []
    for key, label in (("five_hour", "5 giờ"), ("seven_day", "7 ngày")):
        w = windows.get(key)
        if w:
            reset = datetime.fromtimestamp(w["resetsAt"]).strftime("%d/%m %H:%M") if w.get("resetsAt") else "?"
            parts.append("%s %d%% (làm mới %s)" % (label, round(window_used(info, key) * 100), reset))
    overage = "không tính phí vượt hạn mức" if info.get("overageStatus") == "rejected" else \
        "vượt hạn mức: %s" % info.get("overageStatus")
    return "%s · %s" % (", ".join(parts) or "?", overage)


def show_status(root, acfg, last=8):
    active_name = find_profile(acfg, profile_prefs(root).get("active"))["name"]
    for prof in profiles(acfg):
        print("%s %s%s: %s" % ("▶" if prof["name"] == active_name else " ", prof["name"],
                               "" if prof["logged_in"] else " (chưa đăng nhập)",
                               usage_line(profile_usage(root, acfg, prof["name"])[0])))
    statuses = load_statuses(root)
    if not statuses:
        print("Chưa có lượt autofix nào.")
        return
    active = [s for s in statuses if s.get("state") in ("queued", "preparing", "running", "shipping")]
    done = sorted([s for s in statuses if s not in active], key=lambda s: s.get("updated_at", ""), reverse=True)
    for s in active + done[:5]:
        since = s.get("started_at") or s.get("queued_at") or ""
        elapsed = ""
        if s.get("state") in ("preparing", "running", "shipping") and since:
            elapsed = " · %d phút" % ((datetime.now() - datetime.fromisoformat(since)).total_seconds() // 60)
        print("%-10s %-9s %s%s%s%s" % (s["display_id"], s.get("state"), since[5:16].replace("T", " "), elapsed,
                                      (" · " + s["profile"]) if s.get("profile") else "",
                                      (" · " + s["verdict"]) if s.get("verdict") else ""))
        if s.get("run_dir"):
            print("           %s" % s["run_dir"])
        if s.get("state") != "running":
            continue
        events = read_events(os.path.join(s["run_dir"], "claude.jsonl"))
        actions = [line for e in events for line in describe(e)]
        tools = sum(1 for line in actions if not line.startswith(("💬", "■")))
        print("           %d thao tác — gần nhất:" % tools)
        for line in actions[-last:]:
            print("             · " + line)
        for r, path in R.trees(acfg, s["run_dir"]):
            sub = r["key"]
            if os.path.isdir(path):
                res = subprocess.run(["git", "diff", "--shortstat", "HEAD"], cwd=path, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, universal_newlines=True)
                stat = res.stdout.strip() if res.returncode == 0 else ""
                print("           %s: %s" % (sub.upper(), stat or "chưa sửa file"))


def follow(root, target):
    statuses = [s for s in load_statuses(root) if s.get("state") == "running"]
    if target:
        statuses = [s for s in load_statuses(root) if s["display_id"] == target or s.get("run_name") == target]
    if not statuses or not statuses[-1].get("run_dir"):
        print("Không có lượt nào đang chạy%s." % (" cho " + target if target else ""))
        return
    s = statuses[-1]
    path = os.path.join(s["run_dir"], "claude.jsonl")
    print("Theo dõi %s — %s (Ctrl+C để thoát, phiên vẫn chạy tiếp)" % (s["display_id"], path))
    pos = 0
    while True:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                f.seek(pos)
                for line in f:
                    if not line.endswith("\n"):
                        break
                    pos += len(line.encode("utf-8"))
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    for text in describe(event):
                        print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), text), flush=True)
                    if event.get("type") == "result":
                        return
        time.sleep(1)


def ticket_commits(repo_dir, display_id):
    """Commit đã sửa ticket trên nhánh hiện tại (mới nhất trước), nhận theo tiền tố `[SOAR-xxxx]` của message."""
    out = run(["git", "log", "--no-merges", "--format=%H %s", "HEAD"], cwd=repo_dir, check=False)
    tag = "[%s]" % display_id
    return [line.split(" ", 1)[0] for line in out.splitlines() if " " in line and tag in line.split(" ", 1)[1][:40]]


def review_ticket(api, cfg, acfg, ref, profile=None, gemini_from=None):
    """
    Test lại bằng Gemini một ticket đã sửa + push trước đây (--review). Tạo worktree mới từ nhánh của mình, lấy các
    commit `[SOAR-xxxx]`, build/test, Gemini test, Claude xem lại ý chưa đạt; chỉ push nếu Claude sửa thêm.
    """
    root = os.path.expanduser(acfg["root"])
    wp = api.get("/api/v3/work_packages/%s" % ref)
    wp_id, display_id = wp["id"], wp.get("displayId") or "WP-%s" % wp["id"]
    # QA trên server đang chạy (deploy_qa) thì ưu tiên nó: chờ xong mới test lại ticket cũ.
    qa_flag = os.path.join(root, ".qa-active")
    if os.path.exists(qa_flag):
        log("%s: chờ QA trên server xong (ưu tiên QA)" % display_id)
        while os.path.exists(qa_flag) and time.time() - os.path.getmtime(qa_flag) < 6 * 3600:
            time.sleep(30)
    global_lock = acquire_slot(root, acfg, wait=False)
    if global_lock is None:
        log("%s: chờ có suất chạy (đang chạy đủ %s lượt)" % (display_id, acfg.get("max_parallel")))
        global_lock = acquire_slot(root, acfg)
    # Gói cho bước Claude xem lại; không gói nào dùng được thì deliver tự bỏ bước đó.
    prof, _ = choose_profile(root, acfg, profile)
    if prof:
        use_profile(acfg, prof)
    run_name = "%s-%s-%s" % (display_id, "followup" if gemini_from else "review", datetime.now().strftime("%m%d-%H%M"))
    run_dir = os.path.join(root, run_name)
    fe_dir, be_dir = os.path.join(run_dir, "fe"), os.path.join(run_dir, "be")
    set_status(root, display_id, state="shipping", event="FOLLOWUP" if gemini_from else "REVIEW", run_dir=run_dir, run_name=run_name, verdict=None,
               wp_id=wp_id, profile=acfg.get("_profile"),
               started_at=datetime.now().isoformat(timespec="seconds"), pid=os.getpid())
    try:
        log("%s: test lại (%s)" % (display_id, run_dir))
        notify("[GEMINI TEST] %s" % display_id, "Bắt đầu test lại bản đã sửa: " + wp.get("subject", ""))
        wp = fetch_ticket(api, wp_id, os.path.join(run_dir, "ticket"))
        make_trees(acfg, run_dir, "auto/" + run_name)
        review_of = tree_commits(acfg, run_dir, display_id)
        review_of = {k: v for k, v in review_of.items() if v}
        # Báo cáo sửa cũ (nếu thư mục lượt sửa còn) giúp Gemini hiểu ý định.
        old = sorted(p for p in glob.glob(os.path.join(root, display_id + "-1*", "report.md")))
        report_text = open(old[-1], encoding="utf-8").read() if old else ""
        if report_text:
            shutil.copy(old[-1], os.path.join(run_dir, "report.md"))
        if not review_of:
            verdict = "không tìm thấy commit [%s] trên nhánh" % display_id
        else:
            _, verdict = deliver(api, cfg, acfg, wp_id, display_id, wp, run_dir, fe_dir, be_dir, report_text,
                                 review_of=review_of, gemini_from=gemini_from)
            if gemini_from:
                verdict += followup_done_status(api, acfg, root, wp_id, display_id, verdict)
        commits = ["%s %s" % (k, ", ".join(c[:8] for c in v)) for k, v in review_of.items()]
        saved = write_summary_report(acfg, display_id, run_name, wp, "TEST LẠI: " + verdict, commits, run_dir,
                                     report_text)
        set_status(root, display_id, state="done", verdict="test lại: " + verdict, commits=commits)
        inbox(cfg, "**TEST LẠI** %s — %s · báo cáo: %s" % (display_id, verdict, saved))
        notify("[TEST LẠI] %s: %s" % (display_id, verdict[:120]), wp.get("subject", ""))
    except Exception as ex:  # noqa: BLE001
        log("%s: test lại lỗi %s" % (display_id, ex))
        set_status(root, display_id, state="error", verdict="test lại lỗi: " + str(ex).splitlines()[0][:200])
        inbox(cfg, "**TEST LẠI LỖI** %s — %s (thư mục %s)" % (display_id, str(ex).splitlines()[0], run_dir))
        notify("[TEST LẠI LỖI] %s" % display_id, str(ex)[:200])
    finally:
        fcntl.flock(global_lock, fcntl.LOCK_UN)


def ship_run(cfg, acfg, run_name, profile=None):
    """Giao lại một lượt ĐÃ SỬA bị dừng ở bước kiểm tra / push (vd test lỗi sẵn trên nhánh chung)."""
    root = os.path.expanduser(acfg["root"])
    if re.match(r"^[A-Z][A-Z0-9]*-\d+$", run_name):  # chỉ mã ticket → lượt chạy mới nhất có report.md
        runs = sorted((d for d in os.listdir(root) if d.startswith(run_name + "-")
                       and os.path.isfile(os.path.join(root, d, "report.md"))),
                      key=lambda d: os.path.getmtime(os.path.join(root, d)))
        if runs:
            log("%s: dùng lượt chạy mới nhất %s" % (run_name, runs[-1]))
            run_name = runs[-1]
    run_dir = os.path.join(root, run_name)
    m = re.match(r"^((?:[A-Z][A-Z0-9]*)-\d+)-", run_name)
    if not m or not os.path.isdir(run_dir):
        sys.exit("không có lượt chạy %s" % run_name)
    display_id = m.group(1)
    report_path = os.path.join(run_dir, "report.md")
    report_text = open(report_path, encoding="utf-8").read() if os.path.exists(report_path) else ""
    kind = next((k for k in ("ĐÃ SỬA", "KHÔNG SỬA ĐƯỢC") if report_text.lstrip().startswith("KẾT QUẢ: " + k)), None)
    if not kind:
        sys.exit("%s: báo cáo không phải ĐÃ SỬA / KHÔNG SỬA ĐƯỢC — không giao lại" % run_name)
    fe_dir, be_dir = os.path.join(run_dir, "fe"), os.path.join(run_dir, "be")
    for r, d in R.trees(acfg, run_dir):  # cấu hình thư mục dùng chung / setup có thể đổi sau khi tạo worktree
        if os.path.isdir(d):
            R.link_shared(r, d)
            err = R.run_setup(r, d, build_env(acfg), os.path.join(run_dir, "setup.log"))
            if err:
                log(err)
    has_fix = any_fix(acfg, run_dir)
    ticket_lock = open(os.path.join(root, ".%s.lock" % display_id), "w")
    try:
        fcntl.flock(ticket_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("%s đang được xử lý" % display_id)
    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    wp = api.get("/api/v3/work_packages/%s" % display_id)
    wp_id = str(wp["id"])
    prof, why = choose_profile(root, acfg, profile)
    if prof:
        use_profile(acfg, prof)
    slot = acquire_slot(root, acfg)
    try:
        set_status(root, display_id, state="shipping", run_dir=run_dir, run_name=run_name, wp_id=wp_id,
                   pid=os.getpid(), event="SHIP")
        log("%s: giao lại %s (%s)" % (display_id, run_name, "kiểm tra lại + push" if has_fix else kind.lower()))
        try:
            if kind == "KHÔNG SỬA ĐƯỢC":
                report_cannot_fix(api, acfg, wp_id, report_text, run_name)
                shipped, ship_msg = False, "đã comment, chuyển UNREVIEWED"
            elif not has_fix:
                shipped, ship_msg = False, deliver_already_fixed(api, cfg, acfg, wp_id, display_id, wp, run_dir,
                                                                 fe_dir, be_dir, report_text)
            else:
                shipped, ship_msg = deliver(api, cfg, acfg, wp_id, display_id, wp, run_dir, fe_dir, be_dir,
                                            report_text)
        except Exception as ex:  # noqa: BLE001
            shipped, ship_msg = False, "ship lỗi: %s" % str(ex).splitlines()[0][:200]
        manual = has_fix and not shipped and "UNREVIEWED" not in ship_msg
        verdict = kind + " → " + ship_msg + (" (sửa tay rồi push)" if manual else "")
        set_status(root, display_id, state="done", verdict=verdict)
        inbox(cfg, "**AUTOFIX** %s — giao lại %s: %s" % (display_id, run_name, verdict))
        notify("[AUTOFIX] %s: %s" % (display_id, verdict[:120]), wp.get("subject", ""))
        print(verdict)
    finally:
        if slot:
            slot.close()


SOAR_API = os.path.join(os.path.dirname(os.path.abspath(__file__)), "soar_api.py")
UI_CHECK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_check.py")

ASSIST_NOTE = """

## BƯỚC CLAUDE HỖ TRỢ (lý do lượt chạy này)
Lượt trước của ticket này kết luận CẦN QUYẾT ĐỊNH / chưa làm được vì thiếu thứ mà lượt chạy nền không tự lấy được.
Báo cáo của lượt trước (đọc kỹ mục "Cần quyết định / rủi ro" và "Người quyết định"): {before}
Công cụ ĐÃ lấy sẵn thêm tài liệu cho bạn:
{material}
{api_note}{note}
## Làm
1. Đọc lý do bị kẹt trong báo cáo lượt trước, rồi dùng tài liệu trên để gỡ: file đính kèm đúng tên gốc nằm trong
   {run_dir}/assist/attachments/ (svg / ảnh / tài liệu… — đưa vào repo bằng Read rồi Write nếu cần dùng làm asset);
   ticket liên quan trong {run_dir}/assist/related/<MÃ>/ticket.md; dữ liệu thật trên server test qua GET API.
2. Gỡ được → làm tiếp việc sửa như hướng dẫn ở trên (sửa, tự kiểm tra hồi quy, build / test, commit) và ghi lại
   {run_dir}/report.md với `KẾT QUẢ: ĐÃ SỬA` (ghi rõ trong "Nguyên nhân" đã dùng tài liệu nào để gỡ).
3. Vẫn cần người quyết (đổi hành vi sản phẩm, thiếu thông tin không có trong tài liệu, ý kiến trái nhau…) → KHÔNG đoán,
   ghi lại report.md `KẾT QUẢ: CẦN QUYẾT ĐỊNH`, mục "Cần quyết định / rủi ro" nói rõ đã thử tài liệu nào và vì sao vẫn
   chưa đủ, câu hỏi cụ thể cho người quyết.
4. Worktree có thể đã có commit của lượt trước: xem `git -C <worktree> log --oneline -3` trước, không commit trùng.
5. Có ảnh minh chứng (lỗi giao diện, hoặc đã test trên giao diện) → thêm vào report.md mục `## Ảnh minh chứng`, mỗi ảnh
   một dòng `- TRƯỚC: <đường dẫn ảnh> — <chú thích ngắn cho tester>` hoặc `- SAU: …` (tối đa 6 ảnh). TRƯỚC = hiện
   trạng lỗi: ảnh gốc của tester ({run_dir}/ticket/images/…) và / hoặc ảnh chụp server test có khoanh vùng lỗi; SAU =
   ảnh đã đúng (chỉ khi server đã chạy bản có bản sửa). Công cụ tự upload ảnh và chèn vào comment trên ticket.
"""

ASSIST_API_NOTE = """Gọi API server test (CHỈ ĐỌC, tự đăng nhập): `python3 {soar_api} GET /api/v1/...` (một lệnh đơn đúng dạng này;
file tĩnh FE: `python3 {soar_api} GET {fe_url}/index.html`). Không gọi được POST / PUT / DELETE: cần ghi dữ liệu thì
ghi vào báo cáo. Server test có thể đang chạy bản cũ hơn code trong worktree.
Test trên GIAO DIỆN thật (trình duyệt, tự đăng nhập bằng tài khoản / OTP ghi trong ticket): ghi các bước bằng Write vào
{run_dir}/ui/<tên>.json rồi chạy `python3 {ui_check} {run_dir}/ui/<tên>.json` (một lệnh đơn). Ảnh ra
{run_dir}/ui/<tên>-shots/*.png — xem bằng Read. Bước (mảng JSON): {{"goto": "/duong-dan"}}, {{"click": "text=Lưu"}},
{{"fill": ["css", "chữ"]}}, {{"press": "Enter"}}, {{"wait": 1500}}, {{"text": "css"}}, {{"style": ["css", ["color"]]}},
{{"screenshot": "ten", "mark": ["css vùng cần chú ý", …], "note": "nhãn ngắn"}} (khoanh đỏ vùng cần chú ý). Màn hình
đăng nhập / trang công khai: {{"login": false, "steps": [...]}}. Chỉ thao tác để tái hiện / kiểm tra, KHÔNG tạo / sửa /
xoá dữ liệu thật (nút xoá / đăng xuất bị chặn). Giao diện server là bản ĐÃ DEPLOY (chưa có bản sửa trong worktree).
"""


def _safe_name(name):
    name = re.sub(r"[^\w.\-() ]+", "_", os.path.basename(name or "file")).strip(" .") or "file"
    return name[:120]


def gather_assist(api, acfg, wp, run_dir, report_text, note=""):
    """Tài liệu cho bước Claude hỗ trợ (công cụ có mạng, Claude thì không): mọi file đính kèm + ticket liên quan.
    Trả danh sách dòng mô tả (đưa vào prompt)."""
    display_id = wp.get("displayId") or "WP-%s" % wp["id"]
    out = os.path.join(run_dir, "assist")
    att_dir, rel_dir = os.path.join(out, "attachments"), os.path.join(out, "related")
    os.makedirs(att_dir, exist_ok=True)
    os.makedirs(rel_dir, exist_ok=True)
    lines = []
    try:
        atts = api.get("/api/v3/work_packages/%s/attachments" % wp["id"]).get("_embedded", {}).get("elements", [])
    except (op_watch.urllib.error.URLError, OSError, ValueError) as ex:
        atts = []
        lines.append("- (không lấy được danh sách file đính kèm: %s)" % ex)
    used = set()
    for a in atts:
        name = _safe_name(a.get("fileName"))
        if name in used:
            name = "%s-%s" % (a.get("id"), name)
        used.add(name)
        size = a.get("fileSize") or 0
        if size > 20 * 1024 * 1024:
            lines.append("- %s (%d MB) — bỏ qua, quá 20 MB" % (name, size // 1048576))
            continue
        path = os.path.join(att_dir, name)
        req = op_watch.urllib.request.Request(api.url + "/api/v3/attachments/%s/content" % a.get("id"),
                                              headers=api.headers)
        try:
            with op_watch.urllib.request.urlopen(req, timeout=60) as resp, open(path, "wb") as f:
                shutil.copyfileobj(resp, f)
            lines.append("- %s (%s, %s byte, đăng lúc %s)" % (path, a.get("contentType") or "?", size,
                                                            (a.get("createdAt") or "")[:16].replace("T", " ")))
        except (op_watch.urllib.error.URLError, OSError) as ex:
            lines.append("- %s — tải lỗi: %s" % (name, ex))
    text = report_text + "\n" + (note or "")
    try:
        with open(os.path.join(run_dir, "ticket", "ticket.md"), encoding="utf-8") as f:
            text = f.read() + "\n" + text
    except OSError:
        pass
    related = []
    for rid in re.findall(r"\b(%s)\b" % envconf.TICKET_RX, text):
        if rid != display_id and rid not in related:
            related.append(rid)
    for rid in related[:int(acfg.get("assist_max_related") or 8)]:
        try:
            w = api.get("/api/v3/work_packages/%s" % rid)
            fetch_ticket(api, w["id"], os.path.join(rel_dir, rid), figma_designs=False)
            lines.append("- Ticket liên quan %s — %s: %s/ticket.md" % (rid, w.get("subject", ""),
                                                                    os.path.join(rel_dir, rid)))
        except (op_watch.urllib.error.URLError, OSError, ValueError, KeyError) as ex:
            lines.append("- Ticket liên quan %s — không tải được: %s" % (rid, ex))
    if not lines:
        lines.append("- (ticket không có file đính kèm / ticket liên quan)")
    with open(os.path.join(out, "material.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log("%s: hỗ trợ — %d file đính kèm, %d ticket liên quan" % (display_id, len(atts), min(len(related),
                                                                 int(acfg.get("assist_max_related") or 8))))
    return lines


def report_verdict(report_path):
    """(nội dung report.md, kết luận dòng đầu không có "KẾT QUẢ:")."""
    try:
        with open(report_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return "", ""
    first = text.strip().splitlines()[0] if text.strip() else ""
    return text, first.replace("KẾT QUẢ:", "").strip() or first


def assist_step(api, cfg, acfg, wp, run_dir, report_text, note=""):
    """Một lượt Claude hỗ trợ trên worktree của lượt chạy: tải tài liệu, chạy Claude (thêm quyền GET API server test),
    trả (report_text, verdict) mới. Báo cáo cũ giữ ở report-before-assist.md."""
    display_id = wp.get("displayId") or "WP-%s" % wp["id"]
    report = os.path.join(run_dir, "report.md")
    before = os.path.join(run_dir, "report-before-assist.md")
    if os.path.exists(report):
        shutil.copy(report, before)
    material = gather_assist(api, acfg, wp, run_dir, report_text, note)
    try:
        dcfg = envconf.deploy_config()
    except Exception:  # noqa: BLE001 — thiếu cấu hình server test thì chỉ không cho gọi API
        dcfg = {}
    fe_url = dcfg.get("fe_url") or ""
    api_ok = os.path.exists(SOAR_API) and bool(dcfg.get("api_url"))
    with open(os.path.join(run_dir, "prompt.md"), encoding="utf-8") as f:
        prompt = f.read()
    prompt += ASSIST_NOTE.format(
        before=before if os.path.exists(before) else "(không có)", material="\n".join(material), run_dir=run_dir,
        api_note=ASSIST_API_NOTE.format(soar_api=SOAR_API, fe_url=fe_url, ui_check=UI_CHECK, run_dir=run_dir)
        if api_ok else "",
        note=("\n## Ghi chú của dev cho lượt hỗ trợ này (làm theo)\n%s\n" % note.strip()) if note and note.strip() else "")
    with open(os.path.join(run_dir, "assist", "prompt.md"), "w", encoding="utf-8") as f:
        f.write(prompt)
    log("%s: Claude hỗ trợ (tài liệu thêm + GET API server test)%s" % (display_id, " — có ghi chú" if note else ""))
    inbox(cfg, "**AUTOFIX HỖ TRỢ** %s — agent cần hỗ trợ, Claude đang gỡ bằng file đính kèm / ticket liên quan / API "
               "server test" % display_id)
    t0 = time.time()
    acfg["_assist"] = True
    try:
        code = run_claude(acfg, prompt, run_dir, append=True)
    finally:
        acfg.pop("_assist", None)
    log_time(api, acfg, str(wp["id"]), time.time() - t0, "dev",
             "[Autofix] Claude hỗ trợ (%s)" % os.path.basename(run_dir))
    with open(os.path.join(run_dir, "assist", "done"), "w") as f:
        f.write(datetime.now().isoformat(timespec="seconds"))
    text, verdict = report_verdict(report)
    if not text:
        verdict = "KHÔNG CÓ BÁO CÁO sau bước hỗ trợ (Claude thoát mã %s)" % code
    log("%s: sau bước hỗ trợ — %s" % (display_id, verdict))
    return text, verdict


def wants_assist(acfg, verdict, report_text):
    return bool(acfg.get("assist_auto", True)) and verdict.startswith("CẦN QUYẾT ĐỊNH") \
        and review_decider(report_text)[0] == "dev"


def assist_run(cfg, acfg, run_name, note="", profile=None):
    """Nút "Claude hỗ trợ" (chạy tay, kèm ghi chú): một lượt hỗ trợ trên lượt chạy cũ rồi giao kết quả như thường."""
    root = os.path.expanduser(acfg["root"])
    if re.match(r"^%s$" % envconf.TICKET_RX, run_name):
        runs = sorted((d for d in os.listdir(root) if d.startswith(run_name + "-")
                       and os.path.isfile(os.path.join(root, d, "prompt.md"))),
                      key=lambda d: os.path.getmtime(os.path.join(root, d)))
        if runs:
            run_name = runs[-1]
    run_dir = os.path.join(root, run_name)
    m = re.match(r"^(%s)-" % envconf.TICKET_RX, run_name)
    if not m or not os.path.isfile(os.path.join(run_dir, "prompt.md")):
        sys.exit("không có lượt chạy %s" % run_name)
    display_id = m.group(1)
    missing = [r["label"] for r, d in R.trees(acfg, run_dir) if not os.path.isdir(d)]
    if missing:
        sys.exit("%s: worktree %s đã bị dọn — bấm Chạy lại autofix (kèm comment hướng dẫn trên ticket) thay vì hỗ trợ"
                 % (run_name, ", ".join(missing)))
    for r, d in R.trees(acfg, run_dir):
        R.link_shared(r, d)
    ticket_lock = open(os.path.join(root, ".%s.lock" % display_id), "w")
    try:
        fcntl.flock(ticket_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("%s đang được xử lý" % display_id)
    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    wp = api.get("/api/v3/work_packages/%s" % display_id)
    wp_id = str(wp["id"])
    prof, why = choose_profile(root, acfg, profile)
    if prof is None:
        ticket_lock.close()
        sys.exit("%s: không có gói Claude dùng được (%s)" % (display_id, why))
    use_profile(acfg, prof)
    slot = acquire_slot(root, acfg)
    set_status(root, display_id, state="running", run_dir=run_dir, run_name=run_name, wp_id=wp_id, pid=os.getpid(),
               event="ASSIST", profile=acfg.get("_profile"), started_at=datetime.now().isoformat(timespec="seconds"))
    try:
        fetch_ticket(api, wp_id, os.path.join(run_dir, "ticket"))  # comment mới nhất (vd hướng dẫn của dev)
        report_text, _old = report_verdict(os.path.join(run_dir, "report.md"))
        report_text, verdict = assist_step(api, cfg, acfg, wp, run_dir, report_text, note)
    except Exception as ex:  # noqa: BLE001
        set_status(root, display_id, state="error", verdict="hỗ trợ lỗi: " + str(ex).splitlines()[0][:200])
        inbox(cfg, "**AUTOFIX LỖI** %s — bước hỗ trợ: %s" % (display_id, str(ex).splitlines()[0]))
        slot.close()
        ticket_lock.close()
        raise
    slot.close()
    ticket_lock.close()  # ship_run tự lấy lại khoá ticket + suất chạy
    if verdict.startswith(("ĐÃ SỬA", "KHÔNG SỬA ĐƯỢC")):
        ship_run(cfg, acfg, run_name, acfg.get("_profile"))
        return
    if verdict.startswith("CẦN QUYẾT ĐỊNH") and acfg.get("review_status_id"):
        try:
            mark_for_review(api, wp_id, acfg["review_status_id"], report_text, run_name, run_dir)
            verdict += " → đã chuyển UNREVIEWED"
        except (op_watch.urllib.error.URLError, OSError, ValueError, KeyError) as ex:
            verdict += " (chuyển UNREVIEWED lỗi: %s)" % ex
    elif verdict.startswith("CẦN TEST TAY"):
        try:
            verdict += " → " + report_manual_test(api, wp_id, report_text, run_name, acfg.get("review_status_id"))
        except (op_watch.urllib.error.URLError, OSError, ValueError, KeyError) as ex:
            verdict += " (comment lên ticket lỗi: %s)" % ex
    verdict = "Sau hỗ trợ: " + verdict
    set_status(root, display_id, state="done", verdict=verdict)
    inbox(cfg, "**AUTOFIX** %s — %s · báo cáo: %s" % (display_id, verdict, os.path.join(run_dir, "report.md")))
    notify("[AUTOFIX] %s: %s" % (display_id, verdict[:120]), wp.get("subject", ""))
    print(verdict)


# =================================================================== hàng chờ Claude xem lại

def pending_dir(root):
    path = os.path.join(root, ".pending-followup")
    os.makedirs(path, exist_ok=True)
    return path


def queue_followup(root, display_id, wp_id, run_dir, gemini_verdict, reason, pushed=True, attempts=None):
    """Ghi ticket cần Claude xem lại nhận xét Gemini (bị bỏ vì hết hạn mức) vào <root>/.pending-followup/."""
    path = os.path.join(pending_dir(root), display_id + ".json")
    old = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
    data = {"display_id": display_id, "wp_id": str(wp_id), "run_dir": run_dir, "gemini_verdict": gemini_verdict,
            "reason": reason, "pushed": pushed,
            "attempts": old.get("attempts", 0) if attempts is None else attempts,
            "queued_at": old.get("queued_at") or datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds")}
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)


def clear_followup(root, display_id):
    path = os.path.join(pending_dir(root), display_id + ".json")
    if os.path.exists(path):
        os.remove(path)


def load_pending(root):
    out = []
    for name in sorted(os.listdir(pending_dir(root))):
        if name.endswith(".json"):
            with open(os.path.join(pending_dir(root), name), encoding="utf-8") as f:
                out.append(json.load(f))
    return sorted(out, key=lambda p: p.get("queued_at", ""))


def followup_limits(acfg):
    """Cấu hình để chọn gói cho bước Claude xem lại: dùng ngưỡng 7 ngày "xem lại" của từng gói."""
    return dict(acfg, _followup=True)


def backfill_pending(root, acfg, tickets=()):
    """Đưa các lượt đã bỏ bước Claude xem lại (trước khi có hàng chờ) vào hàng chờ. Trả danh sách ticket đã thêm."""
    added = []
    for st in load_statuses(root):
        if tickets and st["display_id"] not in tickets:
            continue
        run_dir = st.get("run_dir") or ""
        if "bỏ Claude xem lại" not in (st.get("verdict") or "") or not os.path.exists(os.path.join(run_dir, "gemini.md")):
            continue
        with open(os.path.join(run_dir, "gemini.md"), encoding="utf-8") as f:
            m = re.search(r"KẾT LUẬN:\s*(KHÔNG ĐẠT|ĐẠT|CẦN XEM)", f.read()[:500])
        verdict = m.group(1) if m else ""
        if verdict not in acfg.get("followup_on", []):
            continue
        wp_id = st.get("wp_id")
        if not wp_id:
            # Lượt cũ không ghi wp_id: lấy từ link ticket trong ticket.md / báo cáo tổng hợp.
            for path in glob.glob(os.path.join(os.path.expanduser(acfg.get("report_dir") or ""),
                                               st.get("run_name", "") + ".md")):
                m = re.search(r"/work_packages/(\d+)", open(path, encoding="utf-8").read())
                wp_id = m.group(1) if m else None
        if not wp_id:
            continue
        queue_followup(root, st["display_id"], wp_id, run_dir, verdict, st.get("verdict"),
                       pushed="đã push" in st.get("verdict", ""))
        added.append(st["display_id"])
    return added


def process_pending(api, cfg, acfg, force=None):
    """
    Chạy Claude xem lại cho các ticket trong hàng chờ khi có gói còn hạn mức (op-watch gọi định kỳ qua on_tick).
    - Bản sửa đã push: worktree mới từ nhánh của mình (review_ticket), dùng lại gemini.md, Claude xem lại, push phần
      sửa thêm + comment.
    - Bản sửa chưa push (Gemini KHÔNG ĐẠT chặn push): làm tiếp ngay trong worktree cũ, xem lại xong mới push + Resolved.
    force: tên ticket — chạy ticket đó ngay (vẫn cần gói còn hạn mức).
    """
    root = os.path.expanduser(acfg["root"])
    pending = [p for p in load_pending(root) if not force or p["display_id"] == force]
    if not pending:
        return
    # Chỉ một tiến trình xử lý hàng chờ mỗi lúc (on_tick của op-watch và lượt sửa mới đều có thể gọi).
    tick_lock = open(os.path.join(root, ".followup.lock"), "w")
    try:
        fcntl.flock(tick_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    for item in pending:
        display_id = item["display_id"]
        st = next((s for s in load_statuses(root) if s["display_id"] == display_id), {})
        # Ticket đã có lượt sửa mới hơn (reopen, chạy lại) thì nhận xét Gemini cũ không còn đúng.
        if st.get("started_at", "") > item["queued_at"] and st.get("event") in ("NEW", "REOPENED", "MANUAL", "QA_FAIL") \
                and os.path.realpath(st.get("run_dir") or "") != os.path.realpath(item["run_dir"]):
            log("%s: bỏ khỏi hàng chờ xem lại (đã có lượt sửa mới hơn)" % display_id)
            clear_followup(root, display_id)
            continue
        if st.get("state") in ("queued", "preparing", "running", "shipping"):
            continue
        prof, why = choose_profile(root, followup_limits(acfg))
        if prof is None:
            log("hàng chờ xem lại: %d ticket, chưa có gói còn hạn mức (%s)" % (len(pending), why))
            return
        if item.get("attempts", 0) >= 3:
            inbox(cfg, "**XEM LẠI BỎ QUA** %s — đã thử 3 lần không xong, xem tay: %s" % (
                display_id, os.path.join(item["run_dir"], "gemini.md")))
            clear_followup(root, display_id)
            continue
        # Đếm số lần thử trước khi chạy: lượt lỗi giữa chừng vẫn còn trong hàng chờ, quá 3 lần thì bỏ.
        queue_followup(root, display_id, item["wp_id"], item["run_dir"], item["gemini_verdict"], item["reason"],
                       item.get("pushed", True), attempts=item.get("attempts", 0) + 1)
        msg = "Gemini %s — có hạn mức (%s), Claude xem lại" % (item["gemini_verdict"], prof["name"])
        log("%s: %s" % (display_id, msg))
        inbox(cfg, "**XEM LẠI TỪ HÀNG CHỜ** %s — %s" % (display_id, msg))
        notify("[XEM LẠI TỪ HÀNG CHỜ] %s" % display_id, msg)
        if not item.get("pushed", True) and all(os.path.isdir(d) for _, d in R.trees(acfg, item["run_dir"])):
            resume_unpushed(api, cfg, acfg, item, prof)
        else:
            review_ticket(api, cfg, acfg, item["wp_id"], prof["name"], gemini_from=item["run_dir"])


def followup_done_status(api, acfg, root, wp_id, display_id, verdict):
    """
    Sau lượt xem lại từ hàng chờ: Claude đã quyết định (ticket ra khỏi hàng chờ) mà ticket vẫn TO DO → Resolved.
    CẦN QUYẾT ĐỊNH thì deliver đã chuyển UNREVIEWED; tester đổi trạng thái khác thì không đụng tới.
    """
    todo = acfg.get("followup_status_id")
    if not todo or os.path.exists(os.path.join(pending_dir(root), display_id + ".json")) or "UNREVIEWED" in verdict:
        return ""
    try:
        if wp_status_id(api, wp_id) != int(todo):
            return ""
        set_wp_status(api, wp_id, acfg["resolved_status_id"])
        return " · xem lại xong, chuyển Resolved"
    except (op_watch.urllib.error.URLError, OSError, ValueError, KeyError) as ex:
        return " · chuyển Resolved lỗi: %s" % ex


def resume_unpushed(api, cfg, acfg, item, prof):
    """Bản sửa bị Gemini chặn push và bỏ bước xem lại: làm tiếp trong worktree cũ (deliver như lượt sửa mới)."""
    root = os.path.expanduser(acfg["root"])
    display_id, run_dir = item["display_id"], item["run_dir"]
    fe_dir, be_dir = os.path.join(run_dir, "fe"), os.path.join(run_dir, "be")
    global_lock = acquire_slot(root, acfg)
    use_profile(acfg, prof)
    set_status(root, display_id, state="shipping", event="FOLLOWUP", profile=prof["name"], pid=os.getpid(),
               started_at=datetime.now().isoformat(timespec="seconds"))
    try:
        wp = api.get("/api/v3/work_packages/%s" % item["wp_id"])
        report_path = os.path.join(run_dir, "report.md")
        report_text = open(report_path, encoding="utf-8").read() if os.path.exists(report_path) else ""
        _, verdict = deliver(api, cfg, acfg, item["wp_id"], display_id, wp, run_dir, fe_dir, be_dir, report_text,
                             gemini_from=run_dir)
        verdict += followup_done_status(api, acfg, root, item["wp_id"], display_id, verdict)
        set_status(root, display_id, state="done", verdict="xem lại: " + verdict)
        inbox(cfg, "**XEM LẠI** %s — %s" % (display_id, verdict))
        notify("[XEM LẠI] %s: %s" % (display_id, verdict[:120]), wp.get("subject", ""))
    except Exception as ex:  # noqa: BLE001
        set_status(root, display_id, state="error", verdict="xem lại lỗi: " + str(ex).splitlines()[0][:200])
        inbox(cfg, "**XEM LẠI LỖI** %s — %s" % (display_id, str(ex).splitlines()[0]))
    finally:
        fcntl.flock(global_lock, fcntl.LOCK_UN)


# =================================================================== phục hồi lượt bị ngắt

MAX_RECOVER = 3


def ticket_locked(root, display_id):
    """Khoá ticket (.SOAR-x.lock) đang bị một tiến trình giữ."""
    path = os.path.join(root, ".%s.lock" % display_id)
    if not os.path.exists(path):
        return False
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
    return False


def report_done(path):
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        return f.read(200).lstrip().startswith("KẾT QUẢ:")


def recover_plan(api, root, acfg, s):
    """
    Việc cần làm cho một lượt bị ngắt: (tham số op_autofix, mô tả) hoặc (None, kết luận ghi vào trạng thái).
    """
    display_id, state, event = s["display_id"], s.get("state"), s.get("event") or "MANUAL"
    run_dir = s.get("run_dir") or ""
    run_name = s.get("run_name") or os.path.basename(run_dir)
    fe_dir, be_dir = os.path.join(run_dir, "fe"), os.path.join(run_dir, "be")
    has_tree = bool(run_dir) and all(os.path.isdir(d) for _, d in R.trees(acfg, run_dir))

    if event == "FOLLOWUP":
        # Hàng chờ xem lại còn mục của ticket (đã đếm số lần thử) → process_pending tự chạy lại ở on_tick sau.
        if os.path.exists(os.path.join(pending_dir(root), display_id + ".json")):
            return None, "bị ngắt khi Claude xem lại — hàng chờ xem lại sẽ chạy lại"
        return None, "bị ngắt khi Claude xem lại (không còn trong hàng chờ) — xem tay %s" % run_dir
    if event == "REVIEW":
        return ["--review", display_id], "test lại từ đầu"
    if state == "shipping" or event == "SHIP":
        if not has_tree or not report_done(os.path.join(run_dir, "report.md")):
            return None, "bị ngắt khi push, không còn worktree / báo cáo — xem tay %s" % run_dir
        if any_fix(acfg, run_dir):
            return ["--ship", run_name], "giao lại %s (kiểm tra + push)" % run_name
        return None, ("bị ngắt khi push: không còn commit chưa push (có thể đã push xong) — kiểm tra comment / "
                      "trạng thái ticket tay · %s" % run_dir)

    wp_id = str(s.get("wp_id") or "")
    if not wp_id.isdigit():
        wp_id = str(api.get("/api/v3/work_packages/%s" % display_id)["id"])
    argv = ["--wp", wp_id, "--event", event]
    if event == "QA_FAIL" and s.get("qa_report") and os.path.exists(s["qa_report"]):
        argv += ["--qa-report", s["qa_report"]]
    if state in ("preparing", "running") and has_tree and os.path.exists(os.path.join(run_dir, "prompt.md")):
        return argv + ["--resume", run_name], "làm tiếp trong %s" % run_name
    return argv, "chạy lại từ đầu" if state != "queued" else "xếp hàng lại"


def recover(cfg, acfg, dry_run=False):
    """
    Tìm lượt có trạng thái đang chạy mà tiến trình đã chết (máy tắt / op-watch khởi động lại…) và chạy lại:
    queued → xếp hàng lại; preparing/running → làm tiếp trên worktree cũ; shipping → --ship; REVIEW → --review;
    FOLLOWUP → để hàng chờ xem lại xử lý. Mỗi ticket tự chạy lại tối đa MAX_RECOVER lần. op-watch gọi qua on_tick
    (lượt đầu chạy ngay khi khởi động).
    """
    root = os.path.expanduser(acfg["root"])
    os.makedirs(root, exist_ok=True)
    lock = open(os.path.join(root, ".recover.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return []
    stale = [s for s in load_statuses(root) if s.get("state") in ACTIVE_STATES and not status_alive(s)
             and not ticket_locked(root, s["display_id"])]
    if not stale:
        return []
    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    try:
        api.me()
    except (op_watch.urllib.error.URLError, OSError, ValueError) as ex:
        log("phục hồi: %d lượt bị ngắt, chưa gọi được OpenProject (%s) — để lượt sau" % (len(stale), ex))
        return []
    # Lượt đã làm dở trước (giữ thứ tự xếp hàng cũ) — queued sau cùng, theo thời điểm vào hàng.
    rank = {"shipping": 0, "running": 1, "preparing": 2, "queued": 3}
    stale.sort(key=lambda s: (rank.get(s.get("state"), 9), s.get("queued_at") or s.get("started_at") or ""))
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    for s in stale:
        display_id = s["display_id"]
        attempts = s.get("recover_attempts", 0)
        try:
            if attempts >= MAX_RECOVER:
                argv, what = None, "bị ngắt %d lần, không tự chạy lại nữa — chạy tay trên trang theo dõi" % attempts
            else:
                argv, what = recover_plan(api, root, acfg, s)
        except Exception as ex:  # noqa: BLE001 — một ticket lỗi không chặn các ticket khác
            log("phục hồi %s: lỗi %s — để lượt sau" % (display_id, ex))
            continue
        line = "%s (%s, %s): %s" % (display_id, s.get("state"), s.get("event"), what)
        out.append(line)
        if dry_run:
            print(line + ("" if argv is None else "  →  op_autofix.py " + " ".join(argv)))
            continue
        log("phục hồi " + line)
        if argv is None:
            set_status(root, display_id, state="error", verdict="bị ngắt: " + what)
            inbox(cfg, "**AUTOFIX BỊ NGẮT** %s — %s" % (display_id, what))
            continue
        with open(os.path.join(root, ".recover-%s.log" % display_id), "a", encoding="utf-8") as logf:
            logf.write("\n[%s] %s\n" % (datetime.now().isoformat(timespec="seconds"), " ".join(argv)))
            logf.flush()
            child = subprocess.Popen([sys.executable, os.path.join(here, "op_autofix.py")] + argv, cwd=here,
                                     stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     start_new_session=True)
        # Ghi PID con ngay: lượt on_tick sau thấy tiến trình còn sống, không mở trùng.
        set_status(root, display_id, pid=child.pid, recover_attempts=attempts + 1,
                   recovered_at=datetime.now().isoformat(timespec="seconds"))
        inbox(cfg, "**AUTOFIX BỊ NGẮT → CHẠY LẠI** %s — %s (lần %d/%d)" % (display_id, what, attempts + 1, MAX_RECOVER))
        # Cách nhau một chút để các lượt xếp hàng lại giữ đúng thứ tự cũ khi giành suất chạy.
        time.sleep(1)
    if out and not dry_run:
        notify("[AUTOFIX BỊ NGẮT] %d lượt" % len(out), "\n".join(out)[:300])
    return out


def main():
    parser = argparse.ArgumentParser(description="Tự sửa bug OpenProject tới bước commit bằng Claude Code.")
    parser.add_argument("--wp", help="id work package (mặc định lấy từ biến OP_WP_ID của op-watch)")
    parser.add_argument("--event", help="sự kiện (mặc định OP_EVENT); chỉ chạy với các sự kiện trong cấu hình")
    parser.add_argument("--dry-run", action="store_true", help="chuẩn bị ticket + worktree + prompt, không gọi Claude")
    parser.add_argument("--clean", metavar="RUN", help="xoá worktree + nhánh auto của một lượt chạy (tên thư mục)")
    parser.add_argument("--ship", metavar="RUN",
                        help="giao lại lượt đã sửa mà chưa push (tên thư mục, vd SOAR-2797-1103-1515, hoặc mã ticket = lượt mới nhất): kiểm tra lại, "
                             "Gemini xem lại, push, comment, Resolved; ĐÃ SỬA không commit mới → Gemini kiểm code hiện "
                             "tại rồi Resolved / UNREVIEWED; KHÔNG SỬA ĐƯỢC → comment + UNREVIEWED")
    parser.add_argument("--assist", metavar="RUN",
                        help="Claude hỗ trợ lượt CẦN QUYẾT ĐỊNH (tên thư mục hoặc mã ticket = lượt mới nhất): tải file "
                             "đính kèm, ticket liên quan, cho GET API server test, gỡ vướng rồi giao như thường")
    parser.add_argument("--note", default="", help="ghi chú / hướng dẫn của dev cho --assist")
    parser.add_argument("--status", action="store_true", help="xem các lượt đang chờ / đang chạy / vừa xong")
    parser.add_argument("--follow", nargs="?", const="", metavar="TICKET",
                        help="xem trực tiếp thao tác của lượt đang chạy (hoặc của TICKET)")
    parser.add_argument("--review", nargs="+", metavar="TICKET",
                        help="Gemini test lại các ticket đã sửa + push (SOAR-xxxx hoặc id), lần lượt từng cái")
    parser.add_argument("--qa-report", metavar="QA_MD",
                        help="báo cáo QA trên server KHÔNG ĐẠT (deploy_qa): Claude kiểm chứng rồi sửa tiếp / giải thích")
    parser.add_argument("--profile", metavar="GÓI", help="chạy bằng gói Claude này (mặc định: gói đang chọn)")
    parser.add_argument("--set-dev", nargs="*", metavar="TICKET",
                        help="điền mình vào trường Dev (dev_field) cho các ticket (mặc định: mọi ticket autofix đã chạy)")
    parser.add_argument("--probe", nargs="?", const="", metavar="GÓI",
                        help="hỏi Claude một câu ngắn để cập nhật hạn mức của gói (mặc định: mọi gói đã đăng nhập)")
    parser.add_argument("--followup-pending", nargs="?", const="", metavar="TICKET",
                        help="Claude xem lại các ticket trong hàng chờ nếu có gói còn hạn mức (op-watch gọi định kỳ)")
    parser.add_argument("--queue-followup", nargs="*", metavar="TICKET",
                        help="đưa các lượt đã bỏ bước Claude xem lại vào hàng chờ (không ghi ticket = quét tất cả)")
    parser.add_argument("--ignore-limits", action="store_true",
                        help="bỏ qua ngưỡng hạn mức tự đặt (pause_above_utilization / switch_above_7d / followup_skip_above_7d)")
    parser.add_argument("--recover", action="store_true",
                        help="chạy lại các lượt bị ngắt (trạng thái đang chạy mà tiến trình đã chết) — op-watch gọi "
                             "định kỳ qua on_tick; kèm --dry-run để chỉ liệt kê")
    parser.add_argument("--resume", metavar="RUN",
                        help="làm tiếp lượt bị ngắt trong thư mục cũ (vd SOAR-2570-1103-1650) thay vì tạo worktree mới")
    parser.add_argument("--config", help="đường dẫn config op-watch")
    args = parser.parse_args()

    cfg = op_watch.load_config(argparse.Namespace(config=args.config, interval=None, on_change=None, no_notify=False))
    acfg = dict(AUTOFIX_DEFAULTS)
    acfg.update(cfg.get("autofix") or {})
    root = os.path.expanduser(acfg["root"])
    if args.ignore_limits:
        acfg["_ignore_limits"] = True

    if args.recover:
        recover(cfg, acfg, args.dry_run)
        return
    if args.clean:
        clean(acfg, args.clean)
        return
    if args.ship:
        ship_run(cfg, acfg, args.ship, args.profile)
        return
    if args.assist:
        os.makedirs(root, exist_ok=True)
        assist_run(cfg, acfg, args.assist, args.note, args.profile)
        return
    if args.set_dev is not None:
        api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
        ids = args.set_dev or sorted({s["display_id"] for s in load_statuses(root) if s.get("display_id")})
        for t in ids:
            print("%s: %s" % (t, set_dev(api, acfg, t)))
        return
    if args.probe is not None:
        os.makedirs(root, exist_ok=True)
        for prof in profiles(acfg):
            if (args.probe and prof["name"] != args.probe) or not prof["logged_in"]:
                continue
            code = probe_profile(acfg, prof)
            print("%s: %s" % (prof["name"], usage_line(profile_usage(root, acfg, prof["name"])[0])
                              if code == 0 else "lỗi (mã %s)" % code))
        return
    if args.queue_followup is not None:
        os.makedirs(root, exist_ok=True)
        added = backfill_pending(root, acfg, args.queue_followup)
        print("Đã xếp hàng xem lại: %s" % (", ".join(added) or "không có"))
        return
    if args.followup_pending is not None:
        os.makedirs(root, exist_ok=True)
        if not load_pending(root):
            return
        process_pending(op_watch.OpenProject(cfg["url"], cfg["api_key"]), cfg, acfg, args.followup_pending or None)
        return
    if args.status:
        os.makedirs(root, exist_ok=True)
        show_status(root, acfg)
        return
    if args.review:
        os.makedirs(root, exist_ok=True)
        api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
        for ref in args.review:
            review_ticket(api, cfg, acfg, ref, args.profile)
        return
    if args.follow is not None:
        try:
            follow(root, args.follow)
        except KeyboardInterrupt:
            pass
        return

    wp_id = args.wp or os.environ.get("OP_WP_ID")
    event = args.event or os.environ.get("OP_EVENT") or "MANUAL"
    if not wp_id:
        sys.exit("Thiếu --wp")
    if not acfg["enabled"] or (event not in ("MANUAL", "QA_FAIL") and event not in acfg["events"]):
        return

    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
    wp = api.get("/api/v3/work_packages/%s" % wp_id)
    display_id = wp.get("displayId") or "WP-%s" % wp_id
    os.makedirs(root, exist_ok=True)
    num = re.search(r"(\d+)$", display_id)
    if event != "MANUAL" and acfg.get("min_ticket_number") and num and int(num.group(1)) < acfg["min_ticket_number"]:
        log("%s (%s): bỏ qua — chỉ tự sửa ticket từ SOAR-%d trở lên" % (display_id, event, acfg["min_ticket_number"]))
        return

    # Ticket này đang được xử lý (op-watch có thể báo lại khi có comment / reopen) thì bỏ qua.
    ticket_lock = open(os.path.join(root, ".%s.lock" % display_id), "w")
    try:
        fcntl.flock(ticket_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("%s đang được xử lý, bỏ qua" % display_id)
        return

    # Tối đa max_parallel lượt chạy cùng lúc: hết suất thì chờ ở đây.
    log("%s (%s): chờ tới lượt" % (display_id, event))
    set_status(root, display_id, state="queued", event=event, queued_at=datetime.now().isoformat(timespec="seconds"),
               started_at=None, run_dir=None, verdict=None, pid=os.getpid(), wp_id=str(wp["id"]),
               qa_report=os.path.abspath(args.qa_report) if args.qa_report else None)
    global_lock = acquire_slot(root, acfg, wait=False)
    if global_lock is None:
        busy = [st["display_id"] for st in load_statuses(root)
                if st.get("state") in ("preparing", "running", "shipping") and st.get("display_id") != display_id]
        msg = "chờ %s xong" % ", ".join(busy) if busy else "chờ lượt trước xong"
        inbox(cfg, "**AUTOFIX XẾP HÀNG** %s — %s" % (display_id, msg))
        notify("[AUTOFIX XẾP HÀNG] %s" % display_id, msg)
        global_lock = acquire_slot(root, acfg)

    # Ticket này được sửa lại từ đầu: nhận xét Gemini cũ của nó trong hàng chờ không còn giá trị.
    clear_followup(root, display_id)
    # Ưu tiên hàng chờ xem lại: có gói còn hạn mức thì xử lý các ticket đang chờ trước rồi mới sửa ticket mới.
    # Lượt chạy tay bỏ qua ngưỡng (--ignore-limits) thì chỉ làm đúng ticket được yêu cầu.
    if not args.dry_run and not acfg.get("_ignore_limits") and load_pending(root) \
            and choose_profile(root, followup_limits(acfg))[0]:
        log("%s: chạy hàng chờ xem lại trước" % display_id)
        fcntl.flock(global_lock, fcntl.LOCK_UN)
        process_pending(api, cfg, acfg)
        global_lock = acquire_slot(root, acfg)
    prof, switch_note = choose_profile(root, acfg, args.profile)
    if prof is None and not args.dry_run:
        msg = "hoãn: %s — chạy tay sau bằng --wp %s (hoặc chọn gói khác trên trang theo dõi)" % (switch_note, wp_id)
        log("%s: %s" % (display_id, msg))
        set_status(root, display_id, state="paused", verdict=msg)
        inbox(cfg, "**AUTOFIX HOÃN** %s — %s" % (display_id, msg))
        notify("[AUTOFIX HOÃN] %s" % display_id, msg)
        fcntl.flock(global_lock, fcntl.LOCK_UN)
        return

    if prof:
        use_profile(acfg, prof)
        if switch_note:
            inbox(cfg, "**AUTOFIX ĐỔI GÓI** %s — %s" % (display_id, switch_note))
            notify("[AUTOFIX ĐỔI GÓI] %s" % display_id, switch_note)
    # --resume: lượt bị ngắt giữa chừng — dùng lại thư mục, worktree và prompt cũ (không tạo nhánh mới).
    resume_dir = os.path.join(root, os.path.basename(args.resume)) if args.resume else None
    if resume_dir and not (os.path.basename(resume_dir).startswith(display_id + "-")
                           and os.path.isfile(os.path.join(resume_dir, "prompt.md"))
                           and all(os.path.isdir(d) for _, d in R.trees(acfg, resume_dir))):
        log("%s: không làm tiếp được %s (thiếu prompt / worktree) — sửa lại từ đầu" % (display_id, args.resume))
        resume_dir = None
    run_name = (os.path.basename(resume_dir) if resume_dir
                else "%s-%s" % (display_id, datetime.now().strftime("%m%d-%H%M")))
    run_dir = os.path.join(root, run_name)
    fe_dir, be_dir = os.path.join(run_dir, "fe"), os.path.join(run_dir, "be")
    branch = "auto/" + run_name
    report = os.path.join(run_dir, "report.md")
    started = time.time()
    set_status(root, display_id, state="preparing", run_dir=run_dir, run_name=run_name, wp_id=wp_id,
               profile=acfg.get("_profile"), started_at=datetime.now().isoformat(timespec="seconds"))
    try:
        if resume_dir:
            log("%s: làm tiếp lượt bị ngắt %s" % (display_id, run_dir))
            wp = fetch_ticket(api, wp_id, os.path.join(run_dir, "ticket"))
            with open(os.path.join(run_dir, "prompt.md"), encoding="utf-8") as f:
                prompt = f.read()
        else:
            log("%s: tải ticket, tạo worktree %s" % (display_id, run_dir))
            log("%s: trường %s: %s" % (display_id, acfg.get("dev_field") or "Dev", set_dev(api, acfg, wp_id)))
            wp = fetch_ticket(api, wp_id, os.path.join(run_dir, "ticket"))
            notes = make_trees(acfg, run_dir, branch)
            notes = [n for n in notes if n]
            if notes:
                inbox(cfg, "**AUTOFIX CẢNH BÁO** %s — %s" % (display_id, "; ".join(notes)))
            merge_note = "".join(
                "\n- LƯU Ý: %s. Có thể code liên quan đã đổi ở nhánh chung: nếu gặp, ghi rõ trong report.md." % n
                for n in notes)

            prompt = PROMPT.format(
                project_name=envconf.PROJECT_NAME, assignee=assignee_name(api),
                bash_rules=BASH_RULES.format(run_dir=run_dir), decider_rules=DECIDER_RULES,
                display_id=display_id, wp_id=wp_id, status=wp["_links"]["status"].get("title"), event=event,
                subject=wp.get("subject", ""), run_dir=run_dir, merge_note=merge_note, **R.prompt_kwargs(acfg, run_dir, branch))
            if args.qa_report and os.path.exists(args.qa_report):
                qa_copy = os.path.join(run_dir, "qa-server.md")
                shutil.copy(args.qa_report, qa_copy)
                extra = {}
                for key, name, dest in (("qa_rounds", "gemini-rounds.jsonl", "qa-rounds.jsonl"),
                                        ("deploy_check", "deploy-check.md", "qa-deploy-check.md")):
                    src = os.path.join(os.path.dirname(args.qa_report), name)
                    if os.path.exists(src):
                        shutil.copy(src, os.path.join(run_dir, dest))
                        extra[key] = os.path.join(run_dir, dest)
                    else:
                        extra[key] = "(không có)"
                with open(qa_copy, encoding="utf-8") as f:
                    m = re.search(r"QA:\s*([^\n_]+)", f.read(300))
                extra["qa_verdict"] = (m.group(1).strip() if m else "KHÔNG ĐẠT")
                extra["qa_rules"] = read_qa_rules()
                prompt += QA_FAIL_NOTE.format(qa_report=qa_copy, display_id=display_id, **extra)
            with open(os.path.join(run_dir, "prompt.md"), "w", encoding="utf-8") as f:
                f.write(prompt)
            if args.dry_run:
                log("dry-run: đã chuẩn bị %s (không gọi Claude)" % run_dir)
                set_status(root, display_id, state="done", verdict="dry-run")
                return

        if resume_dir and report_done(report):
            # Claude đã viết xong báo cáo trước khi bị ngắt: chỉ còn bước kiểm tra / push phía sau.
            log("%s: lượt bị ngắt đã có report.md — bỏ qua Claude, làm tiếp bước giao" % display_id)
            code = 0
            dev_t0 = None
        else:
            log("%s: chạy Claude (giới hạn %s USD)%s" % (display_id, acfg["max_budget_usd"],
                                                       " — làm tiếp lượt bị ngắt" if resume_dir else ""))
            inbox(cfg, "**AUTOFIX %s** %s — %s · xem: `op_autofix.py --follow %s`" % (
                "LÀM TIẾP" if resume_dir else "ĐANG CHẠY", display_id, wp.get("subject", ""), display_id))
            notify("[AUTOFIX %s] %s" % ("LÀM TIẾP" if resume_dir else "ĐANG CHẠY", display_id), wp.get("subject", ""))
            set_status(root, display_id, state="running")
            dev_t0 = time.time()
            if resume_dir:
                # Nhật ký phiên bị ngắt để riêng: hit_limit chỉ xét phiên mới (sự kiện hết hạn mức cũ không tính).
                for name in ("claude.jsonl", "claude.err"):
                    old = os.path.join(run_dir, name)
                    if os.path.exists(old):
                        with open(old, "rb") as src, open(old + ".interrupted", "ab") as dst:
                            shutil.copyfileobj(src, dst)
                        os.remove(old)
            code = run_claude(acfg, prompt + (INTERRUPTED_NOTE if resume_dir else ""), run_dir, fe_dir, be_dir)
        # Hết hạn mức giữa chừng: chạy tiếp trên cùng worktree bằng gói khác (mỗi gói tối đa một lần).
        tried = [acfg["_profile"]]
        while not os.path.exists(report) and hit_limit(os.path.join(run_dir, "claude.jsonl")):
            nxt, why = choose_profile(root, acfg, exclude=tried)
            if nxt is None:
                inbox(cfg, "**AUTOFIX HẾT HẠN MỨC** %s — %s dừng giữa chừng, không còn gói khác (%s)" % (
                    display_id, acfg["_profile"], why))
                break
            msg = "%s hết hạn mức giữa chừng → chạy tiếp bằng %s" % (acfg["_profile"], nxt["name"])
            log("%s: %s" % (display_id, msg))
            inbox(cfg, "**AUTOFIX ĐỔI GÓI** %s — %s" % (display_id, msg))
            notify("[AUTOFIX ĐỔI GÓI] %s" % display_id, msg)
            use_profile(acfg, nxt)
            tried.append(nxt["name"])
            set_status(root, display_id, profile=nxt["name"])
            code = run_claude(acfg, prompt + RESUME_NOTE, run_dir, fe_dir, be_dir, append=True)

        if dev_t0:
            log_time(api, acfg, wp_id, time.time() - dev_t0, "dev",
                     "[Autofix] Claude sửa (%s%s)" % (run_name, ", làm tiếp lượt bị ngắt" if resume_dir else ""))
        verdict = "KHÔNG CÓ BÁO CÁO (Claude thoát mã %s, xem claude.jsonl / claude.err)" % code
        report_text = ""
        if os.path.exists(report):
            with open(report, encoding="utf-8") as f:
                report_text = f.read()
            first = report_text.strip().splitlines()[0] if report_text.strip() else ""
            verdict = first.replace("KẾT QUẢ:", "").strip() or first
        if wants_assist(acfg, verdict, report_text) and not os.path.exists(os.path.join(run_dir, "assist", "done")):
            set_status(root, display_id, state="running", verdict="Claude hỗ trợ (agent cần hỗ trợ)")
            try:
                report_text, verdict = assist_step(api, cfg, acfg, wp, run_dir, report_text)
                verdict = verdict or "KHÔNG CÓ BÁO CÁO sau bước hỗ trợ"
            except Exception as ex:  # noqa: BLE001 — hỗ trợ lỗi thì giao kết quả cũ như thường
                log("%s: bước hỗ trợ lỗi: %s" % (display_id, ex))
                report_text, verdict = report_verdict(report)
        if verdict.startswith("CẦN QUYẾT ĐỊNH") and acfg.get("review_status_id"):
            try:
                mark_for_review(api, wp_id, acfg["review_status_id"], report_text, run_name, run_dir)
                verdict += " → đã chuyển UNREVIEWED"
            except (op_watch.urllib.error.URLError, OSError, ValueError, KeyError) as ex:
                verdict += " (chuyển UNREVIEWED lỗi: %s)" % ex
        commits = []
        for r, repo_dir in R.trees(acfg, run_dir):
            base, label = r["branch"], r["label"]
            out = run(["git", "log", "--oneline", "origin/%s..HEAD" % base, "--no-merges",
                       "--grep=" + display_id], cwd=repo_dir, check=False).strip()
            if out:
                commits.append("%s %s" % (label, out.splitlines()[0].split()[0]))
        has_fix = any_fix(acfg, run_dir)
        if verdict.startswith("ĐÃ SỬA") and has_fix and acfg.get("auto_ship"):
            set_status(root, display_id, state="shipping")
            log("%s: kiểm tra lại + push" % display_id)
            notify("[AUTOFIX ĐANG PUSH] %s" % display_id, "Đã sửa — đang build / test, Gemini test lại rồi push")
            try:
                shipped, ship_msg = deliver(api, cfg, acfg, wp_id, display_id, wp, run_dir, fe_dir, be_dir,
                                            report_text)
            except Exception as ex:  # noqa: BLE001 — ghi lại, vẫn báo kết quả sửa
                shipped, ship_msg = False, "ship lỗi: %s" % str(ex).splitlines()[0][:200]
            verdict += " → " + ship_msg
            if not shipped and "UNREVIEWED" not in ship_msg:
                verdict += " (sửa tay rồi push)"
        elif verdict.startswith("ĐÃ SỬA") and not has_fix and acfg.get("auto_ship"):
            # Lỗi đã được sửa sẵn (không commit mới): Gemini kiểm tra code hiện tại rồi mới cập nhật ticket.
            set_status(root, display_id, state="shipping")
            try:
                verdict += " → " + deliver_already_fixed(api, cfg, acfg, wp_id, display_id, wp, run_dir, fe_dir,
                                                         be_dir, report_text)
            except Exception as ex:  # noqa: BLE001
                verdict += " → cập nhật ticket lỗi: %s" % str(ex).splitlines()[0][:200]
        elif verdict.startswith("CẦN TEST TAY"):
            try:
                verdict += " → " + report_manual_test(api, wp_id, report_text, run_name, acfg.get("review_status_id"))
            except (op_watch.urllib.error.URLError, OSError, ValueError, KeyError) as ex:
                verdict += " (comment lên ticket lỗi: %s)" % ex
        elif verdict.startswith("QA SAI"):
            try:
                verdict += " → " + report_qa_wrong(api, wp_id, display_id, report_text, run_name)
            except (op_watch.urllib.error.URLError, OSError, ValueError, KeyError) as ex:
                verdict += " (comment lên ticket lỗi: %s)" % ex
        elif verdict.startswith("KHÔNG SỬA ĐƯỢC") and acfg.get("review_status_id"):
            try:
                report_cannot_fix(api, acfg, wp_id, report_text, run_name)
                verdict += " → đã comment, chuyển UNREVIEWED"
            except (op_watch.urllib.error.URLError, OSError, ValueError, KeyError) as ex:
                verdict += " (comment / chuyển UNREVIEWED lỗi: %s)" % ex
        minutes = int((time.time() - started) / 60)
        summary = "%s — %s%s (%d phút) · báo cáo: %s" % (
            display_id, verdict, (" · commit " + ", ".join(commits)) if commits else "", minutes, report)
        log(summary)
        result = [e for e in read_events(os.path.join(run_dir, "claude.jsonl")) if e.get("type") == "result"]
        set_status(root, display_id, state="done", verdict=verdict, commits=commits,
                   cost_usd=result[-1].get("total_cost_usd") if result else None)
        try:
            saved = write_summary_report(acfg, display_id, run_name, wp, verdict, commits, run_dir, report_text)
        except OSError as ex:
            saved = ""
            log("%s: không ghi được báo cáo tổng hợp: %s" % (display_id, ex))
        if saved:
            summary += " · tổng hợp: " + saved
        inbox(cfg, "**AUTOFIX** " + summary)
        notify("[AUTOFIX] %s: %s" % (display_id, verdict), wp.get("subject", ""))
    except Exception as ex:  # noqa: BLE001 — mọi lỗi đều phải báo ra inbox, không chết im lặng
        log("%s: lỗi %s" % (display_id, ex))
        set_status(root, display_id, state="error", verdict="lỗi: " + str(ex).splitlines()[0][:200])
        inbox(cfg, "**AUTOFIX LỖI** %s — %s (thư mục %s)" % (display_id, str(ex).splitlines()[0], run_dir))
        notify("[AUTOFIX LỖI] %s" % display_id, str(ex)[:200])
    finally:
        fcntl.flock(global_lock, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
