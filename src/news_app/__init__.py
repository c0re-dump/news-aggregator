import argparse
import logging


def main() -> None:
    p = argparse.ArgumentParser(prog="news-app", description="Sort Bulgarian YouTube news videos by category.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the web app (also refreshes in the background)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    r = sub.add_parser("refresh", help="fetch new videos and classify them once")
    r.add_argument("--backfill", action="store_true", help="with YOUTUBE_API_KEY: load older videos too")
    sub.add_parser("reclassify", help="clear all categories and classify every video again")
    g = sub.add_parser("segment", help="split full shows into timed segments (all pending, or one video id)")
    g.add_argument("video_id", nargs="?")
    h = sub.add_parser("resolve", help="find the channel ID for a @handle")
    h.add_argument("handle")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("news_app.server:app", host=args.host, port=args.port)
    elif args.cmd == "refresh":
        from . import classifier, fetcher
        print(f"new videos: {fetcher.refresh(backfill=args.backfill)}")
        print(f"classified: {classifier.classify_pending(limit=100_000)}")
        from . import segmenter
        print(f"show segments: {segmenter.segment_pending()}")
    elif args.cmd == "segment":
        import anthropic
        from . import db, segmenter
        if args.video_id:
            with db.connect() as conn:
                row = conn.execute("SELECT * FROM videos WHERE id = ?", (args.video_id,)).fetchone()
            if not row:
                raise SystemExit(f"video not in database: {args.video_id}")
            print(f"segments: {segmenter.segment_video(anthropic.Anthropic(), dict(row))}")
        else:
            print(f"segments: {segmenter.segment_pending()}")
    elif args.cmd == "reclassify":
        from . import classifier, db
        with db.connect() as conn:
            conn.execute("UPDATE videos SET category = NULL")
        print(f"classified: {classifier.classify_pending(limit=100_000)}")
    elif args.cmd == "resolve":
        from .fetcher import resolve_handle
        res = resolve_handle(args.handle)
        if not res:
            raise SystemExit(f"channel not found: {args.handle}")
        cid, title = res
        print(f'[[channel]]\nname = "{title}"\nhandle = "{args.handle}"\nid = "{cid}"')
