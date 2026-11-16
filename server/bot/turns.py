"""Splitting an over where the voice changes, and spotting overs with two voices at once.

On CB the next station often keys up the instant the last one lets go, or before, so the busy
line never drops for long enough to end the over: two or three people arrive as one recording.
Someone also regularly transmits over the top of someone else (doubling). Either way, treating
the recording as one person's voice gives an average of several voices. That identifies nobody
and, if it is learned, spreads one person's voiceprint over everyone.

So each over long enough to tell (2 x WIN_S) is looked at in short overlapping windows. A voice
change is two windows in a row that are unlike the voice so far (similarity below
speaker.split); one odd window is just noise. The cut goes at the quietest moment between the
last window of the old voice and the first of the new one, which is usually the changeover gap.
Parts too short to tell anything from are folded into a neighbour, and neighbouring parts whose
voices turn out alike after all are joined back up.

A part whose windows keep disagreeing with each other (more than MIXED_SHARE of them) has more
than one voice right through it: doubling. It is still transcribed and labelled with the closest
voice, but never learned from.
"""

import numpy as np

WIN_S = 1.5          # window length: long enough for a voice embedding to mean something
HOP_S = 0.75         # step between windows (longer overs use a longer step, see MAX_WINDOWS)
MAX_WINDOWS = 40     # windows per over at most, to bound the work on long overs
MIN_PART_S = 1.2     # parts shorter than this are folded into a neighbour
QUIET_DB = 30        # windows this far below the over's loudest are pauses or squelch noise: skipped
MIXED_SHARE = 0.3    # a part with more than this share of windows unlike the rest is "mixed"
FRAME_S = 0.02       # resolution of the cut


def _unit(v):
    n = float(np.linalg.norm(v))
    return v / n if n else v


def _db(x):
    return 20 * np.log10(float(np.sqrt(np.mean(np.square(x)))) + 1e-9)


def _quietest(samples, lo, hi, rate):
    """Index of the start of the quietest FRAME_S frame in samples[lo:hi]."""
    f = max(1, int(FRAME_S * rate))
    if hi - lo < 2 * f:
        return (lo + hi) // 2
    n = (hi - lo) // f
    frames = samples[lo:lo + n * f].reshape(n, f)
    return lo + int(np.argmin(np.mean(np.square(frames), axis=1))) * f


def split(samples, rate, embed_many, threshold):
    """Where the voice changes in one over.

    samples: float32 audio; embed_many(list of equal-length arrays) -> one embedding per array;
    threshold: similarity below which two stretches are different voices (speaker.split).
    Returns (parts, mixed, sims): parts [(start, end)] sample indices covering the whole over, in
    order; mixed[i] True if part i has more than one voice right through it; sims the window
    similarities that decided it (for the log, to tune speaker.split with).
    """
    n = len(samples)
    win = int(WIN_S * rate)
    whole = [(0, n)], [False], []
    if n < 2 * win:
        return whole
    hop = max(int(HOP_S * rate), -(-(n - win) // (MAX_WINDOWS - 1)))
    starts = list(range(0, n - win + 1, hop))
    levels = [_db(samples[s:s + win]) for s in starts]
    loudest = max(levels)
    keep = [s for s, lvl in zip(starts, levels) if lvl >= loudest - QUIET_DB]
    if len(keep) < 3:
        return whole
    emb = np.array([_unit(np.asarray(e, np.float32)) for e in embed_many([samples[s:s + win] for s in keep])])

    # Walk the windows: a new voice is two in a row unlike the current one.
    firsts, ref, sims = [0], [emb[0]], []
    for j in range(1, len(keep)):
        voice = _unit(np.mean(ref[-3:], axis=0))
        sim = float(emb[j] @ voice)
        sims.append(round(sim, 3))
        if sim >= threshold:
            ref.append(emb[j])
        elif j + 1 == len(keep) or float(emb[j + 1] @ voice) < threshold:
            firsts.append(j)
            ref = [emb[j]]
        # else: one odd window (a cough, a burst of noise): ignore it

    # Cut between the middle of the last old window and the middle of the first new one.
    cuts = [_quietest(samples, keep[j - 1] + win // 2, keep[j] + win // 2, rate) for j in firsts[1:]]
    bounds = [0, *cuts, n]
    parts = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
    groups = [[j for j, s in enumerate(keep) if a <= s + win // 2 < b] for a, b in parts]

    # Fold parts too short to judge into the neighbour they're most like (or just a neighbour).
    min_len = int(MIN_PART_S * rate)
    i = 0
    while len(parts) > 1 and i < len(parts):
        a, b = parts[i]
        if b - a >= min_len:
            i += 1
            continue
        k = i - 1 if i == len(parts) - 1 or (i > 0 and _close(emb, groups[i], groups[i - 1]) >=
                                             _close(emb, groups[i], groups[i + 1])) else i + 1
        lo, hi = min(i, k), max(i, k)
        parts[lo:hi + 1] = [(parts[lo][0], parts[hi][1])]
        groups[lo:hi + 1] = [groups[lo] + groups[hi]]
        i = max(0, lo)

    # Join neighbours that are the same voice after all.
    i = 0
    while i < len(parts) - 1:
        if _close(emb, groups[i], groups[i + 1]) >= threshold:
            parts[i:i + 2] = [(parts[i][0], parts[i + 1][1])]
            groups[i:i + 2] = [groups[i] + groups[i + 1]]
        else:
            i += 1

    mixed = []
    for g in groups:
        if len(g) < 3:
            mixed.append(False)
            continue
        voice = _unit(np.mean(emb[g], axis=0))
        unlike = sum(float(emb[j] @ voice) < threshold for j in g)
        mixed.append(unlike / len(g) > MIXED_SHARE)
    return parts, mixed, sims


def _close(emb, ga, gb):
    """How alike two groups of windows are (their mean voices); -1 if either is empty."""
    if not ga or not gb:
        return -1.0
    return float(_unit(np.mean(emb[ga], axis=0)) @ _unit(np.mean(emb[gb], axis=0)))
