import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from .env. Real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")
CHANNELS_FILE = Path(os.environ.get("NEWS_APP_CHANNELS", ROOT / "channels.toml"))
DB_PATH = Path(os.environ.get("NEWS_APP_DB", ROOT / "data" / "news.db"))

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY") or None
CLAUDE_MODEL = os.environ.get("NEWS_APP_MODEL", "claude-opus-5-5")
REFRESH_MINUTES = int(os.environ.get("NEWS_APP_REFRESH_MINUTES", "30"))
# With the YouTube Data API key: how many days of history to load per channel,
# and a safety cap on videos per channel.
BACKFILL_DAYS = int(os.environ.get("NEWS_APP_BACKFILL_DAYS", "31"))
MAX_PER_CHANNEL = int(os.environ.get("NEWS_APP_MAX_PER_CHANNEL", "2000"))

# key -> (Bulgarian label, short guidance for the classifier)
CATEGORIES: dict[str, tuple[str, str]] = {
    "domestic_politics": ("Вътрешна политика", "Bulgarian parliament, government, parties, ministers, political scandals."),
    "elections": ("Избори", "Elections, campaigns, polls, voting, referendums (Bulgarian or foreign)."),
    "local": ("Местни новини", "City/municipality/regional news: mayors, local infrastructure, local incidents."),
    "world": ("Световни новини", "Foreign affairs, international politics, wars, EU/NATO, other countries."),
    "economy": ("Икономика и бизнес", "Economy, prices, inflation, euro adoption, budget, taxes, markets, companies, energy prices."),
    "justice_crime": ("Съд и криминални", "Courts, prosecution, police, arrests, crime, corruption investigations."),
    "society": ("Общество", "Healthcare, education, social issues, protests, strikes, pensions, consumer topics."),
    "incidents": ("Инциденти и бедствия", "Accidents, fires, floods, weather emergencies, disasters."),
    "tech_science": ("Технологии и наука", "Technology, AI, science, space, innovation."),
    "culture_lifestyle": ("Култура и лайфстайл", "Culture, celebrities, entertainment, food, travel, behind-the-scenes."),
    "sport": ("Спорт", "Sports."),
    "other": ("Друго", "Anything that fits none of the above."),
}

FORMATS: dict[str, str] = {
    "interview": "Интервю / гост в студио",
    "report": "Репортаж / новина",
    "panel": "Дискусия / коментар",
    "full_show": "Цяло предаване / емисия",
}


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    handle: str = ""
    enabled: bool = True
    include: str = ""       # keep only videos whose title contains this text
    segment: bool = False   # split long full-show videos into timed segments


def load_channels() -> list[Channel]:
    with open(CHANNELS_FILE, "rb") as f:
        data = tomllib.load(f)
    channels = [Channel(**c) for c in data.get("channel", [])]
    return [c for c in channels if c.enabled]
