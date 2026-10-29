"""
pdf_text.py
===========
The text of a PDF, without a dependency: enough for reading reports, papers
and manuals the web tools fetch (fetch_page / web_read) and PDFs dropped into
the chat (uploads.py).

  * Objects are found by scanning ("n g obj ... endobj") plus compressed
    object streams (/Type /ObjStm), so PDFs without a usable xref table and
    PDF 1.5+ files both work.
  * Streams are decoded with FlateDecode (zlib, with PNG predictors) or read
    as they are; other filters (DCT images, JBIG2 ...) are skipped - they hold
    pictures, not text.
  * Each page's content stream is interpreted just far enough for text: font
    selection (Tf), strings shown (Tj, TJ, ', "), and line moves (Td, TD, T*,
    Tm) that become line breaks. A font's /ToUnicode CMap maps its codes to
    characters; without one, bytes are read as WinAnsi (Latin-1).

Not handled (the text comes out empty or garbled, and callers say so):
scanned PDFs (images of text - they need OCR), encrypted PDFs, and fonts
with neither a ToUnicode map nor a standard encoding.
"""
from __future__ import annotations

import re
import zlib

MAX_PAGES = 400
_OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")
_REF_RE = re.compile(rb"(\d+)\s+\d+\s+R\b")


class PDFError(Exception):
    pass


def _find_dict_end(data: bytes, start: int) -> int:
    """Index just after the '>>' closing the '<<' at data[start]."""
    depth, i, n = 0, start, len(data)
    while i < n - 1:
        two = data[i:i + 2]
        if two == b"<<":
            depth += 1
            i += 2
            continue
        if two == b">>":
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        if data[i:i + 1] == b"(":                 # skip a literal string (it may hold "<<")
            i = _skip_literal(data, i)
            continue
        i += 1
    return n


def _skip_literal(data: bytes, i: int) -> int:
    depth, n = 0, len(data)
    while i < n:
        c = data[i:i + 1]
        if c == b"\\":
            i += 2
            continue
        if c == b"(":
            depth += 1
        elif c == b")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _png_unpredict(raw: bytes, columns: int) -> bytes:
    """Undo PNG predictors (used by cross-reference and object streams)."""
    row = columns + 1
    out, prev = bytearray(), bytearray(columns)
    for r in range(0, len(raw), row):
        kind, line = raw[r], bytearray(raw[r + 1:r + row])
        for i in range(len(line)):
            left = line[i - 1] if i else 0
            up = prev[i] if i < len(prev) else 0
            if kind == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif kind == 4:
                ul = prev[i - 1] if i and i - 1 < len(prev) else 0
                p = left + up - ul
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - ul)
                line[i] = (line[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else ul)) & 0xFF
        out += line
        prev = line
    return bytes(out)


class _Doc:
    def __init__(self, data: bytes):
        if not data.startswith(b"%PDF") and b"%PDF" not in data[:1024]:
            raise PDFError("not a PDF file")
        if re.search(rb"/Encrypt\s", data[-4096:]) or re.search(rb"trailer[^>]*/Encrypt\s", data):
            raise PDFError("the PDF is encrypted")
        self.data = data
        self.objs: dict = {}              # num -> (dict bytes, stream bytes | None)
        self._scan()

    # ---- objects -------------------------------------------------------------
    def _scan(self):
        data = self.data
        for m in _OBJ_RE.finditer(data):
            num, body_start = int(m.group(1)), m.end()
            end = data.find(b"endobj", body_start)
            if end < 0:
                continue
            body = data[body_start:end]
            head, stream = body, None
            s = body.find(b"stream")
            if s >= 0 and body.lstrip().startswith(b"<<"):
                dstart = body.find(b"<<")
                dend = _find_dict_end(body, dstart)
                if dend <= s:
                    head = body[:dend]
                    raw = body[s + 6:]
                    raw = raw[2:] if raw.startswith(b"\r\n") else raw[1:] if raw[:1] in (b"\n", b"\r") else raw
                    e = raw.rfind(b"endstream")
                    stream = raw[:e] if e >= 0 else raw
            self.objs[num] = (head, stream)       # a later definition (incremental update) wins
        for num, (head, stream) in list(self.objs.items()):
            if stream is not None and re.search(rb"/Type\s*/ObjStm", head):
                self._unpack_objstm(head, stream)

    def _unpack_objstm(self, head: bytes, stream: bytes):
        data = self.decode(head, stream)
        if not data:
            return
        n = int((re.search(rb"/N\s+(\d+)", head) or [0, b"0"])[1])
        first = int((re.search(rb"/First\s+(\d+)", head) or [0, b"0"])[1])
        nums = [int(x) for x in re.findall(rb"\d+", data[:first])]
        pairs = list(zip(nums[0::2], nums[1::2]))[:n]
        for i, (num, off) in enumerate(pairs):
            start = first + off
            end = first + pairs[i + 1][1] if i + 1 < len(pairs) else len(data)
            if num not in self.objs:
                self.objs[num] = (data[start:end], None)

    def decode(self, head: bytes, stream: bytes | None) -> bytes:
        if stream is None:
            return b""
        m = re.search(rb"/Length\s+(\d+)(?!\s+\d+\s+R)", head)
        if m and int(m.group(1)) <= len(stream):
            stream = stream[:int(m.group(1))]
        filters = re.findall(rb"/(\w+Decode)\b", head.split(b"/DecodeParms")[0])
        data = stream
        for f in filters:
            if f in (b"FlateDecode", b"Fl"):
                try:
                    data = zlib.decompress(data)
                except zlib.error:
                    try:
                        data = zlib.decompressobj().decompress(data)       # truncated stream: take what inflates
                    except zlib.error:
                        return b""
                pred = re.search(rb"/Predictor\s+(\d+)", head)
                if pred and int(pred.group(1)) >= 10:
                    cols = re.search(rb"/Columns\s+(\d+)", head)
                    data = _png_unpredict(data, int(cols.group(1)) if cols else 1)
            elif f in (b"ASCIIHexDecode", b"AHx"):
                hexs = re.sub(rb"[^0-9A-Fa-f]", b"", data.split(b">")[0])
                data = bytes.fromhex((hexs + b"0" * (len(hexs) % 2)).decode())
            else:
                return b""                           # DCT, JBIG2, CCITT ...: pictures
        return data

    def get(self, num: int) -> tuple[bytes, bytes | None]:
        return self.objs.get(num, (b"", None))

    def resolve(self, value: bytes) -> bytes:
        m = re.fullmatch(rb"\s*(\d+)\s+\d+\s+R\s*", value)
        return self.get(int(m.group(1)))[0] if m else value

    # ---- pages ---------------------------------------------------------------
    def pages(self) -> list:
        """Page object numbers in document order (the /Kids tree from /Root)."""
        root = None
        trailer = re.findall(rb"/Root\s+(\d+)\s+\d+\s+R", self.data)
        if trailer:
            root = int(trailer[-1])
        if root is None:
            for num, (head, _s) in self.objs.items():
                if re.search(rb"/Type\s*/Catalog", head):
                    root = num
                    break
        order: list = []
        if root is not None:
            m = re.search(rb"/Pages\s+(\d+)\s+\d+\s+R", self.get(root)[0])
            if m:
                self._walk(int(m.group(1)), order, set())
        if not order:                          # no usable tree: every page object, in file order
            order = [n for n, (h, _s) in sorted(self.objs.items()) if re.search(rb"/Type\s*/Page(?!s)\b", h)]
        return order[:MAX_PAGES]

    def _walk(self, num: int, out: list, seen: set):
        if num in seen or len(out) >= MAX_PAGES:
            return
        seen.add(num)
        head = self.get(num)[0]
        kids = re.search(rb"/Kids\s*\[([^\]]*)\]", head)
        if re.search(rb"/Type\s*/Page(?!s)\b", head) and not kids:
            out.append(num)
            return
        for ref in _REF_RE.findall(kids.group(1) if kids else b""):
            self._walk(int(ref), out, seen)

    def _inherited(self, num: int, key: bytes) -> bytes:
        """A page attribute, looking up the /Parent chain (e.g. /Resources)."""
        for _ in range(20):
            head = self.get(num)[0]
            m = re.search(rb"/" + key + rb"\s*(\d+\s+\d+\s+R|<<)", head)
            if m:
                if m.group(1) == b"<<":
                    return head[m.start(1):_find_dict_end(head, m.start(1))]
                return self.resolve(m.group(1))
            p = re.search(rb"/Parent\s+(\d+)\s+\d+\s+R", head)
            if not p:
                break
            num = int(p.group(1))
        return b""

    def page_contents(self, num: int) -> bytes:
        head = self.get(num)[0]
        m = re.search(rb"/Contents\s*(\[[^\]]*\]|\d+\s+\d+\s+R)", head)
        if not m:
            return b""
        parts = []
        for ref in _REF_RE.findall(m.group(1)):
            h, s = self.get(int(ref))
            if s is None and h.strip().startswith(b"["):          # /Contents N R pointing to an array
                for inner in _REF_RE.findall(h):
                    parts.append(self.decode(*self.get(int(inner))))
            else:
                parts.append(self.decode(h, s))
        return b"\n".join(parts)

    def page_fonts(self, num: int) -> dict:
        """{font resource name: code -> text map (or None)} for one page."""
        res = self._inherited(num, b"Resources")
        fm = re.search(rb"/Font\s*(<<|\d+\s+\d+\s+R)", res)
        if not fm:
            return {}
        fonts = res[fm.start(1):_find_dict_end(res, fm.start(1))] if fm.group(1) == b"<<" else self.resolve(fm.group(1))
        out = {}
        for name, ref in re.findall(rb"/([^\s/<>\[\]()]+)\s+(\d+)\s+\d+\s+R", fonts):
            out[name.decode("latin-1")] = self._font_map(int(ref))
        return out

    _font_cache: dict = {}

    def _font_map(self, ref: int):
        if ref in self._font_cache:
            return self._font_cache[ref]
        head = self.get(ref)[0]
        cmap = None
        tu = re.search(rb"/ToUnicode\s+(\d+)\s+\d+\s+R", head)
        if tu:
            cmap = _parse_cmap(self.decode(*self.get(int(tu.group(1)))))
        two_byte = bool(re.search(rb"/Subtype\s*/Type0", head))
        self._font_cache[ref] = (cmap, two_byte)
        return self._font_cache[ref]


def _hex_text(h: bytes) -> str:
    h = re.sub(rb"[^0-9A-Fa-f]", b"", h)
    b = bytes.fromhex((h + b"0" * (len(h) % 2)).decode())
    if len(b) >= 2 and len(b) % 2 == 0:
        try:
            return b.decode("utf-16-be")
        except UnicodeDecodeError:
            pass
    return b.decode("latin-1")


def _parse_cmap(data: bytes) -> dict:
    """ToUnicode CMap -> {code bytes: text}."""
    out: dict = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f\s]+)>\s*<([0-9A-Fa-f\s]*)>", block):
            out[bytes.fromhex(re.sub(rb"\s", b"", src).decode())] = _hex_text(dst)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for m in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f\s]*>|\[[^\]]*\])", block):
            lo, hi = int(m.group(1), 16), int(m.group(2), 16)
            width = len(m.group(1)) // 2
            if hi - lo > 5000:
                continue
            dst = m.group(3)
            if dst.startswith(b"["):
                items = re.findall(rb"<([0-9A-Fa-f\s]*)>", dst)
                for i, item in enumerate(items[:hi - lo + 1]):
                    out[(lo + i).to_bytes(width, "big")] = _hex_text(item)
            else:
                base = re.sub(rb"[^0-9A-Fa-f]", b"", dst)
                start = int(base or b"0", 16)
                for i in range(hi - lo + 1):
                    code = (start + i).to_bytes(max(2, len(base) // 2), "big")
                    try:
                        text = code.decode("utf-16-be")
                    except UnicodeDecodeError:
                        text = ""
                    out[(lo + i).to_bytes(width, "big")] = text
    return out


# ---- content stream tokens -----------------------------------------------------
_TOKEN_RE = re.compile(rb"\((?:\\.|[^\\()]|\((?:\\.|[^\\()])*\))*\)|<[0-9A-Fa-f\s]*>|\[|\]|/[^\s/\[\]()<>{}]+|"
                       rb"[-+]?\d*\.\d+|[-+]?\d+|[A-Za-z'\"*]+", re.S)
_ESC = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}


def _literal(tok: bytes) -> bytes:
    s, out, i = tok[1:-1], bytearray(), 0
    while i < len(s):
        c = s[i:i + 1]
        if c == b"\\" and i + 1 < len(s):
            nxt = s[i + 1:i + 2]
            if nxt in _ESC:
                out += _ESC[nxt]
                i += 2
                continue
            m = re.match(rb"[0-7]{1,3}", s[i + 1:i + 4])
            if m:
                out.append(int(m.group(0), 8) & 0xFF)
                i += 1 + len(m.group(0))
                continue
            if nxt in (b"\n", b"\r"):                    # line continuation
                i += 2
                continue
            i += 1
            continue
        out += c
        i += 1
    return bytes(out)


def _show(raw: bytes, font) -> str:
    cmap, two_byte = font if font else (None, False)
    if cmap:
        out, i = [], 0
        widths = sorted({len(k) for k in cmap}, reverse=True) or [1]
        while i < len(raw):
            for w in widths:
                code = raw[i:i + w]
                if code in cmap:
                    out.append(cmap[code])
                    i += w
                    break
            else:
                i += 2 if two_byte else 1
        return "".join(out)
    if two_byte:
        return ""                                      # CID codes without a map: unreadable
    return raw.decode("cp1252", errors="replace")


def _page_text(content: bytes, fonts: dict) -> str:
    out: list = []
    stack: list = []
    font = None
    last_y = None
    for m in _TOKEN_RE.finditer(content):
        tok = m.group(0)
        c = tok[:1]
        if c == b"(":
            stack.append(("s", _literal(tok)))
        elif c == b"<":
            h = re.sub(rb"\s", b"", tok[1:-1])
            stack.append(("s", bytes.fromhex((h + b"0" * (len(h) % 2)).decode())))
        elif c == b"[":
            stack.append(("[", None))
        elif c == b"]":
            items = []
            while stack and stack[-1][0] != "[":
                items.append(stack.pop())
            if stack:
                stack.pop()
            stack.append(("a", list(reversed(items))))
        elif c == b"/":
            stack.append(("n", tok[1:].decode("latin-1")))
        elif c.isdigit() or c in b"-+.":
            try:
                stack.append(("f", float(tok)))
            except ValueError:
                pass
        else:
            op = tok
            if op == b"Tf" and len(stack) >= 2 and stack[-2][0] == "n":
                font = fonts.get(stack[-2][1])
            elif op in (b"Tj", b"'", b'"'):
                if op != b"Tj":
                    out.append("\n")
                s = next((v for k, v in reversed(stack) if k == "s"), None)
                if s is not None:
                    out.append(_show(s, font))
            elif op == b"TJ":
                arr = next((v for k, v in reversed(stack) if k == "a"), None) or []
                for kind, v in arr:
                    if kind == "s":
                        out.append(_show(v, font))
                    elif kind == "f" and v < -180:           # a wide gap between words
                        out.append(" ")
            elif op in (b"Td", b"TD"):
                nums = [v for k, v in stack[-2:] if k == "f"]
                if len(nums) == 2 and abs(nums[1]) > 0.5:
                    out.append("\n")
                elif len(nums) == 2 and nums[0] > 0:
                    out.append(" ")
            elif op == b"Tm":
                nums = [v for k, v in stack[-6:] if k == "f"]
                if len(nums) == 6:
                    if last_y is not None and abs(nums[5] - last_y) > 1:
                        out.append("\n")
                    elif last_y is not None:
                        out.append(" ")
                    last_y = nums[5]
            elif op == b"T*":
                out.append("\n")
            elif op == b"ET":
                out.append("\n")
            stack.clear() if op not in (b"BT",) else None
    text = "".join(out)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract(data: bytes, max_chars: int = 400_000) -> dict:
    """{"text", "pages", "title"} of a PDF's bytes. Raises PDFError for a file
    that is not a PDF or is encrypted."""
    doc = _Doc(data)
    doc._font_cache = {}
    pages = doc.pages()
    texts, total = [], 0
    for i, num in enumerate(pages, 1):
        try:
            t = _page_text(doc.page_contents(num), doc.page_fonts(num))
        except Exception:                               # one bad page never loses the rest
            t = ""
        if t:
            texts.append(f"[Page {i}]\n{t}")
            total += len(t)
            if total > max_chars:
                break
    title = ""
    m = re.search(rb"/Title\s*(\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>)", data[-200_000:] if len(data) > 200_000 else data)
    if m:
        raw = m.group(1)
        title = _hex_text(raw[1:-1]) if raw.startswith(b"<") else _literal(raw).decode("latin-1", errors="replace")
        if title.startswith("﻿") or raw.startswith(b"(\xfe\xff"):
            try:
                title = _literal(raw)[2:].decode("utf-16-be")
            except UnicodeDecodeError:
                pass
    return {"text": "\n\n".join(texts)[:max_chars], "pages": len(pages), "title": re.sub(r"\s+", " ", title).strip()}
