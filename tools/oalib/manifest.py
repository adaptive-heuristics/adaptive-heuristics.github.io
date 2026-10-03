"""Per-exhibit fingerprints and the diff that drives `status`, the build report and the commit message."""
from __future__ import annotations

import json
from pathlib import Path

from .config import Config
from .util import sha256_file, sha256_text, write_json


def compute(model: dict, stage: Path) -> dict:
    out = {"pdf": model["pdf"]["sha256"], "source_date": model["build"]["source_date"], "exhibits": {},
           "site": sha256_text("".join(sha256_file(p) for p in sorted((stage / "assets").glob("site.*"))))}
    for e in model["exhibits"]:
        inputs = {
            "caption": sha256_text(e["title"]),
            "note": sha256_text("".join(p["note_text"] for p in e["parts"])),
            "tables": sha256_text("".join(t["src_sha"] for p in e["parts"] for t in p["tables"])),
            "figures": sha256_text("".join(g["src_sha"] for p in e["parts"]
                                           for g in list(p["graphics"]) + [x for pn in p["panels"] for x in pn["graphics"]])),
            "refs": sha256_text(json.dumps([r["text"] for r in e["main_refs"]])),
            "pages": sha256_text(json.dumps(e["page_labels"])),
        }
        page = stage / e["url"].strip("/") / "index.html"
        out["exhibits"][e["label"]] = {"id": e["id"], "number": e["number"], "kind": e["kind_word"],
                                       "title": e["title"], "inputs": inputs,
                                       "page": sha256_file(page) if page.exists() else ""}
    return out


def state_dir(cfg: Config) -> Path:
    return cfg.cache / "state"


def load_state(cfg: Config) -> dict | None:
    p = state_dir(cfg) / "published.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_pending(cfg: Config, new: dict, sha: str, d: dict) -> None:
    write_json(state_dir(cfg) / "pending.json", {"manifest": new, "source_sha": sha, "diff": d})


def mark_published(cfg: Config) -> None:
    p = state_dir(cfg) / "pending.json"
    if p.exists():
        pend = json.loads(p.read_text(encoding="utf-8"))
        write_json(state_dir(cfg) / "published.json", pend["manifest"] | {"source_sha": pend["source_sha"]})


NAMES = {"caption": "title", "note": "note", "tables": "table", "figures": "figure", "refs": "main-paper references",
         "pages": "PDF pages"}


def diff(old: dict | None, new: dict) -> dict:
    if not old:
        return {"first": True, "added": [f"{v['kind']} {v['number']}" for v in new["exhibits"].values()],
                "removed": [], "renumbered": [], "changed": [], "pdf": True, "site": True}
    o, n = old["exhibits"], new["exhibits"]
    added = [f"{n[k]['kind']} {n[k]['number']}" for k in n if k not in o]
    removed = [f"{o[k]['kind']} {o[k]['number']}" for k in o if k not in n]
    renum, changed = [], []
    for k in n:
        if k not in o:
            continue
        a, b = o[k], n[k]
        if a["number"] != b["number"]:
            renum.append(f"{a['kind']} {a['number']} -> {b['number']}")
        parts = [NAMES[f] for f in b["inputs"] if a["inputs"].get(f) != b["inputs"][f]]
        if parts:
            changed.append(f"{b['kind']} {b['number']} ({', '.join(parts)})")
        elif a.get("page") != b.get("page"):
            changed.append(f"{b['kind']} {b['number']} (rendering)")
    return {"first": False, "added": added, "removed": removed, "renumbered": renum, "changed": changed,
            "pdf": old.get("pdf") != new["pdf"], "site": old.get("site") != new["site"]}


def describe(d: dict) -> str:
    if d.get("first"):
        return f"first build: {len(d['added'])} exhibits"
    lines = []
    for key, label in (("added", "added"), ("removed", "removed"), ("renumbered", "renumbered"),
                       ("changed", "changed")):
        if d[key]:
            lines.append(f"{label}: " + ", ".join(d[key]))
    if d["pdf"]:
        lines.append("the PDF changed")
    if d["site"]:
        lines.append("site design changed")
    return "\n".join(lines) if lines else "no changes since the last publish"


def commit_message(d: dict, source_date: str) -> str:
    if d.get("first"):
        return f"Publish the online appendix ({source_date})"
    head = f"Update the online appendix ({source_date})"
    body = describe(d)
    return head + ("\n\n" + body if body else "")
