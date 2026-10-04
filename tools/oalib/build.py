"""Orchestrate a full build into .cache/stage/docs (never touches docs/ directly)."""
from __future__ import annotations

import html as _html
import re
import shutil
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import latex, snapshot
from .latexaux import parse_aux
from .config import Config
from .bib import link_urls, parse_bbl
from .crossrefs import FALLBACK, KIND_WORD, Resolver, assign_groups, exhibit_url, scan_paper
from .figures import convert as convert_figure
from .inline import Inline
from .mathml import MathRegistry
from .pdfmap import PdfIndex, postprocess
from .questions import parse_questions, render as render_question
from .table2html import parse_table, to_html
from .texdoc import Doc, Exhibit, parse_appendix
from .textsec import Page, find_units, finish, footnote_numbers, plain, site_targets
from .util import BuildError, info, read_text, sha256_file, sha256_text, warn, write_json, write_text

TOOLS = Path(__file__).resolve().parents[1]


@dataclass
class BuildResult:
    stage: Path
    model: dict
    snap: snapshot.Snapshot
    doc: Doc
    pdf: PdfIndex
    compile: latex.CompileResult
    paper_aux_labels: set


def _fmt_size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{n // 1024} KB"


def _fmt_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _strip_period(s: str) -> str:
    s = s.rstrip()
    return s[:-1] if s.endswith(".") else s


def _paper_title(cfg: Config, doc: Doc, mk) -> str:
    if cfg.paper_title:
        return cfg.paper_title
    t = mk("appendix title").inline(doc.title).text
    m = re.search(r"[“\"](.+?)[”\"]", t)
    return m.group(1).strip() if m else t


def _page_range(pdf: PdfIndex, anchor: str, next_anchor: str | None) -> tuple[int, int]:
    start = pdf.page_of(anchor)
    if not next_anchor:
        return start, pdf.page_count - 1
    nxt = pdf.page_of(next_anchor)
    y = pdf.y_of(next_anchor)
    h = pdf.doc[nxt].rect.height
    end = nxt - 1 if (y is not None and y >= h - 72 - 60) else nxt
    return start, max(start, end)


def run_build(cfg: Config, *, pull: bool = False, force_compile: bool = False) -> BuildResult:
    snap = snapshot.take(cfg, pull=pull)
    app_dir, paper_dir, key = snapshot.build_trees(cfg, snap)
    app_job, paper_job = snap.appendix[:-4], snap.paper[:-4]
    comp = latex.compile_all(cfg, app_dir, paper_dir, app_job, paper_job, force=force_compile)
    app_aux, paper_aux = parse_aux(comp.app_aux), parse_aux(comp.paper_aux)
    latex.warn_paper(paper_dir, paper_job, set(app_aux.labels))

    stage = cfg.cache / "stage" / "docs"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    doc = parse_appendix(app_dir / snap.appendix, app_aux)
    math = MathRegistry(cfg.node, TOOLS / "js" / "tex2mml.cjs", cfg.cache / "mathml-cache.json")

    # PDF first (the resolver links into it)
    pdf_url = "/" + cfg.pdf_name
    paper_title = _paper_title(cfg, doc, lambda w: Inline(None, math, w))
    pdf_title = f"Online Appendix to “{paper_title}”"
    pp = postprocess(comp.pdf, stage / cfg.pdf_name, pdf_title)
    info(f"PDF: {comp.report.pages} pages, {pp['links_removed']} links to other files removed")
    pdf = PdfIndex(stage / cfg.pdf_name)
    viewer_url = "/pdf/"  # in-site PDF viewer: opens the page in any browser instead of downloading the file
    resolver = Resolver(doc, app_aux, paper_aux, pdf, viewer_url)
    # the appendix's running text gets pages of its own; references to it (and to its questions) link there
    units = find_units(doc)
    resolver.site = site_targets(doc, units)
    # the reference list gets a page too; citations link to their entries there
    bib = parse_bbl(comp.app_aux.with_suffix(".bbl"))
    refs_url = "/references/"
    resolver.bib = {b.key: f"{refs_url}#ref-{b.key}" for b in bib}

    def mk(where: str, link: bool = True) -> Inline:
        return Inline(resolver, math, where, link_refs=link)

    pmap = scan_paper(read_text(paper_dir / snap.paper), set(app_aux.labels))
    overrides = {}
    gfile = cfg.site / "groups.toml"
    if gfile.exists():
        with open(gfile, "rb") as fh:
            overrides = {k: str(v) for k, v in tomllib.load(fh).get("override", {}).items()}

    # ---- sections ----
    heads = doc.sections
    sections_out = []
    for i, s in enumerate(heads):
        nxt = next((h.anchor for h in heads[i + 1:] if h.level <= s.level), None)
        if nxt is None and s.level >= 1:
            refs_toc = [t for t in app_aux.toc if t.kind == "section" and not t.number]
            nxt = refs_toc[0].anchor if refs_toc else None
        p0, p1 = _page_range(pdf, s.anchor, nxt)
        r = mk(f"heading {s.id}").inline(s.title)
        sections_out.append({
            "id": s.id, "level": s.level, "label": s.label, "title_html": r.html, "title": r.text,
            "has_exhibits": s.has_exhibits, "pages": [p0, p1],
            "page_labels": [pdf.labels[p0], pdf.labels[p1]],
            "href": f"{viewer_url}#page={p0 + 1}",
        })

    # ---- exhibits ----
    asset_root = stage / "assets" / "x"
    figcfg = cfg.figures
    exhibits_out = []
    forward: dict[str, list[str]] = {}
    oa_refs: dict[str, list[str]] = {}
    for ex in doc.exhibits:
        kw = "Figure" if ex.kind == "figure" else "Table"
        tag = f"{kw} {ex.number}"
        short = "fig" if ex.kind == "figure" else "tab"
        adir = asset_root / short / ex.number
        arel = f"/assets/x/{short}/{ex.number}/"
        refs_all: list[str] = []

        def take(r):
            refs_all.extend(r.refs)
            return r

        title = take(mk(f"{tag} caption").inline(ex.title))
        parts_out = []
        n_parts = len(ex.parts)
        searchable = []
        for k, part in enumerate(ex.parts):
            ppage = pdf.page_of(part.anchor)
            note = take(mk(f"{tag} note" + (f" (part {k + 1})" if n_parts > 1 else "")).block(part.note)) \
                if part.note and part.note.strip() else None
            if note:
                searchable.append(note.text)
            graphics_out = []
            panel_count = sum(len(p.panels) for p in ex.parts)
            for g_i, g in enumerate(part.graphics):
                if panel_count and part.panels:
                    stem = f"{ex.number}-legend" if len(part.graphics) == 1 else f"{ex.number}-legend{g_i + 1}"
                elif n_parts > 1:
                    stem = f"{ex.number}-{k + 1}"
                elif len(part.graphics) > 1:
                    stem = f"{ex.number}-{g_i + 1}"
                else:
                    stem = ex.number
                fa = convert_figure(app_dir / g.path, adir, stem, svg_max_gzip_kb=figcfg.get("svg_max_gzip_kb", 150),
                                    svg_max_drawings=figcfg.get("svg_max_drawings", 5000),
                                    webp_quality=figcfg.get("webp_quality", 90), png_dpi=figcfg.get("png_dpi", 300))
                searchable.append(fa.text)
                graphics_out.append({
                    "src": arel + fa.display, "kind": fa.kind, "png": arel + fa.png,
                    "w": round(fa.w_pt, 1), "h": round(fa.h_pt, 1), "width": g.width,
                    "legend": bool(panel_count and part.panels), "src_sha": fa.src_sha, "text": fa.text,
                })
            panels_out = []
            for pn in part.panels:
                sub = take(mk(f"{tag} panel ({pn.letter})").inline(pn.subcaption)) if pn.subcaption else None
                pg = []
                for g_i, g in enumerate(pn.graphics):
                    stem = f"{ex.number}{pn.letter}" + (f"-{g_i + 1}" if len(pn.graphics) > 1 else "")
                    fa = convert_figure(app_dir / g.path, adir, stem,
                                        svg_max_gzip_kb=figcfg.get("svg_max_gzip_kb", 150),
                                        svg_max_drawings=figcfg.get("svg_max_drawings", 5000),
                                        webp_quality=figcfg.get("webp_quality", 90),
                                        png_dpi=figcfg.get("png_dpi", 300))
                    searchable.append(fa.text)
                    pg.append({"src": arel + fa.display, "kind": fa.kind, "png": arel + fa.png,
                               "w": round(fa.w_pt, 1), "h": round(fa.h_pt, 1), "width": g.width,
                               "src_sha": fa.src_sha, "text": fa.text})
                if sub:
                    searchable.append(sub.text)
                group_html = mk(f"{tag} panel group").inline(pn.group).html if pn.group else None
                panels_out.append({"letter": pn.letter, "label": pn.label, "sub_html": sub.html if sub else "",
                                   "sub_text": sub.text if sub else "", "graphics": pg, "width": pn.width,
                                   "group": group_html})
            tables_out = []
            for t_i, ti in enumerate(part.tables):
                src_text = read_text(app_dir / ti.path)
                tbl = parse_table(src_text, ti.path, ti.coltypes,
                                  lambda where, _tag=tag: mk(f"{_tag} {where}"))
                heading = mk(f"{tag} panel heading").inline(ti.heading) if ti.heading else None
                pm = re.match(r"Panel\s+([A-Z])\b", heading.text) if heading else None
                letter = pm.group(1) if pm else (chr(ord("A") + t_i) if len(part.tables) > 1 else "")
                tid = f"t-{ex.number}" + (f"-{letter}" if letter else "")
                html_table = to_html(tbl, table_id=tid)
                cell_text = " ".join(" ".join(c.text_lines) for r in tbl.rows for c in r.cells)
                searchable.append(cell_text)
                tables_out.append({
                    "letter": letter, "heading_html": heading.html if heading else "",
                    "heading": heading.text if heading else "", "html": html_table,
                    "dense": bool(ti.size) or tbl.ncols >= 8, "ncols": tbl.ncols, "numbers": tbl.numbers,
                    "src": ti.path, "src_sha": sha256_text(src_text), "cells": cell_text,
                })
            parts_out.append({
                "n": k + 1, "continued": part.continued, "anchor": part.anchor, "pdf_page": ppage,
                "page_label": pdf.labels[ppage], "note_html": note.html if note else "",
                "note_text": note.text if note else "", "graphics": graphics_out, "panels": panels_out,
                "tables": tables_out, "landscape": part.landscape, "caption_src": part.caption,
                "note_src_sha": sha256_text(part.note or ""),
            })
        pages = sorted({p["pdf_page"] for p in parts_out})
        main_refs, oa = [], []
        for lab in refs_all:
            if lab in app_aux.labels:
                oa.append(lab)
            elif lab in paper_aux.labels:
                main_refs.append(lab)
        forward[ex.id] = main_refs
        oa_refs[ex.id] = oa
        exhibits_out.append({
            "id": ex.id, "kind": ex.kind, "kind_word": kw, "number": ex.number, "label": ex.label,
            "labels": ex.labels, "url": exhibit_url(ex), "title_html": _strip_period(title.html),
            "title": _strip_period(title.text), "section": ex.section, "subsection": ex.subsection,
            "parts": parts_out, "pages": pages, "page_labels": [pdf.labels[p] for p in pages],
            "pdf_href": f"{viewer_url}#page={pages[0] + 1}", "questions": [],
            "n_panels": sum(len(p["panels"]) for p in parts_out) + sum(len(p["tables"]) for p in parts_out
                                                                       if len(p["tables"]) > 1),
            "wide": any(p["landscape"] for p in parts_out),
            "search": " ".join(searchable),
        })

    # ---- survey questions behind each response figure (from the appendix's \respbox pointers) ----
    by_label_ex = {lab: e for e in exhibits_out for lab in e["labels"]}
    for q in parse_questions(doc, app_aux):
        q_html, q_text = render_question(q.tex, lambda w, _k=q.key: mk(f"question {_k}: {w}"))
        for fig_label, panel in q.targets:
            e = by_label_ex.get(fig_label)
            if e is None:
                raise BuildError(f"question {q.key} points to {fig_label}, which is not an exhibit")
            have = next((x for x in e["questions"] if x["key"] == q.key), None)
            if have:
                if panel and panel not in have["panels"]:
                    have["panels"].append(panel)
                continue
            if q.label:
                q_href = resolver.site.get(q.label)
            else:
                u = next((u for u in units if u.covers(q.offset)), None)
                q_href = f"{u.url}#{q.key}" if u else None
            e["questions"].append({"key": q.key, "label": q.label, "source": q.source, "number": q.number,
                                   "panels": [panel] if panel else [], "html": q_html, "text": q_text,
                                   "href": q_href})
    ref_link = re.compile(r'<a class="ref" href="[^"]*" data-ref="(q:[^"]+)"(?: target="_blank" rel="noopener")?>')
    for e in exhibits_out:
        shown = {x["label"]: x["key"] for x in e["questions"] if x["label"]}
        if not shown:
            continue
        for p in e["parts"]:
            p["note_html"] = ref_link.sub(
                lambda m, s=shown: (f'<a class="ref" href="#q-{s[m.group(1)]}" data-ref="{m.group(1)}">'
                                    if m.group(1) in s else m.group(0)), p["note_html"])

    # ---- main-paper cross-links ----
    groups = assign_groups(doc, forward, oa_refs, pmap, paper_aux, overrides)
    def paper_ref_entry(lab: str) -> dict:
        L = paper_aux.labels[lab]
        prefix = lab.split(":")[0]
        word = KIND_WORD.get(prefix, "")
        num = f"({L.number})" if prefix == "eq" else L.number
        title = mk(f"main-paper title of {lab}", link=False).inline(L.title).text if prefix in ("tab", "fig") else ""
        if prefix in ("sec", "subsec", "subsubsec"):
            title = mk(f"main-paper title of {lab}", link=False).inline(L.title).text
        return {"label": lab, "text": f"{word} {num}".strip(), "title": _strip_period(title)}

    def heading_entry(num: str) -> dict:
        t = pmap.titles.get(num, "")
        tr = mk(f"main-paper heading {num}", link=False).inline(t) if t else None
        return {"number": num, "title": tr.text if tr else "", "title_html": tr.html if tr else ""}

    by_id = {e["id"]: e for e in exhibits_out}
    for ex in doc.exhibits:
        e = by_id[ex.id]
        seen, refs = set(), []
        for lab in forward[ex.id]:
            if lab not in seen:
                seen.add(lab)
                refs.append(paper_ref_entry(lab))
        e["main_refs"] = refs
        cites = []
        for c in pmap.citations:
            if c.label in ex.labels:
                ent = {"ctx": c.ctx, "panel": c.panel, "paragraph": "", "heading": None, "float": None}
                if c.heading:
                    ent["heading"] = heading_entry(c.heading)
                if c.paragraph:
                    ent["paragraph"] = mk("main-paper paragraph", link=False).inline(c.paragraph).text.rstrip(".")
                if c.ctx == "float" and c.float_label and c.float_label in paper_aux.labels:
                    ent["float"] = paper_ref_entry(c.float_label)
                cites.append(ent)
        e["cited_in"] = cites
        g, rule = groups[ex.id]
        e["group"] = {"key": g, "rule": rule}

    group_keys = sorted({g for g, _ in groups.values()},
                        key=lambda k: (k == FALLBACK, [int(x) for x in k.split(".")] if k != FALLBACK else []))
    groups_out = []
    for k in group_keys:
        members = [e["id"] for e in exhibits_out if e["group"]["key"] == k]
        if k == FALLBACK:
            groups_out.append({"key": k, "number": "", "title": "Additional evidence without a main-paper link",
                               "title_html": "Additional evidence without a main-paper link", "exhibits": members})
        else:
            h = heading_entry(k)
            groups_out.append({"key": k, "number": k, "title": h["title"], "title_html": h["title_html"],
                               "exhibits": members})

    # ---- text pages: the text-only subsections, and the running text around the exhibits ----
    fn_numbers = footnote_numbers(doc)
    sec_by_id = {s["id"]: s for s in sections_out}
    texts_out = []
    mcq_seen: dict[str, int] = {}
    for u in units:
        pg = Page(doc, u, lambda w, _id=u.section.id: mk(f"section {_id}: {w}"), fn_numbers,
                  mcq_seen.get(u.top.id, 0))
        head = None
        if u.intro:
            ht = mk(f"heading {u.top.id}").inline(u.top.title)
            pg.text.append(ht.text)
            head = {"number": u.top.id, "title": ht.text, "title_html": ht.html, "html": finish(pg.body(*u.intro))}
        tr = mk(f"heading {u.section.id}").inline(u.section.title)
        pg.text.append(tr.text)
        body = finish(pg.body(*u.span))
        notes = [{"n": f["n"], "html": finish(f["html"])} for f in pg.footnotes()]
        mcq_seen[u.top.id] = pg.mcq
        # a lead page lists the figures and tables of its (sub)section
        members = [e["id"] for e in exhibits_out
                   if (e["subsection"] if u.section.level == 2 else e["section"]) == u.section.id] \
            if u.kind == "lead" else []
        p0 = sec_by_id[u.top.id]["pages"][0] if u.intro else sec_by_id[u.section.id]["pages"][0]
        p1 = sec_by_id[u.section.id]["pages"][1]
        texts_out.append({
            "id": u.id, "kind": "section", "kind_word": "", "number": u.section.id, "url": u.url,
            "title": tr.text, "title_html": tr.html, "top": u.top.id, "head": head, "html": body,
            "footnotes": notes, "text": plain(" ".join(pg.text + pg.fn_text)), "body_text": plain(" ".join(pg.text)),
            "fn_text": [plain(x) for x in pg.fn_text], "pages": [p0, p1],
            "page_labels": [pdf.labels[p0], pdf.labels[p1]], "pdf_href": f"{viewer_url}#page={p0 + 1}",
            "lead": u.kind == "lead", "members": members,
        })

    # ---- references ----
    references = None
    if bib:
        refs_toc = [t for t in app_aux.toc if t.kind == "section" and not t.number]
        if not refs_toc:
            raise BuildError("the appendix has a bibliography but no contents line for it")
        p0, p1 = _page_range(pdf, refs_toc[0].anchor, None)
        rt = mk("references heading").inline(refs_toc[0].title)
        items = []
        for b in bib:
            r = mk(f"reference {b.key}").inline(b.tex)
            items.append({"key": b.key, "html": link_urls(r.html), "text": r.text})
        body_text = " ".join([rt.text] + [x["text"] for x in items])
        references = {
            "id": "references", "kind": "references", "kind_word": "", "number": "", "url": refs_url,
            "title": rt.text, "title_html": rt.html, "items": items, "text": body_text, "body_text": body_text,
            "fn_text": [], "pages": [p0, p1], "page_labels": [pdf.labels[p0], pdf.labels[p1]],
            "pdf_href": f"{viewer_url}#page={p0 + 1}",
        }

    # ---- math -> MathML, then fill every HTML string ----
    math.render()
    def fill(obj):
        if isinstance(obj, str):
            return math.fill(obj) if "\u0000M" in obj else obj
        if isinstance(obj, list):
            return [fill(x) for x in obj]
        if isinstance(obj, dict):
            return {k: fill(v) for k, v in obj.items()}
        return obj
    exhibits_out = fill(exhibits_out)
    sections_out = fill(sections_out)
    texts_out = fill(texts_out)
    references = fill(references)

    # prev/next in reading order: a (sub)section's text page, then its figures and tables
    seq = []
    for top in [s for s in doc.sections if s.level == 1]:
        subs = [s for s in doc.sections if s.level == 2 and s.id.startswith(top.id + ".")]
        sub_ids = {s.id for s in subs}
        seq += [t["id"] for t in texts_out if t["number"] == top.id]
        seq += [e["id"] for e in exhibits_out if e["section"] == top.id and e["subsection"] not in sub_ids]
        for s in subs:
            seq += [t["id"] for t in texts_out if t["number"] == s.id]
            seq += [e["id"] for e in exhibits_out if e["subsection"] == s.id]
    if references:
        seq.append(references["id"])
    nav = {x["id"]: x for x in exhibits_out + texts_out + ([references] if references else [])}
    if sorted(seq) != sorted(nav):
        raise BuildError("the reading order does not cover every page")
    for i, k in enumerate(seq):
        nav[k]["prev"] = seq[i - 1] if i > 0 else None
        nav[k]["next"] = seq[i + 1] if i + 1 < len(seq) else None

    pdf_size = (stage / cfg.pdf_name).stat().st_size
    model = {
        "schema": 1,
        "site": {"title": cfg.title, "paper_title": paper_title, "base_url": cfg.base_url,
                 "noindex": cfg.noindex, "note_position": cfg.note_position},
        "build": {"source_date": snap.date, "source_date_text": _fmt_date(snap.date)},
        "pdf": {"url": pdf_url, "pages": pdf.page_count, "size": _fmt_size(pdf_size),
                "sha256": sha256_file(stage / cfg.pdf_name)},
        "counts": {"exhibits": len(exhibits_out), "figures": sum(e["kind"] == "figure" for e in exhibits_out),
                   "tables": sum(e["kind"] == "table" for e in exhibits_out)},
        "sections": sections_out,
        "exhibits": exhibits_out,
        "texts": texts_out,
        "references": references,
        "groups": groups_out,
    }
    return BuildResult(stage, model, snap, doc, pdf, comp, set(paper_aux.labels))
