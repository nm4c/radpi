"""Speech to text for each over.

Backends (bot.stt.backend):
  faster_whisper  Whisper on the Pi's CPU (pip install faster-whisper). base.en takes a few
                  seconds per over on a Pi 5; tiny.en is quicker and rougher, small.en better and slower.
  openai          any server with the OpenAI /audio/transcriptions API: OpenAI itself, Speaches,
                  LocalAI, or a Whisper server on the AI HAT+ 2 that offers that API.
  mock            canned phrases, for trying the rest of the agent without a model.

transcribe(samples, hint, lenient) takes float32 audio at 16 kHz; hint is extra vocabulary (known
names); lenient is set for scratchy overs (quality.py) so hiss doesn't get real words filtered out.
Blocking backends run in a worker thread (see agent.py); the HTTP one is async.

Whisper makes things up when there's no speech to hear: squelch noise, a carrier, the tail of an
over. The large models (large-v3-turbo especially) are the worst: they fill noise with stock
phrases ("Thank you for watching"), repeat the vocabulary hint back ("Bluey, Shorty, Bluey,
Shorty..."), or get stuck saying one phrase over and over. So (bot.stt.filter, on by default):
  - the server is asked to skip non-speech first (vad_filter; Speaches and faster-whisper);
  - each segment Whisper itself thinks is probably not speech, or is too repetitive to be real
    (compression ratio), is dropped;
  - a phrase repeated three or more times in a row is said once;
  - what's left is dropped if it's only the hint read back, a stock phrase, in another alphabet
    when language is en, or more words than anyone can say in that time.
A repeater's tail (the carrier and hiss after the station lets go) is the usual trigger: cut it
off with bot.tail_ms.
"""

import logging
import random
import re

import aiohttp

from .capture import to_pcm, wav_bytes

log = logging.getLogger("bot.stt")

NO_SPEECH = 0.6        # segment's no_speech_prob above this (and unsure of its words) = not speech
LOW_LOGPROB = -1.0     # "unsure of its words": average log probability below this
MAX_COMPRESSION = 2.4  # text that compresses better than this is a repetition loop
MAX_WPS = 5.0          # words per second nobody beats on a radio
STOCK = {  # what Whisper says to silence and noise (learned from subtitled video)
    "thank you", "thank you very much", "thanks for watching", "thank you for watching",
    "thanks for watching and see you next time", "please subscribe", "subscribe", "you", "bye",
    "bye bye", "so", "uh", "um", "hmm", "okay", "oh", "ah", "the end", "silence", "music",
    "applause", "laughter", "beep", "i'm sorry", "subtitles by the amara org community",
}


def _words(text):
    return re.findall(r"[\w']+", text.lower())


def _unloop(text):
    """A phrase of 1-8 words said three or more times in a row, said once."""
    words = text.split()
    norm = [w.lower().strip(".,!?") for w in words]
    out, i = [], 0
    while i < len(words):
        step = 1
        for n in range(1, min(8, (len(words) - i) // 3) + 1):
            reps = 1
            while norm[i + reps * n:i + (reps + 1) * n] == norm[i:i + n]:
                reps += 1
            if reps >= 3:
                out += words[i:i + n]
                step = reps * n
                break
        else:
            out.append(words[i])
        i += step
    return " ".join(out)


def keep_segment(seg, lenient=False):
    """Whisper's own opinion of one segment (a dict or an object with the usual fields).
    lenient (a scratchy over, see quality.py): hiss makes Whisper doubt real speech, so only
    repetition loops are dropped."""
    get = seg.get if isinstance(seg, dict) else lambda k, d=None: getattr(seg, k, d)
    no_speech, logprob = get("no_speech_prob", 0.0) or 0.0, get("avg_logprob", 0.0) or 0.0
    if not lenient and no_speech > NO_SPEECH and logprob < LOW_LOGPROB:
        return False
    return (get("compression_ratio", 0.0) or 0.0) <= MAX_COMPRESSION


def tidy(text, seconds, prompt="", language="en"):
    """The transcript with Whisper's inventions taken out; "" if nothing real is left."""
    text = _unloop(" ".join(text.split()))
    letters = [c for c in text if c.isalpha()]
    if language == "en" and letters and sum(not c.isascii() for c in letters) > 0.2 * len(letters):
        return ""   # noise "heard" as another language
    words = _words(text)
    if not words:
        return ""
    if " ".join(words) in STOCK:
        return ""
    if prompt and set(words) <= set(_words(prompt)) | {"and", "over", "here"}:
        return ""   # the vocabulary hint read back
    if seconds > 0 and len(words) > MAX_WPS * seconds + 3:
        return ""
    return text


class FasterWhisper:
    is_async = False

    def __init__(self, cfg):
        from faster_whisper import WhisperModel  # optional dependency (requirements-bot.txt)
        self.cfg = cfg
        log.info("loading Whisper model %s (%s)", cfg["model"], cfg["compute_type"])
        self.model = WhisperModel(cfg["model"], device="cpu", compute_type=cfg["compute_type"])

    def transcribe(self, samples, hint="", lenient=False):
        prompt = " ".join(p for p in (self.cfg["prompt"], hint) if p) or None
        filt = self.cfg["filter"]
        segments, _ = self.model.transcribe(
            samples, language=self.cfg["language"] or None, beam_size=int(self.cfg["beam_size"]),
            initial_prompt=prompt, condition_on_previous_text=False, vad_filter=bool(filt) and not lenient,
            temperature=0.0)
        text = " ".join(s.text.strip() for s in segments if not filt or keep_segment(s, lenient)).strip()
        return tidy(text, len(samples) / 16000, prompt or "", self.cfg["language"]) if filt else text


class OpenAITranscribe:
    is_async = True

    def __init__(self, cfg, rate):
        self.cfg = cfg
        self.rate = rate
        self.url = cfg["url"].rstrip("/") + "/audio/transcriptions"
        self.filter = bool(cfg["filter"])
        # OpenAI's own service rejects fields it doesn't know (vad_filter), and its newer models
        # only answer in plain json; Speaches and the like take both.
        self.extras = self.filter and "api.openai.com" not in self.url

    async def _post(self, samples, prompt, extras, vad=True):
        form = aiohttp.FormData()
        form.add_field("file", wav_bytes(to_pcm(samples), self.rate), filename="over.wav", content_type="audio/wav")
        form.add_field("model", self.cfg["model"])
        form.add_field("temperature", "0")
        if self.cfg["language"]:
            form.add_field("language", self.cfg["language"])
        if prompt:
            form.add_field("prompt", prompt)
        if extras:
            form.add_field("response_format", "verbose_json")
            form.add_field("vad_filter", "true" if vad else "false")
        headers = {"Authorization": f"Bearer {self.cfg['api_key']}"} if self.cfg["api_key"] else {}
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(self.url, data=form, headers=headers) as r:
                if r.status != 200:
                    raise RuntimeError(f"transcription server: HTTP {r.status} {(await r.text())[:200]}")
                return await r.json()

    async def transcribe(self, samples, hint="", lenient=False):
        prompt = " ".join(p for p in (self.cfg["prompt"], hint) if p)
        if self.extras:
            try:
                data = await self._post(samples, prompt, True, vad=not lenient)
            except RuntimeError as e:
                if "HTTP 4" not in str(e):
                    raise
                log.warning("transcription server won't take vad_filter/verbose_json (%s); "
                            "filtering the text only", e)
                self.extras = False
                data = await self._post(samples, prompt, False)
        else:
            data = await self._post(samples, prompt, False)
        segments = data.get("segments")
        if self.filter and segments:
            text = " ".join((sg.get("text") or "").strip() for sg in segments if keep_segment(sg, lenient))
        else:
            text = data.get("text") or ""
        text = text.strip()
        return tidy(text, len(samples) / self.rate, prompt, self.cfg["language"]) if self.filter else text


class MockSTT:
    is_async = False
    LINES = [
        "Anyone got a copy on this channel, over.",
        "Yeah righto, heading back to the depot now.",
        "Has anyone seen the keys for the white ute?",
        "Radio check, how do you read me.",
        "Weather's turning, might knock off early this arvo.",
        "G'day all, it's Dave, just checking in.",
    ]

    def __init__(self, cfg=None):
        self.rng = random.Random(1)

    def transcribe(self, samples, hint="", lenient=False):
        return self.rng.choice(self.LINES)


def create(cfg, rate):
    kind = cfg["backend"]
    if kind == "faster_whisper":
        return FasterWhisper(cfg)
    if kind == "openai":
        return OpenAITranscribe(cfg, rate)
    return MockSTT(cfg)
