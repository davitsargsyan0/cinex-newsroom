#test
"""Turn a news story into a caption, hashtags, and an image brief using OpenAI.

Input is only the story's title, summary, and source. The program does not scrape
the full article. The model returns JSON, which is validated with Pydantic.
"""

import json

from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator

from newsroom.config import settings
from newsroom.news import Story


SYSTEM_PROMPT = """\
You are a social media editor for a neutral news account. Given a news story's
title and summary, produce ONE JSON object and nothing else. Do not use markdown
fences or add a preamble.

Hard rules:
- Paraphrase only. Do not copy sentences or headlines verbatim from the input.
- Use neutral, factual framing.
- Do not invent facts or quotes.
- caption: a short hook line, followed by 2-3 short paraphrased paragraphs.
- caption_hy: the same reporting written in Eastern Armenian, using Armenian
  script. Write it as a native Armenian editor would - a natural rewrite, not a
  word-for-word translation and never a transliteration of English into Armenian
  letters. Keep it about as long as `caption`. Leave widely used product, company
  and person names in their original Latin spelling.
- hashtags: 8-15 relevant hashtags without the '#' character, in English. One
  shared list serves both captions.
- alt_text: a plain-language description of a fitting image.
- image_brief:
    - keywords: stock-photo search keywords that are conceptual and do not name
      a specific real person, event, or place.
    - ai_prompt: a conceptual, editorial, or symbolic image-generation prompt.
      Never request a photorealistic reconstruction of a real event or a real
      identifiable public figure.
- sources: a list of outlet names to credit.
- If the story should not be posted, return:
  {"skip": true, "reason": "..."}
  and omit the other fields.

Return strict JSON only.
"""


def _coerce_str_list(value):
    """Accept a comma-separated string where a list was asked for.

    The model returns `"a, b, c"` instead of `["a", "b", "c"]` often enough that
    retrying just burns a second call and fails the same way.
    """
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class ImageBrief(BaseModel):
    keywords: list[str]
    ai_prompt: str

    _split_keywords = field_validator("keywords", mode="before")(_coerce_str_list)


class GeneratedDraft(BaseModel):
    caption: str | None = None
    caption_hy: str | None = None
    hashtags: list[str] = Field(default_factory=list)
    alt_text: str | None = None
    image_brief: ImageBrief | None = None
    sources: list[str] = Field(default_factory=list)
    skip: bool = False
    reason: str | None = None

    _split_hashtags = field_validator("hashtags", "sources", mode="before")(_coerce_str_list)


def _call_openai(user_content: str) -> str:
    client = OpenAI(api_key=settings.openai_api_key)

    # gpt-4.1 rather than -mini: mini's Eastern Armenian is noticeably weaker, and at
    # a few stories a day the cost difference is immaterial.
    response = client.responses.create(
        model="gpt-4.1",
        instructions=SYSTEM_PROMPT,
        input=user_content,
        max_output_tokens=1800,  # room for both language blocks
    )

    return response.output_text


def _build_user_prompt(story: Story) -> str:
    return (
        f"Title: {story.title}\n"
        f"Summary: {story.summary}\n"
        f"Source: {story.source}\n"
    )


def parse_draft_json(text: str) -> GeneratedDraft:
    """Clean and validate the model's JSON response."""
    cleaned = (
        text.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )

    data = json.loads(cleaned)
    return GeneratedDraft.model_validate(data)


def generate_draft(story: Story) -> GeneratedDraft | None:
    """Return a validated draft, or None when the model skips the story."""
    prompt = _build_user_prompt(story)
    raw = _call_openai(prompt)

    try:
        draft = parse_draft_json(raw)
    except (json.JSONDecodeError, ValidationError):
        retry_prompt = (
            prompt
            + "\n\nYour previous response was invalid. "
            + "Return only one valid JSON object matching the required structure."
        )

        raw_retry = _call_openai(retry_prompt)
        draft = parse_draft_json(raw_retry)

    if draft.skip:
        return None

    return draft
