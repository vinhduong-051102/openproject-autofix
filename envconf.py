"""
envconf: đọc cấu hình từ biến môi trường / file `.env` cạnh các script (chỉ dùng thư viện chuẩn, Python 3.8).

Thứ tự ưu tiên của một giá trị:
    biến môi trường thật  >  .env  >  file JSON (~/.config/op-watch/*.json)  >  mặc định trong code
Riêng các khoá được chỉnh trên trang theo dõi (số agent song song, ngưỡng hạn mức, tự chuyển gói…) thì file JSON
thắng — trang ghi vào JSON, .env chỉ là giá trị ban đầu khi JSON chưa có khoá đó (xem MANAGED).

Giá trị rỗng (`KEY=`) coi như không đặt. Trong giá trị dùng được `${TEN_BIEN}` (biến đã có, kể cả `${TOOL_DIR}` =
thư mục chứa các script) và `~`.
"""
import os
import re

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.environ.get("OP_WATCH_ENV_FILE") or os.path.join(TOOL_DIR, ".env")
_VAR = re.compile(r"\$\{(\w+)\}")


def _expand(value):
    return _VAR.sub(lambda m: os.environ.get(m.group(1), TOOL_DIR if m.group(1) == "TOOL_DIR" else ""), value)


def load_dotenv(path=ENV_FILE):
    """Nạp .env vào os.environ, không ghi đè biến môi trường đã có."""
    os.environ.setdefault("TOOL_DIR", TOOL_DIR)
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not re.match(r"^[A-Za-z_]\w*$", key):
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                quoted, value = value[0], value[1:-1]
                if quoted == '"':
                    value = _expand(value.replace('\\"', '"'))
            else:
                value = _expand(re.split(r"\s+#", value, 1)[0].strip())
            if value and not os.environ.get(key):
                os.environ[key] = value


load_dotenv()


def get(name, default=None):
    value = os.environ.get(name)
    return _expand(value) if value not in (None, "") else default


def path(name, default):
    return os.path.expanduser(get(name, default))


def _bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on", "có", "co")


def _list(v):
    return [x.strip() for x in str(v).split(",") if x.strip()]


def _status_map(v):
    """"1:New,100:Reopened" → {"1": "New", "100": "Reopened"}"""
    out = {}
    for part in _list(v):
        k, _, name = part.partition(":")
        out[k.strip()] = name.strip() or k.strip()
    return out


def _num(v):
    v = str(v).strip()
    return float(v) if any(c in v for c in ".eE") else int(v)


CONVERT = {"str": str, "path": os.path.expanduser, "int": int, "float": _num, "bool": _bool, "list": _list,
           "statuses": _status_map, "int_or_none": lambda v: None if str(v).strip().lower() in ("null", "none") else int(v)}


def overlay(target, mapping, managed=()):
    """Ghi giá trị từ biến môi trường vào dict `target`. Khoá trong `managed` chỉ đặt khi target chưa có."""
    for env, (key, kind) in mapping.items():
        raw = get(env)
        if raw is None:
            continue
        if key in managed and key in target:
            continue
        target[key] = CONVERT[kind](raw)
    return target


# ---- Bảng biến môi trường → khoá cấu hình -------------------------------------------------------------------------
# config.json (phần chung op-watch)
OP_ENV = {
    "OP_URL": ("url", "str"),
    "OP_API_KEY": ("api_key", "str"),
    "OP_PROJECT": ("project", "str"),
    "OP_WATCH_STATUSES": ("statuses", "statuses"),
    "OP_INTERVAL": ("interval", "int"),
    "OP_INBOX": ("inbox", "str"),
    "OP_NOTIFY": ("notify", "bool"),
    "OP_ON_CHANGE": ("on_change", "str"),
    "OP_ON_TICK": ("on_tick", "str"),
    "OP_TICK_INTERVAL": ("tick_interval", "int"),
}

# config.json → "autofix"
AUTOFIX_ENV = {
    "AUTOFIX_ENABLED": ("enabled", "bool"),
    "AUTOFIX_EVENTS": ("events", "list"),
    "AUTOFIX_ROOT": ("root", "str"),
    "FE_REPO": ("fe_repo", "path"),
    "FE_BRANCH": ("fe_branch", "str"),
    "FE_UPSTREAM": ("fe_upstream", "str"),
    "BE_REPO": ("be_repo", "path"),
    "BE_BRANCH": ("be_branch", "str"),
    "BE_UPSTREAM": ("be_upstream", "str"),
    "BE_SUBDIR": ("be_subdir", "str"),
    "AUTOFIX_MAX_BUDGET_USD": ("max_budget_usd", "float"),
    "AUTOFIX_MIN_TICKET_NUMBER": ("min_ticket_number", "int"),
    "AUTOFIX_AUTO_SHIP": ("auto_ship", "bool"),
    "AUTOFIX_REPORT_DIR": ("report_dir", "str"),
    "AUTOFIX_CLAUDE_RESOLVE_CONFLICTS": ("claude_resolve_conflicts", "bool"),
    "OP_STATUS_REVIEW": ("review_status_id", "int_or_none"),
    "OP_STATUS_RESOLVED": ("resolved_status_id", "int"),
    "OP_STATUS_FOLLOWUP": ("followup_status_id", "int_or_none"),
    "CLAUDE_CMD": ("claude", "str"),
    "CLAUDE_MODEL": ("model", "str"),
    "NODE_BIN": ("node_bin", "str"),
    "GEMINI_ENABLED": ("gemini_enabled", "bool"),
    "GEMINI_CMD": ("gemini_cmd", "str"),
    "GEMINI_MODEL": ("gemini_model", "str"),
    "GEMINI_TIMEOUT": ("gemini_timeout", "int"),
    "GEMINI_BLOCK_ON_FAIL": ("gemini_block_on_fail", "bool"),
    # Các khoá dưới đây chỉnh trên trang theo dõi → JSON thắng (AUTOFIX_MANAGED).
    "AUTOFIX_MAX_PARALLEL": ("max_parallel", "int"),
    "AUTOFIX_PAUSE_ABOVE_UTILIZATION": ("pause_above_utilization", "float"),
    "AUTOFIX_SWITCH_ABOVE_7D": ("switch_above_7d", "float"),
    "AUTOFIX_FOLLOWUP_SKIP_ABOVE_7D": ("followup_skip_above_7d", "float"),
    "AUTOFIX_AUTO_SWITCH": ("auto_switch", "bool"),
}
AUTOFIX_MANAGED = ("max_parallel", "pause_above_utilization", "switch_above_7d", "followup_skip_above_7d",
                   "auto_switch")

# deploy.json (server test + QA)
DEPLOY_ENV = {
    "DEPLOY_HOST": ("host", "str"),
    "DEPLOY_SSH_USER": ("user", "str"),
    "DEPLOY_SSH_PASSWORD": ("password", "str"),
    "DEPLOY_BASE": ("base", "str"),
    "DEPLOY_FE_URL": ("fe_url", "str"),
    "DEPLOY_API_URL": ("api_url", "str"),
    "DEPLOY_APP_USER": ("app_user", "str"),
    "DEPLOY_APP_PASSWORD": ("app_password", "str"),
    "DEPLOY_APP_OTP": ("app_otp", "str"),
    "DEPLOY_DEFAULT_ACCOUNT": ("default_account", "str"),
    "DEPLOY_FE_SERVICE": ("fe_service", "str"),
    "DEPLOY_BE_SERVICE": ("be_service", "str"),
    "DEPLOY_FE_IMAGE": ("fe_image", "str"),
    "DEPLOY_BE_IMAGE": ("be_image", "str"),
    "DEPLOY_BE_RELEASE_BRANCH": ("be_release_branch", "str"),
    "DEPLOY_BE_CONTAINER": ("be_container", "str"),
    "DEPLOY_WAIT_MINUTES": ("wait_minutes", "int"),
    "DEPLOY_SERVER_RETRY_MINUTES": ("server_retry_minutes", "int"),
    "QA_USER_ID": ("qa_user_id", "int"),
    "QA_USER_NAME": ("qa_user_name", "str"),
    "QA_ENGINE": ("qa_engine", "str"),
    "QA_MODEL": ("qa_model", "str"),
    "QA_ROUNDS": ("qa_rounds", "int"),
    "QA_CALLS_PER_ROUND": ("qa_calls_per_round", "int"),
    "QA_CALL_TIMEOUT": ("qa_call_timeout", "int"),
    "QA_FAIL_AUTOFIX_MAX": ("qa_fail_autofix_max", "int"),
    "QA_PARALLEL": ("qa_parallel", "int"),  # chỉnh trên trang → JSON thắng
    "QA_TICKET_ACCOUNTS": ("qa_ticket_accounts", "list"),
    "QA_REVIEW_STATUSES": ("qa_review_status_ids", "list"),
}
DEPLOY_MANAGED = ("qa_parallel",)

# gitea.json
GITEA_ENV = {
    "GITEA_URL": ("url", "str"),
    "GITEA_TOKEN": ("token", "str"),
}

# ---- Đường dẫn dùng chung ----------------------------------------------------------------------------------------
CONFIG_DIR = path("OP_WATCH_CONFIG_DIR", "~/.config/op-watch")
CONFIG_PATH = path("OP_WATCH_CONFIG", os.path.join(CONFIG_DIR, "config.json"))
DEPLOY_CONFIG = path("DEPLOY_CONFIG", os.path.join(CONFIG_DIR, "deploy.json"))
GITEA_CONFIG = path("GITEA_CONFIG", os.path.join(CONFIG_DIR, "gitea.json"))
TOKEN_DIR = path("CLAUDE_TOKEN_DIR", os.path.join(CONFIG_DIR, "tokens"))
STATE_PATH = path("OP_WATCH_STATE", "~/.local/state/op-watch/state.json")
CACHE_DIR = path("OP_WATCH_CACHE_DIR", "~/.cache/op-watch")
DASHBOARD_PORT = int(get("DASHBOARD_PORT", "8765"))
# IP nghe thêm ngoài 127.0.0.1 (vd IP Tailscale 100.x), cách nhau dấu phẩy. Trống = chỉ localhost.
DASHBOARD_EXTRA_BIND = [h.strip() for h in get("DASHBOARD_EXTRA_BIND", "").split(",") if h.strip()]


def _read_json(p):
    try:
        import json
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def deploy_config():
    """deploy.json + biến DEPLOY_* / QA_* (chưa gộp mặc định của deploy_qa)."""
    data = _read_json(DEPLOY_CONFIG)
    return overlay(data, DEPLOY_ENV, managed=DEPLOY_MANAGED)


def gitea_config():
    """gitea.json + GITEA_URL / GITEA_TOKEN."""
    return overlay(_read_json(GITEA_CONFIG), GITEA_ENV)


def autofix_root():
    """Thư mục làm việc autofix (dùng khi chưa có cấu hình đầy đủ, vd hằng số cấp module)."""
    root = get("AUTOFIX_ROOT") or (_read_json(CONFIG_PATH).get("autofix") or {}).get("root") or "~/autofix"
    return os.path.expanduser(root)


def extra_path():
    """Thư mục thêm vào đầu PATH (systemd có thể chạy service trước khi PATH người dùng có ~/.local/bin)."""
    return [os.path.expanduser(p) for p in get("EXTRA_PATH", "~/.local/bin").split(":") if p]
