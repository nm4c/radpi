"""A small screen on the Pi itself: the Pimoroni Display HAT Mini (display: in config.yaml).

The HAT is a 2.0" 320x240 ST7789 LCD on SPI0 CE1 with four buttons:

  GPIO 7, 10, 11  SPI CE1, MOSI, SCLK      GPIO 5   button A     GPIO 16  button X
  GPIO 9          data/command             GPIO 6   button B     GPIO 24  button Y
  GPIO 13         backlight (on/off)

It also has an RGB LED on GPIO 17, 27 and 22. On the RadPi HATs those are PTT, the SA818's
power-down and its power level, so this file never touches them, and Pimoroni's displayhatmini
library (which does) isn't used: the LCD is driven directly with spidev. Take the LED (or its
three resistors) off the board: wired to 3.3 V, it can pull the PTT line partway up at boot,
before the server has set it.

The backlight is plain on/off. Dimming it with PWM would switch 20-odd mA at a few hundred Hz to a
few kHz right over the audio codec, and that's a whine on received and transmitted audio.

The SPI clock is display.spi_hz (default 20 MHz). Its harmonics land every 20 MHz: 460 and 480
MHz, clear of UHF CB (476.4-477.4 MHz). The Pi rounds the clock down to what it can make, so if
you change it, check that no multiple of the real clock lands on a channel you use. The screen
is only redrawn when something on it changes (at most a few times a second).

With gpio.backend: mock there's no hardware: frames are written to display.mock_file (a PNG), to
see the layout while developing.

Pages (button X steps through them):
  radio    channel, frequency and tone, who's on air or what was last heard, operators online
  system   addresses, temperature, uptime, the AI agent's state
"""

import asyncio
import logging
import os
import socket
import subprocess
import time
from pathlib import Path

import numpy as np

from .radio import RadioError

log = logging.getLogger("display")

W, H = 320, 240
PINS = {"dc": 9, "backlight": 13, "a": 5, "b": 6, "x": 16, "y": 24}
SPI_PINS = (7, 9, 10, 11)                 # CE1, MISO (used as DC), MOSI, SCLK
LED_PINS = (17, 27, 22)                   # never used here: see above
ACTIONS = ("channel_up", "channel_down", "page", "backlight", "none")
PAGES = ("radio", "system")

# colours
BG = (12, 14, 18)
FG = (230, 233, 236)
MUTED = (140, 148, 158)
LINE = (44, 50, 58)
RX = (24, 140, 72)
TX = (196, 36, 36)
WARN = (200, 120, 20)
IDLE = (40, 46, 54)
ACCENT = (90, 170, 255)

FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/Library/Fonts",
             "/System/Library/Fonts/Supplemental")
FONT_FILES = {False: ("DejaVuSans.ttf", "Roboto-Regular.ttf", "Arial.ttf"),
              True: ("DejaVuSans-Bold.ttf", "Roboto-Bold.ttf", "Arial Bold.ttf")}


def _font(size, bold=False):
    from PIL import ImageFont
    for d in FONT_DIRS:
        for name in FONT_FILES[bold]:
            p = Path(d) / name
            if p.exists():
                return ImageFont.truetype(str(p), size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:          # Pillow < 10.1
        return ImageFont.load_default()


def _ago(t):
    s = max(0, int(time.time() - t))
    if s < 60:
        return f"{s} s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"


def _addresses():
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2).stdout.split()
        v4 = [a for a in out if "." in a]
        if v4:
            return v4
    except (OSError, subprocess.SubprocessError):
        pass
    try:  # no packets are sent: this only asks the kernel which address it would use
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return [s.getsockname()[0]]
    except OSError:
        return []


def _temperature():
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return None


def _uptime():
    try:
        s = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    d, s = divmod(int(s), 86400)
    h, s = divmod(s, 3600)
    return f"{d} d {h} h" if d else f"{h} h {s // 60} min"


# ---- the LCD ----------------------------------------------------------------------------------

class ST7789:
    """The HAT's 320x240 panel, landscape. show() takes an RGB PIL image of W x H."""

    def __init__(self, cfg, gpio_backend):
        import spidev
        from .radio import gpio_output
        self.spi = spidev.SpiDev()
        self.spi.open(0, 1)                    # SPI0, CE1
        self.spi.mode = 0
        self.spi.max_speed_hz = int(cfg["spi_hz"])
        self.dc = gpio_output(gpio_backend, PINS["dc"], True, "LCD DC")
        self.flip = int(cfg["rotate"]) % 360 == 180
        self.chunk = 4096                      # spidev's default buffer size
        self._init()

    def _cmd(self, c, *data):
        self.dc.off()
        self.spi.writebytes([c])
        if data:
            self.dc.on()
            self.spi.writebytes(list(data))

    def _init(self):
        self._cmd(0x01); time.sleep(0.15)                   # software reset
        self._cmd(0x36, 0x70)                               # MADCTL: landscape 320 x 240
        self._cmd(0xB2, 0x0C, 0x0C, 0x00, 0x33, 0x33)       # porch
        self._cmd(0x3A, 0x05)                               # 16 bits per pixel (RGB565)
        self._cmd(0xB7, 0x14)                               # gate control
        self._cmd(0xBB, 0x37)                               # VCOM
        self._cmd(0xC0, 0x2C)
        self._cmd(0xC2, 0x01)
        self._cmd(0xC3, 0x12)
        self._cmd(0xC4, 0x20)
        self._cmd(0xD0, 0xA4, 0xA1)                         # power
        self._cmd(0xC6, 0x0F)                               # 60 Hz frame rate
        self._cmd(0xE0, 0xD0, 0x04, 0x0D, 0x11, 0x13, 0x2B, 0x3F, 0x54, 0x4C, 0x18, 0x0D, 0x0B, 0x1F, 0x23)
        self._cmd(0xE1, 0xD0, 0x04, 0x0C, 0x11, 0x13, 0x2C, 0x3F, 0x44, 0x51, 0x2F, 0x1F, 0x1F, 0x20, 0x23)
        self._cmd(0x21)                                     # colour inversion on (this panel needs it)
        self._cmd(0x11); time.sleep(0.12)                   # out of sleep
        self._cmd(0x29)                                     # display on

    def show(self, image):
        px = np.asarray(image.convert("RGB"), dtype=np.uint16)
        if self.flip:
            px = px[::-1, ::-1]
        rgb = ((px[..., 0] & 0xF8) << 8) | ((px[..., 1] & 0xFC) << 3) | (px[..., 2] >> 3)
        data = rgb.astype(">u2").tobytes()
        self._cmd(0x2A, 0, 0, (W - 1) >> 8, (W - 1) & 0xFF)
        self._cmd(0x2B, 0, 0, (H - 1) >> 8, (H - 1) & 0xFF)
        self._cmd(0x2C)
        self.dc.on()
        for i in range(0, len(data), self.chunk):
            self.spi.writebytes2(data[i:i + self.chunk])

    def close(self):
        try:
            self._cmd(0x28)                                 # display off
            self._cmd(0x10)                                 # sleep
        finally:
            self.spi.close()
            self.dc.close()


class MockLCD:
    """No hardware: each frame goes to a PNG file (display.mock_file), if one is set."""

    def __init__(self, cfg):
        self.path = Path(cfg["mock_file"]) if cfg.get("mock_file") else None

    def show(self, image):
        if self.path:
            tmp = self.path.with_suffix(".tmp.png")
            image.save(tmp)
            os.replace(tmp, self.path)

    def close(self):
        pass


# ---- the screen -------------------------------------------------------------------------------

class Display:
    def __init__(self, cfg, head):
        self.cfg = d = cfg["display"]
        self.head = head
        self.backend = cfg["gpio"]["backend"]
        self.buttons = {k: (d["buttons"] or {}).get(k, "none") for k in ("a", "b", "x", "y")}
        self.sleep_s = float(d["sleep_s"] or 0)
        self.page = 0
        self.lit = True
        self.last_activity = time.monotonic()
        self.toast = None                     # (text, until): a short message after a button press
        self.last_heard = None                # time.time() the channel was last busy, without the AI agent
        self._was_busy = False
        self._shown = None
        self._sys = (0.0, {})                 # (when, system info): refreshed every few seconds
        self._wake = asyncio.Event()
        self._inputs = []
        self.lcd = None
        self.light = None
        self.fonts = {k: _font(*v) for k, v in {
            "small": (14,), "body": (17,), "body_b": (17, True), "big": (34, True), "mid": (21,),
            "banner": (20, True)}.items()}

    # ---- hardware ----

    def _open(self):
        if self.backend == "mock":
            self.lcd = MockLCD(self.cfg)
            return
        from gpiozero import Button
        from .radio import gpio_output
        self.lcd = ST7789(self.cfg, self.backend)
        self.light = gpio_output(self.backend, PINS["backlight"], True, "LCD backlight")
        self.light.on()
        loop = asyncio.get_running_loop()
        for key in ("a", "b", "x", "y"):
            if self.buttons[key] == "none":
                continue
            b = Button(PINS[key], pull_up=True, bounce_time=0.03)
            b.when_pressed = lambda k=key: loop.call_soon_threadsafe(self._pressed, k)
            self._inputs.append(b)

    def close(self):
        for b in self._inputs:
            b.close()
        if self.light:
            self.light.off()
            self.light.close()
        if self.lcd:
            try:
                self.lcd.close()
            except Exception as e:
                log.warning("closing the display: %s", e)

    # ---- buttons ----

    def _pressed(self, key):
        """A button (called on the event loop). A press while the screen is dark only wakes it."""
        self.last_activity = time.monotonic()
        if not self.lit:
            self._set_light(True)
            self._wake.set()
            return
        action = self.buttons[key]
        if action in ("channel_up", "channel_down"):
            self.head._spawn(self._step(1 if action == "channel_up" else -1))
        elif action == "page":
            self.page = (self.page + 1) % len(PAGES)
        elif action == "backlight":
            self._set_light(False)
        self._wake.set()

    async def _step(self, direction):
        try:
            await self.head.radio.step(direction)
        except RadioError as e:
            self.toast = (str(e), time.monotonic() + 3)
        else:
            r = self.head.radio
            self.head.audit(None, "channel", channel=r.channel, name=r.channel_name(r.channel), by="display")
        self._wake.set()

    def _set_light(self, on):
        self.lit = on
        if self.light:
            (self.light.on if on else self.light.off)()

    # ---- what's on screen ----

    def _view(self):
        """Everything the screen shows, as plain data: redrawn only when this changes."""
        h, r = self.head, self.head.radio
        busy = bool(h._busy())
        if busy:
            self.last_heard = time.time()
        if busy != self._was_busy:
            self._was_busy = busy
            self.last_activity = time.monotonic()
        tx = None
        if r.ptt_owner is not None:
            owner = h._client(r.ptt_owner)
            tx = ("selcall " + h.selcall_tx if h.selcall_tx else
                  "clip: " + h.clip_tx["name"] if h.clip_tx else
                  "AI: " + h.bot_tx["text"] if h.bot_tx else
                  "say: " + h.say_tx["by"] if h.say_tx else
                  owner.name if owner else "on air")
            self.last_activity = time.monotonic()
        info = r.channel_info() or {}
        rssi = r.rssi
        heard = self._heard()
        toast = self.toast[0] if self.toast and self.toast[1] > time.monotonic() else None
        view = {
            "page": PAGES[self.page], "channel": r.channel, "name": r.channel_name(r.channel),
            "info": tuple(sorted((k, str(v)) for k, v in info.items())), "stepping": r.stepping,
            "error": r.error, "tx": tx, "busy": busy, "bars": None if rssi is None else min(4, max(0, (rssi - 40) // 15)),
            "operators": tuple(sorted(c.name for c in h._authed())), "heard": heard, "toast": toast,
            "minute": int(time.time() // 60),
        }
        if view["page"] == "system":
            view["sys"] = tuple(sorted(self._system().items()))
        return view

    def _heard(self):
        """(who, text, ago) for the last over the AI agent heard, or (None, None, ago) from the busy
        line alone; None if nothing yet."""
        bot = self.head.bot
        if bot is not None:
            for it in reversed(list(bot.items.values())):
                if it.get("kind") == "over" and it.get("text") is not None:
                    return (it.get("who") or "Someone", it["text"] or "(no words)", _ago(it["t"]))
        if self.last_heard:
            return (None, None, _ago(self.last_heard))
        return None

    def _system(self):
        when, info = self._sys
        if time.monotonic() - when > 10:
            info = {"addresses": " ".join(_addresses()[:2]) or "no network", "host": socket.gethostname(),
                    "temp": _temperature(), "uptime": _uptime()}
            bot = self.head.bot
            if bot is not None:
                info["ai"] = f"{bot.mode}" + ("" if all(v == "ready" for v in bot.status.values()) else
                                              " · " + ", ".join(f"{k} {v}" for k, v in bot.status.items() if v != "ready"))
            self._sys = (time.monotonic(), info)
        return info

    # ---- drawing ----

    def _draw(self, v):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        f = self.fonts

        # Banner: on air / busy / idle / fault, with the signal bars.
        if v["error"]:
            colour, text = WARN, v["error"]
        elif v["tx"]:
            colour, text = TX, "ON AIR · " + v["tx"]
        elif v["busy"]:
            colour, text = RX, "RECEIVING"
        else:
            colour, text = IDLE, "Channel clear"
        d.rectangle((0, 0, W, 34), fill=colour)
        d.text((10, 17), self._fit(d, text, f["banner"], W - 70), font=f["banner"], fill=FG, anchor="lm")
        if v["bars"] is not None:
            for i in range(4):
                x, hgt = W - 50 + i * 10, 6 + i * 5
                d.rectangle((x, 28 - hgt, x + 6, 28), fill=FG if i < v["bars"] else LINE)

        if v["page"] == "radio":
            self._radio(d, v)
        else:
            self._system_page(d, v)

        # Button hints, by the buttons: A top left, B bottom left, X top right, Y bottom right.
        for key, (x, y) in {"a": (9, 52), "b": (9, 200), "x": (W - 9, 52), "y": (W - 9, 200)}.items():
            self._hint(d, self.buttons[key], x, y)

        # Footer.
        d.line((0, 214, W, 214), fill=LINE)
        if v["toast"]:
            d.text((10, 227), self._fit(d, v["toast"], f["small"], W - 20), font=f["small"], fill=WARN, anchor="lm")
        else:
            ops = v["operators"]
            left = f"{len(ops)} online" + (f": {', '.join(ops)}" if ops else "")
            d.text((10, 227), self._fit(d, left, f["small"], W - 80), font=f["small"], fill=MUTED, anchor="lm")
            d.text((W - 10, 227), time.strftime("%H:%M"), font=f["small"], fill=MUTED, anchor="rm")
        return img

    @staticmethod
    def _hint(d, action, x, y):
        """A small drawn mark by a button for what it does (drawn, so it needs no font glyphs)."""
        if action == "channel_up":
            d.polygon([(x - 5, y + 4), (x + 5, y + 4), (x, y - 5)], fill=MUTED)
        elif action == "channel_down":
            d.polygon([(x - 5, y - 4), (x + 5, y - 4), (x, y + 5)], fill=MUTED)
        elif action == "page":
            for dy in (-4, 0, 4):
                d.line((x - 5, y + dy, x + 5, y + dy), fill=MUTED, width=2)
        elif action == "backlight":
            d.ellipse((x - 3, y - 3, x + 3, y + 3), outline=MUTED, width=2)
            for dx, dy in ((0, -6), (0, 6), (-6, 0), (6, 0)):
                d.point((x + dx, y + dy), fill=MUTED)

    def _radio(self, d, v):
        f = self.fonts
        info = dict(v["info"])
        label = ("VFO" if v["channel"] == 0 else f"CH {v['channel']}")
        name = v["name"] if v["name"] and v["name"] != label else ""
        d.text((24, 62), label + ("…" if v["stepping"] else ""), font=f["big"], fill=FG, anchor="lm")
        if name:
            x = 24 + d.textlength(label + "  ", font=f["big"])
            d.text((x, 64), self._fit(d, name, f["mid"], W - x - 24), font=f["mid"], fill=ACCENT, anchor="lm")
        freq = info.get("rx", "")
        if info.get("tx") and info.get("tx") != freq:
            freq += f"  TX {info['tx']}"
        tone = info.get("tx_tone")
        if tone and tone != "None":
            rx_tone = info.get("rx_tone")
            freq += ("   TSQL " if rx_tone and rx_tone != "None" else "   TONE ") + tone
        if freq:
            d.text((24, 96), self._fit(d, freq, f["small"], W - 48), font=f["small"], fill=MUTED, anchor="lm")
        d.line((24, 112, W - 24, 112), fill=LINE)

        heard = v["heard"]
        if heard is None:
            d.text((24, 132), "Nothing heard yet", font=f["body"], fill=MUTED, anchor="lm")
            return
        who, text, ago = heard
        if who is None:
            d.text((24, 132), f"Last heard {ago}", font=f["body"], fill=MUTED, anchor="lm")
            return
        d.text((24, 132), self._fit(d, who, f["body_b"], 170), font=f["body_b"], fill=FG, anchor="lm")
        d.text((W - 24, 132), ago, font=f["small"], fill=MUTED, anchor="rm")
        for i, line in enumerate(self._wrap(d, text, f["body"], W - 48, 3)):
            d.text((24, 156 + i * 20), line, font=f["body"], fill=FG, anchor="lm")

    def _system_page(self, d, v):
        f = self.fonts
        s = dict(v["sys"])
        rows = [("Address", s.get("addresses")), ("Host", s.get("host")),
                ("Temp", f"{s['temp']:.0f} °C" if s.get("temp") is not None else None),
                ("Up", s.get("uptime")), ("AI", s.get("ai"))]
        y = 56
        for k, val in rows:
            if val is None:
                continue
            d.text((24, y), k, font=f["small"], fill=MUTED, anchor="lm")
            d.text((100, y), self._fit(d, str(val), f["body"], W - 124), font=f["body"], fill=FG, anchor="lm")
            y += 30

    @staticmethod
    def _fit(d, text, font, width):
        if d.textlength(text, font=font) <= width:
            return text
        while text and d.textlength(text + "…", font=font) > width:
            text = text[:-1]
        return text + "…"

    def _wrap(self, d, text, font, width, lines):
        out, cur = [], ""
        for word in text.split():
            trial = f"{cur} {word}".strip()
            if d.textlength(trial, font=font) <= width:
                cur = trial
                continue
            if cur:
                out.append(cur)
            cur = word
            if len(out) == lines:
                break
        if cur and len(out) < lines:
            out.append(cur)
        if len(out) == lines and " ".join(out) != " ".join(text.split()):
            out[-1] = self._fit(d, out[-1] + " …", font, width)
        return out

    # ---- loop ----

    async def run(self):
        try:
            await asyncio.to_thread(self._open) if self.backend != "mock" else self._open()
        except Exception as e:
            log.error("display: can't start it (%s). Is SPI on (dtparam=spi=on)?", e)
            return
        log.info("display ready (%s)", "mock" if self.backend == "mock" else "Display HAT Mini")
        while True:
            try:
                v = self._view()
                if self.sleep_s and self.lit and time.monotonic() - self.last_activity > self.sleep_s:
                    self._set_light(False)
                elif not self.lit and time.monotonic() - self.last_activity < 1:
                    self._set_light(True)   # something happened on the channel: wake up
                if v != self._shown:
                    img = self._draw(v)
                    await asyncio.to_thread(self.lcd.show, img)
                    self._shown = v
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("display: drawing failed")
                await asyncio.sleep(5)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), 0.25)
            except asyncio.TimeoutError:
                pass
