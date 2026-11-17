#!/usr/bin/env python3
"""Generate the IC-F6330D IP mic face for the web page (radio.type icom).

A plain vector drawing of the mic's layout (no photos or logos): LCD, LED, P1-P4, the D-pad,
the 12-key pad, SIDE / PTT on the left edge, VOL +/- on the right, the orange EMERG key and the
power key on top.

Hooks for web/app.js:
  data-key="up"     a radio key: app.js sends mic_key down/up with this name (icom_ipmic.KEYS)
  data-ptt          the side PTT bar: the page's own PTT (browser mic audio)
  data-power        the power key: held 1.5 s on the page, then the radio's power key
  #mic-lcd #mic-text #mic-led #mic-status, .mic-flag[data-flag]   the screen, filled from state

Run after changing anything here:
    python3 assets/control-head/build_ipmic.py
writes web/control-head/ic-f6330d.svg (and output/ic-f6330d.png if rsvg-convert is installed).
"""

import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent.parent / "web" / "control-head" / "ic-f6330d.svg"
OUT = HERE / "output"

W, H = 300, 700
FONT = 'font-family="Liberation Sans, Arial, Helvetica, sans-serif"'
MONO = 'font-family="DejaVu Sans Mono, Menlo, Consolas, monospace"'
KEY_FILL = "#2c2f33"
KEY_EDGE = "#44494f"
WHITE = "#f2f2f2"


def rrect(x, y, w, h, r, fill, extra=""):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" {extra}/>'


def key(name, x, y, w, h, label, sub=None, r=9, size=17):
    """A drawn key with its own hit area (the rect carries data-key)."""
    cx = x + w / 2
    parts = [rrect(x, y, w, h, r, KEY_FILL, f'stroke="{KEY_EDGE}" stroke-width="1.5"')]
    if sub:
        parts.append(f'<text x="{cx - 6}" y="{y + h / 2 + 6}" {FONT} font-size="{size}" font-weight="700" '
                     f'fill="{WHITE}" text-anchor="end">{label}</text>')
        parts.append(f'<text x="{cx - 1}" y="{y + h / 2 + 5}" {FONT} font-size="9" fill="#c9c9c9">{sub}</text>')
    else:
        parts.append(f'<text x="{cx}" y="{y + h / 2 + 6}" {FONT} font-size="{size}" font-weight="700" '
                     f'fill="{WHITE}" text-anchor="middle">{label}</text>')
    parts.append(rrect(x, y, w, h, r, "transparent", f'class="mkey" data-key="{name}" aria-label="{name}"'))
    return "\n".join(parts)


def body():
    p = []
    # side keys first, so the housing overlaps their inner edge
    p.append(rrect(8, 148, 20, 46, 6, "#33373b", 'stroke="#111" stroke-width="2"'))           # SIDE
    p.append(rrect(4, 214, 24, 160, 10, "#2a2d31", 'stroke="#111" stroke-width="2"'))         # PTT
    p.append(rrect(272, 150, 20, 44, 6, "#33373b", 'stroke="#111" stroke-width="2"'))         # VOL+
    p.append(rrect(272, 200, 20, 44, 6, "#33373b", 'stroke="#111" stroke-width="2"'))         # VOL-
    p.append(rrect(116, 4, 68, 22, 7, "#e6742b", 'stroke="#7a3510" stroke-width="2"'))        # EMERG
    # housing and face
    p.append(rrect(18, 14, 264, 674, 64, "#202326", 'stroke="#0b0c0d" stroke-width="5"'))
    p.append(rrect(32, 28, 236, 646, 52, "url(#face)"))
    # side key labels and hit areas
    p.append(f'<text x="18" y="296" {FONT} font-size="11" font-weight="700" fill="#9aa0a6" text-anchor="middle" '
             f'transform="rotate(-90 18 296)">PTT</text>')
    p.append(rrect(0, 210, 34, 168, 10, "transparent", 'class="mkey" data-ptt="1" aria-label="PTT"'))
    p.append(rrect(0, 144, 34, 54, 6, "transparent", 'class="mkey" data-key="side" aria-label="side (squelch)"'))
    p.append(rrect(266, 146, 34, 52, 6, "transparent", 'class="mkey" data-key="volup" aria-label="volume up"'))
    p.append(rrect(266, 198, 34, 50, 6, "transparent", 'class="mkey" data-key="voldn" aria-label="volume down"'))
    p.append(f'<text x="282" y="176" {FONT} font-size="14" font-weight="700" fill="#9aa0a6" text-anchor="middle">+</text>')
    p.append(f'<text x="282" y="226" {FONT} font-size="16" font-weight="700" fill="#9aa0a6" text-anchor="middle">&#8722;</text>')
    p.append(rrect(110, 0, 80, 30, 7, "transparent", 'class="mkey" data-key="emerg" aria-label="emerg (menu)"'))
    # power key (top left) and LED (top right)
    p.append('<circle cx="74" cy="56" r="12" fill="#2c2f33" stroke="#44494f" stroke-width="1.5"/>')
    p.append('<path d="M69.5 52.5 A6.5 6.5 0 1 0 78.5 52.5" fill="none" stroke="#c9c9c9" stroke-width="2" stroke-linecap="round"/>'
             '<line x1="74" y1="48" x2="74" y2="56" stroke="#c9c9c9" stroke-width="2" stroke-linecap="round"/>')
    p.append('<circle cx="74" cy="56" r="16" fill="transparent" class="mkey" data-power="1" aria-label="power (hold)"/>')
    p.append(rrect(222, 40, 11, 30, 5, "#1a1c1e", 'stroke="#0a0a0a" stroke-width="1.5"'))
    p.append(rrect(224, 42, 7, 26, 3.5, "#26302a", 'id="mic-led"'))
    # mic holes and name
    p.append('<circle cx="150" cy="46" r="2.2" fill="#0c0d0e"/><circle cx="143" cy="46" r="1.6" fill="#0c0d0e"/>'
             '<circle cx="157" cy="46" r="1.6" fill="#0c0d0e"/>')
    p.append(f'<text x="150" y="82" {FONT} font-size="15" font-weight="700" letter-spacing="2" fill="#e8e8e8" '
             f'text-anchor="middle">IC-F6330D</text>')
    return "\n".join(p)


def lcd():
    p = [rrect(48, 94, 204, 124, 12, "#0a0b0c"),
         rrect(57, 103, 186, 106, 5, "#9aa39a", 'id="mic-lcd"')]
    ink = 'fill="#16191a"'
    # signal bars (shown while linked), then the icons the protocol has given away so far
    for i in range(4):
        h = 4 + i * 3
        p.append(f'<rect class="mic-flag" data-flag="link" x="{66 + i * 5}" y="{124 - h}" width="3.4" height="{h}" {ink}/>')
    flags = [("shift", "SFT", 98), ("talkaround", "TA", 128), ("scan", "SCAN", 153), ("busy", "BUSY", 190)]
    for name, label, x in flags:
        p.append(f'<text class="mic-flag" data-flag="{name}" x="{x}" y="124" {FONT} font-size="10" '
                 f'font-weight="700" {ink}>{label}</text>')
    p.append(f'<text id="mic-status" x="236" y="124" {FONT} font-size="10" font-weight="700" fill="#b0141a" '
             f'text-anchor="end"></text>')
    p.append(f'<text id="mic-text" x="150" y="170" {MONO} font-size="27" font-weight="700" {ink} '
             f'text-anchor="middle" data-max-width="176" xml:space="preserve"></text>')
    p.append(f'<text id="mic-sub" x="150" y="198" {FONT} font-size="10" {ink} text-anchor="middle"></text>')
    return "\n".join(p)


def keys():
    p = []
    p.append(key("p1", 46, 238, 60, 36, "P1"))
    p.append(key("p3", 46, 284, 60, 36, "P3"))
    p.append(key("p2", 194, 238, 60, 36, "P2"))
    p.append(key("p4", 194, 284, 60, 36, "P4"))
    # D-pad: a rounded square with a white ring and four bars, split into four hit areas
    p.append(rrect(114, 236, 72, 86, 18, KEY_FILL, f'stroke="{KEY_EDGE}" stroke-width="1.5"'))
    p.append(rrect(124, 248, 52, 62, 12, "none", f'stroke="{WHITE}" stroke-width="3"'))
    p.append(f'<rect x="142" y="256" width="16" height="4" rx="2" fill="{WHITE}"/>'
             f'<rect x="142" y="298" width="16" height="4" rx="2" fill="{WHITE}"/>'
             f'<rect x="132" y="271" width="4" height="16" rx="2" fill="{WHITE}"/>'
             f'<rect x="164" y="271" width="4" height="16" rx="2" fill="{WHITE}"/>')
    for name, (x, y, w, h) in {"up": (126, 236, 48, 26), "down": (126, 296, 48, 26),
                               "left": (114, 254, 26, 50), "right": (160, 254, 26, 50)}.items():
        p.append(rrect(x, y, w, h, 8, "transparent", f'class="mkey" data-key="{name}" aria-label="{name}"'))
    pad = [("1", ""), ("2", "ABC"), ("3", "DEF"), ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
           ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"), ("*", ""), ("0", ""), ("#", "")]
    for i, (k, sub) in enumerate(pad):
        x, y = 46 + (i % 3) * 72, 334 + (i // 3) * 44
        p.append(key(k, x, y, 64, 36, k, sub or None, size=18))
    return "\n".join(p)


def grille():
    p = []
    for i in range(7):
        y = 528 + i * 16
        inset = 30 + max(0, i - 4) * 10
        p.append(rrect(32 + inset, y, 236 - 2 * inset, 7, 3.5, "#141618"))
    return "\n".join(p)


def svg():
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" data-template="ipmic" role="group" aria-label="IC-F6330D mic">
<defs>
  <linearGradient id="face" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#1b1d20"/><stop offset=".5" stop-color="#25282c"/><stop offset="1" stop-color="#1b1d20"/>
  </linearGradient>
</defs>
{body()}
{lcd()}
{keys()}
{grille()}
</svg>
'''


def main():
    text = svg()
    WEB.write_text(text)
    print("wrote", WEB)
    if shutil.which("rsvg-convert"):
        OUT.mkdir(exist_ok=True)
        subprocess.run(["rsvg-convert", "-z", "2", "-o", str(OUT / "ic-f6330d.png"), str(WEB)], check=True)
        print("wrote", OUT / "ic-f6330d.png")


if __name__ == "__main__":
    main()
