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
import socket
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

DISPLAY_LEN = 68               # 02 0a payload: 8 characters + icon bytes
DEFAULT_ICONS = bytes(48) + b"\x80"   # payload byte 56 = 0x80 in nearly every captured screen

# what the real mic sends after 05 01: "Icom Inc", then its MAC address (00:90:c7 is Icom's)
# and two bytes not yet known. The radio doesn't check the MAC; this one is made up.
IDENT = bytes.fromhex("010101ffffffff60cc49636f6d20496e6301" "0090c7000001" "5a68")


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


def display_bytes(text, flags=b""):
    """The other way round: text (up to 8 characters, a '.' folds into the one before it) and
    the icon bytes after it -> a 68-byte 02 0a payload."""
    out = bytearray()
    for ch in str(text):
        if ch == "." and out and not out[-1] & 0x80:
            out[-1] |= 0x80
            continue
        if len(out) == 8:
            break
        c = ord(ch)
        out.append(c if 0x20 <= c < 0x7F else 0x3F)
    out = bytes(out).ljust(8, b"\0") + bytes(flags)[:DISPLAY_LEN - 8]
    return out.ljust(DISPLAY_LEN, b"\0")


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


class RtpPort:
    """One UDP socket on the RTP port, shared by everything here that uses it: the mic emulator
    (audio from the radio) and MicLink (audio from a real mic) both need port 50000 when RadPi sits
    between the two. Each packet goes to the handler registered for its sender (ip, port), else
    for its sender's ip, else to the default handler."""

    _open = {}

    def __init__(self, key):
        self.key = key
        self.handlers = {}
        self.transport = None
        self.users = 0
        self.ready = asyncio.Event()

    @classmethod
    async def get(cls, local, port):
        key = (local, port)
        hub = cls._open.get(key)
        if hub is None:
            hub = cls._open[key] = cls(key)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(key)
                hub.transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda: _HubProto(hub), sock=sock)
            except BaseException:
                sock.close()
                del cls._open[key]
                hub.ready.set()
                raise
            hub.ready.set()
        else:
            await hub.ready.wait()
            if hub.transport is None:
                raise OSError(f"udp {local}:{port} could not be opened")
        hub.users += 1
        return hub

    def add(self, cb, src=None):
        self.handlers[src] = cb

    def remove(self, src=None):
        self.handlers.pop(src, None)

    def sendto(self, data, addr):
        if self.transport:
            self.transport.sendto(data, addr)

    def release(self):
        self.users -= 1
        if self.users <= 0 and self.transport:
            self.transport.close()
            self.transport = None
            if self._open.get(self.key) is self:
                del self._open[self.key]

    def dispatch(self, data, addr):
        cb = self.handlers.get(addr[:2]) or self.handlers.get(addr[0]) or self.handlers.get(None)
        if cb:
            cb(data, addr)


class _HubProto(asyncio.DatagramProtocol):
    def __init__(self, hub):
        self.hub = hub

    def datagram_received(self, data, addr):
        self.hub.dispatch(data, addr)


def rtp_payload(data):
    """An RTP packet's audio as int16 little-endian, or None if it isn't one."""
    if len(data) < 12 or data[0] >> 6 != 2:
        return None
    payload = data[12 + 4 * (data[0] & 0x0F):]
    n = len(payload) // 2
    if not n:
        return None
    return struct.pack(f"<{n}h", *struct.unpack(f">{n}h", payload[:n * 2]))


class Mic:
    """Pretends to be the IP mic head. The radio connects to us.

    Callbacks (all optional, plain functions):
      on_display(text, flags)   screen changed (flags from display_flags)
      on_led(red, green)        LED changed (red = transmitting, green = busy)
      on_audio(pcm)             20 ms of speaker audio, int16 little-endian bytes at 8 kHz
      on_state(name)            'connected', 'disconnected', 'power_off', 'power_on'
      on_tx(bool)               radio started / stopped wanting mic audio
      on_frame(start, group, cmd, data)   every frame from the radio, after the above
    """

    def __init__(self, listen="0.0.0.0", radio_ip=None, ident=IDENT, on_display=None,
                 on_led=None, on_audio=None, on_state=None, on_tx=None, radio_rtp_port=RTP_PORT,
                 ctrl_port=CTRL_PORT, rtp_port=RTP_PORT, on_frame=None):
        self.listen, self.radio_ip, self.ident = listen, radio_ip, ident
        self.radio_rtp_port, self.ctrl_port, self.rtp_port = radio_rtp_port, ctrl_port, rtp_port
        self.on_frame = on_frame
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
        # reuse_port: a MicLink may connect out from this same port (RadPi between radio and mic)
        self._server = await asyncio.start_server(self._client, self.listen, self.ctrl_port,
                                                  reuse_port=hasattr(socket, "SO_REUSEPORT"))
        self._udp = await RtpPort.get(self.listen, self.rtp_port)
        self._udp.add(self._on_rtp)
        log.info("listening on %s tcp %d / udp %d", self.listen, self.ctrl_port, self.rtp_port)

    def drop(self):
        """Close the control link; the radio will connect again."""
        if self.writer:
            self.writer.close()

    def close_now(self):
        self.drop()
        if getattr(self, "_server", None):
            self._server.close()
        if self._udp:
            self._udp.remove()
            self._udp.release()
            self._udp = None

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
        self._emit(self.on_frame, start, group, cmd, bytes(data))

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

    def _on_rtp(self, data, addr):
        if self.peer and addr[0] != self.peer:
            return
        pcm = rtp_payload(data)
        if pcm and self.on_audio:
            self._emit(self.on_audio, pcm)

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




# What the radio sends a mic after the handshake when it is switched on (captured; the meaning of
# 06 09 / 06 0a / 02 0d is not known, they are replayed as they are).
STARTUP = [
    (0x02, 0x06, "01000000"), (0x02, 0x0B, "00"), (0x01, 0x04, "00000000"), (0x01, 0x08, "00"),
    (0x02, 0x02, "00"), (0x02, 0x01, "ff"), (0x01, 0x08, "01"), (0x02, 0x0B, "02"),
    (0x06, 0x09, "00" * 32), (0x06, 0x0A, "7c706050423b342d2620"), (0x02, 0x0D, "000401"),
    (0x02, 0x0E, "04"), (0x02, 0x0E, "05"),
]


class MicLink:
    """Plays the radio's part for a real IP mic: connects out to it, does the handshake and
    keepalives, and lets the caller drive the screen, LED and speaker.

    Callbacks (optional, plain functions):
      on_key(code, down)     a key (KEYS code; down False = released)
      on_ptt(bool)           PTT bar
      on_power(bool)         power key down / up
      on_audio(pcm)          mic audio, int16 little-endian at 8 kHz (only sent while asked for)
      on_state(name)         'connected' / 'disconnected'
      on_frame(start, group, cmd, data)   every frame from the mic (after the above)
    """

    KEEPALIVE_S = 2.0
    RETRY_S = 3.0

    def __init__(self, mic_ip, on_key=None, on_ptt=None, on_power=None, on_audio=None,
                 on_state=None, on_frame=None, startup=True, local="0.0.0.0", rtp_port=RTP_PORT,
                 src_port=CTRL_PORT, mic_port=CTRL_PORT, mic_rtp_port=RTP_PORT):
        self.mic_ip, self.local, self.startup, self.rtp_port = mic_ip, local, startup, rtp_port
        self.src_port, self.mic_port, self.mic_rtp_port = src_port, mic_port, mic_rtp_port
        self.ptt = False
        self.on_key, self.on_ptt, self.on_power = on_key, on_ptt, on_power
        self.on_audio, self.on_state, self.on_frame = on_audio, on_state, on_frame
        self.writer = None
        self.connected = asyncio.Event()
        self.ident = None
        self.held = None
        self.rtp_from = None           # where the mic's audio actually came from (diagnostics)
        self._udp = None
        self._task = None
        self._seq = random.randrange(65536)
        self._ts = random.randrange(1 << 32)
        self._ssrc = random.randrange(1 << 32)
        self._closing = False

    async def start(self):
        self._udp = await RtpPort.get(self.local, self.rtp_port)
        self._udp.add(self._on_rtp, (self.mic_ip, self.mic_rtp_port))
        self._task = asyncio.create_task(self._run())

    async def close(self):
        self.close_now()

    def close_now(self):
        self._closing = True
        if self._task:
            self._task.cancel()
        if self.writer:
            self.writer.close()
        if self._udp:
            self._udp.remove((self.mic_ip, self.mic_rtp_port))
            self._udp.release()
            self._udp = None

    async def _run(self):
        while not self._closing:
            try:
                reader, writer = await asyncio.wait_for(self._connect(), 5)
            except (OSError, asyncio.TimeoutError) as e:
                log.info("mic %s: %s", self.mic_ip, e or "no answer")
                await asyncio.sleep(self.RETRY_S)
                continue
            log.info("connected to mic %s", self.mic_ip)
            self.writer = writer
            ka = asyncio.create_task(self._keepalive())
            parser = Parser()
            try:
                self._send(frame(0x05, 0x05, b"\x01"))
                while True:
                    data = await reader.read(4096)
                    if not data:
                        log.info("mic closed the connection%s",
                                 "" if self.connected.is_set() else " before the handshake finished")
                        break
                    log.debug("mic raw %s", data.hex(" "))
                    for f in parser.feed(data):
                        self._handle(*f)
            except (ConnectionError, OSError) as e:
                log.info("mic link: %s", e)
            finally:
                ka.cancel()
                self.writer = None
                writer.close()
                if self.connected.is_set():
                    self.connected.clear()
                    self._emit(self.on_state, "disconnected")
                if self.held is not None:
                    self._emit(self.on_key, self.held, False)
                    self.held = None
                if self.ptt:
                    self.ptt = False
                    self._emit(self.on_ptt, False)
            await asyncio.sleep(self.RETRY_S)

    async def _connect(self):
        """The radio always connects from port 52001 to 52001, and the mic ignores anything
        else, so bind the source port too."""
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind((self.local, self.src_port))
            sock.setblocking(False)
            await loop.sock_connect(sock, (self.mic_ip, self.mic_port))
        except BaseException:
            sock.close()
            raise
        return await asyncio.open_connection(sock=sock)

    async def _keepalive(self):
        while True:
            await asyncio.sleep(self.KEEPALIVE_S)
            self._send(frame(0x05, 0x00, b"\x4d\x00"))

    def _send(self, raw):
        if self.writer:
            self.writer.write(raw)

    def send_frame(self, group, cmd, data=b"", start=0xF3):
        self._send(frame(group, cmd, data, start))

    def _emit(self, cb, *a):
        if cb:
            try:
                cb(*a)
            except Exception:
                log.exception("callback")

    def _handle(self, start, group, cmd, data):
        if group == 0xEE:
            if cmd == (0x05, 0x03) and self.writer:      # mic acked our goodbye
                pass
            return
        if start == 0xF5:
            self._send(ack(group, cmd))
        gc = (group, cmd)
        if gc == (0x05, 0x06):                           # end of the mic's hello
            self._send(frame(0x05, 0x01, b"\x01"))
        elif gc == (0x05, 0x02):                         # its ID
            self.ident = bytes(data)
            if self.startup:
                self._send(b"".join(frame(g, c, bytes.fromhex(d)) for g, c, d in STARTUP))
            self.connected.set()
            self._emit(self.on_state, "connected")
        elif gc == (0x05, 0x04):                         # mic's goodbye
            if self.writer:
                self.writer.close()
        elif gc == (0x01, 0x01) and data:
            k = data[0]
            if k == NO_KEY:
                pass
            elif k & 0x80:
                self.held = None
                self._emit(self.on_key, k & 0x7F, False)
            elif k != 0x1F:                              # 1f only appears in the hello
                self.held = k
                self._emit(self.on_key, k, True)
        elif gc == (0x01, 0x00) and data:
            on = bool(data[0])
            if on != self.ptt:
                self.ptt = on
                self._emit(self.on_ptt, on)
        elif gc == (0x01, 0x09) and data:
            self._emit(self.on_power, bool(data[0]))
        self._emit(self.on_frame, start, group, cmd, bytes(data))

    # ---- screen, LED, audio routing
    def display(self, text, flags=DEFAULT_ICONS):
        self._send(frame(0x02, 0x07, b"\x02") + frame(0x02, 0x0A, display_bytes(text, flags))
                   + frame(0x02, 0x08, b"\x00\x44"))

    def led(self, red=False, green=False):
        self._send(frame(0x02, 0x02, bytes(((LED_RED if red else 0) | (LED_GREEN if green else 0),))))

    def backlight(self, on):
        self._send(frame(0x02, 0x0B, b"\x02" if on else b"\x00"))

    def audio_flags(self, rx=False, tx=False):
        f = (AUDIO_RX if rx else 0) | (AUDIO_TX if tx else 0)
        self._send(frame(0x01, 0x04, bytes((f, 0, 0, 0))))

    def goodbye(self, power_off=True):
        self._send(frame(0x05, 0x03, b"\x02" if power_off else b"\x01", start=0xF5))

    def send_audio(self, payload_be):
        """One 20 ms packet of int16 big-endian PCM to the mic's speaker."""
        if not (self._udp and self.connected.is_set()):
            return
        hdr = struct.pack(">BBHII", 0x80, PT, self._seq, self._ts, self._ssrc)
        self._udp.sendto(hdr + payload_be, (self.mic_ip, self.mic_rtp_port))
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts = (self._ts + FRAME) & 0xFFFFFFFF

    def _on_rtp(self, data, addr):
        if self.rtp_from != addr:
            self.rtp_from = addr
            log.info("mic audio from %s:%d", *addr[:2])
        pcm = rtp_payload(data)
        if pcm and self.on_audio:
            self._emit(self.on_audio, pcm)
