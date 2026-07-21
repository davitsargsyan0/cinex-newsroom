"""Source, brand, and host the slides for one story.

Sits between images/branding/host so the pipeline (`cli.py`) and the Telegram
"regenerate image" button (`bot.py`) build carousels the exact same way.
"""

from dataclasses import dataclass

from newsroom import branding, host
from newsroom.generate import ImageBrief
from newsroom.images import get_images


@dataclass
class Slides:
    image_urls: list[str]
    attributions: list[str]
    image_source: str  # "stock" unless any slide had to be AI-generated


def build_slides(
    story_key: str,
    headline: str,
    image_brief: ImageBrief,
    count: int | None = None,
) -> Slides:
    """Fetch images, stamp Cinex branding on each, upload, and return the hosted URLs."""
    sourced = get_images(image_brief, count=count)

    branded = [
        branding.apply_template(
            image.jpeg_bytes,
            headline=headline,
            slide_index=index,
            total=len(sourced),
        )
        for index, image in enumerate(sourced)
    ]

    return Slides(
        image_urls=host.upload_images(branded, story_key),
        attributions=[img.attribution for img in sourced if img.attribution],
        # If any slide is AI-generated the whole post carries the disclosure.
        image_source="ai" if any(img.image_source == "ai" for img in sourced) else "stock",
    )
