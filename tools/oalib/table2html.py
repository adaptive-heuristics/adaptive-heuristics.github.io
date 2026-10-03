"""Convert a booktabs table fragment (.tex) into an HTML table and CSV rows.

Strict: anything outside the supported subset raises BuildError naming the file and line.
"""
from __future__ import annotations

import csv
import html as _html
import io
import re
from dataclasses import dataclass, field

from .inline import Inline
from .texscan import blank_comments, find_env, line_of, read_group, read_opt, skip_ws
from .util import BuildError


@dataclass
class Col:
    align: str          # l c r j
    width: str | None   # CSS width or None


@dataclass
class Cell:
    html: str
    text_lines: list[str]
    colspan: int = 1
    align: str = "l"
    indent: int = 0
    cmid: str = ""      # "", "lr", "l", "r", "-" (untrimmed)
    header: bool = False


@dataclass
class Row:
    cells: list[Cell]
    rule_above: bool = False
    gap_before: str | None = None
    gap_after: str | None = None
    head: bool = False


@dataclass
class Table:
    path: str
    cols: list[Col]
    rows: list[Row]
    full_width: bool
    numbers: list[str] = field(default_factory=list)

    @property
    def ncols(self) -> int:
        return len(self.cols)


PRE_OK = [
    r"\\setstretch\s*\{[^}]*\}", r"\\setlength\s*\{\\tabcolsep\}\s*\{[^}]*\}",
    r"\\renewcommand\s*\{?\\cellalign\}?\s*\{[^}]*\}", r"\\renewcommand\s*\{?\\arraystretch\}?\s*\{[^}]*\}",
    r"\\begin\s*\{threeparttable\}(\[[^\]]*\])?", r"\\end\s*\{threeparttable\}", r"\\centering",
    r"\\(small|footnotesize|scriptsize|normalsize)", r"\\vspace\*?\s*\{[^}]*\}",
]


def _check_outside(s: str, path: str, where: str) -> None:
    rest = s
    for p in PRE_OK:
        rest = re.sub(p, " ", rest)
    if rest.strip():
        raise BuildError(f"{path}: unsupported content {where} the tabular: {rest.strip()[:80]!r}")


def _length_css(arg: str) -> str | None:
    a = arg.strip()
    m = re.fullmatch(r"([0-9.]*)\s*\\(linewidth|textwidth|columnwidth|hsize)", a)
    if m:
        f = float(m.group(1)) if m.group(1) else 1.0
        return f"{f * 100:.0f}%"
    m = re.fullmatch(r"([0-9.]+)\s*(cm|mm|in|pt|em|ex|bp)", a)
    if m:
        unit = {"bp": "pt"}.get(m.group(2), m.group(2))
        return f"{m.group(1)}{unit}"
    return None


def parse_colspec(spec: str, coltypes: dict, path: str) -> list[Col]:
    cols: list[Col] = []
    i, n = 0, len(spec)
    pending: str | None = None
    while i < n:
        c = spec[i]
        if c in " \t\n|":
            i += 1
            continue
        if c in "@!":
            _, i = read_group(spec, i + 1)
            continue
        if c == ">":
            pre, i = read_group(spec, i + 1)
            if "\\centering" in pre:
                pending = "c"
            elif "\\raggedleft" in pre:
                pending = "r"
            elif "\\raggedright" in pre:
                pending = "l"
            continue
        if c == "<":
            _, i = read_group(spec, i + 1)
            continue
        if c == "*":
            num, i = read_group(spec, i + 1)
            sub, i = read_group(spec, i)
            for _ in range(int(num)):
                cols += parse_colspec(sub, coltypes, path)
            continue
        if c in "lcr":
            cols.append(Col(pending or c, None))
            pending = None
            i += 1
            continue
        if c in "pmb":
            w, i = read_group(spec, i + 1)
            cols.append(Col(pending or "j", _length_css(w)))
            pending = None
            continue
        if c == "X":
            cols.append(Col(pending or "l", None))
            pending = None
            i += 1
            continue
        if c in coltypes:
            nargs, align = coltypes[c]
            width = None
            i += 1
            if nargs:
                j = skip_ws(spec, i)
                if j >= n or spec[j] != "{":
                    raise BuildError(f"{path}: column type {c} takes an argument here ({spec!r})")
                w, i = read_group(spec, j)
                width = _length_css(w)
            else:
                j = skip_ws(spec, i)
                if j < n and spec[j] == "{":
                    raise BuildError(f"{path}: column type {c} takes no argument in this scope ({spec!r})")
            cols.append(Col(align, width))
            continue
        raise BuildError(f"{path}: unsupported column type {c!r} in {spec!r}")
    return cols


# ---- body scanning -----------------------------------------------------------------------------
_RULE = re.compile(r"\\(toprule|midrule|bottomrule|hline|cmidrule|addlinespace|morecmidrules)(?![A-Za-z@])")


def _scan(body: str):
    """Yield tokens: ('text', s) ('sep',) ('end', len) ('rule', kind, arg) at brace depth 0 outside math."""
    i, n = 0, len(body)
    buf = []
    depth = 0
    in_math = False
    while i < n:
        c = body[i]
        if c == "\\":
            if depth == 0 and not in_math:
                if body.startswith("\\\\", i):
                    if buf:
                        yield ("text", "".join(buf)); buf = []
                    opt, j = read_opt(body, i + 2)
                    yield ("end", opt)
                    i = j
                    continue
                m = _RULE.match(body, i)
                if m:
                    if "".join(buf).strip():
                        yield ("text", "".join(buf)); buf = []
                    else:
                        buf = []
                    kind = m.group(1)
                    j = m.end()
                    arg = None
                    if kind == "cmidrule":
                        trim = ""
                        k = skip_ws(body, j)
                        if k < n and body[k] == "(":
                            close = body.index(")", k)
                            trim = body[k + 1:close]
                            j = close + 1
                        _, j = read_opt(body, j)
                        rng, j = read_group(body, j)
                        arg = (trim, rng)
                    elif kind in ("addlinespace", "toprule", "midrule", "bottomrule"):
                        arg, j = read_opt(body, j)
                    yield ("rule", kind, arg)
                    i = j
                    continue
            buf.append(body[i:i + 2])
            i += 2
            continue
        if c == "$":
            in_math = not in_math
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "&" and depth == 0 and not in_math:
            yield ("text", "".join(buf)); buf = []
            yield ("sep",)
            i += 1
            continue
        buf.append(c)
        i += 1
    if "".join(buf).strip():
        yield ("text", "".join(buf))


def _gap_class(length: str | None) -> str:
    if length is None:
        return "g05"
    m = re.fullmatch(r"\s*([0-9.]+)\s*(em|ex|pt|mm|cm)\s*", length)
    if not m:
        return "g05"
    v = float(m.group(1)) * {"em": 1, "ex": 0.45, "pt": 0.09, "mm": 0.26, "cm": 2.6}[m.group(2)]
    for cls, lim in (("g025", 0.35), ("g05", 0.55), ("g06", 0.8), ("g1", 1.25)):
        if v <= lim:
            return cls
    return "g15"


_MULTICOL = re.compile(r"^\s*\\multicolumn(?![A-Za-z@])")
_MAKECELL = re.compile(r"^\s*\\makecell(?![A-Za-z@])")
_INDENT = re.compile(r"^\s*\\(quad|qquad)(?![A-Za-z@])\s*")


def _split_makecell(content: str) -> list[str] | None:
    m = _MAKECELL.match(content)
    if not m:
        return None
    opt, j = read_opt(content, m.end())
    inner, j2 = read_group(content, j)
    if content[j2:].strip():
        return None
    parts, depth, cur, k = [], 0, [], 0
    while k < len(inner):
        ch = inner[k]
        if ch == "\\" and inner.startswith("\\\\", k) and depth == 0:
            parts.append("".join(cur)); cur = []
            _, k = read_opt(inner, k + 2)
            continue
        if ch == "\\":
            cur.append(inner[k:k + 2]); k += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        cur.append(ch)
        k += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts]


_NUM_MINUS = re.compile(r"(?<![\w.])-(?=\.?\d)")


def _cell(raw: str, col_align: str, conv: Inline) -> Cell:
    s = raw.strip()
    colspan, align = 1, col_align
    m = _MULTICOL.match(s)
    if m:
        num, j = read_group(s, m.end())
        spec, j = read_group(s, j)
        content, j2 = read_group(s, j)
        if s[j2:].strip():
            raise BuildError(f"text after \\multicolumn: {s[j2:].strip()[:40]!r}")
        colspan = int(num)
        cols = parse_colspec(spec, {}, "multicolumn")
        align = cols[0].align if cols else align
        s = content.strip()
    if s.startswith("\\multirow"):
        raise BuildError("\\multirow is not supported")
    indent = 0
    while True:
        mm = _INDENT.match(s)
        if not mm:
            break
        indent += 2 if mm.group(1) == "qquad" else 1
        s = s[mm.end():]
    lines = _split_makecell(s) or [s]
    htmls, texts = [], []
    for ln in lines:
        r = conv.inline(ln)
        h = r.html
        h = _NUM_MINUS.sub("\u2212", h) if re.search(r"\d", h) else h
        htmls.append(h)
        texts.append(r.text)
    return Cell("<br>".join(htmls), texts, colspan, align, indent)


def parse_table(src: str, path: str, coltypes: dict, conv_factory) -> Table:
    text = blank_comments(src.replace("\r\n", "\n"))
    env_name = None
    for name in ("tabularx", "tabular"):
        if find_env(text, name):
            env_name = name
            break
    if env_name is None:
        raise BuildError(f"{path}: no tabular environment")
    b0, c0, c1, e1 = find_env(text, env_name)
    if find_env(text, "tabular", e1) or find_env(text, "tabularx", e1):
        raise BuildError(f"{path}: more than one tabular environment")
    _check_outside(text[:b0], path, "before")
    _check_outside(text[e1:], path, "after")
    head = text[c0:c1]
    j = 0
    full = False
    if env_name == "tabularx":
        _, j = read_group(head, j)
        full = True
    else:
        _, j = read_opt(head, j)
    spec, j = read_group(head, j)
    cols = parse_colspec(spec, coltypes, path)
    body = head[j:]
    body_offset = c0 + j

    rows: list[Row] = []
    cells_raw: list[str] = []
    cur: list[str] = []
    pending_rule = False
    pending_gap: str | None = None
    seen_mid = False
    top = bottom = False

    def close_row(gap_after):
        nonlocal pending_rule, pending_gap
        row_cells = cur + [""] if not cur else cur
        rows.append(Row([Cell("", [c]) for c in row_cells], rule_above=pending_rule, gap_before=pending_gap,
                        gap_after=_gap_class(gap_after) if gap_after else None, head=not seen_mid))
        pending_rule = False
        pending_gap = None

    buf = ""
    raw_rows: list[list[str]] = []
    for tok in _scan(body):
        if tok[0] == "text":
            buf += tok[1]
        elif tok[0] == "sep":
            cur.append(buf); buf = ""
        elif tok[0] == "end":
            cur.append(buf); buf = ""
            close_row(tok[1])
            raw_rows.append(cur)
            cur = []
        else:
            kind, arg = tok[1], tok[2]
            if buf.strip() or cur:
                raise BuildError(f"{path}:{line_of(text, body_offset)}: \\{kind} inside a row")
            buf = ""
            if kind == "toprule":
                top = True
            elif kind == "bottomrule":
                bottom = True
            elif kind in ("midrule", "hline"):
                pending_rule = True
                seen_mid = True
            elif kind == "addlinespace":
                pending_gap = _gap_class(arg)
            elif kind == "cmidrule":
                if not rows:
                    raise BuildError(f"{path}: \\cmidrule before the first row")
                trim, rng = arg
                a, b = (int(x) for x in rng.split("-"))
                rows[-1].cells.append(Cell("", ["\u0000cmid", trim or "-", str(a), str(b)]))
            else:
                raise BuildError(f"{path}: unsupported rule \\{kind}")
    if buf.strip() or cur:
        cur.append(buf)
        close_row(None)
        raw_rows.append(cur)
    if not top or not bottom:
        raise BuildError(f"{path}: expected \\toprule and \\bottomrule")

    # convert cells
    out_rows: list[Row] = []
    numbers: list[str] = []
    for r_idx, (row, raw) in enumerate(zip(rows, raw_rows)):
        cmids = [c.text_lines for c in row.cells if c.text_lines and c.text_lines[0] == "\u0000cmid"]
        cells = []
        col = 0
        for c_idx, rc in enumerate(raw):
            conv = conv_factory(f"{path} row {r_idx + 1} col {c_idx + 1}")
            try:
                cell = _cell(rc, cols[min(col, len(cols) - 1)].align if cols else "l", conv)
            except BuildError as e:
                raise BuildError(f"{path} row {r_idx + 1} col {c_idx + 1}: {e}")
            cells.append(cell)
            col += cell.colspan
            numbers += re.findall(r"[-−]?\d[\d,]*\.?\d*", " ".join(cell.text_lines))
        span = sum(c.colspan for c in cells)
        if span > len(cols):
            if span == len(cols) + 1 and not raw[-1].strip():
                cells = cells[:-1]
                span -= 1
            else:
                raise BuildError(f"{path} row {r_idx + 1}: {span} columns but the spec has {len(cols)}")
        while span < len(cols):
            cells.append(Cell("", [""]))
            span += 1
        # cmidrules: mark covered cells
        for _, trim, a, b in cmids:
            a, b = int(a), int(b)
            pos = 1
            for c in cells:
                s0, s1 = pos, pos + c.colspan - 1
                if s0 >= a and s1 <= b:
                    c.cmid = trim
                elif s1 >= a and s0 <= b:
                    raise BuildError(f"{path}: \\cmidrule {a}-{b} cuts through a spanned cell")
                pos += c.colspan
        row.cells = cells
        out_rows.append(row)
    return Table(path, cols, out_rows, full, numbers)


def to_html(t: Table, *, table_id: str = "", caption_html: str = "") -> str:
    cls = "tbl" + (" full" if t.full_width else "")
    parts = [f'<table class="{cls}"' + (f' id="{table_id}"' if table_id else "") + ">"]
    if caption_html:
        parts.append(f'<caption class="sr-only">{caption_html}</caption>')
    if any(c.width for c in t.cols):
        parts.append("<colgroup>" + "".join(
            f'<col style="width:{c.width}">' if c.width else "<col>" for c in t.cols) + "</colgroup>")
    head_rows = [r for r in t.rows if r.head]
    body_rows = [r for r in t.rows if not r.head]
    if not body_rows:  # no \midrule at all: everything is body
        head_rows, body_rows = [], t.rows
    def row_html(r: Row, in_head: bool) -> str:
        rcls = []
        if r.rule_above:
            rcls.append("rule")
        if r.gap_before:
            rcls.append("b" + r.gap_before)
        if r.gap_after:
            rcls.append("a" + r.gap_after)
        cells = []
        for k, c in enumerate(r.cells):
            ccls = [f"a-{c.align}"]
            if c.indent:
                ccls.append(f"ind{min(c.indent, 3)}")
            if c.cmid:
                ccls.append("cm" + ("" if c.cmid == "-" else c.cmid))
            attrs = f' class="{" ".join(ccls)}"'
            if c.colspan > 1:
                attrs += f' colspan="{c.colspan}"'
            if in_head:
                tag = "th"
                attrs += ' scope="colgroup"' if c.colspan > 1 else ' scope="col"'
            elif k == 0 and c.html:
                tag = "th"
                attrs += ' scope="row"'
            else:
                tag = "td"
            cells.append(f"<{tag}{attrs}>{c.html}</{tag}>")
        return f'<tr{(" class=" + chr(34) + " ".join(rcls) + chr(34)) if rcls else ""}>' + "".join(cells) + "</tr>"
    if head_rows:
        parts.append("<thead>" + "".join(row_html(r, True) for r in head_rows) + "</thead>")
    parts.append("<tbody>" + "".join(row_html(r, False) for r in body_rows) + "</tbody>")
    parts.append("</table>")
    return "".join(parts)


def to_csv(t: Table) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in t.rows:
        depth = max((len(c.text_lines) for c in r.cells), default=1)
        for k in range(depth):
            line = []
            for c in r.cells:
                txt = c.text_lines[k] if k < len(c.text_lines) else ""
                line.append(txt)
                line += [""] * (c.colspan - 1)
            if any(x.strip() for x in line):
                w.writerow(line)
    return "\ufeff" + buf.getvalue()
