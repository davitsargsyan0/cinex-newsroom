import httpx
import pytest
import respx

from newsroom import images
from newsroom.generate import ImageBrief
from newsroom.images import Candidate

HEADLINE = "Motorola tablet confirmed to feature nine JBL speakers"
QUERIES = ["speaker cone macro", "person holding tablet", "dark studio desk"]


@pytest.fixture(autouse=True)
def stub_settings(monkeypatch):
    monkeypatch.setattr(images.settings, "pexels_api_key", "pexels-key")
    monkeypatch.setattr(images.settings, "openai_api_key", "openai-key")
    monkeypatch.setattr(images.settings, "images_per_post", 3)


@pytest.fixture
def no_selection_call(monkeypatch):
    """Use the deterministic fallback so these tests don't depend on a model call."""
    monkeypatch.setattr(
        images,
        "_select_candidates",
        lambda headline, candidates, count: images._one_per_query(candidates, count),
    )


def _photo(photo_id: int, alt: str) -> dict:
    return {
        "id": photo_id,
        "alt": alt,
        "photographer": f"Photographer {photo_id}",
        "src": {"original": f"https://images.pexels.com/photos/{photo_id}/x.jpeg"},
    }


def _mock_pexels(mock, photos_by_query: dict[str, list[dict]]):
    """Serve a distinct result set per query and record the queries issued."""
    issued: list[str] = []

    def handler(request):
        query = dict(httpx.URL(str(request.url)).params)["query"]
        issued.append(query)
        return httpx.Response(200, json={"photos": photos_by_query.get(query, [])})

    mock.get(images.PEXELS_SEARCH_URL).mock(side_effect=handler)
    return issued


def _mock_downloads(mock):
    downloaded: list[str] = []

    def handler(request):
        downloaded.append(str(request.url))
        return httpx.Response(200, content=b"jpeg-bytes")

    mock.get(url__regex=r"https://images\.pexels\.com/photos/.*").mock(side_effect=handler)
    return downloaded


def test_searches_once_per_query_and_never_joins_them(no_selection_call):
    brief = ImageBrief(queries=QUERIES, ai_prompt="x")
    photos = {q: [_photo(i * 10 + n, f"{q} {n}") for n in range(5)] for i, q in enumerate(QUERIES)}

    with respx.mock(assert_all_called=False) as mock:
        issued = _mock_pexels(mock, photos)
        _mock_downloads(mock)
        images.get_images(brief, count=3, headline=HEADLINE)

    assert issued == QUERIES
    # The old bug: every keyword concatenated into a single broad query.
    assert " ".join(QUERIES) not in issued


def test_downloads_at_twice_the_canvas_not_a_preview(no_selection_call):
    brief = ImageBrief(queries=QUERIES[:1], ai_prompt="x")

    with respx.mock(assert_all_called=False) as mock:
        _mock_pexels(mock, {QUERIES[0]: [_photo(1, "a"), _photo(2, "b"), _photo(3, "c")]})
        downloaded = _mock_downloads(mock)
        images.get_images(brief, count=3, headline=HEADLINE)

    for url in downloaded:
        assert "w=2160" in url and "h=2700" in url and "fit=crop" in url
        assert "large2x" not in url


def test_only_the_chosen_images_are_downloaded(no_selection_call):
    """15 candidates per query are inspected, but only `count` are fetched."""
    brief = ImageBrief(queries=QUERIES, ai_prompt="x")
    photos = {q: [_photo(i * 10 + n, f"alt {n}") for n in range(5)] for i, q in enumerate(QUERIES)}

    with respx.mock(assert_all_called=False) as mock:
        _mock_pexels(mock, photos)
        downloaded = _mock_downloads(mock)
        result = images.get_images(brief, count=3, headline=HEADLINE)

    assert len(downloaded) == 3
    assert len(result) == 3


def test_the_same_photo_surfacing_twice_is_not_reused(no_selection_call):
    """Two queries often return the same popular photo; a carousel must not repeat it."""
    brief = ImageBrief(queries=QUERIES[:2], ai_prompt="x")
    shared = _photo(99, "the same popular photo")

    with respx.mock(assert_all_called=False) as mock:
        _mock_pexels(mock, {
            QUERIES[0]: [shared, _photo(1, "a")],
            QUERIES[1]: [shared, _photo(2, "b")],
        })
        _mock_downloads(mock)
        result = images.get_images(brief, count=3, headline=HEADLINE)

    # Three slides, three different photos -- the shared one is used at most once.
    assert len(result) == 3
    assert len({r.attribution for r in result}) == 3


def test_fallback_takes_one_candidate_per_query_when_selection_fails(monkeypatch):
    """A failed selection call must not fall back to three photos from one query."""
    candidates = [
        Candidate(1, "a", "u1", "P1", QUERIES[0]),
        Candidate(2, "b", "u2", "P2", QUERIES[0]),
        Candidate(3, "c", "u3", "P3", QUERIES[1]),
        Candidate(4, "d", "u4", "P4", QUERIES[2]),
    ]

    def boom(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(images, "OpenAI", boom)

    chosen = images._select_candidates(HEADLINE, candidates, 3)

    assert [c.query for c in chosen] == QUERIES  # one from each, not three from the first


def test_selection_ignores_out_of_range_and_duplicate_indexes(monkeypatch):
    candidates = [Candidate(i, f"alt {i}", f"u{i}", f"P{i}", QUERIES[0]) for i in range(4)]

    class _Resp:
        output_text = '{"choices": [2, 2, 99, -1, 0]}'

    monkeypatch.setattr(
        images, "OpenAI",
        lambda **kw: type("C", (), {"responses": type("R", (), {"create": lambda *a, **k: _Resp()})()})(),
    )

    chosen = images._select_candidates(HEADLINE, candidates, 3)

    assert [c.photo_id for c in chosen] == [2, 0]


def test_widens_search_when_portrait_results_are_thin():
    """Portrait stock is a small pool; a thin result set retries without the filter."""
    orientations: list[str | None] = []

    def handler(request):
        params = dict(httpx.URL(str(request.url)).params)
        orientations.append(params.get("orientation"))
        if params.get("orientation") == "portrait":
            return httpx.Response(200, json={"photos": [_photo(1, "only one")]})
        return httpx.Response(200, json={"photos": [_photo(i, f"alt {i}") for i in range(5)]})

    with respx.mock(assert_all_called=False) as mock:
        mock.get(images.PEXELS_SEARCH_URL).mock(side_effect=handler)
        found = images._search_pexels("speaker cone macro")

    assert orientations == ["portrait", None]
    assert len(found) == 5


def test_raises_when_nothing_can_be_sourced(monkeypatch):
    brief = ImageBrief(queries=["nothing matches"], ai_prompt="x")
    monkeypatch.setattr(images, "_generate_ai_image", lambda p: (_ for _ in ()).throw(RuntimeError()))

    with respx.mock(assert_all_called=False) as mock:
        mock.get(images.PEXELS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json={"photos": []})
        )
        with pytest.raises(RuntimeError, match="Could not source any image"):
            images.get_images(brief, count=3, headline=HEADLINE)
