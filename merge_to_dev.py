#!/usr/bin/env python3
"""
merge_to_dev: đưa nhánh cá nhân vào nhánh dev chung qua Pull Request trên Gitea (chạy hằng ngày lúc 12h bằng
systemd timer op-merge.timer).

Với mỗi repo (FE FE_BRANCH → FE_UPSTREAM, BE BE_BRANCH → BE_UPSTREAM; cấu hình trong .env):
  1. Nhánh của mình không có commit nào chưa vào nhánh chung → bỏ qua.
  2. Trong worktree tạm: merge nhánh chung vào nhánh của mình trước (để PR không xung đột). Xung đột ở
     db/master*.xml → giữ cả hai bên; xung đột khác → Claude gỡ (af.resolve_conflicts), không gỡ được → dừng, báo.
  3. Build FE / compile BE trên kết quả merge. Lỗi → dừng, báo.
  4. Có merge mới ở bước 2 → push nhánh của mình. Tạo PR (hoặc dùng PR đang mở) rồi merge qua Gitea API.

Gitea: GITEA_URL + GITEA_TOKEN trong .env, hoặc ~/.config/op-watch/gitea.json (chmod 600) {"url": "...", "token": "..."}.
Kết quả: log, thông báo desktop, dòng **MERGE DEV** trong inbox op-watch.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import op_autofix as af  # noqa: E402  (dùng chung run / notify / inbox / build_env / gộp master.xml)
import op_watch  # noqa: E402

import envconf  # noqa: E402

GITEA_CONFIG = envconf.GITEA_CONFIG


def load_gitea():
    """gitea.json + GITEA_URL / GITEA_TOKEN (biến môi trường / .env thắng)."""
    return envconf.gitea_config()

REPOS = [
    # (nhãn, repo local, nhánh của mình, nhánh chung)
    ("FE", "fe_repo", "fe_branch", "fe_upstream"),
    ("BE", "be_repo", "be_branch", "be_upstream"),
]


def log(msg):
    print("[%s] [merge-dev] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


class Gitea:
    def __init__(self, url, token):
        self.url = url.rstrip("/") + "/api/v1"
        self.headers = {"Authorization": "token " + token, "Content-Type": "application/json",
                        "Accept": "application/json"}

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method, headers=self.headers,
                                     data=json.dumps(body).encode("utf-8") if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                text = resp.read().decode("utf-8")
                return json.loads(text) if text.strip() else {}
        except urllib.error.HTTPError as ex:
            detail = ex.read().decode("utf-8", "replace")[:300]
            raise RuntimeError("Gitea %s %s → %s %s" % (method, path, ex.code, detail))

    def open_pr(self, owner, repo, head, base):
        for page in range(1, 6):
            prs = self.call("GET", "/repos/%s/%s/pulls?state=open&limit=50&page=%d" % (owner, repo, page))
            for pr in prs:
                if pr["head"]["ref"] == head and pr["base"]["ref"] == base:
                    return pr
            if len(prs) < 50:
                return None
        return None


def owner_repo(repo_dir):
    """host:owner/repo(.git) hoặc https://host/owner/repo → (owner, repo), lấy từ remote origin của repo."""
    url = af.run(["git", "remote", "get-url", "origin"], cwd=repo_dir).strip()
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", url)
    if not m:
        raise RuntimeError("không đọc được owner/repo từ remote %s" % url)
    return m.group(1), m.group(2)


def merge_upstream(acfg, work, own, dev):
    """Merge origin/<dev> vào worktree (đang ở origin/<own>). Trả True nếu tạo merge commit mới."""
    if subprocess.run(["git", "merge-base", "--is-ancestor", "origin/" + dev, "HEAD"], cwd=work).returncode == 0:
        return False
    res = subprocess.run(["git", "merge", "-q", "-m", "Merge branch '%s' into %s" % (dev, own), "origin/" + dev],
                         cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if res.returncode == 0:
        return True
    af.resolve_conflicts(acfg, work, "merge %s vào %s" % (dev, own))
    return True

def check_build(acfg, label, work, log_path):
    if label == "FE":
        cmd = ["npm", "run", "build"]
    else:
        cmd = ["mvn", "-q", "-o", "-f", os.path.join(work, acfg["be_subdir"], "pom.xml"), "compile", "-DskipTests"]
    with open(log_path, "a", encoding="utf-8") as out:
        out.write("\n$ %s\n" % " ".join(cmd))
        out.flush()
        code = subprocess.run(cmd, cwd=work, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              env=af.build_env(acfg), timeout=1800).returncode
    if code != 0:
        raise RuntimeError("%s `%s` lỗi sau khi merge (xem %s)" % (label, " ".join(cmd[:3]), log_path))


def process(acfg, gitea, label, repo, own, dev, dry_run, log_path):
    """Trả dòng kết quả cho repo."""
    af.run(["git", "fetch", "-q", "origin", own, dev], cwd=repo)
    pending = af.run(["git", "log", "--no-merges", "--format=%h %s", "origin/%s..origin/%s" % (dev, own)],
                     cwd=repo).strip().splitlines()
    if not pending:
        return "%s: không có commit mới" % label
    tickets = sorted(set(re.findall(r"\[(SOAR-\d+)\]", "\n".join(pending))))
    work = tempfile.mkdtemp(prefix="merge-%s-" % label.lower(), dir=os.path.expanduser(acfg["root"]))
    os.rmdir(work)
    af.run(["git", "worktree", "add", "-q", "--detach", work, "origin/" + own], cwd=repo)
    try:
        if label == "FE":
            nm = os.path.join(repo, "node_modules")
            if os.path.isdir(nm):
                os.symlink(nm, os.path.join(work, "node_modules"))
        merged = merge_upstream(acfg, work, own, dev)
        check_build(acfg, label, work, log_path)
        if dry_run:
            return "%s: dry-run — %d commit (%s) sẵn sàng merge%s" % (
                label, len(pending), ", ".join(tickets) or "không có mã ticket",
                ", cần push merge %s vào %s trước" % (dev, own) if merged else "")
        if merged:
            af.run(["git", "push", "-q", "origin", "HEAD:refs/heads/" + own], cwd=work)
        owner, name = owner_repo(repo)
        pr = gitea.open_pr(owner, name, own, dev)
        if pr is None:
            body = "Tự động lúc %s (merge_to_dev).\n\n%d commit:\n%s" % (
                datetime.now().strftime("%d/%m/%Y %H:%M"), len(pending), "\n".join("- " + c for c in pending[:100]))
            pr = gitea.call("POST", "/repos/%s/%s/pulls" % (owner, name),
                            {"head": own, "base": dev, "title": own, "body": body})
        # Gitea kiểm tra khả năng merge bất đồng bộ ngay sau khi tạo / cập nhật PR: thử lại vài lần.
        last = None
        for _ in range(6):
            try:
                gitea.call("POST", "/repos/%s/%s/pulls/%d/merge" % (owner, name, pr["number"]),
                           {"Do": "merge", "delete_branch_after_merge": False})
                last = None
                break
            except RuntimeError as ex:
                last = ex
                time.sleep(10)
        if last:
            raise RuntimeError("PR #%d đã tạo nhưng merge lỗi: %s" % (pr["number"], last))
        return "%s: đã merge PR #%d %s → %s (%d commit: %s)" % (
            label, pr["number"], own, dev, len(pending), ", ".join(tickets) or "không có mã ticket")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", work], cwd=repo, check=False)


def main():
    parser = argparse.ArgumentParser(description="Merge nhánh cá nhân vào nhánh dev chung qua PR Gitea.")
    parser.add_argument("--dry-run", action="store_true", help="merge + build thử, không push / không tạo PR")
    parser.add_argument("--only", choices=["FE", "BE"], help="chỉ một repo")
    parser.add_argument("--no-deploy", action="store_true", help="không deploy + QA lên server 86 sau khi merge")
    args = parser.parse_args()

    cfg = op_watch.load_config(argparse.Namespace(config=None, interval=None, on_change=None, no_notify=False))
    acfg = dict(af.AUTOFIX_DEFAULTS)
    acfg.update(cfg.get("autofix") or {})
    os.makedirs(os.path.expanduser(acfg["root"]), exist_ok=True)

    gitea = None
    if not args.dry_run:
        g = load_gitea()
        if not g.get("token") or not g.get("url"):
            msg = "thiếu GITEA_URL / GITEA_TOKEN (.env) hoặc %s — chưa merge" % GITEA_CONFIG
            log(msg)
            af.inbox(cfg, "**MERGE DEV LỖI** " + msg)
            af.notify("[MERGE DEV LỖI]", msg)
            sys.exit(1)
        gitea = Gitea(g["url"], g["token"])

    log_path = os.path.join(os.path.expanduser(acfg["root"]),
                            "merge-dev-%s.log" % datetime.now().strftime("%m%d-%H%M"))
    results, failed = [], False
    for label, repo_key, own_key, dev_key in REPOS:
        if args.only and args.only != label:
            continue
        try:
            line = process(acfg, gitea, label, acfg[repo_key], acfg[own_key], acfg[dev_key], args.dry_run, log_path)
        except Exception as ex:  # noqa: BLE001 — repo này lỗi vẫn chạy repo kia, báo đủ
            failed = True
            line = "%s: LỖI %s" % (label, str(ex).splitlines()[0][:300])
        log(line)
        results.append(line)

    summary = " · ".join(results)
    title = "[MERGE DEV%s]" % (" LỖI" if failed else "")
    af.inbox(cfg, "**MERGE DEV%s** %s" % (" LỖI" if failed else "", summary))
    af.notify(title, summary)
    # Sau merge: deploy những gì mới trên dev-ss / develop-ss lên server 86 rồi QA từng ticket (deploy_qa.py).
    if not args.dry_run and not args.no_deploy:
        import deploy_qa
        try:
            deploy_qa.run_all()
        except Exception as ex:  # noqa: BLE001
            log("deploy + QA lỗi: %s" % ex)
            af.inbox(cfg, "**DEPLOY 86 LỖI** %s" % str(ex).splitlines()[0][:300])
            af.notify("[DEPLOY 86 LỖI]", str(ex)[:200])
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
