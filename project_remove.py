#!/usr/bin/env python3
"""
project_remove: gỡ một dự án khỏi tool cùng dữ liệu tool tạo ra cho dự án đó.

  python3 project_remove.py <mã dự án>                  # xem trước (không xoá gì)
  python3 project_remove.py <mã dự án> --yes            # gỡ (giữ lượt còn commit chưa push)
  python3 project_remove.py <mã dự án> --yes --drop-unpushed   # gỡ cả lượt còn commit chưa push

Gỡ:
  - cấu hình dự án trong config.json (bản cũ sao lưu config.json.bak-<giờ>);
  - lượt autofix / QA của ticket <PREFIX>-n: thư mục ~/autofix/<PREFIX>-n-…, worktree + nhánh auto/<lượt> trong repo;
  - trạng thái (.status), hàng chờ Claude xem lại (.pending-followup), khoá / log (.<PREFIX>-n.lock, .recover-…,
    .dashboard-…), log deploy của dự án, thư mục deploy-<repo> và mốc deploy (deploy-state.json) của repo dự án;
  - báo cáo tổng hợp (report_dir/<PREFIX>-n-….md);
  - token / phiên đăng nhập app của dự án trong ~/.cache/op-watch (chỉ dự án không kiểu SOAR — dự án kiểu SOAR dùng
    chung file với dự án SOAR mặc định nên giữ);
  - chat Telegram đang gán cho dự án → về dự án mặc định.
KHÔNG đụng: thư mục repo (code của bạn), nhánh của bạn / remote, dữ liệu trên OpenProject, dòng inbox cũ.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402
import projects as P  # noqa: E402
import repos as R  # noqa: E402

HOME = os.path.expanduser("~")


def _short(p):
    return p.replace(HOME, "~", 1) if p.startswith(HOME) else p


def _size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for dp, _, fs in os.walk(path):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def _ctx():
    import config_page
    import op_autofix as af
    cfg = config_page._cfg()
    acfg = dict(af.AUTOFIX_DEFAULTS, **(cfg.get("autofix") or {}))
    return cfg, acfg, os.path.expanduser(acfg["root"])


def plan(key):
    """Những gì sẽ gỡ cho dự án `key` (không xoá gì)."""
    import cleanup
    import op_autofix as af
    cfg, acfg, root = _ctx()
    items = P.projects(cfg)
    proj = P.get(cfg, key)
    if not proj:
        raise ValueError("không có dự án %s" % key)
    if len(items) < 2:
        raise ValueError("không gỡ được dự án cuối cùng — tool cần ít nhất một dự án")
    pre = proj["prefix"]
    rx = re.compile(r"^%s-\d+(?:[-.]|$)" % re.escape(pre))  # SOAR-12-…, SOAR-12.json, SOAR-12.lock (không khớp SOARX-)
    sacfg = P.scope_acfg(acfg, proj)
    out = {"key": proj["key"], "name": proj["name"], "prefix": pre, "default": items[0]["key"] == proj["key"],
           "active": [], "runs": [], "files": [], "deploy_state": [], "chats": [], "repos": []}

    for s in af.load_statuses(root):
        if rx.match(s.get("display_id") or "") and s.get("state") in af.ACTIVE_STATES and af.status_alive(s):
            out["active"].append(s["display_id"])

    try:
        repo_list = R.repos(sacfg) if (proj["repos"] or proj["_legacy"]) else []
    except ValueError:
        repo_list = []
    out["repos"] = [{"key": r["key"], "path": _short(r["path"])} for r in repo_list]
    # Repo dùng chung với dự án khác (cùng thư mục) thì không xoá mốc deploy / thư mục deploy của nó.
    other_paths = set()
    for p in items:
        if p["key"] != proj["key"] and (p["repos"] or p["_legacy"]):
            try:
                other_paths |= {os.path.realpath(r["path"]) for r in R.repos(P.scope_acfg(acfg, p))}
            except ValueError:
                pass

    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        path = os.path.join(root, name)
        if os.path.isdir(path) and rx.match(name):
            qa = "-qa-" in name
            unp = False if qa else cleanup.unpushed(sacfg, root, name)
            out["runs"].append({"name": name, "path": path, "qa": qa, "unpushed": bool(unp), "size": _size(path)})

    def add(path, why):
        if os.path.exists(path):
            out["files"].append({"path": path, "why": why, "size": _size(path)})

    sd = os.path.join(root, ".status")
    for f in sorted(os.listdir(sd)) if os.path.isdir(sd) else []:
        if rx.match(f):
            add(os.path.join(sd, f), "trạng thái")
    pd = os.path.join(root, ".pending-followup")
    for f in sorted(os.listdir(pd)) if os.path.isdir(pd) else []:
        if rx.match(f):
            add(os.path.join(pd, f), "hàng chờ xem lại")
    for f in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if f.startswith(".") and rx.match(re.sub(r"^\.(recover-|dashboard-)?", "", f)):
            add(os.path.join(root, f), "khoá / log")
    for f in glob.glob(os.path.join(root, "deploy-%s-*.log" % proj["key"])):
        add(f, "log deploy")
    for r in repo_list:
        if os.path.realpath(r["path"]) in other_paths:
            continue
        add(os.path.join(root, "deploy-" + r["key"]), "thư mục build deploy")
    report_dir = os.path.expanduser(acfg.get("report_dir") or "")
    if report_dir and os.path.isdir(report_dir):
        for f in sorted(os.listdir(report_dir)):
            if rx.match(f):
                add(os.path.join(report_dir, f), "báo cáo tổng hợp")
    # Token / phiên app: dự án kiểu SOAR dùng chung soar_token_* với dự án SOAR → giữ.
    dcfg = P.deploy_cfg(proj, {})
    if ((dcfg.get("auth") or {}).get("kind") or "soar") != "soar" and not proj["_legacy"]:
        tag = re.sub(r"[^\w.-]", "_", proj["key"])
        for pat in ("api_token_%s_*" % tag, "ui-%s-*" % proj["key"]):
            for f in glob.glob(os.path.join(envconf.CACHE_DIR, pat)):
                add(f, "token / phiên đăng nhập app")

    try:
        with open(os.path.join(root, "deploy-state.json"), encoding="utf-8") as f:
            ds = json.load(f)
        out["deploy_state"] = [r["key"] for r in repo_list if r["key"] in ds
                               and os.path.realpath(r["path"]) not in other_paths]
    except (OSError, ValueError):
        pass
    try:
        import tg_requests
        out["chats"] = [c.get("title") or c.get("id") or "?" for c in tg_requests.settings().get("chats") or []
                        if (c.get("project") or "") in (proj["key"], proj["prefix"])]
    except Exception:  # noqa: BLE001 — chưa cấu hình Telegram
        pass
    out["qa_keys"] = {}
    for fn in ("qa-history.json", "qa-fail-count.json", "qa-pending.json"):
        try:
            with open(os.path.join(root, fn), encoding="utf-8") as f:
                d = json.load(f)
            ks = [k for k in d if rx.match(k)] if isinstance(d, dict) else []
            if ks:
                out["qa_keys"][fn] = ks
        except (OSError, ValueError):
            pass
    out["total_size"] = sum(x["size"] for x in out["runs"] + out["files"])
    return out


def remove(key, drop_unpushed=False):
    """Gỡ dự án theo plan(). Trả (thông báo, chi tiết)."""
    import op_autofix as af
    import config_page
    pl = plan(key)
    if pl["active"]:
        raise ValueError("dự án còn lượt đang chạy: %s — huỷ hoặc chờ xong rồi gỡ" % ", ".join(pl["active"]))
    cfg, acfg, root = _ctx()
    kept, removed, errors = [], 0, []
    # 1) Lượt chạy: xoá worktree + nhánh auto/<lượt> trong repo TRƯỚC khi bỏ dự án khỏi config (af.clean cần repo).
    af._CFG = cfg
    for r in pl["runs"]:
        if r["unpushed"] and not drop_unpushed:
            kept.append(r["name"])
            continue
        try:
            if r["qa"]:
                shutil.rmtree(r["path"], ignore_errors=True)
            else:
                af.clean(acfg, r["name"])
            removed += 1
        except Exception as ex:  # noqa: BLE001
            errors.append("%s: %s" % (r["name"], ex))
    for rp in pl["repos"]:
        subprocess.run(["git", "-C", os.path.expanduser(rp["path"]), "worktree", "prune"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 2) File / thư mục khác
    for f in pl["files"]:
        try:
            if os.path.isdir(f["path"]) and not os.path.islink(f["path"]):
                shutil.rmtree(f["path"])
            elif os.path.exists(f["path"]):
                os.remove(f["path"])
        except OSError as ex:
            errors.append("%s: %s" % (_short(f["path"]), ex))
    # 3) Mốc deploy của repo dự án
    if pl["deploy_state"]:
        path = os.path.join(root, "deploy-state.json")
        try:
            with open(path, encoding="utf-8") as f:
                ds = json.load(f)
            for k in pl["deploy_state"]:
                ds.pop(k, None)
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(ds, f, ensure_ascii=False, indent=2)
            os.replace(path + ".tmp", path)
        except (OSError, ValueError) as ex:
            errors.append("deploy-state.json: %s" % ex)
    # 3b) Lịch sử / hàng chờ QA của ticket thuộc dự án
    for fn, ks in pl["qa_keys"].items():
        path = os.path.join(root, fn)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            for k in ks:
                d.pop(k, None)
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(path + ".tmp", path)
        except (OSError, ValueError) as ex:
            errors.append("%s: %s" % (fn, ex))
    # 4) Chat Telegram gán cho dự án → dự án mặc định
    if pl["chats"]:
        try:
            import tg_requests
            chats = tg_requests.settings().get("chats") or []
            for c in chats:
                if (c.get("project") or "") in (pl["key"], pl["prefix"]):
                    c["project"] = ""
            tg_requests.save_settings({"chats": chats})
        except Exception as ex:  # noqa: BLE001
            errors.append("Telegram: %s" % ex)
    # 5) Cấu hình (sao lưu config.json.bak-<giờ>)
    data = envconf._read_json(config_page._cfg_path())
    items = config_page._materialize(data, cfg)
    data["projects"] = [p for p in items if p.get("key") != pl["key"]]
    if (data.get("autofix") or {}).get("repos"):
        data["autofix"]["repos_before_projects"] = data["autofix"].pop("repos")
    config_page._write_cfg(data)

    msg = "Đã gỡ dự án %s: %d lượt chạy, %d file / thư mục (%.0f MB)" % (
        pl["name"], removed, len(pl["files"]), pl["total_size"] / 1e6)
    if kept:
        msg += " · GIỮ %d lượt còn commit chưa push: %s" % (len(kept), ", ".join(kept))
    if pl["chats"]:
        msg += " · %d chat Telegram về dự án mặc định" % len(pl["chats"])
    if errors:
        msg += " · lỗi: " + "; ".join(errors[:5])
    return msg, {"kept": kept, "errors": errors}


def main():
    ap = argparse.ArgumentParser(description="Gỡ dự án khỏi op-watch cùng dữ liệu liên quan")
    ap.add_argument("key")
    ap.add_argument("--yes", action="store_true", help="gỡ thật (không có thì chỉ xem trước)")
    ap.add_argument("--drop-unpushed", action="store_true", help="xoá cả lượt còn commit chưa push")
    a = ap.parse_args()
    try:
        pl = plan(a.key)
        print("Dự án %s (%s, tiền tố %s)%s" % (pl["name"], pl["key"], pl["prefix"], " — đang là dự án MẶC ĐỊNH" if pl["default"] else ""))
        if pl["active"]:
            print("  ĐANG CHẠY: %s" % ", ".join(pl["active"]))
        for r in pl["runs"]:
            print("  lượt  %-40s %6.1f MB%s" % (r["name"], r["size"] / 1e6, "  ⚠ commit chưa push" if r["unpushed"] else ""))
        for f in pl["files"]:
            print("  file  %-60s %s" % (_short(f["path"]), f["why"]))
        if pl["deploy_state"]:
            print("  mốc deploy: %s" % ", ".join(pl["deploy_state"]))
        if pl["chats"]:
            print("  chat Telegram về mặc định: %s" % ", ".join(pl["chats"]))
        for fn, ks in pl["qa_keys"].items():
            print("  %s: %d mục" % (fn, len(ks)))
        print("  Tổng %.1f MB · repo giữ nguyên: %s" % (pl["total_size"] / 1e6, ", ".join(r["path"] for r in pl["repos"]) or "-"))
        if a.yes:
            print(remove(a.key, a.drop_unpushed)[0])
    except ValueError as ex:
        sys.exit("Lỗi: %s" % ex)


if __name__ == "__main__":
    main()
