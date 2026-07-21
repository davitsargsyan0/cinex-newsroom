"""Compose a photo into a branded Cinex carousel slide.

Every slide is 1080x1350 (Instagram portrait) and carries the same furniture:
the Cinex wordmark top-left, a bottom gradient scrim so text stays legible over
any photo, headline text, and a slide counter. Slide 1 gets the full headline;
later slides get a short kicker so the carousel reads as one set without
repeating the whole title three times.

Fonts and the logo are vendored under `assets/` on purpose -- the GitHub Actions
runner has no usable system fonts, so `ImageFont.load_default()` (a tiny bitmap
face) would be the silent fallback.
"""

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

ASSETS_DIR = Path(__file__).parent / "assets"
FONT_PATH = ASSETS_DIR / "fonts" / "Inter.ttf"
LOGO_PATH = ASSETS_DIR / "cinex_logo.png"

CANVAS = (1080, 1350)
MARGIN = 64

# Inter is a variable font; these are (optical size, weight) axis settings.
WEIGHT_BOLD = 700.0
WEIGHT_REGULAR = 400.0

HEADLINE_SIZE = 62
KICKER_SIZE = 40
COUNTER_SIZE = 32

LOGO_WIDTH = 260
LOGO_OPACITY = 230  # out of 255

SCRIM_HEIGHT = 700  # px of gradient rising from the bottom edge
SCRIM_MAX_ALPHA = 242
SCRIM_CURVE = 1.6   # <2 darkens earlier, so the top text line still has contrast

BRAND_ACCENT = (233, 74, 90)  # Cinex red, used for the rule above the headline


@lru_cache(maxsize=8)
def _font(size: int, weight: float) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONT_PATH), size)
    # Optical size tracks the point size so large text keeps tight spacing.
    font.set_variation_by_axes([min(max(size, 14.0), 32.0), weight])
    return font


@lru_cache(maxsize=1)
def _white_logo() -> Image.Image:
    """The wordmark as solid white on real transparency.

    The source art is a dark grey wordmark on an *opaque white* background -- its
    alpha channel is nearly all 255, so keying on alpha would paste a white box.
    Instead we key on luminance: dark ink becomes opaque, white ground becomes
    transparent, and the anti-aliased edges survive as partial alpha.
    """
    logo = Image.open(LOGO_PATH).convert("RGBA")

    # Ignore any fully transparent padding rows in the source.
    source_alpha = logo.split()[-1]
    ink = ImageChops.multiply(
        ImageOps.invert(logo.convert("L")),
        source_alpha,
    )

    # The source wordmark is a grey gradient, so a straight inversion leaves the
    # lighter half semi-transparent and it disappears over bright photos. Push
    # anything meaningfully inked to fully opaque, keeping only the outermost
    # edge pixels partial so the mark stays anti-aliased.
    ink = ink.point(lambda v: min(255, int(v * 3.2)))

    white = Image.new("RGBA", logo.size, (255, 255, 255, 0))
    white.putalpha(ink)
    white = white.crop(ink.getbbox() or (0, 0, *logo.size))

    ratio = LOGO_WIDTH / white.width
    return white.resize((LOGO_WIDTH, max(1, round(white.height * ratio))), Image.LANCZOS)


def _paste_with_shadow(canvas: Image.Image, overlay: Image.Image, xy: tuple[int, int]) -> None:
    """Composite `overlay` over a soft dark halo so it survives on light photos."""
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    silhouette = Image.new("RGBA", overlay.size, (0, 0, 0, 190))
    silhouette.putalpha(ImageChops.multiply(overlay.split()[-1], Image.new("L", overlay.size, 190)))
    shadow.paste(silhouette, xy, silhouette)
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)))
    canvas.alpha_composite(overlay, xy)


def _draw_text(draw: ImageDraw.ImageDraw, xy, text, font, fill) -> None:
    """Text with a tight drop shadow, so it reads against any scrim brightness."""
    x, y = xy
    draw.text((x + 2, y + 3), text, font=font, fill=(0, 0, 0, 150))
    draw.text((x, y), text, font=font, fill=fill)


def _fit_cover(img: Image.Image) -> Image.Image:
    """Center-crop to the canvas aspect ratio, then resize to fill it exactly."""
    target_ratio = CANVAS[0] / CANVAS[1]
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

    return img.resize(CANVAS, Image.LANCZOS)


def _scrim() -> Image.Image:
    """A black overlay that ramps from transparent to near-opaque at the bottom."""
    gradient = Image.new("L", (1, SCRIM_HEIGHT))
    for y in range(SCRIM_HEIGHT):
        gradient.putpixel((0, y), int(SCRIM_MAX_ALPHA * (y / SCRIM_HEIGHT) ** SCRIM_CURVE))
    alpha = gradient.resize((CANVAS[0], SCRIM_HEIGHT), Image.BILINEAR)

    scrim = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    black = Image.new("RGBA", (CANVAS[0], SCRIM_HEIGHT), (0, 0, 0, 255))
    black.putalpha(alpha)
    scrim.paste(black, (0, CANVAS[1] - SCRIM_HEIGHT), black)
    return scrim


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int) -> list[str]:
    """Greedy word wrap, truncating with an ellipsis once `max_lines` is full."""
    words = text.split()
    lines: list[str] = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        if font.getlength(candidate) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break

    if len(lines) < max_lines and current:
        lines.append(current)

    if len(lines) == max_lines and (len(" ".join(lines).split()) < len(words)):
        last = lines[-1]
        while last and font.getlength(last + "...") > max_width:
            last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
        lines[-1] = last + "..."

    return lines


def _headline_for_slide(headline: str, slide_index: int) -> tuple[str, int, float]:
    """Return (text, font size, weight) for this slide's text block."""
    if slide_index == 0:
        return headline, HEADLINE_SIZE, WEIGHT_BOLD
    # Later slides carry a condensed kicker so the set reads as one story.
    return headline, KICKER_SIZE, WEIGHT_REGULAR


def apply_template(
    jpeg_bytes: bytes,
    headline: str,
    slide_index: int,
    total: int,
) -> bytes:
    """Render one branded slide and return it as JPEG bytes.

    `slide_index` is 0-based; slide 0 is the cover and gets the full headline.
    """
    photo = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    canvas = _fit_cover(photo).convert("RGBA")

    canvas.alpha_composite(_scrim())
    draw = ImageDraw.Draw(canvas)

    text, size, weight = _headline_for_slide(headline, slide_index)
    font = _font(size, weight)
    max_lines = 4 if slide_index == 0 else 2
    lines = _wrap(text, font, CANVAS[0] - 2 * MARGIN, max_lines)

    line_height = int(size * 1.3)
    block_height = line_height * len(lines)
    baseline = CANVAS[1] - MARGIN - COUNTER_SIZE - 48
    y = baseline - block_height

    # Accent rule sits directly above the text block.
    draw.rectangle(
        [MARGIN, y - 34, MARGIN + 96, y - 28],
        fill=BRAND_ACCENT,
    )

    for line in lines:
        _draw_text(draw, (MARGIN, y), line, font, (255, 255, 255, 255))
        y += line_height

    counter_font = _font(COUNTER_SIZE, WEIGHT_BOLD)
    counter = f"{slide_index + 1}/{total}"
    _draw_text(
        draw,
        (CANVAS[0] - MARGIN - draw.textlength(counter, font=counter_font),
         CANVAS[1] - MARGIN - COUNTER_SIZE),
        counter,
        counter_font,
        (255, 255, 255, 210),
    )

    logo = _white_logo().copy()
    logo.putalpha(logo.split()[-1].point(lambda a: int(a * LOGO_OPACITY / 255)))
    _paste_with_shadow(canvas, logo, (MARGIN, MARGIN))

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()
