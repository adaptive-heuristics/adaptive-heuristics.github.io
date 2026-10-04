"""The appendix's reference list, read from the BibTeX output (.bbl) that the PDF typesets."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .texscan import read_group, read_opt
from .util import BuildError, read_text


@dataclass
class BibItem:
    key: str
    tex: str          # the formatted entry, as LaTeX


def parse_bbl(path: Path) -> list[BibItem]:
    """Every \\harvarditem (or \\bibitem) of the thebibliography environment, in the order the PDF prints them."""
    if not path.exists():
        return []
    s = read_text(path)
    a, b = s.find("\\begin{thebibliography}"), s.find("\\end{thebibliography}")
    if a < 0 or b < 0:
        raise BuildError(f"{path.name}: no thebibliography environment")
    _, body_at = read_group(s, a + len("\\begin{thebibliography}"))
    body = s[body_at:b]
    heads = list(re.finditer(r"\\(harvarditem|bibitem)(?![A-Za-z@])", body))
    out = []
    for i, m in enumerate(heads):
        j = m.end()
        _, j = read_opt(body, j)
        if m.group(1) == "harvarditem":
            for _ in range(2):                    # {long authors}{year}
                _, j = read_group(body, j)
        key, j = read_group(body, j)
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        out.append(BibItem(key.strip(), re.sub(r"\s+", " ", body[j:end]).strip()))
    return out


def link_urls(html: str) -> str:
    """Raw URLs in a rendered entry become links (a full stop after one ends the sentence, not the URL)."""
    def one(m: re.Match) -> str:
        url = m.group(0)
        tail = ""
        while url and url[-1] in ".,;:)":
            tail = url[-1] + tail
            url = url[:-1]
        return f'<a class="ext" href="{url}" rel="noopener noreferrer">{url}</a>{tail}'
    return re.sub(r"https?://[^\s<>\"]+", one, html)
