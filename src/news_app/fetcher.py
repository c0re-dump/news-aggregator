"""Fetch recent videos from YouTube channels.

Two sources:
- RSS feed (default, no key needed): the newest 15 uploads of each channel.
  The app polls it on a timer, so history builds up in the database over time.
- YouTube Data API v3 (when YOUTUBE_API_KEY is set): backfills older uploads
  (last BACKFILL_DAYS days), gives exact durations and marks Shorts.
"""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

from . import db
from .config import BACKFILL_DAYS, MAX_PER_CHANNEL, YOUTUBE_API_KEY, Channel, load_channels

log = logging.getLogger(__name__)

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
UA = {"User-Agent": "Mozilla/5.0 (news-app)", "Accept-Language": "bg,en"}
API = "https://www.googleapis.com/youtube/v3"


def _client() -> httpx.Client:
    return httpx.Client(headers=UA, timeout=20, follow_redirects=False)


def fetch_rss(client: httpx.Client, ch: Channel) -> list[dict]:
    r = client.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": ch.id})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for e in root.findall("atom:entry", NS):
        group = e.find("media:group", NS)
        desc = group.findtext("media:description", "", NS) if group is not None else ""
        thumb = group.find("media:thumbnail", NS) if group is not None else None
        link = e.find("atom:link", NS)
        out.append({
            "id": e.findtext("yt:videoId", "", NS),
            "channel_id": ch.id,
            "channel_name": ch.name,
            "title": e.findtext("atom:title", "", NS),
            "description": desc or "",
            "published_at": e.findtext("atom:published", "", NS),
            "thumbnail": thumb.get("url") if thumb is not None else None,
            "is_short": int(link is not None and "/shorts/" in (link.get("href") or "")),
        })
    return out


_DUR = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _iso_duration(s: str) -> int | None:
    m = _DUR.fullmatch(s or "")
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + se


def _playlist(client: httpx.Client, playlist_id: str, cutoff: str, cap: int):
    """Yield playlistItems snippets, newest first, until `cutoff` (ISO date) or `cap` items."""
    token, n = None, 0
    while n < cap:
        r = client.get(f"{API}/playlistItems", params={
            "part": "snippet", "playlistId": playlist_id, "maxResults": 50,
            "key": YOUTUBE_API_KEY, **({"pageToken": token} if token else {}),
        })
        if r.status_code == 404:  # e.g. a channel without Shorts has no Shorts playlist
            return
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])
        for it in items:
            yield it["snippet"]
            n += 1
        # Stop when the whole page is older than the cutoff.
        if not items or all(it["snippet"].get("publishedAt", "") < cutoff for it in items):
            return
        token = data.get("nextPageToken")
        if not token:
            return


def fetch_api(client: httpx.Client, ch: Channel, days: int) -> list[dict]:
    """Load the last `days` days of uploads with the Data API (1 quota unit per 50 videos)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # "UU..." is the uploads playlist, "UUSH..." holds only the channel's Shorts.
    shorts = {sn["resourceId"]["videoId"] for sn in _playlist(client, "UUSH" + ch.id[2:], cutoff, MAX_PER_CHANNEL)}
    out: list[dict] = []
    for sn in _playlist(client, "UU" + ch.id[2:], cutoff, MAX_PER_CHANNEL):
        vid = sn.get("resourceId", {}).get("videoId")
        if not vid or sn.get("publishedAt", "") < cutoff:
            continue
        thumbs = sn.get("thumbnails", {})
        thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url")
        out.append({
            "id": vid, "channel_id": ch.id, "channel_name": ch.name,
            "title": sn.get("title", ""), "description": sn.get("description", ""),
            "published_at": sn.get("publishedAt", ""), "thumbnail": thumb,
            "is_short": int(vid in shorts),
        })
    add_durations(client, out)
    return out


def add_durations(client: httpx.Client, videos: list[dict]) -> None:
    """Fill duration_s with videos.list, 50 ids per call (1 quota unit each)."""
    for i in range(0, len(videos), 50):
        chunk = videos[i:i + 50]
        r = client.get(f"{API}/videos", params={
            "part": "contentDetails", "id": ",".join(v["id"] for v in chunk), "key": YOUTUBE_API_KEY,
        })
        r.raise_for_status()
        durs = {it["id"]: _iso_duration(it["contentDetails"].get("duration", "")) for it in r.json().get("items", [])}
        for v in chunk:
            v["duration_s"] = durs.get(v["id"])


def refresh(backfill: bool = False) -> int:
    """Fetch all channels. Returns the number of new videos stored."""
    total_new = 0
    with connect_known() as known, _client() as client:
        for ch in load_channels():
            try:
                if YOUTUBE_API_KEY and (backfill or not known.get(ch.id)):
                    videos = fetch_api(client, ch, BACKFILL_DAYS)
                else:
                    videos = fetch_rss(client, ch)
                if ch.include:
                    videos = [v for v in videos if ch.include.casefold() in v["title"].casefold()]
                fresh = [v for v in videos if v["id"] not in known.get(ch.id, set())]
                if YOUTUBE_API_KEY and fresh and any("duration_s" not in v for v in fresh):
                    add_durations(client, fresh)
                new = db.upsert_videos(videos)  # also refreshes titles and Shorts flags
                total_new += new
                log.info("%s: %d fetched, %d new", ch.name, len(videos), new)
            except Exception as e:  # one broken channel must not stop the others
                log.warning("%s: fetch failed: %s", ch.name, e)
    return total_new


class connect_known:
    """Map of channel_id -> set of video ids already in the database."""

    def __enter__(self) -> dict[str, set[str]]:
        known: dict[str, set[str]] = {}
        with db.connect() as conn:
            for cid, vid in conn.execute("SELECT channel_id, id FROM videos"):
                known.setdefault(cid, set()).add(vid)
        return known

    def __exit__(self, *exc) -> None:
        return None


def resolve_handle(handle: str) -> tuple[str, str] | None:
    """Return (channel_id, title) for a handle like '@bTVMediaGroup'."""
    handle = handle if handle.startswith("@") else "@" + handle
    with httpx.Client(headers=UA, timeout=20, follow_redirects=True) as c:
        if YOUTUBE_API_KEY:
            r = c.get(f"{API}/channels", params={"part": "snippet", "forHandle": handle, "key": YOUTUBE_API_KEY})
            items = r.json().get("items", []) if r.is_success else []
            if items:
                return items[0]["id"], items[0]["snippet"]["title"]
        html = c.get(f"https://www.youtube.com/{handle}").text
    m = re.search(r'"externalId":"(UC[\w-]{22})"', html)
    t = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    return (m.group(1), t.group(1) if t else handle) if m else None
