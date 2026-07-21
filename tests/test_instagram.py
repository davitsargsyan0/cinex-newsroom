import httpx
import pytest
import respx

from newsroom import instagram
from newsroom.instagram import InstagramPublishError

IG_USER = "test-ig-user"
BASE = instagram.GRAPH_API_BASE
CAPTION = "Caption text"
URLS = [f"https://cdn.example.com/slide_{i}.jpg" for i in range(3)]


@pytest.fixture(autouse=True)
def stub_settings(monkeypatch):
    monkeypatch.setattr(instagram.settings, "ig_user_id", IG_USER)
    monkeypatch.setattr(instagram.settings, "ig_access_token", "token")
    # Keep the container poll from actually sleeping between attempts.
    monkeypatch.setattr(instagram, "POLL_INTERVAL_SECONDS", 0)


def _mock_graph(mock, container_ids):
    """Hand out container ids in order, report every container FINISHED, then publish."""
    ids = iter(container_ids)
    created = []

    def create(request):
        params = dict(httpx.URL(str(request.url)).params)
        created.append(params)
        return httpx.Response(200, json={"id": next(ids)})

    mock.post(f"{BASE}/{IG_USER}/media").mock(side_effect=create)
    mock.get(url__regex=rf"{BASE}/[\w-]+").mock(
        return_value=httpx.Response(200, json={"status_code": "FINISHED"})
    )
    mock.post(f"{BASE}/{IG_USER}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": "published-media-id"})
    )
    return created


def test_carousel_creates_children_then_parent():
    with respx.mock(assert_all_called=False) as mock:
        created = _mock_graph(mock, ["child0", "child1", "child2", "parent"])

        media_id = instagram.publish(URLS, CAPTION)

    assert media_id == "published-media-id"

    children, parent = created[:3], created[3]
    # Every child is flagged as a carousel item and carries no caption of its own.
    for params, url in zip(children, URLS):
        assert params["is_carousel_item"] == "true"
        assert params["image_url"] == url
        assert "caption" not in params

    assert parent["media_type"] == "CAROUSEL"
    assert parent["children"] == "child0,child1,child2"
    assert parent["caption"] == CAPTION


def test_single_image_skips_the_carousel_flow():
    with respx.mock(assert_all_called=False) as mock:
        created = _mock_graph(mock, ["container", "unused"])

        media_id = instagram.publish([URLS[0]], CAPTION)

    assert media_id == "published-media-id"
    assert len(created) == 1
    assert "is_carousel_item" not in created[0]
    assert created[0]["caption"] == CAPTION


def test_a_plain_string_url_still_publishes():
    with respx.mock(assert_all_called=False) as mock:
        created = _mock_graph(mock, ["container"])

        assert instagram.publish(URLS[0], CAPTION) == "published-media-id"

    assert created[0]["image_url"] == URLS[0]


def test_rejects_an_empty_image_list():
    with pytest.raises(InstagramPublishError, match="no images"):
        instagram.publish([], CAPTION)


def test_rejects_more_than_ten_images():
    too_many = [f"https://cdn.example.com/{i}.jpg" for i in range(11)]

    with pytest.raises(InstagramPublishError, match="at most 10"):
        instagram.publish(too_many, CAPTION)


def test_container_error_status_raises():
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{BASE}/{IG_USER}/media").mock(
            return_value=httpx.Response(200, json={"id": "container"})
        )
        mock.get(url__regex=rf"{BASE}/[\w-]+").mock(
            return_value=httpx.Response(200, json={"status_code": "ERROR"})
        )

        with pytest.raises(InstagramPublishError, match="failed processing"):
            instagram.publish([URLS[0]], CAPTION)
