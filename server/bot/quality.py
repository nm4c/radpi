"""How clean an over sounds: hiss and crackle, judged from the audio alone.

Through a repeater the signal strength is always the repeater's, and good, so it says nothing
about the station behind it. What does show is the audio: a station going into the repeater
weakly comes out with hiss under (and between) its words, and a mobile on the fringe or a dodgy
mic lead adds crackle (clicks and pops).

  snr_db   speech level against the hiss floor, from 20 ms frames: the loudest third (speech)
           against the quietest tenth (the gaps between words). Frames below SILENCE_DB are a
           closed squelch (the pre-roll before the over), not hiss, and are left out.
  crackle  share of frames with a click in them: a spike a few samples wide that a 5-sample
           running median takes out, standing far above that frame's level (speech is smooth at
           that scale, so it passes the median almost untouched).
  grade    clean, rough or poor (bot.quality sets the lines).

A poor over still gets transcribed and identified, but it isn't learned from (no new person, no
voiceprint sample), its loudness isn't read as shouting (hiss reads as level), Whisper's
"probably not speech" filter is relaxed for it, and the LLM is told the audio was scratchy so it
doesn't take mangled words literally.
"""

import numpy as np

FRAME_S = 0.02
SILENCE_DB = -70     # a closed squelch, not a noise floor
CLICK = 6.0          # a click: a spike this many times the frame's RMS level
CLICK_MIN = 0.05     # and at least this big (full scale = 1)
TARGET_DB = -20      # level_up brings speech to about this
MAX_GAIN_DB = 20


def _frames(samples, rate):
    f = max(1, int(FRAME_S * rate))
    n = len(samples) // f
    return samples[:n * f].reshape(n, f) if n else np.zeros((0, f), np.float32)


def measure(samples, rate, cfg=None):
    """{"snr_db", "crackle", "grade"}; None if the over is too short or silent to judge."""
    cfg = cfg or {}
    fr = _frames(np.asarray(samples, np.float32), rate)
    if len(fr) < 25:
        return None
    rms = np.sqrt(np.mean(np.square(fr), axis=1))
    db = 20 * np.log10(rms + 1e-9)
    live = db > SILENCE_DB
    if live.sum() < 25:
        return None
    lv = np.sort(db[live])
    floor = float(np.mean(lv[:max(1, len(lv) // 10)]))
    speech = float(np.mean(lv[-max(1, len(lv) // 3):]))
    snr = speech - floor

    x = fr.reshape(-1)
    pad = np.pad(x, 2, mode="edge")
    smooth = np.median(np.stack([pad[i:i + len(x)] for i in range(5)]), axis=0)
    spike = np.abs(x - smooth).reshape(fr.shape).max(axis=1)
    clicks = (spike > CLICK * rms) & (spike > CLICK_MIN) & live
    crackle = float(clicks.sum() / live.sum())

    rough_snr, poor_snr = float(cfg.get("rough_snr_db", 25)), float(cfg.get("poor_snr_db", 12))
    rough_cr, poor_cr = float(cfg.get("rough_crackle", 0.05)), float(cfg.get("poor_crackle", 0.2))
    grade = ("poor" if snr < poor_snr or crackle > poor_cr else
             "rough" if snr < rough_snr or crackle > rough_cr else "clean")
    return {"snr_db": round(snr, 1), "crackle": round(crackle, 3), "grade": grade}


def describe(q):
    """A few words for the LLM; None for a clean over."""
    if not q or q["grade"] == "clean":
        return None
    what = []
    if q["snr_db"] < 25:
        what.append("hissy")
    if q["crackle"] > 0.05:
        what.append("crackly")
    sound = " and ".join(what) or "scratchy"
    if q["grade"] == "poor":
        return f"very {sound}, barely readable: the words may be misheard"
    return f"a bit {sound}: some words may be misheard"


def level_up(samples, rate):
    """Quiet stations brought up to a normal speech level for Whisper (at most +MAX_GAIN_DB,
    never clipping). Loud ones are left alone."""
    x = np.asarray(samples, np.float32)
    fr = _frames(x, rate)
    if not len(fr):
        return x
    rms = np.sqrt(np.mean(np.square(fr), axis=1))
    loud = np.sort(rms)[-max(1, len(rms) // 3):]
    level = 20 * np.log10(float(np.mean(loud)) + 1e-9)
    peak = float(np.max(np.abs(x))) or 1.0
    gain_db = min(MAX_GAIN_DB, TARGET_DB - level, 20 * np.log10(0.95 / peak))
    if gain_db <= 0.5:
        return x
    return (x * 10 ** (gain_db / 20)).astype(np.float32)
