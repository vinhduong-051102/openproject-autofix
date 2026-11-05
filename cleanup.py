#!/usr/bin/env python3
"""
cleanup: dọn dữ liệu autofix / QA của các bug đã chuyển cho người QA (deploy.json qa_user_id).

Một ticket được dọn khi trên OpenProject nó đang giao cho người QA và không có lượt autofix / QA nào đang chạy. Xoá:
  - thư mục lượt chạy ~/autofix/<SOAR-x>-* (fix, -qa-, -review-), kèm git worktree + nhánh local auto/<lượt>
  - báo cáo ~/Downloads/autofix-reports/<SOAR-x>-*.md (report_dir)
  - .status/<SOAR-x>.json, .<SOAR-x>.lock, .dashboard-<SOAR-x>.log, .pending-followup/<SOAR-x>.json
  - mục của ticket trong qa-history.json, qa-fail-count.json, qa-pending.json
  - các dòng inbox chỉ nói về ticket đó (giữ dòng MERGE DEV / DEPLOY 86 / BÁO CÁO NGÀY)
Không đụng tới commit đã push, PR, OpenProject.

Dọn nhẹ (mọi lượt đã xong quá PRUNE_AFTER giây, kể cả chưa chuyển QA): xoá thư mục tạo lại được trong worktree —
node_modules cài riêng (lệnh setup), target / dist / .turbo… — CHỈ khi git bỏ qua (ignored) và không phải symlink.
Giữ mã nguồn + commit (ship / follow-up / xem lại vẫn chạy được; lệnh setup tự chạy lại khi cần build).

    cleanup.py              # dọn
    cleanup.py --dry-run    # chỉ liệt kê
    cleanup.py --every 3600 # dọn nếu lần dọn trước đã quá 3600 giây (op-watch gọi định kỳ)
"""
import argparse
import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402
import op_autofix as af  # noqa: E402
import op_watch  # noqa: E402

PRUNE_AFTER = int(envconf.get("CLEANUP_PRUNE_AFTER") or 3600)
PRUNE_NAMES = {"node_modules", "target", "dist", "build", ".turbo", ".next", ".nuxt", ".parcel-cache", ".vite",
               "coverage", ".gradle", "__pycache__", ".pytest_cache", ".mypy_cache"}
RUN_RX = re.compile(r"^((?:[A-Z][A-Z0-9]*)-\d+)-(?:qa-|review-|followup-|conflict-)?\d{4}-\d{4}[a-z]?$")
KEEP_INBOX = ("**MERGE DEV**", "**DEPLOY 86", "**BÁO CÁO NGÀY**")


def local_tickets(root):
    """{SOAR-x: [tên thư mục lượt chạy]} + ticket chỉ còn dấu vết (status, lock…)."""
    out = {}
    for name in os.listdir(root):
        m = RUN_RX.match(name)
        if m and os.path.isdir(os.path.join(root, name)):
            out.setdefault(m.group(1), []).append(name)
    for pat in (".status/*.json", ".pending-followup/*.json"):
        for p in glob.glob(os.path.join(root, pat)):
            out.setdefault(os.path.basename(p)[:-5], [])
    for p in glob.glob(os.path.join(root, ".*.lock")):
        m = re.match(r"^\.((?:[A-Z][A-Z0-9]*)-\d+)\.lock$", os.path.basename(p))
        if m:
            out.setdefault(m.group(1), [])
    return out


def busy(root, display_id):
    """Ticket đang có lượt autofix (khoá ticket đang giữ / trạng thái đang chạy, tiến trình còn sống) hoặc đang QA."""
    lock = os.path.join(root, ".%s.lock" % display_id)
    if os.path.exists(lock):
        with open(lock, "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                return True
    st = os.path.join(root, ".status", display_id + ".json")
    if os.path.exists(st):
        try:
            data = json.load(open(st, encoding="utf-8"))
        except ValueError:
            data = {}
        if data.get("state") in af.ACTIVE_STATES and af.status_alive(data):
            return True
    # QA trên server đang chạy (deploy_qa ghi .qa-active) và thư mục QA của ticket mới được tạo
    if os.path.exists(os.path.join(root, ".qa-active")):
        for d in glob.glob(os.path.join(root, "%s-qa-*" % display_id)):
            if time.time() - os.path.getmtime(d) < 3600:
                return True
    return False


def _ignored(repo_dir, rel):
    return subprocess.run(["git", "-C", repo_dir, "check-ignore", "-q", rel + "/"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def _heavy_dirs(repo_dir):
    """Thư mục tạo lại được trong worktree (theo PRUNE_NAMES), git bỏ qua, không phải symlink; không đi vào bên trong."""
    out = []
    for dp, dns, _ in os.walk(repo_dir):
        if os.path.basename(dp) == ".git":
            dns[:] = []
            continue
        keep = []
        for d in dns:
            p = os.path.join(dp, d)
            if os.path.islink(p) or d == ".git":
                continue
            if d in PRUNE_NAMES and _ignored(repo_dir, os.path.relpath(p, repo_dir)):
                out.append(p)
            else:
                keep.append(d)
        dns[:] = keep
    return out


DELIVERED_RX = re.compile(r"đã push|chuyển Resolved|UNREVIEWED|đã comment", re.I)


def _status(root, display_id):
    try:
        with open(os.path.join(root, ".status", display_id + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _untouched(repo_dir):
    """Worktree không có commit nào ngoài remote và không có thay đổi chưa commit (repo này không bị sửa)."""
    out = subprocess.run(["git", "-C", repo_dir, "rev-list", "--no-merges", "--max-count=1", "HEAD", "--not", "--remotes"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
    st = subprocess.run(["git", "-C", repo_dir, "status", "--porcelain", "--untracked-files=no"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
    return out.returncode == 0 and not out.stdout.strip() and st.returncode == 0 and not st.stdout.strip()


def drop_trees(acfg, root, dry=False):
    """Bỏ worktree không còn cần (lượt đã xong quá PRUNE_AFTER giây, ticket không chạy):
    - lượt cũ đã có lượt mới hơn của cùng ticket thay thế, không còn commit chưa push → xoá hẳn (af.clean);
    - lượt hiện tại đã giao xong (push / Resolved / UNREVIEWED…) → xoá worktree của repo không bị sửa gì.
    Trả số byte thu hồi."""
    import repos as R
    freed, notes = 0, []
    by_ticket = {}
    for name in os.listdir(root):
        m = RUN_RX.match(name)
        if m and os.path.isdir(os.path.join(root, name)):
            by_ticket.setdefault(m.group(1), []).append(name)
    for display_id, runs in sorted(by_ticket.items()):
        if busy(root, display_id):
            continue
        st = _status(root, display_id)
        current = st.get("run_name") or ""
        for name in runs:
            rd = os.path.join(root, name)
            if time.time() - os.path.getmtime(rd) < PRUNE_AFTER:
                continue
            older = current and name != current and os.path.isdir(os.path.join(root, current)) and \
                os.path.getmtime(rd) <= os.path.getmtime(os.path.join(root, current))
            if older and not unpushed(acfg, root, name):
                size = du([rd])
                notes.append("%s (lượt cũ) %.0f MB" % (name, size / 1e6))
                freed += size
                if not dry:
                    af.clean(acfg, name)
                continue
            if name == current and st.get("state") == "done" and DELIVERED_RX.search(st.get("verdict") or ""):
                for r, d in R.trees(acfg, rd):
                    if os.path.isdir(d) and _untouched(d):
                        size = du([d])
                        notes.append("%s/%s (không sửa) %.0f MB" % (name, r["key"], size / 1e6))
                        freed += size
                        if not dry:
                            subprocess.run(["git", "worktree", "remove", "--force", d], cwd=r["path"],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            shutil.rmtree(d, ignore_errors=True)
    if notes:
        print("bỏ worktree không cần (%.1f GB)%s: %s" % (freed / 1e9, " — thử" if dry else "", ", ".join(notes[:40])))
    return freed


def prune(acfg, root, dry=False):
    """Dọn nhẹ các lượt đã xong quá PRUNE_AFTER giây. Trả số byte thu hồi."""
    import repos as R
    freed, runs = 0, []
    for name in sorted(os.listdir(root)):
        m = RUN_RX.match(name)
        rd = os.path.join(root, name)
        if not m or not os.path.isdir(rd) or os.path.exists(os.path.join(rd, ".pruned")):
            continue
        if time.time() - os.path.getmtime(rd) < PRUNE_AFTER or busy(root, m.group(1)):
            continue
        newest = max([os.path.getmtime(os.path.join(rd, f)) for f in os.listdir(rd)] + [os.path.getmtime(rd)])
        if time.time() - newest < PRUNE_AFTER:
            continue
        heavy = []
        for r, d in R.trees(acfg, rd):
            if os.path.isdir(d):
                heavy += _heavy_dirs(d)
        size = du(heavy) if heavy else 0
        if heavy:
            runs.append("%s %.0f MB" % (name, size / 1e6))
        if not dry:
            for p in heavy:
                shutil.rmtree(p, ignore_errors=True)
            for r, d in R.trees(acfg, rd):  # setup phải chạy lại khi lượt này cần build (ship / follow-up)
                gd = subprocess.run(["git", "-C", d, "rev-parse", "--git-dir"], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, universal_newlines=True).stdout.strip() if os.path.isdir(d) else ""
                mark = os.path.join(d, gd, "op-setup-done") if gd else ""
                if mark and os.path.exists(mark):
                    os.remove(mark)
            open(os.path.join(rd, ".pruned"), "w").close()
        freed += size
    if runs:
        print("dọn nhẹ %d lượt (%.1f GB)%s: %s" % (len(runs), freed / 1e9, " — thử" if dry else "", ", ".join(runs[:30])))
    return freed


def unpushed(acfg, root, run_name):
    """Worktree / nhánh auto/<lượt> còn commit chưa nằm trên bất kỳ nhánh remote nào → không được xoá."""
    import repos as R
    for r in R.repos(acfg):
        repo, wt = r["path"], os.path.join(root, run_name, r["key"])
        refs = []
        if os.path.isdir(wt):
            refs.append(("-C", wt, "HEAD"))
        branch = "auto/" + run_name
        if subprocess.run(["git", "-C", repo, "rev-parse", "-q", "--verify", "refs/heads/" + branch],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            refs.append(("-C", repo, "refs/heads/" + branch))
        for c, path, ref in refs:
            out = subprocess.run(["git", c, path, "rev-list", "--no-merges", "--max-count=1", ref, "--not", "--remotes"],
                                 capture_output=True, text=True)
            if out.returncode != 0 or out.stdout.strip():
                return True
    return False


def handed_over(api, display_id, qa_user_id):
    try:
        wp = api.get("/api/v3/work_packages/%s" % display_id)
    except Exception as ex:  # noqa: BLE001 — ticket không đọc được thì không dọn
        return None, str(ex)
    href = ((wp.get("_links") or {}).get("assignee") or {}).get("href") or ""
    return href.rstrip("/").endswith("/%s" % qa_user_id), (wp.get("_links") or {}).get("status", {}).get("title")


def drop_key(path, key, dry):
    if not os.path.exists(path):
        return False
    try:
        data = json.load(open(path, encoding="utf-8"))
    except ValueError:
        return False
    changed = False
    if isinstance(data, dict) and key in data:
        data.pop(key)
        changed = True
    if isinstance(data, dict) and isinstance(data.get("tickets"), list) and key in data["tickets"]:
        data["tickets"] = [t for t in data["tickets"] if t != key]
        changed = True
    if changed and not dry:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    return changed


def clean_inbox(path, ids, dry):
    if not path or not os.path.exists(path) or not ids:
        return 0
    rx = re.compile(r"\b(%s)\b" % "|".join(re.escape(i) for i in ids))
    kept, dropped = [], 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            found = set(re.findall(r"\b[A-Z][A-Z0-9]*-\d+\b", line))
            if rx.search(line) and found and found <= set(ids) and not any(k in line for k in KEEP_INBOX):
                dropped += 1
                continue
            kept.append(line)
    if dropped and not dry:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, path)
    return dropped


def du(paths):
    total = 0
    for p in paths:
        for dirpath, _, files in os.walk(p):
            for fn in files:
                try:
                    total += os.lstat(os.path.join(dirpath, fn)).st_size
                except OSError:
                    pass
    return total


def run(dry=False, only=None):
    cfg = op_watch.load_config(argparse.Namespace(config=None, interval=None, on_change=None, no_notify=True))
    acfg = dict(af.AUTOFIX_DEFAULTS)
    acfg.update(cfg.get("autofix") or {})
    dcfg = envconf.deploy_config()
    qa_user = dcfg.get("qa_user_id")
    if not qa_user:
        sys.exit("Chưa cấu hình qa_user_id (deploy.json / QA_USER_ID) — không biết ai là người QA")
    root = os.path.expanduser(acfg["root"])
    report_dir = os.path.expanduser(acfg.get("report_dir") or "")
    pruned = drop_trees(acfg, root, dry) + prune(acfg, root, dry)
    if pruned > 200e6 and not dry:
        af.inbox(cfg, "**DỌN DẸP** dọn nhẹ worktree đã xong: thu hồi %.1f GB (node_modules / target / dist…)" % (pruned / 1e9))
    api = op_watch.OpenProject(cfg["url"], cfg["api_key"])

    done, freed = [], 0
    for display_id, runs in sorted(local_tickets(root).items()):
        if only and display_id not in only:
            continue
        ok, status = handed_over(api, display_id, qa_user)
        if not ok:
            continue
        if busy(root, display_id):
            print("%s: đang chạy, để lần sau" % display_id)
            continue
        unsafe = [r for r in runs if unpushed(acfg, root, r)]
        if unsafe:
            print("%s: giữ lại vì còn commit chưa có trên remote: %s" % (display_id, ", ".join(unsafe)))
            continue
        dirs = [os.path.join(root, r) for r in runs]
        reports = glob.glob(os.path.join(report_dir, "%s-*.md" % display_id)) if report_dir else []
        size = du(dirs) + sum(os.path.getsize(p) for p in reports)
        print("%s (%s, đã chuyển QA): %d lượt chạy, %d báo cáo, %.1f MB%s"
              % (display_id, status, len(runs), len(reports), size / 1e6, " — thử" if dry else ""))
        if dry:
            done.append(display_id)
            freed += size
            continue
        for r in runs:
            af.clean(acfg, r)
        for p in reports + [os.path.join(root, ".status", display_id + ".json"),
                            os.path.join(root, ".%s.lock" % display_id),
                            os.path.join(root, ".dashboard-%s.log" % display_id),
                            os.path.join(root, ".pending-followup", display_id + ".json")]:
            if os.path.exists(p):
                os.remove(p)
        for name in ("qa-history.json", "qa-fail-count.json", "qa-pending.json"):
            drop_key(os.path.join(root, name), display_id, dry)
        done.append(display_id)
        freed += size
    # Bản nháp tạo task (trang theo dõi) cũ hơn 14 ngày
    drafts = os.path.join(root, "task-drafts")
    if os.path.isdir(drafts):
        for name in os.listdir(drafts):
            path = os.path.join(drafts, name)
            if os.path.isdir(path) and time.time() - os.path.getmtime(path) > 14 * 86400:
                print("bản nháp tạo task cũ: %s%s" % (name, " — thử" if dry else ""))
                if not dry:
                    shutil.rmtree(path, ignore_errors=True)
    lines = clean_inbox(os.path.expanduser(cfg.get("inbox") or ""), done, dry)
    if done:
        msg = "%s %d ticket đã chuyển QA (%s) · %.0f MB · %d dòng inbox" % (
            "sẽ dọn" if dry else "đã dọn", len(done), ", ".join(done), freed / 1e6, lines)
        print(msg)
        if not dry:
            af.inbox(cfg, "**DỌN DẸP** " + msg)
    else:
        print("không có ticket nào cần dọn")
    return done


def main():
    p = argparse.ArgumentParser(description="Dọn dữ liệu autofix / QA của các bug đã chuyển cho người QA")
    p.add_argument("--dry-run", action="store_true", help="chỉ liệt kê, không xoá")
    p.add_argument("--every", type=int, metavar="GIÂY", help="chỉ chạy nếu lần dọn trước đã quá số giây này")
    p.add_argument("tickets", nargs="*", help="chỉ dọn các ticket này (SOAR-x)")
    args = p.parse_args()
    stamp = os.path.join(envconf.autofix_root(), ".last-cleanup")
    if args.every and os.path.exists(stamp) and time.time() - os.path.getmtime(stamp) < args.every:
        return
    with open(os.path.join(envconf.autofix_root(), ".cleanup.lock"), "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        run(args.dry_run, set(args.tickets) or None)
        if not args.dry_run:
            open(stamp, "w").close()


if __name__ == "__main__":
    main()
