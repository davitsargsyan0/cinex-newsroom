"""SQLite persistence: which stories we've posted, and pending drafts awaiting approval."""

import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import hashlib

from newsroom.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS posted_stories (
    story_key   TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    posted_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS newsroom_pending (
    story_key       TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL,   -- DRAFT | PENDING_APPROVAL | PUBLISHED | REJECTED | FAILED
    caption         TEXT,
    hashtags        TEXT,            -- JSON list
    alt_text        TEXT,
    image_url       TEXT,
    image_source    TEXT,            -- stock | ai
    sources         TEXT,            -- JSON list of outlet names
    ig_media_id     TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""

# Columns added after the first release. Existing newsroom.db files predate them,
# so they are applied with ALTER TABLE rather than folded into SCHEMA above.
MIGRATIONS = {
    "caption_hy": "TEXT",          # Armenian caption
    "image_urls": "TEXT",          # JSON list, one entry per carousel slide
    "image_attributions": "TEXT",  # JSON list, aligned with image_urls
    "image_brief": "TEXT",         # JSON, so a regenerate reuses the original brief
}


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(newsroom_pending)")}
    for column, coltype in MIGRATIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE newsroom_pending ADD COLUMN {column} {coltype}")


@contextmanager
def get_conn():
    db_path = Path(settings.newsroom_db_path)
    if db_path.parent != Path("."):
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_json(value) -> str:
    """Serialize to JSON, passing through values that are already JSON strings.

    `bot.py` re-saves drafts it read back with `get_draft()`, where list columns
    are still encoded; without this they would be double-encoded on every edit.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value)


def load_json_column(value, default=None):
    """Decode a JSON list/dict column that may be NULL or already decoded."""
    if value is None or value == "":
        return default if default is not None else []
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else []


def is_already_posted(story_key: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM posted_stories WHERE story_key = ?", (story_key,)
        ).fetchone()
        return row is not None


def mark_posted(story_key: str, title: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO posted_stories (story_key, title, posted_at) VALUES (?, ?, ?)",
            (story_key, title, _now()),
        )


def save_draft(story_key: str, title: str, draft: dict) -> None:
    """Insert or update a pending draft with generated content, status = PENDING_APPROVAL."""
    image_urls = draft.get("image_urls") or ([draft["image_url"]] if draft.get("image_url") else [])

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO newsroom_pending
                (story_key, title, status, caption, caption_hy, hashtags, alt_text,
                 image_url, image_urls, image_attributions, image_brief,
                 image_source, sources, created_at, updated_at)
            VALUES (?, ?, 'PENDING_APPROVAL', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(story_key) DO UPDATE SET
                status=excluded.status,
                caption=excluded.caption,
                caption_hy=excluded.caption_hy,
                hashtags=excluded.hashtags,
                alt_text=excluded.alt_text,
                image_url=excluded.image_url,
                image_urls=excluded.image_urls,
                image_attributions=excluded.image_attributions,
                image_brief=excluded.image_brief,
                image_source=excluded.image_source,
                sources=excluded.sources,
                updated_at=excluded.updated_at
            """,
            (
                story_key,
                title,
                draft.get("caption"),
                draft.get("caption_hy"),
                _as_json(draft.get("hashtags", [])),
                draft.get("alt_text"),
                image_urls[0] if image_urls else None,  # slide 1, for previews
                _as_json(image_urls),
                _as_json(draft.get("image_attributions", [])),
                _as_json(draft.get("image_brief")) if draft.get("image_brief") else None,
                draft.get("image_source"),
                _as_json(draft.get("sources", [])),
                _now(),
                _now(),
            ),
        )


def update_status(story_key: str, status: str, ig_media_id: str | None = None, error: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE newsroom_pending
            SET status = ?, ig_media_id = COALESCE(?, ig_media_id), error = ?, updated_at = ?
            WHERE story_key = ?
            """,
            (status, ig_media_id, error, _now(), story_key),
        )
    if status == "PUBLISHED":
        with get_conn() as conn:
            row = conn.execute(
                "SELECT title FROM newsroom_pending WHERE story_key = ?", (story_key,)
            ).fetchone()
        if row:
            mark_posted(story_key, row["title"])


def get_pending() -> list[dict]:
    """All drafts still sitting in PENDING_APPROVAL, e.g. to resurface in Telegram after a restart."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM newsroom_pending WHERE status = 'PENDING_APPROVAL'"
        ).fetchall()
        return [dict(r) for r in rows]


def get_draft(story_key: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM newsroom_pending WHERE story_key = ?", (story_key,)
        ).fetchone()
        return dict(row) if row else None

def resolve_story_key(short_key: str) -> str | None:
    """Find the original full story key using its short SHA-256 hash."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT story_key FROM newsroom_pending"
        ).fetchall()

    for row in rows:
        full_key = row["story_key"]
        candidate = hashlib.sha256(full_key.encode("utf-8")).hexdigest()[:16]

        if candidate == short_key:
            return full_key

    return None