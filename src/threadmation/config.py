"""Konfigurasi dari environment / file .env, plus file rules YAML."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .client import DEFAULT_BASE_URL


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Loader .env minimal (tanpa dependency). Tidak menimpa env yang sudah ada."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass
class Settings:
    access_token: str = ""
    user_id: str = ""
    app_secret: str = ""
    webhook_verify_token: str = ""
    api_key: str = ""
    db_path: str = "threadmation.db"
    rules_path: str = "config/rules.yaml"
    event_webhook_url: str = ""
    api_base: str = DEFAULT_BASE_URL
    webhook_auto_reply: bool = False
    social_bridge_dir: str = ""
    crosspost: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(os.environ.get("THREADMATION_ENV_FILE", ".env"))
        e = os.environ.get
        return cls(
            access_token=e("THREADS_ACCESS_TOKEN", ""),
            user_id=e("THREADS_USER_ID", ""),
            app_secret=e("THREADS_APP_SECRET", ""),
            webhook_verify_token=e("THREADS_WEBHOOK_VERIFY_TOKEN", ""),
            api_key=e("THREADMATION_API_KEY", ""),
            db_path=e("THREADMATION_DB", "threadmation.db"),
            rules_path=e("THREADMATION_RULES", "config/rules.yaml"),
            event_webhook_url=e("THREADMATION_EVENT_WEBHOOK_URL", ""),
            api_base=e("THREADS_API_BASE", DEFAULT_BASE_URL) or DEFAULT_BASE_URL,
            webhook_auto_reply=e("THREADMATION_WEBHOOK_AUTO_REPLY", "").lower() in ("1", "true", "yes"),
            social_bridge_dir=e("THREADMATION_SOCIAL_BRIDGE", ""),
            crosspost=[x.strip() for x in e("THREADMATION_CROSSPOST", "").split(",") if x.strip()],
        )


@dataclass
class ResponseRule:
    name: str
    match_any: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=lambda: ["mention", "reply"])
    reply: str | None = None
    dm: str | None = None
    hide: bool = False


@dataclass
class Rules:
    queries: list[dict] = field(default_factory=list)
    include: dict[str, float] = field(default_factory=dict)
    exclude: list[str] = field(default_factory=list)
    min_score: float = 1.0
    search_type: str = "RECENT"
    results_per_query: int = 25
    crawl_own_replies: bool = True
    crawl_mentions: bool = True
    own_posts_lookback: int = 10
    responses: list[ResponseRule] = field(default_factory=list)
    max_replies_per_run: int = 10
    reply_delay_seconds: float = 20.0

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Rules":
        p = Path(path)
        if not p.is_file():
            example = Path(__file__).resolve().parents[2] / "config" / "rules.example.yaml"
            if example.is_file():
                p = example
            else:
                return cls()
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        leads = raw.get("leads", {}) or {}
        responder = raw.get("responder", {}) or {}
        queries = [q if isinstance(q, dict) else {"q": str(q)} for q in leads.get("queries", [])]
        return cls(
            queries=queries,
            include={str(k).lower(): float(v) for k, v in (leads.get("include") or {}).items()},
            exclude=[str(x).lower() for x in leads.get("exclude", [])],
            min_score=float(leads.get("min_score", 1.0)),
            search_type=leads.get("search_type", "RECENT"),
            results_per_query=int(leads.get("results_per_query", 25)),
            crawl_own_replies=bool(leads.get("crawl_own_replies", True)),
            crawl_mentions=bool(leads.get("crawl_mentions", True)),
            own_posts_lookback=int(leads.get("own_posts_lookback", 10)),
            responses=[ResponseRule(**r) for r in responder.get("rules", [])],
            max_replies_per_run=int(responder.get("max_replies_per_run", 10)),
            reply_delay_seconds=float(responder.get("reply_delay_seconds", 20)),
        )
