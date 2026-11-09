"""
repos: danh sách repo mà tool làm việc (autofix / merge / deploy / QA / tạo task) — cấu hình động.

Khai báo trong config.json → "autofix": {"repos": [...]} (trang theo dõi → Cấu hình). Không khai báo thì dựng 2 repo FE / BE
từ các khoá cũ (fe_repo / fe_branch / fe_upstream / be_repo / be_branch / be_upstream / be_subdir) — hành vi như trước.

Mỗi repo:
  key        "fe" — tên thư mục worktree trong lượt chạy (<run>/<key>), chữ thường / số / - _
  label      "FE" — nhãn hiện trong log / báo cáo (mặc định KEY viết hoa)
  name       tên dài (tuỳ chọn)
  path       đường dẫn repo trên máy
  branch     nhánh cá nhân autofix push lên        upstream  nhánh chung (merge vào, PR)
  subdir     thư mục module chính trong repo (vd "SOAR"), có thể trống
  tech       công nghệ, ghi trong prompt ("React/Vite/TS")
  describe   ghi chú thêm sau "— nhánh …" trong prompt (dùng {dir} {subdir})
  rules      quy ước code khi sửa (prompt)
  check      cách Claude tự kiểm tra (prompt, dùng {dir} {subdir})
  build      [lệnh] tool tự chạy để kiểm tra (luôn chạy)        test  [lệnh] chạy khi bản sửa có file test ({tests})
  test_files regex đường dẫn file test trong diff → tên test = tên file bỏ đuôi
  test_compare "surefire": test lỗi thì so với commit trước bản sửa (lỗi sẵn trên nhánh chung không chặn push)
  link       [thư mục] dùng chung từ repo gốc vào worktree (vd node_modules; glob được: packages/*/node_modules)
  setup      [lệnh] chạy MỘT lần trong worktree mới trước khi build (vd "bun install --frozen-lockfile") — dùng khi
             không dùng chung được thư mục phụ thuộc (monorepo: gói nội bộ phải trỏ vào chính worktree)
  build_compare "errors": build / test lỗi thì chạy lại trên commit trước bản sửa, chỉ chặn nếu có dòng lỗi MỚI
             (lỗi có sẵn trên nhánh chung không chặn push)
  allow      [quy tắc Bash] thêm cho Claude (mặc định lấy theo lệnh build / test)
  merge      true: merge_to_dev đưa nhánh cá nhân vào nhánh chung           merge_check  [lệnh] build thử sau merge
  deploy     "fe-bundle" | "be-image" | "command" | "none" — cách deploy lên server test (deploy_qa)
  deploy_cmd    [lệnh] (deploy "command") chạy trong worktree mới của origin/<deploy_ref>, theo thứ tự; dùng {dir} {ref}
                {sha} {host} {user} {base}; biến môi trường DEPLOY_HOST / DEPLOY_USER / DEPLOY_BASE / SSHPASS có sẵn
  deploy_ref    nhánh đem deploy (mặc định = upstream)
  deploy_health URL kiểm tra sau deploy (đợi trả 2xx), trống = không kiểm tra
Trong lệnh dùng được {dir} (thư mục worktree / repo), {subdir}, {tests} (danh sách test, phân cách dấu phẩy), {key}.
"""
import getpass
import os
import re
import shlex

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
DEPLOY_KINDS = ("fe-bundle", "be-image", "command", "none")

FE_DEFAULT = {
    "key": "fe", "label": "FE", "name": "Frontend", "tech": "React/Vite/TS",
    "describe": ". Đọc {dir}/AGENTS.md và docs/ui-v2/README.md nếu sửa giao diện.",
    "rules": "chỉ dùng token --t-*, sửa primitive dùng chung trước",
    "check": "`npm run build` (+ `npx tsc --noEmit -p tsconfig.app.json` lọc theo file đã sửa, chỉ lỗi mới mới tính)",
    "check_short": "`npm run build`",
    "build": ["npm run build"], "test": [], "test_files": "", "test_compare": "",
    "link": ["node_modules"],
    "allow": ["Bash(npm run build*)", "Bash(npm run lint*)", "Bash(npx tsc*)", "Bash(npx eslint*)"],
    "merge": True, "merge_check": ["npm run build"], "deploy": "fe-bundle",
    "ref_hint": ' (route / menu / nhãn i18n trong src/) — dùng để ghi ĐÚNG đường dẫn màn hình, tên menu, tên tab, nhãn',
}
BE_DEFAULT = {
    "key": "be", "label": "BE", "name": "Backend", "tech": "Spring Boot/Java",
    "describe": "; module chính ở {dir}/{subdir}.",
    "rules": "thêm/sửa unit test khi sửa logic",
    "check": "`mvn -q -o -f {dir}/{subdir}/pom.xml test -Dtest='<Test liên quan>' -Dsurefire.failIfNoSpecifiedTests=false`\n"
             "   rồi `mvn -q -o -f {dir}/{subdir}/pom.xml compile -DskipTests`",
    "check_short": "`mvn -q -o -f {dir}/{subdir}/pom.xml test -Dtest='<Test>' -Dsurefire.failIfNoSpecifiedTests=false`"
                   "\n   và `mvn -q -o -f {dir}/{subdir}/pom.xml compile -DskipTests`",
    "build": ["mvn -q -o -f {dir}/{subdir}/pom.xml compile -DskipTests"],
    "test": ["mvn -q -o -f {dir}/{subdir}/pom.xml test -Dtest={tests} -Dsurefire.failIfNoSpecifiedTests=false"],
    "test_files": r"/src/test/.*Test\.java$", "test_compare": "surefire",
    "link": [], "allow": ["Bash(mvn *)"],
    "merge": True, "merge_check": ["mvn -q -o -f {dir}/{subdir}/pom.xml compile -DskipTests"], "deploy": "be-image",
    "ref_hint": ' — dùng để ghi đúng API (method + path) nếu ticket liên quan API',
}
GENERIC_DEFAULT = {
    "label": "", "name": "", "tech": "", "describe": "", "rules": "", "check": "", "check_short": "",
    "subdir": "", "build": [], "test": [], "test_files": "", "test_compare": "", "link": [], "allow": [],
    "merge": True, "merge_check": None, "deploy": "none", "ref_hint": "",
    "deploy_cmd": [], "deploy_ref": "", "deploy_health": "", "setup": [], "build_compare": "errors",
}


def _norm(raw, base):
    r = dict(base)
    r.update({k: v for k, v in (raw or {}).items() if v is not None})
    r["key"] = str(r.get("key") or "").strip().lower()
    if not KEY_RE.match(r["key"]):
        raise ValueError("repo key không hợp lệ: %r (chữ thường, số, - _)" % r["key"])
    r["label"] = str(r.get("label") or r["key"].upper()).strip()
    r["path"] = os.path.expanduser(str(r.get("path") or "").strip())
    for k in ("build", "test", "link", "allow", "deploy_cmd", "setup"):
        v = r.get(k) or []
        r[k] = [v] if isinstance(v, str) else [str(x) for x in v if str(x).strip()]
    if r.get("merge_check") is None:
        r["merge_check"] = list(r["build"])
    elif isinstance(r["merge_check"], str):
        r["merge_check"] = [r["merge_check"]]
    if r.get("deploy") not in DEPLOY_KINDS:
        r["deploy"] = "none"
    r["deploy_ref"] = str(r.get("deploy_ref") or r.get("upstream") or "").strip()
    r["deploy_health"] = str(r.get("deploy_health") or "").strip()
    r["merge"] = bool(r.get("merge"))
    if not r.get("check"):
        r["check"] = " rồi ".join("`%s`" % c for c in r["build"] + r["test"]) or "(không có lệnh kiểm tra)"
    if not r.get("check_short"):
        r["check_short"] = r["check"]
    if not r["allow"]:
        r["allow"] = _allow_rules(r["build"] + r["test"] + r["merge_check"])
    return r


def legacy(acfg):
    user = getpass.getuser()
    fe = dict(FE_DEFAULT, path=acfg.get("fe_repo") or "~/soar-fe",
              branch=acfg.get("fe_branch") or "dev-ss-%s" % user, upstream=acfg.get("fe_upstream") or "dev-ss", subdir="")
    be = dict(BE_DEFAULT, path=acfg.get("be_repo") or "~/SOAR_BE",
              branch=acfg.get("be_branch") or "develop-ss-%s" % user, upstream=acfg.get("be_upstream") or "develop-ss",
              subdir=acfg.get("be_subdir") or "SOAR")
    return [_norm(fe, FE_DEFAULT), _norm(be, BE_DEFAULT)]


def repos(acfg):
    """Danh sách repo đã chuẩn hoá (thứ tự = thứ tự khai báo; repo đầu là thư mục làm việc của Claude)."""
    raw = acfg.get("repos")
    if not raw:
        proj = acfg.get("_project")
        if proj and not proj.get("_legacy"):  # dự án khai báo mà chưa có repo: KHÔNG lấy FE / BE mặc định (repo SOAR)
            return []
        return legacy(acfg)
    out, seen = [], set()
    for x in raw:
        base = FE_DEFAULT if x.get("preset") == "fe" else BE_DEFAULT if x.get("preset") == "be" else GENERIC_DEFAULT
        r = _norm(x, base)
        if r["key"] in seen:
            raise ValueError("repo key trùng: %s" % r["key"])
        seen.add(r["key"])
        out.append(r)
    return out


def find(acfg, label_or_key):
    for r in repos(acfg):
        if label_or_key in (r["label"], r["key"]):
            return r
    return None


def trees(acfg, run_dir):
    """[(repo, thư mục worktree của lượt chạy)]."""
    return [(r, os.path.join(run_dir, r["key"])) for r in repos(acfg)]


def fill(tpl, repo, d, tests=""):
    return (tpl or "").replace("{dir}", d).replace("{subdir}", repo.get("subdir") or "").replace(
        "{tests}", tests).replace("{key}", repo["key"])


SHELL_RX = re.compile(r"&&|\|\||[|;<>$`*?]|^\s*(cd|export|source|\.)\s|(^|\s)[A-Za-z_][A-Za-z0-9_]*=")


def argv(cmd):
    """Một lệnh → argv: có cú pháp shell (cd … &&, |, ;, $VAR, glob, VAR=…) thì chạy qua bash -c, không thì tách từ."""
    return ["bash", "-c", cmd] if SHELL_RX.search(cmd) else shlex.split(cmd)


def commands(repo, which, d, tests=""):
    """Lệnh (argv) build / test / merge_check của repo, đã điền {dir} {subdir} {tests}."""
    return [argv(fill(c, repo, d, tests)) for c in repo.get(which) or [] if c.strip()]


def _allow_rules(cmds):
    """Quy tắc Bash(...) cho Claude theo các lệnh: mỗi đoạn (tách && ; |) lấy 2 từ đầu, bỏ đoạn cd / export."""
    out = set()
    for c in cmds:
        for seg in re.split(r"&&|\|\||;|\|", c):
            w = shlex.split(seg, posix=True) if seg.strip() else []
            while w and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0]):
                w = w[1:]
            if w and w[0] not in ("cd", "export", "source", "."):
                out.add("Bash(%s*)" % " ".join(w[:2]))
    return sorted(out)


def link_shared(repo, d, src_root=None):
    """Symlink các thư mục dùng chung (link) từ repo gốc vào worktree d. Hỗ trợ glob, vd "packages/*/node_modules"
    (monorepo bun / pnpm: mỗi package có node_modules riêng, symlink tương đối về node_modules gốc)."""
    import glob
    root = src_root or repo["path"]
    made = []
    for pat in repo.get("link") or []:
        for src in sorted(glob.glob(os.path.join(root, pat))) if any(c in pat for c in "*?[") else [os.path.join(root, pat)]:
            dst = os.path.join(d, os.path.relpath(src, root))
            if os.path.isdir(src) and not os.path.lexists(dst) and os.path.isdir(os.path.dirname(dst)):
                os.symlink(src, dst)
                made.append(dst)
    return made


def run_setup(repo, d, env=None, log_path=None):
    """Lệnh setup của repo trong worktree d, mỗi worktree một lần (đánh dấu .op-setup-done trong .git riêng của
    worktree). Trả "" nếu xong / không có, chuỗi lỗi nếu hỏng."""
    import subprocess
    cmds = commands(repo, "setup", d)
    if not cmds:
        return ""
    try:
        gd = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=d, stdout=subprocess.PIPE, universal_newlines=True,
                            stderr=subprocess.DEVNULL).stdout.strip()
        mark = os.path.join(d, gd, "op-setup-done") if gd else None
    except OSError:
        mark = None
    if mark and os.path.exists(mark):
        return ""
    out = open(log_path, "a", encoding="utf-8") if log_path else None
    try:
        for c in cmds:
            if out:
                out.write("\n$ %s   # setup\n" % " ".join(c))
                out.flush()
            res = subprocess.run(c, cwd=d, stdout=out or subprocess.DEVNULL, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, env=env, timeout=1800)
            if res.returncode != 0:
                return "%s: setup `%s` lỗi (mã %s)" % (repo["label"], " ".join(c)[-80:], res.returncode)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return "%s: setup lỗi: %s" % (repo["label"], ex)
    finally:
        if out:
            out.close()
    if mark:
        open(mark, "w").close()
    return ""


def module_dir(repo, d):
    return os.path.join(d, repo["subdir"]) if repo.get("subdir") else d


# ---- đoạn văn trong prompt ------------------------------------------------------------------------------------

def prompt_code(acfg, run_dir, branch):
    lines = []
    for r, d in trees(acfg, run_dir):
        tech = " (%s)" % r["tech"] if r.get("tech") else ""
        lines.append("- %s%s: %s — nhánh %s%s" % (r["label"], tech, d, branch, fill(r.get("describe") or "", r, d)))
    return "\n".join(lines)


def prompt_rules(acfg):
    parts = ["%s: %s" % (r["label"], r["rules"]) for r in repos(acfg) if r.get("rules")]
    return " (" + "; ".join(parts) + ")" if parts else ""


def prompt_checks(acfg, run_dir, short=False):
    key = "check_short" if short else "check"
    # {tests} trong lệnh test: chỗ Claude tự điền tên test liên quan
    return (";\n   " if not short else "; ").join("%s %s" % (r["label"], fill(r[key], r, d, "<test liên quan>"))
                                                  for r, d in trees(acfg, run_dir))


def prompt_worktrees(acfg, run_dir):
    out = []
    for r, d in trees(acfg, run_dir):
        out.append("%s %s%s" % (r["label"], d, " (module %s)" % os.path.join(d, r["subdir"]) if r.get("subdir") else ""))
    return ", ".join(out)


def prompt_git_dirs(acfg, run_dir):
    return " / ".join("`git -C %s …`" % d for _, d in trees(acfg, run_dir))


def prompt_cwd(acfg, run_dir):
    r, d = trees(acfg, run_dir)[0]
    cmd = r["build"][0] if r["build"] else ""
    return "Thư mục làm việc hiện tại ĐÃ LÀ %s%s" % (
        d, ": chạy %s đúng nguyên văn `%s` (không `--prefix`, không `cd`)." % (r["label"], fill(cmd, r, d)) if cmd else ".")


def prompt_kwargs(acfg, run_dir, branch=""):
    rs = repos(acfg)
    return dict(code_repos=prompt_code(acfg, run_dir, branch), n_repos=len(rs), repo_rules=prompt_rules(acfg),
                repo_checks=prompt_checks(acfg, run_dir), repo_checks_short=prompt_checks(acfg, run_dir, short=True),
                worktrees=prompt_worktrees(acfg, run_dir), git_dirs=prompt_git_dirs(acfg, run_dir),
                cwd_note=prompt_cwd(acfg, run_dir))


def prompt_refs(acfg):
    """Nguồn tham khảo (chỉ đọc) khi AI soạn task: mã nguồn của mọi repo."""
    return "\n".join("- Mã %s: %s%s" % (r["label"], r["path"], r.get("ref_hint") or "") for r in repos(acfg))


def allowed_bash(acfg):
    out = []
    for r in repos(acfg):
        for a in r["allow"]:
            if a not in out:
                out.append(a)
    return out
