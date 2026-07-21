"""Central configuration, loaded from environment variables / .env file."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM providers
    openai_api_key: str

    # News sources
    newsapi_key: str | None = None

    # Image hosting
    cloudinary_cloud_name: str
    cloudinary_api_key: str
    cloudinary_api_secret: str

    # Stock photos
    pexels_api_key: str

    # Instagram / Meta Graph API
    ig_user_id: str
    ig_access_token: str

    # Meta app credentials, needed only to refresh ig_access_token before its
    # 60-day expiry. Optional so the pipeline still runs without them.
    fb_app_id: str | None = None
    fb_app_secret: str | None = None

    # Telegram
    telegram_bot_token: str
    telegram_authorized_chat_id: int

    # Behavior
    top_stories_per_run: int = 3       # overall cap for one run
    tech_stories_per_run: int = 2      # filled first
    general_stories_per_run: int = 1   # fills whatever the tech quota left over
    images_per_post: int = 3           # carousel slides
    newsroom_db_path: str = "newsroom.db"


# Import this singleton elsewhere: `from newsroom.config import settings`
settings = Settings()
