#!/usr/bin/env python3
"""
figma: đọc thiết kế từ link figma.com trong ticket / comment (Figma REST API, FIGMA_TOKEN trong .env).

Với mỗi link (figma.com/design|file|proto/<key>/...?node-id=1-2):
  - ảnh PNG của đúng frame / node trong link (scale 2) → Claude nhìn bố cục
  - thông số: kích thước, vị trí trong frame, auto-layout (padding / gap), màu nền / viền, bo góc, bóng, chữ (nội dung,
    font, cỡ, độ đậm, line-height) → sửa đúng pixel
Link không có node-id: liệt kê các trang / frame của file, chụp tối đa FRAMES_NO_NODE frame đầu của trang đầu.
Kết quả cache theo (file, node, phiên bản file) trong <root>/.figma-cache để không gọi API lặp lại.

    figma.py <link>            # thử: in thông số, lưu ảnh vào thư mục hiện tại/figma
"""
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf  # noqa: E402

API = "https://api.figma.com/v1"
LINK_RX = re.compile(r"https?://(?:www\.)?figma\.com/(?:design|file|proto|board)/([A-Za-z0-9]{10,})[^\s)\]\"'<>]*")
MAX_LINKS = 6
MAX_SPEC_LINES = 900
MAX_DEPTH = 12
FRAMES_NO_NODE = 3
CACHE_VER = "v2"
# Lớp "lá" (hình / chữ / ảnh…): chụp riêng chỉ ra một mảng màu → lấy frame màn hình chứa nó.
LEAF_TYPES = {"RECTANGLE", "VECTOR", "ELLIPSE", "LINE", "STAR", "REGULAR_POLYGON", "BOOLEAN_OPERATION", "TEXT", "SLICE"}
CONTAINER_TYPES = ("FRAME", "COMPONENT", "COMPONENT_SET", "INSTANCE", "SECTION")
MAX_IMG_SIDE = 2400  # ảnh dài hơn (px, sau scale) thì hạ scale cho Claude đọc được
TIMEOUT = 60


def token():
    return envconf.get("FIGMA_TOKEN") or ""


def find_links(text):
    """[(file_key, node_id | None, url)] — bỏ trùng, giữ thứ tự xuất hiện."""
    out, seen = [], set()
    for m in LINK_RX.finditer(text or ""):
        url = m.group(0).rstrip(".,;")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url.replace("&amp;", "&")).query)
        node = (q.get("node-id") or [None])[0]
        node = node.replace("-", ":") if node else None
        k = (m.group(1), node)
        if k not in seen:
            seen.add(k)
            out.append((m.group(1), node, url))
    return out[:MAX_LINKS]


CACHE_TTL = int(envconf.get("FIGMA_CACHE_HOURS") or 24) * 3600


def _blocked():
    """Figma đang chặn (429) tới thời điểm nào (epoch) — 0 nếu không chặn."""
    try:
        until = float(open(os.path.join(_cache_dir(), "blocked-until")).read().strip())
    except (OSError, ValueError):
        return 0
    return until if until > time.time() else 0


def _get(path, tok):
    until = _blocked()
    if until:
        raise RuntimeError("Figma đang giới hạn API tới %s (ghế Figma loại View/Collab chỉ được gọi rất ít; dùng ghế "
                           "Dev / Full hoặc đính kèm ảnh thiết kế)" % time.strftime("%H:%M %d/%m", time.localtime(until)))
    req = urllib.request.Request(API + path, headers={"X-Figma-Token": tok})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        body = ex.read().decode("utf-8", "replace")[:200]
        if ex.code in (401, 403):
            raise RuntimeError("Figma từ chối (%s): token sai / hết hạn hoặc không có quyền xem file này" % ex.code)
        if ex.code == 404:
            raise RuntimeError("Figma: không thấy file / node (404)")
        if ex.code == 429:
            # Retry-After có thể tới vài ngày (ghế View/Collab: x-figma-rate-limit-type=low) → nhớ để không gọi lại.
            try:
                wait = float(ex.headers.get("Retry-After") or 60)
            except ValueError:
                wait = 60
            with open(os.path.join(_cache_dir(), "blocked-until"), "w") as f:
                f.write(str(time.time() + wait))
            kind = ex.headers.get("x-figma-rate-limit-type") or ""
            raise RuntimeError("Figma giới hạn API (429%s) tới %s%s" % (
                ", loại " + kind if kind else "", time.strftime("%H:%M %d/%m", time.localtime(time.time() + wait)),
                " — ghế Figma View/Collab chỉ được gọi rất ít; nên dùng token của tài khoản có ghế Dev / Full"
                if kind == "low" else ""))
        raise RuntimeError("Figma API lỗi %s: %s" % (ex.code, body))


def _download(url, path):
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r, open(path + ".tmp", "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(path + ".tmp", path)


# ---- thông số node → markdown ---------------------------------------------------------------------------------------

def _hex(c, opacity=1.0):
    if not c:
        return ""
    r, g, b = (round(c.get(k, 0) * 255) for k in "rgb")
    a = c.get("a", 1) * (opacity if opacity is not None else 1)
    s = "#%02X%02X%02X" % (r, g, b)
    return s if a >= 0.999 else "%s @%d%%" % (s, round(a * 100))


def _paints(paints):
    out = []
    for p in paints or []:
        if p.get("visible") is False:
            continue
        t = p.get("type")
        if t == "SOLID":
            out.append(_hex(p.get("color"), p.get("opacity", 1)))
        elif t and t.startswith("GRADIENT"):
            out.append("%s(%s)" % (t.lower(), " → ".join(_hex(s.get("color")) for s in p.get("gradientStops") or [])))
        elif t == "IMAGE":
            out.append("ảnh")
    return ", ".join(x for x in out if x)


def _num(v):
    return ("%g" % round(v, 1)) if isinstance(v, (int, float)) else str(v)


def _describe(n, origin):
    """Một dòng mô tả node (không kèm thụt lề)."""
    parts = ["**%s** `%s`" % (n.get("name", "?").replace("*", ""), n.get("type"))]
    bb = n.get("absoluteBoundingBox") or {}
    if bb:
        parts.append("%s×%s @(%s,%s)" % (_num(bb.get("width")), _num(bb.get("height")),
                                         _num(bb.get("x", 0) - origin[0]), _num(bb.get("y", 0) - origin[1])))
    if n.get("layoutMode") in ("HORIZONTAL", "VERTICAL"):
        pad = [n.get(k, 0) for k in ("paddingTop", "paddingRight", "paddingBottom", "paddingLeft")]
        parts.append("auto-layout %s gap %s padding %s%s" % (
            "ngang" if n["layoutMode"] == "HORIZONTAL" else "dọc", _num(n.get("itemSpacing", 0)),
            " ".join(_num(x) for x in pad),
            " căn %s/%s" % (n.get("primaryAxisAlignItems", "MIN"), n.get("counterAxisAlignItems", "MIN"))))
    fill = _paints(n.get("fills"))
    if fill and n.get("type") != "TEXT":
        parts.append("nền " + fill)
    stroke = _paints(n.get("strokes"))
    if stroke:
        parts.append("viền %s %spx" % (stroke, _num(n.get("strokeWeight", 1))))
    if n.get("cornerRadius"):
        parts.append("bo góc %s" % _num(n["cornerRadius"]))
    elif n.get("rectangleCornerRadii"):
        parts.append("bo góc %s" % " ".join(_num(x) for x in n["rectangleCornerRadii"]))
    for e in n.get("effects") or []:
        if e.get("visible") is False:
            continue
        if "SHADOW" in (e.get("type") or ""):
            off = e.get("offset") or {}
            parts.append("bóng %s %s,%s blur %s" % (_hex(e.get("color")), _num(off.get("x", 0)), _num(off.get("y", 0)),
                                                   _num(e.get("radius", 0))))
        elif "BLUR" in (e.get("type") or ""):
            parts.append("blur %s" % _num(e.get("radius", 0)))
    if n.get("opacity") is not None and n.get("opacity") < 1:
        parts.append("mờ %d%%" % round(n["opacity"] * 100))
    if n.get("type") == "TEXT":
        st = n.get("style") or {}
        parts.append("chữ “%s”" % (n.get("characters") or "").replace("\n", " ⏎ ")[:200])
        parts.append("font %s %s/%s%s" % (st.get("fontFamily", "?"), _num(st.get("fontWeight", "")),
                                          _num(st.get("fontSize", "")),
                                          " lh %s" % _num(st["lineHeightPx"]) if st.get("lineHeightPx") else ""))
        if fill:
            parts.append("màu " + fill)
        if st.get("textAlignHorizontal") and st["textAlignHorizontal"] != "LEFT":
            parts.append("căn " + st["textAlignHorizontal"].lower())
    if n.get("type") == "INSTANCE" and n.get("componentId"):
        parts.append("component")
    return " · ".join(parts)


VECTOR_TYPES = {"VECTOR", "BOOLEAN_OPERATION", "ELLIPSE", "STAR", "REGULAR_POLYGON", "LINE"}


def _vector_only(n):
    """(số vector, {màu}) nếu mọi lá bên dưới là vector (icon / logo) — không thì None."""
    if not n.get("children"):
        return None
    count, colors = 0, set()
    stack = list(n["children"])
    while stack:
        c = stack.pop()
        if c.get("children") and c.get("type") not in VECTOR_TYPES:
            stack += c["children"]
            continue
        if c.get("type") not in VECTOR_TYPES:
            return None
        count += 1
        col = _paints(c.get("fills"))
        if col:
            colors.add(col)
    return (count, colors) if count else None


def spec(node):
    """Cây node → các dòng markdown (thụt lề theo cấp), tối đa MAX_SPEC_LINES dòng."""
    bb = node.get("absoluteBoundingBox") or {}
    origin = (bb.get("x", 0), bb.get("y", 0))
    lines = []

    def walk(n, depth):
        if len(lines) >= MAX_SPEC_LINES or n.get("visible") is False:
            return
        vec = _vector_only(n)
        if vec and n.get("type") not in LEAF_TYPES:
            lines.append("  " * depth + "- " + _describe(n, origin) + " · icon / hình vẽ (%d vector, màu %s)" % (
                vec[0], ", ".join(sorted(vec[1])[:4]) or "?"))
            return
        lines.append("  " * depth + "- " + _describe(n, origin))
        if depth < MAX_DEPTH:
            for c in n.get("children") or []:
                walk(c, depth + 1)
    walk(node, 0)
    if len(lines) >= MAX_SPEC_LINES:
        lines.append("- … (cắt bớt, frame quá lớn — xem ảnh)")
    return lines


# ---- tải 1 link -------------------------------------------------------------------------------------------------------

def _cache_dir():
    d = os.path.join(envconf.autofix_root(), ".figma-cache")
    os.makedirs(d, exist_ok=True)
    return d


def _screen_of(file_key, node_id, tok):
    """Frame màn hình chứa node: /files?ids= trả cây từ gốc tới node → frame cấp trang (con trực tiếp của page);
    không có thì frame / component gần nhất bên trên."""
    doc = _get("/files/%s?ids=%s" % (file_key, urllib.parse.quote(node_id)), tok).get("document") or {}

    def path(n, trail):
        if n.get("id") == node_id:
            return trail
        for c in n.get("children") or []:
            r = path(c, trail + [c])
            if r is not None:
                return r
        return None
    for page in doc.get("children") or []:
        trail = path(page, [])
        if trail is None:
            continue
        # trail: [con của page, …, node]
        anc = trail[:-1]
        if anc and anc[0].get("type") in CONTAINER_TYPES:
            return anc[0]["id"]
        near = [a for a in anc if a.get("type") in CONTAINER_TYPES]
        return near[-1]["id"] if near else None
    return None


def fetch_link(file_key, node_id, out_dir, tok=None):
    """Tải ảnh + thông số. Trả {"title", "images": [đường dẫn], "spec": đường dẫn .md, "nodes": [...]}."""
    tok = tok or token()
    if not tok:
        raise RuntimeError("chưa có FIGMA_TOKEN (Cấu hình → .env)")
    os.makedirs(out_dir, exist_ok=True)
    latest = os.path.join(_cache_dir(), "latest-" + hashlib.sha1(("%s|%s|%s" % (CACHE_VER, file_key, node_id)).encode())
                          .hexdigest()[:12])
    prev = open(latest, encoding="utf-8").read().strip() if os.path.exists(latest) else ""
    prev_ok = prev and os.path.exists(os.path.join(prev, "result.json"))
    # Bản đã tải trong CACHE_TTL: dùng luôn, không gọi API (hạn mức Figma rất ít với ghế View/Collab).
    if prev_ok and time.time() - os.path.getmtime(os.path.join(prev, "result.json")) < CACHE_TTL:
        return _copy_out(json.load(open(os.path.join(prev, "result.json"), encoding="utf-8")), prev, out_dir)
    try:
        return _fetch_fresh(file_key, node_id, out_dir, tok, latest)
    except RuntimeError as ex:
        if prev_ok:  # 429 / mạng lỗi: dùng bản tải gần nhất
            return _copy_out(json.load(open(os.path.join(prev, "result.json"), encoding="utf-8")), prev, out_dir,
                             note="(Figma API lỗi: %s — dùng bản tải lúc %s)" % (
                                 str(ex)[:120], time.strftime("%H:%M %d/%m", time.localtime(
                                     os.path.getmtime(os.path.join(prev, "result.json"))))))
        raise


def _fetch_fresh(file_key, node_id, out_dir, tok, latest):
    import secrets
    cache = os.path.join(_cache_dir(), "%s-%s" % (hashlib.sha1(("%s|%s" % (file_key, node_id)).encode()).hexdigest()[:10],
                                                  secrets.token_hex(3)))
    title = file_key
    os.makedirs(cache, exist_ok=True)
    if node_id:
        data = _get("/files/%s/nodes?ids=%s" % (file_key, urllib.parse.quote(node_id)), tok)
        title = data.get("name") or title
        nodes = [v["document"] for v in (data.get("nodes") or {}).values() if v and v.get("document")]
        if not nodes:
            raise RuntimeError("Figma: không thấy node %s trong file" % node_id)
        picked = nodes[0]
        if picked.get("type") in LEAF_TYPES or not picked.get("children"):
            frame = _screen_of(file_key, node_id, tok)
            if frame and frame != node_id:
                data = _get("/files/%s/nodes?ids=%s" % (file_key, urllib.parse.quote(frame)), tok)
                up = [v["document"] for v in (data.get("nodes") or {}).values() if v and v.get("document")]
                if up:
                    up[0]["_picked"] = "%s `%s`" % (picked.get("name"), picked.get("type"))
                    nodes = up
    else:
        full = _get("/files/%s?depth=2" % file_key, tok)
        title, doc = full.get("name") or title, full["document"]
        pages = doc.get("children") or []
        frames = [c for c in (pages[0].get("children") or []) if c.get("type") in ("FRAME", "SECTION", "COMPONENT")] \
            if pages else []
        ids = ",".join(f["id"] for f in frames[:FRAMES_NO_NODE])
        data = _get("/files/%s/nodes?ids=%s" % (file_key, urllib.parse.quote(ids)), tok) if ids else {"nodes": {}}
        nodes = [v["document"] for v in (data.get("nodes") or {}).values() if v and v.get("document")]
    big = max([max((n.get("absoluteBoundingBox") or {}).get("width") or 0,
                   (n.get("absoluteBoundingBox") or {}).get("height") or 0) for n in nodes] + [1])
    scale = 2 if big * 2 <= MAX_IMG_SIDE else max(0.5, round(MAX_IMG_SIDE / big, 2))
    imgs = _get("/images/%s?ids=%s&format=png&scale=%s" % (
        file_key, urllib.parse.quote(",".join(n["id"] for n in nodes)), scale), tok).get("images") or {}
    res = {"title": title, "nodes": []}
    for n in nodes:
        safe = re.sub(r"[^\w-]+", "_", n["id"])
        png = None
        if imgs.get(n["id"]):
            png = "%s_%s.png" % (file_key[:8], safe)
            try:
                _download(imgs[n["id"]], os.path.join(cache, png))
            except (urllib.error.URLError, OSError):
                png = None
        bb = n.get("absoluteBoundingBox") or {}
        res["nodes"].append({"id": n["id"], "name": n.get("name"), "type": n.get("type"), "png": png,
                             "size": "%s×%s" % (_num(bb.get("width", 0)), _num(bb.get("height", 0))),
                             "picked": n.get("_picked"), "spec": spec(n)})
    with open(os.path.join(cache, "result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    with open(latest, "w", encoding="utf-8") as f:
        f.write(cache)
    return _copy_out(res, cache, out_dir)


def _copy_out(res, cache, out_dir, note=""):
    os.makedirs(out_dir, exist_ok=True)
    out = {"title": res["title"] + (" " + note if note else ""), "images": [], "nodes": res["nodes"]}
    for n in res["nodes"]:
        if n.get("png") and os.path.exists(os.path.join(cache, n["png"])):
            dst = os.path.join(out_dir, n["png"])
            shutil.copy(os.path.join(cache, n["png"]), dst)
            out["images"].append(dst)
            n["png_path"] = dst
    return out


def ticket_section(text, out_dir):
    """Đọc mọi link Figma trong text (mô tả + comment) → khối markdown thêm vào ticket.md ("" nếu không có link)."""
    links = find_links(text)
    if not links:
        return ""
    lines = ["", "## Thiết kế Figma (tool đã tải qua Figma API — làm theo đúng thông số dưới đây)", ""]
    if not token():
        lines.append("Ticket có %d link Figma nhưng chưa cấu hình FIGMA_TOKEN nên không đọc được: %s"
                     % (len(links), ", ".join(u for _, _, u in links)))
        return "\n".join(lines) + "\n"
    for key, node, url in links:
        lines.append("### %s" % url)
        try:
            r = fetch_link(key, node, out_dir)
        except Exception as ex:  # noqa: BLE001 — một link lỗi không chặn ticket
            lines += ["(không đọc được: %s)" % str(ex)[:200], ""]
            continue
        lines.append("File: %s" % r["title"])
        for n in r["nodes"]:
            lines.append("")
            lines.append("#### %s `%s` (%s)" % (n.get("name"), n.get("type"), n.get("size")))
            if n.get("picked"):
                lines.append("(Link trỏ vào lớp %s nằm trong màn hình này — tool lấy cả màn hình; phần cần sửa thường quanh "
                             "lớp đó, đọc kỹ yêu cầu trong ticket để biết vùng nào.)" % n["picked"])
            if n.get("png_path"):
                lines.append("Ảnh (đọc bằng Read): %s" % n["png_path"])
            lines.append("Thông số (kích thước · vị trí trong frame · auto-layout · màu · font cỡ/độ đậm):")
            lines += n["spec"]
        lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sec = ticket_section(" ".join(sys.argv[1:]), os.path.join(os.getcwd(), "figma"))
    print(sec or "không thấy link Figma")
