"""Keep the Instagram access token alive.

The account publishes with a long-lived **Facebook user** token, which Meta expires
after 60 days. There is no permanent alternative here: a Page access token would
never expire, but `/me/accounts` is empty for this app, so no Page token exists to
use instead. The token therefore has to be exchanged for a fresh one on a schedule.

Exchanging needs the app's own credentials (`FB_APP_ID` / `FB_APP_SECRET`), and the
result has to be written back to wherever the token is stored, because each exchange
issues a *new* token rather than extending the old one.
"""

import datetime
import logging

import httpx

from newsroom.config import settings

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com/v19.0"


class TokenRefreshError(RuntimeError):
    pass


def token_expires_at(token: str | None = None) -> datetime.datetime | None:
    """When the current token dies. None means Meta reports it as non-expiring."""
    token = token or settings.ig_access_token

    resp = httpx.get(
        f"{GRAPH_API_BASE}/debug_token",
        params={"input_token": token, "access_token": token},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})

    if not data.get("is_valid"):
        raise TokenRefreshError(f"token is not valid: {data.get('error', {}).get('message')}")

    expires_at = data.get("expires_at")
    if not expires_at:  # 0 or missing means it never expires
        return None
    return datetime.datetime.fromtimestamp(expires_at, datetime.timezone.utc)


def days_until_expiry(token: str | None = None) -> float | None:
    expires_at = token_expires_at(token)
    if expires_at is None:
        return None
    return (expires_at - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 86400


def refresh_token(token: str | None = None) -> str:
    """Exchange the current long-lived token for a new one, valid another ~60 days.

    Returns the new token. The caller is responsible for persisting it -- the old
    token keeps working until its own expiry, so nothing breaks if that fails.
    """
    token = token or settings.ig_access_token

    if not settings.fb_app_id or not settings.fb_app_secret:
        raise TokenRefreshError(
            "FB_APP_ID and FB_APP_SECRET must be set to refresh the access token"
        )

    resp = httpx.get(
        f"{GRAPH_API_BASE}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.fb_app_id,
            "client_secret": settings.fb_app_secret,
            "fb_exchange_token": token,
        },
        timeout=15,
    )

    if resp.status_code != 200:
        # Never surface the response body: it can echo the token back.
        raise TokenRefreshError(f"token exchange failed with HTTP {resp.status_code}")

    new_token = resp.json().get("access_token")
    if not new_token:
        raise TokenRefreshError("token exchange returned no access_token")

    # Verify before returning, so a caller never persists a token it hasn't proven
    # works. Checking after storage is not an option in CI: `secrets.*` resolves at
    # job start, so a later step would re-read the *old* value and pass regardless.
    try:
        remaining = days_until_expiry(new_token)
    except Exception as exc:  # noqa: BLE001
        raise TokenRefreshError(f"refreshed token failed validation: {exc}") from exc

    if remaining is not None and remaining < 1:
        raise TokenRefreshError(f"refreshed token expires in {remaining:.2f} days")

    logger.info("Refreshed Instagram token; valid for %.0f more days", remaining or 0)
    return new_token
