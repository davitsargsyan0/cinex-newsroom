"""Instagram Graph API publishing: mandatory container flow.

Single image:
1. POST /{ig-user-id}/media          -> creation_id
2. GET  /{creation-id}?fields=status_code  -> poll until FINISHED
3. POST /{ig-user-id}/media_publish  -> ig_media_id

Carousel (2-10 images) adds a child step in front:
1. POST /{ig-user-id}/media?is_carousel_item=true   -> one child id per image
2. POST /{ig-user-id}/media?media_type=CAROUSEL&children=... -> creation_id
3. poll, then publish exactly as above

Requires a Business (not Creator) IG account linked to a Facebook Page, and a
Meta developer app with instagram_business_basic + instagram_business_content_publish.
Because we only post to our own account, no App Review is needed -- the app can
stay in Development Mode with the poster added as an app admin.
"""

import time

import httpx

from newsroom.config import settings

GRAPH_API_BASE = "https://graph.facebook.com/v19.0"
POLL_INTERVAL_SECONDS = 3
POLL_TIMEOUT_SECONDS = 60
MAX_CAROUSEL_ITEMS = 10


class InstagramPublishError(RuntimeError):
    pass


def _post_media(params: dict) -> str:
    resp = httpx.post(
        f"{GRAPH_API_BASE}/{settings.ig_user_id}/media",
        params={**params, "access_token": settings.ig_access_token},
        timeout=15,
    )
    if resp.status_code != 200:
        raise InstagramPublishError(f"media container creation failed: {resp.text}")
    return resp.json()["id"]


def _create_media_container(image_url: str, caption: str) -> str:
    return _post_media({"image_url": image_url, "caption": caption})


def _create_carousel_container(image_urls: list[str], caption: str) -> str:
    """Create one child container per image, then the parent carousel container."""
    child_ids = [
        _post_media({"image_url": url, "is_carousel_item": "true"})
        for url in image_urls
    ]
    # Children must finish processing before the parent will accept them.
    for child_id in child_ids:
        _poll_until_finished(child_id)

    return _post_media(
        {
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "caption": caption,
        }
    )


def _poll_until_finished(creation_id: str) -> None:
    elapsed = 0
    while elapsed < POLL_TIMEOUT_SECONDS:
        resp = httpx.get(
            f"{GRAPH_API_BASE}/{creation_id}",
            params={"fields": "status_code", "access_token": settings.ig_access_token},
            timeout=10,
        )
        resp.raise_for_status()
        status = resp.json().get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise InstagramPublishError(f"container {creation_id} failed processing")
        time.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS
    raise InstagramPublishError(f"container {creation_id} did not finish within timeout")


def _publish_container(creation_id: str) -> str:
    resp = httpx.post(
        f"{GRAPH_API_BASE}/{settings.ig_user_id}/media_publish",
        params={"creation_id": creation_id, "access_token": settings.ig_access_token},
        timeout=15,
    )
    if resp.status_code != 200:
        raise InstagramPublishError(f"media publish failed: {resp.text}")
    return resp.json()["id"]  # this is the ig_media_id


def publish(image_urls: str | list[str], caption: str) -> str:
    """Publish one image or a carousel, returning the resulting ig_media_id."""
    urls = [image_urls] if isinstance(image_urls, str) else list(image_urls)

    if not urls:
        raise InstagramPublishError("no images to publish")
    if len(urls) > MAX_CAROUSEL_ITEMS:
        raise InstagramPublishError(
            f"carousel supports at most {MAX_CAROUSEL_ITEMS} images, got {len(urls)}"
        )

    if len(urls) == 1:
        creation_id = _create_media_container(urls[0], caption)
    else:
        creation_id = _create_carousel_container(urls, caption)

    _poll_until_finished(creation_id)
    return _publish_container(creation_id)
