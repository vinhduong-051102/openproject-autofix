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

## Nhiều dự án (projects.py — tab Dự án)

Tool làm được cho nhiều dự án OpenProject cùng lúc; mỗi dự án có repo, server test, quy ước riêng. Chưa khai báo thì
dùng MỘT dự án dựng từ cấu hình cũ (`OP_PROJECT`, `OP_TICKET_PREFIX`, `OP_PROJECT_NAME`, danh sách repo, `deploy.json`)
— chạy như trước.

Thêm dự án: trang theo dõi → **tab Dự án** → chọn dự án OpenProject → **+ Thêm dự án** (tự đọc tên + tiền tố mã
ticket) → điền bối cảnh, server test → **Lưu dự án** → bấm **Repo ↓** để thêm repo cho dự án (chọn thư mục, AI điền)
→ **⟳ Áp dụng**. Lần lưu đầu, repo hiện có chuyển vào dự án đầu tiên (`config.json` → `projects`, sao lưu `.bak-*`).

| Phần | Theo dự án như thế nào |
| --- | --- |
| op-watch | theo dõi ticket giao cho mình ở mọi dự án (trạng thái chung hoặc `statuses` riêng của dự án) |
| autofix | nhận dự án theo **tiền tố mã ticket** (SOAR-123 → dự án SOAR): chỉ dùng repo, bối cảnh, mẫu tiêu đề, ngưỡng `min_ticket_number` của dự án đó; dự án tắt "tự sửa" thì chỉ chạy khi bấm tay |
| merge | đưa nhánh cá nhân của repo **mọi dự án** vào nhánh chung (`merge_to_dev.py --project X`: một dự án) |
| deploy + QA | lần lượt từng dự án có repo deploy / bật QA; server lấy từ phần **Server test** của dự án (dự án dựng từ cấu hình cũ dùng `deploy.json`). QA tự động dùng `soar_api` / `ui_check` (đăng nhập kiểu SOAR) — dự án khác để tắt QA, vẫn có autofix + Gemini review + merge + deploy. `deploy_qa.py --project X` |
| soar_api / ui_check | đọc server của dự án qua biến `OP_PROJECT_KEY` (autofix / deploy_qa tự đặt cho tiến trình con) |
| Tạo task / Tách việc | Tạo task chọn dự án; tách việc tạo việc con vào dự án của ticket cha |
| Telegram | mỗi nguồn chat chọn dự án nhận việc (Cài đặt Telegram → cột Cách nhận) |
| Tab OpenProject | chọn dự án / Tất cả dự án |

Ràng buộc: mã dự án, tiền tố mã ticket, dự án OP và **mã repo (key)** không trùng giữa các dự án.

**AI đọc source điền cấu hình**: nút trên thẻ dự án — Claude (chỉ đọc) đọc các repo của dự án, điền bối cảnh dự án
(nghiệp vụ, màn hình chính ↔ thư mục code, thuật ngữ, quy ước), cách đăng nhập app và đường dẫn kiểm tra server; xem
lại rồi Lưu dự án.

**Đăng nhập app theo dự án** (mục *Đăng nhập app*, dùng cho QA tự động, Claude hỗ trợ gọi API, `ui_check`):
`soar` (login + OTP như SOAR) · `json` (POST đường dẫn + mẫu body `{user}` `{password}` `{otp}`, bước OTP tuỳ chọn,
token theo `token_path` hoặc tự tìm, không có token thì dùng cookie phiên; gửi JSON hoặc form) · `static` (token / API key
cố định, header + tiền tố tuỳ chỉnh, `-` = không tiền tố) · `none`. Trang đăng nhập + selector ô tài khoản / mật khẩu /
nút cho `ui_check`. Nút **Thử đăng nhập** kiểm tra bằng cấu hình đã lưu (gọi thêm *API thử token* nếu có). Server không
phải kiểu SOAR: kiểm tra sống qua *Đường dẫn kiểm tra sống* (mặc định gốc API), dấu bản đang chạy = băm trang FE.

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
6. Ticket dạng **câu hỏi** ("kiểm tra / làm rõ chức năng X làm gì", không nêu lỗi kèm kết quả mong đợi cụ thể) có
   kết quả `KẾT QUẢ: GIẢI ĐÁP`:
   - Claude đọc code để trả lời, không sửa và không commit.
   - Script comment **[Autofix] Giải đáp**: hành vi thực tế từng trường hợp, kèm **Đề xuất** nếu thấy nên đổi. Sau đó
     chuyển ticket sang `answer_status_id` (mặc định 96 = FEEDBACK, không nằm trong trạng thái theo dõi nên không chạy
     lại). Đặt `null` thì chỉ comment.
   - Không deploy, không chạy Gemini. Lỡ có commit thì để lại trong worktree, không push.
   - Muốn làm theo đề xuất: comment chọn phương án rồi chuyển **Reopened**, autofix sẽ sửa đúng phương án đó.

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

### Huỷ lượt đang chạy
Trên trang theo dõi có 2 chỗ huỷ lượt autofix đang chạy (cả lượt test lại / Claude hỗ trợ):
- nút **■ Huỷ chạy** trong chi tiết lượt;
- nút ■ nhỏ ở dòng hoạt động.

Khi huỷ:
- Tool dừng cả cây tiến trình (Claude, Gemini, build…): gửi SIGTERM, sau 8 giây còn sống thì SIGKILL.
- Lượt chuyển trạng thái **Đã huỷ** và không bị `--recover` chạy lại.
- Worktree và commit giữ nguyên, không đổi gì trên OpenProject. Muốn làm lại thì bấm "Chạy lại autofix".
- Huỷ khi đang kiểm tra / push thì có thể đã push một phần, nên trang sẽ cảnh báo trước.
- Chi tiết lượt đã huỷ hoặc bị ngắt có ô **Phần sửa dở**: file chưa commit, file mới, commit chưa push, báo cáo
  đã có chưa, kèm gợi ý nên làm tiếp hay chạy lại.
- **▶ Làm tiếp**: chạy `--resume` trên đúng worktree cũ, Claude xem phần đã sửa rồi làm tiếp. Đã có báo cáo thì đi
  tiếp bước kiểm tra / giao. **Chạy lại autofix**: lượt mới từ code mới nhất, bỏ phần dở.
- Dọn dẹp (`cleanup.py`) không xoá lượt:
  - còn commit chưa push;
  - còn thay đổi chưa commit (kể cả file mới);
  - vừa huỷ trong `CLEANUP_KEEP_CANCELLED_DAYS` ngày (mặc định 3, đánh dấu bằng file `.cancelled` trong thư mục lượt).

### Gỡ dự án (tab Dự án → "Gỡ dự án…", hoặc `python3 project_remove.py <mã> [--yes]`)
Trang hiện danh sách sẽ xoá trước, phải gõ đúng mã dự án mới xoá được. Những thứ bị xoá:
- cấu hình dự án (config.json được sao lưu trước khi ghi);
- lượt autofix / QA của ticket `<PREFIX>-n`: thư mục, worktree, nhánh `auto/…`;
- trạng thái, hàng chờ xem lại, khoá / log, log deploy;
- thư mục build deploy và mốc deploy của repo dự án (repo dùng chung với dự án khác thì giữ);
- lịch sử / hàng chờ QA, báo cáo tổng hợp;
- token / phiên đăng nhập app của dự án (trừ dự án kiểu SOAR, vì dùng chung file);
- chat Telegram gán cho dự án thì chuyển về dự án mặc định.

Lượt còn commit chưa push được giữ lại, trừ khi tick xoá (`--drop-unpushed`). Không gỡ được khi dự án còn lượt đang
chạy, và không gỡ được dự án cuối cùng. Giữ nguyên: thư mục repo, nhánh trên remote, OpenProject, dòng inbox cũ.

### Thống kê (tab "Thống kê", `stats.py`)
Số liệu lấy từ lịch sử đổi trạng thái trên OpenProject, gồm các ticket có cập nhật trong 7 / 30 / 90 ngày. Lọc được
"Của tôi" hoặc "Cả dự án", và theo dự án.
- **Hoàn thành**: chuyển sang Resolved / Done / Closed. Lần hoàn thành có comment `[Autofix]` trong ±10 phút được
  tính là autofix.
- **Bị reopen**: chuyển sang Reopened.
- **Tỉ lệ reopen**: số ticket hoàn thành trong khoảng rồi bị reopen, chia cho số ticket hoàn thành.
- **Trả lại nội bộ**: từ hoàn thành về UNREVIEWED / TO DO / Review…, thường do QA trên server trượt hoặc chờ Claude
  xem lại. Tách riêng, không tính là reopen.

Trang gồm biểu đồ theo ngày (có dạng bảng), danh sách ticket bị reopen (nhiều lần nhất trước), danh sách hoàn thành,
danh sách trả lại nội bộ, và số liệu theo người / dự án. Bấm mã ticket để xem chi tiết.

Activities được cache ở `~/.cache/op-watch/stats-activities.json` theo `updatedAt`, nên chỉ tải lại ticket có thay
đổi. Kết quả cache 10 phút; nút ↻ để tải lại. Dòng lệnh: `python3 stats.py --days 30 --scope mine|all [--project <mã>]`.

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

## Deploy không dừng trang (deploy_qa.py — DEPLOY_STRATEGY)

Sau khi build + `docker load` image mới và ghi vào `docker-compose.yml`, `deploy_qa.py` đưa bản mới vào chạy theo
`DEPLOY_STRATEGY` (mặc định `bluegreen`). Mỗi phần FE / BE:

1. Kiểm tra trước (chỉ đọc): nginx `soar-https-nginx` chạy, upstream (`soar_frontend_4443` / `soar_backend_4443`) chỉ khai
   báo trong file riêng `nginx-config/op-watch-upstream-{fe,be}.conf`, container chính chạy. Chưa đạt → phần đó dùng `service`.
2. Dựng container tạm `soar-fe-next` / `soar-next` bằng `docker compose run -d --no-deps` (cùng `.env`, volume, mạng
   compose; không mở cổng host).
3. Kiểm tra bản tạm qua mạng nội bộ (curl từ container nginx): FE trả `index.html` có đúng `<meta name="version">`; BE
   `/actuator/health` = 200 và `/api/v1/ping` < 500. BE tạm được gắn thêm alias mạng `soar` (playbook gọi `http://soar:8888`).
4. Ghi upstream → bản tạm, `nginx -t` rồi `nginx -s reload` (graceful), kiểm tra URL công khai.
5. Tạo lại container chính bằng image mới (`docker compose up -d --no-deps --force-recreate <service>`) trong lúc trang
   chạy trên bản tạm; kiểm tra bản chính.
6. Trỏ upstream về bản chính (tên service compose), chờ `DEPLOY_BG_DRAIN_SECONDS`, xoá bản tạm, reload nginx.

Lỗi ở bước 2–4: nginx về bản chính, xoá bản tạm, trả image cũ vào `docker-compose.yml` → trang giữ bản cũ, báo
`DEPLOY 86 LỖI`. Lỗi ở bước 5: trang tiếp tục chạy bằng container tạm (bản mới, đặt `restart unless-stopped`), báo lỗi kèm
lệnh cần làm tay. Tiến trình hiện trên trang theo dõi như log compose trước đây (`.compose-live.log`).

`service`: chỉ tạo lại FE/BE đổi image với `--no-deps` rồi reload nginx (trang dừng ngắn, không đụng playbook / nginx).
`recreate`: cách cũ `docker compose up -d --force-recreate` cả stack.

Xem trước các lệnh (không ssh, không đổi gì): `python3 deploy_qa.py --rollout-plan [fe] [be] [--strategy service]
[--destructive]`.

**BE và DB**: trong lúc chuyển, 2 bản BE chạy cùng Postgres / Redis vài phút. Liquibase của bản mới migrate khi bản cũ vẫn
phục vụ (Liquibase có khoá `DATABASECHANGELOGLOCK`, chỉ một bản migrate). Thêm bảng / cột là an toàn; migration phá cấu trúc
(drop / rename / đổi kiểu cột, NOT NULL…) thì bản cũ có thể lỗi SQL → mặc định (`DEPLOY_BG_BE_DESTRUCTIVE=fallback`) BE
dùng `service` khi diff `db/` có các thay đổi đó. Quartz chạy clustered (JDBC) nên job Quartz không chạy trùng; các
`@Scheduled` (flush thống kê, reaper, cron hằng ngày…) có thể chạy ở cả 2 bản trong vài phút chồng nhau.

Cài một lần trên server (tách upstream nginx ra file riêng để deploy được phép đổi):

```bash
cd ~/soar_v2.0
cp nginx-config/soar-all-4443.conf nginx-config/soar-all-4443.conf.bak-$(date +%Y%m%d-%H%M)-truoc-bluegreen
printf 'upstream soar_frontend_4443 {\n    server soar-fe-nginx:3000;\n    keepalive 64;\n}\n' > nginx-config/op-watch-upstream-fe.conf
printf 'upstream soar_backend_4443 {\n    server service-product-content:8888;\n    keepalive 64;\n}\n' > nginx-config/op-watch-upstream-be.conf
sed -i '/^upstream soar_frontend_4443 {/,/^}/d; /^upstream soar_backend_4443 {/,/^}/d' nginx-config/soar-all-4443.conf
docker exec soar-https-nginx nginx -t && docker exec soar-https-nginx nginx -s reload
```

`nginx -t` lỗi thì trả lại: `cp nginx-config/soar-all-4443.conf.bak-<giờ>-truoc-bluegreen nginx-config/soar-all-4443.conf
&& rm nginx-config/op-watch-upstream-*.conf`. Tắt bluegreen: `DEPLOY_STRATEGY=service` (hoặc `recreate`) trong `.env`.

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
- **Lấy từ KeyVault** (`keyvault.py`, theo [HUONG_DAN_LAY_KEY.md](https://github.com/vinhduong-051102/key_store/blob/main/HUONG_DAN_LAY_KEY.md)):
  token Claude cất trong KeyVault, mỗi lần lấy cần API key + mã OTP Google Authenticator, giải mã tại máy
  (X25519 + HKDF-SHA256 + AES-256-GCM). Đăng ký một lần ở ô **KeyVault** (dưới danh sách gói):
  - Cách A: gửi yêu cầu (cặp khoá sinh trên máy này), chờ quản trị viên duyệt, rồi "Nhận thông tin". Thêm secret
    vào Google Authenticator, nhập OTP để kích hoạt.
  - Cách B: quản trị viên đăng ký giúp. Gửi họ public key ("Hiện public key"), nhận lại API key rồi dán vào.

  Sau đó dùng nút **Lấy từ KeyVault** ở gói, hoặc "+ Thêm gói → Lấy từ KeyVault": chọn key, nhập OTP. Gói nhớ key
  đã chọn (`kv_key`), nên lần sau chỉ cần OTP.

  Lưu ở `~/.config/op-watch/`, quyền 600: `keyvault.json` (API key, request) và `keyvault-recipient.key`
  (private key). Máy chủ mặc định là `http://100.112.133.34:8000`; đổi bằng `KV_SERVER` hoặc `"keyvault": {"server": …}`
  trong config. http chỉ được phép với Tailscale, mạng nội bộ hoặc localhost.

  **Chuyển hết sang KeyVault** (KeyVault → Quản lý):
  1. Mỗi key chọn gói nhận, rồi bấm "Lấy & dùng". Token được thử gọi Claude trước, key hỏng thì gói giữ nguyên.
     Gói thư mục đăng nhập sẽ chuyển thành gói token; thư mục để nguyên.
  2. Bấm "Bỏ N gói không từ KeyVault". Token cũ chuyển vào `tokens/.backup-<giờ>/`.

  Kiểm tra một key mà không lưu vào gói: nút "Kiểm tra", hoặc `python3 keyvault.py test --key-id <id> --otp <mã>`.

  Dòng lệnh: `python3 keyvault.py request|status|claim|activate|import|pubkey|keys|test|reset`, và
  `python3 keyvault.py fetch --key-id <id> --profile "Gói của Dũng"` (hỏi OTP, ghi token cho gói).

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

### Thứ tự dùng gói Claude
Mặc định (chưa sắp thứ tự) mỗi lượt chạy chọn gói **còn nhiều hạn mức nhất** (còn lại = 100% − mức đã dùng cao hơn
trong 5 giờ / 7 ngày; gói chưa có dữ liệu coi như còn đủ). Trên trang theo dõi chọn "Theo thứ tự ưu tiên" rồi sắp bằng
▲ ▼ / "Ưu tiên số 1" (lưu `priority` trong `~/autofix/.profile.json`); gói đầu chạm ngưỡng thì dùng gói kế tiếp (khi bật
Tự chuyển gói). "Đổi tên" đổi tên gói trong config, kèm hạn mức đã lưu, trạng thái lượt chạy và thứ tự ưu tiên.

### Đổi lịch / hoãn merge + deploy tự động
Trên trang theo dõi bấm chip "Merge + deploy + QA":
- **Lịch chạy tự động**: sửa / thêm giờ (HH:MM), chọn Hằng ngày hoặc Thứ 2 – thứ 6, "Lưu lịch" → ghi drop-in
  `~/.config/systemd/user/op-merge.timer.d/schedule.conf` (đè `MERGE_SCHEDULE` của install.sh; xoá file này để quay về
  lịch trong `.env`). Khi lưu, mốc "lần chạy trước" của timer được đặt = bây giờ để `Persistent=true` không chạy bù ngay
  các giờ vừa thêm mà đã qua trong hôm nay.
- **Hoãn**: "Hoãn hôm nay" hoặc hoãn tới hết một ngày → `~/autofix/.merge-skip.json`; lượt chạy theo lịch thấy đang hoãn
  thì ghi inbox `MERGE DEV HOÃN` rồi thoát. "Chạy ngay" trên trang và chạy tay `merge_to_dev.py` vẫn chạy.

### Tạo task từ trang theo dõi
Nút "+ Tạo task": nhập mô tả (lỗi gì, màn nào, tài khoản…) + kéo thả / dán ảnh, file liên quan (tối đa 10 file, 20 MB)
→ "AI soạn mô tả": Claude headless (gói theo thứ tự dùng gói; chỉ đọc file đính kèm + mã FE/BE, chỉ ghi draft.json)
soạn tiêu đề + mô tả theo mẫu ticket gần nhất của người QA (ĐIỀU KIỆN BAN ĐẦU / CÁC BƯỚC / KẾT QUẢ MONG ĐỢI / THỰC TẾ /
MINH CHỨNG), kèm câu hỏi gợi ý bổ sung → xem / sửa tiêu đề, mô tả, loại, ưu tiên, người nhận → "Tạo task trên
OpenProject (New)": tạo work package, upload file, ảnh trong mô tả trỏ tới file đã upload. Giao cho mình thì op-watch
coi là task New và autofix tự sửa. Bản nháp ở `~/autofix/task-drafts/` (cleanup.py xoá sau 14 ngày).

## Yêu cầu qua Telegram (tg_watch.py + tg_requests.py)

**Nguồn chat** (⚙ Cài đặt → Nguồn chat, chọn từ danh sách chat gần đây hoặc nhập @username / t.me/… / ID số): người
hoặc nhóm, mỗi nguồn có
- cách nhận yêu cầu: *Mọi tin* | *Khi nhắc tới tôi / trả lời tin của tôi* (mặc định cho nhóm) | *Chỉ tin có hashtag*;
- cờ **Nhận yêu cầu** (gom tin thành yêu cầu), **Xin ý kiến** (nhận câu hỏi / nhờ test tay), **Trả lời** (tin báo khi
  tạo task / gắn việc / từ chối / xong).
Trong nhóm tin được gom theo từng người gửi; AI lọc biết đang đọc tin nhóm. Thêm / bỏ nguồn thì op-telegram tự khởi
động lại, đổi cách nhận / cờ áp dụng ngay. Cấu hình cũ `TG_TESTERS` / `testers` tự chuyển thành nguồn chat riêng.

Tester nhắn yêu cầu trong **chat riêng hiện tại** với mình trên Telegram → trang theo dõi có mục
"Yêu cầu tester (Telegram)" → mình duyệt thì mới thành task OpenProject.

- `tg_watch.py` (service `op-telegram`): userbot Telethon đăng nhập bằng **tài khoản Telegram của mình**, chỉ nghe chat
  với `TG_TESTERS`; lưu tin của cả hai bên + ảnh / file vào `~/autofix/.telegram/` (SQLite `requests.db`, `media/`).
  Các tin liên tiếp của tester cách nhau ≤ `TG_GROUP_WINDOW` giây (hoặc trả lời vào tin của yêu cầu chưa duyệt) gom thành
  một yêu cầu; hết khoảng đó → **Chờ duyệt**. `TG_DETECT_MODE=hashtag` thì chỉ tin có `#bug`, `#yc`… mới tạo yêu cầu.
- Trên trang theo dõi (bấm một yêu cầu):
  - **Duyệt → AI soạn & tạo task**: dùng luồng "+ Tạo task" (Claude soạn theo mẫu ticket của tester, đính kèm ảnh) rồi tự
    tạo task **New giao cho mình** → op-watch thấy task New → autofix sửa như thường.
  - **Duyệt → xem bản nháp trước**: AI soạn xong, nút "Xem bản nháp & tạo task" mở hộp Tạo task để sửa rồi tạo.
  - **Gắn vào việc có sẵn** (số WP hoặc SOAR-xxxx): comment nội dung tester + đính kèm file vào việc đó.
  - **Từ chối** (kèm lý do) / **Bỏ qua** (không phải yêu cầu) / **Đưa về chờ duyệt**.
  - **💬 Chat**: xem toàn bộ cuộc trò chuyện, **nhập tin cũ** (tg_watch.py đọc lịch sử), chọn tin → tạo yêu cầu.
- **⚙ Cài đặt** (trong mục Telegram trên trang theo dõi): API id / hash, danh sách tester, cách gom tin, hashtag, gửi tin
  trả lời, trạng thái coi là xong, các mẫu tin. Lưu vào `~/.config/op-watch/telegram.json` (quyền 600, đổi đường dẫn bằng
  `TG_CONFIG`), **ghi đè** `TG_*` trong `.env`. Đổi API / tester thì trang tự khởi động lại `op-telegram`; mục khác áp
  dụng ngay. API hash không hiển thị lại trên trang.
  **Đăng nhập trên trang**: "Cài & bật op-telegram" → nhập số điện thoại → mã Telegram gửi về → mật khẩu 2 bước (nếu có);
  có nút Đăng xuất. op-telegram chưa đăng nhập thì không thoát mà chờ lệnh đăng nhập; số điện thoại / mã / mật khẩu đi
  qua hàng lệnh trong `requests.db` (quyền 600) và bị xoá ngay khi đọc, lệnh quá 10 phút bỏ qua.
- **Lọc việc bằng AI** (`TG_AI_FILTER`, mặc định bật; model `TG_AI_MODEL`): gom tin xong → Claude tách việc, bỏ qua trò
  chuyện, đánh dấu việc đã có (đọc trạng thái ticket được nhắc + 200 ticket cập nhật trong 21 ngày + yêu cầu cũ). Mọi
  việc vẫn chờ mình duyệt; việc AI cho là đã có thì phải xác nhận "Vẫn tạo việc mới".
- `TG_NOTIFY_TESTER=1`: tg_watch.py trả lời tester (từ tài khoản mình, reply vào tin đầu của yêu cầu) khi tạo task /
  gắn việc / từ chối, và khi task sang `TG_DONE_STATUSES`. Mẫu tin: `TG_MSG_*` trong `.env`.

Cài đặt:

Toàn bộ làm được trên trang (⚙ Cài đặt). Cách dùng terminal (tuỳ chọn):

```bash
python3 -m pip install --user telethon
# .env: TG_API_ID, TG_API_HASH (https://my.telegram.org → API development tools)
python3 tg_watch.py --login      # nhập số điện thoại, mã Telegram gửi về, mật khẩu 2 bước (nếu có)
python3 tg_watch.py --dialogs    # lấy ID / @username tester → TG_TESTERS
./install.sh op-telegram         # cài + chạy service
systemctl --user restart op-dashboard
journalctl --user -u op-telegram -f
```

File phiên `TG_SESSION` (mặc định `~/.config/op-watch/telegram.session`, quyền 600) có toàn quyền vào tài khoản
Telegram: không chia sẻ / commit; thu hồi ở Telegram → Settings → Devices. Nên báo tester là tin nhắn được ghi nhận tự động.
