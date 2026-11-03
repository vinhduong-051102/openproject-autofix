# op-watch

Theo dõi OpenProject (dự án SOAR) và báo khi có việc cần sửa được giao cho mình:

| Sự kiện | Khi nào |
|---|---|
| `NEW` | Việc mới vào danh sách New/Reopened của mình (bug mới tạo, mới được giao) |
| `REOPENED` | Việc chuyển sang Reopened |
| `COMMENT` | Người khác comment trên việc đang ở New/Reopened (bỏ qua comment của chính mình) |

Mỗi sự kiện: 1 dòng log, thông báo desktop (`notify-send`), 1 dòng checklist trong `~/op-watch-inbox.md`,
và chạy lệnh `on_change` (nếu có).

## Cài đặt (người mới)

Cần: Linux có systemd user, Python 3.8+, git, Node (build FE), Maven (BE), `claude` (Claude Code CLI, đã đăng nhập),
`agy` (Antigravity CLI, cho Gemini review/QA — tuỳ chọn), `notify-send`, `sshpass` + docker (chỉ khi deploy server test).

```bash
git clone <repo> ~/tools/op-watch && cd ~/tools/op-watch
cp .env.example .env && chmod 600 .env     # điền: OP_URL, OP_PROJECT, FE_/BE_REPO + nhánh, GITEA_URL, DEPLOY_*…
```

Bí mật đặt ở **một trong hai** chỗ (`.env` hoặc file JSON quyền 600 trong `~/.config/op-watch/`):

| Bí mật | Biến `.env` | Hoặc file JSON (mẫu trong repo) |
|---|---|---|
| OpenProject API key | `OP_API_KEY` | `config.json` → `api_key` (`config.example.json`) |
| Token Gitea | `GITEA_TOKEN` | `gitea.json` (`gitea.example.json`) |
| Mật khẩu SSH / ứng dụng / OTP server test | `DEPLOY_SSH_PASSWORD`, `DEPLOY_APP_PASSWORD`, `DEPLOY_APP_OTP` | `deploy.json` (`deploy.example.json`) |
| Mật khẩu các tài khoản QA (`accounts`) | — | `deploy.json` → `accounts` |

Thứ tự ưu tiên: **biến môi trường > `.env` > JSON > mặc định**. Ngoại lệ: các giá trị chỉnh trên dashboard (số agent
song song, ngưỡng hạn mức, tự chuyển gói, gói Claude) được ghi vào JSON và JSON thắng `.env`.

Kiểm tra rồi cài service (op-watch, dashboard, merge 12h/18h, báo cáo ngày):

```bash
python3 op_watch.py --list                 # đọc được OpenProject?
python3 merge_to_dev.py --dry-run --no-deploy   # repo + nhánh + build đúng?
./install.sh                               # ghi unit vào ~/.config/systemd/user, enable + start
./install.sh op-watch op-dashboard         # chỉ cài lại vài unit (vd sau khi đổi lịch / thư mục)
xdg-open http://127.0.0.1:8765             # dashboard (DASHBOARD_PORT)
```

Unit mẫu ở `systemd/` (`@TOOL_DIR@`, lịch `MERGE_SCHEDULE` / `REPORT_SCHEDULE` lấy từ `.env`). Script tự nạp `.env`
cạnh nó nên service không cần `EnvironmentFile`. Sửa `.env` xong: `systemctl --user restart op-watch op-dashboard`
(timer thì lượt sau tự đọc lại; đổi lịch thì chạy lại `./install.sh`).

## Cấu hình op-watch

- `OP_WATCH_STATUSES`: id:tên trạng thái cần theo dõi (New = 1, Reopened = 100).
- `OP_INTERVAL`: số giây giữa hai lượt (mặc định 120).
- `OP_ON_CHANGE`: lệnh shell chạy mỗi sự kiện, nhận biến `OP_EVENT`, `OP_WP_ID`, `OP_DISPLAY_ID`,
  `OP_SUBJECT`, `OP_STATUS`, `OP_LINK`, `OP_DETAIL`.

Lần chạy đầu chỉ ghi mốc các việc đang có (không báo); dùng `--notify-existing` để báo cả các việc đó.
Trạng thái lưu ở `OP_WATCH_STATE` (mặc định `~/.local/state/op-watch/state.json`; `--reset` để xoá).

## Dùng

```bash
python3 op_watch.py --list          # in danh sách việc New/Reopened của mình
python3 op_watch.py --once          # chạy một lượt
python3 op_watch.py                 # chạy liên tục (Ctrl+C để dừng)
journalctl --user -u op-watch -f    # log khi chạy bằng service
```

## Tự sửa tới bước commit (op_autofix.py)

op-watch gọi `op_autofix.py` (cấu hình `on_change`) cho mỗi sự kiện. Với `NEW` / `REOPENED`:

1. Khoá: tối đa `max_parallel` phiên cùng lúc (chỉnh trên dashboard); ticket đang xử lý thì bỏ qua sự kiện mới của nó.
2. Tải mô tả + toàn bộ comment + ảnh vào `~/autofix/<SOAR-xxxx>-<MMDD-HHMM>/ticket/`.
3. Tạo worktree riêng `fe/` và `be/` trên nhánh `auto/<SOAR-xxxx>-<MMDD-HHMM>`, tách từ
   `FE_BRANCH` / `BE_BRANCH` và merge sẵn `origin/FE_UPSTREAM` / `origin/BE_UPSTREAM`.
4. Chạy `claude -p` (headless) với `--permission-mode dontAsk`:
   - được: đọc mọi file; sửa / ghi **chỉ trong thư mục lượt chạy**; git status/diff/log/add/commit;
     `npm run build`, `npx tsc`, `npx eslint`, `mvn`;
   - chặn: `git push/fetch/pull/checkout/remote/worktree`, `curl/wget/ssh`, `rm -rf`, `docker`, WebFetch/WebSearch;
   - giới hạn chi phí `max_budget_usd` (mặc định 5 USD / lượt).
5. Claude ghi `report.md` (dòng đầu `KẾT QUẢ: ĐÃ SỬA | CẦN QUYẾT ĐỊNH | KHÔNG SỬA ĐƯỢC`, nguyên nhân, commit,
   kiểm tra, đề xuất comment). Script thêm dòng `AUTOFIX` vào inbox + thông báo desktop.

**Không bao giờ** tự push hay chuyển Resolved — bạn xem `report.md` + commit, rồi bảo Claude (phiên tương tác) đưa
commit lên nhánh của mình, push và chuyển Resolved.

Ngoại lệ duy nhất có ghi OpenProject: kết quả **CẦN QUYẾT ĐỊNH** → script comment `[Cần duyệt]` kèm lý do (mục
"Cần quyết định" trong `report.md`) và chuyển ticket sang **UNREVIEWED** (`review_status_id`, 94; đặt `null` để tắt).
Ticket UNREVIEWED ra khỏi danh sách New/Reopened nên không bị chạy lại.

Quyền git trong phiên nền: dùng `git -C <worktree> log/show/diff/status/blame/grep/add/commit` (chỉ 2 worktree của lượt
chạy); `cd … && …` bị chặn nên Maven chạy bằng `mvn -f <be>/SOAR/pom.xml …`.

```bash
python3 op_autofix.py --wp 72107               # chạy tay cho một work package
python3 op_autofix.py --wp 72107 --dry-run     # chỉ chuẩn bị ticket + worktree + prompt
python3 op_autofix.py --clean SOAR-2789-1030-1205   # xoá worktree + nhánh auto của một lượt
```

Tắt tự sửa: đặt `"autofix": {"enabled": false}` (op-watch vẫn báo như cũ). Đổi model / ngân sách trong
`"autofix"` của `~/.config/op-watch/config.json`. Log của Claude: `claude.json`, `claude.err` trong thư mục lượt chạy.

### Xem tiến trình

```bash
python3 op_autofix.py --status            # hạn mức gói + lượt đang chờ/chạy/vừa xong + 8 thao tác gần nhất + số file đã sửa
python3 op_autofix.py --follow            # xem trực tiếp từng thao tác của lượt đang chạy (Ctrl+C thoát, phiên vẫn chạy)
python3 op_autofix.py --follow SOAR-2789  # theo dõi / xem lại lượt của một ticket
```

Trạng thái mỗi ticket: `~/autofix/.status/<SOAR-xxxx>.json` (`queued` → `preparing` → `running` → `done` | `error` | `paused`).
Luồng thao tác đầy đủ của Claude: `<thư mục lượt>/claude.jsonl`.

### Hạn mức gói

Claude Code đăng nhập bằng tài khoản claude.ai → mỗi lượt autofix trừ vào hạn mức gói (dùng chung với Claude Code / claude.ai),
không tính tiền theo token (tổ chức đã tắt tính phí vượt hạn mức). `--status` hiện mức đã dùng của hạn mức 5 giờ / 7 ngày.
Hạn mức 5 giờ đã dùng ≥ `pause_above_utilization` (mặc định 80%) thì lượt mới bị **hoãn** (ghi inbox `AUTOFIX HOÃN`),
chạy tay sau bằng `--wp <id>`.

### Duyệt ticket UNREVIEWED (chọn phương án)

- **Trên OpenProject:** comment lựa chọn (vd `Chọn phương án 2: …`) → đổi trạng thái về **New** (hoặc Reopened),
  assignee là mình. Trong ~2 phút op-watch thấy ticket quay lại danh sách → autofix chạy lại, đọc comment quyết định
  (sau comment `[Cần duyệt]`) và sửa + commit theo đúng phương án đó.
- **Hoặc trong Claude Code:** nói thẳng "SOAR-xxxx chọn phương án …".

## merge_to_dev — theo lịch (mặc định 12h và 18h) đưa nhánh cá nhân vào nhánh dev chung (PR Gitea)

`merge_to_dev.py`: FE `FE_BRANCH → FE_UPSTREAM`, BE `BE_BRANCH → BE_UPSTREAM` (cấu hình trong `.env`). Không có commit
mới thì bỏ qua; merge nhánh chung vào nhánh mình trước (xung đột ở `db/master*.xml` thì giữ cả hai, xung đột khác giao
Claude gỡ), build FE / compile BE, rồi tạo PR (hoặc dùng PR đang mở) và merge qua Gitea API. Kết quả: thông báo + dòng
`MERGE DEV` trong inbox, log build ở `$AUTOFIX_ROOT/merge-dev-*.log`.

Cài đặt: token Gitea (Settings → Applications → Generate token, quyền repository read & write) vào `GITEA_TOKEN` trong
`.env` hoặc `~/.config/op-watch/gitea.json`; lịch ở `MERGE_SCHEDULE`; rồi `./install.sh op-merge`.

Chạy thử: `python3 merge_to_dev.py --dry-run` (merge + build, không push / không tạo PR); chạy ngay:
`systemctl --user start op-merge.service`; xem log: `journalctl --user -u op-merge -n 50`.

## Nhiều gói Claude + trang theo dõi (op_dashboard.py)

Mỗi gói Claude là một thư mục cấu hình đã đăng nhập riêng (`CLAUDE_CONFIG_DIR`). Khai báo trong config op-watch:

```json
"autofix": { "profiles": [ {"name": "Gói chính", "config_dir": "~/.claude"},
                           {"name": "Gói phụ",  "config_dir": "~/.claude-b"} ] }
```

Thêm gói (trên trang theo dõi, mục "+ Thêm gói"):
- **Token (khuyên dùng):** chạy `claude setup-token`, đăng nhập tài khoản của gói, dán token vào trang. Token lưu ở
  `~/.config/op-watch/tokens/<tên>` (quyền 600); config chỉ ghi `{"name", "token_file"}`. Lượt chạy bằng gói này đặt
  `CLAUDE_CODE_OAUTH_TOKEN`, vẫn dùng thư mục cấu hình mặc định (~/.claude). Token hết hạn → nút "Đổi token".
- **Thư mục đăng nhập:** `{"name", "config_dir"}`, rồi `CLAUDE_CONFIG_DIR=~/.claude-b claude` và gõ `/login`.

- Gói đang dùng lưu ở `~/autofix/.profile.json` (chọn trên trang, hoặc `--profile <tên>` cho một lượt).
- Tự chuyển gói (`auto_switch`, bật mặc định): trước mỗi lượt, gói đang dùng có 5 giờ ≥ `pause_above_utilization`
  hoặc 7 ngày ≥ `switch_above_7d` (hoặc đã bị chặn) → dùng gói khác còn hạn mức; hết gói thì hoãn như cũ.
  Hết hạn mức giữa chừng → chạy tiếp trên cùng worktree bằng gói khác. Bước Claude xem lại sau Gemini cũng đổi gói.
- Hạn mức từng gói lấy từ `rate_limit_event` trong các lượt chạy bằng gói đó; `--probe [tên]` hỏi một câu ngắn
  để cập nhật ngay (nút "Cập nhật hạn mức" trên trang).

```bash
python3 op_dashboard.py --open      # http://127.0.0.1:8765 — chỉ nghe trên máy
```

Trang hiển thị: hạn mức 5 giờ / 7 ngày của từng gói + ngưỡng, chọn gói, bật/tắt tự chuyển; danh sách lượt chạy
(đang chạy trước), thao tác Claude trực tiếp, số file đã sửa, các file báo cáo / Gemini / build-test; nút chạy lại
autofix hoặc test lại bằng gói tùy chọn; inbox.

## Hàng chờ Claude xem lại (khi hết hạn mức)

Gemini kết luận CẦN XEM / KHÔNG ĐẠT mà không gói nào còn hạn mức → bước Claude xem lại bị bỏ:
- ticket ghi vào `~/autofix/.pending-followup/<SOAR-x>.json` và chuyển **TO DO** (`followup_status_id`, 97);
- op-watch chạy `on_tick` (`op_autofix.py --followup-pending`) mỗi `tick_interval` giây (600). Khi có gói còn hạn mức
  (vd sau giờ làm mới), Claude xem lại từng ticket, dùng lại `gemini.md` cũ (không chạy Gemini lại):
  bản đã push → worktree mới, push phần sửa thêm + comment; bản bị chặn push → làm tiếp trong worktree cũ.
  Xem lại xong → **Resolved** (CẦN QUYẾT ĐỊNH → UNREVIEWED). Thử tối đa 3 lần.
- Ưu tiên: lượt sửa ticket mới mà thấy hàng chờ + có hạn mức thì chạy hàng chờ trước.
- Ticket được sửa lại từ đầu (NEW / REOPENED / chạy lại) → tự bỏ khỏi hàng chờ.
- Ngưỡng (5 giờ / 7 ngày / xem lại 7 ngày) chỉnh trên trang theo dõi, mục "Ngưỡng hạn mức" → ghi vào config autofix
  (`pause_above_utilization`, `switch_above_7d`, `followup_skip_above_7d`), lượt sau dùng ngay.
- Ngưỡng riêng từng gói: nút "Chỉnh ngưỡng" trên thẻ gói → `profiles[].limits` = {"five_hour", "seven_day",
  "followup_7d"} (0..1); ô để trống theo ngưỡng chung.
- Chạy tay bỏ qua ngưỡng tự đặt: `--ignore-limits` (trang theo dõi: ô "Bỏ qua ngưỡng hạn mức" khi chạy lại / test lại,
  nút "Chạy ngay (bỏ qua ngưỡng)" ở hàng chờ). Gói đã bị Anthropic chặn thật (rejected) thì vẫn không chạy.
- `--queue-followup [SOAR-x …]`: đưa các lượt cũ đã bỏ bước xem lại vào hàng chờ. Trang theo dõi có mục
  "Chờ Claude xem lại" với nút Chạy ngay / Bỏ khỏi hàng chờ.

## Báo cáo ngày (daily_report.py)

Theo lịch `REPORT_SCHEDULE` (mặc định 08:00 thứ 2 – thứ 6) tạo `$DAILY_REPORT_DIR/bao-cao-DD-MM-YYYY.txt`:
"Hôm qua" = mục "Hôm nay" của báo cáo trước; "Hôm nay" = tối đa `DAILY_REPORT_MAX_ITEMS` việc đang giao cho mình.
`python3 daily_report.py --force` tạo lại báo cáo hôm nay. Cài: `./install.sh op-report`.

### Dọn dữ liệu bug đã chuyển QA (`cleanup.py`)
op-watch gọi `cleanup.py --every 3600` trong `on_tick` (tối đa 1 lần/giờ). Ticket đang giao cho người QA (`qa_user_id`)
và không có lượt nào đang chạy thì bị xoá: thư mục lượt chạy + git worktree + nhánh local `auto/<lượt>`, báo cáo trong
`report_dir`, file trạng thái / khoá, mục trong `qa-history.json` / `qa-fail-count.json` / `qa-pending.json`, các dòng
inbox chỉ nói về ticket đó. Lượt nào còn commit (không phải merge) chưa có trên remote thì GIỮ LẠI.
`cleanup.py --dry-run` để xem trước; `cleanup.py SOAR-123` để dọn riêng một ticket.
