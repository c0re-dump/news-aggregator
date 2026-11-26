"""Put each video into a category, a format, and a few topic tags.

Uses Claude when Anthropic credentials are available. Otherwise it falls back
to simple Bulgarian keyword rules, so the app still works without a key.
"""

import json
import logging
import os
import re
from collections import Counter

import anthropic

from . import db
from .config import CATEGORIES, CLAUDE_MODEL, FORMATS

log = logging.getLogger(__name__)
BATCH = 25

# ---------------------------------------------------------------- Claude

SYSTEM = f"""You sort Bulgarian TV news and talk-show videos from YouTube.
For each video you get its id, channel, title and the start of its description.

Return for every video:
- category: the single best fit, one of:
{chr(10).join(f'  - {k}: {v[1]}' for k, v in CATEGORIES.items())}
- format: one of: {", ".join(f"{k} ({v})" for k, v in FORMATS.items())}.
  A guest in the studio answering questions is "interview". A full morning show or a full newscast is "full_show".
- topics: 1-4 short tags in Bulgarian for the concrete story, so the user can find every video about it
  across channels. Use people's full names ("Благомир Коцев"), places ("Варна"), or named issues
  ("Бюджет 2027", "Еврозона", "Войната в Украйна"). Nominative case, no hashtags, no generic words
  like "политика" or "новини". Reuse a tag from the "known topics" list when it is the same story.

Return one result per input video, with the same id."""

SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "format": {"type": "string", "enum": list(FORMATS)},
                    "topics": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "category", "format", "topics"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def claude_available() -> bool:
    if os.environ.get("NEWS_APP_NO_LLM"):
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("ANTHROPIC_PROFILE")
                or os.path.isdir(os.path.expanduser("~/.config/anthropic")))


def _known_topics(limit: int = 120) -> list[str]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT topics FROM videos WHERE classified_by LIKE 'claude%' ORDER BY published_at DESC LIMIT 1500"
        ).fetchall()
    c = Counter(t for (raw,) in rows for t in json.loads(raw))
    return [t for t, _ in c.most_common(limit)]


def classify_with_claude(client: anthropic.Anthropic, rows: list) -> list[dict]:
    items = [{
        "id": r["id"], "channel": r["channel_name"], "title": r["title"],
        "description": (r["description"] or "")[:400],
        **({"duration_min": round(r["duration_s"] / 60)} if r["duration_s"] else {}),
    } for r in rows]
    known = _known_topics()
    user = (f"Known topics: {json.dumps(known, ensure_ascii=False)}\n\n" if known else "") + \
        "Videos:\n" + json.dumps(items, ensure_ascii=False, indent=1)

    # If the model declines a request, the API retries it on a fallback model.
    # Haiku has no server-side fallback; a declined batch falls back to keyword rules.
    fallback = {} if "haiku" in CLAUDE_MODEL else {
        "betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    resp = client.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": user}],
        **fallback,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the batch: {resp.stop_details}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude output was cut off (max_tokens)")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)["results"]


# ---------------------------------------------------------------- keyword fallback

RULES: list[tuple[str, str]] = [
    ("elections", r"избор|вот\b|вота|гласув|кандидат|предизборн|изборн|референдум|социолог|проучван"),
    ("world", r"украйн|русия|путин|тръмп|сащ|израел|газа|китай|нато|брюксел|европейск|\bес\b|германи|франци|турци|сърби|гърци|румъни|македони|зеленски|войн"),
    ("justice_crime", r"съд|прокур|арест|задържа|обвин|полиц|мвр|дело|присъд|корупц|измам|разслед|криминал|убийств"),
    ("incidents", r"катастроф|пожар|наводн|бедств|загина|инцидент|земетрес|буря|снеговал|верижн|авари"),
    ("economy", r"икономик|бюджет|данъ|инфлац|цени|цена|евро(то)?\b|еврозон|заплат|пенси|бизнес|банк|ток|газ\b|горив|ваучер|nvidia|борс|пазар|търгов"),
    ("domestic_politics", r"парламент|народно събрание|депутат|министър|правителств|кабинет|партия|герб|пп-дб|възраждане|дпс|бсп|итн|пеевски|борисов|радев|опозиц|коалиц|вотум|всс"),
    ("local", r"кмет|общин|столич|софия|варна|пловдив|бургас|русе|стара загора|плевен|благоевград|квартал|междублоков"),
    ("society", r"здрав|болниц|лекар|образован|училищ|учител|студент|протест|стачк|социал|деца|пенсионер"),
    ("tech_science", r"технолог|изкуствен интелект|\bai\b|наук|космос|учен|иноваци|робот"),
    ("sport", r"спорт|футбол|цска|левски|лудогорец|мач|шампион|олимп|ръгби|волейбол|тенис"),
    ("culture_lifestyle", r"зад кулисите|филм|музик|концерт|театър|актьор|певиц|изложб|книга|кулинар|рецепт|коледн|празни"),
]


def classify_with_keywords(r) -> dict:
    text = f"{r['title']} {r['description'][:300]}".casefold()
    title = r["title"].casefold()
    scores = Counter()
    for cat, pat in RULES:
        scores[cat] += 2 * len(re.findall(pat, title)) + len(re.findall(pat, text))
    category = scores.most_common(1)[0][0] if scores and scores.most_common(1)[0][1] > 0 else "other"

    if r["duration_s"] and r["duration_s"] > 60 * 50:
        fmt = "full_show"
    elif re.search(r"емисия|новините на|\| \d+ \w+\.? \d{4}$", title):
        fmt = "full_show"
    elif ":" in r["title"].split("|")[0] or re.search(r"интервю|гост|разговор с", title):
        fmt = "interview"
    else:
        fmt = "report"

    tags = [t.strip("#").capitalize() for t in re.findall(r"#[\w-]+", r["title"])][:4]
    return {"id": r["id"], "category": category, "format": fmt, "topics": tags}


# ---------------------------------------------------------------- entry point

def classify_pending(limit: int = 500) -> int:
    rows = db.unclassified(limit)
    if not rows:
        return 0
    done = 0
    client = anthropic.Anthropic() if claude_available() else None
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        results, by = None, "keywords"
        if client is not None:
            try:
                results, by = classify_with_claude(client, batch), f"claude:{CLAUDE_MODEL}"
            except anthropic.AuthenticationError:
                log.warning("Anthropic credentials rejected; using keyword rules from now on")
                client = None
            except (anthropic.APIError, RuntimeError, ValueError, StopIteration) as e:
                log.warning("Claude batch failed (%s); using keyword rules for this batch", e)
        if results is None:
            results = [classify_with_keywords(r) for r in batch]
        valid_ids = {r["id"] for r in batch}
        returned = {res["id"] for res in results}
        results += [classify_with_keywords(r) for r in batch if r["id"] not in returned]
        for res in results:
            if res["id"] not in valid_ids:
                continue
            db.save_classification(res["id"], res["category"], res["format"], res["topics"], by)
            done += 1
        log.info("classified %d/%d (%s)", min(i + BATCH, len(rows)), len(rows), by)
    return done
