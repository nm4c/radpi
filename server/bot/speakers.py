"""Who is talking: voiceprints, and the people the agent knows.

Each over is turned into an embedding (a vector that describes the voice, not the words) and
compared with every person's voiceprint: the overs of theirs it has kept, one embedding each. A
person's score is the average of their few closest overs, not of all of them, so someone heard on
two different radios still matches whichever one they're on now. Over a radio channel the rig
colours the sound about as much as the voice does, which is why this matters.

  score >= speaker.match         that person; >= speaker.update also adds the over to their voiceprint
  score <  speaker.new           nobody it knows: a new anonymous caller (if the over is long enough)
  in between                     not sure: left unidentified rather than guessed or split off

Anonymous callers whose voiceprints turn out to be close (>= speaker.merge) are merged, so one
person split into "Caller 3" and "Caller 7" comes back together as it hears more of them. Named
people (known, acquainted) are never merged automatically. When the voiceprint is full
(speaker.max_samples), the most redundant over is dropped rather than the oldest, so the different
radios someone uses stay represented.

Anchors keep a voiceprint from drifting. A person's anchors are the overs that are certainly
theirs: the first one it heard, and every over an operator names (in the web page, or enrol).
An automatic update only adds an over that also matches the anchors (>= speaker.update), and an
anonymous caller is only merged into someone whose anchors it matches (>= speaker.merge).
Without that, one wrong match lets another voice in, which makes the next wrong match likelier,
until one person's voiceprint matches everybody. Anchors are never trimmed away.

People come in three kinds:
  known       enrolled by an operator (named in the web page or with  python -m server.bot enrol)
  acquainted  was anonymous, then said a name the LLM picked up
  anonymous   an unfamiliar voice, kept under a pseudonym ("Caller 7") when bot.learn_anonymous
              is on and the over was long enough (speaker.min_enrol_s) to start a voiceprint

Files, in <bot.dir>/people/:
  people.json      [{"id", "name", "kind", "created", "last_seen", "overs"}]
  <id>.npy         that person's voiceprint samples (one embedding per row)
  <id>.anchors.npy  the anchors (same format); a voiceprint from before anchors has none, and
                   then updates aren't checked: name one of their overs to give them one
  <id>.md          notes: written by an operator or by the agent itself; plain text, editable

Embedding backends (bot.speaker.backend):
  ecapa   SpeechBrain's ECAPA-TDNN speaker model (pip install speechbrain torch torchaudio). Good.
  basic   statistics of the log-mel spectrum, numpy only. Rough: fine for testing the pipeline
          with a couple of very different voices, not for real use.
"""

import json
import logging
import secrets
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("bot.speakers")

# (match, update, new, merge) per backend. ECAPA's are for narrowband FM radio audio, which scores
# lower for the same voice than the clean recordings the model was trained on.
DEFAULT_THRESHOLDS = {"ecapa": (0.50, 0.62, 0.38, 0.55), "basic": (0.96, 0.98, 0.93, 0.97)}
# Two 1.5 s windows of one over less alike than this are different voices (speaker.split; turns.py).
# Short windows give noisier embeddings than whole overs, so this sits below speaker.new.
DEFAULT_SPLIT = {"ecapa": 0.30, "basic": 0.90}
TOP_K = 3  # a person's score: the mean of this many of their closest kept overs
NOTES_MAX = 4000  # characters of notes kept per person (older lines are trimmed off the top)


# ---- embeddings -------------------------------------------------------------------

class EcapaEmbedder:
    def __init__(self, model_dir):
        from speechbrain.inference.speaker import EncoderClassifier  # optional dependency
        import torch
        self.torch = torch
        log.info("loading the ECAPA speaker model (first run downloads about 80 MB)")
        self.model = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb",
                                                    savedir=str(model_dir), run_opts={"device": "cpu"})

    def embed(self, samples):
        with self.torch.no_grad():
            e = self.model.encode_batch(self.torch.from_numpy(samples)[None])
        return e.squeeze().cpu().numpy().astype(np.float32)

    def embed_many(self, chunks):
        """One embedding per chunk, all in one batch (the chunks must be the same length)."""
        with self.torch.no_grad():
            e = self.model.encode_batch(self.torch.from_numpy(np.stack(chunks).astype(np.float32)))
        return list(e.squeeze(1).cpu().numpy().astype(np.float32))


class BasicEmbedder:
    """Mean and spread of 40 log-mel bands over the voiced frames. Numpy only."""

    def __init__(self, rate):
        self.rate = rate
        self.n_fft = 512
        self.mel = self._mel_bank(40, self.n_fft, rate, 100, min(4000, rate / 2))

    @staticmethod
    def _mel_bank(n, n_fft, rate, lo, hi):
        mel = lambda f: 2595 * np.log10(1 + f / 700)  # noqa: E731
        inv = lambda m: 700 * (10 ** (m / 2595) - 1)  # noqa: E731
        pts = inv(np.linspace(mel(lo), mel(hi), n + 2))
        bins = np.floor((n_fft + 1) * pts / rate).astype(int)
        bank = np.zeros((n, n_fft // 2 + 1), np.float32)
        for i in range(n):
            a, b, c = bins[i], bins[i + 1], bins[i + 2]
            if b > a:
                bank[i, a:b] = (np.arange(a, b) - a) / (b - a)
            if c > b:
                bank[i, b:c] = (c - np.arange(b, c)) / (c - b)
        return bank

    def embed(self, samples):
        hop = self.n_fft // 2
        if len(samples) < self.n_fft:
            samples = np.pad(samples, (0, self.n_fft - len(samples)))
        frames = np.lib.stride_tricks.sliding_window_view(samples, self.n_fft)[::hop] * np.hanning(self.n_fft)
        spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
        logmel = np.log(spec @ self.mel.T + 1e-8)
        energy = logmel.mean(axis=1)
        voiced = logmel[energy > np.percentile(energy, 40)]
        if len(voiced) < 4:
            voiced = logmel
        voiced = voiced - voiced.mean(axis=1, keepdims=True)  # level-independent
        return np.concatenate([voiced.mean(axis=0), voiced.std(axis=0)]).astype(np.float32)

    def embed_many(self, chunks):
        return [self.embed(c) for c in chunks]


def create_embedder(cfg, rate, bot_dir):
    if cfg["backend"] == "ecapa":
        return EcapaEmbedder(cfg["model_dir"] or Path(bot_dir) / "models" / "ecapa")
    return BasicEmbedder(rate)


def _unit(v):
    n = float(np.linalg.norm(v))
    return v / n if n else v


def _score(samples, e):
    """How well unit embedding e matches a person: the mean of their TOP_K closest samples."""
    sims = samples @ e
    k = min(TOP_K, len(sims))
    return float(np.mean(np.partition(sims, -k)[-k:]))


def _pair_score(a, b):
    """How alike two voiceprints are: each sample's score against the other, averaged both ways."""
    return (np.mean([_score(b, x) for x in a]) + np.mean([_score(a, x) for x in b])) / 2


# ---- people -----------------------------------------------------------------------

class People:
    """The people the agent knows. Used from the event loop and the worker thread, so locked."""

    def __init__(self, directory, cfg, backend):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index = self.dir / "people.json"
        defaults = dict(zip(("match", "update", "new", "merge"), DEFAULT_THRESHOLDS[backend]))
        for key, default in defaults.items():
            # merge -> merge_at: an attribute called merge would hide the merge() method
            setattr(self, "merge_at" if key == "merge" else key, float(cfg[key]) if cfg.get(key) is not None else default)
        self.new = min(self.new, self.match)
        self.split = float(cfg["split"]) if cfg.get("split") is not None else DEFAULT_SPLIT[backend]
        self.max_samples = int(cfg["max_samples"])
        self.lock = threading.RLock()
        self.people = self._load()
        self.samples = {}
        self.anchors = {}
        for p in self.people:
            for store, suffix in ((self.samples, ".npy"), (self.anchors, ".anchors.npy")):
                try:
                    store[p["id"]] = np.load(self.dir / f"{p['id']}{suffix}")
                except (OSError, ValueError):
                    store[p["id"]] = None

    def _load(self):
        try:
            data = json.loads(self.index.read_text())
            return [p for p in data if isinstance(p, dict) and p.get("id")]
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("could not read %s (%s): starting with nobody", self.index, e)
            return []

    def _save(self):
        tmp = self.index.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.people, indent=1))
        tmp.replace(self.index)

    def _save_samples(self, pid):
        for store, suffix in ((self.samples, ".npy"), (self.anchors, ".anchors.npy")):
            s = store.get(pid)
            if s is not None:
                np.save(self.dir / f"{pid}{suffix}", s)

    def voiceprint(self, pid):
        """All of a person's embeddings, anchors and samples together (None if none)."""
        parts = [x for x in (self.anchors.get(pid), self.samples.get(pid)) if x is not None and len(x)]
        if len(parts) == 2 and parts[0].shape[1] != parts[1].shape[1]:
            parts = parts[1:]
        return np.vstack(parts) if parts else None

    def fits(self, pid, emb):
        """Does this voice match pid's anchors well enough to be added to their voiceprint?
        True when they have no anchors (a voiceprint from before anchors existed)."""
        a = self.anchors.get(pid)
        e = _unit(emb)
        if a is None or not len(a) or a.shape[1] != len(e):
            return True
        return float(np.max(a @ e)) >= self.update   # the closest anchor: they may have several radios

    def list(self):
        with self.lock:
            return [dict(p) for p in self.people]

    def get(self, pid):
        with self.lock:
            return next((p for p in self.people if p["id"] == pid), None)

    def find_name(self, name):
        with self.lock:
            key = name.strip().lower()
            return next((p for p in self.people if p["name"].lower() == key and p["kind"] != "anonymous"), None)

    def identify(self, emb):
        """(person, score) of the best match at or above speaker.match, else (None, best score).
        A best score below speaker.new means nobody it knows; in between, not sure."""
        e = _unit(emb)
        best, score = None, -1.0
        with self.lock:
            for p in self.people:
                s = self.voiceprint(p["id"])
                if s is None or s.shape[1] != len(e):
                    continue
                sim = _score(s, e)
                if sim > score:
                    best, score = p, sim
        return (best, score) if best is not None and score >= self.match else (None, score)

    def add_sample(self, pid, emb, anchor=False):
        """Add an over to pid's voiceprint; anchor=True for one that is certainly theirs."""
        with self.lock:
            store = self.anchors if anchor else self.samples
            s = store.get(pid)
            row = _unit(emb)[None]
            s = row if s is None or not len(s) or s.shape[1] != row.shape[1] else np.vstack([s, row])
            store[pid] = self._trim(s)
            self._save_samples(pid)

    def _trim(self, s):
        """Cut samples down to speaker.max_samples by dropping the most redundant of the older ones
        (the one most like the rest), so the voiceprint keeps covering every radio and condition it
        has heard them on. The newest sample always stays."""
        while len(s) > self.max_samples:
            sims = s @ s.T
            redundancy = (sims.sum(axis=1) - 1) / (len(s) - 1)
            s = np.delete(s, int(np.argmax(redundancy[:-1])), axis=0)
        return s

    def similar(self, pid=None):
        """[(score, a, b)] for pairs of people, most alike first; only pairs with pid if given."""
        with self.lock:
            prints = {p["id"]: self.voiceprint(p["id"]) for p in self.people}
            have = [p for p in self.people if prints[p["id"]] is not None]
            out = []
            for i, a in enumerate(have):
                for b in have[i + 1:]:
                    if pid and pid not in (a["id"], b["id"]):
                        continue
                    sa, sb = prints[a["id"]], prints[b["id"]]
                    if sa.shape[1] == sb.shape[1]:
                        out.append((float(_pair_score(sa, sb)), a, b))
            return sorted(out, key=lambda t: -t[0])

    def consolidate(self, pid):
        """Merge anonymous callers whose voice is close to pid's (>= speaker.merge). Returns the
        person pid ended up as (it may have been folded into someone else), and the merges done
        as [(from, into)]."""
        merges = []
        with self.lock:
            for score, a, b in self.similar(pid):
                if score < self.merge_at:
                    break
                if self.get(a["id"]) is None or self.get(b["id"]) is None:
                    continue
                if a["kind"] != "anonymous" and b["kind"] != "anonymous":
                    continue  # two named people: an operator decides that
                # Fold the anonymous one into the named one; two anonymous: into the busier one.
                if a["kind"] == "anonymous" and (b["kind"] != "anonymous" or b.get("overs", 0) >= a.get("overs", 0)):
                    src, dst = a, b
                else:
                    src, dst = b, a
                anchors, theirs = self.anchors.get(dst["id"]), self.voiceprint(src["id"])
                if (anchors is not None and len(anchors) and theirs is not None
                        and anchors.shape[1] == theirs.shape[1]
                        and float(np.mean(np.max(theirs @ anchors.T, axis=1))) < self.merge_at):
                    continue  # alike overall, but not like the overs that are certainly dst's
                self.merge(src["id"], dst["id"])
                merges.append((src, dst))
                if src["id"] == pid:
                    pid = dst["id"]
            return self.get(pid), merges

    def seen(self, pid):
        with self.lock:
            p = self.get(pid)
            if p:
                p["last_seen"] = time.time()
                p["overs"] = p.get("overs", 0) + 1
                self._save()

    def create(self, name, kind, emb=None):
        with self.lock:
            if not name:
                n = sum(p["kind"] == "anonymous" for p in self.people) + 1
                taken = {p["name"] for p in self.people}
                while f"Caller {n}" in taken:
                    n += 1
                name = f"Caller {n}"
            p = {"id": secrets.token_hex(4), "name": name[:40], "kind": kind, "created": time.time(),
                 "last_seen": time.time(), "overs": 0}
            self.people.append(p)
            self.samples[p["id"]] = None
            self.anchors[p["id"]] = None
            if emb is not None:
                self.add_sample(p["id"], emb, anchor=True)  # the first over is certainly theirs
            self._save()
            return p

    def rename(self, pid, name, kind=None):
        with self.lock:
            p = self.get(pid)
            if not p:
                return None
            p["name"] = name.strip()[:40] or p["name"]
            if kind:
                p["kind"] = kind
            self._save()
            return p

    def merge(self, src_id, dst_id, vouched=False):
        """Fold person src into dst (voiceprint, notes, counts), then delete src. vouched: an
        operator said they're the same person, so src's anchors become dst's anchors too;
        otherwise everything of src's joins dst's ordinary samples."""
        with self.lock:
            src, dst = self.get(src_id), self.get(dst_id)
            if not src or not dst or src is dst:
                return dst
            moves = [(self.anchors.get(src_id), self.anchors if vouched else self.samples),
                     (self.samples.get(src_id), self.samples)]
            for b, store in moves:
                if b is None or not len(b):
                    continue
                a = store.get(dst_id)
                store[dst_id] = self._trim(b if a is None or not len(a) or a.shape[1] != b.shape[1] else np.vstack([a, b]))
            self._save_samples(dst_id)
            notes = self.notes(src_id)
            if notes:
                self.add_note(dst_id, notes, stamp=False)
            dst["overs"] = dst.get("overs", 0) + src.get("overs", 0)
            dst["last_seen"] = max(dst.get("last_seen", 0), src.get("last_seen", 0))
            self.delete(src_id)
            return dst

    def delete(self, pid):
        with self.lock:
            p = self.get(pid)
            if not p:
                return None
            self.people.remove(p)
            self.samples.pop(pid, None)
            self.anchors.pop(pid, None)
            for ext in (".npy", ".anchors.npy", ".md"):
                (self.dir / f"{pid}{ext}").unlink(missing_ok=True)
            self._save()
            return p

    def notes(self, pid):
        try:
            return (self.dir / f"{pid}.md").read_text()
        except FileNotFoundError:
            return ""

    def add_note(self, pid, text, stamp=True):
        with self.lock:
            text = " ".join(str(text).split()) if stamp else str(text).strip()
            if not text:
                return
            line = f"- {time.strftime('%Y-%m-%d')}: {text}\n" if stamp else text + "\n"
            notes = (self.notes(pid) + line)[-NOTES_MAX:]
            (self.dir / f"{pid}.md").write_text(notes)
