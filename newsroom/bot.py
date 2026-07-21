"""Telegram bot: sends the composed draft for human approval before anything publishes.

Buttons: Approve / Edit caption / Regenerate image / Reject.
Only messages from TELEGRAM_AUTHORIZED_CHAT_ID are accepted -- everything else is
rejected, since this bot has the power to trigger a real Instagram post.
"""

import logging
import hashlib

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from newsroom.config import settings
from newsroom import db, instagram, slides
from newsroom.generate import ImageBrief

logger = logging.getLogger(__name__)

IG_CAPTION_LIMIT = 2200
TELEGRAM_CAPTION_LIMIT = 1024
SEPARATOR = "━━━━━━━━━━"

# story_key -> "awaiting_caption_edit" while we wait for the user's next text message
_EDIT_STATE: dict[str, bool] = {}


def _authorized(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id != settings.telegram_authorized_chat_id:
        logger.warning("Rejected message from unauthorized chat_id=%s", chat_id)
        return False
    return True


def _build_caption_message(draft: dict) -> str:
    """Assemble the Instagram caption: English, then Armenian, then the credits block.

    Instagram caps captions at 2200 characters. The Armenian block is trimmed first
    when we run over, since the English block carries the hook.
    """
    hashtags = " ".join(f"#{h}" for h in db.load_json_column(draft.get("hashtags")))
    sources = ", ".join(db.load_json_column(draft.get("sources")))
    credits = [c for c in db.load_json_column(draft.get("image_attributions")) if c]

    tail_parts = [hashtags]
    if draft.get("image_source") == "ai":
        tail_parts.append("🖼️ AI-generated image")
    if credits:
        # Pexels requires the photographer credit to be shown.
        tail_parts.append(" · ".join(dict.fromkeys(credits)))
    if sources:
        tail_parts.append(f"Sources: {sources}")
    tail = "\n\n".join(part for part in tail_parts if part)

    english = (draft.get("caption") or "").strip()
    armenian = (draft.get("caption_hy") or "").strip()

    if not armenian:
        return f"{english}\n\n{tail}"

    budget = IG_CAPTION_LIMIT - len(english) - len(tail) - len(SEPARATOR) - 6
    if len(armenian) > budget:
        armenian = armenian[: max(0, budget - 1)].rstrip() + "…" if budget > 40 else ""

    if not armenian:
        return f"{english}\n\n{tail}"

    return f"{english}\n\n{SEPARATOR}\n\n{armenian}\n\n{tail}"


def _telegram_preview(draft: dict) -> str:
    """Same caption, clipped to Telegram's media-group caption limit."""
    caption = _build_caption_message(draft)
    if len(caption) <= TELEGRAM_CAPTION_LIMIT:
        return caption
    return caption[: TELEGRAM_CAPTION_LIMIT - 1].rstrip() + "…"

def _short_story_key(story_key: str) -> str:
    return hashlib.sha256(story_key.encode()).hexdigest()[:16]


def _build_keyboard(story_key: str) -> InlineKeyboardMarkup:
    short_key = _short_story_key(story_key)

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=f"approve:{short_key}",
                ),
                InlineKeyboardButton(
                    "📝 Edit caption",
                    callback_data=f"edit:{short_key}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔄 Regenerate image",
                    callback_data=f"regen:{short_key}",
                ),
                InlineKeyboardButton(
                    "❌ Reject",
                    callback_data=f"reject:{short_key}",
                ),
            ],
        ]
    )


async def send_for_approval(app: Application, story_key: str) -> None:
    """Post the carousel preview, then the control message that carries the buttons.

    Telegram will not attach an inline keyboard to a media group, so the album and
    the buttons have to be two separate messages.
    """
    draft = db.get_draft(story_key)
    if not draft:
        logger.error("No draft found for story_key=%s", story_key)
        return

    image_urls = db.load_json_column(draft.get("image_urls")) or [draft["image_url"]]
    caption_text = _telegram_preview(draft)

    media = [
        InputMediaPhoto(media=url, caption=caption_text if index == 0 else None)
        for index, url in enumerate(image_urls)
    ]
    await app.bot.send_media_group(
        chat_id=settings.telegram_authorized_chat_id,
        media=media,
    )
    await app.bot.send_message(
        chat_id=settings.telegram_authorized_chat_id,
        text=f"⬆️ {draft['title']}\n\n{len(image_urls)} slide(s) — publish?",
        reply_markup=_build_keyboard(story_key),
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _authorized(update):
        await query.answer("Not authorized.")
        return

    action, short_key = query.data.split(":", 1)

    story_key = db.resolve_story_key(short_key)

    if not story_key:
        await query.answer("Draft not found or expired.", show_alert=True)
        return

    await query.answer()

    if action == "approve":
        draft = db.get_draft(story_key)
        image_urls = db.load_json_column(draft.get("image_urls")) or [draft["image_url"]]
        try:
            ig_media_id = instagram.publish(image_urls, _build_caption_message(draft))
            db.update_status(story_key, "PUBLISHED", ig_media_id=ig_media_id)
            await query.edit_message_text(text=f"✅ Published (ig_media_id={ig_media_id})")
        except Exception as exc:  # noqa: BLE001
            db.update_status(story_key, "FAILED", error=str(exc))
            await query.edit_message_text(text=f"⚠️ Publish failed: {exc}")

    elif action == "reject":
        db.update_status(story_key, "REJECTED")
        await query.edit_message_text(text="❌ Rejected")

    elif action == "edit":
        _EDIT_STATE[story_key] = True
        draft = db.get_draft(story_key)
        # No parse_mode: story keys are URLs and titles are arbitrary text, either
        # of which can contain characters that break Telegram's Markdown parser.
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Send the new English caption for:\n{draft['title']}",
        )

    elif action == "regen":
        draft = db.get_draft(story_key)
        # Reuse the brief the model wrote for this story; older drafts predate the
        # stored column, so fall back to the title.
        stored_brief = db.load_json_column(draft.get("image_brief"), default={})
        image_brief = (
            ImageBrief.model_validate(stored_brief)
            if stored_brief
            else ImageBrief(keywords=[draft["title"]], ai_prompt=draft["title"])
        )

        new_slides = slides.build_slides(story_key, draft["title"], image_brief)
        db.save_draft(
            story_key,
            draft["title"],
            {
                **draft,
                "image_urls": new_slides.image_urls,
                "image_attributions": new_slides.attributions,
                "image_source": new_slides.image_source,
            },
        )
        await send_for_approval(context.application, story_key)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    pending_edits = [k for k, v in _EDIT_STATE.items() if v]
    if not pending_edits:
        return

    story_key = pending_edits[-1]
    _EDIT_STATE[story_key] = False

    draft = db.get_draft(story_key)
    db.save_draft(story_key, draft["title"], {**draft, "caption": update.message.text})
    await update.message.reply_text("Caption updated.")
    await send_for_approval(context.application, story_key)


def build_app() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app
