"""Pick the images for one post: search Pexels per slide, then choose deliberately.

The flow is search -> select -> download, and the ordering matters:

- One search **per slide query**, never a bag of all keywords joined together. A
  long multi-concept query makes Pexels loose-match and return generic filler,
  which is how a story about a JBL tablet ended up illustrated with a Sony
  speaker.
- Selection happens on `alt` text via a cheap model call, so an off-topic result
  is rejected rather than published just because it ranked first.
- Only the chosen images are downloaded, at 2x the canvas, so the branding step
  downsamples instead of upscaling a preview.

AI generation stays a capped fallback for when stock genuinely has nothing.
"""

import base64
import json
import logging
from dataclasses import dataclass

import httpx
from openai import OpenAI

from newsroom.config import settings
from newsroom.generate import ImageBrief

logger = logging.getLogger(__name__)

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
CANDIDATES_PER_QUERY = 15
MAX_AI_IMAGES_PER_POST = 1

# Ask Pexels for twice the 1080x1350 canvas and let it crop to our aspect ratio.
# `large2x` is only ~867px wide, i.e. narrower than the canvas, so it would be
# upscaled; `original` is many megapixels and wasteful to fetch whole.
DOWNLOAD_PARAMS = "auto=compress&cs=tinysrgb&fit=crop&w=2160&h=2700"


@dataclass
class SourcedImage:
    jpeg_bytes: bytes
    image_source: str  # "stock" | "ai"
    attribution: str | None = None  # required if stock


@dataclass
class Candidate:
    photo_id: int
    alt: str
    original_url: str
    photographer: str
    query: str  # which slide query surfaced this photo

    @property
    def download_url(self) -> str:
        return f"{self.original_url}?{DOWNLOAD_PARAMS}"

    @property
    def attribution(self) -> str:
        return f"Photo by {self.photographer} on Pexels"


def _search_pexels(query: str, limit: int = CANDIDATES_PER_QUERY) -> list[Candidate]:
    """Return candidate metadata for one query. Downloads nothing."""

    def _request(params: dict) -> list[dict]:
        resp = httpx.get(
            PEXELS_SEARCH_URL,
            headers={"Authorization": settings.pexels_api_key},
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("photos", [])

    base = {"query": query, "per_page": limit}
    photos = _request({**base, "orientation": "portrait"})
    if len(photos) < 3:
        # Portrait stock is a much smaller pool; widening beats returning nothing,
        # since we crop to portrait on download anyway.
        photos = _request(base)

    return [
        Candidate(
            photo_id=photo["id"],
            alt=(photo.get("alt") or "").strip(),
            original_url=photo["src"]["original"],
            photographer=photo.get("photographer", "Pexels contributor"),
            query=query,
        )
        for photo in photos
    ]


SELECT_PROMPT = """\
You are picking stock photos to illustrate a news story on a tech Instagram account.

You will get the story headline and a numbered list of candidate photos described
by their alt text. Choose the {count} best, one per carousel slide.

Rules:
- Reject anything that does not plausibly illustrate this specific story.
- Reject anything whose description implies a recognisable branded product, since
  showing a competitor's device next to this story would be misleading.
- Prefer distinctive, concrete images over generic desk/office/gadget filler.
- Prefer variety: the chosen photos should not all show the same thing.
- Return ONLY a JSON object: {{"choices": [<index>, ...]}} with exactly {count}
  distinct indexes, best first. No prose, no markdown fences.
"""


def _select_candidates(headline: str, candidates: list[Candidate], count: int) -> list[Candidate]:
    """Ask a small model which candidates actually fit the story.

    Falls back to one candidate per distinct query, which is still far better than
    ranking order alone because each query is narrow and on-topic.
    """
    listing = "\n".join(
        f"{i}. [{c.query}] {c.alt or '(no description)'}" for i, c in enumerate(candidates)
    )

    try:
        client = OpenAI(api_key=settings.openai_api_key)
        response = client.responses.create(
            model="gpt-4.1-mini",  # ranking alt text is easy; keep it cheap
            instructions=SELECT_PROMPT.format(count=count),
            input=f"Headline: {headline}\n\nCandidates:\n{listing}",
            max_output_tokens=200,
        )
        raw = response.output_text.strip().removeprefix("```json").removeprefix("```")
        chosen_indexes = json.loads(raw.removesuffix("```").strip())["choices"]

        chosen: list[Candidate] = []
        seen_ids: set[int] = set()
        for index in chosen_indexes:
            if not isinstance(index, int) or not 0 <= index < len(candidates):
                continue
            candidate = candidates[index]
            if candidate.photo_id in seen_ids:
                continue
            seen_ids.add(candidate.photo_id)
            chosen.append(candidate)

        if chosen:
            return chosen[:count]
        logger.warning("Photo selection returned no usable indexes; falling back")
    except Exception:  # noqa: BLE001 - selection is an optimisation, never fatal
        logger.exception("Photo selection call failed; falling back to ranking order")

    return _one_per_query(candidates, count)


def _one_per_query(candidates: list[Candidate], count: int) -> list[Candidate]:
    """Top-ranked candidate from each distinct query, then top-ups in rank order."""
    chosen: list[Candidate] = []
    seen_ids: set[int] = set()

    for query in dict.fromkeys(c.query for c in candidates):
        for candidate in candidates:
            if candidate.query == query and candidate.photo_id not in seen_ids:
                seen_ids.add(candidate.photo_id)
                chosen.append(candidate)
                break

    for candidate in candidates:
        if len(chosen) >= count:
            break
        if candidate.photo_id not in seen_ids:
            seen_ids.add(candidate.photo_id)
            chosen.append(candidate)

    return chosen[:count]


def _download(candidate: Candidate) -> SourcedImage | None:
    try:
        resp = httpx.get(candidate.download_url, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.warning("Photo %s failed to download, skipping", candidate.photo_id)
        return None

    return SourcedImage(
        jpeg_bytes=resp.content,
        image_source="stock",
        attribution=candidate.attribution,
    )


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
        quality="medium",  # 'auto' bills at the high tier for no visible gain here
    )

    image_bytes = base64.b64decode(result.data[0].b64_json)
    return SourcedImage(jpeg_bytes=image_bytes, image_source="ai", attribution=None)


def get_images(
    image_brief: ImageBrief,
    count: int | None = None,
    headline: str = "",
) -> list[SourcedImage]:
    """Search per slide query, select the best fits, and download only those.

    Images come back at 2x the canvas and are NOT resized here -- `branding` does a
    single crop-and-downscale, so the pixels are only resampled once.
    """
    count = count or settings.images_per_post

    candidates: list[Candidate] = []
    seen_ids: set[int] = set()
    for query in image_brief.queries:
        if not query.strip():
            continue
        try:
            found = _search_pexels(query)
        except httpx.HTTPError:
            logger.warning("Pexels query %r failed", query)
            continue
        for candidate in found:
            if candidate.photo_id not in seen_ids:
                seen_ids.add(candidate.photo_id)
                candidates.append(candidate)

    collected: list[SourcedImage] = []
    if candidates:
        for candidate in _select_candidates(headline, candidates, count):
            image = _download(candidate)
            if image:
                collected.append(image)

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

    return collected
