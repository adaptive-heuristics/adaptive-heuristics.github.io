"""Convert the appendix's figure PDFs into web assets (SVG or WebP), PNG and clean PDF downloads."""
from __future__ import annotations

import gzip
import io
import re
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from .util import BuildError, sha256_file

_NUM = re.compile(r"(-?\d+\.\d{3,})")


@dataclass
class FigAssets:
    display: str          # path relative to docs/ (svg or webp)
    kind: str             # svg | webp
    png: str
    pdf: str
    w_pt: float
    h_pt: float
    text: str             # text layer for search
    src_sha: str


def _round_svg(svg: str) -> str:
    return _NUM.sub(lambda m: f"{float(m.group(1)):.2f}".rstrip("0").rstrip("."), svg)


def _clean_pdf(src: Path, dst: Path) -> None:
    doc = fitz.open(src)
    doc.set_metadata({"title": "", "author": "", "subject": "", "keywords": "", "creator": "", "producer": "",
                      "creationDate": "", "modDate": "", "trapped": ""})
    doc.del_xml_metadata()
    info = doc.xref_get_key(-1, "Info")
    if info[0] == "xref":
        ix = int(info[1].split()[0])
        for k in doc.xref_get_keys(ix):
            doc.xref_set_key(ix, k, "null")
    doc.save(dst, garbage=4, deflate=True, no_new_id=True)
    doc.close()


def convert(src: Path, out_dir: Path, stem: str, *, svg_max_gzip_kb: int = 150, svg_max_drawings: int = 5000,
            webp_quality: int = 90, png_dpi: int = 300, css_px: int = 1100) -> FigAssets:
    """Write <stem>.svg|webp, <stem>.png and <stem>.pdf into out_dir. Paths returned relative to out_dir.parent*."""
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(src)
    if doc.page_count != 1:
        raise BuildError(f"{src.name}: expected a single-page PDF, found {doc.page_count} pages")
    page = doc[0]
    w, h = page.rect.width, page.rect.height
    has_images = bool(page.get_images())
    use_svg = False
    svg = ""
    if not has_images:
        n_draw = len(page.get_drawings())
        if n_draw <= svg_max_drawings:
            svg = _round_svg(page.get_svg_image(matrix=fitz.Identity, text_as_path=True))
            use_svg = len(gzip.compress(svg.encode("utf-8"))) <= svg_max_gzip_kb * 1024
    if use_svg:
        disp = out_dir / f"{stem}.svg"
        disp.write_text(svg, encoding="utf-8", newline="\n")
        kind = "svg"
    else:
        zoom = max(2.0, min(4.0, css_px * 2 / w))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        disp = out_dir / f"{stem}.webp"
        img.save(disp, "WEBP", quality=webp_quality, method=6)
        kind = "webp"
    pix = page.get_pixmap(dpi=png_dpi, alpha=False)
    png = out_dir / f"{stem}.png"
    Image.open(io.BytesIO(pix.tobytes("png"))).save(png, "PNG", optimize=True)
    pdf = out_dir / f"{stem}.pdf"
    _clean_pdf(src, pdf)
    words = page.get_text("words")
    text = " ".join(dict.fromkeys(wd[4].replace("−", "-") for wd in words))
    doc.close()
    return FigAssets(disp.name, kind, png.name, pdf.name, w, h, text, sha256_file(src))
