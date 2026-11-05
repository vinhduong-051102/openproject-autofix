"""
config_page: trang Cấu hình trên trang theo dõi — sửa .env + danh sách repo mà không cần mở file.

- Biến cấu hình lấy TỰ ĐỘNG từ .env.example: nhóm theo dòng tiêu đề (# ===== X ===== / # ---- X ----), ghi chú ngay
  trên mỗi KEY= là hướng dẫn. Thêm biến mới vào .env.example là trang tự hiện — không phải sửa giao diện.
- Ghi vào .env giữ nguyên các dòng / ghi chú khác, sao lưu .env trước mỗi lần lưu. Biến bí mật không trả về trang.
- Danh sách repo (repos.py) ghi vào config.json → autofix.repos.
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
    if raw:
        items = [{k: v for k, v in x.items() if k in REPO_FIELDS} for x in raw]
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
            if k in ("build", "test", "link", "allow", "merge_check", "deploy_cmd", "setup"):
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


def http_get(path, acfg):
    if path == "/api/config":
        return schema()
    if path == "/api/config/repos":
        return repos_get(acfg)
    return None


def http_get_q(path, q, acfg):
    if path == "/api/config/browse":
        out = browse(q.get("path") or "~")
        if q.get("repos") == "1":
            out["git_repos"] = [x.replace(HOME, "~", 1) for x in find_git_repos()]
        return out
    return http_get(path, acfg)


def action(body, save_autofix):
    kind = body.get("action")
    if kind == "config_env":
        done = save_env(body.get("changes") or {})
        return {"message": ("Đã lưu %d biến vào .env: %s — bấm Áp dụng để dùng" % (len(done), ", ".join(done)))
                if done else "Không có gì thay đổi", "changed": done}
    if kind == "config_repos":
        clean, warn = repos_check(body.get("repos"))
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


def _claude_json(prompt, p):
    """Gọi Claude chỉ đọc trong repo p (Read / Glob / Grep, không Bash / ghi / mạng) → khối JSON trong kết quả."""
    import op_autofix as af
    import task_create
    _, acfg, _ = task_create.load()
    acfg = dict(af.AUTOFIX_DEFAULTS, **acfg)
    prof, why = af.choose_profile(os.path.expanduser(acfg["root"]), acfg)
    if prof is None:
        raise RuntimeError("không có gói Claude dùng được (%s)" % why)
    af.use_profile(acfg, prof)
    scope = "//" + p.lstrip("/") + "/**"
    cmd = [acfg["claude"], "-p", prompt, "--permission-mode", "dontAsk", "--max-budget-usd", "0.8",
           "--output-format", "json", "--add-dir", p, "--allowedTools", "Read(%s)" % scope, "Glob", "Grep",
           "--disallowedTools", "Bash", "Write", "Edit", "WebFetch", "WebSearch",
           "--model", envconf.get("TG_AI_MODEL") or "sonnet"]
    res = subprocess.run(cmd, cwd=p, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         universal_newlines=True, env=af.build_env(acfg), timeout=300)
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
Mỗi phần tử là một lệnh `bash -c`, chạy lần lượt trong worktree MỚI của origin/<nhánh deploy> (thư mục hiện tại = {{dir}},
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
