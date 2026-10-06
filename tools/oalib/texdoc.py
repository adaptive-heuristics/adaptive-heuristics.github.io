"""Parse the appendix .tex into sections and exhibits (figures/tables with parts, panels, notes, inputs)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .latexaux import Aux, ContentsLine
from .texscan import (blank_comments, find_commands, find_env, line_of, parse_command, read_group,
                      read_opt, skip_ws, strip_outer_braces)
from .util import BuildError, read_text


@dataclass
class Section:
    id: str            # "A", "A.1", "C.1.1"
    level: int         # 1 section, 2 subsection, 3 subsubsection
    title: str         # LaTeX
    label: str | None
    offset: int
    anchor: str = ""   # hyperref anchor from the toc (appendix.A, subsection.A.1, ...)
    has_exhibits: bool = False


@dataclass
class Graphic:
    path: str
    width: float | None    # fraction of \linewidth/\textwidth; None = natural size


@dataclass
class Panel:
    letter: str
    label: str | None
    subcaption: str        # LaTeX
    graphics: list[Graphic]
    width: float | None    # subfigure width fraction
    group: str | None      # \makebox heading above this panel (e.g. "GPT-Based Mapping")


@dataclass
class TableInput:
    path: str
    heading: str | None    # "Panel A: ..." (LaTeX)
    scale: float | None    # \scalebox factor
    size: str | None       # "scriptsize" wrapper
    coltypes: dict         # active user column types {name: (nargs, align)}
    offset: int
    full: bool = False     # set after \fullwidthtabular, which stretches the tabular to the text width


@dataclass
class Part:
    kind: str              # figure | table
    continued: bool
    caption: str           # LaTeX, outer \textbf removed
    label: str | None
    note: str | None       # LaTeX of the note minipage
    graphics: list[Graphic]
    panels: list[Panel]
    tables: list[TableInput]
    landscape: bool
    offset: int
    line: int
    number: str = ""       # from the lof/lot entry
    anchor: str = ""       # hyperref anchor of this part's caption


@dataclass
class Exhibit:
    kind: str
    number: str
    label: str
    parts: list[Part]
    section: str           # top-level section id ("F")
    subsection: str | None # e.g. "D.2"

    @property
    def id(self) -> str:
        return f"{self.kind}/{self.number}"

    @property
    def title(self) -> str:
        return self.parts[0].caption

    @property
    def labels(self) -> list[str]:
        out = []
        for p in self.parts:
            if p.label:
                out.append(p.label)
            out += [pn.label for pn in p.panels if pn.label]
        return out


@dataclass
class Doc:
    path: Path
    text: str
    blank: str
    title: str
    sections: list[Section]
    exhibits: list[Exhibit]
    preamble_coltypes: dict = field(default_factory=dict)

    def loc(self, offset: int) -> str:
        return f"{self.path.name}:{line_of(self.text, offset)}"


_FRAC = re.compile(r"^\s*([0-9.]*)\s*\\(linewidth|textwidth|columnwidth|hsize)\s*$")


def _frac(s: str | None) -> float | None:
    if not s:
        return None
    m = _FRAC.match(s)
    if not m:
        return None
    return float(m.group(1)) if m.group(1) else 1.0


def _width_opt(opts: str | None) -> float | None:
    if not opts:
        return None
    m = re.search(r"width\s*=\s*([^,\]]+)", opts)
    return _frac(m.group(1)) if m else None


def _coltype_def(spec: str) -> str:
    if "\\centering" in spec:
        return "c"
    if "\\raggedleft" in spec:
        return "r"
    return "l"


def _parse_coltypes(blank: str) -> list[tuple[int, int, str, int, str]]:
    """All \\newcolumntype definitions: (offset, scope_end, name, nargs, align)."""
    defs = []
    # group spans from \begingroup ... \endgroup (body only; preamble defs are global)
    spans = []
    stack = []
    for m in re.finditer(r"\\(begingroup|endgroup)(?![A-Za-z@])", blank):
        if m.group(1) == "begingroup":
            stack.append(m.start())
        elif stack:
            spans.append((stack.pop(), m.end()))
    for i in find_commands(blank, "newcolumntype"):
        j = i + len("\\newcolumntype")
        name, j = read_group(blank, j)
        nargs_s, j = read_opt(blank, j)
        spec, j = read_group(blank, j)
        scope_end = len(blank)
        for a, b in spans:
            if a < i < b:
                scope_end = min(scope_end, b)
        defs.append((i, scope_end, name.strip(), int(nargs_s) if nargs_s else 0, _coltype_def(spec)))
    return defs


def _active_coltypes(defs, offset: int) -> dict:
    active = {}
    for (i, end, name, nargs, align) in defs:
        if i < offset < end:
            active[name] = (nargs, align)
    return active


def _caption_text(raw: str) -> str:
    s = raw.strip()
    m = re.fullmatch(r"\\textbf\s*\{(.*)\}\s*", s, flags=re.S)
    if m:
        try:
            inner, end = read_group(s, s.index("{"))
            if end == len(s.rstrip()):
                s = inner
        except BuildError:
            pass
    return s.strip()


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    out = list(text)
    for a, b in spans:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "
    return "".join(out)


def _first_caption(content: str) -> tuple[str, int, int] | None:
    for m in re.finditer(r"\\caption(?![A-Za-z@])", content):
        cmd = parse_command(content, m.start(), 1, 1)
        return cmd.args[0], cmd.start, cmd.end
    return None


def _first_label(content: str, start: int = 0) -> str | None:
    m = re.compile(r"\\label\s*\{([^}]+)\}").search(content, start)
    return m.group(1).strip() if m else None


def _graphics(content: str) -> list[tuple[int, Graphic]]:
    out = []
    for i in find_commands(content, "includegraphics"):
        cmd = parse_command(content, i, 1, 1)
        out.append((i, Graphic(cmd.args[0].strip(), _width_opt(cmd.opts[0]))))
    return out


def _makeboxes(content: str) -> list[tuple[int, str]]:
    out = []
    for i in find_commands(content, "makebox"):
        cmd = parse_command(content, i, 1, 2)
        txt = re.sub(r"^\s*\\(small|footnotesize|normalsize|scriptsize)\b\s*", "", cmd.args[0]).strip()
        out.append((i, txt))
    return out


def _parse_part(kind: str, content: str, base: int, landscape: bool, coldefs, text: str,
                root: Path | None = None) -> Part:
    # subfigures first; mask them so float-level searches ignore their captions/labels/graphics
    sub_spans, panels_raw = [], []
    pos = 0
    while True:
        env = find_env(content, "subfigure", pos)
        if not env:
            break
        b0, c0, c1, e1 = env
        head = content[c0:c1]
        opt, j = read_opt(head, 0)
        width_s, j = read_group(head, j)
        inner = head[j:]
        cap = _first_caption(inner)
        panels_raw.append((b0, Panel(
            letter="", label=_first_label(inner),
            subcaption=cap[0].strip() if cap else "",
            graphics=[g for _, g in _graphics(inner)],
            width=_frac(width_s), group=None)))
        sub_spans.append((b0, e1))
        pos = e1
    masked = _mask(content, sub_spans)
    continued = "\\ContinuedFloat" in masked
    cap = _first_caption(masked)
    if not cap:
        raise BuildError(f"{kind} at line {line_of(text, base)} has no \\caption")
    caption_raw, cap_start, cap_end = cap
    label = _first_label(masked, cap_end)
    env = find_env(masked, "minipage")
    note = None
    note_span = (0, 0)
    if env:
        _, c0, c1, _ = env
        note_span = (env[0], env[3])
        head = masked[c0:c1]
        _, j = read_group(head, 0)  # width argument
        note = content[c0 + j:c1].strip()
        note = re.sub(r"^\\(small|footnotesize|normalsize)\b\s*", "", note).strip()
        note = _expand_inputs(note, root, text, base)
    boxes = _makeboxes(masked)
    # group headings for subfigures
    for b0, pn in panels_raw:
        heads = [t for (i, t) in boxes if i < b0]
        pn.group = heads[-1] if heads else None
    panels = [p for _, p in panels_raw]
    graphics = [g for (i, g) in _graphics(masked)]
    tables = []
    for i in find_commands(masked, "input"):
        if note_span[0] <= i < note_span[1]:
            continue  # the note's own text, kept in a file of its own
        arg, _ = read_group(masked, i + len("\\input"))
        rel = arg.strip()
        rel = rel if rel.endswith(".tex") else rel + ".tex"
        heads = [t for (k, t) in boxes if k < i]
        before = masked[max(0, i - 160):i]
        scale = None
        sm = re.search(r"\\scalebox\s*\{([0-9.]+)\}\s*\{\s*$", before)
        if sm:
            scale = float(sm.group(1))
        size = None
        sm2 = list(re.finditer(r"\{\s*\\(scriptsize|footnotesize|tiny)\b", before))
        if sm2:
            tail = before[sm2[-1].start():]
            depth = sum(1 if ch == "{" else -1 if ch == "}" else 0 for ch in tail)
            if depth > 0:  # the size group is still open at the \input
                size = sm2[-1].group(1)
        full = bool(re.search(r"\\fullwidthtabular(?![A-Za-z@])", masked[:i]))
        tables.append(TableInput(rel, heads[-1] if heads else None, scale, size,
                                 _active_coltypes(coldefs, base + i), base + i, full))
    return Part(kind=kind, continued=continued, caption=_caption_text(caption_raw), label=label, note=note,
                graphics=graphics, panels=panels, tables=tables, landscape=landscape,
                offset=base, line=line_of(text, base))


def _expand_inputs(note: str, root: Path | None, text: str, base: int) -> str:
    """A note may keep its text in a file of its own (\\input{tables/..._note.tex}): read it in, comments removed."""
    def one(m: re.Match) -> str:
        rel = m.group(1).strip()
        rel = rel if rel.endswith(".tex") else rel + ".tex"
        if root is None or not (root / rel).is_file():
            raise BuildError(f"the note at line {line_of(text, base)} inputs {rel}, which is not in the compile tree")
        return blank_comments(read_text(root / rel)).strip()
    return re.sub(r"\\input\s*\{([^}]+)\}", one, note)


def parse_appendix(path: Path, aux: Aux) -> Doc:
    text = read_text(path)
    blank = blank_comments(text)
    begin = blank.find("\\begin{document}")
    if begin < 0:
        raise BuildError("appendix has no \\begin{document}")
    end_doc = blank.find("\\end{document}")
    tm = re.search(r"\\title\s*\{", blank)
    title = read_group(blank, tm.end() - 1)[0] if tm else ""
    coldefs = _parse_coltypes(blank)
    preamble_coltypes = _active_coltypes([d for d in coldefs if d[0] < begin], begin)

    # --- sections (numbered like the appendix: A, A.1, C.1.1) ---
    sections: list[Section] = []
    counters = [0, 0, 0]
    appendix_at = blank.find("\\appendix", begin)
    for m in re.finditer(r"\\(section|subsection|subsubsection)(\*?)(?![A-Za-z@])", blank[:end_doc]):
        if m.start() < begin or (appendix_at >= 0 and m.start() < appendix_at):
            continue
        level = {"section": 1, "subsection": 2, "subsubsection": 3}[m.group(1)]
        _, j = read_opt(blank, m.end())
        title_s, j = read_group(blank, j)
        if m.group(2):
            continue  # unnumbered (e.g. the references heading)
        counters[level - 1] += 1
        for k in range(level, 3):
            counters[k] = 0
        letter = chr(ord("A") + counters[0] - 1)
        sid = letter if level == 1 else letter + "." + ".".join(str(c) for c in counters[1:level])
        lm = re.match(r"\s*\\label\s*\{([^}]+)\}", blank[j:j + 200])
        sections.append(Section(sid, level, title_s.strip(), lm.group(1).strip() if lm else None, m.start()))
    toc_numbered = [t for t in aux.toc if t.kind in ("section", "subsection", "subsubsection") and t.number]
    if len(toc_numbered) != len(sections):
        raise BuildError(f"parsed {len(sections)} numbered headings but the .toc lists {len(toc_numbered)}")
    for s, t in zip(sections, toc_numbered):
        expect = "Online Appendix~" + s.id
        if t.number.replace("\\ ", " ") != expect:
            raise BuildError(f"heading {s.id} {s.title!r} does not match toc entry {t.number!r}")
        s.anchor = t.anchor

    # --- floats ---
    land_spans = []
    pos = begin
    while True:
        env = find_env(blank, "landscape", pos)
        if not env:
            break
        land_spans.append((env[0], env[3]))
        pos = env[3]
    parts: list[Part] = []
    float_re = re.compile(r"\\begin\s*\{(figure|table)\}")
    pos = begin
    while True:
        m = float_re.search(blank, pos, end_doc)
        if not m:
            break
        env = find_env(blank, m.group(1), m.start())
        b0, c0, c1, e1 = env
        content = blank[c0:c1]
        opt, j = read_opt(content, 0)
        landscape = any(a < b0 < b for a, b in land_spans)
        part = _parse_part(m.group(1), content, c0, landscape, coldefs, text, path.parent)
        # use the original (un-blanked) note text but with comments removed
        parts.append(part)
        pos = e1

    # align parts with lof/lot entries (one caption each, in order)
    for kind, entries in (("figure", aux.lof), ("table", aux.lot)):
        mine = [p for p in parts if p.kind == kind]
        if len(mine) != len(entries):
            raise BuildError(f"{len(mine)} {kind} parts in the .tex but {len(entries)} entries in the list of "
                             f"{kind}s")
        for p, e in zip(mine, entries):
            p.number, p.anchor = e.number, e.anchor
            if p.label and p.label in aux.labels and aux.labels[p.label].number != e.number:
                raise BuildError(f"label {p.label} is {aux.labels[p.label].number} but its caption is {e.number}")

    # group parts into exhibits
    exhibits: list[Exhibit] = []
    for p in parts:
        if p.continued:
            prev = next((x for x in reversed(exhibits) if x.kind == p.kind), None)
            if prev is None or prev.number != p.number:
                raise BuildError(f"continued {p.kind} at line {p.line} has no parent with number {p.number}")
            prev.parts.append(p)
            continue
        if not p.label:
            raise BuildError(f"{p.kind} {p.number} at line {p.line} has no \\label")
        secs = [s for s in sections if s.offset < p.offset]
        top = next((s.id for s in reversed(secs) if s.level == 1), "")
        sub = next((s.id for s in reversed(secs) if s.level == 2 and s.id.startswith(top + ".")), None)
        exhibits.append(Exhibit(p.kind, p.number, p.label, [p], top, sub))

    # panel letters: subfigure order across all parts of an exhibit (aux sub@ labels win)
    for ex in exhibits:
        k = 0
        for p in ex.parts:
            for pn in p.panels:
                letter = chr(ord("a") + k)
                if pn.label and ("sub@" + pn.label) in aux.labels:
                    letter = aux.labels["sub@" + pn.label].number
                pn.letter = letter
                k += 1
    for s in sections:
        s.has_exhibits = any(ex.section == s.id or ex.subsection == s.id for ex in exhibits)
    return Doc(path, text, blank, title.strip(), sections, exhibits, preamble_coltypes)


def note_source(doc: Doc, part: Part) -> str | None:
    """The note with comments removed (the blanked text keeps offsets, so this is already comment-free)."""
    return part.note
