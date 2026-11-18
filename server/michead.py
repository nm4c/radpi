"""A real Icom IP mic (the IC-F6330D's PoE mic head) as a control head for RadPi.

RadPi plays the radio's part for the mic (icom_ipmic.MicLink): the mic connects to nothing
itself, the Pi connects to it, as the radio does. The mic only answers the radio address it was
programmed with, so the Pi has to reach it from that address (mic_head.local, or NAT on the
router: see the README).

Two ways of working, depending on radio.type:

  icom      proxy: RadPi sits between the radio and the mic and both keep working. Everything the
            radio sends the mic (screen, LED, backlight, scan blink, volume table) is passed on
            as it is, and the mic's keys go to the radio as if RadPi's own. PTT on the mic is
            RadPi's PTT for an operator called "Mic", so the web page, the audit log, the
            transmit time limit and "in use by" all see it; its audio goes through RadPi's
            transmit path. Speaker audio is the radio's, passed on while the radio sends some.

  others    control head: RadPi draws the screen (the channel name, typed channel numbers, the
            volume), lights the LED (red transmitting, green busy) and acts on the keys as
            mic_head.keys says: channel up/down, volume, a selcall, a clip. Speaker audio is
            the received audio while the squelch is open.
"""

import asyncio
import logging
import time

import numpy as np

from .audio import Decimator, Interpolator
from .icom_ipmic import AUDIO_TX, DEFAULT_ICONS, FRAME, KEYS, RATE, MicLink, frame

log = logging.getLogger("michead")

MIC_CLIENT_ID = -1        # owner id for PTT from the first mic, -2 the second... (operators start
                          # at 1, the AI agent is 0)
KEY_NAMES = {v: k for k, v in KEYS.items()}
ACTIONS = ("none", "channel_up", "channel_down", "volume_up", "volume_down")   # plus selcall:/clip:
FLASH_S = 1.5             # a message (volume, "NO TX") stays on the screen this long
ENTRY_TIMEOUT_S = 4.0     # a half-typed channel number is forgotten after this
BACKLIGHT_S = 10.0        # the radio turns the backlight off about 10 s after the last key
HOLD_TICK_S = 0.25        # PTT keepalive for RadPi's watchdog while the mic's PTT is held
BUSY_ICON = 0x40          # display byte 56: busy (squelch open)


def check_action(a):
    a = str(a)
    if a in ACTIONS or (a.startswith("selcall:") and a[8:].strip()) or (a.startswith("clip:") and a[5:].strip()):
        return a
    raise ValueError(f"{a!r}: use {', '.join(ACTIONS)}, selcall:<digits> or clip:<name>")


class MicOperator:
    """Stands in for an operator when the mic transmits (like the AI agent's BotOperator)."""

    def __init__(self, head, mic_ip, cid, name):
        self.id = cid
        self.name = name
        self.username = "mic"
        self.remote = mic_ip
        self.last_hold = 0.0
        self.fx = None
        self.rtc = None
        self.tx_open = False
        self.authed = False
        self.control = True
        self._head = head

    def send_json(self, obj):
        """RadPi's notices to this operator: shown on the mic's screen for a moment."""
        if obj.get("type") != "notice":
            return
        log.info("%s: %s", self.name, obj.get("text"))
        code = obj.get("code")
        self._head.flash("NO TX" if code == "ptt_denied" else "TX STOP" if code == "ptt_dropped" else "ERROR")


def mic_configs(m):
    """mic_head -> one settings dict a mic: mic_head.mics entries over the mic_head settings (keys
    merged), or mic_head itself for a single mic_ip."""
    base = {k: v for k, v in m.items() if k != "mics"}
    entries = list(m.get("mics") or [])
    if m.get("mic_ip"):
        entries.insert(0, {})
    out = []
    for e in entries:
        c = {**base, **e}
        c["keys"] = {**(base.get("keys") or {}), **(e.get("keys") or {})}
        out.append(c)
    many = len(out) > 1
    for i, c in enumerate(out, 1):
        c["name"] = str(c.get("name") or (f"Mic {i}" if many else "Mic"))
    return out


class MicHeads:
    """All the configured mics. In proxy mode the radio's frames go to every one; PTT is first
    come, first served, like the web page's."""

    def __init__(self, cfg, server):
        self.server = server
        self.radio = server.radio
        self.proxy = cfg["radio"]["type"] == "icom"
        self.last_key = None      # the mic a key was last pressed on (whose audio a send call takes)
        self.heads = [MicHead(cfg, server, m, MIC_CLIENT_ID - i, self)
                      for i, m in enumerate(mic_configs(cfg["mic_head"]))]
        if self.proxy:
            self.radio.mic.on_frame = self._from_radio

    def _from_radio(self, *frame_):
        for h in self.heads:
            h._from_radio(*frame_)

    def op(self, cid):
        return next((h.op for h in self.heads if h.op.id == cid), None)

    def connected_names(self):
        return [h.op.name for h in self.heads if h.connected]

    def feed_rx(self, pcm):
        for h in self.heads:
            h.feed_rx(pcm)

    async def run(self):
        log.info("mic head: %d mic(s): %s", len(self.heads),
                 ", ".join(f"{h.op.name} at {h.cfg['mic_ip']}" for h in self.heads))
        try:
            await asyncio.gather(*(h.run() for h in self.heads))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("mic head stopped")
            raise

    def close(self):
        for h in self.heads:
            h.close()


class MicHead:
    def __init__(self, cfg, server, m, cid, group):
        self.cfg = m
        self.server = server
        self.group = group
        self.radio = server.radio
        self.proxy = cfg["radio"]["type"] == "icom"
        self.op = MicOperator(self, m["mic_ip"], cid, m["name"])
        wire = int(cfg["audio"]["wire_rate"])
        self.m = wire // RATE
        if m["mic_gain"] is not None:
            self.mic_gain = float(m["mic_gain"])
        else:   # undo icom.tx_gain, so the mic reaches the radio at its own level
            self.mic_gain = 1 / float(cfg["icom"]["tx_gain"]) if self.proxy else 4.0
        if m["rx_gain"] is not None:
            self.rx_gain = float(m["rx_gain"])
        else:
            self.rx_gain = 1 / float(cfg["icom"]["rx_gain"]) if self.proxy else 1.0
        self.keys = {k: check_action(v) for k, v in (m["keys"] or {}).items()}
        self.volume = int(m["volume"])
        self.up = Interpolator(self.m)
        self.down = Decimator(self.m)
        self._rx8 = np.zeros(0, np.float32)
        self.link = MicLink(m["mic_ip"], on_key=self._on_key, on_ptt=self._on_ptt,
                            on_power=self._on_power, on_audio=self._on_audio, on_state=self._on_state,
                            startup=True, local=m["local"],
                            src_port=int(m["src_port"]), mic_port=int(m["mic_port"]),
                            mic_rtp_port=int(m["mic_rtp_port"]))
        self._ptt_held = False
        self._entry = ""
        self._entry_t = 0.0
        self._flash = None        # (text, until)
        self._light_until = 0.0
        self._shown = None        # (text, icons, led, light, audio flags) last sent
        self._spk = False         # sending speaker audio
        # proxy: the radio's latest frames, replayed to the mic when it (re)connects
        self._cache = {}
        self._screen = None
        self._radio_link = None

    # ---- lifecycle
    async def run(self):
        await self.link.start()
        log.info("%s: %s %s (connecting from %s)", self.op.name, "proxy for the radio's mic" if self.proxy
                 else "control head at", self.cfg["mic_ip"], self.cfg["local"])
        try:
            while True:
                await asyncio.sleep(0.2)
                self._tick()
        finally:
            self.link.close_now()

    def close(self):
        self.link.close_now()

    @property
    def connected(self):
        return self.link.connected.is_set()

    def _on_state(self, state):
        log.info("%s %s", self.op.name, state)
        self._shown = None
        if state == "connected" and self.proxy:
            self._radio_link = self.radio.link
            self._replay()
        self._tick()

    # ---- proxy: radio -> mic
    def _from_radio(self, start, group, cmd, data):
        if group == 0x05:             # the radio <-> RadPi session, not the mic's
            if cmd == 0x03:           # radio closing: its next session starts from scratch
                self._cache.clear()
                self._screen = None
            return
        if (group, cmd) == (0x02, 0x0A):
            self._screen = data
        elif (group, cmd) not in ((0x02, 0x07), (0x02, 0x08)):
            self._cache[(group, cmd)] = data
        if (group, cmd) == (0x01, 0x04):
            data = self._mask_tx(data)
        if self.connected:
            self.link.send_frame(group, cmd, data)

    def _mask_tx(self, data):
        """The radio asks for mic audio when anyone transmits: only pass that on to the mic when
        it is this mic's transmission, or the radio's own (P4's send call) started from this mic."""
        owner = self.radio.ptt_owner
        mine = owner == self.op.id or (owner is None and self.group.last_key is self)
        if data and data[0] & AUDIO_TX and not mine:
            return bytes((data[0] & ~AUDIO_TX,)) + bytes(data[1:])
        return data

    def _replay(self):
        if not self.radio.link:
            self.link.display("NO RADIO")
            return
        out = b"".join(frame(g, c, self._mask_tx(d) if (g, c) == (0x01, 0x04) else d)
                       for (g, c), d in self._cache.items())
        if self._screen is not None:
            out += frame(0x02, 0x07, b"\x02") + frame(0x02, 0x0A, self._screen) + frame(0x02, 0x08, b"\x00\x44")
        self.link._send(out)

    # ---- keys
    def _on_key(self, code, down):
        name = KEY_NAMES.get(code)
        if self.proxy:
            if self.radio.stepping:      # RadPi is typing a channel: keep out of its way
                return
            if not self.radio.link:
                return
            if down:
                self.group.last_key = self
            (self.radio.mic.key_down if down else self.radio.mic.key_up)(code)
            return
        if not down or name is None:
            return
        self._light_until = time.monotonic() + BACKLIGHT_S
        if name.isdigit():
            self._digit(name)
        elif name == "#" and self._entry:
            self._go(int(self._entry))
        elif name == "*" and self._entry:
            self._entry = ""
        else:
            self._entry = ""
            self._action(self.keys.get(name, "none"))
        self._tick()

    def _digit(self, d):
        width = len(str(self.radio.count))
        self._entry = (self._entry + d)[-width:]
        self._entry_t = time.monotonic()
        if len(self._entry) == width:
            self._go(int(self._entry))

    def _go(self, n):
        self._entry = ""
        if not 1 <= n <= self.radio.count:
            self.flash("NO CH")
            return
        self._spawn(self.radio.goto(n))

    def _action(self, a):
        if a == "channel_up":
            self._spawn(self.radio.step(1))
        elif a == "channel_down":
            self._spawn(self.radio.step(-1))
        elif a in ("volume_up", "volume_down"):
            self.volume = max(0, min(10, self.volume + (1 if a == "volume_up" else -1)))
            self.flash(f"VOL {self.volume}")
        elif a.startswith("selcall:"):
            if not self.server.sc["enabled"]:
                self.flash("NO SEL")
                return
            self.flash("SELCALL")
            self.server._spawn(self.server._selcall_send(self.op, a[8:].strip()))
        elif a.startswith("clip:"):
            if not self.server.clips:
                self.flash("NO CLIP")
                return
            want = a[5:].strip().casefold()
            clip = next((c for c in self.server.clips.list() if c["name"].casefold() == want), None)
            if not clip:
                self.flash("NO CLIP")
                return
            self.flash("CLIP")
            self.server._clip_message(self.op, "clip_play", clip["id"])

    def _spawn(self, coro):
        async def run():
            from .radio import RadioError
            try:
                await coro
            except RadioError as e:
                log.info("%s: %s", self.op.name, e)
                self.flash("ERROR")
        self.server._spawn(run())

    def _on_power(self, down):
        if self.proxy and self.radio.link:
            self.radio.mic.power_key(down)

    # ---- PTT and mic audio
    def _on_ptt(self, on):
        self._ptt_held = on
        self._light_until = time.monotonic() + BACKLIGHT_S
        if on:
            self.op.last_hold = time.monotonic()
            self.server._ptt_on(self.op)
            if self.radio.ptt_owner == self.op.id:
                log.info("PTT from %s", self.op.name)
                self.up.reset()
            self.server.broadcast_state()
        else:
            self.server._spawn(self.server._ptt_off(self.op))
        self._tick()

    def _on_audio(self, pcm):
        owner = self.radio.ptt_owner
        mine = owner == self.op.id
        sendcall = (self.proxy and owner is None and self.radio.mic.flags & AUDIO_TX
                    and self.group.last_key is self)
        if not (mine or sendcall):
            return
        if mine:
            self.op.last_hold = time.monotonic()
        x = np.frombuffer(pcm, "<i2").astype(np.float32) * (self.mic_gain / 32768)
        y = np.clip(self.up(x), -1, 1)
        self.server.audio.push_tx((y * 32767).astype("<i2").tobytes())

    # ---- speaker: called with every frame of received audio (wire rate, int16 LE)
    def feed_rx(self, pcm):
        if not self.connected:
            return
        want = self._speaker_on()
        if want != self._spk:
            self._spk = want
            self._rx8 = np.zeros(0, np.float32)
            self.down = Decimator(self.m)
            if not self.proxy:
                self._send_audio_flags()
        if not want:
            return
        g = self.rx_gain * (1.0 if self.proxy else self._vol_gain())
        x = self.down(np.frombuffer(pcm, "<i2").astype(np.float32) * (g / 32768))
        self._rx8 = np.concatenate([self._rx8, x])
        while len(self._rx8) >= FRAME:
            chunk, self._rx8 = self._rx8[:FRAME], self._rx8[FRAME:]
            self.link.send_audio((np.clip(chunk, -1, 1) * 32767).astype(">i2").tobytes())

    def _speaker_on(self):
        if self.radio.ptt_owner is not None:
            return False
        if self.proxy:
            return bool(self.radio.link and self.radio.mic.flags & 0x01)
        return bool(self.server._busy())

    def _vol_gain(self):
        return 0.0 if self.volume <= 0 else 10 ** ((self.volume - 6) * 3 / 20)

    def _send_audio_flags(self):
        self.link.audio_flags(rx=self._spk, tx=self.radio.ptt_owner == self.op.id and self._ptt_held)

    # ---- the screen (control head only; a proxy shows the radio's)
    def flash(self, text):
        self._flash = (text, time.monotonic() + FLASH_S)
        self._tick()

    def _tick(self):
        if not self.connected:
            return
        now = time.monotonic()
        if self._ptt_held:
            self.op.last_hold = now
        if self.proxy:
            if self.radio.link != self._radio_link:
                self._radio_link = self.radio.link
                if not self.radio.link:      # the radio is gone: say so rather than freeze
                    self.link.led()
                    self.link.audio_flags()
                    self.link.display("NO RADIO")
            if self._flash and now < self._flash[1]:
                if self._shown != ("flash", self._flash[0]):
                    self.link.display(self._flash[0])
                    self._shown = ("flash", self._flash[0])
            elif self._shown and self._shown[0] == "flash":
                self._shown = None
                self._replay()
            return
        if self._entry and now - self._entry_t > ENTRY_TIMEOUT_S:
            self._entry = ""
        r = self.radio
        busy = bool(self.server._busy())
        if self._flash and now < self._flash[1]:
            text = self._flash[0]
        elif self._entry:
            text = f"CH {self._entry}_"
        elif r.error:
            text = "NO RADIO"
        else:
            text = r.channel_name(r.channel)
        icons = bytearray(DEFAULT_ICONS)
        if busy:
            icons[56 - 8] |= BUSY_ICON
        led = (r.ptt_owner is not None, busy and r.ptt_owner is None)
        light = now < self._light_until
        tx = r.ptt_owner == self.op.id and self._ptt_held
        shown = (text, bytes(icons), led, light, tx)
        if shown == self._shown:
            return
        old = self._shown
        self._shown = shown
        if not old or old[:2] != shown[:2]:
            self.link.display(text, bytes(icons))
        if not old or old[2] != led:
            self.link.led(*led)
        if not old or old[3] != light:
            self.link.backlight(light)
        if not old or old[4] != tx:
            self._send_audio_flags()

