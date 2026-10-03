"""Compile the anonymized appendix (and the paper, for its labels) with pdflatex/bibtex. Never latexmk."""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .util import BuildError, info, read_text, run, sha256_file, warn, write_json

TEX_ENV = {
    "max_print_line": "10000",
    "error_line": "254",
    "half_error_line": "238",
    "openout_any": "p",
}
# Neutralize pdfTeX's provenance metadata: no /PTEX.* (file paths, included-PDF info), no dates, no trailer ID.
PRELUDE = r"\pdfsuppressptexinfo=-1\pdfinfoomitdate=1\pdftrailerid{}"


@dataclass
class LogReport:
    errors: list[str] = field(default_factory=list)
    undefined_refs: list[str] = field(default_factory=list)
    undefined_cites: list[str] = field(default_factory=list)
    multiply_defined: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    bad_dests: list[str] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)
    pages: int = 0

    def fatal(self) -> list[str]:
        out = []
        for name in ("errors", "undefined_refs", "undefined_cites", "multiply_defined", "missing", "bad_dests"):
            for item in getattr(self, name):
                out.append(f"{name}: {item}")
        return out


@dataclass
class CompileResult:
    app_dir: Path
    paper_dir: Path
    app_job: str
    paper_job: str
    pdf: Path
    app_aux: Path
    paper_aux: Path
    report: LogReport
    passes: int


def run_tex(cfg: Config, job: str, cwd: Path, *, draft: bool = False) -> None:
    cmd = [cfg.tool("pdflatex"), "-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
           "-no-shell-escape", "-recorder"]
    if draft:
        cmd.append("-draftmode")
    cmd += [f"-jobname={job}", PRELUDE + r"\input{" + job + ".tex}"]
    res = run(cmd, cwd=cwd, env=TEX_ENV, timeout=300, check=False)
    if res.returncode != 0:
        log = cwd / f"{job}.log"
        tail = "\n".join(read_text(log).splitlines()[-40:]) if log.exists() else res.stdout[-3000:]
        raise BuildError(f"pdflatex failed on {job} (exit {res.returncode}):\n{tail}")


def run_bibtex(cfg: Config, job: str, cwd: Path) -> None:
    res = run([cfg.tool("bibtex"), job], cwd=cwd, env=TEX_ENV, timeout=120, check=False)
    blg = cwd / f"{job}.blg"
    text = read_text(blg) if blg.exists() else ""
    if res.returncode >= 2 or "I didn't find a database entry" in text:
        raise BuildError(f"bibtex failed on {job} (exit {res.returncode}):\n{text[-2000:]}")


_ALLOWED = [
    re.compile(r"^(Overfull|Underfull) \\[hv]box"),
    re.compile(r"Float too large for page"),
    re.compile(r"Column C is already defined"),
]


def parse_log(path: Path) -> LogReport:
    rep = LogReport()
    text = read_text(path)
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("! ") or re.match(r"^[^:\s]+\.(tex|sty|cls|cfg|def):\d+: ", s):
            rep.errors.append(s)
        elif m := re.search(r"Reference `([^']+)' on page \S+ undefined", s):
            rep.undefined_refs.append(m.group(1))
        elif m := re.search(r"Citation `([^']+)' on page \S+ undefined", s):
            rep.undefined_cites.append(m.group(1))
        elif "There were undefined references" in s or "There were undefined citations" in s:
            rep.undefined_refs.append(s)
        elif m := re.search(r"Label `([^']+)' multiply defined", s):
            rep.multiply_defined.append(m.group(1))
        elif m := re.search(r"Citation `([^']+)' multiply defined", s):
            rep.multiply_defined.append("citation " + m.group(1))
        elif re.search(r"File `[^']+' not found|No file .*\.aux|Missing character", s):
            rep.missing.append(s)
        elif m := re.search(r"name\{([^}]+)\} has been referenced but does not exist", s):
            rep.bad_dests.append(m.group(1))
        elif m := re.search(r"destination with the same identifier \(name\{([^}]+)\}\)", s):
            if not re.fullmatch(r"page\.\d+", m.group(1)):
                rep.bad_dests.append("duplicate " + m.group(1))
        elif any(p.search(s) for p in _ALLOWED):
            rep.allowed.append(s[:160])
    if m := re.search(r"Output written on .*?\((\d+) pages?", text):
        rep.pages = int(m.group(1))
    rep.undefined_refs = list(dict.fromkeys(rep.undefined_refs))
    rep.undefined_cites = list(dict.fromkeys(rep.undefined_cites))
    return rep


def check_recorder(fls: Path, build_dir: Path, tex_root: Path) -> None:
    """Every file pdflatex read must be in the build tree or the TeX installation (or its user caches)."""
    bad = []
    for line in read_text(fls).splitlines():
        if not line.startswith("INPUT "):
            continue
        p = line[6:].strip().replace("\\", "/")
        low = p.lower()
        if not re.match(r"^[a-z]:/|^/", low):  # relative = inside the build dir
            continue
        if low.startswith(str(build_dir).replace("\\", "/").lower()):
            continue
        if low.startswith(str(tex_root).replace("\\", "/").lower()) or "/.texlive" in low or "texmf" in low:
            continue
        bad.append(p)
    if bad:
        raise BuildError("pdflatex read files outside the build tree:\n  " + "\n  ".join(sorted(set(bad))[:20]))


def compile_all(cfg: Config, app_dir: Path, paper_dir: Path, app_job: str, paper_job: str,
                *, force: bool = False) -> CompileResult:
    marker = app_dir / "compile.ok.json"
    pdf = app_dir / f"{app_job}.pdf"
    if marker.exists() and pdf.exists() and not force:
        info("reusing the previous compile of this snapshot")
        rep = parse_log(app_dir / f"{app_job}.log")
        return CompileResult(app_dir, paper_dir, app_job, paper_job, pdf, app_dir / f"{app_job}.aux",
                             paper_dir / f"{paper_job}.aux", rep, 0)
    tex_root = cfg.texbin.parents[1]
    info("pdflatex: appendix pass 1")
    run_tex(cfg, app_job, app_dir)
    shutil.copy2(app_dir / f"{app_job}.aux", paper_dir / f"{app_job}.aux")
    info("pdflatex: paper (labels only, draft mode)")
    run_tex(cfg, paper_job, paper_dir, draft=True)
    shutil.copy2(paper_dir / f"{paper_job}.aux", app_dir / f"{paper_job}.aux")
    info("bibtex: appendix")
    run_bibtex(cfg, app_job, app_dir)
    aux = app_dir / f"{app_job}.aux"
    prev = sha256_file(aux)
    passes = 1
    for _ in range(5):
        passes += 1
        info(f"pdflatex: appendix pass {passes}")
        run_tex(cfg, app_job, app_dir)
        cur = sha256_file(aux)
        if cur == prev and passes >= 3:
            break
        prev = cur
    else:
        raise BuildError("the appendix .aux did not reach a fixed point after 6 passes")
    rep = parse_log(app_dir / f"{app_job}.log")
    if rep.fatal():
        raise BuildError("the appendix compile log has problems:\n  " + "\n  ".join(rep.fatal()[:30]))
    check_recorder(app_dir / f"{app_job}.fls", app_dir, tex_root)
    paper_rep = parse_log(paper_dir / f"{paper_job}.log")
    if paper_rep.errors:
        raise BuildError("the paper compile log has errors:\n  " + "\n  ".join(paper_rep.errors[:10]))
    if rep.allowed:
        info(f"compile: {len(rep.allowed)} known-harmless warnings (boxes, float size, column type)")
    write_json(marker, {"passes": passes, "pages": rep.pages})
    return CompileResult(app_dir, paper_dir, app_job, paper_job, pdf, aux, paper_dir / f"{paper_job}.aux",
                         rep, passes)


def paper_undefined_refs(paper_dir: Path, paper_job: str) -> list[str]:
    rep = parse_log(paper_dir / f"{paper_job}.log")
    return rep.undefined_refs


def warn_paper(paper_dir: Path, paper_job: str, oa_labels: set[str]) -> None:
    missing = [r for r in paper_undefined_refs(paper_dir, paper_job)
               if r.startswith(("app:", "q:")) or r in oa_labels]
    if missing:
        warn("the main paper references OA labels that do not exist: " + ", ".join(missing))
