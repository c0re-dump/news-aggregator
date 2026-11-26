import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id            TEXT PRIMARY KEY,
    channel_id    TEXT NOT NULL,
    channel_name  TEXT NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    published_at  TEXT NOT NULL,          -- ISO 8601, UTC
    thumbnail     TEXT,
    duration_s    INTEGER,                -- NULL when unknown (RSS source)
    is_short      INTEGER NOT NULL DEFAULT 0,
    category      TEXT,                   -- NULL until classified
    format        TEXT,
    topics        TEXT NOT NULL DEFAULT '[]',  -- JSON list of strings
    classified_by TEXT,
    fetched_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_videos_published ON videos(published_at);
CREATE INDEX IF NOT EXISTS idx_videos_category ON videos(category);

-- Parts of long full-show videos (e.g. BNT "Денят започва"), made by segmenter.py.
CREATE TABLE IF NOT EXISTS segments (
    video_id      TEXT NOT NULL REFERENCES videos(id),
    start_s       INTEGER NOT NULL,
    end_s         INTEGER NOT NULL,
    title         TEXT NOT NULL,
    guests        TEXT NOT NULL DEFAULT '[]',  -- JSON list
    summary       TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL,
    format        TEXT NOT NULL,
    topics        TEXT NOT NULL DEFAULT '[]',  -- JSON list
    classified_by TEXT,
    PRIMARY KEY (video_id, start_s)
);
CREATE TABLE IF NOT EXISTS show_jobs (
    video_id   TEXT PRIMARY KEY,
    status     TEXT NOT NULL,     -- done | no_captions | error: ...
    attempts   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_videos(videos: list[dict]) -> int:
    """Insert new videos and refresh metadata of known ones. Returns count of new rows."""
    if not videos:
        return 0
    with connect() as conn:
        known = {r[0] for r in conn.execute(
            f"SELECT id FROM videos WHERE id IN ({','.join('?' * len(videos))})",
            [v["id"] for v in videos],
        )}
        for v in videos:
            # One date format everywhere, so text comparison of dates works.
            v["published_at"] = datetime.fromisoformat(v["published_at"].replace("Z", "+00:00")) \
                .astimezone(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO videos (id, channel_id, channel_name, title, description, published_at,
                                    thumbnail, duration_s, is_short, fetched_at)
                VALUES (:id, :channel_id, :channel_name, :title, :description, :published_at,
                        :thumbnail, :duration_s, :is_short, :fetched_at)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    thumbnail = excluded.thumbnail,
                    duration_s = COALESCE(excluded.duration_s, videos.duration_s),
                    is_short = MAX(excluded.is_short, videos.is_short)
                """,
                {"duration_s": None, "is_short": 0, "fetched_at": now_iso(), **v},
            )
        return len([v for v in videos if v["id"] not in known])


def unclassified(limit: int = 500) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM videos WHERE category IS NULL AND is_short = 0 ORDER BY published_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def norm_topics(topics: list[str]) -> list[str]:
    """Trim, start with a capital letter, drop duplicates: 'бюджет 2027' -> 'Бюджет 2027'."""
    out = []
    for t in topics:
        t = t.strip().lstrip("#")
        if t:
            t = t[0].upper() + t[1:]
            if t.casefold() not in {x.casefold() for x in out}:
                out.append(t)
    return out[:4]


def save_classification(video_id: str, category: str, fmt: str, topics: list[str], by: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE videos SET category = ?, format = ?, topics = ?, classified_by = ? WHERE id = ?",
            (category, fmt, json.dumps(norm_topics(topics), ensure_ascii=False), by, video_id),
        )


def save_segments(video_id: str, segments: list[dict], by: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM segments WHERE video_id = ?", (video_id,))
        for s in segments:
            conn.execute(
                "INSERT OR REPLACE INTO segments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (video_id, s["start_s"], s["end_s"], s["title"], json.dumps(s["guests"], ensure_ascii=False),
                 s["summary"], s["category"], s["format"],
                 json.dumps(norm_topics(s["topics"]), ensure_ascii=False), by),
            )


def save_show_job(video_id: str, status: str) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO show_jobs (video_id, status, attempts, updated_at) VALUES (?, ?, 1, ?)
               ON CONFLICT(video_id) DO UPDATE SET status = excluded.status,
                   attempts = show_jobs.attempts + 1, updated_at = excluded.updated_at""",
            (video_id, status, now_iso()),
        )


def shows_to_segment(channel_ids: set[str], min_seconds: int, max_attempts: int) -> list[dict]:
    q = f"""SELECT v.* FROM videos v LEFT JOIN show_jobs j ON j.video_id = v.id
              WHERE v.channel_id IN ({','.join('?' * len(channel_ids))})
                AND v.duration_s >= ? AND v.is_short = 0
                AND (j.video_id IS NULL OR (j.status != 'done' AND j.attempts < ?))
              ORDER BY v.published_at DESC"""
    with connect() as conn:
        return [dict(r) for r in conn.execute(q, [*channel_ids, min_seconds, max_attempts])]


def show_segments(video_id: str) -> list[dict]:
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM segments WHERE video_id = ? ORDER BY start_s", (video_id,))]
    for r in rows:
        r["guests"], r["topics"] = json.loads(r["guests"]), json.loads(r["topics"])
    return rows


SPANS = {"day": timedelta(days=1), "week": timedelta(days=7), "month": timedelta(days=30)}


def query_videos(span: str = "day", category: str | None = None, channel: str | None = None,
                 topic: str | None = None, q: str | None = None) -> list[dict]:
    since = (datetime.now(timezone.utc) - SPANS.get(span, SPANS["day"])).isoformat(timespec="seconds")
    # Shorts are stored (so they are not fetched again) but never listed.
    sql = "SELECT * FROM videos WHERE published_at >= ? AND is_short = 0"
    args: list = [since]
    if category:
        sql += " AND category = ?"
        args.append(category)
    if channel:
        sql += " AND channel_id = ?"
        args.append(channel)
    # A full show that was split is listed through its segments instead.
    sql += " AND id NOT IN (SELECT video_id FROM segments)"

    seg_sql = """SELECT s.*, v.channel_id, v.channel_name, v.published_at, v.thumbnail,
                        v.title AS show_title
                 FROM segments s JOIN videos v ON v.id = s.video_id WHERE v.published_at >= ?"""
    seg_args: list = [since]
    if category:
        seg_sql += " AND s.category = ?"
        seg_args.append(category)
    if channel:
        seg_sql += " AND v.channel_id = ?"
        seg_args.append(channel)

    with connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, args)]
        segs = [dict(r) for r in conn.execute(seg_sql, seg_args)]
    for r in rows:
        r["topics"] = json.loads(r["topics"] or "[]")
        r["guests"], r["start_s"], r["end_s"] = [], None, None
    for s in segs:
        rows.append({
            **s, "id": s["video_id"], "description": s["summary"], "is_short": 0,
            "duration_s": s["end_s"] - s["start_s"],
            "topics": json.loads(s["topics"]), "guests": json.loads(s["guests"]),
        })
    # Newest first; parts of one show stay in broadcast order.
    rows.sort(key=lambda r: (r["published_at"], -(r["start_s"] or 0)), reverse=True)
    if topic:
        t = topic.casefold()
        rows = [r for r in rows if any(t == x.casefold() for x in r["topics"])]
    if q:
        needle = q.casefold()
        rows = [r for r in rows if needle in " ".join(
            [r["title"], r["description"], *r["topics"], *r["guests"]]).casefold()]
    return rows
