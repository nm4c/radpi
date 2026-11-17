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

W, H = 300, 640
PAD = 18           # room left and right of the housing for the side keys
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
    # side keys first, so the housing overlaps their inner edge; a lighter strip on each gives it
    # some depth
    side = 'stroke="#0d0e0f" stroke-width="2"'
    p.append(rrect(-2, 146, 28, 50, 7, "#3a3e43", side))                                    # SIDE
    p.append(rrect(1, 152, 4, 38, 2, "#565b61"))
    p.append(rrect(-10, 210, 36, 168, 14, "#30343a", side))                                 # PTT
    p.append(rrect(-6, 220, 5, 148, 2.5, "#4c5157"))
    p.append(rrect(274, 148, 28, 46, 7, "#3a3e43", side))                                   # VOL+
    p.append(rrect(295, 154, 4, 34, 2, "#565b61"))
    p.append(rrect(274, 198, 28, 46, 7, "#3a3e43", side))                                   # VOL-
    p.append(rrect(295, 204, 4, 34, 2, "#565b61"))
    p.append(rrect(116, 4, 68, 22, 7, "#e6742b", 'stroke="#7a3510" stroke-width="2"'))        # EMERG
    # housing and face
    p.append(rrect(18, 14, 264, 606, 64, "#202326", 'stroke="#0b0c0d" stroke-width="5"'))
    p.append(rrect(32, 28, 236, 578, 52, "url(#face)"))
    # side key labels and hit areas
    p.append(f'<text x="7" y="294" {FONT} font-size="12" font-weight="700" fill="#b4bac0" text-anchor="middle" '
             f'transform="rotate(-90 7 294)">PTT</text>')
    p.append(rrect(-14, 206, 50, 176, 14, "transparent", 'class="mkey" data-ptt="1" aria-label="PTT"'))
    p.append(rrect(-6, 142, 40, 58, 7, "transparent", 'class="mkey" data-key="side" aria-label="side (squelch)"'))
    p.append(rrect(266, 144, 40, 52, 7, "transparent", 'class="mkey" data-key="volup" aria-label="volume up"'))
    p.append(rrect(266, 196, 40, 52, 7, "transparent", 'class="mkey" data-key="voldn" aria-label="volume down"'))
    p.append(f'<text x="289" y="176" {FONT} font-size="15" font-weight="700" fill="#b4bac0" text-anchor="middle">+</text>')
    p.append(f'<text x="289" y="226" {FONT} font-size="17" font-weight="700" fill="#b4bac0" text-anchor="middle">&#8722;</text>')
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
    p.append(key("p1", 46, 240, 48, 32, "P1", size=15))
    p.append(key("p3", 46, 284, 48, 32, "P3", size=15))
    p.append(key("p2", 206, 240, 48, 32, "P2", size=15))
    p.append(key("p4", 206, 284, 48, 32, "P4", size=15))
    # D-pad: a rounded square with a white ring and four bars, split into four hit areas
    p.append(rrect(100, 232, 100, 94, 22, KEY_FILL, f'stroke="{KEY_EDGE}" stroke-width="1.5"'))
    p.append(rrect(113, 244, 74, 70, 16, "none", f'stroke="{WHITE}" stroke-width="3"'))
    p.append(f'<rect x="139" y="253" width="22" height="5" rx="2.5" fill="{WHITE}"/>'
             f'<rect x="139" y="300" width="22" height="5" rx="2.5" fill="{WHITE}"/>'
             f'<rect x="122" y="268" width="5" height="22" rx="2.5" fill="{WHITE}"/>'
             f'<rect x="173" y="268" width="5" height="22" rx="2.5" fill="{WHITE}"/>')
    for name, (x, y, w, h) in {"up": (113, 232, 74, 34), "down": (113, 292, 74, 34),
                               "left": (100, 250, 34, 58), "right": (166, 250, 34, 58)}.items():
        p.append(rrect(x, y, w, h, 8, "transparent", f'class="mkey" data-key="{name}" aria-label="{name}"'))
    pad = [("1", ""), ("2", "ABC"), ("3", "DEF"), ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
           ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"), ("*", ""), ("0", ""), ("#", "")]
    for i, (k, sub) in enumerate(pad):
        x, y = 46 + (i % 3) * 72, 334 + (i // 3) * 44
        p.append(key(k, x, y, 64, 36, k, sub or None, size=18))
    return "\n".join(p)


def grille():
    p = []
    for i in range(4):
        y = 526 + i * 14
        inset = 30 + max(0, i - 2) * 12
        p.append(rrect(32 + inset, y, 236 - 2 * inset, 7, 3.5, "#141618"))
    return "\n".join(p)


def svg():
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-PAD} 0 {W + 2 * PAD} {H}" data-template="ipmic" role="group" aria-label="IC-F6330D mic">
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
