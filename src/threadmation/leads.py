"""Crawl leads: keyword search (Threads & X), mention, dan balasan di post Threads Anda."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .client import ThreadsAPIError, ThreadsClient
from .config import ProfileRules, Rules
from .notify import Notifier
from .social import SocialBridge, SocialBridgeError
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


def score_profile(author: dict | None, profile: ProfileRules) -> tuple[float, list[str]]:
    """Skor dari profil penulis. -1 kalau dibuang (kata exclude / jumlah follower di luar batas).

    Follower yang tidak diketahui tidak pernah dibuang, dan centang biru tidak menambah skor:
    yang dicari orang yang benar-benar membangun, bukan akun besar.
    """
    if not author:
        return 0.0, []
    bio = (author.get("bio") or "").lower()
    if any(_contains(bio, bad) for bad in profile.exclude):
        return -1.0, []
    followers = author.get("followers")
    if isinstance(followers, int):
        if profile.min_followers and followers < profile.min_followers:
            return -1.0, []
        if profile.max_followers and followers > profile.max_followers:
            return -1.0, []
    matched = [kw for kw in profile.include if _contains(bio, kw)]
    score = sum(profile.include[kw] for kw in matched)
    low, high = profile.mid_followers
    if profile.mid_bonus and isinstance(followers, int) and high and low <= followers <= high:
        score += profile.mid_bonus
        matched.append(f"followers {low}-{high}")
    return score, [f"bio:{kw}" if not kw.startswith("followers") else kw for kw in matched]


@dataclass
class CrawlReport:
    new_leads: list[dict] = field(default_factory=list)
    scanned: int = 0
    errors: list[str] = field(default_factory=list)


class LeadCrawler:
    def __init__(
        self,
        client: ThreadsClient,
        store: Store,
        rules: Rules,
        notifier: Notifier | None = None,
        social: SocialBridge | None = None,
    ):
        self.client = client
        self.store = store
        self.rules = rules
        self.notifier = notifier or Notifier()
        self.social = social or SocialBridge()
        self._profiles: dict[str, dict | None] = {}
        self._enrich_ok = True

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
        except (ThreadsAPIError, SocialBridgeError) as exc:
            log.error("Crawl %s gagal: %s", name, exc)
            report.errors.append(f"{name}: {exc}")

    # ---------------------------------------------------------------------
    def ingest(self, post: dict, source: str, report: CrawlReport, query: str | None = None, require_score: bool = False) -> bool:
        """Simpan satu post sebagai lead (dipakai crawler & webhook)."""
        report.scanned += 1
        platform = post.get("platform") or "threads"
        if not post.get("id") or (platform == "threads" and post.get("username") == self._own_username()):
            return False
        score, matched = score_text(post.get("text"), self.rules)
        if score < 0:
            return False
        p_score, p_matched = score_profile(post.get("author"), self.rules.profile)
        if p_score < 0:
            return False
        score, matched = score + p_score, matched + p_matched
        if require_score and score < self.rules.min_score:
            return False
        if self.store.add_lead(post, source, query=query, score=score, matched=matched):
            lead = self.store.get_lead(post["id"])
            report.new_leads.append(lead)
            self.notifier.emit("lead.created", lead)
            return True
        return False

    def crawl_search(self, report: CrawlReport) -> None:
        for q in self.rules.queries:
            for platform in q.get("platforms", self.rules.platforms):
                try:
                    results = self._search(platform, q)
                except (ThreadsAPIError, SocialBridgeError) as exc:
                    # Satu platform gagal tidak menghentikan platform/query lain.
                    log.error("Search %s %r gagal: %s", platform, q["q"], exc)
                    report.errors.append(f"search {platform} {q['q']!r}: {exc}")
                    continue
                for post in results:
                    if platform == "threads" and not self._worth_enriching(post):
                        report.scanned += 1
                        continue
                    if platform == "threads":
                        post.setdefault("author", self._threads_profile(post.get("username")))
                    self.ingest(post, "search", report, query=q["q"], require_score=True)

    def _search(self, platform: str, q: dict) -> list[dict]:
        limit = int(q.get("limit", self.rules.results_per_query))
        if platform == "threads":
            return self.client.keyword_search(
                q["q"],
                search_type=q.get("search_type", self.rules.search_type),
                search_mode=q.get("search_mode", "KEYWORD"),
                media_type=q.get("media_type"),
                limit=limit,
            )
        if platform == "x":
            # Retweet hanya mengulang post orang lain, jadi dibuang dari hasil.
            query = q.get("x_query") or f"{q['q']} -is:retweet"
            return self.social.search("x", query, limit=limit)
        raise SocialBridgeError(f"platform search tidak dikenal: {platform}")

    def _worth_enriching(self, post: dict) -> bool:
        """Lewati post yang sudah tersimpan atau kena exclude, supaya tidak boros panggilan profil."""
        if not post.get("id") or self.store.get_lead(post["id"]):
            return False
        return score_text(post.get("text"), self.rules)[0] >= 0

    def _threads_profile(self, username: str | None) -> dict | None:
        if not username or not self.rules.profile.enrich_threads or not self._enrich_ok:
            return None
        if username not in self._profiles:
            try:
                p = self.client.profile_lookup(username)
                self._profiles[username] = {
                    "name": p.get("name"),
                    "bio": p.get("biography"),
                    "followers": p.get("follower_count"),
                    "verified": p.get("is_verified"),
                }
            except ThreadsAPIError as exc:
                # Biasanya permission threads_profile_discovery belum aktif: matikan untuk run ini.
                log.warning("Profil Threads tidak bisa diambil (%s); lanjut tanpa skor profil", exc)
                self._enrich_ok = False
                return None
        return self._profiles[username]

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
