"""Convert LaTeX captions, notes and table cells to HTML (plus plain text for search and CSV).

Strict: an unknown control sequence raises BuildError with the location, so nothing is silently dropped.
"""
from __future__ import annotations

import html as _html
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol

from .mathml import MathRegistry
from .texscan import read_group, read_opt
from .util import BuildError


@dataclass
class RefTarget:
    text: str
    href: str | None
    kind: str          # oa-exhibit | oa-section | oa-question | oa-other | main
    label: str


class Resolver(Protocol):
    def ref(self, label: str) -> RefTarget: ...
    def cite(self, key: str) -> tuple[str, str, str]: ...   # (short authors, year, long authors)


SYMBOLS = {
    "%": "%", "&": "&amp;", "$": "$", "#": "#", "_": "_", "{": "{", "}": "}", " ": " ",
    ",": "\u202f", ";": "\u2005", ":": "\u2005", "!": "", "@": "", "/": "", "-": "",
    "ldots": "…", "dots": "…", "textellipsis": "…", "textendash": "–", "textemdash": "—",
    "textbackslash": "\\", "textasciitilde": "~", "S": "§", "P": "¶", "dag": "†", "ddag": "‡",
    "textdollar": "$", "checkmark": "✓", "textregistered": "®", "texttrademark": "™", "copyright": "©",
    "pounds": "£", "euro": "€", "textquoteright": "’", "textquoteleft": "‘", "textquotedblleft": "“",
    "textquotedblright": "”", "textbar": "|", "textless": "&lt;", "textgreater": "&gt;", "quad": "\u2003",
    "qquad": "\u2003\u2003", "enspace": "\u2002", "thinspace": "\u202f", "nobreakspace": "\u00a0",
    "textunderscore": "_", "textperiodcentered": "·", "textbullet": "•", "slash": "/",
    "\n": " ",  # backslash at the end of a line = control space
}
IGNORE0 = {
    "small", "footnotesize", "scriptsize", "tiny", "normalsize", "large", "Large", "LARGE", "huge", "Huge",
    "centering", "raggedright", "raggedleft", "noindent", "indent", "smallskip", "medskip", "bigskip",
    "ignorespaces", "protect", "relax", "unskip", "nopagebreak", "pagebreak", "hfill", "hfil", "vfill",
    "allowbreak", "sloppy", "normalfont", "rmfamily", "upshape", "mdseries", "null", "strut", "leavevmode",
    "phantomsection", "nolinebreak", "xspace", "arraybackslash", "displaystyle",
}
IGNORE1 = {"vspace", "vspace*", "label", "setstretch", "enlargethispage", "phantom", "hphantom", "vphantom"}
FORMAT1 = {
    "textbf": ("<strong>", "</strong>"), "textit": ("<em>", "</em>"), "emph": ("<em>", "</em>"),
    "textsl": ("<em>", "</em>"), "textsc": ('<span class="sc">', "</span>"), "texttt": ("<code>", "</code>"),
    "textrm": ("", ""), "textnormal": ("", ""), "textup": ("", ""), "textmd": ("", ""), "mbox": ("", ""),
    "text": ("", ""), "hbox": ("", ""), "textsf": ("", ""), "underline": ("<u>", "</u>"),
    "textsuperscript": ("<sup>", "</sup>"), "textsubscript": ("<sub>", "</sub>"), "blue": ("", ""),
    "red": ("", ""), "nohyphens": ("", ""),
}
DECL = {"itshape": ("<em>", "</em>"), "em": ("<em>", "</em>"), "bfseries": ("<strong>", "</strong>"),
        "scshape": ('<span class="sc">', "</span>")}
ACCENTS = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308", "~": "\u0303", "=": "\u0304",
           ".": "\u0307", "c": "\u0327", "v": "\u030c", "u": "\u0306", "H": "\u030b", "r": "\u030a",
           "k": "\u0328"}

SIMPLE_MATH = {
    "+": "+", "-": "\u2212", "\\checkmark": "✓", "\\times": "×", "\\le": "≤", "\\leq": "≤", "\\ge": "≥",
    "\\geq": "≥", "<": "&lt;", ">": "&gt;", "=": "=", "\\pm": "±", "\\cdot": "·", "\\rightarrow": "→",
    "\\to": "→", "\\%": "%", "\\approx": "≈", "\\neq": "≠", "\\sim": "∼",
}
MATH_TEXT = {
    "\\eta": "η", "\\alpha": "α", "\\beta": "β", "\\gamma": "γ", "\\delta": "δ", "\\Delta": "Δ",
    "\\varepsilon": "ε", "\\epsilon": "ε", "\\theta": "θ", "\\lambda": "λ", "\\Lambda": "Λ", "\\mu": "μ",
    "\\sigma": "σ", "\\Sigma": "Σ", "\\tau": "τ", "\\rho": "ρ", "\\phi": "φ", "\\pi": "π", "\\omega": "ω",
    "\\times": "×", "\\le": "≤", "\\leq": "≤", "\\ge": "≥", "\\geq": "≥", "\\cdot": "·", "\\rightarrow": "→",
    "\\to": "→", "\\checkmark": "✓", "\\in": "∈", "\\sum": "∑", "\\infty": "∞", "\\approx": "≈",
    "\\neq": "≠", "\\pm": "±", "\\Pr": "Pr", "\\ln": "ln", "\\log": "log", "\\exp": "exp", "\\%": "%",
    "\\sim": "∼", "\\mid": "|",
}
SUPERSCRIPTS = str.maketrans("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻")
DISPLAY_ENVS = ("equation*", "equation", "align*", "align", "gather*", "displaymath")


def math_text(tex: str) -> str:
    """Rough plain-text rendering of math (for search and CSV only)."""
    t = tex
    t = re.sub(r"\\(?:mathrm|text|textit|mathit|operatorname|mathbf|textbf|mathsf|mbox)\s*\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"\\(left|right|big|Big|bigg|Bigg)\b", "", t)
    t = re.sub(r"\\[!,;:> ]", " ", t)
    t = t.replace("{,}", ",")
    for k in sorted(MATH_TEXT, key=len, reverse=True):
        t = re.sub(re.escape(k) + r"(?![A-Za-z])", MATH_TEXT[k], t)
    t = re.sub(r"\^\{?([0-9+\-])\}?", lambda m: m.group(1).translate(SUPERSCRIPTS), t)
    t = re.sub(r"[\^_]\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"[\^_]", "", t)
    t = re.sub(r"\\[A-Za-z]+", "", t)
    t = t.replace("{", "").replace("}", "").replace("~", " ")
    t = t.replace("-", "−") if re.fullmatch(r"\s*-\s*[\d.]*\s*", t) else t
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class Rendered:
    html: str
    text: str
    refs: list[str] = field(default_factory=list)
    cites: list[str] = field(default_factory=list)


class Inline:
    """One converter per call site (`where` is used in error messages)."""

    def __init__(self, resolver: Resolver, math: MathRegistry, where: str, *, link_refs: bool = True):
        self.r, self.math, self.where, self.link_refs = resolver, math, where, link_refs
        self.refs: list[str] = []
        self.cites: list[str] = []

    # -- public -------------------------------------------------------------------------------
    def inline(self, src: str) -> Rendered:
        blocks = self._run(src, block=False)
        html_out = "".join(b[1] for b in blocks if b[0] == "p")
        text_out = " ".join(b[2] for b in blocks if b[0] == "p")
        return Rendered(html_out.strip(), _squash(text_out), self.refs, self.cites)

    def block(self, src: str) -> Rendered:
        blocks = self._run(src, block=True)
        out, txt = [], []
        for kind, h, t in blocks:
            if kind == "p":
                h = h.strip()
                if h:
                    out.append(f"<p>{h}</p>")
                    txt.append(_squash(t))
            else:
                out.append(f'<div class="eq">{h}</div>')
                txt.append(t)
        return Rendered("\n".join(out), "\n\n".join(txt), self.refs, self.cites)

    # -- engine -------------------------------------------------------------------------------
    def _err(self, msg: str, s: str, i: int):
        ctx = s[max(0, i - 40):i + 40].replace("\n", " ")
        raise BuildError(f"{self.where}: {msg} near …{ctx}…")

    def _run(self, s: str, *, block: bool):
        self.s = s
        blocks: list[list] = [["p", "", ""]]
        h, t = self._seq(s, 0, len(s), block=block, blocks=blocks)
        blocks[-1][1] += h
        blocks[-1][2] += t
        return [tuple(b) for b in blocks]

    def _seq(self, s: str, i: int, end: int, *, block: bool, blocks=None) -> tuple[str, str]:
        """Convert s[i:end]. When `blocks` is given (top level of a block), paragraph breaks and
        display math close the current paragraph."""
        H: list[str] = []
        T: list[str] = []
        wrap_stack: list[tuple[str, str]] = []
        while i < end:
            c = s[i]
            if c == "\\":
                # display math environments
                m = re.match(r"\\begin\s*\{(" + "|".join(re.escape(e) for e in DISPLAY_ENVS) + r")\}", s[i:end])
                if m:
                    env = m.group(1)
                    close = s.find("\\end{" + env + "}", i)
                    if close < 0:
                        self._err(f"unclosed {env}", s, i)
                    tex = s[i + m.end():close]
                    if blocks is None:
                        self._err("display math is only allowed in notes", s, i)
                    self._flush(blocks, H, T)
                    H, T = [], []
                    tex = re.sub(r"\\label\s*\{[^}]*\}", "", tex)
                    blocks.append(["eq", self.math.add(tex, True, self.where), math_text(tex)])
                    blocks.append(["p", "", ""])
                    i = close + len("\\end{" + env + "}")
                    continue
                if s.startswith("\\[", i):
                    close = s.find("\\]", i)
                    if close < 0 or blocks is None:
                        self._err("display math \\[ is only allowed in notes", s, i)
                    tex = s[i + 2:close]
                    self._flush(blocks, H, T)
                    H, T = [], []
                    blocks.append(["eq", self.math.add(tex, True, self.where), math_text(tex)])
                    blocks.append(["p", "", ""])
                    i = close + 2
                    continue
                i = self._command(s, i, end, H, T, block=block, blocks=blocks, wrap_stack=wrap_stack)
                continue
            if c == "{":
                inner, j = read_group(s, i)
                h, t = self._seq(s, i + 1, j - 1, block=block)
                H.append(h)
                T.append(t)
                i = j
                continue
            if c == "}":
                self._err("unbalanced '}'", s, i)
            if c == "$":
                if s.startswith("$$", i):
                    close = s.find("$$", i + 2)
                    if close < 0 or blocks is None:
                        self._err("display math $$ is only allowed in notes", s, i)
                    tex = s[i + 2:close]
                    self._flush(blocks, H, T)
                    H, T = [], []
                    blocks.append(["eq", self.math.add(tex, True, self.where), math_text(tex)])
                    blocks.append(["p", "", ""])
                    i = close + 2
                    continue
                j = i + 1
                while j < end:
                    if s[j] == "\\":
                        j += 2
                        continue
                    if s[j] == "$":
                        break
                    j += 1
                if j >= end:
                    self._err("unclosed $", s, i)
                h, t = self._math_inline(s[i + 1:j])
                H.append(h)
                T.append(t)
                i = j + 1
                continue
            if c == "~":
                H.append("\u00a0")
                T.append(" ")
                i += 1
                continue
            if c == "`":
                if s.startswith("``", i):
                    H.append("“"); T.append("“"); i += 2
                else:
                    H.append("‘"); T.append("‘"); i += 1
                continue
            if c == "'":
                if s.startswith("''", i):
                    H.append("”"); T.append("”"); i += 2
                else:
                    H.append("’"); T.append("’"); i += 1
                continue
            if c == "-":
                if s.startswith("---", i):
                    H.append("—"); T.append("—"); i += 3
                elif s.startswith("--", i):
                    H.append("–"); T.append("–"); i += 2
                else:
                    H.append("-"); T.append("-"); i += 1
                continue
            if c in " \t\n":
                j = i
                while j < end and s[j] in " \t\n":
                    j += 1
                if s.count("\n", i, j) >= 2 and blocks is not None:
                    self._flush(blocks, H, T)
                    H, T = [], []
                    blocks.append(["p", "", ""])
                else:
                    H.append(" ")
                    T.append(" ")
                i = j
                continue
            if c in "&#^_":
                self._err(f"unexpected '{c}' in text", s, i)
            H.append(_html.escape(c, quote=False))
            T.append(c)
            i += 1
        while wrap_stack:
            pre, post = wrap_stack.pop()
            H.append(post)
        return "".join(H), "".join(T)

    @staticmethod
    def _flush(blocks, H, T):
        blocks[-1][1] += "".join(H)
        blocks[-1][2] += "".join(T)

    def _arg(self, s: str, i: int) -> tuple[str, int]:
        return read_group(s, i, what=f"argument in {self.where}")

    def _command(self, s, i, end, H, T, *, block, blocks, wrap_stack) -> int:
        m = re.match(r"\\([A-Za-z@]+\*?|.|\n)", s[i:end])
        if not m:
            self._err("stray backslash", s, i)
        name = m.group(1)
        j = i + m.end()
        if re.fullmatch(r"[A-Za-z@]+\*?", name):
            while j < end and s[j] in " \t" :
                j += 1
            if j < end and s[j] == "\n" and not s.startswith("\n\n", j):
                j += 1
        if name == "\\":
            _, j = read_opt(s, j)
            H.append("<br>")
            T.append(" ")
            return j
        if name in ("par",):
            if blocks is not None:
                self._flush(blocks, H, T)
                H.clear(); T.clear()
                blocks.append(["p", "", ""])
            else:
                H.append(" "); T.append(" ")
            return j
        if name in ("newline", "linebreak"):
            _, j = read_opt(s, j)
            H.append("<br>"); T.append(" ")
            return j
        if name in SYMBOLS:
            H.append(SYMBOLS[name])
            T.append(_html.unescape(SYMBOLS[name]) if SYMBOLS[name] != "<br>" else " ")
            return j
        if name in IGNORE0:
            return j
        if name in IGNORE1:
            _, j = read_opt(s, j)
            _, j = self._arg(s, j)
            return j
        if name in ("hspace", "hspace*"):
            arg, j = self._arg(s, j)
            if not re.fullmatch(r"\s*0(pt|em|mm|cm)?\s*", arg):
                H.append(" "); T.append(" ")
            return j
        if name in DECL:
            pre, post = DECL[name]
            H.append(pre)
            wrap_stack.append((pre, post))
            return j
        if name in FORMAT1:
            arg, j2 = self._arg(s, j)
            h, t = self._seq(s, j + (s[j:].index("{")) + 1, j2 - 1, block=False)
            pre, post = FORMAT1[name]
            H.append(pre + h + post)
            T.append(t)
            return j2
        if name == "textcolor":
            _, j = self._arg(s, j)
            arg, j2 = self._arg(s, j)
            h, t = self._seq(s, s.index("{", j) + 1, j2 - 1, block=False)
            H.append(h); T.append(t)
            return j2
        if name in ACCENTS:
            if j < end and s[j] == "{":
                arg, j2 = self._arg(s, j)
            else:
                arg, j2 = s[j], j + 1
            ch = unicodedata.normalize("NFC", arg.strip()[:1] + ACCENTS[name]) + arg.strip()[1:]
            H.append(_html.escape(ch)); T.append(ch)
            return j2
        if name in ("ref", "eqref"):
            label, j2 = self._arg(s, j)
            tgt = self.r.ref(label.strip())
            self.refs.append(label.strip())
            text = tgt.text if name == "ref" else f"({tgt.text})"
            html_text = _html.escape(text).replace("~", "\u00a0")
            plain = text.replace("~", " ")
            if tgt.href and self.link_refs:
                newtab = ' target="_blank" rel="noopener"' if tgt.href.startswith("/pdf/") else ""
                H.append(f'<a class="ref" href="{_html.escape(tgt.href)}" data-ref="{_html.escape(tgt.label)}"'
                         f'{newtab}>{html_text}</a>')
            else:
                H.append(html_text)
            T.append(plain)
            return j2
        if name in ("citet", "citep", "citealt", "citealp", "citeauthor", "citeyear", "cite", "citet*", "citep*"):
            o1, j = read_opt(s, j)
            o2, j = read_opt(s, j)
            keys, j2 = self._arg(s, j)
            pre, post = (None, o1) if o2 is None else (o1, o2)
            base = name.rstrip("*")
            items = []
            for key in [k.strip() for k in keys.split(",") if k.strip()]:
                short, year, _long = self.r.cite(key)
                self.cites.append(key)
                if base in ("citet", "cite"):
                    item = f"{short} ({year})"
                elif base == "citealt":
                    item = f"{short} {year}"
                elif base in ("citep", "citealp"):
                    item = f"{short}, {year}"
                elif base == "citeauthor":
                    item = short
                else:
                    item = year
                items.append([item, self.r.cite_href(key) if self.link_refs else None])
            if post and base in ("citet", "cite"):
                items[-1][0] = items[-1][0][:-1] + f", {post})"
            def clean(x: str) -> str:
                return re.sub(r"\\(?:&)", "&", x.replace("~", "\u00a0"))
            # each work links to its entry on the references page
            texts = [clean(t) for t, _ in items]
            links = [f'<a class="cite" href="{_html.escape(h)}">{_html.escape(t)}</a>' if h else _html.escape(t)
                     for t, (_, h) in zip(texts, items)]
            body_t, body_h = "; ".join(texts), "; ".join(links)
            if base == "citep":
                a, b = clean(f"{pre} " if pre else ""), clean(f", {post}" if post else "")
                body_t = f"({a}{body_t}{b})"
                body_h = f"({_html.escape(a)}{body_h}{_html.escape(b)})"
            H.append(body_h); T.append(body_t)
            return j2
        if name in ("url", "nolinkurl"):
            url, j2 = self._arg(s, j)
            url = url.strip()
            esc = _html.escape(url)
            H.append(f'<a class="ext" href="{esc}" rel="noopener noreferrer">{esc}</a>')
            T.append(url)
            return j2
        if name == "href":
            url, j2 = self._arg(s, j)
            h, t = None, None
            arg, j3 = self._arg(s, j2)
            h, t = self._seq(s, s.index("{", j2) + 1, j3 - 1, block=False)
            H.append(f'<a class="ext" href="{_html.escape(url.strip())}" rel="noopener noreferrer">{h}</a>')
            T.append(t)
            return j3
        if name == "makecell":
            _, j = read_opt(s, j)
            arg, j2 = self._arg(s, j)
            h, t = self._seq(s, s.index("{", j) + 1, j2 - 1, block=False)
            H.append(h); T.append(t)
            return j2
        if name in ("(",):
            close = s.find("\\)", j)
            h, t = self._math_inline(s[j:close])
            H.append(h); T.append(t)
            return close + 2
        self._err(f"unsupported command \\{name}", s, i)
        return j

    def _math_inline(self, tex: str) -> tuple[str, str]:
        t = tex.strip()
        if t in SIMPLE_MATH:
            return f'<span class="m">{SIMPLE_MATH[t]}</span>', _html.unescape(SIMPLE_MATH[t])
        if re.fullmatch(r"-?\d+(?:\{,\}\d{3})*(?:\.\d+)?", t):
            v = t.replace("{,}", ",").replace("-", "\u2212")
            return f'<span class="m">{v}</span>', v
        return self.math.add(t, False, self.where), math_text(t)


def _squash(t: str) -> str:
    return re.sub(r"[ \t\n\u00a0]+", " ", t).strip()
