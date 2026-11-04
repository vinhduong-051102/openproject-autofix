#!/usr/bin/env bash
# Cài các systemd user unit của op-watch từ mẫu trong systemd/ (điền đường dẫn thư mục này + lịch trong .env).
#   ./install.sh                     # cài tất cả, enable + start
#   ./install.sh op-watch op-dashboard   # chỉ các unit này
#   ./install.sh --no-start ...      # chỉ ghi file + daemon-reload
set -euo pipefail
TOOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PYTHON="$(command -v python3)"
START=1
if [[ "${1:-}" == "--no-start" ]]; then START=0; shift; fi
NAMES=("$@")
[[ ${#NAMES[@]} -eq 0 ]] && NAMES=(op-watch op-dashboard op-merge op-report)
# op-telegram (yêu cầu tester qua Telegram) cài riêng: ./install.sh op-telegram, hoặc trang theo dõi → Telegram → ⚙ Cài đặt

[[ -f "$TOOL_DIR/.env" ]] || echo "Chưa có $TOOL_DIR/.env — chạy: cp .env.example .env rồi điền giá trị" >&2

# Đọc lịch + PATH qua envconf (cùng thứ tự ưu tiên với các script).
read_env() { (cd "$TOOL_DIR" && "$PYTHON" -c "import envconf,sys; print(envconf.get(sys.argv[1], sys.argv[2]))" "$1" "$2"); }
EXTRA_PATH="$(cd "$TOOL_DIR" && "$PYTHON" -c "import envconf; print(':'.join(envconf.extra_path()))")"
oncal() { local out="" IFS=';'; for t in $1; do t="$(echo "$t" | sed 's/^ *//;s/ *$//')"; [[ -n "$t" ]] && out+="OnCalendar=$t"$'\n'; done; printf '%s' "$out"; }
MERGE_ONCALENDAR="$(oncal "$(read_env MERGE_SCHEDULE '*-*-* 12:00:00;*-*-* 18:00:00')")"
REPORT_ONCALENDAR="$(oncal "$(read_env REPORT_SCHEDULE 'Mon..Fri *-*-* 08:00:00')")"

mkdir -p "$UNIT_DIR"
render() {
  "$PYTHON" - "$1" "$2" <<PY
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
for k, v in {"@TOOL_DIR@": """$TOOL_DIR""", "@PYTHON@": """$PYTHON""", "@EXTRA_PATH@": """$EXTRA_PATH""",
             "@MERGE_ONCALENDAR@": """$MERGE_ONCALENDAR""".rstrip(), "@REPORT_ONCALENDAR@": """$REPORT_ONCALENDAR""".rstrip()}.items():
    text = text.replace(k, v)
open(dst, "w", encoding="utf-8").write(text)
PY
}
ENABLE=()
for n in "${NAMES[@]}"; do
  for ext in service timer; do
    src="$TOOL_DIR/systemd/$n.$ext"
    [[ -f "$src" ]] || continue
    render "$src" "$UNIT_DIR/$n.$ext"
    echo "đã ghi $UNIT_DIR/$n.$ext"
  done
  if [[ -f "$TOOL_DIR/systemd/$n.timer" ]]; then ENABLE+=("$n.timer"); else ENABLE+=("$n.service"); fi
done
systemctl --user daemon-reload
if [[ $START -eq 1 ]]; then
  systemctl --user enable --now "${ENABLE[@]}"
  for u in "${ENABLE[@]}"; do [[ "$u" == *.service ]] && systemctl --user restart "$u"; done
  systemctl --user --no-pager list-timers 'op-*' || true
fi
