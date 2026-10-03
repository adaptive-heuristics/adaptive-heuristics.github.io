"""Snapshot the manuscript repository at one commit and prepare the two compile trees."""
from __future__ import annotations

import fnmatch
import io
import os
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .texscan import blank_comments, read_group, find_commands, parse_command
from .util import BuildError, info, read_text, run, sha256_text, write_text

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


@dataclass
class Snapshot:
    sha: str
    date: str                 # commit date (UTC, YYYY-MM-DD)
    root: Path                # extracted tree (read-only use)
    appendix: str             # appendix file name, e.g. 3_appendix_20260929.tex
    paper: str                # paper file name, e.g. 2_paper_20260924.tex
    fragments: list[str]      # table fragments \input by the appendix (relative paths with .tex)
    graphics: list[str]       # figure files \includegraphics'd by the appendix (relative paths)


def git(cfg: Config, *args: str, timeout: float = 120, check: bool = True):
    return run(["git", "-c", "core.autocrlf=false", "-C", str(cfg.source), *args],
               env=GIT_ENV, timeout=timeout, check=check)


def pull_ff_only(cfg: Config) -> str:
    res = git(cfg, "pull", "--ff-only", "-q", check=False)
    if res.returncode != 0:
        raise BuildError("could not fast-forward the manuscript clone (git pull --ff-only failed):\n"
                         + (res.stderr or res.stdout).strip())
    return res.stdout.strip()


def head(cfg: Config) -> tuple[str, str]:
    sha = git(cfg, "rev-parse", "HEAD").stdout.strip()
    iso = git(cfg, "show", "-s", "--format=%cI", sha).stdout.strip()
    date = datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%Y-%m-%d")
    return sha, date


def export(cfg: Config, sha: str, dest: Path) -> None:
    """Extract the committed tree at `sha` (never the working tree, which Overleaf may be rewriting)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    proc = subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(cfg.source), "archive", "--format=tar",
                           sha], capture_output=True, timeout=300, env={**os.environ, **GIT_ENV})
    if proc.returncode != 0:
        raise BuildError("git archive failed: " + proc.stderr.decode("utf-8", "replace"))
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tf:
        tf.extractall(dest, filter="data")


def find_appendix(root: Path, pattern: str) -> str:
    matches = sorted(p.name for p in root.iterdir() if p.is_file() and fnmatch.fnmatch(p.name, pattern))
    if not matches:
        raise BuildError(f"no file matching {pattern} in the manuscript root")
    return matches[-1]


def find_paper(appendix_text: str) -> str:
    text = blank_comments(appendix_text)
    m = re.search(r"\\myexternaldocument\s*\{([^}]+)\}", text) or \
        re.search(r"\\externaldocument(?:\[[^\]]*\])*\s*\{([^}]+)\}", text)
    if not m:
        raise BuildError("the appendix does not name the main paper (\\myexternaldocument / \\externaldocument)")
    name = m.group(1).strip()
    return name if name.endswith(".tex") else name + ".tex"


def appendix_inputs(appendix_text: str) -> tuple[list[str], list[str]]:
    """Table fragments and graphics that the (comment-blanked) appendix actually uses."""
    text = blank_comments(appendix_text)
    body_start = text.find("\\begin{document}")
    frags, graphics = [], []
    for i in find_commands(text, "input", body_start):
        arg, _ = read_group(text, i + len("\\input"))
        rel = arg.strip()
        frags.append(rel if rel.endswith(".tex") else rel + ".tex")
    for i in find_commands(text, "includegraphics", body_start):
        cmd = parse_command(text, i, 1, 1)
        graphics.append(cmd.args[0].strip())
    return list(dict.fromkeys(frags)), list(dict.fromkeys(graphics))


def sanitize_author(text: str, replacement: str) -> tuple[str, int]:
    """Replace every \\author{...} outside comments with \\author{replacement}. Returns (text, count)."""
    blank = blank_comments(text)
    hits = [m.start() for m in re.finditer(r"\\author(?![A-Za-z@])", blank)]
    if len(hits) > 1:
        raise BuildError(f"the appendix has {len(hits)} \\author commands; expected at most one")
    if not hits:
        return text, 0
    i = hits[0]
    _, end = read_group(blank, i + len("\\author"), what="\\author")
    return text[:i] + "\\author{" + replacement + "}" + text[end:], 1


def resolve_graphic(root: Path, rel: str) -> str:
    """Return the relative path of the file \\includegraphics{rel} picks up (pdf first, as pdflatex does)."""
    cand = [rel] if Path(rel).suffix else [rel + ext for ext in (".pdf", ".png", ".jpg", ".jpeg")]
    for c in cand:
        if (root / c).is_file():
            return c
    raise BuildError(f"graphic {rel} is not in the snapshot")


def take(cfg: Config, *, pull: bool) -> Snapshot:
    if pull:
        info("pulling the manuscript clone (fast-forward only)")
        pull_ff_only(cfg)
    sha, date = head(cfg)
    root = cfg.cache / "src" / sha
    if not (root / ".complete").exists():
        info(f"exporting snapshot {sha[:10]} ({date})")
        export(cfg, sha, root)
        (root / ".complete").write_text("ok")
    appendix = find_appendix(root, cfg.appendix_glob)
    atext = read_text(root / appendix)
    paper = find_paper(atext)
    if not (root / paper).is_file():
        raise BuildError(f"the appendix points to {paper}, which is not in the snapshot")
    frags, graphics = appendix_inputs(atext)
    graphics = [resolve_graphic(root, g) for g in graphics]
    for f in frags:
        if not (root / f).is_file():
            raise BuildError(f"table fragment {f} is not in the snapshot")
    return Snapshot(sha, date, root, appendix, paper, frags, graphics)


def build_trees(cfg: Config, snap: Snapshot) -> tuple[Path, Path, str]:
    """Create <cache>/build/<key>/{app,paper}. Returns (app_dir, paper_dir, key)."""
    atext = read_text(snap.root / snap.appendix)
    sanitized, n = sanitize_author(atext, cfg.author_replacement)
    key = f"{snap.sha[:12]}-{sha256_text(sanitized)[:8]}"
    base = cfg.cache / "build" / key
    app_dir, paper_dir = base / "app", base / "paper"
    if (base / ".trees").exists():
        return app_dir, paper_dir, key
    if base.exists():
        shutil.rmtree(base)
    info(f"preparing compile trees ({'author block removed' if n else 'no author block found'})")
    shutil.copytree(snap.root, paper_dir, ignore=shutil.ignore_patterns(".complete"))
    for p in paper_dir.glob("latexmkrc"):
        p.unlink()
    for p in paper_dir.glob(".latexmkrc"):
        p.unlink()
    app_dir.mkdir(parents=True)
    write_text(app_dir / snap.appendix, sanitized)
    for rel in snap.fragments + snap.graphics + cfg.bib_files + ([cfg.bst_file] if cfg.bst_file else []):
        src = snap.root / rel
        if not src.is_file():
            raise BuildError(f"{rel} is missing from the snapshot")
        dst = app_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    (base / ".trees").write_text("ok")
    return app_dir, paper_dir, key
