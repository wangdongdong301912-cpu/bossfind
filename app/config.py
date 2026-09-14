from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_path: Path
    credential_ttl_minutes: int
    headless: bool
    target_url: str
    browser_cdp_url: str


@lru_cache
def get_settings() -> Settings:
    raw_db = Path(os.getenv("BOSSFIND_DATABASE", "data/bossfind.db"))
    database_path = raw_db if raw_db.is_absolute() else ROOT / raw_db
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return Settings(
        database_path=database_path,
        credential_ttl_minutes=max(5, int(os.getenv("BOSSFIND_CREDENTIAL_TTL_MINUTES", "30"))),
        headless=_as_bool(os.getenv("BOSSFIND_HEADLESS", "false")),
        target_url=os.getenv("BOSSFIND_TARGET_URL", "https://www.zhipin.com/jiaxing/?seoRefer=index"),
        browser_cdp_url=os.getenv("BOSSFIND_BROWSER_CDP_URL", "http://127.0.0.1:9222"),
    )
