"""Load oa.toml (tracked) and oa.local.toml (gitignored)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .util import BuildError

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Config:
    root: Path
    source: Path
    texbin: Path
    node: Path
    gh: Path
    appendix_glob: str
    bib_files: list[str]
    bst_file: str
    title: str
    paper_title: str
    base_url: str
    pdf_name: str
    note_position: str
    noindex: bool
    author_replacement: str
    identity_name: str
    identity_email: str
    github_user: str
    github_user_id: int
    repo: str
    figures: dict = field(default_factory=dict)
    checks: dict = field(default_factory=dict)
    banned_terms: list[str] = field(default_factory=list)

    @property
    def cache(self) -> Path:
        return self.root / ".cache"

    @property
    def docs(self) -> Path:
        return self.root / "docs"

    @property
    def site(self) -> Path:
        return self.root / "site"

    def tool(self, name: str) -> str:
        exe = self.texbin / (name + ".exe")
        return str(exe if exe.exists() else self.texbin / name)


def load(root: Path = ROOT) -> Config:
    tracked = root / "oa.toml"
    local = root / "oa.local.toml"
    if not local.exists():
        raise BuildError("oa.local.toml is missing; copy oa.local.example.toml and fill it in")
    with open(tracked, "rb") as fh:
        t = tomllib.load(fh)
    with open(local, "rb") as fh:
        l = tomllib.load(fh)
    p = l.get("paths", {})
    a = t.get("anonymity", {})
    s = t.get("site", {})
    src = t.get("source", {})
    cfg = Config(
        root=root,
        source=Path(p["source"]),
        texbin=Path(p["texbin"]),
        node=Path(p["node"]),
        gh=Path(p["gh"]),
        appendix_glob=src.get("appendix_glob", "3_appendix_*.tex"),
        bib_files=list(src.get("bib_files", [])),
        bst_file=src.get("bst_file", ""),
        title=s.get("title", "Online Appendix"),
        paper_title=s.get("paper_title", ""),
        base_url=s.get("base_url", "/"),
        pdf_name=s.get("pdf_name", "OA.pdf"),
        note_position=s.get("note_position", "below"),
        noindex=bool(s.get("noindex", True)),
        author_replacement=a.get("author_replacement", ""),
        identity_name=a["identity_name"],
        identity_email=a["identity_email"],
        github_user=a["github_user"],
        github_user_id=int(a["github_user_id"]),
        repo=a["repo"],
        figures=t.get("figures", {}),
        checks=t.get("checks", {}),
        banned_terms=list(l.get("anonymity", {}).get("banned_terms", [])),
    )
    if not cfg.banned_terms:
        raise BuildError("oa.local.toml has no banned_terms; the anonymity check cannot run without them")
    if cfg.note_position not in ("below", "paper"):
        raise BuildError("site.note_position must be 'below' or 'paper'")
    return cfg
