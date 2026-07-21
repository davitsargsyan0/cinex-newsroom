"""CLI entrypoint.

    newsroom run                 # full pipeline: fetch -> generate -> host -> send for approval
    newsroom run --dry-run       # fetch + generate only, print results, don't host/send/publish
    newsroom run --no-generate   # fetch only, print candidate stories
    newsroom pending             # resurface any drafts still awaiting approval in Telegram
"""

import asyncio
import logging
from pathlib import Path

import typer

from newsroom import bot, branding, db, slides, tokens
from newsroom.generate import generate_draft
from newsroom.images import get_images
from newsroom.news import fetch_top_stories

app = typer.Typer(help="Automated newsroom -> Instagram posting pipeline.")
logging.basicConfig(level=logging.INFO)
# httpx logs every request URL at INFO, and our query strings carry API keys
# (NewsAPI, Pexels). That would write secrets straight into the CI log.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("newsroom.cli")


async def _send_approval_messages(
    telegram_app,
    story_keys: list[str],
) -> None:
    """Send all Telegram approval messages using one event loop."""
    for story_key in story_keys:
        await bot.send_for_approval(telegram_app, story_key)
        typer.echo("  -> sent for Telegram approval.")


def _render_preview(story, draft, out_dir: Path) -> None:
    """Dry-run helper: write branded slides to disk instead of hosting them."""
    out_dir.mkdir(parents=True, exist_ok=True)
    typer.echo(f"  -> image queries: {draft.image_brief.queries}")
    sourced = get_images(draft.image_brief, headline=story.title)

    for index, image in enumerate(sourced):
        branded = branding.apply_template(
            image.jpeg_bytes,
            headline=story.title,
            slide_index=index,
            total=len(sourced),
        )
        slug = "".join(c for c in story.title if c.isalnum() or c == " ").strip()
        path = out_dir / f"{slug[:40].replace(' ', '_')}_{index}.jpg"
        path.write_bytes(branded)
        typer.echo(f"  -> wrote {path}")


@app.command()
def run(
    dry_run: bool = typer.Option(
        False,
        help="Fetch + generate only; skip hosting/sending/publishing.",
    ),
    no_generate: bool = typer.Option(
        False,
        help="Fetch only; print candidate stories and exit.",
    ),
    save_slides: Path = typer.Option(
        None,
        help="With --dry-run, render the branded slides to this directory to eyeball them.",
    ),
):
    """Run one pass of the pipeline: fetch top unposted stories, draft each, queue for approval."""
    stories = fetch_top_stories()

    if not stories:
        typer.echo("No new unposted stories found.")
        raise typer.Exit()

    if no_generate:
        for story in stories:
            typer.echo(
                f"- {story.title}  ({story.source})  {story.url}"
            )
        raise typer.Exit()

    telegram_app = None if dry_run else bot.build_app()
    approval_story_keys: list[str] = []

    for story in stories:
        typer.echo(f"\nProcessing: {story.title}")

        try:
            draft = generate_draft(story)
        except Exception as exc:  # noqa: BLE001 - one bad story must not sink the run
            logger.warning("Generation failed for %r: %s", story.title, exc)
            typer.echo(f"  -> generation failed, skipping: {exc}")
            continue

        if draft is None:
            typer.echo("  -> model chose to skip this story.")
            continue

        if draft.image_brief is None:
            typer.echo("  -> model returned no image brief, skipping.")
            continue

        if dry_run:
            typer.echo(f"  -> caption (en):\n{draft.caption}\n")
            typer.echo(f"  -> caption (hy):\n{draft.caption_hy}\n")
            typer.echo(f"  -> hashtags: {draft.hashtags}")
            if save_slides:
                _render_preview(story, draft, save_slides)
            continue

        try:
            built = slides.build_slides(story.story_key, story.title, draft.image_brief)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slide build failed for %r: %s", story.title, exc)
            typer.echo(f"  -> could not build slides, skipping: {exc}")
            continue
        typer.echo(f"  -> {len(built.image_urls)} slide(s), source: {built.image_source}")

        db.save_draft(
            story.story_key,
            story.title,
            {
                "caption": draft.caption,
                "caption_hy": draft.caption_hy,
                "hashtags": draft.hashtags,
                "alt_text": draft.alt_text,
                "image_urls": built.image_urls,
                "image_attributions": built.attributions,
                "image_brief": draft.image_brief.model_dump(),
                "image_source": built.image_source,
                "sources": draft.sources,
            },
        )

        approval_story_keys.append(story.story_key)

    if approval_story_keys:
        asyncio.run(
            _send_approval_messages(
                telegram_app,
                approval_story_keys,
            )
        )


@app.command()
def pending():
    """Resend drafts still waiting for approval."""
    telegram_app = bot.build_app()
    drafts = db.get_pending()

    if not drafts:
        typer.echo("No pending drafts.")
        raise typer.Exit()

    story_keys = [draft["story_key"] for draft in drafts]

    asyncio.run(
        _send_approval_messages(
            telegram_app,
            story_keys,
        )
    )

    typer.echo(
        f"Resent {len(drafts)} pending draft(s) for approval."
    )

async def _poll_for(telegram_app, seconds: int) -> None:
    """Poll for a bounded window, then shut down cleanly.

    `run_polling()` blocks forever, which a CI job can't do, so this drives the
    python-telegram-bot lifecycle by hand instead.
    """
    async with telegram_app:
        await telegram_app.start()
        await telegram_app.updater.start_polling()
        try:
            await asyncio.sleep(seconds)
        finally:
            await telegram_app.updater.stop()
            await telegram_app.stop()


@app.command()
def token_status():
    """Report how long the Instagram access token has left."""
    remaining = tokens.days_until_expiry()

    if remaining is None:
        typer.echo("Token does not expire.")
        return

    typer.echo(f"Token expires in {remaining:.1f} days ({tokens.token_expires_at():%Y-%m-%d}).")
    if remaining < 14:
        typer.echo("WARNING: fewer than 14 days left - refresh it now.", err=True)
        raise typer.Exit(code=1)


@app.command()
def refresh_token(
    quiet: bool = typer.Option(
        False,
        help="Print ONLY the new token to stdout, for piping into a secret store.",
    ),
):
    """Exchange the Instagram token for a fresh one, valid another ~60 days.

    The new token is written to stdout so it can be piped straight into
    `gh secret set` -- it is never logged, because this repository is public.
    """
    new_token = tokens.refresh_token()

    if quiet:
        # Bare token, no newline decoration, nothing else on stdout.
        typer.echo(new_token, nl=False)
        return

    remaining = tokens.days_until_expiry(new_token)
    typer.echo(
        f"Refreshed. New token is valid for {remaining:.0f} days. "
        "Re-run with --quiet to pipe it into your secret store."
    )


@app.command()
def listen(
    timeout: int = typer.Option(
        0,
        help="Seconds to listen before exiting. 0 means run forever (local use).",
    ),
):
    """Keep the Telegram bot running and process approval buttons."""
    telegram_app = bot.build_app()

    if timeout > 0:
        typer.echo(f"Telegram approval bot is listening for {timeout}s.")
        asyncio.run(_poll_for(telegram_app, timeout))
        typer.echo("Approval window closed.")
        return

    typer.echo("Telegram approval bot is listening. Press Ctrl+C to stop.")
    telegram_app.run_polling()

if __name__ == "__main__":
    app()
