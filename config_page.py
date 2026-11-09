"""
config_page: trang Cấu hình trên trang theo dõi — sửa .env + danh sách repo mà không cần mở file.

- Biến cấu hình lấy TỰ ĐỘNG từ .env.example: nhóm theo dòng tiêu đề (# ===== X ===== / # ---- X ----), ghi chú ngay
  trên mỗi KEY= là hướng dẫn. Thêm biến mới vào .env.example là trang tự hiện — không phải sửa giao diện.
- Ghi vào .env giữ nguyên các dòng / ghi chú khác, sao lưu .env trước mỗi lần lưu. Biến bí mật không trả về trang.
- Dự án (projects.py) ghi vào config.json → projects; mỗi dự án có danh sách repo riêng (repos.py). Chưa khai báo dự án
  thì repo ghi vào autofix.repos như cũ; lưu dự án lần đầu thì chuyển autofix.repos vào dự án đầu tiên.
- Áp dụng: khởi động lại dịch vụ đọc .env lúc chạy; đổi lịch → cài lại timer.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402
import repos as R  # noqa: E402
import projects as P  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, ".env.example")
SECRET_RX = re.compile(r"(API_KEY|TOKEN|PASSWORD|PASS|HASH|SECRET|OTP)$")
KEY_LINE = re.compile(r"^(#\s*)?([A-Z][A-Z0-9_]+)=(.*)$")
SECTION = re.compile(r"^#\s*(?:=+|-+)\s*(.+?)\s*(?:=+|-+)\s*$")
# Biến cần cài lại timer khi đổi (install.sh điền lịch vào unit)
SCHEDULE_UNITS = {"MERGE_SCHEDULE": "op-merge", "REPORT_SCHEDULE": "op-report", "LIMITS_SCHEDULE": "op-limits"}
SERVICES = ("op-watch", "op-telegram")  # đọc .env lúc khởi động (dashboard tự khởi động lại sau cùng)


def _kinds():
    """Kiểu của biến (int / float / bool / list…) theo bảng biến của envconf — để kiểm tra khi lưu."""
    out = {}
    for name in ("OP_ENV", "AUTOFIX_ENV", "DEPLOY_ENV", "GITEA_ENV"):
        for env, (_, kind) in getattr(envconf, name, {}).items():
            out[env] = kind
    out.update({"DASHBOARD_PORT": "int", "OP_LOG_TIME": "bool", "OP_SET_DATES": "bool"})
    return out


def _parse_env(path):
    vals = {}
    if not os.path.exists(path):
        return vals
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = KEY_LINE.match(line.rstrip("\n"))
            if m and not m.group(1):
                v = m.group(3).strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                vals[m.group(2)] = v
    return vals


def schema():
    """Nhóm biến từ .env.example + giá trị hiện tại trong .env (bí mật chỉ cho biết đã đặt chưa)."""
    cur = _parse_env(envconf.ENV_FILE)
    kinds = _kinds()
    groups, group, help_lines, seen = [], {"title": "Chung", "items": []}, [], set()
    prev_help, prev_key = "", False  # các biến liền nhau dùng chung ghi chú phía trên
    with open(EXAMPLE, encoding="utf-8") as f:
        lines = [l.rstrip("\n") for l in f]
    for line in lines:
        if not line.strip():
            help_lines, prev_key = [], False
            continue
        sm = SECTION.match(line)
        if sm and not KEY_LINE.match(line.lstrip("# ")):
            title = sm.group(1).strip(" -=")
            if title and not title.startswith("op-watch /"):
                if group["items"]:
                    groups.append(group)
                group = {"title": title, "items": []}
            help_lines = []
            continue
        km = KEY_LINE.match(line)
        if km and (not km.group(1) or km.group(1).strip() == "#") and km.group(2) not in seen:
            key = km.group(2)
            seen.add(key)
            secret = bool(SECRET_RX.search(key))
            val = cur.get(key)
            text = " ".join(help_lines).strip() or (prev_help if prev_key else "")
            help_lines, prev_help, prev_key = [], text, True
            item = {"key": key, "help": text, "default": km.group(3).strip(),
                    "optional": bool(km.group(1)), "kind": kinds.get(key, "str"), "secret": secret,
                    "set": val not in (None, ""), "in_env": key in cur}
            if not secret:
                item["value"] = val if val is not None else ""
            group["items"].append(item)
            continue
        if line.startswith("#"):
            help_lines.append(line.lstrip("#").strip())
            prev_key = False
    if group["items"]:
        groups.append(group)
    extra = [{"key": k, "value": ("" if SECRET_RX.search(k) else v), "secret": bool(SECRET_RX.search(k)),
              "set": v != "", "help": "có trong .env nhưng không có trong .env.example", "kind": "str",
              "in_env": True, "default": "", "optional": True} for k, v in cur.items() if k not in seen]
    if extra:
        groups.append({"title": "Biến khác trong .env", "items": extra})
    return {"groups": groups, "env_path": envconf.ENV_FILE, "example": EXAMPLE}


def _check(key, value, kind):
    if value == "":
        return value
    if kind == "int":
        if not re.match(r"^-?\d+$", value):
            raise ValueError("%s phải là số nguyên" % key)
    elif kind in ("float",):
        try:
            float(value)
        except ValueError:
            raise ValueError("%s phải là số" % key)
    elif kind == "int_or_none":
        if value.lower() not in ("null", "none") and not re.match(r"^-?\d+$", value):
            raise ValueError("%s phải là số hoặc null" % key)
    elif kind == "bool":
        if value.lower() not in ("0", "1", "true", "false", "yes", "no", "on", "off", "có", "không"):
            raise ValueError("%s phải là true / false" % key)
    if "\n" in value:
        raise ValueError("%s không được xuống dòng (dùng \\n)" % key)
    return value


def _quote(v):
    if v == "" or re.match(r"^[^\s\"'#]*$", v) or (" #" not in v and not v.startswith(("'", '"')) and v == v.strip()):
        return v
    return '"%s"' % v.replace("\\", "\\\\").replace('"', '\\"')


def save_env(changes):
    """Ghi các biến đã đổi vào .env (giữ dòng / ghi chú khác). Biến bí mật để trống = giữ nguyên. Trả danh sách đổi."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("chưa có thay đổi")
    known = {i["key"]: i for g in schema()["groups"] for i in g["items"]}
    kinds = _kinds()
    path = envconf.ENV_FILE
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
    cur = _parse_env(path)
    done = []
    for key, value in changes.items():
        if not re.match(r"^[A-Z][A-Z0-9_]+$", key or ""):
            raise ValueError("tên biến không hợp lệ: %r" % key)
        if key not in known:
            raise ValueError("biến %s không có trong .env.example" % key)
        value = "" if value is None else str(value).strip()
        if known[key]["secret"] and value == "":
            continue  # bí mật để trống = giữ nguyên
        value = _check(key, value, kinds.get(key, "str"))
        if cur.get(key, None) == value or (key not in cur and value == ""):
            continue
        new = "%s=%s" % (key, _quote(value))
        for i, l in enumerate(lines):
            m = KEY_LINE.match(l)
            if m and not m.group(1) and m.group(2) == key:
                lines[i] = new
                break
        else:
            if "# ---- Đặt từ trang theo dõi ----" not in lines:
                lines += ["", "# ---- Đặt từ trang theo dõi ----"]
            lines.append(new)
        done.append(key)
    if not done:
        return []
    if os.path.exists(path):
        shutil.copy2(path, "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S")))
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(path + ".tmp", path)
    return done


# ---------------------------------------------------------------- danh sách repo

REPO_FIELDS = ("key", "label", "name", "preset", "path", "branch", "upstream", "subdir", "tech", "describe", "rules",
               "check", "build", "test", "test_files", "test_compare", "link", "allow", "merge", "merge_check", "deploy",
               "ref_hint", "deploy_cmd", "deploy_ref", "deploy_health", "deploy_desc", "setup",
               "build_compare")


def repos_get(acfg):
    """Danh sách repo như đang khai báo (config.json) — chưa khai báo thì 2 repo FE / BE từ khoá cũ, dạng preset."""
    raw = acfg.get("repos")
    proj = acfg.get("_project")
    if raw or (proj and not proj.get("_legacy")):
        items = [{k: v for k, v in x.items() if k in REPO_FIELDS} for x in raw or []]
        declared = True
    else:
        items = []
        for r in R.legacy(acfg):
            items.append({"preset": r["key"], "key": r["key"], "path": r["path"], "branch": r["branch"],
                          "upstream": r["upstream"], "subdir": r.get("subdir") or "", "deploy": r["deploy"],
                          "merge": True})
        declared = False
    presets = {"fe": {k: v for k, v in R.FE_DEFAULT.items() if k in REPO_FIELDS},
               "be": {k: v for k, v in R.BE_DEFAULT.items() if k in REPO_FIELDS}}
    return {"repos": items, "declared": declared, "presets": presets, "deploy_kinds": list(R.DEPLOY_KINDS),
            "effective": [{k: r.get(k) for k in REPO_FIELDS} for r in R.repos(acfg)]}


def _git_ok(path, *args):
    return subprocess.run(["git", "-C", path] + list(args), stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def repos_check(items):
    """Chuẩn hoá + kiểm tra (đường dẫn là repo git, nhánh có trên origin). Trả (danh sách sạch, cảnh báo)."""
    clean = []
    for x in items or []:
        if not isinstance(x, dict):
            continue
        y = {}
        for k in REPO_FIELDS:
            v = x.get(k)
            if v in (None, "", []):
                continue
            if k == "deploy_cmd":  # script: giữ nguyên dòng (thụt lề, dòng trống trong heredoc)
                v = [str(s_).rstrip() for s_ in (v.splitlines() if isinstance(v, str) else v)]
                while v and not v[-1]:
                    v.pop()
            elif k in ("build", "test", "link", "allow", "merge_check", "setup"):
                v = [s.strip() for s in (v.splitlines() if isinstance(v, str) else v) if str(s).strip()]
            elif k == "merge":
                v = bool(v)
            else:
                v = str(v).strip() if not isinstance(v, (bool, list)) else v
            y[k] = v
        if y.get("preset") not in ("fe", "be"):
            y.pop("preset", None)
        clean.append(y)
    if not clean:
        raise ValueError("cần ít nhất một repo")
    norm = R.repos({"repos": clean})  # ném ValueError nếu key sai / trùng
    if sum(1 for r in norm if r["deploy"] == "fe-bundle") > 1 or sum(1 for r in norm if r["deploy"] == "be-image") > 1:
        raise ValueError("mỗi kiểu deploy (fe-bundle / be-image) chỉ dùng cho một repo")
    for r in norm:
        if r["deploy"] == "command" and not r["deploy_cmd"]:
            raise ValueError("%s: deploy \"command\" cần ít nhất một lệnh deploy" % r["label"])
    warn = []
    for r in norm:
        if not r["path"] or not os.path.isdir(r["path"]):
            raise ValueError("%s: không có thư mục %s" % (r["label"], r["path"] or "(trống)"))
        if not _git_ok(r["path"], "rev-parse", "--git-dir"):
            raise ValueError("%s: %s không phải repo git" % (r["label"], r["path"]))
        if not r.get("branch") or not r.get("upstream"):
            raise ValueError("%s: cần nhánh cá nhân (branch) và nhánh chung (upstream)" % r["label"])
        if not _git_ok(r["path"], "rev-parse", "-q", "--verify", "refs/remotes/origin/" + r["upstream"]):
            warn.append("%s: chưa thấy origin/%s (đã fetch chưa?)" % (r["label"], r["upstream"]))
        if not _git_ok(r["path"], "rev-parse", "-q", "--verify", "refs/remotes/origin/" + r["branch"]):
            warn.append("%s: nhánh cá nhân origin/%s chưa có — autofix cần nhánh này, tạo bằng: git -C %s push origin "
                        "origin/%s:refs/heads/%s" % (r["label"], r["branch"], r["path"], r["upstream"], r["branch"]))
        if not r["build"]:
            warn.append("%s: chưa có lệnh build — autofix sẽ không tự kiểm tra repo này" % r["label"])
        if r.get("subdir") and not os.path.isdir(os.path.join(r["path"], r["subdir"])):
            warn.append("%s: không thấy thư mục con %s" % (r["label"], r["subdir"]))
    return clean, warn


# ---------------------------------------------------------------- áp dụng

def apply(changed_keys=()):
    """Khởi động lại dịch vụ đọc .env; đổi lịch thì cài lại timer. Dashboard khởi động lại sau cùng (không chờ)."""
    notes = []
    units = sorted({SCHEDULE_UNITS[k] for k in changed_keys if k in SCHEDULE_UNITS})
    if units:
        res = subprocess.run(["bash", os.path.join(HERE, "install.sh")] + units, cwd=HERE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
        notes.append("cài lại %s%s" % (", ".join(units), "" if res.returncode == 0 else " (lỗi: %s)" % res.stdout[-200:]))
    for svc in SERVICES:
        st = subprocess.run(["systemctl", "--user", "is-active", svc], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, universal_newlines=True).stdout.strip()
        if st in ("active", "activating"):
            subprocess.run(["systemctl", "--user", "restart", svc], check=False)
            notes.append("khởi động lại %s" % svc)
    subprocess.Popen(["bash", "-c", "sleep 1; systemctl --user restart op-dashboard"], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    notes.append("trang theo dõi khởi động lại sau 1 giây (tải lại trang)")
    return "; ".join(notes)


def http_get(path, acfg, q=None):
    q = q or {}
    if path == "/api/config":
        return schema()
    if path == "/api/config/repos":
        cfg = _cfg()
        proj = (P.get(cfg, q["project"]) if q.get("project") else None) or P.default(cfg)
        return dict(repos_get(P.scope_acfg(acfg, proj) if cfg.get("projects") else acfg), project=proj["key"])
    if path == "/api/config/projects":
        return projects_get()
    if path == "/api/config/op_projects":
        return {"items": op_projects()}
    return None


def http_get_q(path, q, acfg):
    if path == "/api/config/browse":
        out = browse(q.get("path") or "~")
        if q.get("repos") == "1":
            out["git_repos"] = [x.replace(HOME, "~", 1) for x in find_git_repos()]
        return out
    return http_get(path, acfg, q)


def action(body, save_autofix):
    kind = body.get("action")
    if kind == "config_env":
        done = save_env(body.get("changes") or {})
        return {"message": ("Đã lưu %d biến vào .env: %s — bấm Áp dụng để dùng" % (len(done), ", ".join(done)))
                if done else "Không có gì thay đổi", "changed": done}
    if kind == "config_repos":
        clean, warn = repos_check(body.get("repos"))
        if body.get("project") and (_cfg().get("projects") or len(P.projects(_cfg())) > 1
                                    or body["project"] != P.default(_cfg())["key"]):
            save_project_repos(body["project"], clean)
        else:
            save_autofix(repos=clean)
        return {"message": "Đã lưu %d repo%s" % (len(clean), (" — lưu ý: " + "; ".join(warn)) if warn else ""),
                "warnings": warn}
    if kind == "config_repos_check":
        _, warn = repos_check(body.get("repos"))
        return {"message": "Hợp lệ%s" % ((" — lưu ý: " + "; ".join(warn)) if warn else ""), "warnings": warn}
    if kind == "config_repo_analyze":
        r = analyze_repo(body.get("path"), body.get("existing") or [])
        return dict(r, message="%s phân tích xong %s" % ("AI" if r["source"] == "ai" else "Đã đoán (không AI)", r["repo"]["path"]))
    if kind == "config_deploy_suggest":
        r = suggest_deploy(body.get("repo") or {}, body.get("desc"))
        return dict(r, message="AI đã viết %d lệnh deploy" % len(r["deploy_cmd"]))
    if kind == "config_projects":
        items, warn = projects_save(body.get("projects") or [])
        return {"message": "Đã lưu %d dự án%s — bấm Áp dụng để các dịch vụ đọc lại" % (
            len(items), (" — lưu ý: " + "; ".join(warn)) if warn else ""), "warnings": warn}
    if kind == "config_project_detect":
        return dict(detect_project(body.get("op_project")), message="Đã đọc dự án trên OpenProject")
    if kind == "config_project_suggest":
        r = suggest_project(body.get("project"))
        return dict(r, message="AI đã đọc source và điền cấu hình — xem lại rồi bấm Lưu dự án")
    if kind == "config_auth_test":
        return {"message": test_auth(body.get("project"))}
    if kind == "config_apply":
        return apply(body.get("changed") or [])
    raise ValueError("thao tác không hợp lệ")


# ---------------------------------------------------------------- chọn thư mục + AI phân tích repo

HOME = os.path.realpath(os.path.expanduser("~"))
SKIP_DIRS = {"node_modules", ".git", ".cache", ".npm", ".m2", ".local", ".config", "snap", ".venv", "venv", "target",
             "dist", "build", "__pycache__", ".gradle", ".cargo", ".rustup"}


def _inside_home(path):
    p = os.path.realpath(os.path.expanduser(path or "~"))
    if p != HOME and not p.startswith(HOME + os.sep):
        raise ValueError("chỉ chọn thư mục trong %s" % HOME)
    return p


def browse(path=None):
    """Thư mục con của path (trong home) + đánh dấu repo git."""
    p = _inside_home(path)
    if not os.path.isdir(p):
        raise ValueError("không có thư mục %s" % p)
    dirs = []
    try:
        names = sorted(os.listdir(p), key=str.lower)
    except OSError as ex:
        raise ValueError("không đọc được %s: %s" % (p, ex))
    for n in names:
        full = os.path.join(p, n)
        if n.startswith(".") or not os.path.isdir(full) or n in SKIP_DIRS:
            continue
        dirs.append({"name": n, "path": full, "git": os.path.exists(os.path.join(full, ".git"))})
    crumbs, cur = [], p
    while True:
        crumbs.insert(0, {"name": os.path.basename(cur) or cur, "path": cur})
        if cur == HOME:
            break
        cur = os.path.dirname(cur)
    return {"path": p, "git": os.path.exists(os.path.join(p, ".git")), "dirs": dirs[:500], "crumbs": crumbs,
            "parent": os.path.dirname(p) if p != HOME else None}


def find_git_repos(max_depth=3, limit=60):
    """Repo git trong home (tối đa max_depth cấp) — gợi ý chọn nhanh."""
    out, skip = [], os.path.realpath(envconf.autofix_root())  # bỏ worktree tạm của autofix
    for root, dirs, _ in os.walk(HOME):
        depth = root[len(HOME):].count(os.sep)
        if os.path.realpath(root) == skip:
            dirs[:] = []
            continue
        if os.path.isfile(os.path.join(root, ".git")):  # worktree (.git là file) — không phải repo gốc
            dirs[:] = []
            continue
        if ".git" in dirs:
            if root != HOME:
                out.append(root)
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS and depth < max_depth]
        if len(out) >= limit:
            break
    return sorted(out, key=lambda x: os.path.getmtime(x), reverse=True)


def _git(path, *args):
    r = subprocess.run(["git", "-C", path] + list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       universal_newlines=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def git_facts(path):
    remote = [b.strip().replace("origin/", "", 1) for b in _git(path, "branch", "-r", "--format=%(refname:short)").splitlines()
              if b.strip().startswith("origin/") and "HEAD" not in b]
    return {"current": _git(path, "branch", "--show-current"), "remote_branches": remote[:200],
            "default": _git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").replace("origin/", ""),
            "remote_url": _git(path, "remote", "get-url", "origin"),
            "recent": _git(path, "log", "--oneline", "-8")}


def guess_repo(path, existing=()):
    """Đoán nhanh (không AI) từ file đặc trưng — để điền ngay và làm dự phòng khi AI lỗi."""
    import getpass
    name = os.path.basename(path.rstrip("/"))
    key = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")[:30] or "repo"
    used = {e.get("key") for e in existing}
    base, i = key, 2
    while key in used:
        key, i = "%s%d" % (base, i), i + 1
    g = git_facts(path)
    rb = g["remote_branches"]
    user = getpass.getuser()
    upstream = next((b for b in ("dev-ss", "develop-ss", "develop", "dev", "main", "master") if b in rb), g["default"] or "")
    # Quy ước nhánh cá nhân: <nhánh chung>-<user> (vd dev-ss-vinhdq); có trên origin thì dùng đúng tên đó.
    exact = "%s-%s" % (upstream, user) if upstream else ""
    ends = [b for b in rb if b.endswith("-" + user)]
    branch = exact if exact in rb else (ends[0] if ends else exact)
    has = lambda *f: any(os.path.exists(os.path.join(path, x)) for x in f)  # noqa: E731
    r = {"key": key, "path": path.replace(HOME, "~", 1), "branch": branch, "upstream": upstream, "deploy": "none",
         "merge": True}
    sub = ""
    if not has("pom.xml"):  # Maven nhiều module: module chính có pom.xml riêng
        for d in sorted(os.listdir(path)):
            if os.path.isfile(os.path.join(path, d, "pom.xml")):
                sub = d
                break
    if has("package.json"):
        try:
            pkg = json.load(open(os.path.join(path, "package.json"), encoding="utf-8"))
        except (OSError, ValueError):
            pkg = {}
        scripts = pkg.get("scripts") or {}
        deps = dict(pkg.get("dependencies") or {}, **(pkg.get("devDependencies") or {}))
        tech = "/".join(x for x, k in (("React", "react"), ("Vue", "vue"), ("Angular", "@angular/core"), ("Next.js", "next"),
                                       ("Vite", "vite"), ("TS", "typescript"), ("Express", "express"), ("NestJS", "@nestjs/core"))
                        if k in deps) or "Node.js"
        r.update(tech=tech, build=["npm run build"] if "build" in scripts else (["npm run lint"] if "lint" in scripts else []),
                 test=["npm test -- {tests}"] if "test" in scripts else [], test_files=r"\.(test|spec)\.[jt]sx?$",
                 link=["node_modules"])
    elif has("pom.xml") or sub:
        d = "{dir}/{subdir}/pom.xml" if sub else "{dir}/pom.xml"
        r.update(tech="Java/Maven", subdir=sub, build=["mvn -q -o -f %s compile -DskipTests" % d],
                 test=["mvn -q -o -f %s test -Dtest={tests} -Dsurefire.failIfNoSpecifiedTests=false" % d],
                 test_files=r"/src/test/.*Test\.java$", test_compare="surefire")
    elif has("build.gradle", "build.gradle.kts"):
        r.update(tech="Java/Gradle", build=["./gradlew -q compileJava"], test=["./gradlew -q test --tests {tests}"],
                 test_files=r"/src/test/.*Test\.(java|kt)$")
    elif has("pyproject.toml", "setup.py", "requirements.txt"):
        r.update(tech="Python", build=["python3 -m compileall -q {dir}"], test=["python3 -m pytest -q -k {tests}"],
                 test_files=r"(^|/)test_.*\.py$")
    elif has("go.mod"):
        r.update(tech="Go", build=["go build ./..."], test=["go test ./... -run {tests}"], test_files=r"_test\.go$")
    elif has("Cargo.toml"):
        r.update(tech="Rust", build=["cargo build -q"], test=["cargo test -q {tests}"], test_files=r"(^|/)tests/.*\.rs$")
    return r, g


ANALYZE_PROMPT = """Bạn phân tích một repo code để khai báo cho công cụ tự sửa bug (autofix). Chỉ ĐỌC (Read / Glob / Grep),
không sửa gì, không chạy lệnh. Repo: {path}

## Thông tin git
Nhánh hiện tại: {current} · nhánh mặc định origin: {default} · remote: {remote_url}
Nhánh trên origin: {branches}
Commit gần đây:
{recent}

## Đoán sơ bộ (theo file đặc trưng, có thể sai — kiểm tra lại)
{guess}

## Các repo đã khai báo (đặt tên nhánh theo cùng quy ước)
{existing}

## Làm
Đọc file cấu hình build (package.json / pom.xml / build.gradle / pyproject / go.mod / Makefile…), README / AGENTS.md /
CONTRIBUTING, cấu trúc thư mục. Xác định:
- tech: công nghệ chính ngắn gọn; subdir: thư mục module chính nếu build chạy trong thư mục con (vd Maven nhiều module).
- build: lệnh tool tự chạy để kiểm tra code compile / build được (nhanh, không cần mạng nếu được, KHÔNG deploy). test: lệnh
  chạy test, dùng {{tests}} = danh sách tên test (phân cách dấu phẩy) nếu chạy chọn lọc được; test_files: regex đường dẫn
  file test (khớp với "/" + đường dẫn tương đối). Dùng {{dir}} = thư mục repo, {{subdir}} = subdir trong lệnh nếu cần đường
  dẫn tuyệt đối. link: thư mục phụ thuộc cài sẵn nên dùng chung (vd node_modules; monorepo có node_modules từng package thì thêm glob
  "packages/*/node_modules"…), không có thì [].
- check: 1 câu hướng dẫn Claude tự kiểm tra (lệnh nguyên văn trong `…`). rules: quy ước code quan trọng (từ README /
  AGENTS.md / cấu hình lint), ngắn. describe: ghi chú cho Claude (bắt đầu bằng "; " hoặc ". "), vd đọc file hướng dẫn nào.
- upstream: nhánh chung để merge vào (nhánh tích hợp: dev / develop / dev-ss…); branch: nhánh cá nhân autofix push lên
  (theo quy ước các repo đã khai báo, có trên origin thì dùng đúng tên đó).
- ref_hint: AI dùng mã repo này để làm gì khi soạn task (vd "— ghi đúng API (method + path)"), bắt đầu bằng " — ".
- label: nhãn ngắn viết hoa (2–8 ký tự); key: chữ thường / số / - _ ; name: tên dài.

## Kết quả: chỉ một khối JSON
{{"key": "", "label": "", "name": "", "tech": "", "subdir": "", "branch": "", "upstream": "", "build": ["…"],
  "test": ["…"], "test_files": "", "link": [], "check": "", "rules": "", "describe": "", "ref_hint": "",
  "notes": "điểm chưa chắc / người dùng nên kiểm tra"}}
"""


def _claude_json(prompt, p, extra=(), budget="0.8"):
    """Gọi Claude chỉ đọc trong repo p (+ các repo extra) (Read / Glob / Grep, không Bash / ghi / mạng) → khối JSON."""
    import op_autofix as af
    import task_create
    _, acfg, _ = task_create.load()
    acfg = dict(af.AUTOFIX_DEFAULTS, **acfg)
    dirs = [p] + [x for x in extra if x and x != p]
    reads = ["Read(%s)" % ("//" + d.lstrip("/") + "/**") for d in dirs]
    cmd = [acfg["claude"], "-p", prompt, "--permission-mode", "dontAsk", "--max-budget-usd", budget,
           "--output-format", "json", "--add-dir"] + dirs + ["--allowedTools"] + reads + ["Glob", "Grep",
           "--disallowedTools", "Bash", "Write", "Edit", "WebFetch", "WebSearch",
           "--model", envconf.get("TG_AI_MODEL") or "sonnet"]
    res, _ = af.claude_oneshot(acfg, cmd, cwd=p, timeout=900 if float(budget) > 1 else 300)  # gói bị vô hiệu → tự dùng gói khác
    text = (json.loads(res.stdout).get("result") or "") if res.stdout.strip().startswith("{") else ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("Claude không trả JSON (mã %s)" % res.returncode)
    return json.loads(m.group(0))


DEPLOY_PROMPT = """Bạn viết LỆNH DEPLOY cho một repo, theo mô tả của người dùng. CHỈ ĐỌC repo (không sửa, không chạy lệnh).

## Repo
Thư mục: {path} · nhánh deploy mặc định: {ref} · công nghệ: {tech}
Lệnh build đã khai báo: {build}

## Server test (biến có sẵn khi lệnh chạy)
- {{host}} / $DEPLOY_HOST = {host} · {{user}} / $DEPLOY_USER = {user} · {{base}} / $DEPLOY_BASE = {base}
- $SSHPASS = mật khẩu SSH (dùng `sshpass -e ssh …`, `sshpass -e scp …`, `sshpass -e rsync -e ssh …`; KHÔNG ghi mật khẩu
  vào lệnh). Thêm `-o StrictHostKeyChecking=accept-new` cho ssh / scp.
- Cách repo FE / BE hiện có đang deploy (tham khảo quy ước server): image docker tải lên {base}/images, `docker load`,
  sửa docker-compose.yml trong {base} rồi `docker compose up -d <service>`.

## Mô tả của người dùng
{desc}

## Cách lệnh chạy
Các phần tử (mỗi phần tử một dòng) ghép thành MỘT script bash có `set -e`, chạy trong worktree MỚI của origin/<nhánh deploy> (thư mục hiện tại = {{dir}},
chưa cài phụ thuộc trừ thư mục dùng chung: {link}); lệnh lỗi (mã ≠ 0) thì dừng và báo deploy lỗi. Dùng được {{dir}} {{ref}}
{{sha}} (commit đang deploy) {{host}} {{user}} {{base}}. Đọc package.json / Dockerfile / docker-compose / Makefile / script
deploy / README trong repo để dùng đúng lệnh build, thư mục output, tên image / service. Lệnh phải chạy không tương tác,
idempotent nếu được. Không đoán được chi tiết nào thì vẫn viết lệnh hợp lý nhất và nêu rõ trong notes.

## Kết quả: chỉ một khối JSON
{{"deploy_cmd": ["…"], "deploy_ref": "", "deploy_health": "URL kiểm tra sau deploy hoặc trống", "notes": "giả định / điều cần kiểm tra lại"}}
"""


def suggest_deploy(repo, desc):
    """AI viết lệnh deploy (repo "command") từ mô tả. Trả {"deploy_cmd", "deploy_ref", "deploy_health", "notes"}."""
    desc = (desc or "").strip()
    if not desc:
        raise ValueError("nhập mô tả cách deploy trước")
    p = _inside_home((repo or {}).get("path") or "")
    if not os.path.isdir(p):
        raise ValueError("chưa có đường dẫn repo hợp lệ")
    lst = lambda v: [v] if isinstance(v, str) else list(v or [])  # noqa: E731
    d = envconf.deploy_config()
    prompt = DEPLOY_PROMPT.format(path=p, ref=repo.get("deploy_ref") or repo.get("upstream") or "?",
                                  tech=repo.get("tech") or "?", build="; ".join(lst(repo.get("build"))) or "(không có)",
                                  host=d.get("host") or "?", user=d.get("user") or "?", base=d.get("base") or "?",
                                  link=", ".join(lst(repo.get("link"))) or "(không có)", desc=desc[:3000])
    out = _claude_json(prompt, p)
    cmds = [str(c).strip() for c in lst(out.get("deploy_cmd")) if str(c).strip()]
    if not cmds:
        raise ValueError("AI không viết được lệnh: %s" % (out.get("notes") or "?"))
    secret = d.get("password") or ""
    if secret and any(secret in c for c in cmds):
        raise ValueError("AI ghi mật khẩu vào lệnh — bỏ kết quả, thử lại")
    return {"deploy_cmd": cmds, "deploy_ref": str(out.get("deploy_ref") or "").strip(),
            "deploy_health": str(out.get("deploy_health") or "").strip(), "notes": str(out.get("notes") or "")[:800]}


def analyze_repo(path, existing=()):
    """AI phân tích repo (chỉ đọc) → đề xuất khai báo. Trả {"repo": {...}, "source": "ai"|"đoán", "notes", "git"}."""
    p = _inside_home(path)
    if not os.path.isdir(os.path.join(p, ".git")) and not os.path.isfile(os.path.join(p, ".git")):
        raise ValueError("%s không phải repo git" % p)
    guess, g = guess_repo(p, existing)
    prompt = ANALYZE_PROMPT.format(path=p, current=g["current"] or "?", default=g["default"] or "?",
                                   remote_url=g["remote_url"] or "?", branches=", ".join(g["remote_branches"][:80]) or "(không có)",
                                   recent=g["recent"] or "(không có)", guess=json.dumps(guess, ensure_ascii=False),
                                   existing="\n".join("- %s: %s nhánh %s → %s" % (e.get("label") or e.get("key"), e.get("path"),
                                                                                  e.get("branch"), e.get("upstream"))
                                                      for e in existing) or "(chưa có)")
    try:
        out = _claude_json(prompt, p)
    except Exception as ex:  # noqa: BLE001 — AI lỗi vẫn trả kết quả đoán
        return {"repo": guess, "source": "đoán", "notes": "AI phân tích lỗi (%s) — dùng kết quả đoán theo file" % str(ex)[:150],
                "git": g}
    repo = dict(guess)
    for k in ("key", "label", "name", "tech", "subdir", "branch", "upstream", "build", "test", "test_files", "link",
              "check", "rules", "describe", "ref_hint"):
        v = out.get(k)
        if v not in (None, "", []):
            repo[k] = v
    repo["key"] = re.sub(r"[^a-z0-9_-]+", "-", str(repo.get("key") or guess["key"]).lower()).strip("-")[:30] or guess["key"]
    used = {e.get("key") for e in existing}
    if repo["key"] in used:
        repo["key"] = guess["key"]
    return {"repo": repo, "source": "ai", "notes": str(out.get("notes") or "")[:600], "git": g}


# ---------------------------------------------------------------- dự án (projects.py)

def _cfg_path():
    return envconf.CONFIG_PATH


def _cfg():
    """config.json như trên đĩa + biến môi trường (để dựng dự án cũ khi chưa khai báo)."""
    import argparse
    import op_watch
    try:
        return op_watch.load_config(argparse.Namespace(config=None, interval=None, on_change=None, no_notify=True))
    except SystemExit:
        return envconf._read_json(_cfg_path())


def _write_cfg(data):
    path = _cfg_path()
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak-" + time.strftime("%m%d-%H%M"))
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(path + ".tmp", 0o600)
    os.replace(path + ".tmp", path)


def _api():
    import op_watch
    cfg = _cfg()
    return op_watch.OpenProject(cfg["url"], cfg["api_key"])


def projects_get():
    cfg = _cfg()
    items = P.projects(cfg)
    out = []
    for p in items:
        q = P.public(p)
        try:
            q["repo_labels"] = [r["label"] for r in R.repos(P.scope_acfg(cfg.get("autofix") or {}, p))] \
                if (p["repos"] or p["_legacy"]) else []
        except ValueError:
            q["repo_labels"] = []
        out.append(q)
    return {"projects": out, "declared": bool(cfg.get("projects")), "deploy_fields": list(P.DEPLOY_FIELDS),
            "secret_deploy": list(P.SECRET_DEPLOY), "statuses": cfg.get("statuses") or {}}


def op_projects():
    """Dự án trên OpenProject mà API key đọc được (để chọn khi thêm dự án)."""
    data = _api().get("/api/v3/projects", {"pageSize": 500})
    return sorted(({"id": str(e["id"]), "identifier": e.get("identifier") or "", "name": e.get("name") or ""}
                   for e in data.get("_embedded", {}).get("elements", [])), key=lambda x: x["name"].lower())


def detect_project(op_project):
    """Tên + tiền tố mã ticket của một dự án OP (tiền tố học từ một work package của dự án)."""
    op_project = str(op_project or "").strip()
    if not op_project:
        raise ValueError("chọn dự án OpenProject")
    api = _api()
    pj = api.get("/api/v3/projects/%s" % op_project)
    prefix = ""
    try:
        d = api.get("/api/v3/projects/%s/work_packages" % pj["id"], {"pageSize": 1})
        el = (d.get("_embedded") or {}).get("elements") or []
        m = re.match(r"([A-Za-z][A-Za-z0-9_]*)-\d+$", (el[0].get("displayId") or "") if el else "")
        prefix = m.group(1).upper() if m else ""
    except Exception:  # noqa: BLE001 — chưa có work package: để người dùng tự nhập
        pass
    ident = pj.get("identifier") or ""
    return {"op_project": str(pj["id"]), "name": pj.get("name") or ident, "identifier": ident,
            "prefix": prefix or re.sub(r"[^A-Z0-9_]", "", ident.upper())[:20],
            "key": re.sub(r"[^a-z0-9_-]", "-", (prefix or ident).lower()).strip("-")[:30] or "p%s" % pj["id"],
            "prefix_from_wp": bool(prefix)}


def _materialize(data, cfg):
    """config.json chưa có "projects": dựng dự án đầu từ cấu hình cũ (repo trong autofix.repos chuyển vào dự án)."""
    if data.get("projects"):
        return data["projects"]
    p = P.legacy(cfg)
    p.pop("_legacy", None)
    p["repos"] = (data.get("autofix") or {}).get("repos") or []
    if not p["repos"]:  # repo FE / BE dựng từ khoá cũ (fe_repo …) → ghi thành preset để dự án tự đủ
        p["repos"] = [{"preset": r["key"], "key": r["key"], "path": r["path"], "branch": r["branch"],
                       "upstream": r["upstream"], "subdir": r.get("subdir") or "", "deploy": r["deploy"], "merge": True}
                      for r in R.legacy(cfg.get("autofix") or {})]
    return [p]


def projects_save(items):
    """Lưu danh sách dự án (không gồm repo — repo lưu riêng theo dự án). Trả (danh sách, cảnh báo)."""
    data = envconf._read_json(_cfg_path())
    cfg = _cfg()
    old = {p["key"]: p for p in _materialize(data, cfg)}
    out = []
    for x in items:
        if not isinstance(x, dict):
            continue
        y = {k: x.get(k) for k in P.FIELDS if k in x and k != "repos"}
        prev = old.get(str(x.get("_orig") or x.get("key") or "").strip().lower()) or {}
        y["repos"] = prev.get("repos") or []
        dep = {k: v for k, v in (x.get("deploy") or {}).items() if k in P.DEPLOY_FIELDS}
        for k in P.SECRET_DEPLOY:  # "••••••" = giữ mật khẩu cũ
            if str(dep.get(k) or "").startswith("•"):
                dep[k] = (prev.get("deploy") or {}).get(k, "")
        if isinstance(dep.get("auth"), dict):  # cách đăng nhập app; "••••••" = giữ token cũ
            au = {k: v for k, v in dep["auth"].items() if k in AUTH_FIELDS and v not in (None, "")}
            if str(au.get("token") or "").startswith("•"):
                au["token"] = ((prev.get("deploy") or {}).get("auth") or {}).get("token", "")
            if "form" in au:
                au["form"] = au["form"] in (True, "1", "true", "on")
            if au.get("kind") not in AUTH_KINDS:
                au = {}
            for k in ("login_body", "otp_body"):
                if au.get(k):
                    try:
                        json.loads(au[k].replace("{user}", "u").replace("{password}", "p").replace("{otp}", "1"))
                    except ValueError:
                        raise ValueError("%s: mẫu %s không phải JSON hợp lệ" % (x.get("name") or x.get("key"), k))
            dep["auth"] = au
        for k in ("qa_user_id",):
            if dep.get(k) not in (None, ""):
                try:
                    dep[k] = int(dep[k])
                except (TypeError, ValueError):
                    raise ValueError("%s: qa_user_id phải là số" % y.get("key"))
        for k in ("qa_review_status_ids", "qa_ticket_accounts"):
            if isinstance(dep.get(k), str):
                dep[k] = [s_.strip() for s_ in dep[k].split(",") if s_.strip()]
        y["deploy"] = {k: v for k, v in dep.items() if v not in (None, "", [])}
        if "statuses" in y and isinstance(y["statuses"], str):
            y["statuses"] = envconf._status_map(y["statuses"])
        if prev.get("deploy_inherit") and "deploy_inherit" not in x:
            y["deploy_inherit"] = True
        out.append(y)
    norm = P.validate(out)
    warn = []
    for p in norm:
        if not p["repos"]:
            warn.append("%s: chưa có repo — autofix bỏ qua ticket của dự án này tới khi thêm repo" % p["name"])
        if p["qa"] and not (p["deploy_inherit"] or (p["deploy"].get("fe_url") and p["deploy"].get("api_url"))):
            warn.append("%s: bật QA tự động nhưng chưa có fe_url / api_url" % p["name"])
    data["projects"] = out
    if (data.get("autofix") or {}).get("repos"):
        data["autofix"]["repos_before_projects"] = data["autofix"].pop("repos")  # đã chuyển vào dự án
    _write_cfg(data)
    return out, warn


def save_project_repos(key, repos):
    data = envconf._read_json(_cfg_path())
    items = _materialize(data, _cfg())
    proj = next((p for p in items if p.get("key") == key), None)
    if not proj:
        raise ValueError("không có dự án %s" % key)
    proj["repos"] = repos
    P.validate(items)  # mã repo không trùng giữa các dự án
    data["projects"] = items
    if (data.get("autofix") or {}).get("repos"):
        data["autofix"]["repos_before_projects"] = data["autofix"].pop("repos")
    _write_cfg(data)


# ---------------------------------------------------------------- cách đăng nhập app của dự án (soar_api / ui_check)

AUTH_KINDS = ("soar", "json", "static", "none")
AUTH_FIELDS = ("kind", "login_path", "login_body", "otp_path", "otp_body", "token_path", "header", "scheme", "token",
               "form", "check_path", "ui_login_path", "ui_user", "ui_password", "ui_submit")

AUTH_PROMPT = """Bạn đọc source code của một dự án để ĐIỀN CẤU HÌNH cho công cụ tự sửa bug + test tự động (autofix):
bối cảnh dự án cho AI sửa bug, cách đăng nhập app (gọi API + mở giao diện), đường dẫn kiểm tra server.
CHỈ ĐỌC code (Read / Glob / Grep), không sửa, không chạy lệnh. Đọc README / docs / cấu trúc thư mục / route / controller
/ menu / i18n trước, đừng đọc tràn lan.

Dự án: {name} (mã ticket {prefix}-…). Các repo:
{repos}

## 1. Bối cảnh dự án ("context", tiếng Việt, tối đa ~1500 ký tự, gạch đầu dòng)
Ứng dụng làm gì; các phân hệ / màn hình chính (tên menu như người dùng thấy) và repo / thư mục code tương ứng; thuật
ngữ nghiệp vụ hay gặp; vai trò người dùng / phân quyền; quy ước quan trọng khi sửa (i18n, API chung, migration…). Viết
để một AI khác đọc ticket lỗi là biết tìm code ở đâu.

## 2. Đăng nhập + kiểm tra server
Tìm trong code backend (controller / route đăng nhập, cấu hình security / JWT / session) và frontend (trang login, chỗ
gọi API đăng nhập, header gửi token):
- API đăng nhập: method + đường dẫn (so với API URL gốc: {api_url}), body gửi lên (tên trường username / email,
  password), có bước OTP / 2FA không (đường dẫn + body), token trả về nằm ở đâu trong JSON (vd data.access_token), hay
  server đặt cookie phiên; header gửi token (Authorization: Bearer … / X-Auth-Token / …); body dạng JSON hay form.
- Giao diện đăng nhập: đường dẫn trang login trên FE, selector ô tài khoản, ô mật khẩu, nút đăng nhập (CSS selector ổn
  định: name / type / id / data-testid).
- Một API GET nhẹ cần đăng nhập để thử token (vd /api/users/me) → check_path.
- Endpoint kiểm tra server sống không cần đăng nhập (health / ping / actuator) → ping_path.

Kết quả: một khối JSON DUY NHẤT (không giải thích ngoài JSON):
{{"context": "- ...\\n- ...", "ping_path": "/api/health",
  "kind": "json" | "soar" | "static" | "none",
  "login_path": "/api/...", "login_body": "{{\\"username\\": \\"{{user}}\\", \\"password\\": \\"{{password}}\\"}}",
  "otp_path": "" , "otp_body": "", "token_path": "data.access_token", "header": "Authorization", "scheme": "Bearer",
  "form": false, "check_path": "/api/...",
  "ui_login_path": "/login", "ui_user": "input[name=username]", "ui_password": "input[type=password]",
  "ui_submit": "button[type=submit]",
  "notes": "căn cứ (file:dòng), điểm chưa chắc"}}
login_body / otp_body là CHUỖI mẫu JSON, dùng {{user}} {{password}} {{otp}} làm chỗ điền. "soar" chỉ khi đúng luồng
POST /api/v2/auth/login rồi /api/v2/auth/validate-otp. Không chắc trường nào thì để "" và ghi trong notes.
"""


def _project_and_repos(key):
    cfg = _cfg()
    proj = P.get(cfg, key or "")
    if not proj:
        raise ValueError("không có dự án %s (lưu dự án trước)" % key)
    rs = R.repos(P.scope_acfg(cfg.get("autofix") or {}, proj)) if (proj["repos"] or proj["_legacy"]) else []
    return proj, [r for r in rs if os.path.isdir(r["path"])]


def suggest_project(key):
    """AI đọc source các repo của dự án → bối cảnh dự án + cách đăng nhập + ping_path (chưa lưu — trang hiện để người
    dùng xem lại rồi Lưu dự án)."""
    proj, rs = _project_and_repos(key)
    if not rs:
        raise ValueError("dự án %s chưa có repo — thêm repo trước để AI đọc source" % proj["name"])
    d = envconf.deploy_config(proj["key"])
    repos = "\n".join("- %s (%s): %s" % (r["label"], r.get("tech") or "?", r["path"]) for r in rs)
    out = _claude_json(AUTH_PROMPT.format(name=proj["name"], prefix=proj["prefix"], repos=repos,
                                          api_url=d.get("api_url") or "(chưa đặt)"),
                       rs[0]["path"], [r["path"] for r in rs[1:]], budget="2")
    auth = {k: out[k] for k in AUTH_FIELDS if k in out and out[k] not in (None, "") and k != "token"}
    if auth.get("kind") not in AUTH_KINDS:
        auth["kind"] = "json"
    for k in ("login_body", "otp_body"):
        if isinstance(auth.get(k), dict):
            auth[k] = json.dumps(auth[k], ensure_ascii=False)
    ctx = out.get("context")
    ctx = "\n".join(ctx) if isinstance(ctx, list) else str(ctx or "")
    return {"auth": auth, "context": ctx.strip()[:3000], "ping_path": str(out.get("ping_path") or "").strip(),
            "notes": str(out.get("notes") or "")[:1000]}


def test_auth(key):
    """Thử đăng nhập bằng cấu hình đã LƯU của dự án: lấy token, gọi check_path nếu có. Trả mô tả."""
    import soar_api
    proj = P.get(_cfg(), key or "")
    if not proj:
        raise ValueError("không có dự án %s" % key)
    cfg = envconf.deploy_config(proj["key"])
    if not cfg.get("api_url"):
        raise ValueError("%s: chưa có URL API (Server test)" % proj["name"])
    a = soar_api.auth_cfg(cfg)
    try:
        if a["kind"] in ("static", "none"):
            user, token = "", soar_api.login(cfg, "", "")
        else:
            user, pw = soar_api.credentials(cfg, None)
            token = soar_api.login(cfg, user, pw)
    except SystemExit as ex:
        raise ValueError(str(ex))
    msg = "Đăng nhập %s được (%s)" % (("bằng " + user) if user else "", "cookie phiên" if token.startswith("cookie:")
                                    else ("token …%s" % token[-6:]) if token else "không cần token")
    if a.get("check_path"):
        code, _, body = soar_api.send("GET", cfg["api_url"].rstrip("/") + "/" + a["check_path"].lstrip("/"), None,
                                      soar_api.auth_headers(cfg, token))
        msg += " · GET %s → HTTP %s%s" % (a["check_path"], code, "" if code < 400 else " " + body[:150].decode("utf-8", "replace"))
    return msg
