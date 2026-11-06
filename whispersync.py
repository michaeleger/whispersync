#!/usr/bin/env python3
"""
whispersync — second-opinion transcripts for Eager to Be Healthy.

Riverside's transcript gives us speakers and turn structure but garbles names,
numbers, and whole phrases. Whisper (run locally on a Linux box, CPU only) hears the
words better but has no idea who is talking. whispersync keeps Riverside's
speaker turns, swaps in Whisper's words, and writes a short review list of
every spot where the two disagree so a human can check it by ear.

Commands
  episodes                 list recent episodes from the RSS feed
  run <episode>            fetch + transcribe + re-listen + Gemini + sync, end to end
  fetch <episode>          download the MP3 and Riverside transcript only
  transcribe <episode>     run Whisper on an episode already fetched (or --audio)
  repair <episode>         re-listen where Whisper skipped speech Riverside has
  google <episode>         Gemini transcript as a tie-breaking third opinion (needs GEMINI_API_KEY)
  watch                    one hands-off round for cron: new episodes + inbox requests
  sync <episode>           re-run the comparison without re-transcribing
  selftest                 check the alignment logic (no model, no network)

<episode> is "latest", a number from `episodes`, or a word from the title
("doerr", "smith"). For pre-publish checks, skip the feed entirely:

  whispersync.py run --audio export.mp3 --riverside transcript.txt \
      --name jane-doe --guest "Jane Doe"
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import re
import shutil
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Settings (override with environment variables or flags)
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
FEED_URL = os.environ.get("WS_FEED", "https://api.riverside.fm/hosting/drlxDc7x.rss")
WORK_DIR = Path(os.environ.get("WS_WORK", ROOT / "work"))
MODEL_DIR = Path(os.environ.get("WS_MODELS", ROOT / "models"))
OUT_DIR = Path(os.environ.get("WS_OUT", ROOT / "out"))
INBOX_DIR = Path(os.environ.get("WS_INBOX", ROOT / "inbox"))
DEFAULT_MODEL = os.environ.get("WS_MODEL", "large-v3-turbo")
DEFAULT_THREADS = int(os.environ.get("WS_THREADS", "6"))  # 6 of 16: same cap that kept gemma stable
HOST_NAME = os.environ.get("WS_HOST", "Michael Scott Eger")
GEMINI_MODEL = os.environ.get("WS_GEMINI_MODEL", "gemini-flash-latest")  # alias for Google's newest Flash
GEMINI_CHUNK = int(os.environ.get("WS_GEMINI_CHUNK", "600"))  # seconds of audio per request
UA = "whispersync/1.0 (+eagertobehealthy.com)"

NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "podcast": "https://podcastindex.org/namespace/1.0",
}

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(msg, flush=True)


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower().replace("&", "and"))
    return re.sub(r"[\s_-]+", "-", text).strip("-")


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_ts(text: str) -> float:
    """'1:02:03', '50:22', '0:9.772' -> seconds."""
    parts = text.strip().split(":")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def http_get(url: str, dest: Path | None = None) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        if dest is None:
            return resp.read()
        tmp = dest.with_suffix(dest.suffix + ".part")
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        last = time.time()
        with open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if time.time() - last > 5 and total:
                    log(f"  … {got * 100 // total}% of {total // (1 << 20)} MB")
                    last = time.time()
        tmp.replace(dest)
        return None


def unique_path(path: Path) -> Path:
    """Never overwrite: name.ext -> name_2.ext -> name_3.ext."""
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not cand.exists():
            return cand
        n += 1


# ---------------------------------------------------------------------------
# Number words, so "thirty" and "30" count as the same word
# ---------------------------------------------------------------------------

_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _num_words(n: int) -> list[str]:
    if n < 20:
        return [_ONES[n]]
    if n < 100:
        t, o = divmod(n, 10)
        return [_TENS[t]] + ([_ONES[o]] if o else [])
    if n < 1000:
        h, r = divmod(n, 100)
        return [_ONES[h], "hundred"] + (_num_words(r) if r else [])
    if 1100 <= n <= 2099 and n % 100:  # years: 1985 -> nineteen eighty five
        a, b = divmod(n, 100)
        return _num_words(a) + (["oh"] if b < 10 else []) + _num_words(b)
    if n < 10000:
        th, r = divmod(n, 1000)
        return _num_words(th) + ["thousand"] + (_num_words(r) if r else [])
    return [str(n)]


# Words that carry no meaning on their own. A disagreement made only of these
# ("It was" vs "It's", "I that's" vs "This is") isn't worth a listen.
# Negations (not, no, never, didn't, wasn't…) are deliberately NOT here.
FUNCTION = set("""
a an the and but or so if then than that this these those there here it its is was were are be been being
am im i you youre youve youd youll we were wed weve well they theyre theyve he hes she shes me my mine our
ours your yours their them us him her his of to in on at for with from by as about into onto up out off over
do does did doing have has had having ive id ill will would could should can may might must just like really
very oh yeah yes yep yup okay right um uh uhm umm er erm ah eh hmm hm mm mhm mmhmm huh know mean kind sort
thats whats theres heres lets going got get gets go goes what which who whom how when where why also too all
some any more most now actually basically little bit lot things thing stuff something anything everything
because cause even still back way one ones s t d y w m
""".split())
FILLERS = FUNCTION  # kept for older call sites
BACKCHANNEL = {"mmhmm", "mhm", "mm", "hmm", "yeah", "yep", "yes", "okay", "ok", "right", "sure", "great",
               "excellent", "absolutely", "exactly", "wow", "ha", "haha", "oh", "uh", "huh", "cool", "nice",
               "thats", "true", "totally", "definitely", "correct", "hm"}
SPOKEN_PUNCT = {"dot", "slash", "com"}
NEGATIONS = {"no", "not", "never", "nothing", "none", "nobody", "dont", "didnt", "doesnt", "wasnt", "werent",
             "isnt", "arent", "cant", "couldnt", "wouldnt", "shouldnt", "wont", "havent", "hasnt", "hadnt"}
CASUAL = {  # spoken forms both tools write differently
    "gonna": ["going", "to"], "wanna": ["want", "to"], "gotta": ["got", "to"], "kinda": ["kind", "of"],
    "sorta": ["sort", "of"], "outta": ["out", "of"], "lotta": ["lot", "of"], "dunno": ["dont", "know"],
    "cause": ["because"], "cuz": ["because"], "alright": ["all", "right"], "ok": ["okay"],
    "mmhmm": ["mmhmm"], "mhm": ["mmhmm"], "uhhuh": ["mmhmm"], "till": ["until"], "til": ["until"],
}
_TENS_PLURAL = {t[:-1] + "ies": t for t in _TENS[2:]}  # twenties -> twenty


def norm_tokens(word: str) -> list[str]:
    """Normalize one display word into zero or more comparison tokens."""
    w = word.lower().replace("’", "'").replace("%", " percent")
    w = re.sub(r"(\d)\s*'\s*(\d{1,2})\s*(\"|\u201d|'')?", r"\1 foot \2 ", w)  # 6'2" -> 6 foot 2
    w = re.sub(r"[“”\"(),.!?;:…\[\]{}*~]", " ", w)
    w = w.replace("'", "")
    w = re.sub(r"(?<=[a-z])-(?=hmm\b)", "", w)          # mm-hmm -> mmhmm
    w = re.sub(r"(?<=\w)[-/](?=\w)", " ", w)            # well-being -> well being
    out: list[str] = []
    for piece in w.split():
        piece = re.sub(r"[^a-z0-9]", "", piece)
        if not piece:
            continue
        if piece.isdigit() and len(piece) <= 4:
            out.extend(_num_words(int(piece)))
            continue
        m = re.fullmatch(r"(\d+)(st|nd|rd|th|s)", piece)
        if m and len(m.group(1)) <= 4:
            out.extend(_num_words(int(m.group(1))))
        elif piece in CASUAL:
            out.extend(CASUAL[piece])
        elif piece in _TENS_PLURAL:
            out.append(_TENS_PLURAL[piece])
        else:
            out.append(piece)
    return out


# ---------------------------------------------------------------------------
# Riverside transcript parsing (txt, vtt, srt, podcast-index json)
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    speaker: str
    start: float
    text: str


@dataclass
class RiversideDoc:
    header: list[str] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)


TURN_RE = re.compile(r"^(?P<spk>[^\n()]{1,80}?)\s*\((?P<ts>\d+(?::\d+(?:\.\d+)?){1,2})\)\s*$")


def parse_riverside_txt(text: str) -> RiversideDoc:
    doc = RiversideDoc()
    cur: Turn | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = TURN_RE.match(line.strip())
        if m:
            if cur:
                cur.text = " ".join(buf).strip()
                doc.turns.append(cur)
            cur = Turn(m.group("spk").strip(), parse_ts(m.group("ts")), "")
            buf = []
        elif cur is None:
            if line.strip():
                doc.header.append(line.rstrip())
        else:
            if line.strip():
                buf.append(line.strip())
    if cur:
        cur.text = " ".join(buf).strip()
        doc.turns.append(cur)
    return doc


COLON_RE = re.compile(r"^(?P<spk>[A-Z][^:\n]{1,60}?):\s+(?P<text>.+)$")


def parse_colon_txt(text: str) -> RiversideDoc:
    """'Speaker: words' paragraphs with no timestamps (Riverside's RSS transcripts.txt)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.replace("\r", "")) if p.strip()]
    counts: dict[str, int] = {}
    for para in paras:
        m = COLON_RE.match(para.split("\n", 1)[0])
        if m and len(m.group("spk").split()) <= 5:
            counts[m.group("spk")] = counts.get(m.group("spk"), 0) + 1
    def looks_like_name(k: str) -> bool:  # "Michael Scott Eger", "Dr. Ana Sample"
        parts = k.split()
        return len(parts) >= 2 and all(p[:1].isupper() or p.lower() in {"de", "van", "von", "da", "la"} for p in parts)
    speakers = {k for k, n in counts.items() if n >= 2 or looks_like_name(k)} or set(counts)
    doc = RiversideDoc()
    for para in paras:
        para = " ".join(para.split("\n"))
        m = COLON_RE.match(para)
        if m and m.group("spk") in speakers:
            doc.turns.append(Turn(m.group("spk").strip(), 0.0, m.group("text").strip()))
        elif doc.turns:
            doc.turns[-1].text += " " + para
        else:
            doc.header.append(para)
    return doc


CUE_TIME = re.compile(r"(\d+:)?\d+:\d+[.,]\d+\s*-->\s*(\d+:)?\d+:\d+[.,]\d+")


def parse_cues(text: str) -> RiversideDoc:
    """WebVTT / SRT, with optional <v Speaker> or 'Speaker: text' labels."""
    doc = RiversideDoc()
    blocks = re.split(r"\n\s*\n", text.replace("\r", ""))
    for block in blocks:
        lines = [l for l in block.split("\n") if l.strip()]
        idx = next((i for i, l in enumerate(lines) if CUE_TIME.search(l)), None)
        if idx is None:
            continue
        start = parse_ts(lines[idx].split("-->")[0].strip().replace(",", "."))
        body = " ".join(lines[idx + 1:])
        spk = ""
        m = re.match(r"<v\s+([^>]+)>(.*)", body)
        if m:
            spk, body = m.group(1).strip(), m.group(2)
        else:
            m = re.match(r"([A-Z][\w .'-]{1,40}):\s+(.*)", body)
            if m:
                spk, body = m.group(1).strip(), m.group(2)
        body = re.sub(r"</?[^>]+>", "", body).strip()
        if not body:
            continue
        if doc.turns and (not spk or spk == doc.turns[-1].speaker):
            doc.turns[-1].text += " " + body
        else:
            doc.turns.append(Turn(spk or "Speaker", start, body))
    return doc


def parse_json_transcript(text: str) -> RiversideDoc:
    data = json.loads(text)
    doc = RiversideDoc()
    for seg in data.get("segments", []):
        spk = seg.get("speaker") or "Speaker"
        body = (seg.get("body") or "").strip()
        if not body:
            continue
        if doc.turns and doc.turns[-1].speaker == spk:
            doc.turns[-1].text += " " + body
        else:
            doc.turns.append(Turn(spk, float(seg.get("startTime", 0)), body))
    return doc


def load_riverside(path: Path) -> RiversideDoc:
    text = path.read_text(encoding="utf-8", errors="replace")
    head = text.lstrip()[:200]
    if head.startswith("{"):
        doc = parse_json_transcript(text)
    elif head.startswith("WEBVTT") or CUE_TIME.search(text[:2000]):
        doc = parse_cues(text)
    else:
        doc = parse_riverside_txt(text)
        if not doc.turns:
            doc = parse_colon_txt(text)
    if not doc.turns:
        raise SystemExit(f"Couldn't find any speaker turns in {path}")
    return doc


def fix_speaker_names(doc: RiversideDoc, guest: str | None, renames: dict[str, str]) -> dict[str, str]:
    """Apply --rename pairs, then snap near-miss guest labels ('AnA Melikian') to the guest name."""
    applied: dict[str, str] = {}
    labels = sorted({t.speaker for t in doc.turns})
    for label in labels:
        new = renames.get(label)
        if not new and guest and label != guest and label != HOST_NAME:
            ratio = difflib.SequenceMatcher(None, label.lower(), guest.lower()).ratio()
            if ratio >= 0.75 or label.lower() == guest.lower():
                new = guest
        if new and new != label:
            applied[label] = new
    for t in doc.turns:
        t.speaker = applied.get(t.speaker, t.speaker)
    return applied


# ---------------------------------------------------------------------------
# RSS feed
# ---------------------------------------------------------------------------


@dataclass
class Episode:
    index: int
    title: str
    pub: str
    audio_url: str
    duration: str
    transcripts: list[tuple[str, str]]  # (type, url)
    description: str = ""
    guid: str = ""

    @property
    def guest(self) -> str:
        parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", self.title)]
        if len(parts) >= 3 and parts[0].lower().startswith("eager to be healthy"):
            return parts[1]
        if len(parts) >= 2 and parts[0].lower().startswith("eager to be healthy"):
            return parts[1]
        return parts[0]

    @property
    def slug(self) -> str:
        return slugify(self.guest) or slugify(self.title)[:40]


def _strip_html(text: str) -> str:
    import html
    text = re.sub(r"<\s*(br|/p|p)[^>]*>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def load_feed() -> list[Episode]:
    raw = http_get(FEED_URL)
    root = ET.fromstring(raw)
    eps: list[Episode] = []
    for i, item in enumerate(root.iter("item"), start=1):
        enc = item.find("enclosure")
        trans = [(t.get("type", ""), t.get("url", "")) for t in item.findall("podcast:transcript", NS)]
        eps.append(Episode(
            index=i,
            title=(item.findtext("title") or "").strip(),
            pub=(item.findtext("pubDate") or "").strip(),
            audio_url=enc.get("url", "") if enc is not None else "",
            duration=(item.findtext("itunes:duration", default="", namespaces=NS) or "").strip(),
            transcripts=trans,
            description=_strip_html(item.findtext("description") or ""),
            guid=(item.findtext("guid") or "").strip() or (item.findtext("title") or "").strip(),
        ))
    return eps


def pick_episode(eps: list[Episode], query: str) -> Episode:
    if query in ("latest", "last", "newest"):
        return eps[0]
    if query.isdigit():
        n = int(query)
        if 1 <= n <= len(eps):
            return eps[n - 1]
        raise SystemExit(f"No episode #{n}; the feed has {len(eps)}.")
    hits = [e for e in eps if query.lower() in e.title.lower()]
    if not hits:
        raise SystemExit(f'No episode title contains "{query}". Try: whispersync.py episodes')
    if len(hits) > 1:
        log(f'{len(hits)} episodes match "{query}"; using the newest: {hits[0].title}')
    return hits[0]


def pick_transcript(ep: Episode) -> tuple[str, str] | None:
    pref = ["text/plain", "application/json", "text/vtt", "application/x-subrip", "application/srt", "text/srt"]
    for want in pref:
        for typ, url in ep.transcripts:
            if typ.lower() == want and url:
                return typ, url
    return ep.transcripts[0] if ep.transcripts else None


# ---------------------------------------------------------------------------
# Work folder
# ---------------------------------------------------------------------------


@dataclass
class Job:
    dir: Path
    meta: dict

    @property
    def audio(self) -> Path | None:
        p = self.meta.get("audio")
        return Path(p) if p else None

    @property
    def riverside(self) -> Path | None:
        p = self.meta.get("riverside")
        return Path(p) if p else None

    def save(self) -> None:
        (self.dir / "job.json").write_text(json.dumps(self.meta, indent=2), encoding="utf-8")


def job_for(args, need_feed: bool = True) -> Job:
    """Resolve an episode (feed or local files) into a work folder with job.json."""
    local_audio = Path(args.audio).expanduser().resolve() if getattr(args, "audio", None) else None
    local_riv = Path(args.riverside).expanduser().resolve() if getattr(args, "riverside", None) else None

    if local_audio or (args.episode is None and local_riv):
        name = args.name or (slugify(args.guest) if args.guest else None) or slugify(local_audio.stem if local_audio else local_riv.stem)
        jdir = WORK_DIR / name
        jdir.mkdir(parents=True, exist_ok=True)
        job = Job(jdir, _read_meta(jdir))
        job.meta.update({"name": name, "title": job.meta.get("title") or args.guest or name})
        if args.guest:
            job.meta["guest"] = args.guest
        if local_audio:
            job.meta["audio"] = str(local_audio)
        if local_riv:
            job.meta["riverside"] = str(local_riv)
        job.save()
        return job

    if args.episode is None:
        raise SystemExit("Name an episode (latest, a number, or a title word) or pass --audio.")

    # An existing work folder by name skips the feed.
    direct = WORK_DIR / args.episode
    if (direct / "job.json").exists():
        job = Job(direct, _read_meta(direct))
    else:
        eps = load_feed()
        ep = pick_episode(eps, args.episode)
        name = args.name or ep.slug
        jdir = WORK_DIR / name
        jdir.mkdir(parents=True, exist_ok=True)
        job = Job(jdir, _read_meta(jdir))
        job.meta.update({
            "name": name, "title": ep.title, "guest": ep.guest, "pub": ep.pub,
            "audio_url": ep.audio_url, "duration": ep.duration,
            "transcripts": ep.transcripts, "description": ep.description,
        })
    if args.guest:
        job.meta["guest"] = args.guest
    if local_riv:
        job.meta["riverside"] = str(local_riv)
    job.save()
    return job


def _read_meta(jdir: Path) -> dict:
    p = jdir / "job.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def do_fetch(job: Job) -> None:
    m = job.meta
    if not job.audio or not job.audio.exists():
        if not m.get("audio_url"):
            raise SystemExit("No audio: pass --audio or pick an episode from the feed.")
        dest = job.dir / "audio.mp3"
        if not dest.exists():
            log(f"Downloading audio → {dest}")
            http_get(m["audio_url"], dest)
        m["audio"] = str(dest)
    if not job.riverside or not job.riverside.exists():
        ep_like = Episode(0, m.get("title", ""), "", "", "", [tuple(t) for t in m.get("transcripts", [])])
        pick = pick_transcript(ep_like)
        if pick:
            typ, url = pick
            ext = {"text/plain": ".txt", "application/json": ".json", "text/vtt": ".vtt"}.get(typ.lower(), ".srt")
            dest = job.dir / f"riverside{ext}"
            log(f"Downloading Riverside transcript ({typ}) → {dest}")
            http_get(url, dest)
            m["riverside"] = str(dest)
        else:
            log("The feed has no transcript for this episode. Pass --riverside <file> to compare;"
                " otherwise you'll get Whisper's text without speaker names.")
    job.save()


# ---------------------------------------------------------------------------
# Whisper
# ---------------------------------------------------------------------------


def load_glossary(job: Job) -> list[str]:
    terms: list[str] = []
    gfile = ROOT / "glossary.txt"
    if gfile.exists():
        for line in gfile.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                terms.append(line)
    extra = job.dir / "glossary.txt"  # per-episode names: book titles, companies
    if extra.exists():
        terms += [l.strip() for l in extra.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    if job.meta.get("guest"):
        terms.insert(0, job.meta["guest"])
    seen, out = set(), []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def load_audio(path: Path):
    """Decode to 16 kHz mono float32 with ffmpeg. Returns None if ffmpeg isn't available."""
    import subprocess
    if not shutil.which("ffmpeg"):
        return None
    import numpy as np
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def _patch_pyav() -> None:
    """faster-whisper 1.2 passes metadata_errors= to av.open; newer PyAV dropped it."""
    try:
        import av
        if getattr(av.open, "_ws_patched", False):
            return
        orig = av.open

        def _open(*a, **k):
            try:
                return orig(*a, **k)
            except TypeError:
                k.pop("metadata_errors", None)
                return orig(*a, **k)
        _open._ws_patched = True
        av.open = _open
    except ImportError:
        pass


class Engine:
    """Loads the model and decoded audio once, for the full pass and the repair pass."""

    def __init__(self, job: Job, model_name: str, threads: int, beam: int):
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise SystemExit("faster-whisper isn't installed. Run ./setup.sh first.")
        try:
            os.nice(10)  # stay polite to the desktop / xRDP session
        except OSError:
            pass
        import inspect
        self.job, self.model_name, self.threads, self.beam = job, model_name, threads, beam
        self.hotwords = ", ".join(load_glossary(job))[:600] or None
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        log(f"Loading Whisper model {model_name} (int8, {threads} threads)…")
        self.model = WhisperModel(model_name, device="cpu", compute_type="int8",
                                  cpu_threads=threads, download_root=str(MODEL_DIR))
        self.base = dict(language="en", beam_size=beam, word_timestamps=True, condition_on_previous_text=False)
        if "hotwords" in inspect.signature(self.model.transcribe).parameters:
            self.base["hotwords"] = self.hotwords
        else:  # older faster-whisper
            self.base["initial_prompt"] = self.hotwords
        log("Decoding audio…")
        audio = load_audio(job.audio)
        if audio is None:
            log("  ffmpeg not found; using PyAV instead (sudo apt install ffmpeg is the sturdier path)")
            _patch_pyav()
            from faster_whisper.audio import decode_audio
            audio = decode_audio(str(job.audio), sampling_rate=16000)
        self.audio = audio

    @staticmethod
    def _seg(s, offset: float = 0.0, **extra) -> dict:
        seg = {
            "start": round(s.start + offset, 3), "end": round(s.end + offset, 3), "text": s.text.strip(),
            "avg_logprob": round(s.avg_logprob, 3), "no_speech_prob": round(s.no_speech_prob, 3),
            "words": [{"word": w.word, "start": round(w.start + offset, 3), "end": round(w.end + offset, 3),
                       "prob": round(w.probability, 3)} for w in (s.words or [])],
        }
        seg.update(extra)
        return seg

    def full(self):
        segments, info = self.model.transcribe(self.audio, vad_filter=True,
                                               vad_parameters={"min_silence_duration_ms": 500}, **self.base)
        return segments, info

    def window(self, start: float, end: float) -> list[dict]:
        """Re-listen to one stretch with VAD off (VAD is the usual reason Whisper skips speech)."""
        a, b = max(0, int(start * 16000)), min(len(self.audio), int(end * 16000))
        segments, _ = self.model.transcribe(self.audio[a:b], vad_filter=False, **self.base)
        return [self._seg(s, offset=a / 16000, repair=True) for s in segments]


def do_transcribe(job: Job, engine_args: tuple, force: bool = False) -> Engine | None:
    out = job.dir / "whisper.json"
    if out.exists() and not force:
        log(f"Whisper transcript already exists ({out.name}); use --force to redo it.")
        return None
    if not job.audio or not job.audio.exists():
        raise SystemExit("No audio file yet. Run fetch first, or pass --audio.")
    eng = Engine(job, *engine_args)
    t0 = time.time()
    segments, info = eng.full()
    dur = info.duration or 1
    log(f"Transcribing {fmt_ts(dur)} of audio. Progress every 30 seconds; Ctrl-C is safe (just rerun).")
    partial = job.dir / "whisper.partial.jsonl"
    segs = []
    last = time.time()
    with open(partial, "w", encoding="utf-8") as fh:
        for s in segments:
            seg = eng._seg(s)
            segs.append(seg)
            fh.write(json.dumps(seg) + "\n")
            if time.time() - last > 30:
                pct = s.end * 100 / dur
                speed = s.end / max(1, time.time() - t0)
                left = (dur - s.end) / max(0.01, speed)
                log(f"  {fmt_ts(s.end)} / {fmt_ts(dur)}  ({pct:.0f}%, {speed:.2f}x realtime, ~{fmt_ts(left)} left)")
                last = time.time()
    elapsed = time.time() - t0
    data = {
        "model": eng.model_name, "threads": eng.threads, "beam": eng.beam, "audio": str(job.audio),
        "duration": dur, "elapsed": round(elapsed, 1), "hotwords": eng.hotwords,
        "created": datetime.now().isoformat(timespec="seconds"), "segments": segs,
    }
    save_whisper(job, data)
    partial.unlink(missing_ok=True)
    log(f"Whisper done in {fmt_ts(elapsed)} ({dur / max(1, elapsed):.2f}x realtime).")
    return eng


def save_whisper(job: Job, data: dict) -> None:
    data["segments"].sort(key=lambda s: s["start"])
    (job.dir / "whisper.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    (job.dir / "whisper.txt").write_text(
        "\n".join(f"[{fmt_ts(s['start'])}]{' [re-listened]' if s.get('repair') else ''} {s['text']}"
                  for s in data["segments"]) + "\n", encoding="utf-8")


def _n(w: dict) -> str:
    return " ".join(norm_tokens(w["word"]))


def _edge_overlap(known: list[str], new: list[str], leading: bool) -> int:
    """How many words at the start (leading) or end of `new` repeat the neighbouring `known` words."""
    best = 0
    for k in range(1, min(len(known), len(new)) + 1):
        if leading and new[:k] == known[-k:]:
            best = k
        if not leading and new[-k:] == known[:k]:
            best = k
    return best


def do_repair(job: Job, engine_args: tuple, eng: Engine | None = None, force: bool = False) -> None:
    """Second pass: wherever Riverside has real speech and Whisper left a silence, listen again."""
    wpath = job.dir / "whisper.json"
    if not wpath.exists() or not job.riverside or not job.riverside.exists():
        return
    wdata = json.loads(wpath.read_text(encoding="utf-8"))
    if wdata.get("repair_done") and not force:
        return
    # start clean: drop earlier re-listen results and un-retire words
    wdata["segments"] = [sg for sg in wdata["segments"] if not sg.get("repair")]
    for sg in wdata["segments"]:
        for w in sg["words"]:
            w.pop("suppressed", None)
    doc = load_riverside(job.riverside)
    _, diffs, _ = sync(doc, wdata)
    wins = []
    for d in diffs:
        if d.minor or d.gap is None or d.r_tokens < 4:
            continue
        g0, g1 = d.gap
        if g1 - g0 < 1.5:
            continue
        wins.append([g0, g1, [d]])
    wins.sort(key=lambda w: w[0])
    merged: list[list] = []
    for w in wins:
        if merged and w[0] <= merged[-1][1] + 1.0:
            merged[-1][1] = max(merged[-1][1], w[1])
            merged[-1][2] += w[2]
        else:
            merged.append(w)

    repairs = []
    if merged:
        log(f"Re-listening to {len(merged)} stretch(es) where Riverside has speech and Whisper has none…")
        eng = eng or Engine(job, *engine_args)
        existing = sorted((w for s in wdata["segments"] for w in s["words"] if not w.get("suppressed")),
                          key=lambda w: w["start"])
        for g0, g1, ds in merged:
            r_text = " ".join(d.riverside for d in ds)
            r_norm = [t for x in r_text.split() for t in norm_tokens(x)]
            segs = eng.window(max(0.0, g0 - 1.0), min(g1 + 1.0, g0 + 120))
            for sg in segs:
                sg["words"] = [w for w in sg["words"] if g0 - 0.3 <= (w["start"] + w["end"]) / 2 <= g1 + 0.3]
            segs = [sg for sg in segs if sg["words"]]
            # The re-listen overlaps the edges; drop words the first pass already has on either side.
            before = [w for w in existing if w["end"] <= g0 + 0.3 and w["prob"] >= 0.1][-6:]
            after = [w for w in existing if w["start"] >= g1 - 0.3 and w["prob"] >= 0.1][:6]
            flat = [w for sg in segs for w in sg["words"]]
            nb = _edge_overlap([_n(w) for w in before], [_n(w) for w in flat], leading=True)
            na = _edge_overlap([_n(w) for w in after], [_n(w) for w in flat[nb:]], leading=False)
            keep = flat[nb:len(flat) - na] if na else flat[nb:]
            keep_ids = {id(w) for w in keep}
            for sg in segs:
                sg["words"] = [w for w in sg["words"] if id(w) in keep_ids]
                sg["text"] = _span_text(w["word"] for w in sg["words"])
            segs = [sg for sg in segs if sg["words"]]
            rec_norm = [t for w in keep for t in norm_tokens(w["word"])]
            ratio = difflib.SequenceMatcher(None, rec_norm, r_norm, autojunk=False).ratio() if rec_norm else 0.0
            ok = len(keep) >= 3 and ratio >= 0.35
            text = _span_text(w["word"] for w in keep)
            repairs.append({"start": round(g0, 2), "end": round(g1, 2), "accepted": ok, "match": round(ratio, 2),
                            "whisper": text, "riverside": r_text})
            if ok:
                # the first pass often parks one stretched, near-zero-confidence word in the hole; retire it
                for w in existing:
                    if w["prob"] < 0.1 and w["start"] < g1 and w["end"] > g0 - 0.5:
                        w["suppressed"] = True
                wdata["segments"] += segs
            log(f"  {fmt_ts(g0)}–{fmt_ts(g1)}: {'recovered ' + str(len(keep)) + ' words' if ok else 'nothing usable, left for you to check'}")
    wdata["repairs"] = repairs
    wdata["repair_done"] = True
    save_whisper(job, wdata)


# ---------------------------------------------------------------------------
# Google (Gemini) pass: an independent third opinion
# ---------------------------------------------------------------------------

GEMINI_SYSTEM = """You are a verbatim transcription engine for a two-person podcast interview.
Write down what is said, in order, exactly as the speakers said it.

Rules:
1. Clean verbatim. Write every word that carries meaning exactly as it was said, including casual
   forms ("gonna", "kinda") and grammar mistakes. Leave out only filler sounds (um, uh, mm-hmm),
   stutters, and abandoned false starts: "I I I think" becomes "I think", and "the hippocam, the
   hippocampus" becomes "the hippocampus". Do not summarize, paraphrase, reorder, swap in "better"
   words, or skip anything, even if it seems unimportant.
2. Never add words that were not spoken. If a word or phrase is unclear, write [unintelligible].
   If there is music or no speech, write [music] or nothing.
3. The glossary and episode notes are for spelling only. Use them to spell names and terms
   correctly when you hear them. Never insert a glossary word that was not actually said.
4. Plain text only: natural paragraphs, no speaker labels, no timestamps, no headings, no commentary.
5. The clip may begin or end mid-sentence. Transcribe exactly what is in the clip."""


def gemini_key() -> str | None:
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(var):
            return os.environ[var].strip()
    for f in (ROOT / ".env", Path.home() / "aiscripts" / ".env", Path.home() / ".env"):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
            for line in lines:
                m = re.match(r"\s*(?:export\s+)?(GEMINI_API_KEY|GOOGLE_API_KEY)\s*=\s*['\"]?([^'\"\s#]+)", line)
                if m:
                    return m.group(2)
            # a bare key pasted on its own line in whispersync's own .env (AI Studio keys start AQ. or AIza)
            if f == ROOT / ".env":
                for line in lines:
                    if re.fullmatch(r"\s*(AQ\.|AIza)[\w.\-]{20,}\s*", line):
                        return line.strip()
        except OSError:
            continue
    return None


def ensure_description(job: Job) -> str:
    """Episode notes give Gemini correct spellings (book titles, sites). Pull them from the feed if missing."""
    if job.meta.get("description") is None and job.meta.get("audio_url"):
        try:
            for ep in load_feed():
                if ep.title == job.meta.get("title"):
                    job.meta["description"] = ep.description
                    break
            else:
                job.meta["description"] = ""
        except Exception:
            job.meta["description"] = ""
        job.save()
    ctx = job.dir / "context.txt"  # optional: anything else worth knowing (intake notes, names)
    extra = ctx.read_text(encoding="utf-8") if ctx.exists() else ""
    return "\n\n".join(x for x in (job.meta.get("description") or "", extra) if x.strip())


def plan_chunks(duration: float, wdata: dict | None, target: float) -> list[tuple[float, float]]:
    """Cut points every ~target seconds, nudged to the widest pause nearby so no word is split."""
    if duration <= target * 1.25:
        return [(0.0, duration)]
    words = sorted((w for sg in (wdata or {}).get("segments", []) for w in sg.get("words", [])),
                   key=lambda w: w["start"])
    cuts, t = [], target
    while t < duration - target * 0.25:
        best = t
        if words:
            window = [(b["start"] - a["end"], (a["end"] + b["start"]) / 2) for a, b in zip(words, words[1:])
                      if t - 45 <= a["end"] <= t + 45]
            if window:
                best = max(window)[1]
        cuts.append(best)
        t = best + target
    edges = [0.0] + cuts + [duration]
    return list(zip(edges, edges[1:]))


def cut_audio(src: Path, start: float, end: float) -> bytes:
    import subprocess
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", str(src),
           "-ac", "1", "-ar", "16000", "-b:a", "64k", "-f", "mp3", "-"]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def _looping(words: list[str], times: int = 5) -> bool:
    """A model stuck in a loop repeats a phrase back to back, over and over. People repeat themselves
    too ("Where are the benzos? Where are the benzos? Where are the benzos?"), so it takes five in a row."""
    ws = [re.sub(r"[^\w']", "", w.lower()) for w in words]
    n = len(ws)
    for L in range(3, 13):
        for i in range(0, n - times * L + 1):
            first = ws[i:i + L]
            if all(ws[i + k * L:i + (k + 1) * L] == first for k in range(1, times)):
                return True
    return False


def do_google(job: Job, model: str = GEMINI_MODEL, force: bool = False) -> bool:
    """Transcribe the episode with Gemini, in ~10-minute pieces, and save gemini.json."""
    out = job.dir / "gemini.json"
    if out.exists() and not force:
        log(f"Gemini transcript already exists ({out.name}); use --force-google to redo it.")
        return True
    key = gemini_key()
    if not key:
        log("Google pass skipped: no GEMINI_API_KEY (see README, 'Google pass').")
        return False
    if not shutil.which("ffmpeg"):
        log("Google pass skipped: it needs ffmpeg to cut the audio (sudo apt install ffmpeg).")
        return False
    try:
        from google import genai
        from google.genai import types, errors
    except ImportError:
        log("Google pass skipped: google-genai isn't installed. Rerun ./setup.sh.")
        return False
    if not job.audio or not job.audio.exists():
        raise SystemExit("No audio file yet. Run fetch first, or pass --audio.")

    wpath = job.dir / "whisper.json"
    wdata = json.loads(wpath.read_text(encoding="utf-8")) if wpath.exists() else None
    duration = (wdata or {}).get("duration") or float(load_audio(job.audio).shape[0]) / 16000
    chunks = plan_chunks(duration, wdata, GEMINI_CHUNK)
    wwords = sorted((w for sg in (wdata or {}).get("segments", []) for w in sg.get("words", [])
                     if not w.get("suppressed")), key=lambda w: w["start"])

    glossary = load_glossary(job)
    notes = ensure_description(job)
    client = genai.Client(api_key=key)
    cfg = dict(system_instruction=GEMINI_SYSTEM, max_output_tokens=16384)
    if hasattr(types, "AutomaticFunctionCallingConfig"):  # no tools here; also silences an SDK warning
        cfg["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
    config = types.GenerateContentConfig(**cfg)
    log(f"Google pass: {len(chunks)} piece(s) of ~{GEMINI_CHUNK // 60} min with {model}…")

    def ask(audio: bytes, prompt: str):
        delay = 15
        for attempt in range(6):
            try:
                return client.models.generate_content(
                    model=model, config=config,
                    contents=[types.Part.from_bytes(data=audio, mime_type="audio/mp3"), prompt])
            except errors.APIError as e:
                code = getattr(e, "code", 0) or 0
                if code in (400, 401, 403) and re.search(r"api.?key|permission|unauth", str(e), re.I):
                    raise SystemExit(
                        f"Google turned the key down ({code}): {str(e)[:200]}\n"
                        "Check the key in .env. New AI Studio keys start with 'AQ.'; if one of those is refused, "
                        "update Google's library (.venv/bin/pip install -U google-genai) and try again.")
                if code in (429, 500, 502, 503, 504) and attempt < 5:
                    log(f"    Google said {code}; waiting {delay}s and trying again")
                    time.sleep(0 if os.environ.get("WS_TEST_NOSLEEP") else delay)
                    delay = min(delay * 2, 120)
                    continue
                raise

    results, versions = [], set()
    t0 = time.time()
    for n, (a, b) in enumerate(chunks, 1):
        audio = cut_audio(job.audio, a, b)
        expected = sum(1 for w in wwords if a <= w["start"] < b) or None
        prompt = (f"Glossary (spelling only): {', '.join([HOST_NAME] + glossary)}\n\n"
                  + (f"Episode notes (spelling only):\n{notes}\n\n" if notes else "")
                  + f"This is part {n} of {len(chunks)}, from {fmt_ts(a)} to {fmt_ts(b)} of the episode. "
                    "Transcribe it verbatim.")
        best = None
        for attempt in (1, 2):
            resp = ask(audio, prompt)
            text = (resp.text or "").strip()
            versions.add(getattr(resp, "model_version", None) or model)
            nwords = len(text.split())
            ratio = (nwords / expected) if expected else 1.0
            suspect = _looping(text.split()) or (expected and (ratio < 0.7 or ratio > 1.6))
            cand = {"start": round(a, 2), "end": round(b, 2), "text": text, "words": nwords,
                    "whisper_words": expected, "ratio": round(ratio, 2), "attempts": attempt, "suspect": bool(suspect)}
            if best is None or abs(ratio - 1) < abs(best["ratio"] - 1):
                best = cand
            if not suspect:
                break
            log(f"    part {n} looks off ({nwords} words vs ~{expected} from Whisper); asking once more")
        results.append(best)
        log(f"  part {n}/{len(chunks)} {fmt_ts(a)}–{fmt_ts(b)}: {best['words']} words"
            + (" (still looks off; its spots will be marked)" if best["suspect"] else ""))
    data = {"model_requested": model, "model_version": ", ".join(sorted(v for v in versions if v)),
            "created": datetime.now().isoformat(timespec="seconds"), "elapsed": round(time.time() - t0, 1),
            "chunks": results}
    out.write_text(json.dumps(data, indent=1), encoding="utf-8")
    (job.dir / "gemini.txt").write_text(
        "\n\n".join(f"[{fmt_ts(c['start'])}]\n{c['text']}" for c in results) + "\n", encoding="utf-8")
    log(f"Google pass done in {fmt_ts(data['elapsed'])} ({data['model_version']}).")
    return True


# ---------------------------------------------------------------------------
# Sync: align Whisper's words onto Riverside's turns
# ---------------------------------------------------------------------------


@dataclass
class RTok:
    norm: str
    turn: int
    word: int  # index into the turn's display words


@dataclass
class WTok:
    norm: str
    word: int  # index into whisper word list


@dataclass
class Diff:
    time: float
    speaker: str
    turn: int
    riverside: str
    whisper: str
    kind: str        # names | numbers | missing-in-riverside | missing-in-whisper | words
    unsure: bool     # Whisper's own confidence was low
    r_before: str
    r_after: str
    w_before: str
    w_after: str
    minor: bool
    gap: tuple | None = None   # (start, end) of the silence Whisper left, for Riverside-only spans
    r_tokens: int = 0
    wt: tuple = (0, 0)         # whisper token span
    wspan: tuple = (0, 0)      # whisper word span
    rt: tuple = (0, 0)         # riverside token span
    gemini: str | None = None  # what Gemini heard here (None = no Google pass)
    g_before: str = ""
    g_after: str = ""
    verdict: str = ""          # whisper+gemini | riverside+gemini | split


def build_tokens(doc: RiversideDoc, words: list[dict]):
    r_disp = [t.text.split() for t in doc.turns]
    rtoks: list[RTok] = []
    for ti, disp in enumerate(r_disp):
        for wi, w in enumerate(disp):
            for n in norm_tokens(w):
                rtoks.append(RTok(n, ti, wi))
    wtoks: list[WTok] = []
    for wi, w in enumerate(words):
        for n in norm_tokens(w["word"]):
            wtoks.append(WTok(n, wi))
    return r_disp, rtoks, wtoks


def _span_text(items) -> str:
    return re.sub(r"\s+", " ", "".join(items)).strip()


COMMON_CAPS = {"i", "im", "ive", "id", "ill", "and", "but", "so", "the", "a", "we", "you", "it", "oh",
               "yeah", "well", "now", "that", "ok", "okay", "yes", "no", "my", "god"}
UNKNOWN_SPEAKER = "Unknown speaker"


def _is_fragment(tok: str, near: set[str]) -> bool:
    """Cut-off word Riverside kept ("hippocam", "underst", "f"): a stub of a word said right next to it."""
    if tok in FUNCTION or tok in _ONES or tok in _TENS or tok in NEGATIONS:
        return False
    if len(tok) <= 2 and tok.isalpha():
        return True
    inflections = {"s", "es", "ed", "d", "ing", "er", "ers", "ly", "y"}
    return any(len(n) > len(tok) and n.startswith(tok) and n[len(tok):] not in inflections for n in near)


def _stem(t: str) -> str:
    for suf in ("ing", "ed", "es", "s", "d"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def _same_stem(a: str, b: str) -> bool:
    if _stem(a) == _stem(b):
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 4 and long_.startswith(short) and len(long_) - len(short) <= 3


class Classifier:
    """Decides how much a disagreement matters. Built once per transcript."""

    def __init__(self, r_display: list[str], w_display: list[str]):
        # Words that show up lowercase somewhere are ordinary words, not names,
        # even when one tool capitalizes them ("Gut", "That's", "Sugar").
        self.lower_seen: set[str] = set()
        for w in r_display + w_display:
            core = re.sub(r"^[^\w]+|[^\w]+$", "", w)
            if re.fullmatch(r"[a-z][a-z']*", core):  # plain words only; skip urls like jane-doerr.com
                self.lower_seen.update(norm_tokens(core))

    def has_name(self, words: list[str]) -> bool:
        for w in words:
            core = re.sub(r"[^\w'\-]", "", w)
            if not core or not core[0].isupper():
                continue
            toks = norm_tokens(core)
            if not toks or all(t in COMMON_CAPS or t in BACKCHANNEL or t in self.lower_seen or len(t) <= 2
                               for t in toks):
                continue
            return True
        return False

    def classify(self, r_norm, w_norm, r_words, w_words, r_near, w_near) -> tuple[str, bool]:
        """Return (kind, minor). r_near / w_near: tokens just around the span on each side."""
        near = set(r_near) | set(w_near)  # context only, not the disagreeing words themselves
        rn = [t for t in r_norm if t not in SPOKEN_PUNCT and not _is_fragment(t, near)]
        wn = [t for t in w_norm if t not in SPOKEN_PUNCT and not _is_fragment(t, near)]
        if "".join(rn) == "".join(wn):
            return "words", True  # spacing/compounding only: "health care" vs "healthcare"
        diff_r = set(rn) - set(wn)
        diff_w = set(wn) - set(rn)
        changed = diff_r | diff_w
        named = self.has_name(w_words) or self.has_name(r_words)
        numeric = bool(re.search(r"\d", " ".join(w_words + r_words))) or any(
            (t in _ONES and t != "one") or t in _TENS or t in {"hundred", "thousand", "percent"} for t in changed)
        negation = bool(changed & NEGATIONS)

        # false starts, even with names or numbers in them ("ten alarm fire, you know, ten alarm fire")
        if not wn and rn and {t for t in rn if t not in FUNCTION} <= set(r_near):
            return "missing-in-whisper", True
        if not rn and wn and {t for t in wn if t not in FUNCTION} <= set(w_near):
            return "missing-in-riverside", True
        if not named and not numeric and not negation:
            if changed <= FUNCTION:
                return "words", True  # "It was" vs "It's", ums, stutters
            if len(diff_r) == 1 and len(diff_w) == 1 and _same_stem(next(iter(diff_r)), next(iter(diff_w))):
                return "words", True  # struggled / struggle, symptom / symptoms
            # false starts: every meaningful word on the short side is repeated right next to it
            if not wn and {t for t in rn if t not in FUNCTION} <= set(r_near):
                return "missing-in-whisper", True
            if not rn and {t for t in wn if t not in FUNCTION} <= set(w_near):
                return "missing-in-riverside", True
            if not wn and set(rn) <= (FUNCTION | BACKCHANNEL):
                return "missing-in-whisper", True
            if not rn and set(wn) <= (FUNCTION | BACKCHANNEL):
                return "missing-in-riverside", True

        if not rn:
            if len(wn) >= 6:
                return "missing-in-riverside", False
            return ("names" if named else "numbers" if numeric else "missing-in-riverside"), False
        if not wn:
            if len(rn) >= 6:
                return "missing-in-whisper", False
            return ("names" if named else "numbers" if numeric else "missing-in-whisper"), False
        if numeric:
            return "numbers", False
        if named:
            return "names", False
        return "words", False


def _find_gap(words: list[dict], k: int) -> tuple[float, float]:
    """The silence Whisper left around word index k (where Riverside-only speech sits).
    Whisper often parks one stretched, low-confidence word at the edge of a skip, so look a few words
    either side for the real hole."""
    g0 = words[k - 1]["end"] if k > 0 else 0.0
    g1 = words[k]["start"] if k < len(words) else words[-1]["end"]
    for j in range(k, min(len(words) - 1, k + 4)):          # forward
        if words[j + 1]["start"] - words[j]["end"] >= 1.5:
            g1 = max(g1, words[j + 1]["start"])
            break
    for j in range(k - 1, max(0, k - 5), -1):                # backward
        if j - 1 >= 0 and words[j]["start"] - words[j - 1]["end"] >= 1.5:
            g0 = min(g0, words[j - 1]["end"])
            break
    return g0, g1


def _join_words(idx: list[int], words: list[dict], ov: dict | None = None) -> str:
    """Whisper's words as readable text. Segments often end without punctuation, which runs
    sentences together ("low inflammation It's hard"), so close them where the next word is capitalized.
    ov: overrides from the three-way vote: {"prefix": {i: text}, "suffix": {i: text}, "skip": set()}"""
    ov = ov or {"prefix": {}, "suffix": {}, "skip": set()}
    pieces: list[tuple[str, bool]] = []  # (text, closes_a_segment)
    for i in idx:
        if i in ov["prefix"]:
            pieces.append((" " + ov["prefix"][i], False))
        if i not in ov["skip"]:
            pieces.append((words[i]["word"], bool(words[i].get("_seg_end"))))
        if i in ov["suffix"]:
            pieces.append((" " + ov["suffix"][i], False))
    out = []
    for n, (w, seg_end) in enumerate(pieces):
        nxt = pieces[n + 1][0].strip() if n + 1 < len(pieces) else ""
        stripped = w.rstrip()
        if seg_end and stripped and stripped[-1].isalnum() and (not nxt or nxt[:1].isupper()):
            w = stripped + "."
        out.append(w)
    return _span_text(out)


PURE_FILLERS = {"um", "uh", "uhm", "umm", "er", "erm", "ah", "eh", "hmm", "hm", "mm", "mhm", "mmhmm"}


def _core(toks: list[str]) -> str:
    return "".join(t for t in toks if t not in PURE_FILLERS and t not in SPOKEN_PUNCT)


def _agree(a: list[str], b: list[str]) -> bool:
    ca, cb = _core(a), _core(b)
    if ca == cb:
        return True
    if min(len(a), len(b)) >= 4 and ca and cb:  # long stretches: tolerate a stray small word
        return difflib.SequenceMatcher(None, ca, cb, autojunk=False).ratio() >= 0.92
    return False


def gemini_words(gdata: dict) -> list[dict]:
    out = []
    for c in gdata.get("chunks", []):
        for w in re.sub(r"\[(music|unintelligible|inaudible|crosstalk)[^\]]*\]", " \u2026 ", c["text"], flags=re.I).split():
            out.append({"word": w, "suspect": c.get("suspect", False)})
    return out


def reconcile_crosstalk(doc: RiversideDoc, words: list[dict], wtoks: list, rtoks: list, diffs: list,
                        word_turn: list) -> int:
    """Riverside records each speaker on their own track, so its labels survive crosstalk; Whisper hears
    one mixed track and folds an interjection into whoever was talking ("…plus NAC. NAC? Yeah."). When
    words Whisper put in one speaker's turn match words Riverside has only in the other speaker's
    neighbouring turn, give them to Riverside's speaker."""
    moved = 0
    taken: set[int] = set()
    w_diffs = [d for d in diffs if d.wspan[1] > d.wspan[0]]
    pieces = []  # (diff, turn, i1, i2): Riverside-only stretches, split where Riverside's turn changes
    for r in diffs:
        i1, i2 = r.rt
        if i2 <= i1 or r.wspan[1] > r.wspan[0]:
            continue  # only stretches Riverside has and Whisper doesn't
        start = i1
        for i in range(i1 + 1, i2 + 1):
            if i == i2 or rtoks[i].turn != rtoks[start].turn:
                pieces.append((r, rtoks[start].turn, start, i))
                start = i
    for r, b, i1, i2 in pieces:
        r_core = _core([rtoks[i].norm for i in range(i1, i2)])
        if len(r_core) < 2:
            continue
        best = None
        for w in w_diffs:
            if abs(w.time - r.time) > 30:
                continue
            for a0 in range(w.wspan[0], w.wspan[1]):
                if a0 in taken:
                    continue
                for a1 in range(a0 + 1, min(w.wspan[1], a0 + (i2 - i1) + 4) + 1):
                    run = list(range(a0, a1))
                    if any(x in taken for x in run):
                        break
                    a_turns = {word_turn[x] for x in run}
                    if len(a_turns) != 1:
                        break
                    a = a_turns.pop()
                    if a < 0 or a == b or abs(a - b) > 2 or doc.turns[a].speaker == doc.turns[b].speaker:
                        break
                    w_core = _core([n for x in run for n in norm_tokens(words[x]["word"])])
                    if not w_core:
                        continue
                    ratio = 1.0 if w_core == r_core else (
                        difflib.SequenceMatcher(None, w_core, r_core, autojunk=False).ratio() if len(r_core) >= 6 else 0.0)
                    if ratio >= 0.85 and (best is None or ratio > best[0] or
                                          (ratio == best[0] and abs(words[a0]["start"] - r.time) < abs(words[best[1][0]]["start"] - r.time))):
                        best = (ratio, run, w)
        if best:
            _, run, w = best
            for x in run:
                word_turn[x] = b
                taken.add(x)
            if len(pieces) and sum(1 for pc in pieces if pc[0] is r) == 1:
                r.minor, r.verdict = True, "crosstalk"
            if set(run) >= set(range(*w.wspan)):
                w.minor, w.verdict = True, "crosstalk"
            moved += len(run)
    return moved


def vote(doc: RiversideDoc, wtoks: list, words: list[dict], rtoks: list, diffs: list, word_turn: list, gdata: dict):
    """Third opinion. For each real disagreement, see what Gemini heard over the same words.
    Two engines agreeing settles it; the transcript takes the winning words."""
    gw = gemini_words(gdata)
    gtoks = [(n, gi) for gi, w in enumerate(gw) for n in norm_tokens(w["word"])]
    sm = difflib.SequenceMatcher(None, [t.norm for t in wtoks], [n for n, _ in gtoks], autojunk=False)
    nW = len(wtoks)
    B = [0] * (nW + 1)
    ins: dict[int, tuple[int, int]] = {}
    for tag, a1, a2, b1, b2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(a2 - a1):
                B[a1 + k] = b1 + k
        elif tag == "replace":
            for k in range(a1, a2):
                B[k] = b1 + (k - a1) * (b2 - b1) // (a2 - a1)
        elif tag == "delete":
            for k in range(a1, a2):
                B[k] = b1
        else:  # insert: Gemini has words Whisper doesn't, at boundary a1
            ins[a1] = (b1, b2)
    B[nW] = len(gtoks)
    lo = lambda q: ins[q][0] if q in ins else B[q]
    hi = lambda q: ins[q][1] if q in ins else B[q]

    ov = {"prefix": {}, "suffix": {}, "skip": set(), "turn_fill": {}}
    counts = {"whisper+gemini": 0, "riverside+gemini": 0, "split": 0}
    for d in diffs:
        if d.minor:
            continue
        j1, j2 = d.wt
        gs, ge = (hi(j1), lo(j2)) if j2 > j1 else (lo(j1), hi(j1))
        ge = max(gs, ge)
        # widen to whole display words ("jane-doerr.com." is three tokens but one word on the page)
        while gs < ge < len(gtoks) and gtoks[ge][1] == gtoks[ge - 1][1]:
            ge += 1
        while 0 < gs < ge and gtoks[gs - 1][1] == gtoks[gs][1]:
            gs -= 1
        g_norm = [gtoks[k][0] for k in range(gs, ge)]
        gi0 = gtoks[gs][1] if gs < len(gtoks) else len(gw)
        gi1 = gtoks[ge - 1][1] + 1 if ge > gs else gi0
        d.gemini = " ".join(w["word"] for w in gw[gi0:gi1])
        d.g_before = " ".join(w["word"] for w in gw[max(0, gi0 - 8):gi0])
        d.g_after = " ".join(w["word"] for w in gw[gi1:gi1 + 8])
        suspect = any(w["suspect"] for w in gw[max(0, gi0 - 1):gi1 + 1])
        r_norm = [rtoks[i].norm for i in range(*d.rt)]
        w_norm = [wtoks[j].norm for j in range(j1, j2)]
        if not suspect and _agree(g_norm, w_norm) and not _agree(r_norm, w_norm):
            d.verdict, d.minor = "whisper+gemini", True
        elif not suspect and _agree(g_norm, r_norm) and not _agree(r_norm, w_norm):
            d.verdict, d.minor = "riverside+gemini", True
            wa, wb = d.wspan
            text = d.gemini or ""
            # Gemini's stretch can run into the words on either side ("jane-doerr.com." next to
            # Whisper's ".com,"); drop those neighbours so nothing is said twice.
            tail = _edge_overlap([wtoks[k].norm for k in range(j2, min(nW, j2 + 4))], g_norm, leading=False)
            head = _edge_overlap([wtoks[k].norm for k in range(max(0, j1 - 4), j1)], g_norm, leading=True)
            if g_norm and (tail or head):
                extra = {wtoks[k].word for k in range(j2, j2 + tail)} | {wtoks[k].word for k in range(j1 - head, j1)}
                extra = {wi for wi in extra if word_turn[wi] == d.turn}
                if extra:
                    lo_w, hi_w = min([wa] + list(extra)), max([wb - 1] + list(extra)) + 1
                    wa, wb = lo_w, hi_w
            if wb > wa:                       # replace Whisper's words
                if text:
                    ov["prefix"][wa] = text
                ov["skip"].update(range(wa, wb))
            elif text and not ({t for t in g_norm if t not in FUNCTION}
                               <= {wtoks[k].norm for k in range(max(0, j1 - 12), min(nW, j1 + 12))}):
                # Whisper had nothing here: add the words (unless they just repeat what's next to them)
                if wa < len(words) and word_turn[wa] == d.turn:
                    ov["prefix"][wa] = (ov["prefix"].get(wa, "") + " " + text).strip()
                elif wa > 0 and word_turn[wa - 1] == d.turn:
                    ov["suffix"][wa - 1] = (ov["suffix"].get(wa - 1, "") + " " + text).strip()
                else:
                    ov["turn_fill"][d.turn] = (ov["turn_fill"].get(d.turn, "") + " " + text).strip()
        else:
            d.verdict = "split"
        counts[d.verdict] += 1
    return ov, counts


def sync(doc: RiversideDoc, wdata: dict, gdata: dict | None = None):
    words = []
    for seg in wdata["segments"]:
        ws = [dict(w) for w in seg.get("words", []) if not w.get("suppressed")]
        if ws:
            ws[-1]["_seg_end"] = True
        words += ws
    words.sort(key=lambda w: w["start"])
    r_disp, rtoks, wtoks = build_tokens(doc, words)
    r_flat = [(ti, wi) for ti, disp in enumerate(r_disp) for wi in range(len(disp))]
    r_pos = {tw: g for g, tw in enumerate(r_flat)}
    r_word = lambda g: r_disp[r_flat[g][0]][r_flat[g][1]]
    clf = Classifier([r_word(g) for g in range(len(r_flat))], [w["word"].strip() for w in words])
    sm = difflib.SequenceMatcher(None, [t.norm for t in rtoks], [t.norm for t in wtoks], autojunk=False)
    ops = sm.get_opcodes()

    word_turn = [-1] * len(words)
    extra_turns: list[tuple[int, list[int], str]] = []  # (before turn index, whisper word idxs, label)
    diffs: list[Diff] = []
    matched = 0

    def assign(wi: int, ti: int):
        if word_turn[wi] == -1:
            word_turn[wi] = ti

    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            matched += i2 - i1
            for k in range(i2 - i1):
                assign(wtoks[j1 + k].word, rtoks[i1 + k].turn)
            continue

        at_boundary = i1 == 0 or i1 >= len(rtoks) or rtoks[i1].turn != rtoks[i1 - 1].turn
        bumper = False
        if tag == "insert" and j2 - j1 >= 8 and at_boundary:
            ins_text = " ".join(wtoks[k].norm for k in range(j1, j2))
            t_ins = words[wtoks[j1].word]["start"]
            total = words[-1]["end"] if words else 0
            bumper = "eager to be healthy" in ins_text and (t_ins < 120 or t_ins > total - 180)
            # A stretch Riverside never transcribed, between two turns (intro voiceover, a missed reply).
            idx = sorted({wtoks[k].word for k in range(j1, j2)})
            for wi in idx:
                word_turn[wi] = -2
            before = rtoks[i1].turn if i1 < len(rtoks) else len(doc.turns)
            extra_turns.append((before, idx, ("Show intro" if before == 0 or t_ins < 120 else "Show outro") if bumper else UNKNOWN_SPEAKER))
        elif tag in ("replace", "insert"):
            for k in range(j1, j2):
                if tag == "replace":
                    ri = i1 + (k - j1) * (i2 - i1) // max(1, j2 - j1)
                    ti = rtoks[min(ri, i2 - 1)].turn
                else:
                    ti = rtoks[i1 - 1].turn if i1 > 0 else (rtoks[0].turn if rtoks else 0)
                assign(wtoks[k].word, ti)

        # Riverside side of the disagreement (global word positions)
        r_g = sorted({r_pos[(rtoks[i].turn, rtoks[i].word)] for i in range(i1, i2)})
        if r_g:
            rg0, rg1 = r_g[0], r_g[-1] + 1
        else:  # nothing on Riverside's side: anchor between neighbours
            rg0 = rg1 = (r_pos[(rtoks[i1].turn, rtoks[i1].word)] if i1 < len(rtoks) else len(r_flat))
        r_words = [r_word(g) for g in range(rg0, rg1)]
        # Whisper side
        w_idx = sorted({wtoks[j].word for j in range(j1, j2)})
        if w_idx:
            wg0, wg1 = w_idx[0], w_idx[-1] + 1
        else:
            wg0 = wg1 = wtoks[j1].word if j1 < len(wtoks) else len(words)
        w_words = [words[i]["word"].strip() for i in range(wg0, wg1)]

        r_near = [t.norm for t in rtoks[max(0, i1 - 10):i1] + rtoks[i2:i2 + 10]]
        w_near = [t.norm for t in wtoks[max(0, j1 - 10):j1] + wtoks[j2:j2 + 10]]
        kind, minor = clf.classify([rtoks[i].norm for i in range(i1, i2)], [wtoks[j].norm for j in range(j1, j2)],
                                   r_words, w_words, r_near, w_near)
        if bumper:
            kind, minor = "missing-in-riverside", True  # the show's intro/outro voiceover
        tref = words[min(wg0, len(words) - 1)]["start"] if words else 0.0
        unsure = bool(w_idx) and min(words[i]["prob"] for i in w_idx) < 0.5
        turn = r_flat[min(rg0, len(r_flat) - 1)][0] if r_flat else 0
        gap = None
        if not w_idx and words:
            gap = _find_gap(words, wg0)
        diffs.append(Diff(
            time=tref, turn=turn,
            speaker=(extra_turns[-1][2] if (w_idx and word_turn[w_idx[0]] == -2) else (doc.turns[turn].speaker if doc.turns else "")),
            riverside=" ".join(r_words), whisper=" ".join(w_words), kind=kind, unsure=unsure,
            r_before=" ".join(r_word(g) for g in range(max(0, rg0 - 8), rg0) if r_word(g) != "~"),
            r_after=" ".join(r_word(g) for g in range(rg1, min(len(r_flat), rg1 + 8)) if r_word(g) != "~"),
            w_before=_span_text(w["word"] for w in words[max(0, wg0 - 8):wg0]),
            w_after=_span_text(w["word"] for w in words[wg1:wg1 + 8]),
            minor=minor, gap=gap, r_tokens=i2 - i1, wt=(j1, j2), wspan=(wg0, wg1), rt=(i1, i2)))

    crosstalk_moved = reconcile_crosstalk(doc, words, wtoks, rtoks, diffs, word_turn)
    ov, vote_counts = (vote(doc, wtoks, words, rtoks, diffs, word_turn, gdata) if gdata
                       else ({"prefix": {}, "suffix": {}, "skip": set(), "turn_fill": {}}, None))

    # Build synced turns
    turn_words: dict[int, list[int]] = {}
    for wi, ti in enumerate(word_turn):
        if ti >= 0:
            turn_words.setdefault(ti, []).append(wi)
    extras: dict[int, list[tuple[list[int], str]]] = {}
    for before, idx, label in extra_turns:
        extras.setdefault(before, []).append((idx, label))

    synced: list[Turn] = []
    riverside_only = dropped = crosstalk_kept = 0

    def add(turn: Turn):
        if synced and synced[-1].speaker == turn.speaker and turn.speaker not in (UNKNOWN_SPEAKER, "Show intro", "Show outro"):
            synced[-1].text = (synced[-1].text + " " + turn.text).strip()
        else:
            synced.append(turn)

    for ti in range(len(doc.turns) + 1):
        for idx, label in extras.get(ti, []):
            add(Turn(label, words[idx[0]]["start"], _join_words(idx, words, ov)))
        if ti == len(doc.turns):
            break
        t = doc.turns[ti]
        idx = turn_words.get(ti)
        toks = [n for x in t.text.split() for n in norm_tokens(x)]
        if idx:
            add(Turn(t.speaker, words[idx[0]]["start"], _join_words(idx, words, ov)))
        elif ti in ov["turn_fill"]:
            add(Turn(t.speaker, synced[-1].start if synced else t.start, ov["turn_fill"][ti]))  # Gemini confirmed it
        elif not toks or set(toks) <= (BACKCHANNEL | FUNCTION):
            dropped += 1  # "Mm-hmm." / "Yeah." over the other speaker; Whisper skips these
        elif len(toks) <= 12:
            crosstalk_kept += 1  # short line over the other speaker: Riverside's own track is the authority
            add(Turn(t.speaker, synced[-1].start if synced else t.start, " ".join(x for x in t.text.split() if x != "~")))
        else:
            riverside_only += 1
            add(Turn(t.speaker, synced[-1].start if synced else t.start,
                     "[Riverside only, Whisper missed this] " + " ".join(x for x in t.text.split() if x != "~")))

    stats = {
        "riverside_words": len(rtoks),
        "whisper_words": len(wtoks),
        "matched": matched,
        "agreement": matched / max(1, len(rtoks)),
        "turns": len(doc.turns),
        "turns_without_whisper": riverside_only,
        "backchannel_turns_dropped": dropped,
        "crosstalk_kept": crosstalk_kept,
        "crosstalk_words_moved": crosstalk_moved,
        "unknown_speaker_turns": len(extra_turns),
        "vote": vote_counts,
        "gemini_model": (gdata or {}).get("model_version"),
    }
    return synced, diffs, stats


def repeated_swaps(diffs: list[Diff]) -> list[tuple[str, str, int]]:
    counts: dict[tuple[str, str], int] = {}
    for d in diffs:
        if d.minor or not d.riverside or not d.whisper:
            continue
        rw, ww = d.riverside.split(), d.whisper.split()
        if len(rw) > 4 or len(ww) > 4:
            continue
        # equal-length spans pair up word by word: "Doerrr. Three" / "Doerr. 30"
        pairs = list(zip(rw, ww)) if len(rw) == len(ww) else [(d.riverside, d.whisper)]
        for r, w in pairs:
            key = (re.sub(r"[^\w' ]", "", r).strip(), re.sub(r"[^\w' -]", "", w).strip())
            if not key[0] or not key[1] or key[0].lower() == key[1].lower() or key[0].lower() in FILLERS:
                continue
            counts[key] = counts.get(key, 0) + 1
    return sorted(((r, w, n) for (r, w), n in counts.items() if n >= 2), key=lambda x: -x[2])


KIND_LABEL = {
    "names": "name or proper noun",
    "numbers": "number",
    "missing-in-riverside": "Riverside skipped this",
    "missing-in-whisper": "Whisper didn't hear this",
    "words": "different words",
}
KIND_ORDER = {"names": 0, "numbers": 1, "missing-in-riverside": 2, "missing-in-whisper": 3, "words": 4}


def write_outputs(job: Job, doc: RiversideDoc, synced: list[Turn], diffs: list[Diff], stats: dict,
                  wdata: dict, renames: dict[str, str], max_items: int) -> dict[str, Path]:
    jd = job.dir
    m = job.meta
    title = m.get("title") or m.get("name")

    # 1) synced transcript in Riverside's own layout, so the post skill reads it unchanged
    lines = []
    if doc.header:
        lines += doc.header
    vote_note = f", checked against Gemini ({stats.get('gemini_model')})" if stats.get("vote") else ""
    lines += [f"* whispersync: Riverside speakers + Whisper {wdata.get('model')} words{vote_note}, "
              f"{date.today().isoformat()}", ""]
    for t in synced:
        lines += [f"{t.speaker} ({fmt_ts(t.start)})", t.text, ""]
    synced_path = jd / "transcript-whisper.txt"
    synced_path.write_text("\n".join(lines), encoding="utf-8")

    # 2) review list
    review = [d for d in diffs if not d.minor]
    review.sort(key=lambda d: (KIND_ORDER[d.kind], d.time))
    shown = review[:max_items]
    shown.sort(key=lambda d: d.time)
    swaps = repeated_swaps(diffs)
    counts = {k: sum(1 for d in review if d.kind == k) for k in KIND_ORDER}

    md = [f"# Transcript check: {title}", ""]
    md += [f"- **Audio:** `{Path(wdata.get('audio', '')).name}` · {fmt_ts(wdata.get('duration', 0))}",
           f"- **Whisper:** {wdata.get('model')} · {wdata.get('threads')} threads · took {fmt_ts(wdata.get('elapsed', 0))}",
           f"- **Agreement:** {stats['agreement'] * 100:.1f}% of Riverside's words match Whisper word for word "
           "(Riverside keeps every false start and \"mm-hmm\"; Whisper drops them, so mid-80s to 90s is normal)",
           f"- **To check:** {len(review)} spots ({counts['names']} names, {counts['numbers']} numbers, "
           f"{counts['missing-in-riverside']} Riverside skipped, {counts['missing-in-whisper']} Whisper didn't hear, "
           f"{counts['words']} other). {sum(1 for d in diffs if d.minor)} filler/spacing differences ignored.",
           ]
    vc = stats.get("vote")
    if vc:
        md.append(f"- **Google check ({stats.get('gemini_model')}):** of {sum(vc.values())} real disagreements, "
                  f"Whisper and Gemini agreed on {vc['whisper+gemini']} (settled), Riverside and Gemini agreed on "
                  f"{vc['riverside+gemini']} (the transcript now uses their words), and {vc['split']} are still split. "
                  "Only the split ones are listed below.")
    if renames:
        md.append("- **Speaker labels fixed:** " + "; ".join(f"{a} → {b}" for a, b in renames.items()))
    if stats.get("backchannel_turns_dropped"):
        md.append(f"- **Tidied:** {stats['backchannel_turns_dropped']} \"Mm-hmm\" / \"Yeah\" turns spoken over the other "
                  "person were left out of the synced transcript.")
    if stats.get("crosstalk_words_moved") or stats.get("crosstalk_kept"):
        md.append(f"- **Crosstalk:** Riverside records each speaker separately, so its labels win. "
                  f"{stats.get('crosstalk_words_moved', 0)} words Whisper put in the wrong turn were handed back to "
                  f"Riverside's speaker, and {stats.get('crosstalk_kept', 0)} short lines only Riverside caught are kept "
                  "as Riverside wrote them.")
    if stats["turns_without_whisper"]:
        md.append(f"- **Heads up:** {stats['turns_without_whisper']} Riverside turns have no Whisper words at all. They're kept "
                  "in the synced transcript marked [Riverside only, Whisper missed this]. Worth a listen.")
    if stats.get("unknown_speaker_turns"):
        md.append(f"- **New turns:** {stats['unknown_speaker_turns']} stretch(es) Riverside never transcribed were added "
                  "(show intro/outro voiceover, or \"Unknown speaker\" to label by ear).")
    if stats["agreement"] < 0.6:
        md.append("- **Warning:** agreement is low. Make sure the audio and transcript are the same cut.")
    if vc:
        md += ["", "The synced transcript keeps Riverside's speakers and uses Whisper's words, except where Riverside "
               "and Gemini outvoted it. The spots below are where all three heard something different.", ""]
    else:
        md += ["", "The synced transcript keeps Riverside's speakers and uses Whisper's words. "
               "Whisper is usually right on names and phrasing, but not always, so the spots below are worth a listen.", ""]

    fx = stats.get("fixes") or []
    if fx:
        ok = [f for f in fx if f["ok"]]
        md += [f"## Your fixes ({len(ok)} of {len(fx)} applied)", "",
               "From `fixes.txt` in the episode's work folder.", ""]
        for f in fx:
            md.append(f"- {'✓' if f['ok'] else '✗ ' + f['why'] + ':'} `{f['src']}`")
        md.append("")

    repairs = wdata.get("repairs") or []
    if repairs:
        md += ["## Whisper skipped these the first time", "",
               "Riverside had speech here and Whisper had silence, so whispersync listened to these stretches again.", ""]
        for r in repairs:
            if r["accepted"]:
                md.append(f"- **{fmt_ts(r['start'])}–{fmt_ts(r['end'])}** recovered: \"{r['whisper']}\"")
            else:
                md.append(f"- **{fmt_ts(r['start'])}–{fmt_ts(r['end'])}** still missing, check by ear. Riverside has: \"{r['riverside']}\"")
        md.append("")

    outvoted = [d for d in diffs if d.verdict == "riverside+gemini"]
    if outvoted:
        md += ["## Whisper was outvoted", "",
               "Riverside and Gemini heard the same thing here, so the synced transcript uses their words instead of Whisper's.", ""]
        for d in sorted(outvoted, key=lambda d: d.time):
            md.append(f"- **{fmt_ts(d.time)} · {d.speaker}:** \"{d.gemini or '(nothing)'}\" (Whisper had \"{d.whisper or 'nothing'}\")")
        md.append("")

    if swaps:
        md += ["## Repeated swaps", "", "Same mix-up more than once. Usually a name or term worth fixing everywhere.", "",
               "| Riverside wrote | Whisper heard | Times |", "|---|---|---|"]
        md += [f"| {r} | {w} | {n} |" for r, w, n in swaps[:25]]
        md.append("")

    md += [f"## Spots to check by ear ({len(shown)} of {len(review)})", ""]
    if len(review) > len(shown):
        md += [f"Showing names, numbers, and skipped speech first. The full list is in `review.csv`.", ""]
    for n, d in enumerate(shown, 1):
        flag = " · Whisper unsure" if d.unsure else ""
        md.append(f"**{n}. {fmt_ts(d.time)} · {d.speaker}** · {KIND_LABEL[d.kind]}{flag}")
        md.append(f"- Riverside: …{d.r_before} **{d.riverside or '(nothing)'}** {d.r_after}…")
        md.append(f"- Whisper: …{d.w_before} **{d.whisper or '(nothing)'}** {d.w_after}…")
        if d.gemini is not None:
            md.append(f"- Gemini: …{d.g_before} **{d.gemini or '(nothing)'}** {d.g_after}…")
        md.append("")
    review_path = jd / "review.md"
    review_path.write_text("\n".join(md), encoding="utf-8")

    with open(jd / "review.csv", "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["time", "speaker", "kind", "minor", "verdict", "whisper_unsure", "riverside", "whisper", "gemini",
                     "riverside_context", "whisper_context", "gemini_context"])
        for d in sorted(diffs, key=lambda d: d.time):
            wr.writerow([fmt_ts(d.time), d.speaker, d.kind, d.minor, d.verdict, d.unsure, d.riverside, d.whisper,
                         d.gemini if d.gemini is not None else "",
                         f"{d.r_before} [{d.riverside}] {d.r_after}", f"{d.w_before} [{d.whisper}] {d.w_after}",
                         f"{d.g_before} [{d.gemini}] {d.g_after}" if d.gemini is not None else ""])
    (jd / "stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return {"synced": synced_path, "review": review_path}


def publish_outputs(job: Job, paths: dict[str, Path]) -> list[Path]:
    """Copy the two human-facing files into the creative workspace, never overwriting."""
    if not OUT_DIR.exists():
        log(f"(Skipping copy: {OUT_DIR} not found. Files stay in {job.dir})")
        return []
    stamp = date.today().strftime("%y%m%d")
    name = job.meta.get("name")
    out = []
    for key, suffix, ext in (("synced", "transcript-whisper", ".txt"), ("review", "transcript-review", ".md")):
        dest = unique_path(OUT_DIR / f"{name}-{suffix}_{stamp}{ext}")
        shutil.copyfile(paths[key], dest)
        out.append(dest)
    return out


def whisper_only_transcript(job: Job, wdata: dict) -> Path:
    lines = [f"* whispersync: Whisper {wdata.get('model')} only (no Riverside transcript to sync)", ""]
    for s in wdata["segments"]:
        lines += [f"Speaker ({fmt_ts(s['start'])})", s["text"], ""]
    p = job.dir / "transcript-whisper.txt"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Your fixes: work/<guest>/fixes.txt, applied to the synced transcript on every sync
# ---------------------------------------------------------------------------

FIXES_HELP = """# whispersync fixes for this episode. One per line; applied every time the transcript is rebuilt.
#
#   old words => new words            replace everywhere
#   @14:45 old words => new words     replace only near that time (safer for common words)
#   @40:55 >> Jane Doerr: NAC?    those exact words become their own turn for that speaker
#
# Lines starting with # are ignored.
"""


def load_fixes(job: Job) -> list[dict]:
    path = job.dir / "fixes.txt"
    if not path.exists():
        return []
    fixes = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"@(\d+:\d{2}(?::\d{2})?)\s+(.*)$", line)
        t, body = (parse_ts(m.group(1)), m.group(2)) if m else (None, line)
        if body.startswith(">>"):
            sm = re.match(r">>\s*([^:]+?):\s*(.+)$", body)
            if sm:
                fixes.append({"line": n, "time": t, "kind": "split", "speaker": sm.group(1).strip(),
                              "text": sm.group(2).strip(), "src": line})
                continue
        if "=>" in body:
            old, new = (x.strip() for x in body.split("=>", 1))
            if old:
                fixes.append({"line": n, "time": t, "kind": "replace", "old": old, "new": new, "src": line})
                continue
        fixes.append({"line": n, "kind": "bad", "src": line})
    return fixes


def _turns_near(synced: list[Turn], t: float) -> list[int]:
    """Turn indexes to search for a fix pinned at time t: the turn that contains t first, then neighbours."""
    starts = [x.start for x in synced]
    k = max([i for i, s0 in enumerate(starts) if s0 <= t + 3] or [0])
    order = [k, k + 1, k - 1, k + 2, k - 2]
    return [i for i in order if 0 <= i < len(synced)]


def apply_fixes(synced: list[Turn], fixes: list[dict]) -> tuple[list[Turn], list[dict]]:
    report = []
    for f in fixes:
        if f["kind"] == "bad":
            report.append({**f, "ok": False, "why": "couldn't read this line"})
            continue
        targets = _turns_near(synced, f["time"]) if f["time"] is not None else list(range(len(synced)))
        if f["kind"] == "replace":
            hits = 0
            for i in targets:
                txt = synced[i].text
                if f["old"] in txt:
                    synced[i].text = txt.replace(f["old"], f["new"]) if f["time"] is None else txt.replace(f["old"], f["new"], 1)
                    hits += txt.count(f["old"]) if f["time"] is None else 1
                    if f["time"] is not None:
                        break
            if not hits:  # second try, ignoring case
                for i in targets:
                    m = re.search(re.escape(f["old"]), synced[i].text, re.I)
                    if m:
                        synced[i].text = synced[i].text[:m.start()] + f["new"] + synced[i].text[m.end():]
                        hits = 1
                        break
            synced = [x for x in synced if x.text.strip()]
            report.append({**f, "ok": bool(hits), "why": "" if hits else "words not found near there"})
        else:  # split into its own turn
            done = False
            for i in targets:
                txt = synced[i].text
                pos = txt.find(f["text"])
                if pos < 0:
                    continue
                before, after = txt[:pos].strip(), txt[pos + len(f["text"]):].strip()
                orig = synced[i]
                pieces = ([Turn(orig.speaker, orig.start, before)] if before else []) + \
                         [Turn(f["speaker"], orig.start, f["text"])] + \
                         ([Turn(orig.speaker, orig.start, after)] if after else [])
                synced[i:i + 1] = pieces
                done = True
                break
            report.append({**f, "ok": done, "why": "" if done else "words not found near there"})
    # merge neighbours that now share a speaker
    merged: list[Turn] = []
    for x in synced:
        if merged and merged[-1].speaker == x.speaker and x.speaker not in (UNKNOWN_SPEAKER, "Show intro", "Show outro"):
            merged[-1].text = (merged[-1].text + " " + x.text).strip()
        else:
            merged.append(x)
    return merged, report


def do_sync(job: Job, renames: dict[str, str], max_items: int, copy_out: bool, use_google: bool = True) -> None:
    wpath = job.dir / "whisper.json"
    if not wpath.exists():
        raise SystemExit("No whisper.json yet. Run transcribe first.")
    wdata = json.loads(wpath.read_text(encoding="utf-8"))
    if not job.riverside or not job.riverside.exists():
        p = whisper_only_transcript(job, wdata)
        log(f"No Riverside transcript, so no comparison. Whisper text: {p}")
        return
    doc = load_riverside(job.riverside)
    applied = fix_speaker_names(doc, job.meta.get("guest"), renames)
    gpath = job.dir / "gemini.json"
    gdata = json.loads(gpath.read_text(encoding="utf-8")) if (use_google and gpath.exists()) else None
    synced, diffs, stats = sync(doc, wdata, gdata)
    fixes = load_fixes(job)
    if fixes:
        synced, fix_report = apply_fixes(synced, fixes)
        stats["fixes"] = fix_report
        bad = [f for f in fix_report if not f["ok"]]
        log(f"Your fixes: {len(fix_report) - len(bad)} applied" + (f", {len(bad)} not applied (see review)" if bad else ""))
    paths = write_outputs(job, doc, synced, diffs, stats, wdata, applied, max_items)
    review = sum(1 for d in diffs if not d.minor)
    log(f"Agreement {stats['agreement'] * 100:.1f}% · {review} spots to check")
    log(f"  synced transcript: {paths['synced']}")
    log(f"  review list:       {paths['review']}")
    if copy_out:
        for p in publish_outputs(job, paths):
            log(f"  copied → {p}")


# ---------------------------------------------------------------------------
# Hands-off mode: `whispersync watch`, run by cron every 20 minutes
# ---------------------------------------------------------------------------

def set_status(job: Job, state: str, **extra) -> None:
    path = job.dir / "status.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.update({"state": state, "updated": datetime.now().isoformat(timespec="seconds"), **extra})
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def pipeline(job: Job, engine_args: tuple, use_google: bool = True, gemini_model: str = GEMINI_MODEL,
             copy_out: bool = True, max_items: int = 120, renames: dict | None = None) -> None:
    """The whole job: fetch, Whisper, re-listen, Gemini, vote, fixes, outputs. Safe to rerun."""
    set_status(job, "running", step="fetch", started=datetime.now().isoformat(timespec="seconds"))
    do_fetch(job)
    set_status(job, "running", step="whisper")
    eng = do_transcribe(job, engine_args)
    set_status(job, "running", step="re-listen")
    do_repair(job, engine_args, eng)
    if use_google:
        set_status(job, "running", step="google")
        do_google(job, gemini_model)
    set_status(job, "running", step="sync")
    do_sync(job, renames or {}, max_items, copy_out=copy_out, use_google=use_google)


def job_from_episode(ep: Episode) -> Job:
    jdir = WORK_DIR / ep.slug
    jdir.mkdir(parents=True, exist_ok=True)
    job = Job(jdir, _read_meta(jdir))
    job.meta.update({"name": ep.slug, "title": ep.title, "guest": ep.guest, "pub": ep.pub, "guid": ep.guid,
                     "audio_url": ep.audio_url, "duration": ep.duration, "transcripts": ep.transcripts,
                     "description": ep.description})
    job.save()
    return job


def job_from_request(req: dict, req_path: Path) -> Job:
    """Inbox request (JSON). Fields: name, guest, title; audio_url or audio; riverside_url, riverside
    (a path) or riverside_text; description; glossary (list); action: run (default) | sync."""
    name = slugify(req.get("name") or req.get("guest") or req_path.stem)
    jdir = WORK_DIR / name
    jdir.mkdir(parents=True, exist_ok=True)
    job = Job(jdir, _read_meta(jdir))
    job.meta.setdefault("name", name)
    for k in ("guest", "title", "description"):
        if req.get(k):
            job.meta[k] = req[k]
    job.meta.setdefault("title", req.get("guest") or name)
    if req.get("audio_url"):
        if req.get("audio_url") != job.meta.get("audio_url"):
            (jdir / "audio.mp3").unlink(missing_ok=True)   # new cut: forget the old audio and Whisper pass
            (jdir / "whisper.json").unlink(missing_ok=True)
            (jdir / "gemini.json").unlink(missing_ok=True)
        job.meta["audio_url"] = req["audio_url"]
        job.meta.pop("audio", None)
    elif req.get("audio"):
        job.meta["audio"] = str(Path(req["audio"]).expanduser())
    if req.get("riverside_text"):
        (jdir / "riverside.txt").write_text(req["riverside_text"], encoding="utf-8")
        job.meta["riverside"] = str(jdir / "riverside.txt")
    elif req.get("riverside_url"):
        dest = jdir / "riverside.txt"
        http_get(req["riverside_url"], dest)
        job.meta["riverside"] = str(dest)
    elif req.get("riverside"):
        job.meta["riverside"] = str(Path(req["riverside"]).expanduser())
    if req.get("glossary"):
        (jdir / "glossary.txt").write_text("\n".join(req["glossary"]) + "\n", encoding="utf-8")
    job.save()
    return job


def _ollama_busy() -> bool:
    """Whisper and a big Ollama model at once can hang a small server. Wait for the next round."""
    if os.environ.get("WS_IGNORE_OLLAMA") or not shutil.which("ollama"):
        return False
    import subprocess
    try:
        out = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return False
    return len([l for l in out.splitlines() if l.strip()]) > 1


def do_watch(args) -> int:
    import fcntl
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(WORK_DIR / ".watch.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # the previous round is still working
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    wlog = WORK_DIR / "watch.log"
    if wlog.exists() and wlog.stat().st_size > 2_000_000:
        wlog.replace(WORK_DIR / "watch.log.1")  # keep the cron log from growing forever
    state_path = WORK_DIR / ".watch-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    engine_args = (args.model, args.threads, args.beam)
    use_google = not args.no_google
    did = []

    def save_state():
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def history(event: dict):
        with open(WORK_DIR / "watch-history.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **event}) + "\n")

    # 1) inbox requests first: those are the ones somebody is waiting on
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    for sub in ("done", "failed"):
        (INBOX_DIR / sub).mkdir(exist_ok=True)
    requests = sorted(INBOX_DIR.glob("*.json"))
    if requests and _ollama_busy():
        log(f"[{stamp}] Ollama has a model loaded; leaving {len(requests)} request(s) for later.")
        if args.wait_if_busy:
            time.sleep(args.wait_if_busy)  # the watcher re-wakes as soon as we exit; don't spin
        requests = []
    for rp in requests:
        try:
            req = json.loads(rp.read_text(encoding="utf-8"))
            action = req.get("action", "run")
            log(f"[{stamp}] inbox: {rp.name} ({action})")
            job = job_from_request(req, rp)
            if action == "sync":
                set_status(job, "running", step="sync")
                do_sync(job, parse_renames(req.get("rename")), 120, copy_out=True, use_google=use_google)
            else:
                pipeline(job, engine_args, use_google, args.gemini_model, renames=parse_renames(req.get("rename")))
            set_status(job, "done", finished=datetime.now().isoformat(timespec="seconds"), request=rp.name)
            rp.rename(unique_path(INBOX_DIR / "done" / rp.name))
            history({"what": "inbox", "request": rp.name, "job": job.meta.get("name"), "ok": True})
            did.append(f"inbox {rp.name} → {job.meta.get('name')}")
        except (Exception, SystemExit) as e:
            log(f"[{stamp}]   failed: {e}")
            rp.rename(unique_path(INBOX_DIR / "failed" / rp.name))
            (INBOX_DIR / "failed" / (rp.stem + ".error.txt")).write_text(f"{type(e).__name__}: {e}\n", encoding="utf-8")
            history({"what": "inbox", "request": rp.name, "ok": False, "error": str(e)[:300]})

    # 2) newly published episodes
    if not args.no_feed:
        try:
            eps = load_feed()
        except Exception as e:
            log(f"[{stamp}] couldn't read the feed: {e}")
            eps = []
        if eps and "seen" not in state:
            # first run: don't chew through the whole back catalog unless asked
            keep = eps[: args.backfill] if args.backfill else []
            state["seen"] = [e.guid for e in eps if e not in keep]
            state["installed"] = stamp
            save_state()
            log(f"[{stamp}] first run: {len(state['seen'])} existing episodes marked as already done.")
        fresh = [e for e in reversed(eps) if e.guid not in state.get("seen", [])]
        if fresh and _ollama_busy():
            log(f"[{stamp}] Ollama has a model loaded; {len(fresh)} new episode(s) wait for the next round.")
            fresh = []
        for ep in fresh:
            tries = state.setdefault("tries", {}).get(ep.guid, 0)
            if tries >= 3:
                continue
            log(f"[{stamp}] new episode: {ep.title}")
            job = job_from_episode(ep)
            try:
                pipeline(job, engine_args, use_google, args.gemini_model)
                set_status(job, "done", finished=datetime.now().isoformat(timespec="seconds"))
                state["seen"].append(ep.guid)
                state["tries"].pop(ep.guid, None)
                history({"what": "episode", "title": ep.title, "job": job.meta.get("name"), "ok": True})
                did.append(f"episode {ep.title}")
            except (Exception, SystemExit) as e:
                state["tries"][ep.guid] = tries + 1
                set_status(job, "failed", error=str(e)[:300])
                log(f"[{stamp}]   failed ({tries + 1}/3): {e}")
                history({"what": "episode", "title": ep.title, "ok": False, "error": str(e)[:300]})
            save_state()
    if did:
        log(f"[{stamp}] done: " + "; ".join(did))
    return 0


# ---------------------------------------------------------------------------
# Self-test (no model, no network)
# ---------------------------------------------------------------------------

SELFTEST_RIVERSIDE = """* Edit - Test
duration ~1:00

Jane Doerr (0:00)
I'm Jane Doerrr and I'm eager to be healthy.

Michael Scott Eger (0:05)
our guest today is Jane Doerrr. Three decades of back trouble. She wrote The Long Road Through Care. ~ Welcome.

JanE Doer (0:20)
Thank you so much, Michael. I I'm so glad to be here.

Michael Scott Eger (0:26)
Go to eager to be healthy dot com slash support.
"""

SELFTEST_WHISPER = (
    "I'm Jane Doerr and I'm eager to be healthy. "
    "Our guest today is Jane Doerr. 30 decades of back trouble. "
    "She wrote The Long Road Through Chronic Pain. Welcome. "
    "Thank you so much, Michael. I'm so glad to be here. "
    "Go to eagertobehealthy.com/support. That's all."
)


def selftest() -> int:
    doc = parse_riverside_txt(SELFTEST_RIVERSIDE)
    assert len(doc.turns) == 4, doc.turns
    assert doc.header[0].startswith("* Edit")
    applied = fix_speaker_names(doc, "Jane Doerr", {})
    assert applied == {"JanE Doer": "Jane Doerr"}, applied

    toks = SELFTEST_WHISPER.split()
    t, words = 0.0, []
    for w in toks:
        words.append({"word": " " + w, "start": t, "end": t + 0.3, "prob": 0.9 if w != "30" else 0.4})
        t += 0.4
    wdata = {"model": "test", "segments": [{"start": 0, "end": t, "text": SELFTEST_WHISPER, "words": words}]}
    synced, diffs, stats = sync(doc, wdata)
    review = [d for d in diffs if not d.minor]
    kinds = {(d.riverside, d.whisper): d.kind for d in review}
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  ok   " if cond else "  FAIL ") + msg)
        ok &= bool(cond)

    check(len(synced) == 4, "keeps all 4 Riverside turns")
    check(synced[0].text.startswith("I'm Jane Doerr and"), "turn 1 uses Whisper's spelling")
    check(synced[2].speaker == "Jane Doerr", "guest label 'JanE Doer' snapped to 'Jane Doerr'")
    check(any(r.startswith("Doerrr") and kinds[(r, w)] == "names" for r, w in kinds), "flags Doerrr → Doerr as a name")
    check(any("Care" in r and "Chronic" in w for r, w in kinds), "flags The Long Road Through Care → Chronic Pain")
    check(any(kinds[k] == "numbers" for k in kinds), "flags Three → 30 as a number")
    check(not any(r.startswith("I I") or w.startswith("I I") for r, w in kinds), "ignores the 'I I' stutter")
    check(not any("dot" in r for r, w in kinds), "treats 'dot com slash' and '.com/' as the same")
    check(any(d.kind == "missing-in-riverside" or "That's" in d.whisper for d in diffs), "notices Whisper-only words at the end")
    check(synced[3].text.endswith("That's all."), "trailing Whisper words land in the last turn")
    check(0.6 < stats["agreement"] < 1.0, f"agreement looks sane ({stats['agreement']:.2f})")
    swaps = repeated_swaps(diffs)
    check(any(r == "Doerrr" for r, w, n in swaps), "lists Doerrr → Doerr under repeated swaps")
    # Lessons from the first real run (Doerr, Oct 2026)
    doc2 = parse_colon_txt(
        "Jane Doerr: we're gonna talk about the hippocam the hippocampus and it struggled a lot\n\n"
        "Michael Scott Eger: Mm-hmm.\n\n"
        "Jane Doerr: so that's that's where the pain lives\n")
    w2 = ("We're going to talk about the hippocampus and it struggle a lot. So that's where the pain lives. "
          "Then a long stretch here. My chiropractor gave me its protocol.").split()
    t2, words2 = 0.0, []
    for x in w2:
        if x == "My":
            t2 += 20  # a 20-second hole, like the one Whisper left at 40:20
        words2.append({"word": " " + x, "start": t2, "end": t2 + 0.3, "prob": 0.9})
        t2 += 0.4
    synced2, diffs2, stats2 = sync(doc2, {"segments": [{"words": words2}]})
    flagged2 = [d for d in diffs2 if not d.minor]
    check(not any("gonna" in d.riverside or "hippocam" in d.riverside or "struggled" in d.riverside for d in flagged2),
          "ignores gonna/going to, cut-off words, and struggled/struggle")
    check(stats2["backchannel_turns_dropped"] == 1 and all(t.speaker != "Michael Scott Eger" for t in synced2),
          "drops the 'Mm-hmm.' turn and merges the guest's turns")
    k = next(i for i, w in enumerate(words2) if w["word"] == " My")
    g = _find_gap(words2, k - 1)
    check(g[1] - g[0] > 15, f"finds the hole Whisper left even past a stretched edge word ({g[1] - g[0]:.0f}s)")

    # Three-way vote: Gemini agrees with Whisper on the name, with Riverside on a word Whisper got wrong
    doc3 = parse_riverside_txt("Jane Doerr (0:00)\nI'm Jane Doerrr and I take Atavan for my gut every day.\n")
    w3 = "I'm Jane Doerr and I take Ativan for my got every day.".split()
    words3 = [{"word": " " + x, "start": i * 0.4, "end": i * 0.4 + 0.3, "prob": 0.9} for i, x in enumerate(w3)]
    g3 = {"model_version": "test", "chunks": [{"text": "I'm Jane Doerr and I take Ativan for my gut every day.",
                                               "suspect": False}]}
    synced3, diffs3, stats3 = sync(doc3, {"segments": [{"words": words3}]}, g3)
    check(stats3["vote"] == {"whisper+gemini": 2, "riverside+gemini": 1, "split": 0},
          f"three-way vote settles names and outvotes Whisper's 'got' ({stats3['vote']})")
    check("my gut every day" in synced3[0].text and "got" not in synced3[0].text,
          "synced transcript takes the winning words")

    # crosstalk: Riverside's per-speaker tracks decide who said the interjection Whisper folded in
    doc4 = parse_colon_txt("Michael Scott Eger: it's over the counter liver support plus knack. to yeah. And\n\n"
                           "Jane Doerr: N A C, mm.\n\n"
                           "Michael Scott Eger: the two combine feed the precursors.\n")
    w4 = "It's over the counter liver support plus NAC. NAC? Yeah. And the two combined feed the precursors.".split()
    words4 = [{"word": " " + x, "start": i * 0.4, "end": i * 0.4 + 0.3, "prob": 0.9} for i, x in enumerate(w4)]
    synced4, _, stats4 = sync(doc4, {"segments": [{"words": words4}]})
    check([t.speaker for t in synced4] == ["Michael Scott Eger", "Jane Doerr", "Michael Scott Eger"]
          and synced4[1].text == "NAC?", "crosstalk goes to Riverside's speaker ('NAC?' is Jane's)")

    # fixes.txt: replace near a time, split crosstalk into its own turn, delete a turn
    turns = [Turn("Michael Scott Eger", 10.0, "plus NAC. NAC? Yeah. And the two combined"),
             Turn("Jane Doerr", 20.0, "[Riverside only, Whisper missed this] N A C, mm."),
             Turn("Michael Scott Eger", 30.0, "flush those dachshunds out.")]
    fx = [{"line": 1, "time": 10.0, "kind": "split", "speaker": "Jane Doerr", "text": "NAC?", "src": "a"},
          {"line": 2, "time": 20.0, "kind": "replace", "old": "[Riverside only, Whisper missed this] N A C, mm.", "new": "", "src": "b"},
          {"line": 3, "time": 30.0, "kind": "replace", "old": "dachshunds", "new": "toxins", "src": "c"}]
    out, rep = apply_fixes(turns, fx)
    check(all(r["ok"] for r in rep) and [t.speaker for t in out] == ["Michael Scott Eger", "Jane Doerr", "Michael Scott Eger"]
          and out[1].text == "NAC?" and out[2].text.endswith("flush those toxins out."),
          "fixes.txt: replaces near a time, splits crosstalk, drops a turn")

    colon = parse_colon_txt("Jane Doerr: I'm Jane Doer and I'm eager.\n\n"
                            "Michael Scott Eger: Our guest today. Note: this is not a speaker.\n\n"
                            "Jane Doerr: Thank you.\n\nMichael Scott Eger: Welcome.\n")
    check(len(colon.turns) == 4 and colon.turns[1].text.endswith("not a speaker."),
          "reads the feed's 'Speaker: text' transcript")
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_renames(pairs: list[str] | None) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        if "=" in p:
            a, b = p.split("=", 1)
            out[a.strip()] = b.strip()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="whispersync", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("episodes", help="list episodes in the RSS feed")
    p.add_argument("--limit", type=int, default=15)

    def common(p, transcribe=False):
        p.add_argument("episode", nargs="?", help="latest | number | title word | work-folder name")
        p.add_argument("--audio", help="local audio file instead of the feed MP3 (pre-publish checks)")
        p.add_argument("--riverside", help="local Riverside transcript (.txt export, .vtt, .srt, .json)")
        p.add_argument("--name", help="short name for the work folder and output files (default: guest slug)")
        p.add_argument("--guest", help='guest name as it should be spelled, e.g. "Jane Doerr"')
        p.add_argument("--rename", action="append", metavar="OLD=NEW", help="fix a speaker label; repeatable")
        p.add_argument("--max-items", type=int, default=120, help="cap on spots listed in review.md")
        p.add_argument("--no-copy", action="store_true", help="don't copy results into WS_OUT")
        p.add_argument("--no-google", action="store_true", help="skip the Gemini third opinion")
        p.add_argument("--gemini-model", default=GEMINI_MODEL, help=f"Gemini model (default {GEMINI_MODEL}, the newest Flash)")
        p.add_argument("--force-google", action="store_true", help="redo the Gemini pass even if gemini.json exists")
        if transcribe:
            p.add_argument("--model", default=DEFAULT_MODEL, help=f"Whisper model (default {DEFAULT_MODEL})")
            p.add_argument("--threads", type=int, default=DEFAULT_THREADS, help=f"CPU threads (default {DEFAULT_THREADS})")
            p.add_argument("--beam", type=int, default=5, help="beam size; 1 is faster, 5 is more careful")
            p.add_argument("--force", action="store_true", help="redo Whisper (or the re-listen pass) even if already done")

    common(sub.add_parser("run", help="fetch + transcribe + re-listen + sync"), transcribe=True)
    common(sub.add_parser("fetch", help="download audio + Riverside transcript"))
    common(sub.add_parser("transcribe", help="run Whisper"), transcribe=True)
    common(sub.add_parser("repair", help="re-listen where Whisper skipped speech Riverside has"), transcribe=True)
    common(sub.add_parser("google", help="get Gemini's transcript (third opinion) and re-sync"))
    common(sub.add_parser("sync", help="compare and write outputs"))
    w = sub.add_parser("watch", help="one hands-off round: new feed episodes + inbox requests (for cron)")
    w.add_argument("--model", default=DEFAULT_MODEL)
    w.add_argument("--threads", type=int, default=DEFAULT_THREADS)
    w.add_argument("--beam", type=int, default=5)
    w.add_argument("--no-google", action="store_true")
    w.add_argument("--gemini-model", default=GEMINI_MODEL)
    w.add_argument("--no-feed", action="store_true", help="only handle inbox requests")
    w.add_argument("--backfill", type=int, default=0, help="first run only: also process the newest N episodes")
    w.add_argument("--wait-if-busy", type=int, default=0, help="seconds to pause before exiting when Ollama is busy")
    sub.add_parser("selftest", help="check alignment logic offline")

    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()

    if args.cmd == "watch":
        return do_watch(args)

    if args.cmd == "episodes":
        for ep in load_feed()[: args.limit]:
            tr = ", ".join(t for t, _ in ep.transcripts) or "no transcript"
            log(f"{ep.index:>3}  {ep.pub[:16]:<16}  {ep.duration:>8}  {ep.title}  [{tr}]")
        return 0

    job = job_for(args)
    renames = parse_renames(args.rename)
    if args.cmd in ("fetch", "run"):
        do_fetch(job)
    eng = None
    engine_args = (getattr(args, "model", DEFAULT_MODEL), getattr(args, "threads", DEFAULT_THREADS), getattr(args, "beam", 5))
    if args.cmd in ("transcribe", "run"):
        if not job.audio or not job.audio.exists():
            do_fetch(job)
        eng = do_transcribe(job, engine_args, args.force)
    if args.cmd in ("repair", "run"):
        do_repair(job, engine_args, eng, force=(args.cmd == "repair" and args.force))
    if args.cmd in ("google", "run") and not args.no_google:
        if not job.audio or not job.audio.exists():
            do_fetch(job)
        do_google(job, args.gemini_model, force=args.force_google)
    if args.cmd in ("sync", "run", "repair", "google"):
        do_sync(job, renames, args.max_items, copy_out=not args.no_copy, use_google=not args.no_google)
    return 0


if __name__ == "__main__":
    sys.exit(main())
