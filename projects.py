"""
projects: tool làm việc cho NHIỀU dự án OpenProject — mỗi dự án có repo, server test, quy ước riêng.

Khai báo trong config.json → "projects": [...] (trang theo dõi → tab Dự án). Chưa khai báo thì dựng MỘT dự án từ
cấu hình cũ (project / OP_TICKET_PREFIX / OP_PROJECT_NAME / autofix.repos / deploy.json) — hành vi như trước.

Mỗi dự án:
  key             mã ngắn (chữ thường, số, - _), vd "soar"
  name            tên hiện trong prompt / trang theo dõi, vd "SOAR"
  op_project      id (hoặc identifier) dự án trên OpenProject, vd "223"
  prefix          tiền tố mã ticket (SOAR → SOAR-123) — dùng nhận ticket thuộc dự án nào, tag commit [SOAR-123]
  enabled         false = không tự sửa ticket mới của dự án (vẫn xem / tạo task được)
  statuses        {"id": "tên"} trạng thái cần theo dõi (trống = dùng chung "statuses" của config)
  min_ticket_number  chỉ tự sửa ticket có số từ mức này trở lên (0 = tất cả)
  title_template  mẫu tiêu đề khi AI soạn task lỗi
  context         bối cảnh dự án (nghiệp vụ, thuật ngữ, quy ước) — thêm vào prompt Claude khi sửa ticket
  repos           danh sách repo (xem repos.py). Mã repo (key) không trùng giữa các dự án.
  deploy          server test riêng: host, user, password, base, fe_url, api_url, qa_user_id, qa_user_name…
                  (ghi đè deploy.json). Dự án dựng từ cấu hình cũ dùng luôn deploy.json (deploy_inherit).
                  deploy.auth: cách đăng nhập app (soar_api / ui_check): kind soar | json | static | none — xem soar_api.py
  qa              true = QA tự động trên server test sau deploy (cần fe_url / api_url + cách đăng nhập); false = chỉ
                  deploy, không QA tự động
"""
import copy
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,19}$")
FIELDS = ("key", "name", "op_project", "prefix", "enabled", "statuses", "min_ticket_number", "title_template",
          "context", "repos", "deploy", "deploy_inherit", "qa")
# Khoá server test ghi đè được theo dự án (còn lại — thông số rollout FE/BE kiểu SOAR — lấy từ deploy.json).
DEPLOY_FIELDS = ("host", "user", "password", "base", "fe_url", "api_url", "app_user", "app_password", "app_otp",
                 "default_account", "qa_user_id", "qa_user_name", "qa_review_status_ids", "qa_ticket_accounts", "auth", "ping_path")
SECRET_DEPLOY = ("password", "app_password", "app_otp")
# Dự án không kế thừa deploy.json thì KHÔNG mang các khoá này của dự án khác sang (tránh deploy / QA nhầm server).
SERVER_FIELDS = DEPLOY_FIELDS + ("accounts",)


def legacy(cfg):
    """Một dự án dựng từ cấu hình cũ (chưa có "projects")."""
    acfg = cfg.get("autofix") or {}
    prefix = envconf.TICKET_PREFIX
    return {"key": re.sub(r"[^a-z0-9_-]", "", prefix.lower()) or "main", "name": envconf.PROJECT_NAME,
            "op_project": str(cfg.get("project") or ""), "prefix": prefix, "enabled": True,
            "min_ticket_number": acfg.get("min_ticket_number") or 0, "title_template": envconf.TASK_TITLE_TEMPLATE,
            "context": "", "repos": acfg.get("repos") or [], "deploy": {}, "deploy_inherit": True, "qa": True,
            "_legacy": True}


def _norm(raw):
    p = {k: copy.deepcopy(raw[k]) for k in FIELDS if k in raw}
    p["key"] = str(p.get("key") or "").strip().lower()
    if not KEY_RE.match(p["key"]):
        raise ValueError("mã dự án không hợp lệ: %r (chữ thường, số, - _)" % p["key"])
    p["prefix"] = str(p.get("prefix") or "").strip().upper()
    if not PREFIX_RE.match(p["prefix"]):
        raise ValueError("%s: tiền tố mã ticket không hợp lệ: %r (vd SOAR)" % (p["key"], p["prefix"]))
    p["name"] = str(p.get("name") or p["prefix"]).strip()
    p["op_project"] = str(p.get("op_project") or "").strip()
    if not p["op_project"]:
        raise ValueError("%s: thiếu dự án OpenProject (op_project)" % p["key"])
    p["enabled"] = p.get("enabled") is not False
    p["qa"] = bool(p.get("qa"))
    p["deploy_inherit"] = bool(p.get("deploy_inherit"))
    p["statuses"] = {str(k): v for k, v in (p.get("statuses") or {}).items()}
    try:
        p["min_ticket_number"] = int(p.get("min_ticket_number") or 0)
    except (TypeError, ValueError):
        p["min_ticket_number"] = 0
    p["title_template"] = str(p.get("title_template") or envconf.TASK_TITLE_TEMPLATE).strip()
    p["context"] = str(p.get("context") or "").strip()
    p["repos"] = list(p.get("repos") or [])
    p["deploy"] = {k: v for k, v in (p.get("deploy") or {}).items() if k in DEPLOY_FIELDS and v not in (None, "")}
    p["_legacy"] = bool(raw.get("_legacy"))
    return p


def projects(cfg, enabled_only=False):
    """Danh sách dự án đã chuẩn hoá (dự án đầu = mặc định)."""
    raw = cfg.get("projects")
    items = [_norm(x) for x in raw if isinstance(x, dict)] if raw else [_norm(legacy(cfg))]
    return [p for p in items if p["enabled"]] if enabled_only else items


def validate(items):
    """Kiểm tra cả danh sách: mã / tiền tố / dự án OP / mã repo không trùng. Trả danh sách đã chuẩn hoá."""
    import repos as R
    out = [_norm(x) for x in items]
    if not out:
        raise ValueError("cần ít nhất một dự án")
    for field, what in (("key", "mã dự án"), ("prefix", "tiền tố mã ticket"), ("op_project", "dự án OpenProject")):
        seen = set()
        for p in out:
            if p[field] in seen:
                raise ValueError("%s trùng: %s" % (what, p[field]))
            seen.add(p[field])
    owner = {}
    for p in out:
        for r in R.repos(scope_acfg({}, p)) if p["repos"] else []:
            if r["key"] in owner:
                raise ValueError("mã repo %r dùng ở cả %s và %s — mỗi repo chỉ thuộc một dự án" % (
                    r["key"], owner[r["key"]], p["key"]))
            owner[r["key"]] = p["key"]
    return out


def default(cfg):
    return projects(cfg)[0]


def get(cfg, key):
    for p in projects(cfg):
        if key in (p["key"], p["prefix"], p["op_project"]):
            return p
    return None


def prefix_of(ticket):
    m = re.match(r"^([A-Za-z][A-Za-z0-9_]*)-\d+", str(ticket or ""))
    return m.group(1).upper() if m else ""


def by_ticket(cfg, ticket, wp=None):
    """Dự án của một ticket: theo tiền tố mã (SOAR-123 / tên thư mục lượt chạy), không khớp thì theo dự án OP của
    work package (wp), cuối cùng là dự án mặc định."""
    items = projects(cfg)
    pre = prefix_of(ticket)
    for p in items:
        if pre and p["prefix"] == pre:
            return p
    if wp:
        href = ((wp.get("_links") or {}).get("project") or {}).get("href") or ""
        pid = href.rstrip("/").split("/")[-1]
        for p in items:
            if pid and p["op_project"] == pid:
                return p
    return items[0]


def scope_acfg(acfg, proj):
    """Bản sao cấu hình autofix chỉ còn repo / quy ước của dự án `proj`."""
    out = dict(acfg)
    if proj.get("repos"):
        out["repos"] = proj["repos"]
    elif not proj.get("_legacy"):
        out["repos"] = []
    if proj.get("min_ticket_number") is not None:
        out["min_ticket_number"] = proj["min_ticket_number"]
    out["_project"] = proj
    return out


def scope(cfg, acfg, proj):
    """(cfg, acfg) theo dự án: cfg["project"] = dự án OP, repo / trạng thái theo dự án."""
    c = dict(cfg)
    c["project"] = proj["op_project"]
    if proj.get("statuses"):
        c["statuses"] = dict(proj["statuses"])
    c["_project"] = proj
    a = scope_acfg(acfg if acfg is not None else (cfg.get("autofix") or {}), proj)
    c["autofix"] = a
    return c, a


def for_ticket(cfg, acfg, ticket, wp=None):
    return scope(cfg, acfg, by_ticket(cfg, ticket, wp))


def all_repos(acfg, cfg):
    """Bản sao acfg với repo của MỌI dự án (merge / dọn dẹp làm trên tất cả repo)."""
    import repos as R
    items = projects(cfg)
    out = dict(acfg)
    if len(items) == 1:
        return scope_acfg(acfg, items[0])
    merged = []
    for p in items:
        merged += [dict(r, _project=p["key"]) for r in R.repos(scope_acfg(acfg, p))] if (p["repos"] or p["_legacy"]) else []
    out["repos"] = merged
    return out


def current(cfg_or_acfg):
    """Dự án đã gắn vào cấu hình (sau scope), không có thì None."""
    return (cfg_or_acfg or {}).get("_project")


def name(cfg_or_acfg):
    p = current(cfg_or_acfg)
    return p["name"] if p else envconf.PROJECT_NAME


def title_template(cfg_or_acfg):
    p = current(cfg_or_acfg)
    return p["title_template"] if p else envconf.TASK_TITLE_TEMPLATE


def prefix(cfg_or_acfg):
    p = current(cfg_or_acfg)
    return p["prefix"] if p else envconf.TICKET_PREFIX


def deploy_cfg(proj, base):
    """Cấu hình server test của dự án: base (mặc định deploy_qa + deploy.json) ghi đè bởi proj["deploy"]. Dự án
    không kế thừa deploy.json thì bỏ các khoá server / tài khoản của deploy.json."""
    if proj.get("deploy_inherit") or proj.get("_legacy"):
        d = dict(base)
    else:
        d = {k: v for k, v in base.items() if k not in SERVER_FIELDS}
    d.update(proj.get("deploy") or {})
    d["_project"] = proj["key"]
    d["_inherit"] = bool(proj.get("deploy_inherit") or proj.get("_legacy"))  # server kiểu SOAR (deploy.json)
    return d


def public(proj):
    """Dự án để hiện trên trang (ẩn mật khẩu server)."""
    p = {k: copy.deepcopy(v) for k, v in proj.items() if k in FIELDS or k == "_legacy"}
    p["deploy"] = {k: ("••••••" if k in SECRET_DEPLOY and v else v) for k, v in (p.get("deploy") or {}).items()}
    if isinstance(p["deploy"].get("auth"), dict) and p["deploy"]["auth"].get("token"):
        p["deploy"]["auth"] = dict(p["deploy"]["auth"], token="••••••")
    return p
