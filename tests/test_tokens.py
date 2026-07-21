import datetime

import httpx
import pytest
import respx
import typer

from newsroom import tokens
from newsroom.tokens import TokenRefreshError

BASE = tokens.GRAPH_API_BASE
OLD = "EAAT-old-token"
NEW = "EAAT-new-token"


@pytest.fixture(autouse=True)
def stub_settings(monkeypatch):
    monkeypatch.setattr(tokens.settings, "ig_access_token", OLD)
    monkeypatch.setattr(tokens.settings, "fb_app_id", "app-id")
    monkeypatch.setattr(tokens.settings, "fb_app_secret", "app-secret")


def _in_days(days: float) -> int:
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
    return int(when.timestamp())


def _debug(mock, *, valid=True, expires_at=None, token=None):
    """Mock debug_token, optionally only for a specific input_token."""
    payload = {"data": {"is_valid": valid, "expires_at": expires_at}}
    route = mock.get(f"{BASE}/debug_token")
    if token is None:
        return route.mock(return_value=httpx.Response(200, json=payload))

    def handler(request):
        if dict(httpx.URL(str(request.url)).params).get("input_token") == token:
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"data": {"is_valid": True, "expires_at": _in_days(1)}})

    return route.mock(side_effect=handler)


def test_reports_days_until_expiry():
    with respx.mock(assert_all_called=False) as mock:
        _debug(mock, expires_at=_in_days(55))

        assert 54 < tokens.days_until_expiry() < 56


def test_zero_expiry_means_the_token_never_expires():
    with respx.mock(assert_all_called=False) as mock:
        _debug(mock, expires_at=0)

        assert tokens.token_expires_at() is None
        assert tokens.days_until_expiry() is None


def test_invalid_token_raises():
    with respx.mock(assert_all_called=False) as mock:
        _debug(mock, valid=False)

        with pytest.raises(TokenRefreshError, match="not valid"):
            tokens.days_until_expiry()


def test_refresh_returns_the_new_token():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/oauth/access_token").mock(
            return_value=httpx.Response(200, json={"access_token": NEW})
        )
        _debug(mock, expires_at=_in_days(60))

        assert tokens.refresh_token() == NEW


def test_refresh_sends_the_app_credentials():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{BASE}/oauth/access_token").mock(
            return_value=httpx.Response(200, json={"access_token": NEW})
        )
        _debug(mock, expires_at=_in_days(60))

        tokens.refresh_token()

    params = dict(httpx.URL(str(route.calls[0].request.url)).params)
    assert params["grant_type"] == "fb_exchange_token"
    assert params["client_id"] == "app-id"
    assert params["client_secret"] == "app-secret"
    assert params["fb_exchange_token"] == OLD


def test_refresh_requires_app_credentials(monkeypatch):
    monkeypatch.setattr(tokens.settings, "fb_app_secret", None)

    with pytest.raises(TokenRefreshError, match="FB_APP_ID and FB_APP_SECRET"):
        tokens.refresh_token()


def test_a_token_that_fails_validation_is_never_returned():
    """The whole point: a broken token must not overwrite a working one."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/oauth/access_token").mock(
            return_value=httpx.Response(200, json={"access_token": NEW})
        )
        _debug(mock, valid=False)

        with pytest.raises(TokenRefreshError, match="failed validation"):
            tokens.refresh_token()


def test_a_token_expiring_immediately_is_rejected():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/oauth/access_token").mock(
            return_value=httpx.Response(200, json={"access_token": NEW})
        )
        _debug(mock, expires_at=_in_days(0.25))

        with pytest.raises(TokenRefreshError, match="expires in"):
            tokens.refresh_token()


def test_http_failure_does_not_leak_the_response_body():
    """Meta echoes tokens back in some error bodies; logs here are public."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/oauth/access_token").mock(
            return_value=httpx.Response(400, json={"error": {"message": f"bad token {OLD}"}})
        )

        with pytest.raises(TokenRefreshError) as exc:
            tokens.refresh_token()

    assert OLD not in str(exc.value)
    assert "HTTP 400" in str(exc.value)


def test_missing_access_token_in_response_raises():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/oauth/access_token").mock(
            return_value=httpx.Response(200, json={"token_type": "bearer"})
        )

        with pytest.raises(TokenRefreshError, match="no access_token"):
            tokens.refresh_token()


def test_expiry_warning_fires_below_the_threshold(monkeypatch, capsys):
    """The Telegram nag is the only thing standing between a lapsed token and
    silently broken publishing, so the threshold must actually trigger."""
    from newsroom import cli

    sent = []
    monkeypatch.setattr(cli.tokens, "days_until_expiry", lambda *a, **k: 9.0)
    monkeypatch.setattr(
        cli.tokens, "token_expires_at",
        lambda *a, **k: datetime.datetime(2026, 9, 14, tzinfo=datetime.timezone.utc),
    )
    monkeypatch.setattr(cli.bot, "send_alert", lambda text: sent.append(text))
    monkeypatch.setattr(cli.asyncio, "run", lambda coro: coro)

    with pytest.raises(typer.Exit) as exc:
        cli.token_status(notify=True, warn_days=14)

    assert exc.value.exit_code == 1
    assert len(sent) == 1
    assert "expires in 9 days" in sent[0]


def test_no_warning_when_the_token_has_plenty_of_life(monkeypatch):
    from newsroom import cli

    sent = []
    monkeypatch.setattr(cli.tokens, "days_until_expiry", lambda *a, **k: 55.0)
    monkeypatch.setattr(
        cli.tokens, "token_expires_at",
        lambda *a, **k: datetime.datetime(2026, 9, 14, tzinfo=datetime.timezone.utc),
    )
    monkeypatch.setattr(cli.bot, "send_alert", lambda text: sent.append(text))
    monkeypatch.setattr(cli.asyncio, "run", lambda coro: coro)

    cli.token_status(notify=True, warn_days=14)  # must not raise

    assert sent == []


def test_non_expiring_token_never_warns(monkeypatch):
    from newsroom import cli

    sent = []
    monkeypatch.setattr(cli.tokens, "days_until_expiry", lambda *a, **k: None)
    monkeypatch.setattr(cli.bot, "send_alert", lambda text: sent.append(text))
    monkeypatch.setattr(cli.asyncio, "run", lambda coro: coro)

    cli.token_status(notify=True, warn_days=14)

    assert sent == []
