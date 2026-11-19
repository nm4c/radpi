#!/usr/bin/env python3
"""Generate the RadPi logo files: a raspberry whose stalk is an antenna, sending radio waves, with
a pi on the berry. An original drawing, not the Raspberry Pi Foundation's logo.

    python3 assets/logo/build_logo.py

writes, next to this file:
  radpi-logo.svg / radpi-logo-dark.svg   logo with the name, for light / dark backgrounds
  radpi-icon.svg                         the icon alone (follows the viewer's light / dark setting)
  radpi-logo.png                         preview
  social-preview.png                     1280x640, for GitHub's Settings > Social preview
and the web app's icons in web/icons/ (favicon.svg, icon-180/192/512.png). The PNGs need
rsvg-convert (librsvg).
"""

import math
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ICONS = HERE.parent.parent / "web" / "icons"

LIGHT = {"ink": "#1d2733", "mast": "#2b3542", "wave": "#00a7c4", "tag": "#5b6775"}
DARK = {"ink": "#eef2f6", "mast": "#dfe6ee", "wave": "#3fd2ea", "tag": "#9aa7b4"}
FAVICON = {"mast": "#7d8a98", "wave": "#13b1cd"}
RED = "#e2245c"
APP_BG = "#14171b"     # the web app's theme colour
FONT = ("font-family=\"Nunito, Quicksand, 'Arial Rounded MT Bold', 'Helvetica Neue', Arial, "
        "sans-serif\" font-weight=\"800\"")
TAG_FONT = "font-family=\"'Helvetica Neue', Arial, sans-serif\" font-weight=\"700\""


def style(c, adaptive=False):
    s = (f".ink{{fill:{c['ink']}}}.mast{{stroke:{c['mast']}}}.wave{{stroke:{c['wave']}}}"
         f".tag{{fill:{c['tag']}}}")
    if adaptive:
        d = DARK
        s += (f"@media (prefers-color-scheme: dark){{.ink{{fill:{d['ink']}}}.mast{{stroke:{d['mast']}}}"
              f".wave{{stroke:{d['wave']}}}.tag{{fill:{d['tag']}}}}}")
    return f"<style>{s}</style>"


DEFS = """<defs>
  <radialGradient id="dr" cx="36%" cy="30%" r="78%">
    <stop offset="0" stop-color="#ff7aa0"/><stop offset=".5" stop-color="#e2245c"/><stop offset="1" stop-color="#9c103c"/>
  </radialGradient>
  <linearGradient id="calyx" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#6ddc77"/><stop offset="1" stop-color="#2e9c45"/>
  </linearGradient>
</defs>"""


def arc(cx, cy, r, a0, a1):
    def p(a):
        return cx + r * math.sin(math.radians(a)), cy - r * math.cos(math.radians(a))
    (x0, y0), (x1, y1) = p(a0), p(a1)
    return f"M{x0:.1f} {y0:.1f} A{r} {r} 0 0 1 {x1:.1f} {y1:.1f}"


def icon(transform="", colors=None):
    """The icon in a 200 x 210 box (drawn part about x 40-160, y 6-202). colors: set the mast
    and wave colours on the shapes themselves instead of by class (no <style> needed)."""
    wave = f'stroke="{colors["wave"]}"' if colors else 'class="wave"'
    mast = f'stroke="{colors["mast"]}"' if colors else 'class="mast"'
    g = [f'<g transform="{transform}">' if transform else "<g>"]
    tip = (100, 54)
    for i, r in enumerate((18, 31, 44)):              # radio waves off the antenna tip
        for a0, a1 in ((-64, -26), (26, 64)):
            g.append(f'<path {wave} d="{arc(*tip, r, a0, a1)}" fill="none" '
                     f'stroke-width="{6.5 - i * 1.1:.1f}" stroke-linecap="round" opacity="{1 - i * 0.22:.2f}"/>')
    # the mast: a whip with a loading coil
    g.append(f'<line {mast} x1="100" y1="58" x2="100" y2="112" stroke-width="5.5" stroke-linecap="round"/>')
    for y in (78, 84, 90):
        g.append(f'<line {mast} x1="93.5" y1="{y}" x2="106.5" y2="{y + 3}" stroke-width="3" stroke-linecap="round"/>')
    g.append(f'<circle cx="100" cy="54" r="6.5" fill="{RED}"/><circle cx="98" cy="52" r="2" fill="#fff" opacity=".75"/>')
    # the berry: a rounded cone of drupelets, back rows first
    y0, dy, sp, r = 124, 17, 20, 11.6
    for k, n in enumerate((4, 5, 4, 3, 2)):
        y = y0 + k * dy
        for j in range(n):
            x = 100 + (j - (n - 1) / 2) * sp
            g.append(f'<circle cx="{x:.1f}" cy="{y}" r="{r}" fill="url(#dr)" stroke="#8a0e35" stroke-width="1.1"/>')
            g.append(f'<ellipse cx="{x - 3.8:.1f}" cy="{y - 4.6}" rx="3.2" ry="2.1" fill="#fff" opacity=".6"/>')
    # the calyx, where the mast goes in
    pts = []
    for k in range(12):
        a = math.radians(-90 + k * 30)
        rr = 30 if k % 2 == 0 else 9
        pts.append(f"{100 + rr * math.cos(a):.1f},{116 + rr * math.sin(a) * 0.42:.1f}")
    g.append(f'<polygon points="{" ".join(pts)}" fill="url(#calyx)" stroke="#23763a" stroke-width="1.4" '
             'stroke-linejoin="round"/>')
    # pi
    g.append('<g transform="translate(0 16)" fill="#fff" stroke="#7d0c30" stroke-width="1.6" paint-order="stroke">'
             '<path d="M81 128 Q82 121 90 121 H120 Q122 121 122 124 V127 Q122 129 119 129 H111 V148 Q111 154 '
             '116 154 Q118 154 119 152 L121 153 Q119 161 111 161 Q103 161 103 151 V129 H94 Q93 144 90 156 '
             'Q89 161 84 161 Q80 161 81 156 Q86 144 86 129 Q83 129 81 131 Z"/></g>')
    g.append("</g>")
    return "\n".join(g)


def wordmark(x, y, size=104):
    return (f'<text x="{x}" y="{y}" {FONT} font-size="{size}" letter-spacing="-2">'
            f'<tspan class="ink">Rad</tspan><tspan fill="{RED}">Pi</tspan></text>\n'
            f'<text class="tag" x="{x + 6 * size / 104:.1f}" y="{y + 36 * size / 104:.1f}" {TAG_FONT} '
            f'font-size="{17.5 * size / 104:.1f}" letter-spacing="{5 * size / 104:.1f}">REMOTE RADIO HEAD</text>')


def svg(view, w, h, body, c, adaptive=False, bg=None):
    rect = f'<rect x="{view[0]}" y="{view[1]}" width="{view[2]}" height="{view[3]}" fill="{bg}"/>\n' if bg else ""
    css = style(c, adaptive) + "\n" if c else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{" ".join(map(str, view))}" width="{w}" '
            f'height="{h}" role="img" aria-label="RadPi">\n{css}{DEFS}\n{rect}{body}\n</svg>\n')


def logo(c):
    return svg((30, 0, 520, 210), 520, 210, icon() + "\n" + wordmark(164, 150), c)


def png(src_text, out, w=None, h=None, zoom=None):
    tmp = out.with_suffix(".tmp.svg")
    tmp.write_text(src_text)
    args = ["rsvg-convert", "-o", str(out)]
    if zoom:
        args += ["-z", str(zoom)]
    if w:
        args += ["-w", str(w), "-h", str(h)]
    try:
        subprocess.run(args + [str(tmp)], check=True)
    finally:
        tmp.unlink()
    print("wrote", out)


def main():
    files = {
        HERE / "radpi-logo.svg": logo(LIGHT),
        HERE / "radpi-logo-dark.svg": logo(DARK),
        HERE / "radpi-icon.svg": svg((40, 0, 120, 210), 120, 210, icon(), LIGHT, adaptive=True),
        # RadPi's content policy blocks inline <style>, so the favicon carries its colours on the
        # shapes: mid-tones that show on light and dark browser tabs alike
        ICONS / "favicon.svg": svg((5, 5, 190, 200), 64, 64, icon(colors=FAVICON), None),
    }
    for path, text in files.items():
        path.write_text(text)
        print("wrote", path)
    if not shutil.which("rsvg-convert"):
        print("rsvg-convert not found: PNGs not made")
        return
    png(logo(LIGHT), HERE / "radpi-logo.png", zoom=2)
    # home-screen icons: a solid square (iOS and Android round the corners off)
    app = svg((0, 0, 216, 216), 216, 216, icon("translate(8 5)"), DARK, bg=APP_BG)
    for n in (180, 192, 512):
        png(app, ICONS / f"icon-{n}.png", w=n, h=n)
    # GitHub social preview: 1280 x 640 (2:1), the logo centred on white
    social = svg((0, 0, 1280, 640), 1280, 640,
                 icon("translate(150 125) scale(1.9)") + "\n" + wordmark(500, 372, 200), LIGHT, bg="#ffffff")
    png(social, HERE / "social-preview.png", w=1280, h=640)


if __name__ == "__main__":
    main()
