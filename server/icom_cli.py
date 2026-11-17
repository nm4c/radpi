"""Stand-alone test client for the Icom IP mic protocol: run it on the Pi in place of the mic.

    python -m server.icom_cli [--radio <radio address>] [--record rx.wav] [--gain 0.25] [-v]

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
"""

import argparse
import asyncio
import logging
import math
import struct
import sys
import time
import wave

from .icom_ipmic import KEYS, RATE, Mic


def tone(seconds, hz=1000, level=0.1):
    n = int(seconds * RATE)
    return struct.pack(f"<{n}h", *(int(level * 32767 * math.sin(2 * math.pi * hz * i / RATE))
                                   for i in range(n)))


def read_wav(path):
    with wave.open(path, "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, RATE):
            raise ValueError("needs mono, 16-bit, 8000 Hz")
        return w.readframes(w.getnframes())


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--radio", help="only accept the radio from this address")
    ap.add_argument("--record", help="write speaker audio to this WAV")
    ap.add_argument("--gain", type=float, default=0.25,
                    help="transmit gain for WAVs (the real mic peaks around -18 dBFS)")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO,
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")

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

    loop = asyncio.get_running_loop()
    lines = asyncio.Queue()
    loop.add_reader(sys.stdin, lambda: lines.put_nowait(sys.stdin.readline()))
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
