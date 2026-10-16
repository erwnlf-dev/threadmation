"""Crawl leads dari Threads: keyword search, mention, dan balasan di post Anda."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .client import ThreadsAPIError, ThreadsClient
from .config import Rules
from .notify import Notifier
from .store import Store

log = logging.getLogger(__name__)


def _contains(text: str, keyword: str) -> bool:
    # Batas kata supaya "ai" tidak cocok dengan "pakai".
    return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text) is not None


def score_text(text: str | None, rules: Rules) -> tuple[float, list[str]]:
    """Return (skor, keyword cocok). Skor -1 kalau kena kata exclude."""
    t = (text or "").lower()
    if any(_contains(t, bad) for bad in rules.exclude):
        return -1.0, []
    matched = [kw for kw in rules.include if _contains(t, kw)]
    return sum(rules.include[kw] for kw in matched), matched


@dataclass
class CrawlReport:
    new_leads: list[dict] = field(default_factory=list)
    scanned: int = 0
    errors: list[str] = field(default_factory=list)


class LeadCrawler:
    def __init__(self, client: ThreadsClient, store: Store, rules: Rules, notifier: Notifier | None = None):
        self.client = client
        self.store = store
        self.rules = rules
        self.notifier = notifier or Notifier()

    def run(self, search: bool = True, mentions: bool | None = None, replies: bool | None = None) -> CrawlReport:
        report = CrawlReport()
        mentions = self.rules.crawl_mentions if mentions is None else mentions
        replies = self.rules.crawl_own_replies if replies is None else replies
        if search:
            self._guard(report, "search", self.crawl_search, report)
        if mentions:
            self._guard(report, "mentions", self.crawl_mentions, report)
        if replies:
            self._guard(report, "replies", self.crawl_own_replies, report)
        return report

    @staticmethod
    def _guard(report: CrawlReport, name: str, fn, *args) -> None:
        try:
            fn(*args)
        except ThreadsAPIError as exc:
            log.error("Crawl %s gagal: %s", name, exc)
            report.errors.append(f"{name}: {exc}")

    # ---------------------------------------------------------------------
    def ingest(self, post: dict, source: str, report: CrawlReport, query: str | None = None, require_score: bool = False) -> bool:
        """Simpan satu post sebagai lead (dipakai crawler & webhook)."""
        report.scanned += 1
        if not post.get("id") or post.get("username") == self._own_username():
            return False
        score, matched = score_text(post.get("text"), self.rules)
        if score < 0 or (require_score and score < self.rules.min_score):
            return False
        if self.store.add_lead(post, source, query=query, score=score, matched=matched):
            lead = self.store.get_lead(post["id"])
            report.new_leads.append(lead)
            self.notifier.emit("lead.created", lead)
            return True
        return False

    def crawl_search(self, report: CrawlReport) -> None:
        for q in self.rules.queries:
            query = q["q"]
            results = self.client.keyword_search(
                query,
                search_type=q.get("search_type", self.rules.search_type),
                search_mode=q.get("search_mode", "KEYWORD"),
                media_type=q.get("media_type"),
                limit=int(q.get("limit", self.rules.results_per_query)),
            )
            for post in results:
                self.ingest(post, "search", report, query=query, require_score=True)

    def crawl_mentions(self, report: CrawlReport) -> None:
        for post in self.client.mentions():
            self.ingest(post, "mention", report)

    def crawl_own_replies(self, report: CrawlReport) -> None:
        for own in self.client.user_threads(limit=self.rules.own_posts_lookback):
            if not own.get("has_replies", True):
                continue
            for reply in self.client.conversation(own["id"]):
                self.ingest(reply, "reply", report)

    def _own_username(self) -> str | None:
        try:
            return self.client.username
        except ThreadsAPIError:
            return None
