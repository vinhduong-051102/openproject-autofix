#!/usr/bin/env python3
"""Chuyển .docx → markdown (chỉ dùng thư viện chuẩn): tiêu đề, đoạn, danh sách, bảng, khối code.

Dùng cho công cụ soạn ticket: Claude headless không đọc được .docx, nên file đính kèm .docx được
chuyển sẵn sang .md bên cạnh để prompt trỏ tới.

    python3 docx_text.py file.docx [out.md]
"""
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
CODE_STYLE = re.compile(r"code|source|preformat|html|macro|plain ?text", re.I)
CODE_FONT = re.compile(r"courier|consolas|mono|menlo|lucida console", re.I)


def _style_names(z):
    try:
        root = ET.fromstring(z.read("word/styles.xml"))
    except KeyError:
        return {}
    out = {}
    for st in root.iter(W + "style"):
        sid = st.get(W + "styleId")
        name = st.find(W + "name")
        out[sid] = name.get(W + "val") if name is not None else sid
    return out


def _run_text(r):
    parts = []
    for el in r:
        tag = el.tag[len(W):] if el.tag.startswith(W) else ""
        if tag == "t":
            parts.append(el.text or "")
        elif tag == "tab":
            parts.append("\t")
        elif tag in ("br", "cr"):
            parts.append("\n")
    return "".join(parts)


def _para(p, styles):
    """→ (kiểu, text): kiểu = h1..h6 | li | code | p."""
    ppr = p.find(W + "pPr")
    style, is_list = "", False
    if ppr is not None:
        ps = ppr.find(W + "pStyle")
        if ps is not None:
            style = styles.get(ps.get(W + "val"), ps.get(W + "val") or "")
        is_list = ppr.find(W + "numPr") is not None
    texts, mono = [], True
    for r in p.iter(W + "r"):
        t = _run_text(r)
        if not t:
            continue
        rf = r.find(W + "rPr/" + W + "rFonts")
        font = (rf.get(W + "ascii") or rf.get(W + "hAnsi") or "") if rf is not None else ""
        if t.strip() and not CODE_FONT.search(font):
            mono = False
        texts.append(t)
    text = "".join(texts)
    m = re.match(r"(?i)heading\s*(\d)|title", style or "")
    if m and text.strip():
        return "h%d" % (int(m.group(1)) if m.group(1) else 1), text.strip()
    if CODE_STYLE.search(style or "") or (mono and text.strip()):
        return "code", text
    if is_list or re.search(r"(?i)list", style or ""):
        return "li", text.strip()
    return "p", text.strip()


def _cell_text(tc, styles):
    lines = [_para(p, styles)[1] for p in tc.iter(W + "p")]
    return "<br>".join(l.replace("|", "\\|").replace("\n", "<br>") for l in lines if l.strip())


def convert(path):
    with zipfile.ZipFile(path) as z:
        styles = _style_names(z)
        body = ET.fromstring(z.read("word/document.xml")).find(W + "body")
    out, code = [], []

    def flush_code():
        if code:
            out.extend(["```", *code, "```", ""])
            code.clear()

    for el in body:
        if el.tag == W + "p":
            kind, text = _para(el, styles)
            if kind == "code":
                code.append(text.rstrip("\n"))
                continue
            flush_code()
            if not text:
                continue
            if kind.startswith("h"):
                out.extend(["#" * int(kind[1]) + " " + text, ""])
            elif kind == "li":
                out.append("- " + text)
            else:
                out.extend([text, ""])
        elif el.tag == W + "tbl":
            flush_code()
            rows = [[_cell_text(tc, styles) for tc in tr.findall(W + "tc")] for tr in el.findall(W + "tr")]
            rows = [r for r in rows if any(c for c in r)]
            if not rows:
                continue
            n = max(len(r) for r in rows)
            rows = [r + [""] * (n - len(r)) for r in rows]
            out.append("| " + " | ".join(rows[0]) + " |")
            out.append("|" + "---|" * n)
            out.extend("| " + " | ".join(r) + " |" for r in rows[1:])
            out.append("")
    flush_code()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    md = convert(sys.argv[1])
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w", encoding="utf-8") as f:
            f.write(md)
    else:
        sys.stdout.write(md)
