# news-app

Collects videos from Bulgarian news YouTube channels and sorts them by category
(domestic politics, elections, world, local, economy, …), format (interview, report, full show)
and topic tags (people, places, stories). You pick the time span: day, week or month.

## Run

```bash
uv run news-app serve          # http://127.0.0.1:8000 — refreshes every 30 min in the background
uv run news-app refresh        # one fetch + classify pass, no web server
uv run news-app resolve @Handle   # prints a [[channel]] block for channels.toml
uv run news-app reclassify     # classify all stored videos again (e.g. after you add a Claude key)
uv run news-app segment [ID]   # split full shows into timed segments (all pending, or one video)
```

## Full shows (BNT "Денят започва")

A channel with `segment = true` in `channels.toml` is split into parts:
yt-dlp downloads YouTube's automatic Bulgarian subtitles, and Claude reads them and returns
one segment per interview or report (title, guests, start/end time, category, topics).
Segments appear in the lists like normal videos and play inside the page from start to end.
If the subtitles are not ready yet, the app tries again on the next refreshes (up to 6 times).
When YouTube changes and subtitle download fails, update yt-dlp: `uv lock --upgrade-package yt-dlp`.

## Configuration (environment variables)

| Variable | Effect |
|---|---|
| `ANTHROPIC_API_KEY` | Claude classifies the videos and makes topic tags. Without it, simple keyword rules are used. |
| `NEWS_APP_MODEL` | Claude model. Default `claude-opus-5-5`. |
| `YOUTUBE_API_KEY` | Backfill older videos (`refresh --backfill`), get exact durations and mark Shorts. Without it, the RSS feed gives the newest 15 videos per channel. |
| `NEWS_APP_REFRESH_MINUTES` | Refresh interval. Default 30. |
| `NEWS_APP_BACKFILL_DAYS` | Days of history to load with the API. Default 31. |

Put keys in a `.env` file in the project folder (one `KEY=value` per line). The file is in `.gitignore`.
Channels are in `channels.toml`. Data is in `data/news.db` (SQLite).
