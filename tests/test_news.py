import pytest

from newsroom import news
from newsroom.news import (
    GENERAL,
    TECH,
    Story,
    _is_fuzzy_duplicate,
    _strip_outlet_suffix,
    fetch_top_stories,
)


def test_strips_google_news_outlet_suffix():
    title = "Here are the 30,000 songs Sony is suing Udio over - The Verge"

    assert _strip_outlet_suffix(title, "The Verge") == (
        "Here are the 30,000 songs Sony is suing Udio over"
    )


def test_keeps_hyphens_that_are_part_of_the_headline():
    title = "Apple's M5 - the chip that changes everything"

    assert _strip_outlet_suffix(title, "The Verge") == title


def test_ignores_a_suffix_that_is_not_the_source():
    title = "Something happened - Reuters"

    assert _strip_outlet_suffix(title, "The Verge") == title


def test_fuzzy_duplicate_detects_near_identical_titles():
    seen = ["Fed raises interest rates by half a point"]
    assert _is_fuzzy_duplicate("Fed raises interest rates by 0.5 point", seen) is True


def test_fuzzy_duplicate_allows_distinct_titles():
    seen = ["Fed raises interest rates by half a point"]
    assert _is_fuzzy_duplicate("Local team wins championship game", seen) is False


# Deliberately unrelated wording: titles that merely differ by an index would trip
# the fuzzy duplicate filter and make these tests measure the wrong thing.
TECH_TITLES = [
    "Chipmaker unveils a processor built for on-device inference",
    "Regulators open an inquiry into app store billing rules",
    "Open source database project announces a major rewrite",
    "Satellite startup raises funding for orbital compute",
    "Browser vendor ships a new sandboxing architecture",
]
GENERAL_TITLES = [
    "Coastal cities adopt new flood defence standards",
    "Central bank holds rates steady for a third quarter",
    "Archaeologists document an untouched burial site",
    "Rail operators agree on a cross-border timetable",
    "Health agency updates its vaccination guidance",
]


def _story(title: str, category: str) -> Story:
    slug = title.lower().replace(" ", "-")
    return Story(
        title=title,
        summary="summary",
        source="Test Source",
        url=f"https://example.com/{slug}",
        published="2026-07-21",
        category=category,
    )


def _stories(titles: list[str], category: str, count: int) -> list[Story]:
    return [_story(title, category) for title in titles[:count]]


@pytest.fixture
def offline_feeds(monkeypatch):
    """Stub the network and the posted-history lookup; tests set the pools."""
    pools = {TECH: [], GENERAL: []}

    monkeypatch.setattr(news, "_fetch_feeds", lambda urls, category: list(pools[category]))
    monkeypatch.setattr(news, "_fetch_newsapi", lambda category: [])
    monkeypatch.setattr(news, "is_already_posted", lambda key: False)
    return pools


def test_fills_tech_quota_before_general(offline_feeds, monkeypatch):
    monkeypatch.setattr(news.settings, "tech_stories_per_run", 2)
    monkeypatch.setattr(news.settings, "general_stories_per_run", 1)
    offline_feeds[TECH] = _stories(TECH_TITLES, TECH, 5)
    offline_feeds[GENERAL] = _stories(GENERAL_TITLES, GENERAL, 5)

    selected = fetch_top_stories(limit=3)

    assert [s.category for s in selected] == [TECH, TECH, GENERAL]


def test_general_backfills_when_tech_pool_is_thin(offline_feeds, monkeypatch):
    monkeypatch.setattr(news.settings, "tech_stories_per_run", 2)
    monkeypatch.setattr(news.settings, "general_stories_per_run", 1)
    offline_feeds[TECH] = _stories(TECH_TITLES, TECH, 1)
    offline_feeds[GENERAL] = _stories(GENERAL_TITLES, GENERAL, 5)

    selected = fetch_top_stories(limit=3)

    # The unused tech slot rolls into general rather than shortening the run.
    assert len(selected) == 3
    assert [s.category for s in selected] == [TECH, GENERAL, GENERAL]


def test_story_carried_by_both_pools_is_not_duplicated(offline_feeds, monkeypatch):
    monkeypatch.setattr(news.settings, "tech_stories_per_run", 2)
    monkeypatch.setattr(news.settings, "general_stories_per_run", 1)
    shared_title = "Chipmaker unveils a new processor line"
    offline_feeds[TECH] = [_story(shared_title, TECH)]
    offline_feeds[GENERAL] = [
        _story(shared_title, GENERAL),
        _story("Something else entirely happened", GENERAL),
    ]

    selected = fetch_top_stories(limit=3)

    titles = [s.title for s in selected]
    assert titles.count(shared_title) == 1


def test_never_exceeds_overall_limit(offline_feeds, monkeypatch):
    monkeypatch.setattr(news.settings, "tech_stories_per_run", 5)
    monkeypatch.setattr(news.settings, "general_stories_per_run", 5)
    offline_feeds[TECH] = _stories(TECH_TITLES, TECH, 5)
    offline_feeds[GENERAL] = _stories(GENERAL_TITLES, GENERAL, 5)

    assert len(fetch_top_stories(limit=3)) == 3


def test_skips_already_posted_stories(offline_feeds, monkeypatch):
    monkeypatch.setattr(news.settings, "tech_stories_per_run", 2)
    monkeypatch.setattr(news.settings, "general_stories_per_run", 0)
    offline_feeds[TECH] = _stories(TECH_TITLES, TECH, 3)
    already_posted_url = _story(TECH_TITLES[0], TECH).url
    monkeypatch.setattr(news, "is_already_posted", lambda key: key == already_posted_url)

    titles = [s.title for s in fetch_top_stories(limit=2)]

    assert TECH_TITLES[0] not in titles
    assert len(titles) == 2
