"""Fail-closed checks: anonymity (names, metadata, identity, external requests) and integrity."""
from __future__ import annotations

import difflib
import html as _html
import json
import re
import unicodedata
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from .config import Config
from .util import run, sha256_file

TEXT_EXT = {".html", ".json", ".css", ".js", ".txt", ".svg", ".csv", ".md", ".toml", ".py", ".cjs", ".mjs", ""}
DOCS_EXT = {".html", ".json", ".css", ".js", ".mjs", ".txt", ".svg", ".csv", ".pdf", ".png", ".webp", ".gif",
            ".woff2", ""}
REPO_ALLOW = re.compile(r"^(docs/.*|site/.*|tools/.*|README\.md|\.gitignore|oa\.toml|oa\.local\.example\.toml)$")


VENDOR_DIRS = {"assets/pdfjs/": "site/static/pdfjs/"}   # docs-relative prefix -> tracked source folder


def _vendor_pins(cfg: Config) -> dict[str, dict[str, str]]:
    pins = {}
    for prefix, src in VENDOR_DIRS.items():
        f = cfg.root / src / "SHA256SUMS"
        table = {}
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    h, _, rel = line.partition("  ")
                    table[rel.strip()] = h.strip()
        pins[prefix] = table
    return pins


def _vendored(cfg: Config, rel: str, path: Path, rep: Report, pins: dict) -> bool:
    """True if `rel` is a pinned third-party file whose bytes match (then it is not scanned)."""
    for prefix, src in VENDOR_DIRS.items():
        for base in (prefix, src):
            if rel.startswith(base):
                sub = rel[len(base):]
                if sub in ("SHA256SUMS", "VERSION.txt"):
                    return False
                want = pins.get(prefix, {}).get(sub)
                if want is None:
                    rep.err(f"{rel}: not listed in the pinned third-party files")
                    return True
                if sha256_file(path) != want:
                    rep.err(f"{rel}: differs from the pinned upstream file")
                return True
    return False


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def print(self) -> None:
        for p in self.passed:
            print(f"  ok    {p}")
        for w in self.warnings:
            print(f"  warn  {w}")
        for e in self.errors[:60]:
            print(f"  FAIL  {e}")
        if len(self.errors) > 60:
            print(f"  ... and {len(self.errors) - 60} more failures")
        print(f"checks: {len(self.passed)} passed, {len(self.warnings)} warnings, {len(self.errors)} failures")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).casefold()


class Banned:
    def __init__(self, terms: list[str]):
        self.pats = []
        for t in terms:
            n = _norm(t)
            if len(n) <= 4 and re.fullmatch(r"[\w.]+", n):
                self.pats.append((t, re.compile(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])")))
            else:
                self.pats.append((t, re.compile(re.escape(n))))

    def find(self, text: str) -> list[str]:
        n = _norm(text)
        return [t for t, p in self.pats if p.search(n)]


def _pdf_texts(path: Path) -> list[tuple[str, str]]:
    """Every text-bearing part of a PDF: page text, metadata, XMP, outline, link targets, raw streams."""
    out = []
    doc = fitz.open(path)
    for i, page in enumerate(doc):
        out.append((f"page {i + 1}", page.get_text()))
        for ln in page.get_links():
            out.append((f"link p{i + 1}", json.dumps({k: str(v) for k, v in ln.items()})))
    out.append(("metadata", json.dumps(doc.metadata)))
    out.append(("xmp", doc.get_xml_metadata() or ""))
    out.append(("outline", json.dumps(doc.get_toc())))
    for x in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(x, compressed=True)
            out.append((f"obj {x}", obj))
            if doc.xref_is_stream(x) and not doc.xref_is_image(x):
                raw = doc.xref_stream(x) or b""
                if len(raw) < 4_000_000:
                    out.append((f"stream {x}", raw.decode("latin-1")))
                    if b"\x00" in raw[:200]:
                        try:
                            out.append((f"stream16 {x}", raw.decode("utf-16-be", "ignore")))
                        except Exception:
                            pass
        except Exception:
            continue
    doc.close()
    return out


def _check_pdf_meta(path: Path, rel: str, rep: Report, title_ok: bool) -> None:
    doc = fitz.open(path)
    info = doc.xref_get_key(-1, "Info")
    keys = []
    if info[0] == "xref":
        ix = int(info[1].split()[0])
        keys = [k for k in doc.xref_get_keys(ix) if doc.xref_get_key(ix, k)[0] != "null"]
    allowed = {"Title"} if title_ok else set()
    extra = [k for k in keys if k not in allowed and k != "Title"]
    if extra:
        rep.err(f"{rel}: PDF Info has {extra}")
    if doc.get_xml_metadata():
        rep.err(f"{rel}: PDF has XMP metadata")
    for x in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(x, compressed=True)
        except Exception:
            continue
        if "PTEX" in obj:
            rep.err(f"{rel}: PDF object {x} carries /PTEX metadata")
            break
        if re.search(r"/S\s*/(GoToR|Launch|JavaScript|SubmitForm|ImportData|GoToE)\b", obj):
            rep.err(f"{rel}: PDF object {x} has an external or script action")
            break
        if re.search(r"D:\d{8,14}[+\-Z]", obj):
            rep.err(f"{rel}: PDF object {x} carries a date")
            break
    tid = doc.xref_get_key(-1, "ID")
    if tid[0] != "null":
        rep.warnings.append(f"{rel}: PDF trailer has an /ID (random, harmless)")
    doc.close()


def _visible_text(html: str) -> str:
    html = re.sub(r"<(script|style|math)\b.*?</\1>", " ", html, flags=re.S)
    html = re.sub(r"<[^>]+>", " ", html)
    return _html.unescape(html)


def check_anonymity(cfg: Config, root: Path, rep: Report, banned: Banned) -> None:
    hits = 0
    pins = _vendor_pins(cfg)
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = f.relative_to(root).as_posix()
        if _vendored(cfg, rel, f, rep, pins):
            continue
        for t in banned.find(rel):
            rep.err(f"file name contains banned term {t!r}: {rel}")
            hits += 1
        ext = f.suffix.lower()
        if ext not in DOCS_EXT:
            rep.err(f"unexpected file type in docs/: {rel}")
        if ext in TEXT_EXT:
            text = f.read_text(encoding="utf-8", errors="replace")
            for t in banned.find(text) + banned.find(_html.unescape(text)):
                rep.err(f"{rel}: contains banned term {t!r}")
                hits += 1
        elif ext == ".pdf":
            for where, txt in _pdf_texts(f):
                for t in banned.find(txt):
                    rep.err(f"{rel} ({where}): contains banned term {t!r}")
                    hits += 1
            _check_pdf_meta(f, rel, rep, title_ok=True)
        elif ext in (".png", ".webp"):
            with Image.open(f) as im:
                meta = {k: v for k, v in im.info.items() if k not in ("dpi", "gamma", "transparency", "loop",
                                                                       "background", "duration", "aspect")}
                if meta:
                    rep.err(f"{rel}: image carries metadata {sorted(meta)}")
        elif ext == ".woff2":
            pass
    if not hits:
        rep.passed.append("no banned term in any file, file name or PDF part under docs/")

    # web hygiene
    bad = []
    for f in root.rglob("*.html"):
        rel = f.relative_to(root).as_posix()
        h = f.read_text(encoding="utf-8")
        for m in re.finditer(r"<(img|script|link|iframe|source|video|audio|embed|object)\b[^>]*>", h):
            tag = m.group(0)
            for am in re.finditer(r"\b(src|href|srcset|data|poster)\s*=\s*\"([^\"]*)\"", tag):
                if re.match(r"^(https?:)?//", am.group(2)):
                    bad.append(f"{rel}: external resource {am.group(2)}")
        if cfg.noindex and 'name="robots" content="noindex' not in h:
            bad.append(f"{rel}: missing noindex")
        if 'name="referrer" content="no-referrer"' not in h:
            bad.append(f"{rel}: missing referrer policy")
        if "Content-Security-Policy" not in h:
            bad.append(f"{rel}: missing CSP")
    for f in list(root.rglob("*.css")) + list(root.rglob("*.js")) + list(root.rglob("*.mjs")):
        t = f.read_text(encoding="utf-8")
        for m in re.finditer(r"url\(\s*['\"]?(https?:)?//|@import|fetch\(\s*['\"]https?:", t):
            bad.append(f"{f.relative_to(root).as_posix()}: external reference")
    if (root / "robots.txt").read_text(encoding="utf-8") != "User-agent: *\nDisallow: /\n":
        bad.append("robots.txt does not disallow crawling")
    if not (root / ".nojekyll").exists():
        bad.append(".nojekyll is missing")
    if bad:
        for b in bad:
            rep.err(b)
    else:
        rep.passed.append("no external resources; noindex, no-referrer and CSP on every page; robots.txt blocks crawlers")


def check_repo(cfg: Config, rep: Report, banned: Banned) -> None:
    def git(*a):
        return run(["git", "-C", str(cfg.root), *a], check=False).stdout
    name = git("config", "--local", "user.name").strip()
    email = git("config", "--local", "user.email").strip()
    if name != cfg.identity_name or email != cfg.identity_email:
        rep.err(f"repo git identity is {name!r} <{email}>, expected {cfg.identity_name!r} <{cfg.identity_email}>")
    else:
        rep.passed.append("repo-local git identity is the anonymous one")
    if git("config", "--get", "commit.gpgsign").strip() not in ("", "false"):
        rep.err("commit signing is enabled for this repo")
    log = git("log", "--all", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%ad%x1f%cd%x1f%B%x1e", "--date=raw")
    bad_commits = 0
    for rec in filter(None, (r.strip() for r in log.split("\x1e"))):
        h, an, ae, cn, ce, ad, cd, msg = (rec.split("\x1f") + [""] * 8)[:8]
        problems = []
        if (an, ae, cn, ce) != (cfg.identity_name, cfg.identity_email, cfg.identity_name, cfg.identity_email):
            problems.append(f"identity {an} <{ae}> / {cn} <{ce}>")
        if not ad.endswith("+0000") or not cd.endswith("+0000"):
            problems.append(f"time zone {ad.split()[-1] if ad else '?'}")
        for t in banned.find(msg):
            problems.append(f"message contains {t!r}")
        if problems:
            bad_commits += 1
            rep.err(f"commit {h[:10]}: " + "; ".join(problems))
    if not bad_commits:
        rep.passed.append("every commit is by the anonymous identity, in UTC, with a clean message")
    tracked = [p for p in git("ls-files").splitlines() if p]
    outside = [p for p in tracked if not REPO_ALLOW.match(p)]
    if outside:
        rep.err("tracked files outside the allowlist: " + ", ".join(outside[:10]))
    if any(p.startswith(".cache/") or p == "oa.local.toml" for p in tracked):
        rep.err("oa.local.toml or .cache/ is tracked")
    hits = 0
    pins = _vendor_pins(cfg)
    for p in tracked:
        if p.startswith("docs/"):
            continue
        f = cfg.root / p
        if f.exists() and _vendored(cfg, p, f, rep, pins):
            continue
        if f.suffix.lower() in TEXT_EXT and f.exists():
            for t in banned.find(f.read_text(encoding="utf-8", errors="replace")) + banned.find(p):
                rep.err(f"tracked file {p} contains banned term {t!r}")
                hits += 1
    ign = git("check-ignore", "oa.local.toml", ".cache/x").split()
    if "oa.local.toml" not in ign:
        rep.err("oa.local.toml is not ignored by git")
    if not outside and not hits:
        rep.passed.append(f"{len(tracked)} tracked files: inside the allowlist and free of banned terms")


def _words(s: str) -> list[str]:
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"-\s*\n\s*", "", s)
    return [w.lower() for w in re.findall(r"[A-Za-z]{3,}", s)]


def _nums(s: str) -> Counter:
    s = s.replace("\u2212", "-").replace(",", "")
    return Counter(m.lstrip("+-") for m in re.findall(r"[+\-]?\d+(?:\.\d+)?", s))


def check_integrity(cfg: Config, root: Path, model: dict, rep: Report) -> None:
    ex = model["exhibits"]
    # numbering
    seen = {}
    for e in ex:
        key = (e["kind"], re.match(r"[A-Z]+", e["number"]).group(0))
        seen.setdefault(key, []).append(int(re.search(r"\d+", e["number"]).group(0)))
    gaps = [f"{k[0]} {k[1]}: {v}" for k, v in seen.items() if v != list(range(1, len(v) + 1))]
    if gaps:
        rep.err("exhibit numbers are not contiguous: " + "; ".join(gaps))
    else:
        rep.passed.append(f"{len(ex)} exhibits, numbered contiguously within each section")
    # pages exist
    missing = []
    for e in ex:
        if not (root / e["url"].strip("/") / "index.html").exists():
            missing.append(e["url"])
    if missing:
        rep.err("missing exhibit pages: " + ", ".join(missing))
    # internal links + stray LaTeX
    pdf_pages = model["pdf"]["pages"]
    bad_links, stray = [], []
    for f in root.rglob("*.html"):
        rel = f.relative_to(root).as_posix()
        h = f.read_text(encoding="utf-8")
        for m in re.finditer(r"\b(?:href|src|data-zoom)=\"(/[^\"]*)\"", h):
            url = _html.unescape(m.group(1))
            path, _, frag = url.partition("#")
            path = path.split("?")[0]
            target = root / path.lstrip("/")
            if path.endswith("/"):
                target = target / "index.html"
            if not target.exists():
                bad_links.append(f"{rel} -> {url}")
                continue
            if frag.startswith("page="):
                n = int(frag[5:])
                if not 1 <= n <= pdf_pages:
                    bad_links.append(f"{rel} -> {url} (page out of range)")
            elif frag and target.suffix == ".html":
                if f'id="{frag}"' not in target.read_text(encoding="utf-8"):
                    bad_links.append(f"{rel} -> {url} (no #{frag})")
        vis = _visible_text(h)
        # real TeX leftovers only: control words, $\..., $x_ / $x^, _{ / ^{ (literal "$1,000" is fine)
        tex_pat = r"\\[A-Za-z]{2,}|\$\\|\$[A-Za-z][_^]|[_^]\{"
        if re.search(tex_pat, vis):
            mm = re.search(r".{0,30}(" + tex_pat + r").{0,30}", vis)
            stray.append(f"{rel}: {mm.group(0).strip() if mm else '?'}")
    if bad_links:
        for b in bad_links[:30]:
            rep.err(f"broken internal link {b}")
    else:
        rep.passed.append("every internal link, anchor and PDF page reference resolves")
    if stray:
        for s in stray[:20]:
            rep.err(f"LaTeX left in the rendered text: {s}")
    else:
        rep.passed.append("no LaTeX commands or dollar math left in the rendered text")
    # assets decode with the expected aspect ratio
    bad_assets = []
    for e in ex:
        for p in e["parts"]:
            gs = list(p["graphics"]) + [g for pn in p["panels"] for g in pn["graphics"]]
            for g in gs:
                for key in ("src", "png"):
                    f = root / g[key].lstrip("/")
                    if not f.exists() or f.stat().st_size == 0:
                        bad_assets.append(f"{e['id']}: {g[key]} missing")
                f = root / g["png"].lstrip("/")
                if f.exists():
                    with Image.open(f) as im:
                        ar = im.width / im.height
                    exp = g["w"] / g["h"]
                    if abs(ar - exp) / exp > 0.01:
                        bad_assets.append(f"{e['id']}: {g['png']} aspect {ar:.3f} vs {exp:.3f}")
    if bad_assets:
        for b in bad_assets:
            rep.err(b)
    else:
        rep.passed.append("every figure asset exists and matches its source's aspect ratio")
    # tables and notes against the typeset PDF
    pdf_path = root / model["pdf"]["url"].lstrip("/")
    doc = fitz.open(pdf_path)
    num_bad, note_low = [], []
    thr = float(cfg.checks.get("note_similarity_min", 0.97))
    for e in ex:
        page_text = "\n".join(doc[pg].get_text() for pg in e["pages"])
        pnums = _nums(page_text)
        for p in e["parts"]:
            for t in p["tables"]:
                tn = _nums(" ".join(t["numbers"]))
                missing_n = tn - pnums
                if missing_n:
                    num_bad.append(f"{e['kind_word']} {e['number']}{' ' + t['letter'] if t['letter'] else ''}: "
                                   f"{sum(missing_n.values())} numbers not on the PDF page, e.g. "
                                   f"{list(missing_n.elements())[:6]}")
            if p["note_text"]:
                pw = doc[p["pdf_page"]].get_text() if p["pdf_page"] is not None else page_text
                a, b = _words(p["note_text"]), _words(page_text if len(e["pages"]) == 1 else pw + page_text)
                sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
                matched = sum(bl.size for bl in sm.get_matching_blocks())
                ratio = matched / max(1, len(a))
                if ratio < thr:
                    note_low.append(f"{e['kind_word']} {e['number']} part {p['n']}: note matches the PDF at {ratio:.3f}")
    doc.close()
    if num_bad:
        for b in num_bad:
            rep.err(b)
    else:
        rep.passed.append("every number in every HTML table also appears on its PDF page")
    if note_low:
        for b in note_low:
            rep.err(b)
    else:
        rep.passed.append(f"every note's wording matches its PDF page (>= {thr:.2f})")
    nxt = [e["next"] for e in ex if e["next"]]
    if len(nxt) != len(ex) - 1:
        rep.err("prev/next chain does not cover every exhibit")


def run_checks(cfg: Config, root: Path, model: dict, *, scope: str) -> Report:
    rep = Report()
    banned = Banned(cfg.banned_terms)
    check_anonymity(cfg, root, rep, banned)
    check_integrity(cfg, root, model, rep)
    if scope == "repo":
        check_repo(cfg, rep, banned)
    return rep
