"""
pick_resolve: tự gỡ xung đột khi gắp (cherry-pick) commit sang nhánh khác, rồi kiểm tra lại kết quả.

1. gỡ:  mỗi commit dừng vì xung đột → file .properties / db/master*.xml gộp tự động (giữ dòng cả hai bên); còn lại
        Claude headless gỡ theo ý đồ của ĐÚNG commit đang gắp (không kéo tính năng khác của nhánh nguồn, không dùng
        biến / prop / hàm mà nhánh đích không có) → git add → cherry-pick --continue, lặp tới hết.
2. kiểm tra (chỉ khi có xung đột đã tự gỡ):
   - độ phủ: mỗi dòng các commit gốc thêm vào phải có trong file kết quả (thiếu = mất chức năng); dòng kết quả thêm
     mà commit gốc không có = kéo thừa từ nhánh khác.
   - build: lệnh merge_check của repo + (FE) `tsc --noEmit` lọc theo file đã đổi; so với nhánh đích trước khi gắp,
     chỉ lỗi MỚI mới tính.
   - Claude review chỉ đọc: đối chiếu diff kết quả với commit gốc → `KẾT QUẢ: ĐẠT` | `KẾT QUẢ: CẦN XEM`.
   Không đạt → không push, giữ worktree + báo cáo review.md.
"""
import os
import re
import shutil
import subprocess

import op_autofix as af
import repos as R

PICK_CONFLICT_PROMPT = """Bạn đang gỡ xung đột khi GẮP (cherry-pick) một commit sang nhánh khác, chạy nền không có người theo dõi.
Worktree: {work} — HEAD là nhánh đích `{branch}` (đã gắp xong các commit trước), đang dừng ở commit {commit}
của ticket {ticket}: "{subject}".
File còn xung đột: {files}

Mục tiêu: nhánh đích có ĐÚNG và ĐỦ thay đổi của commit {commit} — không hơn, không kém.
1. Xem commit gốc: `git -C {work} show {commit}` (đây là thứ cần mang sang) và nhánh đích:
   `git -C {work} log --oneline -8 HEAD -- <file>`, `git -C {work} diff`, `git -C {work} show HEAD:<file>`.
   Nhánh đích thường CŨ hơn nhánh nguồn: đoạn "theirs" trong dấu xung đột hay kèm code của tính năng KHÁC (ngữ cảnh
   của commit gốc) mà nhánh đích chưa có.
2. Với từng khối <<<<<<< / ======= / >>>>>>>: giữ code của nhánh đích (HEAD), chỉ thêm / bớt đúng các dòng mà commit
   {commit} thêm / bớt (dòng +/- trong `git show {commit}`). KHÔNG mang thêm dòng ngữ cảnh của nhánh nguồn
   (import, prop, state, điều kiện quyền, tab… của tính năng khác).
3. Mọi tên mà phần đã gộp dùng tới (biến, tham số, prop, hàm, import, khoá i18n, hằng) phải tồn tại trên nhánh đích —
   tìm bằng Grep trong {work}. Không có → viết lại cho hợp nhánh đích nhưng giữ đúng hành vi của commit, không tự
   thêm tính năng khác để có tên đó.
4. Thẻ / ngoặc phải cân: khi bỏ một khối điều kiện của nhánh nguồn (vd `{{cond && (` … `)}}`), giữ lại các thẻ đóng mà
   commit gốc thêm (vd `</div>` bọc mới).
5. Xoá hết dấu xung đột. Chỉ sửa các file xung đột ở trên. Không git add / commit / cherry-pick / checkout / reset.
6. Thay đổi của commit không còn chỗ áp dụng / mâu thuẫn ý đồ với nhánh đích → KHÔNG đoán: để nguyên file đó.

Ghi {report} bằng tiếng Việt. Dòng đầu đúng một trong:
`KẾT QUẢ: ĐÃ GỠ` | `KẾT QUẢ: KHÔNG GỠ ĐƯỢC`
Sau đó: từng file → commit gốc đổi gì → nhánh đích khác gì → đã gộp thế nào; liệt kê dòng ngữ cảnh nhánh nguồn đã CỐ
Ý bỏ (và vì sao).
"""

PICK_REVIEW_PROMPT = """Bạn kiểm tra lại kết quả GẮP commit của ticket {ticket} sang nhánh `{branch}` (chỉ đọc, không sửa file).
Worktree: {work}. Nhánh đích trước khi gắp: {base}. Commit gốc đã gắp: {commits}.
Trong lúc gắp có xung đột đã được tự gỡ ở: {files}.

So sánh:
- Thay đổi gốc: `git -C {work} show <commit>` cho từng commit gốc.
- Kết quả trên nhánh đích: `git -C {work} diff {base} HEAD`.
- Kết quả kiểm tra tự động (độ phủ dòng, build / kiểm tra kiểu so với nhánh đích):
{checks}

Trả lời 2 câu hỏi, có dẫn chứng file:dòng:
1. THIẾU: chức năng / thay đổi nào của commit gốc không có (hoặc chỉ có một phần) trên nhánh đích?
2. HỎNG: phần gộp có làm hỏng gì không — tên không tồn tại trên nhánh đích, thẻ / ngoặc lệch, logic nhánh đích bị
   mất hoặc đổi, code của tính năng khác bị kéo sang (dòng không có trong commit gốc)?
Dòng "thiếu" do nhánh đích đã có sẵn nội dung tương đương, hoặc "thêm" chỉ là đổi thụt lề / viết lại cho hợp nhánh
đích mà giữ đúng hành vi → không tính là lỗi (giải thích ngắn).

Ghi {report} bằng tiếng Việt. Dòng đầu đúng một trong:
`KẾT QUẢ: ĐẠT` | `KẾT QUẢ: CẦN XEM`
Sau đó mục THIẾU, HỎNG (ghi "không có" nếu không có), rồi tóm tắt.
"""

_TRIVIAL = re.compile(r"^[\s{}()\[\];,)]*$|^\s*(//|#|/\*|\*)")


def _git(cwd, *args, check=False, timeout=600):
    res = subprocess.run(["git"] + list(args), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True, timeout=timeout, errors="replace")
    if check and res.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), res.stdout.strip()[-600:]))
    return res.stdout


def unmerged(wt):
    return _git(wt, "diff", "--name-only", "--diff-filter=U").split()


def in_progress(wt):
    p = _git(wt, "rev-parse", "--git-path", "CHERRY_PICK_HEAD").strip()
    return os.path.exists(p if os.path.isabs(p) else os.path.join(wt, p))


def _pick_head(wt):
    return _git(wt, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD").strip()


def _claude(acfg, prompt, wt, report_dir, edit, tag, log):
    """Chạy Claude headless trong worktree (edit=True: được sửa file trong worktree; False: chỉ đọc). Trả dòng đầu report."""
    report = os.path.join(report_dir, tag + ".md")
    wscope, rscope = "/" + os.path.abspath(wt) + "/**", "/" + os.path.abspath(report_dir) + "/**"
    allowed = ["Read", "Glob", "Grep", "Edit(%s)" % rscope, "Write(%s)" % rscope] + [
        "Bash(git -C %s %s*)" % (wt, c) for c in ("log", "show", "diff", "status", "blame", "grep")]
    if edit:
        allowed.append("Edit(%s)" % wscope)
    cmd = [acfg["claude"], "-p", prompt.replace("{report}", report), "--permission-mode", "dontAsk",
           "--max-budget-usd", str(acfg["max_budget_usd"]), "--output-format", "stream-json", "--verbose",
           "--add-dir", report_dir, "--allowedTools"] + allowed + ["--disallowedTools"] + af.DISALLOWED_TOOLS
    if acfg.get("model"):
        cmd += ["--model", acfg["model"]]
    try:
        af.claude_oneshot(dict(acfg), cmd, os.path.join(report_dir, tag + ".jsonl"), os.path.join(report_dir, tag + ".err"),
                          cwd=wt, timeout=3600)
    except (RuntimeError, subprocess.TimeoutExpired) as ex:
        log("Claude (%s) lỗi: %s" % (tag, ex))
    if not os.path.exists(report):
        return "", report
    with open(report, encoding="utf-8") as fh:
        return fh.readline().strip(), report


def _union(wt, files):
    """Gộp tự động file danh sách (db/master*.xml) / .properties: giữ dòng của cả hai bên."""
    done = []
    for f in files:
        if not (af.UNION_MERGE_FILES.search(f) or af.UNION_PROPERTIES.search(f)):
            continue
        p = os.path.join(wt, f)
        try:
            text = open(p, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        merged = af.union_properties(text) if af.UNION_PROPERTIES.search(f) else af.CONFLICT_BLOCK.sub(r"\1\2", text)
        if merged is None:
            continue
        open(p, "w", encoding="utf-8").write(merged)
        done.append(f)
    if done:
        _git(wt, "add", "--", *done, check=True)
    return done


def resolve_all(acfg, wt, branch, ticket, report_dir, log):
    """Gỡ hết xung đột của cherry-pick đang dừng trong wt (lặp qua từng commit). Trả (ok, ghi chú, [file đã gỡ])."""
    if not acfg.get("claude_resolve_conflicts", True):
        return False, "chưa bật gỡ xung đột tự động (claude_resolve_conflicts)", []
    os.makedirs(report_dir, exist_ok=True)
    resolved, notes, step = [], [], 0
    while in_progress(wt):
        step += 1
        if step > 30:
            return False, "quá nhiều lượt gỡ", resolved
        commit = _pick_head(wt)
        files = unmerged(wt)
        if not files:  # commit rỗng sau khi gỡ (đã có sẵn trên nhánh đích)
            out = _git(wt, "-c", "core.editor=true", "cherry-pick", "--continue")
            if in_progress(wt) and not unmerged(wt):
                out = _git(wt, "cherry-pick", "--skip")
                notes.append("%s rỗng trên nhánh đích → bỏ qua" % commit[:9])
            if in_progress(wt) and not unmerged(wt):
                return False, "không tiếp tục được cherry-pick: %s" % out.strip()[-300:], resolved
            continue
        union = _union(wt, files)
        rest = [f for f in files if f not in union]
        if union:
            notes.append("%s: tự gộp %s" % (commit[:9], ", ".join(union)))
        if rest:
            subject = _git(wt, "log", "-1", "--format=%s", commit).strip()
            prompt = PICK_CONFLICT_PROMPT.format(work=wt, branch=branch, commit=commit[:9], ticket=ticket,
                                                 subject=subject, files=", ".join(rest), report="{report}")
            log("xung đột %s ở %s → Claude gỡ" % (commit[:9], ", ".join(rest)))
            first, report = _claude(acfg, prompt, wt, report_dir, True, "resolve-%d-%s" % (step, commit[:9]), log)
            left = [f for f in rest if os.path.exists(os.path.join(wt, f)) and
                    af.CONFLICT_MARKER.search(open(os.path.join(wt, f), encoding="utf-8", errors="replace").read())]
            if "ĐÃ GỠ" not in first:
                return False, "Claude không gỡ được %s (%s, xem %s)" % (commit[:9], first or "không có report", report), resolved
            if left:
                return False, "còn dấu xung đột ở %s (xem %s)" % (", ".join(left), report), resolved
            _git(wt, "add", "-A", "--", *rest, check=True)
            notes.append("%s: Claude gỡ %s" % (commit[:9], ", ".join(rest)))
        resolved += files
        out = _git(wt, "-c", "core.editor=true", "cherry-pick", "--continue")
        if in_progress(wt) and not unmerged(wt) and _pick_head(wt) == commit:
            return False, "cherry-pick --continue lỗi: %s" % out.strip()[-300:], resolved
    return True, "; ".join(notes), sorted(set(resolved))


def _norm(line):
    return re.sub(r"\s+", " ", line).strip()


def _added_removed(diff_text):
    """{file: (dòng thêm, dòng bớt)} từ unified diff."""
    out, cur = {}, None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
            if cur:
                out.setdefault(cur, ([], []))
        elif cur and line.startswith("+") and not line.startswith("+++"):
            out[cur][0].append(line[1:])
        elif cur and line.startswith("-") and not line.startswith("---"):
            out[cur][1].append(line[1:])
    return out


def coverage(wt, base, commits):
    """So dòng của các commit gốc với kết quả. Trả (thiếu, thừa): [(file, dòng)]."""
    want = {}  # file → {dòng chuẩn hoá: dòng gốc}
    removed_later = {}
    for c in commits:
        for f, (add, rem) in _added_removed(_git(wt, "show", "--format=", c)).items():
            for line in rem:
                removed_later.setdefault(f, set()).add(_norm(line))
                want.get(f, {}).pop(_norm(line), None)
            for line in add:
                if not _TRIVIAL.match(line):
                    want.setdefault(f, {})[_norm(line)] = line
    missing = []
    for f, lines in want.items():
        try:
            text = {_norm(x) for x in open(os.path.join(wt, f), encoding="utf-8", errors="replace").read().splitlines()}
        except OSError:
            if lines:
                missing.append((f, "(file không có trên nhánh đích)"))
            continue
        missing += [(f, orig.strip()) for n, orig in lines.items() if n not in text]
    extra = []
    allowed = {f: {_norm(x) for x in v} for f, v in want.items()}
    for f, (add, rem) in _added_removed(_git(wt, "diff", base, "HEAD")).items():
        # dòng có sẵn của nhánh đích chỉ đổi dấu phẩy cuối (để chèn khoá mới phía sau) → không tính là kéo thừa
        old = {_norm(x).rstrip(",") for x in rem}
        extra += [(f, line.strip()) for line in add if not _TRIVIAL.match(line) and _norm(line) not in allowed.get(f, set())
                  and _norm(line).rstrip(",") not in old]
    return missing, extra


def _tsc_errors(wt, files):
    if not os.path.exists(os.path.join(wt, "tsconfig.app.json")) or not shutil.which("npx"):
        return None
    res = subprocess.run(["npx", "tsc", "--noEmit", "-p", "tsconfig.app.json"], cwd=wt, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, universal_newlines=True, timeout=1800, errors="replace")
    keep = tuple(files)
    return {re.sub(r"\(\d+,\d+\)", "", line).strip() for line in res.stdout.splitlines()
            if "error TS" in line and line.startswith(keep)}


def build_check(acfg, repo, wt, base, files, log_path):
    """Lỗi build / kiểm tra kiểu MỚI so với nhánh đích trước khi gắp. Trả (danh sách lỗi mới, ghi chú)."""
    R.link_shared(repo, wt, repo["path"])
    err = R.run_setup(repo, wt, af.build_env(acfg), log_path)
    if err:
        return ["setup lỗi: %s" % err], "setup lỗi"

    def collect():
        errs, codes = set(), []
        with open(log_path, "a", encoding="utf-8") as out:
            for cmd in R.commands(repo, "merge_check", wt):
                out.write("\n$ %s\n" % " ".join(cmd))
                out.flush()
                start = out.tell()
                code = af.run_build(cmd, wt, out, acfg)
                codes.append(code)
                if code != 0:
                    with open(log_path, encoding="utf-8", errors="replace") as f:
                        f.seek(start)
                        errs |= {"build: " + x for x in af.error_lines(f.read())}
        ts = _tsc_errors(wt, files)
        return errs | ({"tsc: " + x for x in ts} if ts else set()), codes

    after, codes = collect()
    if not after:
        return [], "build + kiểm tra kiểu: không lỗi"
    head = _git(wt, "rev-parse", "HEAD").strip()
    try:
        _git(wt, "checkout", "-q", "--detach", base, check=True)
        before, _ = collect()
    finally:
        _git(wt, "checkout", "-q", "--detach", head)
    new = sorted(after - before)
    return new, "%d lỗi, %d có sẵn trên nhánh đích, %d mới" % (len(after), len(after & before), len(new))


def verify(acfg, repo, wt, branch, base, ticket, commits, files, report_dir, log):
    """Kiểm tra kết quả sau khi tự gỡ. Trả (ok, tóm tắt, đường dẫn review.md)."""
    os.makedirs(report_dir, exist_ok=True)
    missing, extra = coverage(wt, base, commits)
    new_errs, build_note = build_check(acfg, repo, wt, base, files, os.path.join(report_dir, "build.log"))
    lines = ["- Độ phủ: %d dòng của commit gốc không thấy trên nhánh đích, %d dòng thêm ngoài commit gốc."
             % (len(missing), len(extra))]
    lines += ["  - THIẾU %s: %s" % (f, x[:200]) for f, x in missing[:40]]
    lines += ["  - THÊM %s: %s" % (f, x[:200]) for f, x in extra[:40]]
    lines.append("- Build / kiểm tra kiểu: %s" % build_note)
    lines += ["  - LỖI MỚI %s" % e[:300] for e in new_errs[:30]]
    checks = "\n".join(lines)
    with open(os.path.join(report_dir, "checks.md"), "w", encoding="utf-8") as fh:
        fh.write(checks + "\n")
    prompt = PICK_REVIEW_PROMPT.format(ticket=ticket, branch=branch, work=wt, base=base,
                                       commits=", ".join(c[:9] for c in commits), files=", ".join(files) or "(không)",
                                       checks=checks, report="{report}")
    first, report = _claude(acfg, prompt, wt, report_dir, False, "review", log)
    ok = "ĐẠT" in first and "CẦN XEM" not in first and not new_errs
    summary = "review: %s · %s · thiếu %d / thêm %d dòng" % (
        first.replace("KẾT QUẢ:", "").strip() or "không có kết luận", build_note, len(missing), len(extra))
    return ok, summary, report
