"""Render the site model into static pages with Jinja2."""
from __future__ import annotations

import difflib
import re
import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from .config import Config
from .util import sha256_file, write_json, write_text

def _suggest(exhibits: list[dict]) -> dict[str, list[str]]:
    """Likely comparison partners: similar titles (robustness variants share long title stems), same group."""
    titles = {e["id"]: re.sub(r"\s+", " ", e["title"].lower()) for e in exhibits}
    out = {}
    for e in exhibits:
        scored = []
        for o in exhibits:
            if o["id"] == e["id"]:
                continue
            sim = difflib.SequenceMatcher(None, titles[e["id"]], titles[o["id"]]).ratio()
            score = sim + (0.15 if o["group"]["key"] == e["group"]["key"] else 0) +                 (0.1 if o["kind"] == e["kind"] else 0)
            scored.append((score, o["id"]))
        scored.sort(key=lambda x: -x[0])
        out[e["id"]] = [i for s, i in scored[:4] if s >= 0.6]
    return out


def _panel_groups(panels: list[dict]) -> list[dict]:
    groups: list[dict] = []
    for pn in panels:
        if not groups or groups[-1]["name"] != pn["group"]:
            groups.append({"name": pn["group"], "panels": []})
        groups[-1]["panels"].append(pn)
    for g in groups:
        w = g["panels"][0]["width"] or 1.0
        g["cols"] = max(1, min(3, round(1 / w))) if w < 0.5 else 1
    return groups


def _short(title: str, n: int = 46) -> str:
    return title if len(title) <= n else title[: n - 1].rsplit(" ", 1)[0] + "…"


def render(cfg: Config, model: dict, stage: Path) -> None:
    site_dir = cfg.site
    env = Environment(loader=FileSystemLoader(str(site_dir / "templates")),
                      autoescape=select_autoescape(["html"]), undefined=StrictUndefined,
                      trim_blocks=True, lstrip_blocks=True)
    env.filters["safe_html"] = lambda s: Markup(s or "")
    env.filters["short"] = _short
    env.filters["pct"] = lambda w: f"{w * 100:.0f}%" if w else "100%"

    # static assets
    assets = stage / "assets"
    (assets / "fonts").mkdir(parents=True, exist_ok=True)
    for f in (site_dir / "fonts").iterdir():
        if f.suffix in (".woff2", ".txt"):
            shutil.copy2(f, assets / "fonts" / f.name)
    for name in ("site.css", "site.js", "pdfview.js", "favicon.svg"):
        shutil.copy2(site_dir / "static" / name, assets / name)
    shutil.copytree(site_dir / "static" / "pdfjs", assets / "pdfjs", dirs_exist_ok=True)
    ver = {"css": sha256_file(assets / "site.css")[:10], "js": sha256_file(assets / "site.js")[:10],
           "pdf": sha256_file(assets / "pdfview.js")[:10]}

    exhibits = model["exhibits"]
    by_id = {e["id"]: e for e in exhibits}
    sug = _suggest(exhibits)
    for e in exhibits:
        e["suggest"] = sug[e["id"]]
        for p in e["parts"]:
            p["panel_groups"] = _panel_groups(p["panels"])
    sections = model["sections"]
    tops = [dict(s) for s in sections if s["level"] == 1]
    for s in tops:
        s["exhibits"] = [e for e in exhibits if e["section"] == s["id"]]
        s["n_exhibits"] = len(s["exhibits"])
    groups = [dict(g) for g in model["groups"]]
    for g in groups:
        g["items"] = [by_id[i] for i in g["exhibits"]]

    ctx = {"site": model["site"], "pdf": model["pdf"], "build": model["build"], "counts": model["counts"],
           "tops": tops, "groups": groups, "exhibits": exhibits, "by_id": by_id, "v": ver}

    write_text(stage / "index.html", env.get_template("index.html").render(**ctx, page="home"))
    for e in exhibits:
        top = next(s for s in tops if s["id"] == e["section"])
        html = env.get_template("exhibit.html").render(**ctx, page="exhibit", e=e, top=top,
                                                        prev=by_id.get(e["prev"]), next=by_id.get(e["next"]))
        write_text(stage / e["url"].strip("/") / "index.html", html)
    write_text(stage / "compare" / "index.html", env.get_template("compare.html").render(**ctx, page="compare"))
    write_text(stage / "pdf" / "index.html", env.get_template("pdf.html").render(**ctx, page="pdf"))
    write_text(stage / "404.html", env.get_template("404.html").render(**ctx, page="404"))
    write_text(stage / "robots.txt", "User-agent: *\nDisallow: /\n")
    write_text(stage / ".nojekyll", "")

    search = []
    for e in exhibits:
        notes = " ".join([p["note_text"] for p in e["parts"]] + [q["text"] for q in e["questions"]])
        cells = " ".join(t["cells"] for p in e["parts"] for t in p["tables"])
        figtext = " ".join(g["text"] for p in e["parts"] for g in p["graphics"]) + " " + " ".join(
            g["text"] for p in e["parts"] for pn in p["panels"] for g in pn["graphics"])
        subs = " ".join(pn["sub_text"] for p in e["parts"] for pn in p["panels"])
        search.append({"id": e["id"], "u": e["url"], "k": e["kind_word"], "n": e["number"], "t": e["title"],
                       "s": e["section"], "g": e["group"]["key"], "note": notes, "cells": cells,
                       "fig": (subs + " " + figtext).strip(), "refs": " ".join(r["text"] for r in e["main_refs"]),
                       "sug": e["suggest"]})
    write_json(stage / "data" / "search.json", search, compact=True)
    lite = {k: v for k, v in model.items() if k != "exhibits"}
    lite["exhibits"] = [{k: v for k, v in e.items() if k not in ("search", "parts")} | {
        "parts": [{k: v for k, v in p.items() if k not in ("note_html", "panel_groups")} for p in e["parts"]]}
        for e in exhibits]
    write_json(stage / "data" / "exhibits.json", lite)
