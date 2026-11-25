"""WebRTC audio for an operator: Opus over UDP, negotiated over the existing WebSocket.

The WebSocket still carries login, PTT, channels and meters. Only audio moves here:
  radio RX -> RxTrack (one per operator)  -> browser
  browser mic -> remote track -> TX buffer -> radio  (only while that operator holds PTT)

Latency choices:
  - RX frames are queued at most `MAX_QUEUE` deep; older ones are dropped rather than delayed.
  - aiortc's audio jitter buffer holds 4 packets (80 ms) before releasing one and stalls for up to
    16 packets on a loss. It's replaced with a small one (webrtc.rx_jitter_packets): ~20 ms normally,
    bounded on loss.
  - Signalling is non-trickle: each side sends a complete SDP once ICE gathering is done. On a LAN
    or tailnet that takes a few ms.
"""

import asyncio
import fractions
import logging
import socket

import numpy as np

log = logging.getLogger("rtc")

try:
    import av
    from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
    from aiortc import rtcrtpreceiver
    from aiortc.jitterbuffer import JitterBuffer
    from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

    AVAILABLE = True
except ImportError:  # aiortc not installed: operators fall back to WebSocket audio
    AVAILABLE = False

MAX_QUEUE = 3  # RX frames (60 ms) waiting for the encoder before old ones are dropped


def setup(cfg):
    """Apply global aiortc tweaks once. Returns False if WebRTC can't be used."""
    if not AVAILABLE or not cfg["webrtc"]["enabled"]:
        return False
    packets = int(cfg["webrtc"]["rx_jitter_packets"])
    if packets & (packets - 1) or not 2 <= packets <= 16:
        raise SystemExit("config: webrtc.rx_jitter_packets must be 2, 4, 8 or 16")

    def jitter_buffer(capacity, prefetch=0, is_video=False):
        if is_video:
            return JitterBuffer(capacity, prefetch, is_video)
        return JitterBuffer(capacity=packets, prefetch=1)

    rtcrtpreceiver.JitterBuffer = jitter_buffer
    ports = parse_ports(cfg["webrtc"].get("udp_ports"))
    if ports:
        _limit_ice_ports(ports)
        log.info("WebRTC audio on UDP %d-%d", ports[0], ports[-1])
    return True


def parse_ports(spec):
    """webrtc.udp_ports: "50100-50149", [50100, 50149] or null -> a range, or None."""
    if spec in (None, "", []):
        return None
    try:
        lo, hi = (spec if isinstance(spec, (list, tuple)) else str(spec).replace(":", "-").split("-"))
        lo, hi = int(lo), int(hi)
    except (TypeError, ValueError):
        raise SystemExit(f"config: webrtc.udp_ports: {spec!r}: give a range, e.g. 50100-50149") from None
    if not 1024 <= lo <= hi <= 65535:
        raise SystemExit("config: webrtc.udp_ports must be within 1024-65535, low to high")
    return range(lo, hi + 1)


def _limit_ice_ports(ports):
    """Make aiortc's ICE (aioice) open its UDP sockets on ports from this range only, so a
    firewall can forward or allow exactly those. aioice always asks for "any free port" (port 0):
    within aioice only, that becomes the first free port of the range."""
    import aioice.ice

    class LoopProxy:
        def __init__(self, loop):
            self._loop = loop

        def __getattr__(self, name):
            return getattr(self._loop, name)

        async def create_datagram_endpoint(self, factory, local_addr=None, **kw):
            if not local_addr or local_addr[1] != 0:
                return await self._loop.create_datagram_endpoint(factory, local_addr=local_addr, **kw)
            last = None
            for port in ports:
                try:
                    return await self._loop.create_datagram_endpoint(factory, local_addr=(local_addr[0], port), **kw)
                except OSError as e:
                    last = e
            log.warning("WebRTC: no free UDP port left in %d-%d (raise webrtc.udp_ports)", ports[0], ports[-1])
            raise last or OSError("no free port")

    class AsyncioShim:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        def get_running_loop(self):
            return LoopProxy(asyncio.get_running_loop())

    aioice.ice.asyncio = AsyncioShim()


async def add_public_candidates(sdp, public):
    """webrtc.public_address (behind a port forward): next to each IPv4 host candidate, the same
    port at the public address, so browsers outside reach the Pi through the forward while ones on
    the local network still use the private address. public: an address or a DNS name."""
    if not public:
        return sdp
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(public, None, family=socket.AF_INET)
        addr = infos[0][4][0]
    except (OSError, IndexError) as e:
        log.warning("webrtc.public_address %s: %s", public, e)
        return sdp
    from aioice.candidate import candidate_priority
    out, added = [], set()
    for line in sdp.split("\r\n"):
        out.append(line)
        if not line.startswith("a=candidate:"):
            continue
        f = line[len("a=candidate:"):].split()
        # foundation component transport priority address port typ host ...
        if len(f) < 8 or f[2].lower() != "udp" or f[7] != "host" or ":" in f[4] or f[4] == addr:
            continue
        if (f[1], f[5]) in added:      # same port on several interfaces: one public candidate
            continue
        added.add((f[1], f[5]))
        comp = int(f[1])
        out.append(f"a=candidate:{f[0]}p {comp} udp {candidate_priority(comp, 'srflx')} {addr} {f[5]} "
                   f"typ srflx raddr {f[4]} rport {f[5]}")
    return "\r\n".join(out)


if AVAILABLE:

    class RxTrack(MediaStreamTrack):
        """Radio receive audio for one operator. Paced by the sound card: one frame per frame_ms."""

        kind = "audio"

        def __init__(self, rate):
            super().__init__()
            self.rate = rate
            self.time_base = fractions.Fraction(1, rate)
            self.pts = 0
            self.queue = asyncio.Queue()

        def feed(self, pcm):
            while self.queue.qsize() >= MAX_QUEUE:
                self.queue.get_nowait()  # behind: drop the oldest frame, keep latency down
            self.queue.put_nowait(pcm)

        async def recv(self):
            if self.readyState != "live":
                raise MediaStreamError
            pcm = await self.queue.get()
            samples = np.frombuffer(pcm, "<i2").reshape(1, -1)
            frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
            frame.sample_rate = self.rate
            frame.time_base = self.time_base
            frame.pts = self.pts
            self.pts += samples.shape[1]
            return frame


class RtcPeer:
    """One operator's WebRTC connection."""

    def __init__(self, cfg, on_tx_audio, on_state):
        w = cfg["webrtc"]
        servers = [RTCIceServer(**s) if isinstance(s, dict) else RTCIceServer(urls=s) for s in w["ice_servers"]]
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
        self.public = w.get("public_address")
        self.rate = int(cfg["audio"]["wire_rate"])
        self.rx = RxTrack(self.rate)
        self.pc.addTrack(self.rx)
        self.ready = False
        self._on_tx_audio = on_tx_audio
        self._on_state = on_state
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=self.rate)
        self._tasks = set()

        @self.pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                t = asyncio.create_task(self._consume(track))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)

        @self.pc.on("connectionstatechange")
        async def on_state():
            state = self.pc.connectionState
            log.info("WebRTC %s", state)
            self.ready = state == "connected"
            self._on_state(state)

    async def answer(self, sdp, kind="offer"):
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=kind))
        await self.pc.setLocalDescription(await self.pc.createAnswer())  # waits for ICE gathering
        return await add_public_candidates(self.pc.localDescription.sdp, self.public)

    def feed(self, pcm):
        if self.ready:
            self.rx.feed(pcm)

    async def _consume(self, track):
        """Browser mic: decode (aiortc gives 48 kHz stereo), convert to the wire format, hand on."""
        try:
            while True:
                frame = await track.recv()
                for out in self._resampler.resample(frame):
                    self._on_tx_audio(out.to_ndarray().astype("<i2").tobytes())
        except MediaStreamError:
            pass

    async def close(self):
        self.ready = False
        self.rx.stop()
        await self.pc.close()
