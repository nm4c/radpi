"""Icom radio with an IP remote mic (IC-F6330D): RadPi stands in for the mic on the network.

The radio connects to the mic's address, so the Pi has to have it (or the radio is programmed
with the Pi's). Everything the mic does goes over IP (protocol in icom_ipmic.py and
docs/ICOM-IP-MIC.md):

  PTT       01 00 frames; the radio answers by asking for mic audio (01 04 bit 08)
  busy      the radio's green LED (squelch open)
  channels  UP/DOWN key presses, each confirmed by the channel name on the radio's screen. The
            names in channel.names must match the screen exactly ("CB Rpt 6"); then the channel
            also follows anyone changing it on the radio itself.
  audio     RTP, 8 kHz 16-bit, resampled to and from audio.wire_rate (IcomAudio below)
  screen    text, icons, LED and backlight go to the web page, which draws the mic, and the keys
            pressed there go back to the radio as key presses
"""

import asyncio
import logging
import threading
import time

import numpy as np

from .audio import Decimator, Interpolator, _dbfs, _TxBuffer, _TxMonitor
from .icom_ipmic import AUDIO_TX, IDENT, KEYS, RATE, Mic
from .radio import Radio, RadioError

log = logging.getLogger("icom")

LINK_TIMEOUT_S = 7        # keepalives come every 2 s
STEP_TIMEOUT_S = 1.5      # a channel key with no new screen after this is a failure
RX_START_MS = 60          # jitter buffer for audio from the radio
RX_MAX_MS = 300
KEY_HOLD_MAX_S = 15       # a key held from the page is let go after this, whatever happens


class _NetPtt:
    """Stands in for the PTT GPIO line."""

    def __init__(self, radio):
        self.radio = radio
        self.is_active = False

    def on(self):
        self.is_active = True
        self.radio.mic.ptt(True)

    def off(self):
        if self.is_active:
            self.is_active = False
            self.radio.mic.ptt(False)

    def close(self):
        self.off()


class IcomRadio(Radio):
    kind = "icom"
    can_sync = True    # "sync" still works when names aren't set up (or don't match the screen)

    def __init__(self, cfg, on_change):
        super().__init__(cfg, on_change)
        i, c = cfg["icom"], cfg["channel"]
        names = {int(k): str(v) for k, v in (c.get("names") or {}).items()}
        self._init_channels(int(c["count"]), names, int(c["start"]))
        self._by_name = {v.strip(): k for k, v in names.items()}
        self.press_s = c["press_ms"] / 1000
        self.gap_s = c["gap_ms"] / 1000
        ident = bytes.fromhex(i["ident"]) if i.get("ident") else IDENT
        self.mic = Mic(i["listen"], i["radio_ip"], ident, on_display=self._on_display,
                       on_led=self._on_led, on_state=self._on_link,
                       radio_rtp_port=int(i.get("radio_rtp_port") or 50000))
        self.link = False
        self.powered = None       # False after the radio says it's switching off
        self.screen = {"text": "", "flags": {}, "red": False, "green": False, "backlight": False}
        self._screen_seen = None
        self._flush_handle = None
        self._held = {}           # key -> (owner, timer handle)
        self._update_error()

    def _make_ptt(self, cfg):
        return _NetPtt(self)

    def _make_busy(self, cfg):
        return None

    # ---- from the radio
    def _on_display(self, text, flags):
        self.screen.update(text=text, flags={k: v for k, v in flags.items() if k != "raw"})
        self.powered = True
        n = self._by_name.get(text.strip())
        if n and n != self.channel:
            self.channel = n
            self._save()
        if self._screen_seen:
            self._screen_seen.set()
        self._update_error()
        self._soon()

    def _on_led(self, red, green):
        self.screen.update(red=red, green=green, backlight=self.mic.backlight)
        self._soon()

    def _on_link(self, state):
        log.info("radio link: %s", state)
        if state == "connected":
            self.link = True
        elif state == "disconnected":
            self.link = False
            self.screen.update(red=False, green=False)
            self._release_all()
            if self.ptt_owner is not None:
                self.unkey("radio link lost")
        elif state == "power_off":
            self.powered = False
            self.screen.update(text="", flags={}, red=False, green=False, backlight=False)
        elif state == "power_on":
            self.powered = None
        self._update_error()
        self._soon()

    def _update_error(self):
        err = ("Radio not connected (waiting for it to connect to the mic address)" if not self.link
               else "Radio is switched off" if self.powered is False else None)
        if err != self.error:
            self.error = err
            if err:
                log.warning("%s", err)

    def _soon(self):
        """Screen changes come in bursts (a scan blinks 15 times a second): one broadcast a burst."""
        if self._flush_handle:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flush_handle = loop.call_later(0.08, self._flush)

    def _flush(self):
        self._flush_handle = None
        self._changed()

    # ---- state for the page
    @property
    def busy_input(self):
        return bool(self.link and self.screen["green"])

    def mic_state(self):
        return {**self.screen, "link": self.link, "powered": self.powered}

    # ---- keys from the page
    def press(self, owner, key, down):
        if key not in KEYS:
            raise RadioError(f"No key {key}")
        if not self.link:
            raise RadioError(self.error)
        if self.stepping:
            raise RadioError("Channel change in progress")
        held = self._held.get(key)
        if down:
            if held:
                return
            self.mic.key_down(key)
            timer = asyncio.get_running_loop().call_later(KEY_HOLD_MAX_S, self.release, owner, key)
            self._held[key] = (owner, timer)
        elif held:
            self.release(held[0], key)

    def release(self, owner, key=None):
        """Let go of one key, or of everything this owner holds (they disconnected)."""
        for k, (who, timer) in list(self._held.items()):
            if who == owner and (key is None or k == key):
                timer.cancel()
                del self._held[k]
                self.mic.key_up(k)

    def _release_all(self):
        for k, (_, timer) in list(self._held.items()):
            timer.cancel()
        self._held.clear()

    async def power(self):
        """Off with a 2 s hold, on with a short press, as on the mic."""
        if not self.link:
            raise RadioError(self.error)
        if self.ptt_owner is not None:
            raise RadioError("Can't switch off while transmitting")
        await self.mic.power(0.5 if self.powered is False else 2.2)

    # ---- channels
    async def _tune(self, target):
        if not self.link or self.powered is False:
            raise RadioError(self.error or "Radio not ready")
        diff = target - self.channel
        if diff == 0:
            return
        if self.wrap:
            up = diff % self.count
            down = self.count - up
            direction, times = (1, up) if up <= down else (-1, down)
        else:
            direction, times = (1 if diff > 0 else -1), abs(diff)
        for _ in range(times):
            nxt = self._next(direction)
            if nxt == self.channel:
                break
            self._screen_seen = asyncio.Event()
            await self.mic.key("up" if direction > 0 else "down", self.press_s)
            try:
                await asyncio.wait_for(self._screen_seen.wait(), STEP_TIMEOUT_S)
            except asyncio.TimeoutError:
                raise RadioError("The radio didn't answer the channel change") from None
            finally:
                self._screen_seen = None
            if self.screen["text"].strip() not in self._by_name:
                self.channel = nxt     # names not set up: count the steps instead
            self._changed()
            await asyncio.sleep(self.gap_s)
            if self.channel == target:
                break

    def sync(self, channel):
        if not 1 <= channel <= self.count:
            raise RadioError(f"No channel {channel}")
        self.channel = channel
        self._save()
        self._changed()

    # ---- lifecycle
    async def run(self):
        await self.mic.start()
        while True:
            await asyncio.sleep(1)
            if self.link and time.monotonic() - self.mic.last_seen > LINK_TIMEOUT_S:
                log.warning("no keepalive from the radio for %d s: dropping the link", LINK_TIMEOUT_S)
                self.mic.drop()

    def close(self):
        self._release_all()
        super().close()
        self.mic.close_now()


class IcomAudio:
    """The audio side of IcomRadio, in place of the sound card: the same interface as
    audio.SoundDeviceAudio. A thread ticks every frame: it hands RadPi one frame of received audio
    (silence when the radio sends none) and, while the radio is transmitting for us, sends one
    frame of transmit audio."""

    def __init__(self, cfg, on_rx, radio):
        a, i = cfg["audio"], cfg["icom"]
        self.wire_rate = int(a["wire_rate"])
        self.m = self.wire_rate // RATE
        self.frame = self.wire_rate * int(a["frame_ms"]) // 1000
        self.frame_s = int(a["frame_ms"]) / 1000
        self.n8 = self.frame // self.m
        self.rx_gain = float(i["rx_gain"])
        self.tx_gain = float(i["tx_gain"])
        self.up = Interpolator(self.m)
        self.down = Decimator(self.m)
        self.tx = _TxBuffer(self.wire_rate, a["tx_prebuffer_ms"])
        self.tx_mon = _TxMonitor(self.frame * 2)
        self.on_rx = on_rx
        self.rx_dbfs = -120.0
        self.mic = radio.mic
        self.mic.on_audio = self._rx
        self._rx_lock = threading.Lock()
        self._rx_buf = np.zeros(0, np.float32)
        self._rx_started = False
        self._stop = threading.Event()
        self._thread = None
        self.loop = None

    def _rx(self, pcm):
        """From the event loop: one RTP packet from the radio (int16 LE, 8 kHz)."""
        x = np.frombuffer(pcm, "<i2").astype(np.float32) * (self.rx_gain / 32768)
        with self._rx_lock:
            self._rx_buf = np.concatenate([self._rx_buf, x])
            cap = RATE * RX_MAX_MS // 1000
            if len(self._rx_buf) > cap:
                self._rx_buf = self._rx_buf[-cap:]

    def _take_rx(self):
        with self._rx_lock:
            if not self._rx_started and len(self._rx_buf) >= RATE * RX_START_MS // 1000:
                self._rx_started = True
            if not self._rx_started:
                return np.zeros(self.n8, np.float32)
            n = min(self.n8, len(self._rx_buf))
            out = np.zeros(self.n8, np.float32)
            out[:n] = self._rx_buf[:n]
            self._rx_buf = self._rx_buf[n:]
            if n < self.n8:
                self._rx_started = False
            return out

    def start(self):
        self.loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name="icom-audio")
        self._thread.start()
        log.info("audio over IP from the radio (8 kHz RTP <-> %d Hz)", self.wire_rate)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(1)

    def _run(self):
        t0 = time.monotonic()
        n = 0
        while not self._stop.is_set():
            n += 1
            delay = t0 + n * self.frame_s - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:          # fell far behind (machine stalled): start the clock again
                t0, n = time.monotonic(), 0
            y = np.clip(self.up(self._take_rx()), -1, 1)
            self.rx_dbfs = _dbfs(y)
            self.on_rx((y * 32767).astype("<i2").tobytes())

            # Hold transmit audio back until the radio is really transmitting, so the first
            # syllable isn't sent into a radio that's still switching over.
            if self.mic.flags & AUDIO_TX:
                out = self.tx.pull(self.frame)
                self.tx_mon.put(out)
                x8 = np.clip(self.down(out) * self.tx_gain, -1, 1)
                payload = (x8 * 32767).astype(">i2").tobytes()
                self.loop.call_soon_threadsafe(self.mic.send_audio, payload)
            else:
                self.tx_mon.put(np.zeros(self.frame, np.float32))

    def take_tx_monitor(self, nbytes):
        return self.tx_mon.take(nbytes)

    def push_tx(self, pcm):
        if len(pcm) % 2 == 0:
            self.tx.push(np.frombuffer(pcm, "<i2").astype(np.float32) / 32768)

    def drain(self, on=True):
        self.tx.draining = on

    def set_tx_buffer(self, prebuffer_ms, max_ms):
        self.tx.set_size(prebuffer_ms, max_ms)

    def tx_buffered_ms(self):
        return self.tx.buffered_ms()

    def clear_tx(self):
        self.tx.clear()
