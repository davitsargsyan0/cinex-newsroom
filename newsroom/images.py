"""Get the images for one post: prefer Pexels stock, fall back to AI generation.

A post is a carousel, so we need several *distinct* photos. Pexels is asked for a
page of results and we take unique photo ids off it; if that comes up short we
retry with a narrower query before generating anything. AI generation is capped
at one image per post because `gpt-image-1` is the only meaningful per-post cost.

Output is always 1080x1350 JPEG (Instagram portrait), plus an `image_source` tag
("stock" or "ai") so the caption can be flagged when AI-generated (see §3.2 in
the spec) and an `attribution` string, which Pexels requires us to display.
"""

import base64
import io
import logging
from dataclasses import dataclass

import httpx
from PIL import Image
from openai import OpenAI

from newsroom.config import settings
from newsroom.generate import ImageBrief

logger = logging.getLogger(__name__)

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
TARGET_SIZE = (1080, 1350)
MAX_AI_IMAGES_PER_POST = 1


@dataclass
class SourcedImage:
    jpeg_bytes: bytes
    image_source: str  # "stock" | "ai"
    attribution: str | None = None  # required if stock


def _search_pexels(query: str, limit: int, exclude_ids: set[int]) -> list[SourcedImage]:
    """Return up to `limit` distinct portrait photos for `query`."""
    resp = httpx.get(
        PEXELS_SEARCH_URL,
        headers={"Authorization": settings.pexels_api_key},
        params={"query": query, "per_page": 20, "orientation": "portrait"},
        timeout=10,
    )
    resp.raise_for_status()

    found: list[SourcedImage] = []
    for photo in resp.json().get("photos", []):
        if len(found) >= limit:
            break
        photo_id = photo.get("id")
        if photo_id in exclude_ids:
            continue

        try:
            img_resp = httpx.get(photo["src"]["large2x"], timeout=15)
            img_resp.raise_for_status()
        except httpx.HTTPError:
            logger.warning("Pexels photo %s failed to download, skipping", photo_id)
            continue

        exclude_ids.add(photo_id)
        photographer = photo.get("photographer", "Pexels contributor")
        found.append(
            SourcedImage(
                jpeg_bytes=img_resp.content,
                image_source="stock",
                attribution=f"Photo by {photographer} on Pexels",
            )
        )

    return found


def _generate_ai_image(ai_prompt: str) -> SourcedImage:
    client = OpenAI(api_key=settings.openai_api_key)
    # Editorial-realism modifiers keep this compliant with the "conceptual/symbolic,
    # never a fabricated photo of a real event" rule.
    full_prompt = (
        f"{ai_prompt} Editorial illustration style, symbolic/conceptual, "
        "not photorealistic, no real identifiable people, no real logos or brand marks."
    )
    result = client.images.generate(
        model="gpt-image-1",
        prompt=full_prompt,
        size="1024x1536",
    )

    image_bytes = base64.b64decode(result.data[0].b64_json)
    return SourcedImage(jpeg_bytes=image_bytes, image_source="ai", attribution=None)


def _resize_to_target(jpeg_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")

    # center-crop to target aspect ratio, then resize
    target_ratio = TARGET_SIZE[0] / TARGET_SIZE[1]
    w, h = img.size
    current_ratio = w / h

    if current_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))

    img = img.resize(TARGET_SIZE, Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def get_images(image_brief: ImageBrief, count: int | None = None) -> list[SourcedImage]:
    """Stock-first: gather `count` distinct images, topping up with AI only if needed."""
    count = count or settings.images_per_post
    keywords = image_brief.keywords or []
    collected: list[SourcedImage] = []
    seen_ids: set[int] = set()

    # Full keyword query first; then progressively broader queries, since a long
    # very specific phrase often returns only one or two usable photos.
    queries = [" ".join(keywords)] + [k for k in keywords]
    for query in queries:
        if len(collected) >= count or not query.strip():
            break
        try:
            collected += _search_pexels(query, count - len(collected), seen_ids)
        except httpx.HTTPError:
            logger.warning("Pexels query %r failed", query)

    ai_budget = MAX_AI_IMAGES_PER_POST
    while len(collected) < count and ai_budget > 0:
        try:
            collected.append(_generate_ai_image(image_brief.ai_prompt))
        except Exception:  # noqa: BLE001 - never let image generation sink the run
            logger.exception("AI image generation failed")
            break
        ai_budget -= 1

    if not collected:
        raise RuntimeError("Could not source any image for this story")

    # A carousel needs at least 2 slides to be worth it, but if stock was thin and
    # the AI budget is spent we publish what we have rather than dropping the story.
    for image in collected:
        image.jpeg_bytes = _resize_to_target(image.jpeg_bytes)

    return collected
