"""The appendix's text-only subsections (questionnaires and prompts) as web pages.

A text unit is a numbered subsection without figures or tables (A.1, B.1, C.1, ...). Its page carries the
subsection with its sub-subsections; the first unit of a section also carries the section's own introduction,
under the section heading, as the PDF does. Everything is read from the comment-blanked source, so nothing in a
LaTeX comment can reach a page.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field

from .texdoc import Doc, Section
from .texscan import find_env, read_group, read_opt
from .util import BuildError

FN_OPEN, FN_CLOSE = "\ue000", "\ue001"           # footnote marker token: FN_OPEN + number + FN_CLOSE
BLANK = "\ue002"                                 # answer field (\underline{\hspace{..}})
_DROP = re.compile(r"\\(clearpage|newpage|begingroup|endgroup|singlespacing|onehalfspacing|doublespacing)"
                   r"(?![A-Za-z@])")
_BLOCK = re.compile(r"\\begin\s*\{(enumerate|itemize|quote)\}|\\(paragraph|subsubsection|respbox)(?![A-Za-z@])"
                    r"|\{\s*\\(small|footnotesize)(?![A-Za-z@])")


@dataclass
class Unit:
    section: Section                  # the subsection the page is for
    top: Section                      # its section
    intro: tuple[int, int] | None     # the section's own introduction, when this is its first subsection
    span: tuple[int, int]             # the subsection's body
    subs: list[Section] = field(default_factory=list)

    @property
    def id(self) -> str:
        return "section/" + self.section.id

    @property
    def url(self) -> str:
        return f"/section/{self.section.id}/"


def heading_end(blank: str, offset: int) -> int:
    """Offset just after a \\(sub)section{...} heading and the \\label that follows it."""
    m = re.match(r"\\(?:sub)*section\*?", blank[offset:])
    if not m:
        raise BuildError(f"no heading at offset {offset}")
    _, j = read_opt(blank, offset + m.end())
    _, j = read_group(blank, j)
    lm = re.match(r"\s*\\label\s*\{[^}]*\}", blank[j:])
    return j + lm.end() if lm else j


def find_units(doc: Doc) -> list[Unit]:
    blank, secs = doc.blank, doc.sections
    end_doc = blank.find("\\end{document}")
    refs = re.search(r"\\(bibliography|printbibliography)(?![A-Za-z@])", blank)
    stop = refs.start() if refs else end_doc
    units = []
    for i, s in enumerate(secs):
        if s.level != 2 or s.has_exhibits:
            continue
        top = next(t for t in reversed(secs[:i]) if t.level == 1)
        nxt = next((t.offset for t in secs[i + 1:] if t.level <= 2), stop)
        first_child = next(t for t in secs if t.level == 2 and t.id.startswith(top.id + "."))
        intro = None
        if first_child is s:
            a, b = heading_end(blank, top.offset), s.offset
            if _DROP.sub("", blank[a:b]).strip():
                intro = (a, b)
        units.append(Unit(s, top, intro, (heading_end(blank, s.offset), nxt),
                          [t for t in secs[i + 1:] if t.level == 3 and t.offset < nxt]))
    return units


def site_targets(doc: Doc, units: list[Unit]) -> dict[str, str]:
    """Labels that now resolve to a page of this site: the units' headings and the questionnaire items in them."""
    out = {}
    for u in units:
        if u.intro and u.top.label:
            out[u.top.label] = u.url
        if u.section.label:
            out[u.section.label] = u.url + (f"#sec-{u.section.id}" if u.intro else "")
        for s in u.subs:
            if s.label:
                out[s.label] = f"{u.url}#sec-{s.id}"
        for a, b in [x for x in (u.intro, u.span) if x]:
            for m in re.finditer(r"\\item\s*\\label\s*\{(q:[^}]+)\}", doc.blank[a:b]):
                out[m.group(1)] = f"{u.url}#q-{m.group(1).split(':', 1)[1]}"
    return out


def footnote_numbers(doc: Doc) -> dict[int, int]:
    """Offset of every \\footnote in running text -> its number (the counter restarts at \\appendix)."""
    blank = doc.blank
    start = max(blank.find("\\appendix"), blank.find("\\begin{document}"))
    floats = []
    for env in ("figure", "table", "minipage"):
        pos = start
        while (e := find_env(blank, env, pos)):
            floats.append((e[0], e[3]))
            pos = e[3]
    out, n = {}, 0
    for m in re.finditer(r"\\footnote(?![A-Za-z@])", blank[start:]):
        at = start + m.start()
        if any(a < at < b for a, b in floats):
            continue
        n += 1
        out[at] = n
    return out


# ---- rendering -----------------------------------------------------------------------------------

class Page:
    """Renders one unit. `mk(where)` returns an Inline converter; footnotes are numbered as in the PDF."""

    def __init__(self, doc: Doc, unit: Unit, mk, fn_numbers: dict[int, int], mcq_start: int):
        self.doc, self.unit, self.mk, self.fn_numbers = doc, unit, mk, fn_numbers
        self.notes: dict[int, str] = {}          # raw LaTeX of the footnotes met on this page
        self.mcq = mcq_start                     # interviewer questions (quotes with a \respbox) seen so far
        self.subs = list(unit.subs)
        self.text: list[str] = []
        self.fn_text: list[str] = []             # kept apart: the PDF sets footnotes at the foot of each page

    # source text with footnotes and answer fields replaced by tokens
    def _prepare(self, a: int, b: int) -> str:
        blank = self.doc.blank
        out, i = [], a
        for m in re.finditer(r"\\footnote(?![A-Za-z@])", blank[a:b]):
            at = a + m.start()
            num = self.fn_numbers.get(at)
            if num is None:
                raise BuildError(f"footnote at {self.doc.loc(at)} has no number")
            body, end = read_group(blank, a + m.end(), what="\\footnote")
            out.append(blank[i:at])
            out.append(f"{FN_OPEN}{num}{FN_CLOSE}")
            self.notes[num] = body
            i = end
        out.append(blank[i:b])
        s = "".join(out)
        s = re.sub(r"\\underline\s*\{\s*\\hspace\*?\s*\{[^}]*\}\s*\}", BLANK, s)
        s = re.sub(r"\\setlist\s*\[[^\]]*\]\s*\{[^}]*\}", "", s)
        return _DROP.sub("", s)

    def body(self, a: int, b: int) -> str:
        return self.blocks(self._prepare(a, b), "body")

    def blocks(self, s: str, where: str) -> str:
        html, pos, runin = [], 0, None
        while True:
            m = _BLOCK.search(s, pos)
            seg = s[pos:m.start() if m else len(s)]
            if seg.strip():
                h, t = self._text(seg, where)
                if runin:
                    h = h.replace("<p>", f'<p><strong class="tx-run">{runin}</strong> ', 1) if h.startswith("<p>") \
                        else f'<p><strong class="tx-run">{runin}</strong></p>' + h
                    runin = None
                html.append(h)
                self.text.append(t)
            if not m:
                break
            if m.group(1):                                      # list or quote
                env = find_env(s, m.group(1), m.start())
                if m.group(1) == "quote":
                    html.append(self._quote(s[env[1]:env[2]], where))
                else:
                    html.append(self._list(m.group(1), s[env[1]:env[2]], where, depth=1))
                pos = env[3]
            elif m.group(2) == "paragraph":
                title, pos = read_group(s, m.end(), what="\\paragraph")
                r = self.mk(f"{where}: paragraph heading").inline(title)
                runin = r.html
                self.text.append(r.text)
            elif m.group(2) == "subsubsection":
                _, j = read_opt(s, m.end())
                title, j = read_group(s, j, what="\\subsubsection")
                lm = re.match(r"\s*\\label\s*\{[^}]*\}", s[j:])
                pos = j + (lm.end() if lm else 0)
                if not self.subs:
                    raise BuildError(f"{where}: unexpected \\subsubsection {title!r}")
                sec = self.subs.pop(0)
                r = self.mk(f"heading {sec.id}").inline(sec.title)
                html.append(f'<h3 id="sec-{sec.id}"><span class="tx-num">{sec.id}</span> {r.html}</h3>')
                self.text.append(r.text)
            elif m.group(2) == "respbox":
                arg, pos = read_group(s, m.end(), what="\\respbox")
                r = self.mk(f"{where}: response box").inline(arg)
                html.append(f'<p class="tx-resp">Responses: {r.html}</p>')
                self.text.append("Responses: " + r.text)
            else:                                               # {\small ...} wrapper around a list
                inner, pos = read_group(s, m.start(), what="size group")
                html.append(self.blocks(re.sub(r"^\s*\\(small|footnotesize)(?![A-Za-z@])", "", inner), where))
        if runin:
            html.append(f'<p><strong class="tx-run">{runin}</strong></p>')
        return "\n".join(x for x in html if x)

    def _text(self, seg: str, where: str) -> tuple[str, str]:
        r = self.mk(where).block(seg)
        return r.html, r.text

    def _quote(self, inner: str, where: str) -> str:
        qid = ""
        if re.search(r"\\respbox(?![A-Za-z@])", inner):
            self.mcq += 1
            qid = f' id="mcq-{self.mcq}"'
        return f'<blockquote class="tx-quote"{qid}>' + self.blocks(inner, where) + "</blockquote>"

    def _list(self, env: str, body: str, where: str, depth: int) -> str:
        opts, j = read_opt(body, 0)
        body = body[j:]
        if env == "itemize":
            kind = "radio" if opts and "bigcirc" in opts else "check" if opts and "square" in opts else "bullet"
            lis = []
            for it in _split_items(body):
                if _BLOCK.search(it) or "\n\n" in it.strip():
                    lis.append("<li>" + _unwrap(self.blocks(it, where)) + "</li>")
                else:
                    r = self.mk(f"{where}: list item").inline(it)
                    lis.append(f"<li>{r.html}</li>")
                    self.text.append(r.text)
            return f'<ul class="q-opts q-{kind}">' + "".join(lis) + "</ul>"
        # enumerate: explicit labels, as LaTeX prints them (1. / (a) / i.)
        items = _split_items(body)
        lis = []
        questionnaire = any(re.match(r"\s*\\label\s*\{q:", it) for it in items)
        for k, it in enumerate(items):
            lm = re.match(r"\s*\\label\s*\{([^}]+)\}", it)
            iid = ""
            if lm:
                it = it[lm.end():]
                if lm.group(1).startswith("q:"):
                    iid = f' id="q-{lm.group(1).split(":", 1)[1]}"'
            num = f"{k + 1}." if depth == 1 else f"({chr(ord('a') + k)})" if depth == 2 else f"{_roman(k + 1)}."
            inner = self._nested(it, where, depth)
            self.text.append(num)
            lis.append(f'<li{iid}><span class="tx-n">{num}</span><div>{inner}</div></li>')
        cls = "tx-ol tx-qs" if questionnaire else "tx-ol"
        return f'<ol class="{cls}">' + "".join(lis) + "</ol>"

    def _nested(self, it: str, where: str, depth: int) -> str:
        # an enumerate inside an item steps down one numbering level
        out, pos = [], 0
        for m in re.finditer(r"\\begin\s*\{enumerate\}", it):
            if m.start() < pos:
                continue
            env = find_env(it, "enumerate", m.start())
            if it[pos:m.start()].strip():
                out.append(self.blocks(it[pos:m.start()], where))
            out.append(self._list("enumerate", it[env[1]:env[2]], where, depth + 1))
            pos = env[3]
        if it[pos:].strip():
            out.append(self.blocks(it[pos:], where))
        return _unwrap("\n".join(out))

    def footnotes(self) -> list[dict]:
        out = []
        for num in sorted(self.notes):
            r = self.mk(f"footnote {num}").block(self.notes[num])
            out.append({"n": num, "html": _unwrap(r.html)})
            self.fn_text.append(r.text)
        return out


def _split_items(body: str) -> list[str]:
    out, depth, cur = [], 0, None
    for m in re.finditer(r"\\(begin|end)\s*\{(itemize|enumerate|description)\}|\\item(?![A-Za-z@])", body):
        if m.group(1) == "begin":
            depth += 1
        elif m.group(1) == "end":
            depth -= 1
        elif depth == 0:
            if cur is not None:
                out.append(body[cur:m.start()])
            cur = m.end()
    if cur is not None:
        out.append(body[cur:])
    return [x.strip() for x in out]


def _unwrap(h: str) -> str:
    """A lone paragraph inside a list item or a footnote needs no <p>."""
    h = h.strip()
    if h.startswith("<p>") and h.endswith("</p>") and h.count("<p>") == 1:
        return h[3:-4]
    return h


def _roman(n: int) -> str:
    return ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"][n - 1]


def finish(html: str) -> str:
    """Turn the footnote and answer-field tokens into markup."""
    html = re.sub(FN_OPEN + r"(\d+)" + FN_CLOSE,
                  lambda m: f'<sup class="fnref"><a id="fnref-{m.group(1)}" href="#fn-{m.group(1)}" '
                            f'aria-label="Footnote {m.group(1)}">{m.group(1)}</a></sup>', html)
    return html.replace(BLANK, '<span class="q-blank" aria-label="text field"></span>')


def plain(text: str) -> str:
    text = re.sub(FN_OPEN + r"\d+" + FN_CLOSE, "", text)
    return re.sub(r"\s+", " ", text.replace(BLANK, "____")).strip()


def esc(s: str) -> str:
    return _html.escape(s)
