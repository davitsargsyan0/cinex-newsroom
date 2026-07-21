import json

from newsroom.bot import IG_CAPTION_LIMIT, SEPARATOR, _build_caption_message, _telegram_preview

ENGLISH = "A chipmaker unveiled a processor.\n\nIt targets on-device inference."
ARMENIAN = "Չիպարտադրողը ներկայացրել է նոր պրոցեսոր։\n\nԱյն նախատեսված է սարքի վրա հաշվարկների համար։"


def _draft(**overrides) -> dict:
    draft = {
        "caption": ENGLISH,
        "caption_hy": ARMENIAN,
        "hashtags": json.dumps(["tech", "AI"]),
        "sources": json.dumps(["Reuters"]),
        "image_attributions": json.dumps(["Photo by Ada on Pexels"]),
        "image_source": "stock",
        "title": "Chipmaker unveils a processor",
    }
    draft.update(overrides)
    return draft


def test_caption_carries_both_languages_and_credits():
    caption = _build_caption_message(_draft())

    assert ENGLISH in caption
    assert ARMENIAN in caption
    assert SEPARATOR in caption
    assert "#tech #AI" in caption
    assert "Photo by Ada on Pexels" in caption  # Pexels requires the credit
    assert "Sources: Reuters" in caption
    assert caption.index(ENGLISH) < caption.index(ARMENIAN)


def test_ai_images_are_disclosed():
    assert "AI-generated image" in _build_caption_message(_draft(image_source="ai"))


def test_stock_images_are_not_disclosed_as_ai():
    assert "AI-generated" not in _build_caption_message(_draft(image_source="stock"))


def test_missing_armenian_falls_back_to_english_only():
    caption = _build_caption_message(_draft(caption_hy=None))

    assert ENGLISH in caption
    assert SEPARATOR not in caption


def test_duplicate_photo_credits_are_collapsed():
    credit = "Photo by Ada on Pexels"
    caption = _build_caption_message(
        _draft(image_attributions=json.dumps([credit, credit, credit]))
    )

    assert caption.count(credit) == 1


def test_overlong_caption_is_trimmed_to_the_instagram_limit():
    caption = _build_caption_message(
        _draft(caption="E" * 1200, caption_hy="Հ" * 1600)
    )

    assert len(caption) <= IG_CAPTION_LIMIT
    # The English block and the credits block survive; Armenian absorbs the cut.
    assert "E" * 1200 in caption
    assert "Sources: Reuters" in caption


def test_english_alone_is_never_truncated_by_the_armenian_budget():
    """If English plus credits already fill the limit, Armenian is dropped entirely."""
    caption = _build_caption_message(_draft(caption="E" * 2100))

    assert "E" * 2100 in caption
    assert SEPARATOR not in caption


def test_telegram_preview_respects_the_shorter_limit():
    preview = _telegram_preview(_draft(caption="E" * 1200, caption_hy="Հ" * 900))

    assert len(preview) <= 1024


def test_handles_decoded_lists_as_well_as_json_strings():
    """save_draft round-trips leave these as JSON strings; fresh drafts pass lists."""
    caption = _build_caption_message(
        _draft(hashtags=["tech", "AI"], sources=["Reuters"], image_attributions=[])
    )

    assert "#tech #AI" in caption
    assert "Sources: Reuters" in caption
