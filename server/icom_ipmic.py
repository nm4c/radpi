"""Icom IP remote mic protocol (IC-F6330D and its PoE IP mic head), reverse engineered from
captures. docs/ICOM-IP-MIC.md has the full write-up; this is the wire format and a mic emulator.

Roles: the radio is the TCP client. It connects to the mic on TCP 52001 (control) and the two
swap audio as RTP on UDP 50000 <-> 50000. Nothing is authenticated or encrypted.

Control frame:
    f3 41 71 | group | cmd | len (2, BE) | data | crc16 (2, BE) | fd
    f5 ...  same layout, but acknowledged by the other end:  f5 41 71 ee | group | cmd | 01 | crc | fd
crc16 is CRC-16/MODBUS over everything between the start byte and the crc. Any byte f0..ff in
the body is sent as ff 0X (X = low nibble), so f3 / f5 / fd only ever appear as delimiters.

Audio: RTP payload type 125, 16-bit signed big-endian PCM, 8 kHz mono, 160 samples (20 ms) a
packet. The radio only sends while it has audio for the speaker; the mic only sends when the
radio asks for it (01 04 bit 08).
"""

import asyncio
import logging
import os
import random
import struct
import time

log = logging.getLogger("icom")

CTRL_PORT = 52001
RTP_PORT = 50000
RATE = 8000
FRAME = 160                    # samples a packet (20 ms)
PT = 125
ADDR = b"\x41\x71"

# key codes (01 01 [code]; release = code | 0x80, then 0x7f = nothing held)
KEYS = {
    "side": 0x00, "voldn": 0x01, "p1": 0x02, "1": 0x03, "2": 0x04, "3": 0x05,
    "volup": 0x10, "p2": 0x12, "4": 0x13, "5": 0x14, "6": 0x15,
    "up": 0x20, "down": 0x21, "p3": 0x22, "7": 0x23, "8": 0x24, "9": 0x25,
    "left": 0x30, "right": 0x31, "p4": 0x32, "*": 0x33, "0": 0x34, "#": 0x35,
    "emerg": 0x40,
}
NO_KEY = 0x7F

# 01 04 flags from the radio
AUDIO_RX = 0x01                # radio is sending speaker audio
AUDIO_TX = 0x08                # radio wants mic audio (transmitting)
# 02 02 LED
LED_RED = 0x02                 # transmitting
LED_GREEN = 0x04               # busy / squelch open

# what the real mic sends after 05 01; "Icom Inc" and what looks like a serial number
IDENT = bytes.fromhex("010101ffffffff60cc49636f6d20496e630100" "90c712982b5a68")


def crc16(data):
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c


def _esc(body):
    out = bytearray()
    for b in body:
        if b >= 0xF0:
            out += bytes((0xFF, b & 0x0F))
        else:
            out.append(b)
    return bytes(out)


def _unesc(raw):
    out, i = bytearray(), 0
    while i < len(raw):
        if raw[i] == 0xFF and i + 1 < len(raw):
            out.append(0xF0 | raw[i + 1])
            i += 2
        else:
            out.append(raw[i])
            i += 1
    return bytes(out)


def frame(group, cmd, data=b"", start=0xF3):
    body = ADDR + bytes((group, cmd)) + struct.pack(">H", len(data)) + bytes(data)
    body += struct.pack(">H", crc16(body))
    return bytes((start,)) + _esc(body) + b"\xfd"


def ack(group, cmd):
    body = ADDR + bytes((0xEE, group, cmd, 0x01))
    body += struct.pack(">H", crc16(body))
    return b"\xf5" + _esc(body) + b"\xfd"


class Parser:
    """Feed TCP bytes, get (start, group, cmd, data) tuples. Acks come back as
    (0xf5, 0xee, (group, cmd), b"")."""

    def __init__(self):
        self.buf = b""

    def feed(self, data):
        self.buf += data
        out = []
        while b"\xfd" in self.buf:
            raw, _, self.buf = self.buf.partition(b"\xfd")
            if not raw or raw[0] not in (0xF3, 0xF5):
                log.debug("junk %s", raw.hex(" "))
                continue
            start, body = raw[0], _unesc(raw[1:])
            if len(body) < 4 or crc16(body[:-2]) != struct.unpack(">H", body[-2:])[0]:
                log.warning("bad frame %s", raw.hex(" "))
                continue
            if body[2] == 0xEE:
                out.append((start, 0xEE, (body[3], body[4]), b""))
                continue
            n = struct.unpack(">H", body[4:6])[0]
            out.append((start, body[2], body[3], body[6:6 + n]))
        return out


def display_text(data):
    """02 0a payload -> the 8 characters on the screen. A set top bit = that character followed
    by a decimal point ('BATT1' b4 '0V' -> 'BATT14.0V')."""
    s = ""
    for b in data[:8]:
        if not b:
            s += " "
            continue
        s += chr(b & 0x7F)
        if b & 0x80:
            s += "."
    return s.rstrip()


def display_flags(data):
    """Icons decoded from the bytes after the text. Only the ones seen in captures are named;
    `raw` keeps the rest (hex) for working out more."""
    b = bytes(data) + bytes(max(0, 62 - len(data)))
    return {
        "busy": bool(b[56] & 0x40),          # squelch open (the radio also lights the green LED)
        "shift": bool(b[58] & 0x80),
        "talkaround": bool(b[58] & 0x40),
        "scan": bool(b[58] & 0x20),
        "raw": bytes(data[8:]).rstrip(b"\0").hex(),
    }


class Mic:
    """Pretends to be the IP mic head. The radio connects to us.

    Callbacks (all optional, plain functions):
      on_display(text, flags)   screen changed (flags from display_flags)
      on_led(red, green)        LED changed (red = transmitting, green = busy)
      on_audio(pcm)             20 ms of speaker audio, int16 little-endian bytes at 8 kHz
      on_state(name)            'connected', 'disconnected', 'power_off', 'power_on'
      on_tx(bool)               radio started / stopped wanting mic audio
    """

    def __init__(self, listen="0.0.0.0", radio_ip=None, ident=IDENT, on_display=None,
                 on_led=None, on_audio=None, on_state=None, on_tx=None, radio_rtp_port=RTP_PORT):
        self.listen, self.radio_ip, self.ident = listen, radio_ip, ident
        self.radio_rtp_port = radio_rtp_port
        self.on_display, self.on_led, self.on_audio = on_display, on_led, on_audio
        self.on_state, self.on_tx = on_state, on_tx
        self.writer = None
        self.peer = None
        self.text = ""
        self.flags_shown = display_flags(b"")
        self.backlight = False
        self.led = 0
        self.flags = 0
        self.tx_wanted = asyncio.Event()
        self.connected = asyncio.Event()
        self._udp = None
        self._seq = random.randrange(65536)
        self._ts = random.randrange(1 << 32)
        self._ssrc = random.randrange(1 << 32)
        self.last_seen = 0.0

    # ---- lifecycle
    async def start(self):
        loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._client, self.listen, CTRL_PORT)
        self._udp, _ = await loop.create_datagram_endpoint(
            lambda: _Rtp(self), local_addr=(self.listen, RTP_PORT))
        log.info("listening on %s tcp %d / udp %d", self.listen, CTRL_PORT, RTP_PORT)

    def drop(self):
        """Close the control link; the radio will connect again."""
        if self.writer:
            self.writer.close()

    def close_now(self):
        self.drop()
        if getattr(self, "_server", None):
            self._server.close()
        if self._udp:
            self._udp.close()

    async def close(self):
        self.close_now()

    async def _client(self, reader, writer):
        peer = writer.get_extra_info("peername")[0]
        if self.radio_ip and peer != self.radio_ip:
            log.warning("refusing control connection from %s", peer)
            writer.close()
            return
        if self.writer:
            log.info("radio reconnected, dropping the old session")
            self.writer.close()
        self.writer, self.peer = writer, peer
        log.info("radio %s connected", peer)
        parser = Parser()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                self.last_seen = time.monotonic()
                for f in parser.feed(data):
                    await self._handle(*f)
        except (ConnectionError, OSError) as e:
            log.info("control link: %s", e)
        finally:
            if self.writer is writer:
                self.writer = None
                self.connected.clear()
                self._set_flags(0)
                self._emit(self.on_state, "disconnected")
            writer.close()

    def _send(self, raw):
        if self.writer:
            self.writer.write(raw)

    def _emit(self, cb, *a):
        if cb:
            try:
                cb(*a)
            except Exception:
                log.exception("callback")

    # ---- incoming
    async def _handle(self, start, group, cmd, data):
        if group == 0xEE:
            if cmd == (0x05, 0x04) and self.writer:      # our goodbye was acked
                self.writer.close()
            return
        if start == 0xF5:
            self._send(ack(group, cmd))
        if (group, cmd) == (0x05, 0x00):                 # keepalive
            self._send(frame(0x05, 0x00, b"\x6d\x01"))
        elif (group, cmd) == (0x05, 0x05):               # hello: our state
            self._send(frame(0x01, 0x0A, b"\x00") + frame(0x01, 0x00, b"\x00")
                       + frame(0x01, 0x01, bytes((NO_KEY,))) + frame(0x01, 0x01, b"\x1f")
                       + frame(0x05, 0x06, b"\x00"))
        elif (group, cmd) == (0x05, 0x01):               # who are you
            self._send(frame(0x05, 0x02, self.ident))
            self._rtp(bytes(FRAME * 2))                  # opens the audio path, as the mic does
            self.connected.set()
            self._emit(self.on_state, "connected")
        elif (group, cmd) == (0x05, 0x03):               # radio closing (01 power on, 02 off)
            self._emit(self.on_state, {1: "power_on", 2: "power_off"}.get(data[:1] and data[0],
                                                                          "closing"))
            self._send(frame(0x05, 0x04, b"\x01", start=0xF5))
        elif (group, cmd) == (0x02, 0x0A):
            self.text, self.flags_shown = display_text(data), display_flags(data)
        elif (group, cmd) == (0x02, 0x08):               # display commit
            self._send(frame(0x02, 0x09, b"\x01"))
            self._emit(self.on_display, self.text, self.flags_shown)
        elif (group, cmd) == (0x02, 0x0B):               # backlight
            self.backlight = bool(data and data[0])
            self._emit(self.on_led, bool(self.led & LED_RED), bool(self.led & LED_GREEN))
        elif (group, cmd) == (0x02, 0x02):
            self.led = data[0] if data else 0
            self._emit(self.on_led, bool(self.led & LED_RED), bool(self.led & LED_GREEN))
        elif (group, cmd) == (0x01, 0x04):
            self._set_flags(data[0] if data else 0)
        else:
            log.debug("radio %02x %02x %s", group, cmd, data.hex(" "))

    def _set_flags(self, f):
        was = bool(self.flags & AUDIO_TX)
        self.flags = f
        now = bool(f & AUDIO_TX)
        if now:
            self.tx_wanted.set()
        else:
            self.tx_wanted.clear()
        if now != was:
            self._emit(self.on_tx, now)

    # ---- outgoing: keys
    def key_down(self, name):
        code = KEYS[name] if isinstance(name, str) else name
        self._send(frame(0x01, 0x01, bytes((code,))))

    def key_up(self, name):
        code = KEYS[name] if isinstance(name, str) else name
        self._send(frame(0x01, 0x01, bytes((code | 0x80,))) + frame(0x01, 0x01, bytes((NO_KEY,))))

    async def key(self, name, hold=0.1):
        self.key_down(name)
        await asyncio.sleep(hold)
        self.key_up(name)

    def power_key(self, down):
        self._send(frame(0x01, 0x09, b"\x01" if down else b"\x00"))

    async def digits(self, s, gap=0.3):
        for ch in s:
            await self.key(ch)
            await asyncio.sleep(gap)

    async def power(self, hold=2.0):
        """The power key (01 09): about 2 s turns the radio off, a short press turns it on. The
        radio drops and remakes the session afterwards."""
        self.power_key(True)
        await asyncio.sleep(hold)
        self.power_key(False)

    def ptt(self, on):
        self._send(frame(0x01, 0x00, b"\x01" if on else b"\x00"))

    # ---- audio
    def send_audio(self, payload_be):
        """One 20 ms packet of int16 big-endian PCM to the radio (only heard while it transmits)."""
        self._rtp(payload_be)

    def _rtp(self, payload):
        if not (self._udp and self.peer):
            return
        hdr = struct.pack(">BBHII", 0x80, PT, self._seq, self._ts, self._ssrc)
        self._udp.sendto(hdr + payload, (self.peer, self.radio_rtp_port))
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts = (self._ts + FRAME) & 0xFFFFFFFF

    async def transmit(self, pcm_le, gain=1.0, wait=1.0):
        """Key up, send 8 kHz int16 little-endian PCM paced at 20 ms, unkey. Returns False if the
        radio never asked for audio (PTT refused, radio off)."""
        self.ptt(True)
        try:
            try:
                await asyncio.wait_for(self.tx_wanted.wait(), wait)
            except asyncio.TimeoutError:
                log.warning("radio didn't go into transmit")
                return False
            t = time.monotonic()
            for i in range(0, len(pcm_le), FRAME * 2):
                chunk = pcm_le[i:i + FRAME * 2].ljust(FRAME * 2, b"\0")
                s = struct.unpack(f"<{FRAME}h", chunk)
                if gain != 1.0:
                    s = [max(-32768, min(32767, int(v * gain))) for v in s]
                self._rtp(struct.pack(f">{FRAME}h", *s))
                t += FRAME / RATE
                await asyncio.sleep(max(0, t - time.monotonic()))
            return True
        finally:
            self.ptt(False)


class _Rtp(asyncio.DatagramProtocol):
    def __init__(self, mic):
        self.mic = mic

    def datagram_received(self, data, addr):
        m = self.mic
        if m.peer and addr[0] != m.peer:
            return
        if len(data) < 12 or data[0] >> 6 != 2:
            return
        cc = data[0] & 0x0F
        payload = data[12 + 4 * cc:]
        n = len(payload) // 2
        if n and m.on_audio:
            m._emit(m.on_audio, struct.pack(f"<{n}h", *struct.unpack(f">{n}h", payload[:n * 2])))
