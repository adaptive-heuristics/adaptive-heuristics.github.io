"""PDF post-processing (anonymize, drop external-document links) and anchor -> physical page mapping."""
from __future__ import annotations

import re
from pathlib import Path

import fitz  # PyMuPDF

from .util import BuildError

_BAD_ACTIONS = {"GoToR", "Launch", "JavaScript", "SubmitForm", "ImportData", "GoToE", "ResetForm"}


def _action_type(doc: fitz.Document, annot_xref: int) -> str | None:
    obj = doc.xref_object(annot_xref, compressed=True)
    m = re.search(r"/S\s*/(\w+)", obj)
    if m:
        return m.group(1)
    a = doc.xref_get_key(annot_xref, "A")
    if a[0] == "xref":
        inner = doc.xref_object(int(a[1].split()[0]), compressed=True)
        m = re.search(r"/S\s*/(\w+)", inner)
        return m.group(1) if m else None
    return None


def postprocess(src: Path, dst: Path, title: str) -> dict:
    """Write an anonymized copy: Title-only Info, no XMP, no links to other files or scripts."""
    doc = fitz.open(src)
    removed = 0
    for page in doc:
        for link in page.get_links():
            xref = link.get("xref", 0)
            kind = _action_type(doc, xref) if xref else None
            if kind in _BAD_ACTIONS or link.get("kind") in (fitz.LINK_LAUNCH, fitz.LINK_GOTOR):
                page.delete_link(link)
                removed += 1
    # catalog-level actions
    cat = doc.pdf_catalog()
    for key in ("OpenAction", "AA"):
        val = doc.xref_get_key(cat, key)
        if val[0] != "null" and any(a in str(val) for a in _BAD_ACTIONS):
            doc.xref_set_key(cat, key, "null")
    doc.set_metadata({"title": title, "author": "", "subject": "", "keywords": "", "creator": "",
                      "producer": "", "creationDate": "", "modDate": "", "trapped": ""})
    doc.del_xml_metadata()
    dst.parent.mkdir(parents=True, exist_ok=True)
    doc.save(dst, garbage=4, deflate=True, no_new_id=True)
    doc.close()
    # Info: keep only /Title
    doc = fitz.open(dst)
    info = doc.xref_get_key(-1, "Info")
    if info[0] == "xref":
        ix = int(info[1].split()[0])
        for k in doc.xref_get_keys(ix):
            if k != "Title":
                doc.xref_set_key(ix, k, "null")
    tmp = dst.with_suffix(".tmp.pdf")
    doc.save(tmp, garbage=4, deflate=True, no_new_id=True)
    doc.close()
    tmp.replace(dst)
    return {"links_removed": removed}


class PdfIndex:
    """Physical pages, printed labels and page texts of the published PDF."""

    def __init__(self, pdf: Path):
        self.path = pdf
        self.doc = fitz.open(pdf)
        self.names = self.doc.resolve_names()
        self.labels = [self.doc[i].get_label() or str(i + 1) for i in range(self.doc.page_count)]
        self._text: dict[int, str] = {}

    @property
    def page_count(self) -> int:
        return self.doc.page_count

    def page_of(self, anchor: str) -> int:
        """0-based physical page index of a named destination."""
        d = self.names.get(anchor)
        if d is None or d.get("page", -1) < 0:
            raise BuildError(f"PDF has no destination {anchor!r}")
        return int(d["page"])

    def y_of(self, anchor: str) -> float | None:
        d = self.names.get(anchor)
        to = d.get("to") if d else None
        return float(to[1]) if to else None

    def text(self, pidx: int) -> str:
        if pidx not in self._text:
            self._text[pidx] = self.doc[pidx].get_text()
        return self._text[pidx]

    def extract(self, pages: list[int], dst: Path, title: str) -> None:
        out = fitz.open()
        for p in pages:
            out.insert_pdf(self.doc, from_page=p, to_page=p, links=False, annots=False)
        out.set_metadata({"title": title, "author": "", "subject": "", "keywords": "", "creator": "",
                          "producer": "", "creationDate": "", "modDate": "", "trapped": ""})
        out.del_xml_metadata()
        dst.parent.mkdir(parents=True, exist_ok=True)
        out.save(dst, garbage=4, deflate=True, no_new_id=True)
        out.close()
