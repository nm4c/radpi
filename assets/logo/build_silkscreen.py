#!/usr/bin/env python3
"""The RadPi logo for PCB silkscreen: one colour (white), no gradients, every line and gap at
least 0.15 mm at the intended size, and the name as outlines (KiCad's SVG import ignores <text>).

    pip install fonttools
    python3 assets/logo/build_silkscreen.py path/to/Nunito[wght].ttf

The font is Nunito (SIL Open Font License, github.com/googlefonts/nunito): only its letter shapes
end up in the SVGs, the font itself isn't needed afterwards. Writes next to this file:

  radpi-silk.svg        icon and name, 30 mm wide
  radpi-silk-icon.svg   icon alone, 10 mm tall

KiCad: Footprint Editor > File > Import > Graphics, layer F.Silkscreen (or B.Silkscreen,
mirrored). The sizes are set in mm in the files; the import's scale changes them. Below about
20 mm wide (logo) or 7 mm tall (icon), lines and gaps fall under 0.15 mm: check with DRC.
"""

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
W = "#ffffff"


def arc(cx, cy, r, a0, a1):
    def p(a):
        return cx + r * math.sin(math.radians(a)), cy - r * math.cos(math.radians(a))
    (x0, y0), (x1, y1) = p(a0), p(a1)
    return f"M{x0:.2f} {y0:.2f} A{r} {r} 0 0 1 {x1:.2f} {y1:.2f}"


def icon():
    """Same drawing as the colour logo (200 x 210 box), simplified for one colour."""
    g = []
    line = f'fill="none" stroke="{W}" stroke-linecap="round"'
    for i, r in enumerate((18, 31, 44)):              # radio waves
        for a0, a1 in ((-64, -26), (26, 64)):
            g.append(f'<path d="{arc(100, 54, r, a0, a1)}" {line} stroke-width="{5.6 - i * 0.6:.1f}"/>')
    g.append(f'<line x1="100" y1="60" x2="100" y2="110" {line} stroke-width="5"/>')   # mast
    for y in (78, 85, 92):                                                             # loading coil
        g.append(f'<line x1="93" y1="{y}" x2="107" y2="{y + 3}" {line} stroke-width="3.6"/>')
    g.append(f'<circle cx="100" cy="54" r="6" fill="{W}"/>')                         # tip
    pts = []                                                                           # calyx
    for k in range(12):
        a = math.radians(-90 + k * 30)
        rr = (30 if math.sin(a) < 0.1 else 13) if k % 2 == 0 else 10   # short points downwards,
        squash = 0.42 if math.sin(a) > 0 else 0.75                       # clear of the dots
        pts.append(f"{100 + rr * math.cos(a):.2f},{114 + rr * math.sin(a) * squash:.2f}")
    g.append(f'<polygon points="{" ".join(pts)}" fill="{W}" stroke="{W}" stroke-width="1.5" stroke-linejoin="round"/>')
    # berry: separate dots (the gaps show the board), with a pi in a space in the middle
    skip = {(1, 1), (1, 2), (1, 3), (2, 1), (2, 2)}
    y0, dy, sp, r = 130, 17, 20, 8.2
    for k, n in enumerate((4, 5, 4, 3, 2)):
        for j in range(n):
            if (k, j) in skip:
                continue
            x = 100 + (j - (n - 1) / 2) * sp
            g.append(f'<circle cx="{x:.1f}" cy="{y0 + k * dy}" r="{r}" fill="{W}"/>')
    pi = f'fill="none" stroke="{W}" stroke-linecap="round" stroke-width="5"'
    g.append(f'<path d="M83 148 H117" {pi}/>')
    g.append(f'<path d="M92 149 Q92 162 86 169" {pi}/>')
    g.append(f'<path d="M108 149 V163 Q108 169 115 166.5" {pi}/>')
    return "\n".join(g)


def wordmark(font_path, x, baseline, size, tracking=-2):
    """'RadPi' as filled outlines, Nunito ExtraBold."""
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    font = TTFont(font_path)
    if "fvar" in font:
        font = instancer.instantiateVariableFont(font, {"wght": 800})
    upm = font["head"].unitsPerEm
    s = size / upm
    cmap, glyphs, hmtx = font.getBestCmap(), font.getGlyphSet(), font["hmtx"]
    out, pen_x = [], x
    for ch in "RadPi":
        name = cmap[ord(ch)]
        pen = SVGPathPen(glyphs)
        glyphs[name].draw(TransformPen(pen, (s, 0, 0, -s, pen_x, baseline)))
        out.append(pen.getCommands())
        pen_x += hmtx[name][0] * s + tracking
    return f'<path fill="{W}" fill-rule="nonzero" d="{" ".join(out)}"/>', pen_x


def doc(view, width_mm, body):
    h_mm = width_mm * view[3] / view[2]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{" ".join(map(str, view))}" '
            f'width="{width_mm:g}mm" height="{h_mm:.2f}mm">\n{body}\n</svg>\n')


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    text, end = wordmark(sys.argv[1], 166, 160, 112)
    right = math.ceil(end + 4)
    view = (36, 2, right - 36, 202)
    (HERE / "radpi-silk.svg").write_text(doc(view, 30, icon() + "\n" + text))
    (HERE / "radpi-silk-icon.svg").write_text(doc((36, 2, 128, 202), 10 * 128 / 202, icon()))
    print("wrote", HERE / "radpi-silk.svg", HERE / "radpi-silk-icon.svg")
    unit = 30 / view[2]
    print(f"at 30 mm wide: 1 drawing unit = {unit:.4f} mm; thinnest line {3.6 * unit:.3f} mm, "
          f"dot gap {(20 - 2 * 8.2) * unit:.3f} mm")


if __name__ == "__main__":
    main()
