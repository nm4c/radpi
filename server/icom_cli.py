"""Stand-alone test client for the Icom IP mic protocol: run it on the Pi in place of the mic.

    python -m server.icom_cli [--radio <radio address>] [--record rx.wav] [--gain 0.25] [-v]
    python -m server.icom_cli --mic <mic address> [--listen <address>] [-v]   (be the radio)

The radio connects to whatever address it is programmed with for the mic, so give the Pi that
address (or point the radio at the Pi) and unplug the real mic first. Then type:

    up / down / left / right / volup / voldn / side / p1..p4 / emerg / 0-9 * #   press a key
    hold <key> <seconds>      hold a key
    ch 012                    type a channel number (three digits)
    ptt <seconds>             key up and send a 1 kHz tone (quiet) for that long
    ptt <file.wav>            key up and send a WAV (mono 8 kHz 16-bit; others are rejected)
    power                     the power key
    status                    screen, LED, link
    quit

With --mic the Pi plays the radio for a real mic (unplug or repoint the real radio first). The
mic only talks to the radio address it is programmed with, so the Pi has to connect from that
address (--listen); the mic sends its audio there too. Keys
pressed on the mic show on its screen, and PTT works like a parrot: what you say is played back
through the mic's speaker when you let go. Commands:

    text <up to 8 chars>      put text on the screen
    led red|green|both|off    the LED
    light on|off              backlight
    bit <byte> <mask>         set icon bits in the screen payload (byte 8-67, hex mask), e.g. bit 56 40
    bits                      clear them (back to the default icons)
    tone [seconds]            1 kHz tone out of the mic's speaker
    raw <group> <cmd> [hex]   send any frame, e.g. raw 02 0e 04
    ipconf <radio ip> <mask> <gateway> <mic ip> <mask> <gateway>
                              give the mic new network settings, as the radio does when it's
                              programmed with a new mic address (06 00). Asks to confirm. Wrong
                              values leave the mic unreachable until its radio programs it again.
    bye                       tell the mic the radio is switching off
    status / quit
"""

import argparse
import asyncio
import logging
import math
import struct
import sys
import time
import wave

from .icom_ipmic import DEFAULT_ICONS, FRAME, KEYS, RATE, Mic, MicLink, ip_config, parse_ip_config


def tone(seconds, hz=1000, level=0.1):
    n = int(seconds * RATE)
    return struct.pack(f"<{n}h", *(int(level * 32767 * math.sin(2 * math.pi * hz * i / RATE))
                                   for i in range(n)))


def read_wav(path):
    with wave.open(path, "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, RATE):
            raise ValueError("needs mono, 16-bit, 8000 Hz")
        return w.readframes(w.getnframes())


def stdin_lines():
    loop = asyncio.get_running_loop()
    q = asyncio.Queue()
    loop.add_reader(sys.stdin, lambda: q.put_nowait(sys.stdin.readline()))
    return q


async def play(link, pcm_le, gain=1.0):
    """Paced 20 ms packets of int16 LE PCM to the mic's speaker."""
    link.audio_flags(rx=True)
    t = time.monotonic()
    for i in range(0, len(pcm_le), FRAME * 2):
        s = struct.unpack(f"<{FRAME}h", pcm_le[i:i + FRAME * 2].ljust(FRAME * 2, b"\0"))
        link.send_audio(struct.pack(f">{FRAME}h", *(max(-32768, min(32767, int(v * gain))) for v in s)))
        t += FRAME / RATE
        await asyncio.sleep(max(0, t - time.monotonic()))
    link.audio_flags()


async def radio_mode(a):
    t0 = time.monotonic()
    say = lambda *x: print(f"[{time.monotonic() - t0:7.2f}]", *x, flush=True)
    names = {v: k for k, v in KEYS.items()}
    icons = bytearray(DEFAULT_ICONS)
    st = {"text": "RADPI", "rec": bytearray(), "ptt": False, "play": None, "ipconf": None}

    def show(text=None):
        if text is not None:
            st["text"] = text
        link.display(st["text"], bytes(icons))

    def on_state(s):
        say("mic", s, link.ident.hex() if s == "connected" and link.ident else "")
        if s == "connected":
            link.backlight(True)
            show("RADPI")

    def on_key(code, down):
        name = names.get(code, f"{code:#04x}")
        say("key", name, "down" if down else "up")
        if down:
            show(name.upper()[:8])

    def on_ptt(on):
        say("PTT", "down" if on else "up")
        st["ptt"] = on
        if on:
            st["rec"] = bytearray()
            link.led(red=True)
            link.audio_flags(tx=True)
            show("PARROT")
        else:
            link.audio_flags()
            link.led()
            pcm = bytes(st["rec"])
            n = len(pcm) // 2
            if n:
                peak = max(abs(v) for v in struct.unpack(f"<{n}h", pcm))
                say(f"got {n / RATE:.1f} s of mic audio, peak {peak}")
                show("PLAYBACK")
                st["play"] = asyncio.create_task(play(link, pcm, a.gain_back))
            else:
                say("no mic audio arrived (check where the mic sends RTP)")
                show("NO AUDIO")

    def on_audio(pcm):
        if st["ptt"]:
            st["rec"] += pcm

    link = MicLink(a.mic, local=a.listen, on_key=on_key, on_ptt=on_ptt, on_audio=on_audio, on_state=on_state,
                   on_power=lambda on: say("power key", "down" if on else "up"),
                   on_frame=(lambda s, g, c, d: say(f"mic {s:02x} {g:02x} {c:02x} {d.hex(' ')}"))
                   if a.v else None)
    await link.start()
    say(f"connecting to the mic at {a.mic}...")
    lines = stdin_lines()
    try:
        while True:
            line = await lines.get()
            if not line:
                break
            w = line.split()
            if not w:
                continue
            c = w[0].lower()
            try:
                if c in ("quit", "exit"):
                    break
                elif c == "status":
                    say(f"link={'up' if link.connected.is_set() else 'down'} ident="
                        f"{link.ident.hex() if link.ident else None} audio_from={link.rtp_from}")
                elif c == "text":
                    show(line.split(None, 1)[1].rstrip("\n") if len(w) > 1 else "")
                elif c == "led":
                    link.led(red=w[1] in ("red", "both"), green=w[1] in ("green", "both"))
                elif c == "light":
                    link.backlight(w[1] == "on")
                elif c == "bit":
                    i = int(w[1])
                    if not 8 <= i < 68:
                        raise ValueError("byte 8-67")
                    icons[i - 8] |= int(w[2], 16)
                    show()
                    say("icons", bytes(icons).hex())
                elif c == "bits":
                    icons[:] = DEFAULT_ICONS
                    show()
                elif c == "tone":
                    await play(link, tone(float(w[1]) if len(w) > 1 else 2))
                elif c == "raw":
                    link.send_frame(int(w[1], 16), int(w[2], 16), bytes.fromhex("".join(w[3:])))
                elif c == "ipconf":
                    payload = ip_config(*w[1:7])
                    st["ipconf"] = payload
                    say("about to send the mic (06 00):")
                    for k, v in parse_ip_config(payload).items():
                        say(f"   {k:10} {v}")
                    say("type yes to send it, anything else to cancel")
                elif c == "yes" and st["ipconf"]:
                    link.send_frame(0x06, 0x00, st["ipconf"])
                    st["ipconf"] = None
                    say("sent: watch for the mic's answer (run with -v to see every frame)")
                elif st["ipconf"]:
                    st["ipconf"] = None
                    say("ipconf cancelled")
                elif c == "bye":
                    link.goodbye()
                else:
                    say("?", c)
            except (IndexError, KeyError, ValueError, OSError) as e:
                say("error:", e)
    finally:
        await link.close()


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mic", help="be the radio and connect to the real mic at this address")
    ap.add_argument("--gain-back", type=float, default=4.0,
                    help="--mic: gain for playing your voice back (the mic is quiet)")
    ap.add_argument("--listen", default="0.0.0.0",
                    help="address to listen on (with --mic: the address to connect from)")
    ap.add_argument("--radio", help="only accept the radio from this address")
    ap.add_argument("--record", help="write speaker audio to this WAV")
    ap.add_argument("--gain", type=float, default=0.25,
                    help="transmit gain for WAVs (the real mic peaks around -18 dBFS)")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO,
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    if a.mic:
        return await radio_mode(a)

    rec = None
    if a.record:
        rec = wave.open(a.record, "wb")
        rec.setnchannels(1)
        rec.setsampwidth(2)
        rec.setframerate(RATE)
    t0 = time.monotonic()
    say = lambda *x: print(f"[{time.monotonic() - t0:7.2f}]", *x, flush=True)

    mic = Mic(a.listen, a.radio,
              on_display=lambda text, f: say(f"screen {text!r}", " ".join(k for k, v in f.items() if v is True)),
              on_led=lambda r, g: say("LED", "RED" if r else "", "GREEN" if g else "",
                                      "" if r or g else "off"),
              on_state=lambda s: say("link", s),
              on_tx=lambda on: say("radio", "TRANSMITTING (wants mic audio)" if on else "tx ended"),
              on_audio=(lambda pcm: rec.writeframes(pcm)) if rec else None)
    await mic.start()
    say("waiting for the radio to connect...")

    lines = stdin_lines()
    try:
        while True:
            line = await lines.get()
            if not line:
                break
            w = line.split()
            if not w:
                continue
            c = w[0].lower()
            try:
                if c in ("quit", "exit"):
                    break
                if c == "status":
                    say(f"link={'up' if mic.connected.is_set() else 'down'} peer={mic.peer} "
                        f"screen={mic.text!r} led={mic.led:#x} flags={mic.flags:#x}")
                elif c in KEYS:
                    await mic.key(c)
                elif c == "hold":
                    await mic.key(w[1].lower(), float(w[2]))
                elif c == "ch":
                    await mic.digits(w[1])
                elif c == "power":
                    await mic.power()
                elif c == "ptt":
                    arg = w[1] if len(w) > 1 else "3"
                    if arg.replace(".", "", 1).isdigit():
                        pcm, gain = tone(float(arg)), 1.0
                    else:
                        pcm, gain = read_wav(arg), a.gain
                    say("PTT", "sent" if await mic.transmit(pcm, gain) else "REFUSED")
                else:
                    say("?", c)
            except (IndexError, KeyError, ValueError, OSError) as e:
                say("error:", e)
    finally:
        await mic.close()
        if rec:
            rec.close()


if __name__ == "__main__":
    asyncio.run(main())
