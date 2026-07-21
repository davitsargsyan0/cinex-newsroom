"""Upload generated or stock images to Cloudinary."""

import hashlib

import cloudinary
import cloudinary.uploader

from newsroom.config import settings


cloudinary.config(
    cloud_name=settings.cloudinary_cloud_name,
    api_key=settings.cloudinary_api_key,
    api_secret=settings.cloudinary_api_secret,
    secure=True,
)


def _safe_public_id(value: str) -> str:
    """Convert a long story identifier or URL into a short Cloudinary-safe ID."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def upload_image(jpeg_bytes: bytes, public_id: str) -> str:
    """Upload JPEG bytes and return the public HTTPS URL."""
    safe_id = _safe_public_id(public_id)

    result = cloudinary.uploader.upload(
        jpeg_bytes,
        public_id=safe_id,
        folder="newsroom",
        resource_type="image",
        overwrite=True,
    )

    return result["secure_url"]


def upload_images(images: list[bytes], story_key: str) -> list[str]:
    """Upload every slide of one story, returning URLs in slide order.

    The per-slide suffix matters: the public_id is derived from the story key
    alone, so without it each slide would overwrite the previous one.
    """
    return [
        upload_image(jpeg_bytes, public_id=f"{story_key}#{index}")
        for index, jpeg_bytes in enumerate(images)
    ]