import sqlite3

import pytest

from newsroom import db

STORY_KEY = "https://example.com/chipmaker-story"
SLIDES = [f"https://cdn.example.com/slide_{i}.jpg" for i in range(3)]


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "newsroom_db_path", str(tmp_path / "test.db"))


def _draft(**overrides) -> dict:
    draft = {
        "caption": "English caption",
        "caption_hy": "Հայերեն",
        "hashtags": ["tech", "AI"],
        "alt_text": "alt",
        "image_urls": SLIDES,
        "image_attributions": ["Photo by Ada on Pexels"],
        "image_brief": {"keywords": ["chips"], "ai_prompt": "symbolic chips"},
        "image_source": "stock",
        "sources": ["Reuters"],
    }
    draft.update(overrides)
    return draft


def test_round_trips_a_multi_slide_draft():
    db.save_draft(STORY_KEY, "Title", _draft())

    stored = db.get_draft(STORY_KEY)

    assert db.load_json_column(stored["image_urls"]) == SLIDES
    assert stored["image_url"] == SLIDES[0]  # slide 1 kept for previews
    assert stored["caption_hy"] == "Հայերեն"
    assert db.load_json_column(stored["image_brief"])["keywords"] == ["chips"]


def test_resaving_a_loaded_draft_does_not_double_encode():
    """bot.py edits re-save the row it just read; list columns must stay decodable."""
    db.save_draft(STORY_KEY, "Title", _draft())
    loaded = db.get_draft(STORY_KEY)

    db.save_draft(STORY_KEY, "Title", {**loaded, "caption": "edited"})
    resaved = db.get_draft(STORY_KEY)

    assert resaved["caption"] == "edited"
    assert db.load_json_column(resaved["image_urls"]) == SLIDES
    assert db.load_json_column(resaved["hashtags"]) == ["tech", "AI"]


def test_publishing_marks_the_story_as_posted():
    db.save_draft(STORY_KEY, "Title", _draft())

    db.update_status(STORY_KEY, "PUBLISHED", ig_media_id="ig-123")

    assert db.is_already_posted(STORY_KEY) is True
    assert db.get_draft(STORY_KEY)["ig_media_id"] == "ig-123"


def test_rejected_story_can_be_reconsidered_later():
    db.save_draft(STORY_KEY, "Title", _draft())

    db.update_status(STORY_KEY, "REJECTED")

    assert db.is_already_posted(STORY_KEY) is False


def test_migration_adds_columns_to_a_pre_existing_database(tmp_path, monkeypatch):
    """Older newsroom.db files lack the carousel columns and must be upgraded in place."""
    legacy_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy_path)
    conn.executescript(
        """
        CREATE TABLE newsroom_pending (
            story_key TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
            caption TEXT, hashtags TEXT, alt_text TEXT, image_url TEXT,
            image_source TEXT, sources TEXT, ig_media_id TEXT, error TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO newsroom_pending VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (STORY_KEY, "Old title", "PENDING_APPROVAL", "old caption", "[]", None,
         "https://cdn.example.com/old.jpg", "stock", "[]", None, None, "t0", "t0"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db.settings, "newsroom_db_path", str(legacy_path))

    stored = db.get_draft(STORY_KEY)  # opening the connection runs the migration

    assert stored["caption"] == "old caption"
    assert stored["image_urls"] is None
    assert stored["caption_hy"] is None


def test_creates_the_state_directory_if_missing(tmp_path, monkeypatch):
    """The Actions workflow points NEWSROOM_DB_PATH at state/, which won't exist yet."""
    nested = tmp_path / "state" / "newsroom.db"
    monkeypatch.setattr(db.settings, "newsroom_db_path", str(nested))

    db.save_draft(STORY_KEY, "Title", _draft())

    assert nested.exists()
