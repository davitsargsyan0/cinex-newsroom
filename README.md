# newsroom

Fetch tech-heavy news → GPT writes a bilingual (English + Armenian) caption,
hashtags, and an image brief → gather 3 stock/AI images and stamp them with Cinex
branding → send to Telegram for human approval → publish as an Instagram carousel.
SQLite tracks what's been posted so the same story never goes out twice.

## Pipeline

```
fetch (news.py) -> generate (generate.py) -> slides (slides.py)
     images.py (source) -> branding.py (brand) -> host.py (upload)
   -> Telegram approval (bot.py) -> publish (instagram.py) -> record (db.py)
```

Status lifecycle: `DRAFT -> PENDING_APPROVAL -> PUBLISHED / REJECTED / FAILED`

## What each run produces

- **Tech-heavy mix.** Each run fills a tech quota first (Google News Technology,
  TechCrunch, The Verge, Ars Technica, NewsAPI `category=technology`), then tops up
  from general top stories. Tune with `TECH_STORIES_PER_RUN` /
  `GENERAL_STORIES_PER_RUN`; unused tech slots roll into general so a run is never
  short. Dedup runs across the whole pool, so a story carried by both a tech outlet
  and the general feed only goes out once.
- **Bilingual captions.** One model call returns `caption` and `caption_hy`
  (Eastern Armenian). The Instagram caption is English, a separator rule, then
  Armenian, then hashtags, the AI-image disclosure, photo credits, and sources. If
  the total exceeds Instagram's 2200-character cap the Armenian block is trimmed
  first; if it still won't fit, it is dropped rather than truncating English.
- **3 branded slides.** Each slide is 1080×1350 with a bottom gradient scrim, the
  headline, a slide counter, and the Cinex wordmark. Published as a carousel.

## Setup

1. `uv venv && source .venv/bin/activate` (or your preferred venv tool)
2. `uv pip install -e ".[dev]"`
3. `cp .env.example .env` and fill in every key:
   - OpenAI API key
   - Cloudinary credentials (free tier is fine)
   - Pexels API key (free)
   - Instagram Business account ID + long-lived access token (see below)
   - Telegram bot token + your own chat ID (message `@userinfobot` to find it)
4. `newsroom run --dry-run` to sanity-check fetch + generation without touching
   Telegram, Cloudinary, or Instagram.
5. `newsroom run` for the real pipeline.

## Instagram / Meta setup (one-time)

1. Convert your Instagram account to a **Business** account (not Creator — Creator
   accounts can't publish via the API) and link it to a Facebook Page.
2. Create a Meta developer app, add the `instagram_business_basic` and
   `instagram_business_content_publish` permissions.
3. Since you're only posting to your own account, **no App Review is needed** —
   keep the app in Development Mode and add yourself as an app admin/tester.
4. Get a long-lived access token and your `IG_USER_ID`:
   - `GET /me/accounts` -> your Page ID
   - `GET /{page-id}?fields=instagram_business_account` -> your IG_USER_ID
5. Put both into `.env`.

## Commands

```
newsroom run                     # full pipeline for this run
newsroom run --dry-run           # fetch + generate only, no posting
newsroom run --dry-run --save-slides ./slides_preview   # ...and write the slides to disk
newsroom run --no-generate       # just list candidate stories
newsroom pending                 # resend any drafts stuck awaiting approval
newsroom listen                  # process approval buttons until Ctrl+C
newsroom listen --timeout 2700   # ...or for a bounded window, then exit (used by CI)
```

## Deployment (GitHub Actions, $0)

`.github/workflows/daily.yml` runs the pipeline on a daily cron. Every credential
comes from **repository secrets** — `.env` is never deployed, and pydantic-settings
prefers real environment variables over the file.

Add these as Actions secrets: `OPENAI_API_KEY`, `NEWSAPI_KEY`,
`CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`,
`PEXELS_API_KEY`, `IG_USER_ID`, `IG_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_AUTHORIZED_CHAT_ID`.

**State** lives at `state/newsroom.db` (via `NEWSROOM_DB_PATH`) and is committed
back by the workflow, so posted-story history survives between runs. To carry over
history from a local database on first deploy:

```
cp newsroom.db state/newsroom.db
```

**The approval tradeoff.** The Telegram buttons only work while a job is alive, so
the daily job stays online for a 45-minute approval window after posting. Anything
you don't approve in that window stays `PENDING_APPROVAL` — the next daily run does
not lose it, and you can open a fresh window any time by running the
**"Open an approval window"** workflow (`approve.yml`) from the Actions tab, which
re-sends every pending draft and listens for 30 minutes.

Both workflows share a `concurrency` group so two runs never write the state DB at
once.

## Design notes

- **No article scraping.** The LLM only sees title + summary, both to avoid
  building a scraping subsystem and to reduce copyright risk. Captions must be a
  full paraphrase/rewrite — no verbatim sentences or headlines.
- **Stock-first images.** Pexels is tried first — the full keyword phrase, then
  progressively narrower queries — and distinct photo IDs are enforced so a
  carousel never repeats the same shot. AI generation (`gpt-image-*`) is only a
  fallback and is **capped at one image per post**, since it is the only meaningful
  per-post cost. AI images must stay conceptual/symbolic — never a fabricated photo
  of a real event, person, or place — and any post containing one carries an
  "AI-generated image" note.
- **Photo credits are published.** Pexels requires attribution, so each
  photographer credit is stored per slide and rendered into the caption.
- **Vendored assets.** `newsroom/assets/` holds the Cinex wordmark and Inter (OFL).
  Nothing depends on system fonts, because the CI runner has none — without this,
  PIL would silently fall back to a tiny bitmap face.
- **Human in the loop.** Nothing publishes without an explicit Approve tap in
  Telegram from the authorized chat ID.

## Tests

```
pytest
```

The suite is fully offline: feeds, the Graph API (via `respx`), and the database
are all stubbed, so no test spends API credit or touches a live account.
