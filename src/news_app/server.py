import logging
import threading
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from . import classifier, db, fetcher, segmenter
from .config import CATEGORIES, FORMATS, REFRESH_MINUTES, load_channels

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
_lock = threading.Lock()
_stop = threading.Event()
status = {"last_refresh": None, "running": False, "last_error": None}


def run_update(backfill: bool = False) -> None:
    if not _lock.acquire(blocking=False):
        return  # an update is already running
    status["running"] = True
    try:
        new = fetcher.refresh(backfill=backfill)
        done = classifier.classify_pending()
        segs = segmenter.segment_pending() if classifier.claude_available() else 0
        log.info("update: %d new videos, %d classified, %d show segments", new, done, segs)
        status["last_error"] = None
    except Exception as e:
        log.exception("update failed")
        status["last_error"] = str(e)
    finally:
        status["last_refresh"] = db.now_iso()
        status["running"] = False
        _lock.release()


def _loop() -> None:
    while not _stop.is_set():
        run_update()
        _stop.wait(REFRESH_MINUTES * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_loop, daemon=True, name="updater").start()
    yield
    _stop.set()


app = FastAPI(title="Bulgarian news sorter", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta():
    return {
        "categories": {k: v[0] for k, v in CATEGORIES.items()},
        "formats": FORMATS,
        "channels": [{"id": c.id, "name": c.name, "handle": c.handle} for c in load_channels()],
        "status": status,
        "llm": classifier.claude_available(),
    }


@app.get("/api/videos")
def videos(span: str = "day", category: str | None = None, channel: str | None = None,
           topic: str | None = None, q: str | None = None,
           format: str | None = None):
    # Counts and topic list use every filter except the category itself,
    # so the category chips show how many videos each tab would contain.
    base = db.query_videos(span, None, channel, topic, q)
    if format:
        base = [v for v in base if v["format"] == format]
    counts = Counter(v["category"] or "pending" for v in base)
    rows = [v for v in base if not category or v["category"] == category]
    topics = Counter(t for v in rows for t in v["topics"])
    return {
        "videos": rows,
        "counts": counts,
        "topics": [{"name": t, "count": n} for t, n in topics.most_common(40) if n > 1 or len(topics) < 15],
        "status": status,
    }


@app.get("/api/show/{video_id}")
def show(video_id: str):
    return {"segments": db.show_segments(video_id)}


@app.post("/api/refresh")
def refresh():
    threading.Thread(target=run_update, daemon=True).start()
    return {"started": True}
