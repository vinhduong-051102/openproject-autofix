#!/usr/bin/env python3
"""Gắp (cherry-pick) commit của một ticket sang các nhánh khác — chạy nền từ nút "Gắp sang nhánh…" trên dashboard.

Mỗi lượt gắp là một thư mục ~/autofix/.picks/<TICKET>-pick-<MMDD-HHMM>/ gồm status.json + log.txt. Với mỗi nhánh đích:
worktree tạm (detached từ origin/<nhánh>) → cherry-pick -x các commit của ticket chưa có trên nhánh → push HEAD lên
origin/<nhánh> → xoá worktree. Xung đột → tự gỡ (pick_resolve: Claude gỡ theo đúng commit đang gắp) rồi kiểm tra lại
(độ phủ dòng của commit gốc, build / kiểm tra kiểu so với nhánh đích, Claude review THIẾU / HỎNG): đạt mới push; không
đạt hoặc không gỡ được → giữ worktree + báo cáo (trạng thái review / conflict) để xem tay, không push.

CLI:  python3 cherry.py --ticket SOAR-2956 --repo fe --branches dev-ss-2.0.34.65-otp,dev-ss-2.0.33 [--no-push] [--no-comment]
      [--no-auto-resolve]
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import envconf  # noqa: E402
import op_watch  # noqa: E402
import op_autofix as af  # noqa: E402
import repos as R  # noqa: E402
import pick_resolve as PR  # noqa: E402

PICKS = ".picks"
BRANCH_RX = re.compile(r"^[\w][\w./-]{0,120}$")
MAX_BRANCHES = 8


def git(cwd, *args, check=True, timeout=600):
    res = subprocess.run(["git"] + list(args), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True, timeout=timeout)
    if check and res.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), res.stdout.strip()[-800:]))
    return res.stdout


def _ticket_rx(display_id):
    # [SOAR-2956] / SOAR-2956: / fix(SOAR-2956) — không khớp SOAR-29560.
    return r"%s([^0-9]|$)" % re.escape(display_id)


def ticket_commits(repo, display_id, fetch=True):
    """Commit của ticket trên nhánh làm việc của repo (origin/<branch>), cũ → mới."""
    path = repo["path"]
    if fetch:
        try:
            git(path, "fetch", "-q", "--prune", "origin", timeout=120)
        except Exception:
            pass
    ref = "origin/%s" % repo["branch"]
    if git(path, "rev-parse", "-q", "--verify", ref, check=False).strip() == "":
        ref = repo["branch"]
    out = git(path, "log", "--reverse", "-E", "--grep", _ticket_rx(display_id), "--format=%H%x1f%s%x1f%ci",
              "-n", "200", ref)
    commits = []
    for line in out.splitlines():
        if "\x1f" not in line:
            continue
        sha, subject, at = line.split("\x1f", 2)
        commits.append({"sha": sha, "short": sha[:9], "subject": subject, "at": at[:16]})
    return commits


def remote_branches(repo, fetch=True):
    path = repo["path"]
    if fetch:
        try:
            git(path, "fetch", "-q", "--prune", "origin", timeout=120)
        except Exception:
            pass
    out = git(path, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)%1f%(committerdate:short)",
              "refs/remotes/origin")
    res = []
    for line in out.splitlines():
        name, _, at = line.partition("\x1f")
        name = name[len("origin/"):] if name.startswith("origin/") else name
        if name in ("HEAD", repo["branch"]):
            continue
        res.append({"name": name, "at": at, "upstream": name == repo.get("upstream")})
    return res


def options(acfg, display_id):
    """Repo của dự án + commit của ticket + nhánh đích để trang hiện hộp chọn."""
    out = []
    for r in R.repos(acfg):
        if not os.path.isdir(os.path.join(r["path"], ".git")) and not os.path.isfile(os.path.join(r["path"], ".git")):
            out.append({"key": r["key"], "label": r["label"], "error": "không có repo tại %s" % r["path"]})
            continue
        try:
            commits = ticket_commits(r, display_id)
            branches = remote_branches(r, fetch=False)
        except Exception as e:
            out.append({"key": r["key"], "label": r["label"], "error": str(e)[:300]})
            continue
        out.append({"key": r["key"], "label": r["label"], "branch": r["branch"], "upstream": r.get("upstream") or "",
                    "commits": commits, "branches": branches})
    return out


def _present(path, ref, c):
    """Commit đã có trên nhánh đích: cùng hash, có dòng (cherry picked from commit <sha>) hoặc cùng tiêu đề."""
    if subprocess.run(["git", "merge-base", "--is-ancestor", c["sha"], ref], cwd=path,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return True
    out = git(path, "log", "-n", "1", "--format=%H", "-F", "--grep", "cherry picked from commit %s" % c["sha"], ref)
    if out.strip():
        return True
    out = git(path, "log", "-n", "1", "--format=%H", "-F", "--grep", c["subject"], ref)
    return bool(out.strip())


def _write(status_path, st):
    tmp = status_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, status_path)


def sync_before_push(wt, br, n):
    """Ngay trước khi push: lấy bản mới nhất của nhánh đích. Có người push chen vào trong lúc gắp / gỡ / kiểm tra →
    đặt lại n commit đã gắp lên trên (rebase). Trả "" (không đổi), mô tả đã rebase, hoặc chuỗi bắt đầu "không …"
    khi rebase xung đột (đã abort, worktree về như trước)."""
    git(wt, "fetch", "-q", "origin", br, timeout=120)
    remote = "origin/" + br
    if subprocess.run(["git", "merge-base", "--is-ancestor", remote, "HEAD"], cwd=wt).returncode == 0:
        return ""
    new = git(wt, "log", "--format=%h %s", "HEAD~%d..%s" % (n, remote), check=False).strip().splitlines()
    out = git(wt, "rebase", "--onto", remote, "HEAD~%d" % n, check=False)
    if subprocess.run(["git", "rev-parse", "-q", "--verify", "REBASE_HEAD"], cwd=wt, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode == 0 or "CONFLICT" in out:
        git(wt, "rebase", "--abort", check=False)
        return "không đặt lại được lên %s mới (có %d commit mới: %s) — xung đột khi rebase" % (
            remote, len(new), "; ".join(new)[:300])
    return "nhánh đích có %d commit mới trong lúc gắp (%s) → đã đặt lại commit gắp lên trên" % (len(new), "; ".join(new)[:300])


def run_pick(cfg, acfg, root, display_id, repo_key, branches, push=True, comment=True, wp_id="", name=None,
             auto_resolve=True):
    repo = R.find(acfg, repo_key)
    if not repo:
        raise ValueError("không có repo %s trong dự án" % repo_key)
    branches = [b for b in dict.fromkeys(branches) if BRANCH_RX.match(b)]
    if not branches:
        raise ValueError("chưa chọn nhánh đích")
    if len(branches) > MAX_BRANCHES:
        raise ValueError("tối đa %d nhánh một lượt" % MAX_BRANCHES)
    if repo["branch"] in branches:
        raise ValueError("%s là nhánh làm việc, không gắp vào chính nó" % repo["branch"])
    name = name or "%s-pick-%s" % (display_id, datetime.now().strftime("%m%d-%H%M%S"))
    pdir = os.path.join(root, PICKS, name)
    os.makedirs(pdir, exist_ok=True)
    status_path = os.path.join(pdir, "status.json")
    logf = open(os.path.join(pdir, "log.txt"), "a", encoding="utf-8")

    def log(msg):
        line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
        logf.write(line + "\n"); logf.flush()
        af.log("gắp %s: %s" % (display_id, msg))

    st = {"name": name, "display_id": display_id, "repo": repo["key"], "label": repo["label"], "branches": branches,
          "push": push, "comment": comment, "wp_id": wp_id, "state": "running", "pid": os.getpid(),
          "started_at": datetime.now().isoformat(timespec="seconds"), "commits": [], "results": []}
    _write(status_path, st)
    path = repo["path"]
    try:
        commits = ticket_commits(repo, display_id)
        st["commits"] = commits
        _write(status_path, st)
        if not commits:
            raise ValueError("không tìm thấy commit nào có %s trên origin/%s" % (display_id, repo["branch"]))
        log("%d commit của %s trên origin/%s: %s" % (len(commits), display_id, repo["branch"],
                                                      ", ".join(c["short"] for c in commits)))
        for br in branches:
            res = {"branch": br, "state": "running", "picked": [], "skipped": [], "message": "", "worktree": ""}
            st["results"].append(res)
            _write(status_path, st)
            wt = os.path.join(pdir, "wt-" + re.sub(r"[^\w.-]+", "_", br))
            try:
                ref = "origin/" + br
                if git(path, "rev-parse", "-q", "--verify", ref, check=False).strip() == "":
                    raise ValueError("không có nhánh %s trên origin" % br)
                todo = [c for c in commits if not _present(path, ref, c)]
                res["skipped"] = [c["short"] for c in commits if c not in todo]
                if not todo:
                    res.update(state="skip", message="đã có đủ %d commit" % len(commits))
                    log("%s: đã có đủ commit, bỏ qua" % br)
                    continue
                git(path, "worktree", "add", "--detach", wt, ref)
                res["worktree"] = wt
                log("%s: worktree %s, gắp %s" % (br, wt, ", ".join(c["short"] for c in todo)))
                out = git(wt, "cherry-pick", "-x", *[c["sha"] for c in todo], check=False)
                if "CONFLICT" in out or git(wt, "diff", "--name-only", "--diff-filter=U", check=False).strip():
                    files = git(wt, "diff", "--name-only", "--diff-filter=U", check=False).split()
                    rdir = os.path.join(pdir, "resolve-" + re.sub(r"[^\w.-]+", "_", br))
                    ok, note = False, "tắt tự gỡ"
                    if auto_resolve:
                        log("%s: xung đột %s → tự gỡ" % (br, ", ".join(files)))
                        ok, note, fixed = PR.resolve_all(acfg, wt, br, display_id, rdir, log)
                    if not ok:
                        done = git(wt, "log", "--format=%h", "%s..HEAD" % ref, check=False).split()
                        res.update(state="conflict", picked=done, report=rdir if auto_resolve else "",
                                   message="xung đột %s%s — gỡ tay trong worktree rồi `git cherry-pick --continue` và push" % (
                                       ", ".join(files)[:300] or "không rõ file",
                                       " (tự gỡ không được: %s)" % note[:300] if auto_resolve else ""))
                        log("%s: XUNG ĐỘT %s; %s; giữ worktree %s" % (br, ", ".join(files), note, wt))
                        continue
                    log("%s: đã tự gỡ (%s) → kiểm tra lại" % (br, note))
                    vok, vsum, vrep = PR.verify(acfg, repo, wt, br, ref, display_id, [c["sha"] for c in todo], fixed, rdir, log)
                    res.update(auto_resolved=note, verify=vsum, report=vrep)
                    log("%s: kiểm tra sau khi gỡ — %s" % (br, vsum))
                    if not vok:
                        res.update(state="review", picked=git(wt, "log", "--format=%h", "%s..HEAD" % ref).split()[::-1],
                                   message="đã tự gỡ xung đột nhưng kiểm tra chưa đạt (%s) — xem %s, chưa push, giữ worktree"
                                   % (vsum, vrep))
                        continue
                    out = ""
                if "error" in out.lower() and "nothing to commit" not in out:
                    raise RuntimeError(out.strip()[-600:])
                res["picked"] = git(wt, "log", "--format=%h", "%s..HEAD" % ref).split()[::-1]
                if push:
                    moved = sync_before_push(wt, br, len(res["picked"]))
                    if moved:
                        log("%s: %s" % (br, moved))
                    if moved.startswith("không"):
                        res.update(state="review", message=moved + " — chưa push, giữ worktree")
                        continue
                    res["picked"] = git(wt, "log", "--format=%h", "-n", str(len(res["picked"])), "HEAD").split()[::-1]
                    git(wt, "push", "origin", "HEAD:refs/heads/%s" % br, timeout=300)
                    res.update(state="ok", message="đã push %d commit%s" % (
                        len(res["picked"]), " (tự gỡ xung đột, kiểm tra đạt: %s)" % res["verify"] if res.get("verify") else ""))
                    log("%s: đã push %s" % (br, ", ".join(res["picked"])))
                else:
                    res.update(state="ok", message="đã gắp %d commit, chưa push (worktree %s)" % (len(res["picked"]), wt))
                    log("%s: đã gắp, không push" % br)
                    continue  # giữ worktree để người dùng xem / push tay
                git(path, "worktree", "remove", "--force", wt, check=False)
                res["worktree"] = ""
            except Exception as e:  # noqa: BLE001
                res.update(state="error", message=str(e)[:600])
                log("%s: LỖI %s" % (br, e))
                if os.path.isdir(wt) and res["state"] == "error" and not res.get("auto_resolved"):
                    git(path, "worktree", "remove", "--force", wt, check=False)
                    res["worktree"] = ""
            finally:
                _write(status_path, st)
        git(path, "worktree", "prune", check=False)
        bad = [r for r in st["results"] if r["state"] in ("conflict", "error", "review")]
        st["state"] = "done" if not bad else "error"
        st["verdict"] = "; ".join("%s: %s" % (r["branch"], r["message"] or r["state"]) for r in st["results"])
        if comment and wp_id and any(r["state"] == "ok" and r["picked"] for r in st["results"]):
            try:
                api = op_watch.OpenProject(cfg["url"], cfg["api_key"])
                lines = ["Đã gắp commit của %s (%s) sang nhánh:" % (display_id, repo["label"])]
                for r in st["results"]:
                    if r["state"] == "ok" and r["picked"]:
                        lines.append("- `%s`: %s%s" % (r["branch"], ", ".join("`%s`" % x for x in r["picked"]),
                                                         "" if push else " (chưa push)"))
                    elif r["state"] == "skip":
                        lines.append("- `%s`: đã có sẵn" % r["branch"])
                    else:
                        lines.append("- `%s`: %s — %s" % (r["branch"], r["state"], r["message"]))
                af.op_write(api, "POST", "/api/v3/work_packages/%s/activities" % wp_id, {"comment": {"raw": "\n".join(lines)}})
                st["commented"] = True
                log("đã ghi comment lên OpenProject")
            except Exception as e:  # noqa: BLE001
                st["comment_error"] = str(e)[:300]
                log("không ghi được comment OpenProject: %s" % e)
    except Exception as e:  # noqa: BLE001
        st.update(state="error", verdict=str(e)[:600])
        log("LỖI: %s" % e)
    st["finished_at"] = datetime.now().isoformat(timespec="seconds")
    st["pid"] = None
    _write(status_path, st)
    af.inbox(cfg, "**GẮP NHÁNH** %s — %s" % (display_id, st.get("verdict") or st["state"]))
    logf.close()
    return st


def picks(root, n=40):
    out = []
    for p in glob.glob(os.path.join(root, PICKS, "*", "status.json")):
        try:
            with open(p, encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            continue
        if st.get("state") == "running" and st.get("pid") and not _alive(st["pid"]):
            st["state"] = "stale"
        st["log"] = os.path.join(os.path.dirname(p), "log.txt")
        out.append(st)
    out.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    return out[:n]


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticket", required=True)
    ap.add_argument("--repo", required=True, help="key repo trong dự án (fe / be / soar-flow…)")
    ap.add_argument("--branches", required=True, help="nhánh đích, cách nhau dấu phẩy")
    ap.add_argument("--wp", default="", help="id work package để ghi comment")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-comment", action="store_true")
    ap.add_argument("--no-auto-resolve", action="store_true", help="xung đột thì dừng để gỡ tay (không tự gỡ)")
    ap.add_argument("--name", help="tên thư mục lượt gắp (dashboard đặt trước)")
    ap.add_argument("--options", action="store_true", help="chỉ in repo / commit / nhánh (JSON) rồi thoát")
    a = ap.parse_args()
    cfg = op_watch.load_config(argparse.Namespace(config=None, interval=None, on_change=None, no_notify=True))
    acfg = dict(af.AUTOFIX_DEFAULTS)
    acfg.update(cfg.get("autofix") or {})
    af._CFG.clear(); af._CFG.update(cfg)
    acfg = af.ticket_acfg(acfg, a.ticket)
    root = os.path.expanduser(acfg["root"])
    if a.options:
        print(json.dumps(options(acfg, a.ticket), ensure_ascii=False, indent=1))
        return
    st = run_pick(cfg, acfg, root, a.ticket, a.repo, a.branches.split(","), push=not a.no_push,
                  comment=not a.no_comment, wp_id=a.wp, name=a.name, auto_resolve=not a.no_auto_resolve)
    print(st.get("verdict") or st["state"])
    sys.exit(0 if st["state"] == "done" else 1)


if __name__ == "__main__":
    main()
