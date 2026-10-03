"""One-time helper: convert the XCharter fonts shipped with TeX Live into subset WOFF2 web fonts.

Usage:  python tools/build_fonts.py

XCharter (an extension of Bitstream Charter) is distributed under the original Bitstream license,
which permits modification and redistribution provided the notice is kept; the notice is written
next to the fonts as XCharter-LICENSE.txt.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "site" / "fonts"

TEXT_FACES = {
    "XCharter-Roman.otf": "xcharter-regular.woff2",
    "XCharter-Italic.otf": "xcharter-italic.woff2",
    "XCharter-Bold.otf": "xcharter-bold.woff2",
    "XCharter-BoldItalic.otf": "xcharter-bolditalic.woff2",
}
MATH_FACE = ("XCharter-Math.otf", "xcharter-math.woff2")

# Latin, punctuation, superscripts, currency, letterlike symbols, arrows and the math operators
# that appear in captions, notes and table cells.
TEXT_UNICODES = [
    "U+0020-007E", "U+00A0-00FF", "U+0100-017F", "U+0192", "U+02C6-02DD",
    "U+2000-206F", "U+2070-209F", "U+20A0-20CF", "U+2100-214F", "U+2190-21FF",
    "U+2200-22FF", "U+2713", "U+FB00-FB06",
]

LICENSE_TEXT = """XCharter fonts (web subsets of XCharter-Roman, -Italic, -Bold, -BoldItalic and XCharter-Math).

XCharter is an extension of Bitstream Charter by Michael Sharpe; portions of the font modifications
are Copyright (c) 2009--2012 Andrey Panov and Copyright (c) 2013--2024 Michael Sharpe. The fonts are
licensed as Free fonts under the same terms as the original from Bitstream. The files here were
converted to WOFF2 and subset to the characters used on this site.

Original Bitstream Free font license:

  (c) Copyright 1989-1992, Bitstream Inc., Cambridge, MA.

  You are hereby granted permission under all Bitstream propriety rights
  to use, copy, modify, sublicense, sell, and redistribute the 4
  Bitstream Charter (r) Type 1 outline fonts for any purpose and without
  restriction; provided, that this notice is left intact on all copies
  of such fonts and that Bitstream's trademark is acknowledged as shown
  below on all unmodified copies of the 4 Charter Type 1 fonts.

    BITSTREAM CHARTER is a registered trademark of Bitstream Inc.
"""


def kpsewhich(name: str) -> Path:
    out = subprocess.run(["kpsewhich", name], capture_output=True, text=True, check=True).stdout.strip()
    if not out:
        raise FileNotFoundError(name)
    return Path(out)


def subset_to_woff2(src: Path, dst: Path, unicodes: list[str] | None) -> None:
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["*"]
    opts.name_IDs = ["*"]
    opts.name_languages = ["*"]
    opts.notdef_outline = True
    opts.glyph_names = False
    opts.hinting = False
    font = subset.load_font(str(src), opts)
    sub = subset.Subsetter(opts)
    if unicodes is None:
        sub.populate(unicodes=font.getBestCmap().keys())
    else:
        sub.populate(unicodes=subset.parse_unicodes(",".join(unicodes)))
    sub.subset(font)
    subset.save_font(font, str(dst), opts)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for src_name, dst_name in TEXT_FACES.items():
        src = kpsewhich(src_name)
        subset_to_woff2(src, OUT / dst_name, TEXT_UNICODES)
        print(f"{dst_name}: {(OUT / dst_name).stat().st_size // 1024} KB")
    src = kpsewhich(MATH_FACE[0])
    subset_to_woff2(src, OUT / MATH_FACE[1], None)  # keep every glyph so MATH variants stay intact
    print(f"{MATH_FACE[1]}: {(OUT / MATH_FACE[1]).stat().st_size // 1024} KB")
    (OUT / "XCharter-LICENSE.txt").write_text(LICENSE_TEXT, encoding="utf-8", newline="\n")
    cmap = TTFont(str(kpsewhich("XCharter-Roman.otf"))).getBestCmap()
    missing = [hex(c) for c in (0x2212, 0x2713, 0x00D7, 0x2264, 0x2192, 0x2009, 0x202F) if c not in cmap]
    print("XCharter-Roman lacks:", ", ".join(missing) or "nothing checked")


if __name__ == "__main__":
    main()
