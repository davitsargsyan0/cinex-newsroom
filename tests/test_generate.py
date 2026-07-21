import json

import pytest

from newsroom.generate import parse_draft_json


def _valid_payload() -> dict:
    return {
        "caption": "A short hook.\n\nParaphrased summary paragraph one.",
        "caption_hy": "Կարճ ներածություն։\n\nՎերաշարադրված ամփոփում։",
        "hashtags": ["news", "worldnews"],
        "alt_text": "Symbolic illustration representing global trade.",
        "image_brief": {
            "queries": ["shipping container stack", "dock worker hands", "harbour at dusk"],
            "ai_prompt": "A symbolic, conceptual illustration of global trade routes.",
        },
        "sources": ["Reuters"],
    }


def test_parses_clean_json():
    draft = parse_draft_json(json.dumps(_valid_payload()))
    assert draft.skip is False
    assert draft.hashtags == ["news", "worldnews"]


def test_strips_markdown_fences():
    fenced = "```json\n" + json.dumps(_valid_payload()) + "\n```"
    draft = parse_draft_json(fenced)
    assert draft.caption.startswith("A short hook")


def test_skip_payload():
    payload = {"skip": True, "reason": "too sensitive"}
    draft = parse_draft_json(json.dumps(payload))
    assert draft.skip is True


def test_invalid_json_raises():
    with pytest.raises(json.JSONDecodeError):
        parse_draft_json("not json at all")


def test_comma_separated_queries_are_coerced_to_a_list():
    """Observed in the wild: gpt-4.1 returns the list as one comma-joined string."""
    payload = _valid_payload()
    payload["image_brief"]["queries"] = "vinyl record macro, recording studio desk"

    draft = parse_draft_json(json.dumps(payload))

    assert draft.image_brief.queries == ["vinyl record macro", "recording studio desk"]


def test_legacy_keywords_brief_still_validates():
    """Drafts saved before the per-slide rewrite are still in the database, and the
    Telegram regenerate button revalidates whatever brief it finds there."""
    payload = _valid_payload()
    payload["image_brief"] = {
        "keywords": ["global trade", "shipping containers"],
        "ai_prompt": "A symbolic illustration.",
    }

    draft = parse_draft_json(json.dumps(payload))

    assert draft.image_brief.queries == ["global trade", "shipping containers"]


def test_queries_win_when_both_fields_are_present():
    payload = _valid_payload()
    payload["image_brief"]["keywords"] = ["stale", "legacy"]

    draft = parse_draft_json(json.dumps(payload))

    assert draft.image_brief.queries[0] == "shipping container stack"


def test_comma_separated_hashtags_and_sources_are_coerced():
    payload = _valid_payload()
    payload["hashtags"] = "news, worldnews"
    payload["sources"] = "Reuters, AP"

    draft = parse_draft_json(json.dumps(payload))

    assert draft.hashtags == ["news", "worldnews"]
    assert draft.sources == ["Reuters", "AP"]


def test_proper_lists_are_left_alone():
    draft = parse_draft_json(json.dumps(_valid_payload()))

    assert draft.image_brief.queries == [
        "shipping container stack",
        "dock worker hands",
        "harbour at dusk",
    ]


def test_parses_armenian_caption():
    draft = parse_draft_json(json.dumps(_valid_payload()))

    assert draft.caption_hy.startswith("Կարճ")


def test_armenian_caption_is_optional():
    """A model response without caption_hy must still validate; the caption falls
    back to English-only rather than failing the whole draft."""
    payload = _valid_payload()
    del payload["caption_hy"]

    draft = parse_draft_json(json.dumps(payload))

    assert draft.caption_hy is None
    assert draft.caption.startswith("A short hook")
