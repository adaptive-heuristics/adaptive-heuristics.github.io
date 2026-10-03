"""Reference resolution, main-paper cross-links and the "group by main-paper section" assignment."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .latexaux import Aux
from .inline import RefTarget
from .pdfmap import PdfIndex
from .texdoc import Doc, Exhibit
from .texscan import blank_comments, find_env, read_group, read_opt
from .util import BuildError

KIND_WORD = {"tab": "Table", "fig": "Figure", "eq": "Equation", "sec": "Section", "subsec": "Section",
             "subsubsec": "Section"}


def exhibit_url(ex: Exhibit) -> str:
    return f"/{ex.kind}/{ex.number}/"


class Resolver:
    def __init__(self, doc: Doc, app_aux: Aux, paper_aux: Aux, pdf: PdfIndex, pdf_url: str):
        self.doc, self.app, self.paper, self.pdf, self.pdf_url = doc, app_aux, paper_aux, pdf, pdf_url
        self.by_label: dict[str, tuple[Exhibit, str | None]] = {}
        for ex in doc.exhibits:
            for p in ex.parts:
                if p.label:
                    self.by_label[p.label] = (ex, None)
                for pn in p.panels:
                    if pn.label:
                        self.by_label[pn.label] = (ex, pn.letter)

    def page_href(self, anchor: str) -> str:
        return f"{self.pdf_url}#page={self.pdf.page_of(anchor) + 1}"

    def ref(self, label: str) -> RefTarget:
        if label in self.app.labels:
            lab = self.app.labels[label]
            if label in self.by_label:
                ex, letter = self.by_label[label]
                href = exhibit_url(ex) + (f"#panel-{letter}" if letter else "")
                return RefTarget(lab.number, href, "oa-exhibit", label)
            kind = "oa-section" if label.startswith("app:") else "oa-question" if label.startswith("q:") else "oa-other"
            href = self.page_href(lab.anchor) if lab.anchor in self.pdf.names else None
            return RefTarget(lab.number, href, kind, label)
        if label in self.paper.labels:
            return RefTarget(self.paper.labels[label].number, None, "main", label)
        raise BuildError(f"\\ref{{{label}}} is defined neither in the appendix nor in the main paper")

    def cite(self, key: str) -> tuple[str, str, str]:
        b = self.app.bibcites.get(key)
        if not b:
            raise BuildError(f"\\cite{{{key}}} is not in the appendix bibliography")
        return b.short, b.year, b.long


# ---- the main paper's structure and its citations of OA labels ----------------------------------

@dataclass
class Heading:
    number: str          # "5.4.1" ("" for \paragraph)
    level: int           # 1..3, 4 = paragraph
    title: str           # LaTeX
    offset: int


@dataclass
class Citation:
    label: str           # OA label cited
    heading: str         # deepest numbered heading, e.g. "5.4.1"
    paragraph: str       # enclosing \paragraph title ("" if none)
    ctx: str             # body | footnote | float
    float_label: str | None
    panel: str | None
    offset: int


@dataclass
class PaperMap:
    headings: list[Heading]
    citations: list[Citation]
    first_body_heading: dict[str, str]       # main-paper label -> heading of its first body citation
    label_heading: dict[str, str]            # main-paper label -> heading where it is defined
    titles: dict[str, str] = field(default_factory=dict)   # heading number -> title (LaTeX)


def scan_paper(paper_text: str, oa_labels: set[str]) -> PaperMap:
    text = blank_comments(paper_text)
    begin = text.find("\\begin{document}")
    end = text.find("\\end{document}")
    headings: list[Heading] = []
    counters = [0, 0, 0]
    for m in re.finditer(r"\\(section|subsection|subsubsection|paragraph)(\*?)(?![A-Za-z@])", text):
        if m.start() < begin or m.start() > end:
            continue
        _, j = read_opt(text, m.end())
        try:
            title, _ = read_group(text, j)
        except BuildError:
            continue
        lvl = {"section": 1, "subsection": 2, "subsubsection": 3, "paragraph": 4}[m.group(1)]
        if lvl == 4:
            headings.append(Heading("", 4, title.strip(), m.start()))
            continue
        if m.group(2):
            continue
        counters[lvl - 1] += 1
        for k in range(lvl, 3):
            counters[k] = 0
        headings.append(Heading(".".join(str(c) for c in counters[:lvl]), lvl, title.strip(), m.start()))
    # footnote spans and float spans
    foot = []
    for m in re.finditer(r"\\footnote(?![A-Za-z@])", text):
        _, j = read_opt(text, m.end())
        try:
            _, k = read_group(text, j)
            foot.append((m.start(), k))
        except BuildError:
            pass
    floats = []
    for env in ("table", "figure", "table*", "figure*"):
        pos = 0
        while True:
            f = find_env(text, env, pos)
            if not f:
                break
            body = text[f[1]:f[2]]
            lm = re.search(r"\\label\s*\{([^}]+)\}", body)
            floats.append((f[0], f[3], lm.group(1).strip() if lm else None))
            pos = f[3]

    def heading_at(off: int) -> tuple[str, str]:
        num, para = "", ""
        for h in headings:
            if h.offset > off:
                break
            if h.level == 4:
                para = h.title
            else:
                num, para = h.number, ""
        return num, para

    cites: list[Citation] = []
    all_refs: list[tuple[str, int]] = []
    for m in re.finditer(r"\\(ref|cref|Cref|autoref|eqref|nameref)\s*\{([^}]+)\}", text):
        if m.start() < begin or m.start() > end:
            continue
        for lab in (x.strip() for x in m.group(2).split(",")):
            all_refs.append((lab, m.start()))
            if lab not in oa_labels:
                continue
            ctx, fl = "body", None
            for a, b, flab in floats:
                if a < m.start() < b:
                    ctx, fl = "float", flab
            if ctx == "body" and any(a < m.start() < b for a, b in foot):
                ctx = "footnote"
            after = text[m.end():m.end() + 40]
            pm = re.match(r"\s*(?:,\s*)?(?:Panels?[~\s]*)?\(([a-z])\)", after) or \
                re.match(r"\s*(?:,\s*)?Panel[~\s]*([A-Z])\b", after)
            before = text[max(0, m.start() - 30):m.start()]
            pb = re.search(r"Panels?[~\s]*\(?([a-zA-Z])\)?\s*(?:of|in)\s*(?:Online Appendix\s*)?(?:Table|Figure)?[~\s]*$",
                           before)
            panel = (pm.group(1) if pm else None) or (pb.group(1) if pb else None)
            num, para = heading_at(m.start())
            if ctx == "float":
                num, para = "", ""
            cites.append(Citation(lab, num, para, ctx, fl, panel, m.start()))
    first_body: dict[str, str] = {}
    for lab, off in all_refs:
        inside_float = any(a < off < b for a, b, _ in floats)
        if inside_float or lab in first_body:
            continue
        first_body[lab] = heading_at(off)[0]
    label_heading: dict[str, str] = {}
    for m in re.finditer(r"\\label\s*\{([^}]+)\}", text):
        if begin < m.start() < end:
            label_heading[m.group(1).strip()] = heading_at(m.start())[0]
    # float notes: attribute to where the float is first cited in the body
    for c in cites:
        if c.ctx == "float" and c.float_label:
            c.heading = first_body.get(c.float_label, "")
    titles = {h.number: h.title for h in headings if h.number}
    return PaperMap(headings, cites, first_body, label_heading, titles)


# ---- grouping ------------------------------------------------------------------------------------

FALLBACK = "zz"


def assign_groups(doc: Doc, forward: dict[str, list[str]], oa_refs: dict[str, list[str]],
                  pmap: PaperMap, paper_aux: Aux, overrides: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Return {exhibit id: (heading number or FALLBACK, rule)}."""
    groups: dict[str, tuple[str, str]] = {}
    by_label = {}
    for ex in doc.exhibits:
        for lab in ex.labels:
            by_label[lab] = ex
    cites_by_ex: dict[str, list[Citation]] = {}
    for c in pmap.citations:
        ex = by_label.get(c.label)
        if ex:
            cites_by_ex.setdefault(ex.id, []).append(c)
    sec_labels = {s.id: s.label for s in doc.sections}

    def rule_direct(ex):
        cs = cites_by_ex.get(ex.id, [])
        for ctx in ("body", "footnote", "float"):
            for c in sorted((c for c in cs if c.ctx == ctx), key=lambda c: c.offset):
                if c.heading:
                    return c.heading, f"cited ({ctx})"
        return None

    def rule_forward(ex):
        refs = [r for r in forward.get(ex.id, []) if r.split(":")[0] in ("tab", "fig", "eq")]
        if not refs:
            return None
        cnt = Counter(refs).most_common()
        if len(cnt) > 1 and cnt[0][1] == cnt[1][1]:
            return None
        lab = cnt[0][0]
        h = pmap.first_body_heading.get(lab) if not lab.startswith("eq:") else pmap.label_heading.get(lab)
        h = h or pmap.label_heading.get(lab)
        return (h, f"refers to {lab}") if h else None

    def rule_section(ex):
        for sid in (ex.subsection, ex.section):
            lab = sec_labels.get(sid) if sid else None
            if not lab:
                continue
            cs = sorted((c for c in pmap.citations if c.label == lab and c.heading), key=lambda c: c.offset)
            if cs:
                return cs[0].heading, f"section {sid} cited"
        return None

    pending = []
    for ex in doc.exhibits:
        if ex.id in overrides:
            groups[ex.id] = (overrides[ex.id], "override")
            continue
        g = rule_direct(ex) or rule_forward(ex)
        if g:
            groups[ex.id] = g
        else:
            pending.append(ex)
    # inherit through OA references (repeat until stable)
    changed = True
    while changed:
        changed = False
        for ex in list(pending):
            for lab in oa_refs.get(ex.id, []):
                tgt = by_label.get(lab)
                if tgt and tgt.id != ex.id and tgt.id in groups:
                    groups[ex.id] = (groups[tgt.id][0], f"inherits from {tgt.kind} {tgt.number}")
                    pending.remove(ex)
                    changed = True
                    break
    for ex in pending:
        groups[ex.id] = rule_section(ex) or (FALLBACK, "no main-paper link")
    return groups
