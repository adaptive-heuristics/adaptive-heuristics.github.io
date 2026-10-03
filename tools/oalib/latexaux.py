"""Parse LaTeX .aux files: labels, list-of-figures/tables entries, table of contents, natbib \\bibcite."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .texscan import read_group
from .util import read_text


@dataclass
class Label:
    name: str
    number: str     # e.g. "F4", "Online Appendix~A.1", "7", "2"
    page: str       # printed page label (NOT the physical page)
    title: str      # nameref title (caption text for floats)
    anchor: str     # hyperref destination name


@dataclass
class ContentsLine:
    kind: str       # figure / table / section / subsection / subsubsection / paragraph
    number: str     # "" when the entry has no \numberline
    title: str
    page: str
    anchor: str


@dataclass
class BibCite:
    key: str
    year: str
    short: str
    long: str


@dataclass
class Aux:
    labels: dict[str, Label] = field(default_factory=dict)
    lof: list[ContentsLine] = field(default_factory=list)
    lot: list[ContentsLine] = field(default_factory=list)
    toc: list[ContentsLine] = field(default_factory=list)
    bibcites: dict[str, BibCite] = field(default_factory=dict)


_ENTRY = re.compile(r"\\(newlabel|@writefile|bibcite)\b")


def _groups(text: str, i: int, n: int) -> tuple[list[str], int]:
    out = []
    for _ in range(n):
        g, i = read_group(text, i, what="aux entry")
        out.append(g)
    return out, i


def _contents(kind_file: str, body: str) -> ContentsLine | None:
    m = re.match(r"\s*\\contentsline\s*", body)
    if not m:
        return None
    (kind, entry, page, anchor), _ = _groups(body, m.end(), 4)
    number, title = "", entry
    nm = re.match(r"\s*\\numberline\s*", entry)
    if nm:
        number, j = read_group(entry, nm.end())
        title = entry[j:]
    title = re.sub(r"\\ignorespaces\s*", "", title).strip()
    return ContentsLine(kind.strip(), number.strip(), title, page.strip(), anchor.strip())


def parse_aux(path: Path) -> Aux:
    text = read_text(path)
    aux = Aux()
    for m in _ENTRY.finditer(text):
        kind = m.group(1)
        try:
            if kind == "newlabel":
                (name, data), _ = _groups(text, m.end(), 2)
                if name.endswith("@cref"):
                    continue
                parts, _ = _groups(data, 0, 4)
                aux.labels[name] = Label(name, parts[0].strip(), parts[1].strip(), parts[2].strip(),
                                         parts[3].strip())
            elif kind == "@writefile":
                (target, body), _ = _groups(text, m.end(), 2)
                line = _contents(target, body)
                if line is None:
                    continue
                {"lof": aux.lof, "lot": aux.lot, "toc": aux.toc}.get(target.strip(), []).append(line)
            else:
                (key, data), _ = _groups(text, m.end(), 2)
                (num, year, short, long), _ = _groups(data, 0, 4)
                aux.bibcites[key.strip()] = BibCite(key.strip(), year.strip(), _unbrace(short), _unbrace(long))
        except Exception:  # entries we do not need (e.g. \newlabel with fewer fields) are skipped
            continue
    return aux


def _unbrace(s: str) -> str:
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    return s
