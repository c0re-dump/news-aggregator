"""Split long full-show videos (e.g. BNT "Денят започва") into timed segments.

1. yt-dlp downloads YouTube's subtitles (automatic Bulgarian captions) with timings.
2. Claude reads the timed transcript and returns one entry per interview/report.
3. Each segment is stored and shown in the website like a normal video,
   and plays inside the page from its start time to its end time.
"""

import json
import logging
import tempfile
from pathlib import Path

import anthropic
import yt_dlp

from . import db
from .classifier import _known_topics
from .config import CATEGORIES, CLAUDE_MODEL, FORMATS, load_channels

log = logging.getLogger(__name__)
MIN_SHOW_SECONDS = 45 * 60
MAX_ATTEMPTS = 6  # captions can appear hours after upload; retry on later refreshes


# ---------------------------------------------------------------- transcript

def fetch_caption_events(video_id: str) -> list[dict] | None:
    """Return json3 caption events ({tStartMs, segs}) or None when YouTube has no captions yet."""
    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["bg"],
            "subtitlesformat": "json3",
            "outtmpl": str(Path(tmp) / "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        files = list(Path(tmp).glob("*.json3"))
        if not files:
            return None
        return [e for e in json.loads(files[0].read_text())["events"] if e.get("segs")]


def hms(s: int) -> str:
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_hms(t: str) -> int:
    parts = [int(p) for p in t.strip().split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return h * 3600 + m * 60 + s


def transcript_text(events: list[dict], every: int = 15) -> str:
    """Join caption events into lines of ~`every` seconds, each with a [H:MM:SS] stamp."""
    lines, cur, start = [], [], None
    for e in events:
        text = "".join(s.get("utf8", "") for s in e["segs"]).replace("\n", " ").strip()
        if not text:
            continue
        t = e["tStartMs"] // 1000
        if start is None:
            start = t
        cur.append(text)
        if t - start >= every:
            lines.append(f"[{hms(start)}] " + " ".join(cur))
            cur, start = [], None
    if cur:
        lines.append(f"[{hms(start)}] " + " ".join(cur))
    return "\n".join(lines)


# ---------------------------------------------------------------- Claude

SYSTEM = f"""You split the transcript of a long Bulgarian TV morning show into its segments,
so a viewer can jump straight to the interview or report they care about.

The transcript comes from automatic speech recognition: expect misheard words and names.
Use the video description (written by the TV channel) to correct the names of guests and topics.
Each transcript line starts with its start time [H:MM:SS].

Rules:
- One segment per interview, studio discussion, report or news block. A typical 2.5 hour show has 10-25 segments.
- `start` is the [H:MM:SS] stamp of the line where the segment begins (copy it exactly from the transcript).
  Start an interview where the host introduces the topic or guest, not in the middle of the first answer.
- Segments are in time order and do not overlap. A segment ends where the next one starts.
- `title`: a short, specific Bulgarian headline for the segment, like a news title (max ~90 characters).
- `guests`: full names with role, e.g. "Любослав Костов, икономист". Empty for reports without guests.
- `summary`: one Bulgarian sentence that says what is discussed.
- `category`: one of {", ".join(CATEGORIES)}.
- `format`: one of {", ".join(k for k in FORMATS if k != "full_show")}.
- `topics`: 1-4 Bulgarian tags for the concrete story (people's full names, places, named issues),
  nominative case, no generic words like "политика".
- Mark the show opening, the preview of the day, weather, ads and the goodbye with `skip: true`."""

SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "title": {"type": "string"},
                    "guests": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "format": {"type": "string", "enum": [k for k in FORMATS if k != "full_show"]},
                    "topics": {"type": "array", "items": {"type": "string"}},
                    "skip": {"type": "boolean"},
                },
                "required": ["start", "title", "guests", "summary", "category", "format", "topics", "skip"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["segments"],
    "additionalProperties": False,
}


def split_with_claude(client: anthropic.Anthropic, video: dict, transcript: str) -> list[dict]:
    known = _known_topics()
    user = (f"Known topic tags (reuse one when it is the same story): {json.dumps(known, ensure_ascii=False)}\n\n"
            f"Video title: {video['title']}\n\nVideo description:\n{video['description']}\n\n"
            f"Transcript:\n{transcript}")
    fallback = {} if "haiku" in CLAUDE_MODEL else {
        "betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    with client.beta.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=32000,
        system=SYSTEM,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": user}],
        **fallback,
    ) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"Claude stopped early: {resp.stop_reason}")
    text = next(b.text for b in resp.content if b.type == "text")
    segs = json.loads(text)["segments"]

    duration = video.get("duration_s") or 0
    out = []
    for s in segs:
        try:
            s["start_s"] = parse_hms(s["start"])
        except ValueError:
            continue
        out.append(s)
    out.sort(key=lambda s: s["start_s"])
    for i, s in enumerate(out):
        s["end_s"] = out[i + 1]["start_s"] if i + 1 < len(out) else (duration or s["start_s"] + 600)
    return [s for s in out if not s["skip"] and s["end_s"] - s["start_s"] >= 60]


# ---------------------------------------------------------------- pipeline

def segment_channels() -> set[str]:
    return {c.id for c in load_channels() if c.segment}


def segment_video(client: anthropic.Anthropic, video: dict) -> int:
    events = fetch_caption_events(video["id"])
    if not events:
        db.save_show_job(video["id"], "no_captions")
        return 0
    segs = split_with_claude(client, video, transcript_text(events))
    db.save_segments(video["id"], segs, f"claude:{CLAUDE_MODEL}")
    db.save_show_job(video["id"], "done")
    log.info("%s: %d segments", video["title"][:60], len(segs))
    return len(segs)


def segment_pending() -> int:
    channels = segment_channels()
    if not channels:
        return 0
    client = anthropic.Anthropic()
    total = 0
    for video in db.shows_to_segment(channels, MIN_SHOW_SECONDS, MAX_ATTEMPTS):
        try:
            total += segment_video(client, video)
        except Exception as e:  # one broken show must not stop the others
            log.warning("%s: segmenting failed: %s", video["id"], e)
            db.save_show_job(video["id"], f"error: {e}"[:300])
    return total
