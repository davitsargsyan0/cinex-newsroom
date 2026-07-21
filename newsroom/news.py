"""Fetch top stories from tech + general RSS feeds (+ optional NewsAPI), dedupe against posted history.

The account is tech-first, so each run fills a tech quota before it takes any
general top stories. Dedup runs across the combined pool, not per feed, so a
story carried by both a tech outlet and the general feed only goes out once.
"""

from dataclasses import dataclass

import feedparser
import httpx
from rapidfuzz import fuzz

from newsroom.config import settings
from newsroom.db import is_already_posted

TECH_FEEDS = [
    "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.arstechnica.com/arstechnica/index",
]
GENERAL_FEEDS = ["https://news.google.com/rss?hl=en"]

NEWSAPI_URL = "https://newsapi.org/v2/top-headlines"

FUZZY_DUP_THRESHOLD = 85  # title similarity above this % is treated as a duplicate

TECH = "tech"
GENERAL = "general"


@dataclass
class Story:
    title: str
    summary: str
    source: str
    url: str
    published: str
    category: str = GENERAL

    @property
    def story_key(self) -> str:
        """Stable-ish key for dedup storage. Real fuzzy-matching happens in dedupe_stories()."""
        return self.url or self.title


def _strip_outlet_suffix(title: str, source: str) -> str:
    """Drop the trailing ' - Outlet' that Google News appends to every headline.

    Left in, it ends up rendered onto the slide art and fed to the model as part
    of the story title. Only an exact match on the known source name is removed,
    so headlines that legitimately contain ' - ' survive intact.
    """
    suffix = f" - {source}"
    if source and title.endswith(suffix):
        return title[: -len(suffix)].strip()
    return title


def _fetch_rss(url: str, category: str) -> list[Story]:
    """Parse one RSS/Atom feed. feedparser normalises both formats to the same fields."""
    feed = feedparser.parse(url)
    stories = []
    for entry in feed.entries:
        source = entry.get("source", {}).get("title") or feed.feed.get("title", "RSS")
        stories.append(
            Story(
                title=_strip_outlet_suffix(entry.get("title", ""), source),
                summary=entry.get("summary", ""),
                source=source,
                url=entry.get("link", ""),
                published=entry.get("published", ""),
                category=category,
            )
        )
    return stories


def _fetch_feeds(urls: list[str], category: str) -> list[Story]:
    stories: list[Story] = []
    for url in urls:
        try:
            stories.extend(_fetch_rss(url, category))
        except Exception:  # noqa: BLE001 - one dead feed must not sink the run
            continue
    return stories


def _fetch_newsapi(category: str) -> list[Story]:
    if not settings.newsapi_key:
        return []

    params = {"apiKey": settings.newsapi_key, "language": "en", "pageSize": 20}
    if category == TECH:
        params["category"] = "technology"

    resp = httpx.get(NEWSAPI_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    stories = []
    for article in data.get("articles", []):
        stories.append(
            Story(
                title=article.get("title", ""),
                summary=article.get("description") or "",
                source=(article.get("source") or {}).get("name", "NewsAPI"),
                url=article.get("url", ""),
                published=article.get("publishedAt", ""),
                category=category,
            )
        )
    return stories


def _is_fuzzy_duplicate(title: str, seen_titles: list[str]) -> bool:
    return any(fuzz.token_sort_ratio(title, seen) >= FUZZY_DUP_THRESHOLD for seen in seen_titles)


def _take_unique(
    candidates: list[Story],
    quota: int,
    seen_titles: list[str],
) -> list[Story]:
    """Pull up to `quota` stories that are new to us and not near-duplicates of `seen_titles`.

    `seen_titles` is mutated so later calls dedupe against everything already chosen.
    """
    picked: list[Story] = []
    for story in candidates:
        if len(picked) >= quota:
            break
        if not story.title or not story.url:
            continue
        if is_already_posted(story.story_key):
            continue
        if _is_fuzzy_duplicate(story.title, seen_titles):
            continue
        picked.append(story)
        seen_titles.append(story.title)
    return picked


def fetch_top_stories(limit: int | None = None) -> list[Story]:
    """Fetch a tech-heavy mix: tech quota first, then general, capped at `limit` overall."""
    limit = limit or settings.top_stories_per_run

    tech_quota = min(settings.tech_stories_per_run, limit)
    seen_titles: list[str] = []

    tech_pool = _fetch_feeds(TECH_FEEDS, TECH) + _fetch_newsapi(TECH)
    selected = _take_unique(tech_pool, tech_quota, seen_titles)

    # Any unused tech slots roll into the general quota so a run is never short.
    general_quota = min(settings.general_stories_per_run + (tech_quota - len(selected)),
                        limit - len(selected))
    if general_quota > 0:
        general_pool = _fetch_feeds(GENERAL_FEEDS, GENERAL) + _fetch_newsapi(GENERAL)
        selected += _take_unique(general_pool, general_quota, seen_titles)

    return selected[:limit]
