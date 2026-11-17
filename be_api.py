#!/usr/bin/env python3
"""Tra cứu API BE (Spring) từ mã nguồn controller — cho QA Gemini dựng đúng request.

Bundle FE chỉ có đường dẫn gốc (vd CHANGE_USER:"/api/v1/cases/swap-user"), không có phương thức HTTP, biến {id} trên
đường dẫn hay cấu trúc body. Gemini QA đoán sai rồi nhận 400/404/405 tới hết vòng (SOAR-1437: PUT /swap-user thiếu
/{id}; SOAR-1497: POST thay vì PUT). Module này đọc controller + DTO của repo BE ở một git ref (nhánh đang deploy, không
đụng working tree) và trả: phương thức, đường dẫn đầy đủ, tham số path / query, body JSON (tên trường JSON thật theo
@JsonProperty / @JsonNaming, trường bắt buộc kèm thông báo validate, enum).

    be_api.py "swap-user"                       # tìm theo đoạn đường dẫn / tên hàm / tên controller
    be_api.py "PUT /api/v1/cases/swap-user/1553" # khớp đường dẫn cụ thể (số khớp {biến})
    be_api.py SwapUserImplement                  # xem trường của một DTO
    be_api.py --repo /path/BE --ref origin/x "…"
"""
import json
import os
import re
import subprocess
import sys

CACHE_DIR = os.path.expanduser(os.environ.get("OP_WATCH_CACHE_DIR") or "~/.cache/op-watch")
MAPPING_RE = re.compile(r"@(Get|Post|Put|Patch|Delete|Request)Mapping\b")
SKIP_PARAM_TYPES = {
    "HttpServletRequest", "HttpServletResponse", "Pageable", "Principal", "Authentication", "Model", "BindingResult",
    "MultipartHttpServletRequest", "Locale", "HttpHeaders", "UriComponentsBuilder", "Sort", "WebRequest",
}
SIMPLE_TYPES = {
    "String", "Long", "long", "Integer", "int", "Boolean", "boolean", "Double", "double", "Float", "float", "Short",
    "short", "Byte", "byte", "BigDecimal", "BigInteger", "LocalDate", "LocalDateTime", "Instant", "Date", "UUID",
    "Object", "Character", "char", "MultipartFile", "OffsetDateTime", "ZonedDateTime", "JsonNode", "Map",
}
VALIDATION = ("NotNull", "NotBlank", "NotEmpty", "Size", "Min", "Max", "Pattern", "Email", "Positive",
              "PositiveOrZero", "Length", "Range", "DecimalMin", "DecimalMax")
_MEM = {}
ID_LIKE = re.compile(r"^(\d+|[0-9a-fA-F-]{8,}|<[^>]*>|\{[^}]*\}|[A-Z]{2}\d{4,})$")


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          universal_newlines=True, errors="replace").stdout


def _resolve(repo, ref):
    for r in ([ref] if ref else []) + ["HEAD"]:
        sha = _git(repo, "rev-parse", "--verify", "-q", r + "^{commit}").strip()
        if sha:
            return sha
    return ""


def _read_many(repo, sha, paths):
    """Nội dung nhiều file ở commit `sha` (git cat-file --batch, một tiến trình)."""
    if not paths:
        return {}
    data = "".join("%s:%s\n" % (sha, p) for p in paths).encode("utf-8")
    out = subprocess.run(["git", "-C", repo, "cat-file", "--batch"], input=data, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL).stdout
    res, pos = {}, 0
    for p in paths:
        nl = out.index(b"\n", pos)
        head = out[pos:nl].split()
        pos = nl + 1
        if len(head) < 3 or head[1] == b"missing":
            continue
        size = int(head[2])
        res[p] = out[pos:pos + size].decode("utf-8", "replace")
        pos += size + 1
    return res


def _strip_comments(src):
    src = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
    return re.sub(r"(?m)//[^\n\"]*$", "", src)


def _balanced(text, i, open_ch="(", close_ch=")"):
    """text[i] == open_ch → chỉ số ngay sau dấu đóng tương ứng (bỏ qua chuỗi)."""
    depth, n, in_str = 0, len(text), None
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == in_str:
                in_str = None
        elif c in "\"'":
            in_str = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _split_top(s, sep=","):
    parts, depth, cur, in_str = [], 0, [], None
    for c in s:
        if in_str:
            cur.append(c)
            if c == in_str:
                in_str = None
            continue
        if c in "\"'":
            in_str = c
        elif c in "(<{[":
            depth += 1
        elif c in ")>}]":
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return [p.strip() for p in parts]


def _mapping_paths(args):
    """Đường dẫn trong đối số @XxxMapping(...): value= / path= / đối số đầu (chuỗi hoặc mảng chuỗi)."""
    if not args:
        return [""]
    inner = args.strip()[1:-1]
    m = re.search(r"\b(?:value|path)\s*=\s*(\{[^}]*\}|\"[^\"]*\")", inner)
    if m:
        src = m.group(1)
    else:
        first = _split_top(inner)[0] if inner.strip() else ""
        src = first if (first.startswith('"') or first.startswith("{")) else ""
    found = re.findall(r"\"([^\"]*)\"", src)
    return found or [""]


def _join(base, sub):
    p = "/" + "/".join(x.strip("/") for x in (base, sub) if x and x.strip("/"))
    return re.sub(r"/+", "/", p)


def _camel_words(name):
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b|\d|_)|[A-Z]?[a-z]+|\d+", name or "")
    out = {w.lower() for w in words}
    return out | {w[:-1] for w in out if w.endswith("s") and len(w) > 3}


def _snake(name):
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s).lower()


def _ann_arg(ann_text, keys=("value", "name")):
    """Giá trị chuỗi của annotation: @X("a") / @X(value = "a") / @X(name = "a")."""
    m = re.search(r"\(\s*\"([^\"]*)\"", ann_text)
    if m:
        return m.group(1)
    for k in keys:
        m = re.search(r"\b%s\s*=\s*\"([^\"]*)\"" % k, ann_text)
        if m:
            return m.group(1)
    return None


def _parse_params(sig):
    """Tham số của hàm controller → [{kind, name, type, required, default}]."""
    out = []
    for raw in _split_top(sig):
        if not raw:
            continue
        anns = []
        rest = raw
        while True:
            m = re.match(r"\s*@([\w.]+)", rest)
            if not m:
                break
            end = m.end()
            if end < len(rest) and rest[end:].lstrip().startswith("("):
                j = end + (len(rest[end:]) - len(rest[end:].lstrip()))
                close = _balanced(rest, j)
                anns.append((m.group(1).split(".")[-1], rest[m.start():close]))
                rest = rest[close:]
            else:
                anns.append((m.group(1).split(".")[-1], rest[m.start():end]))
                rest = rest[end:]
        rest = re.sub(r"\bfinal\s+", "", rest).strip()
        m = re.match(r"(.+?)\s+(\w+)$", rest)
        if not m:
            continue
        ptype, pname = m.group(1).strip(), m.group(2)
        names = {a for a, _ in anns}
        text = dict(anns)
        p = {"name": pname, "type": ptype}
        if "PathVariable" in names:
            p.update(kind="path", name=_ann_arg(text["PathVariable"]) or pname)
        elif "RequestParam" in names:
            t = text["RequestParam"]
            dflt = re.search(r"defaultValue\s*=\s*\"([^\"]*)\"", t)
            p.update(kind="query", name=_ann_arg(t) or pname,
                     required=not re.search(r"required\s*=\s*false", t) and not dflt,
                     default=dflt.group(1) if dflt else None)
        elif "RequestBody" in names:
            p.update(kind="body", required=not re.search(r"required\s*=\s*false", text["RequestBody"]))
        elif "RequestPart" in names:
            p.update(kind="part", name=_ann_arg(text["RequestPart"]) or pname)
        elif names & {"RequestHeader", "CookieValue", "AuthenticationPrincipal", "CurrentUser"}:
            continue
        else:
            base = re.sub(r"<.*", "", ptype).split(".")[-1]
            if base in SKIP_PARAM_TYPES:
                continue
            p.update(kind="query" if base in SIMPLE_TYPES else "query-dto", required=False)
        out.append(p)
    return out


def _parse_controller(path, src):
    src = _strip_comments(src)
    cm = re.search(r"\b(?:class|interface)\s+(\w+)", src)
    if not cm or not re.search(r"@(Rest)?Controller\b", src[:cm.start()] + src[cm.start():cm.start() + 1]):
        if not cm or "@RestController" not in src and "@Controller" not in src:
            return []
    cls = cm.group(1)
    head = src[:cm.start()]
    bases = [""]
    hm = list(re.finditer(r"@RequestMapping\b", head))
    if hm:
        k = hm[-1].end()
        args = head[k:_balanced(head, k)] if head[k:].lstrip().startswith("(") else ""
        if args:
            k2 = k + (len(head[k:]) - len(head[k:].lstrip()))
            args = head[k2:_balanced(head, k2)]
        bases = _mapping_paths(args)
    eps = []
    body = src[cm.end():]
    for m in MAPPING_RE.finditer(body):
        kind = m.group(1)
        k = m.end()
        ws = len(body[k:]) - len(body[k:].lstrip())
        args = ""
        if body[k + ws:k + ws + 1] == "(":
            close = _balanced(body, k + ws)
            args = body[k + ws:close]
            k = close
        # chữ ký hàm: tới dấu "(" đầu tiên sau tên hàm, lấy cặp ngoặc cân bằng
        sm = re.search(r"\b(\w+)\s*\(", body[k:k + 2000])
        if not sm:
            continue
        # bỏ qua các annotation khác (vd @PreAuthorize("...")) đứng giữa mapping và hàm
        seg = body[k:k + 4000]
        pos = 0
        while True:
            am = re.match(r"\s*@[\w.]+", seg[pos:])
            if not am:
                break
            pos += am.end()
            rest = seg[pos:]
            if rest.lstrip().startswith("("):
                j = pos + (len(rest) - len(rest.lstrip()))
                pos = _balanced(seg, j)
        sm = re.search(r"\b(\w+)\s*\(", seg[pos:])
        if not sm:
            continue
        start = pos + sm.end() - 1
        close = _balanced(seg, start)
        name = sm.group(1)
        params = _parse_params(seg[start + 1:close - 1])
        if kind == "Request":
            ms = re.findall(r"RequestMethod\.(\w+)", args) or ["ANY"]
        else:
            ms = [kind.upper()]
        produces = re.search(r"consumes\s*=\s*([^,)]+)", args)
        for b in bases:
            for sub in _mapping_paths(args):
                for meth in ms:
                    eps.append({"method": meth, "path": _join(b, sub), "controller": cls, "handler": name,
                                "params": params, "file": path,
                                "consumes": produces.group(1).strip() if produces else ""})
    return eps


def _parse_dto(src):
    """Trường của một class DTO → (fields, extends, is_enum, enum_values)."""
    src = _strip_comments(src)
    cm = re.search(r"\b(class|enum|record)\s+(\w+)([^{]*)\{", src)
    if not cm:
        return None
    head = src[:cm.start()]
    snake = bool(re.search(r"@JsonNaming\([^)]*Snake", head))
    if cm.group(1) == "enum":
        body = src[cm.end():]
        first = re.split(r";|\}", body, 1)[0]
        vals = [re.match(r"\s*(\w+)", v).group(1) for v in _split_top(first) if re.match(r"\s*(\w+)", v)]
        return {"enum": vals}
    ext = re.search(r"\bextends\s+(\w+)", cm.group(3) or "")
    fields = []
    if cm.group(1) == "record":
        k = src.find("(", cm.start())
        comps = src[k + 1:_balanced(src, k) - 1]
        for p in _split_top(comps):
            mm = re.match(r"(?:@\S+\s+)*(.+?)\s+(\w+)$", p.strip())
            if mm:
                fields.append({"name": mm.group(2), "json": _snake(mm.group(2)) if snake else mm.group(2),
                               "type": mm.group(1), "rules": []})
        return {"fields": fields, "extends": None}
    body = src[cm.end():]
    depth, anns, i, n = 0, [], 0, len(body)
    # chỉ trường cấp 1 (depth 0) của class
    lines = body.split("\n")
    pending = []
    for line in lines:
        s = line.strip()
        if depth == 0:
            for am in re.finditer(r"@(\w+)(\([^@]*?\))?(?=\s|$|@)", s):
                pending.append((am.group(1), am.group(0)))
            fm = re.search(r"\b(?:private|protected|public)\s+(?!static\b)(?!final\s+static)(?:final\s+)?"
                           r"([\w.<>,?\[\] ]+?)\s+(\w+)\s*(=[^;]*)?;", s)
            if fm and " static " not in " " + s + " " and "(" not in s.split("=")[0].split(fm.group(2))[0][-1:]:
                names = dict(pending)
                if "JsonIgnore" not in names:
                    json_name = None
                    if "JsonProperty" in names:
                        json_name = _ann_arg(names["JsonProperty"])
                    fname = fm.group(2)
                    rules = []
                    for a, t in pending:
                        if a in VALIDATION:
                            msg = re.search(r"message\s*=\s*\"([^\"]*)\"", t)
                            extra = re.sub(r"message\s*=\s*\"[^\"]*\"\s*,?", "", t[len(a) + 1:]).strip()
                            extra = re.sub(r",\s*\)$", ")", extra)
                            extra = "" if extra in ("", "()") else extra
                            rules.append(a + (extra if extra else "") + (" «%s»" % msg.group(1) if msg else ""))
                    fields.append({"name": fname, "json": json_name or (_snake(fname) if snake else fname),
                                   "type": fm.group(1).strip(), "rules": rules,
                                   "default": (fm.group(3) or "").lstrip("= ").strip() or None})
                pending = []
            elif s and not s.startswith("@"):
                pending = []
        depth += line.count("{") - line.count("}")
    return {"fields": fields, "extends": ext.group(1) if ext else None}


class Index:
    def __init__(self, repo, ref=None):
        self.repo = os.path.expanduser(repo)
        self.sha = _resolve(self.repo, ref)
        self.ref = ref or "HEAD"
        cache = os.path.join(CACHE_DIR, "be-api-%s.json" % self.sha[:12]) if self.sha else None
        data = None
        if cache and os.path.exists(cache):
            try:
                with open(cache, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = None
        if data is None:
            data = self._build()
            if cache:
                try:
                    os.makedirs(CACHE_DIR, exist_ok=True)
                    with open(cache, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
                except OSError:
                    pass
        self.endpoints = data["endpoints"]
        self.classes = data["classes"]  # tên class → đường dẫn file
        self._dto = {}

    def _build(self):
        files = [p for p in _git(self.repo, "ls-tree", "-r", "--name-only", self.sha).splitlines()
                 if p.endswith(".java") and "/src/main/java/" in "/" + p]
        classes = {}
        for p in files:
            classes.setdefault(os.path.basename(p)[:-5], p)
        ctrl = [p for p in files if re.search(r"(Controller|Resource|Api)\w*\.java$", p)]
        eps = []
        for p, src in _read_many(self.repo, self.sha, ctrl).items():
            if "Mapping" in src:
                eps.extend(_parse_controller(p, src))
        return {"endpoints": eps, "classes": classes}

    def dto(self, name, depth=0):
        name = re.sub(r"<.*", "", name).split(".")[-1].strip()
        if name in self._dto:
            return self._dto[name]
        path = self.classes.get(name)
        info = None
        if path:
            src = _read_many(self.repo, self.sha, [path]).get(path)
            info = _parse_dto(src) if src else None
            if info and info.get("extends") and depth < 3:
                parent = self.dto(info["extends"], depth + 1)
                if parent and parent.get("fields"):
                    have = {f["name"] for f in info["fields"]}
                    info["fields"] = [f for f in parent["fields"] if f["name"] not in have] + info["fields"]
        self._dto[name] = info
        return info

    # ---- tìm ----
    def match(self, query, limit=6):
        q = query.strip()
        mm = re.match(r"^(GET|POST|PUT|PATCH|DELETE)\s+(.+)$", q, re.I)
        want_method = mm.group(1).upper() if mm else None
        if mm:
            q = mm.group(2).strip()
        q = re.sub(r"^https?://[^/]+", "", q).split("?")[0].strip()
        scored = []
        if q.startswith("/"):
            segs = [s for s in q.strip("/").split("/") if s]
            for e in self.endpoints:
                es = [s for s in e["path"].strip("/").split("/") if s]
                if len(es) == len(segs) and all(a == b or (a.startswith("{") and a.endswith("}")) for a, b in zip(es, segs)):
                    # khớp đầy đủ: đoạn tĩnh nhiều hơn = cụ thể hơn. Một chữ (vd "swap-user") rơi vào chỗ {id} là khớp
                    # nhầm (gửi thiếu /{id} → trúng PUT /cases/{id}) → hạ điểm dưới khớp tiền tố đúng.
                    words = sum(1 for a, b in zip(es, segs) if a.startswith("{") and not ID_LIKE.match(b))
                    scored.append((100 + sum(1 for a in es if not a.startswith("{")) - 60 * words, e))
                elif q.rstrip("/") and e["path"].startswith(q.rstrip("/")):
                    scored.append((50 + sum(1 for a in es if not a.startswith("{")) - len(es), e))
            if not scored:  # vd gửi thiếu /{id}: so tiền tố bỏ đoạn cuối
                for e in self.endpoints:
                    if segs and e["path"].strip("/").startswith("/".join(segs[:-1]) + "/" + segs[-1].split("{")[0]):
                        scored.append((10, e))
        else:
            low = q.lower().strip()
            ctrl_q = low[:-len("controller")] if low.endswith("controller") and len(low) > 10 else None
            for e in self.endpoints:
                segs = [s for s in e["path"].lower().strip("/").split("/") if s and not s.startswith("{")]
                ctrl = e["controller"].lower()
                if ctrl_q is not None:
                    # tên controller: khớp đúng tên (SLAController ~ slacontroller), hoặc chứa (CaseController ⊂ …)
                    if ctrl == low or ctrl.replace("_", "") == low:
                        scored.append((40, e))
                    elif ctrl_q and ctrl_q in ctrl:
                        scored.append((25, e))
                    continue
                # từ khoá: khớp đầu một đoạn đường dẫn (sla → sla-warnings, không khớp slack), cả cụm trong đường dẫn,
                # hoặc tên hàm / controller (camelCase).
                if any(s == low or s.startswith(low + "-") or s.rstrip("s") == low.rstrip("s") for s in segs):
                    scored.append((35, e))
                elif "-" in low or "/" in low or len(low) >= 6:
                    if low in e["path"].lower():
                        scored.append((30, e))
                    elif low.replace("-", "") in e["handler"].lower() or low.replace("-", "") in ctrl:
                        scored.append((20, e))
                elif low in _camel_words(e["handler"]) | _camel_words(e["controller"]):
                    scored.append((15, e))  # trùng nguyên một từ camelCase (SLAWarning → sla, warning; Slack ≠ sla)
        if want_method:
            scored = [(s + (5 if e["method"] in (want_method, "ANY") else 0), e) for s, e in scored]
        scored.sort(key=lambda x: (-x[0], x[1]["path"], x[1]["method"]))
        seen, out = set(), []
        for _, e in scored:
            k = (e["method"], e["path"])
            if k not in seen:
                seen.add(k)
                out.append(e)
        return out[:limit]

    def describe_dto(self, name, indent="    "):
        info = self.dto(name)
        if not info:
            return None
        if "enum" in info:
            return "%senum %s: %s" % (indent, name, ", ".join(info["enum"][:40]))
        lines = []
        for f in info["fields"]:
            t = f["type"]
            extra = ""
            sub = self.dto(re.sub(r".*<\s*|\s*>.*", "", t)) if "<" in t else self.dto(t)
            if sub and "enum" in sub:
                extra = " (enum: %s)" % ", ".join(sub["enum"][:15])
            elif sub and sub.get("fields") and re.sub(r"<.*", "", t).split(".")[-1] not in SIMPLE_TYPES:
                extra = " (object %s: %s)" % (re.sub(r".*<\s*|\s*>.*", "", t) if "<" in t else t,
                                              ", ".join(x["json"] for x in sub["fields"][:12]))
            rule = (" — " + "; ".join(f["rules"])) if f["rules"] else ""
            dflt = (" = %s" % f["default"]) if f.get("default") else ""
            lines.append("%s\"%s\": %s%s%s%s" % (indent, f["json"], t, dflt, extra, rule))
        return "\n".join(lines) if lines else "%s(không đọc được trường)" % indent

    def describe(self, e):
        out = ["%s %s   (%s.%s)" % (e["method"], e["path"], e["controller"], e["handler"])]
        example_path = e["path"]
        order = {"path": 0, "query": 1, "query-dto": 1, "part": 2, "body": 3}
        for p in sorted(e["params"], key=lambda x: order.get(x["kind"], 9)):
            if p["kind"] == "path":
                out.append("  path {%s}: %s" % (p["name"], p["type"]))
                example_path = example_path.replace("{%s}" % p["name"], "<%s>" % p["name"])
            elif p["kind"] == "query":
                out.append("  query %s: %s%s%s" % (p["name"], p["type"], " (bắt buộc)" if p.get("required") else "",
                                                     " mặc định=%s" % p["default"] if p.get("default") else ""))
            elif p["kind"] == "query-dto":
                d = self.describe_dto(p["type"])
                out.append("  query (các tham số từ %s, tên camelCase như dưới):" % p["type"])
                if d:
                    out.append(d.replace("\"", ""))
            elif p["kind"] == "body":
                coll = re.match(r"(?:List|Set|Collection)\s*<\s*([\w.]+)\s*>$", p["type"])
                if coll and coll.group(1).split(".")[-1] in SIMPLE_TYPES:
                    out.append("  body JSON: mảng [%s, …]" % coll.group(1))
                    continue
                if coll:
                    out.append("  body JSON: mảng các object %s:" % coll.group(1))
                    out.append(self.describe_dto(coll.group(1)) or "    (không tìm thấy định nghĩa)")
                    continue
                d = self.describe_dto(p["type"])
                out.append("  body JSON %s%s:" % (p["type"], "" if p.get("required", True) else " (tuỳ chọn)"))
                out.append(d or "    (kiểu %s — không tìm thấy định nghĩa)" % p["type"])
            elif p["kind"] == "part":
                out.append("  multipart part %s: %s" % (p["name"], p["type"]))
        out.append("  → gửi: %s %s" % (e["method"], example_path))
        return "\n".join(out)


def lookup(repo, query, ref=None, limit=12):
    """Kết quả tra cứu dạng chữ cho Gemini."""
    query = (query or "").strip()
    if not query:
        return "API cần target: đoạn đường dẫn (vd swap-user), \"PUT /api/v1/cases/swap-user/123\" hoặc tên DTO"
    key = (os.path.expanduser(repo), ref)
    idx = _MEM.get(key)
    if idx is None:
        idx = _MEM[key] = Index(repo, ref)
    if not idx.sha:
        return "API: không đọc được repo BE %s" % repo
    head = "API (mã BE %s @ %s): " % (idx.ref, idx.sha[:9])
    if re.match(r"^[A-Z]\w+$", query) and query in idx.classes and not query.endswith("Controller"):
        d = idx.describe_dto(query)
        if d:
            return head + "DTO %s\n%s" % (query, d)
    found = idx.match(query, limit)
    if not found:
        return head + "không thấy endpoint khớp %r (%d endpoint đã quét). Thử từ khoá ngắn hơn (vd \"swap-user\")." % (
            query, len(idx.endpoints))
    detail, rest = found[:4], found[4:]
    text = "\n\n".join(idx.describe(e) for e in detail)
    if rest:  # liệt kê gọn phần còn lại — tra chi tiết bằng "METHOD /đường/dẫn"
        text += "\n\nCòn khớp (tra chi tiết bằng \"METHOD /đường/dẫn\"):\n" + "\n".join(
            "  %s %s   (%s.%s)" % (e["method"], e["path"], e["controller"], e["handler"]) for e in rest)
    return head + "%d endpoint khớp %r\n\n%s" % (len(found), query, text)


def hint_for(repo, method, target, status, ref=None):
    """Gợi ý cho request vừa trả 400/404/405/415: endpoint BE khớp đường dẫn (đúng phương thức, {biến}, body)."""
    path = re.sub(r"^https?://[^/]+", "", target or "").split("?")[0]
    if "/api/" not in path:
        return ""
    try:
        key = (os.path.expanduser(repo), ref)
        idx = _MEM.get(key) or _MEM.setdefault(key, Index(repo, ref))
        found = idx.match("%s %s" % (method, path), 3)
    except Exception:  # noqa: BLE001 — gợi ý là phụ, không làm hỏng QA
        return ""
    if not found:
        return ""
    exact = [e for e in found if e["method"] == method]
    lead = ("HTTP %s: kiểm tra lại request theo mã BE (đúng phương thức, đủ {biến} trên đường dẫn, tên trường body) "
            "rồi gửi lại trước khi kết luận" % status)
    if status == 405 and not exact:
        lead = "HTTP 405: đường dẫn này KHÔNG nhận %s — dùng đúng phương thức bên dưới" % method
    return "\n\n(gợi ý của công cụ — %s)\n%s" % (lead, "\n\n".join(idx.describe(e) for e in found))


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("query")
    ap.add_argument("--repo", default="/home/vinhdq/SOAR_BE")
    ap.add_argument("--ref", default="origin/release-ss/lastest-k8s")
    a = ap.parse_args(argv)
    print(lookup(a.repo, a.query, a.ref))


if __name__ == "__main__":
    main(sys.argv[1:])
