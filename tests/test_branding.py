import io

import pytest
from PIL import Image

from newsroom import branding

HEADLINE = "Chipmaker unveils a processor built for on-device inference workloads"


def _photo(size=(1600, 2400), color=(40, 60, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.mark.parametrize("source_size", [(1600, 2400), (2400, 1600), (1000, 1000)])
def test_output_is_always_instagram_portrait(source_size):
    """Landscape and square sources must be cropped to the same canvas, not letterboxed."""
    out = branding.apply_template(_photo(size=source_size), HEADLINE, 0, 3)

    img = Image.open(io.BytesIO(out))
    assert img.size == branding.CANVAS
    assert img.format == "JPEG"


def test_cover_and_later_slides_differ():
    cover = branding.apply_template(_photo(), HEADLINE, 0, 3)
    second = branding.apply_template(_photo(), HEADLINE, 1, 3)

    # Same photo, different furniture (headline weight/size and the slide counter).
    assert cover != second


def test_long_headline_is_truncated_not_overflowed():
    very_long = " ".join(["escalating"] * 80)
    font = branding._font(branding.HEADLINE_SIZE, branding.WEIGHT_BOLD)

    lines = branding._wrap(very_long, font, branding.CANVAS[0] - 2 * branding.MARGIN, 4)

    assert len(lines) == 4
    assert lines[-1].endswith("...")
    for line in lines:
        assert font.getlength(line) <= branding.CANVAS[0] - 2 * branding.MARGIN


def test_short_headline_is_not_ellipsised():
    font = branding._font(branding.HEADLINE_SIZE, branding.WEIGHT_BOLD)

    lines = branding._wrap("Short headline", font, branding.CANVAS[0] - 2 * branding.MARGIN, 4)

    assert lines == ["Short headline"]


def test_logo_is_keyed_to_transparency():
    """The source art has an opaque white background; it must not paste as a box."""
    logo = branding._white_logo()
    alpha = logo.split()[-1]

    assert logo.mode == "RGBA"
    # A wordmark is mostly negative space, so most pixels must be transparent.
    transparent = sum(alpha.histogram()[:16])
    assert transparent > 0.4 * (logo.width * logo.height)


def test_high_resolution_source_is_downsampled_not_upscaled():
    """Images now arrive at 2x the canvas; branding must be the only resample."""
    out = branding.apply_template(_photo(size=(2160, 2700)), HEADLINE, 0, 3)

    assert Image.open(io.BytesIO(out)).size == branding.CANVAS


def test_source_already_at_target_ratio_is_not_cropped():
    source = Image.new("RGB", (2160, 2700), (40, 60, 90))

    fitted = branding._fit_cover(source)

    assert fitted.size == branding.CANVAS


def test_branding_survives_a_greyscale_source():
    buf = io.BytesIO()
    Image.new("L", (1600, 2400), 128).save(buf, format="JPEG")

    out = branding.apply_template(buf.getvalue(), HEADLINE, 0, 3)

    assert Image.open(io.BytesIO(out)).size == branding.CANVAS
