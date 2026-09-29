"""A failing host degrades or reports; it never silently drops readings."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from io import BytesIO
from ssl import SSLError
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

import pytest

from custom_components.sp_group import client as client_module
from custom_components.sp_group.client import (
    AuthError,
    HttpResponse,
    Session,
    SpGroupClient,
    TransportError,
    UrllibTransport,
    UsageError,
    _loggable_url,
)
from custom_components.sp_group.const import (
    AUTH0_REFRESH_GRANT,
    EVA_LATEST_SESSION_PATH,
    IDENTITY_HOST,
    JARVIS_ME_PATH,
    JARVIS_SMRD_PATH,
    NJORD_HISTORY_PATH,
    OAUTH_TOKEN_PATH,
    PRICEPLAN_PATH,
    PUBLIC_HOST,
    TYCHE_WALLET_PATH,
)
from custom_components.sp_group.models import UsageReadings

from .conftest import FixedClock, FixtureTransport, fixture_client

# Paths whose host may be down without costing the caller its billed readings.
OPTIONAL_PATHS = [
    EVA_LATEST_SESSION_PATH,
    TYCHE_WALLET_PATH,
    NJORD_HISTORY_PATH,
    PRICEPLAN_PATH,
]


class FailingPathTransport(FixtureTransport):
    """Fixture transport where one path raises the way urlopen would."""

    def __init__(self, path: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.failing_path = path

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        *,
        timeout: int | None = None,
    ) -> HttpResponse:
        if urlparse(url).path == self.failing_path:
            raise TransportError(f"{method} {url} failed: {URLError('unreachable')}")
        return super().request(method, url, headers, body, timeout=timeout)


@pytest.mark.parametrize("path", OPTIONAL_PATHS)
def test_optional_host_failure_keeps_billed_readings(path: str) -> None:
    usage = fixture_client(FailingPathTransport(path)).fetch_usage()
    assert usage.electricity is not None
    assert usage.electricity_kwh > 0


def test_optional_host_failure_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        fixture_client(FailingPathTransport(TYCHE_WALLET_PATH)).fetch_usage()
    assert TYCHE_WALLET_PATH in caplog.text


def test_required_host_failure_reports_as_usage_error() -> None:
    client = fixture_client(FailingPathTransport(JARVIS_ME_PATH))
    with pytest.raises(UsageError) as raised:
        client.fetch_usage()
    assert JARVIS_ME_PATH in str(raised.value)


def test_tariff_host_failure_leaves_tariff_unset() -> None:
    usage = fixture_client(FailingPathTransport(PRICEPLAN_PATH)).fetch_usage()
    assert usage.tariff is None


def test_identity_host_outage_is_not_reported_as_bad_credentials() -> None:
    """A 5xx from Auth0 must not push Home Assistant into a reauth flow."""
    client = SpGroupClient(
        clock=FixedClock(),
        transport=FixtureTransport(
            responses={OAUTH_TOKEN_PATH: HttpResponse(503, b"<html>gateway</html>")}
        )
    )
    with pytest.raises(TransportError) as raised:
        client.login("user@example.com", "secret")
    assert "503" in str(raised.value)


def test_rate_limited_login_is_not_reported_as_bad_credentials() -> None:
    client = SpGroupClient(
        clock=FixedClock(),
        transport=FixtureTransport(
            responses={OAUTH_TOKEN_PATH: HttpResponse(429, b'{"error":"too_many"}')}
        )
    )
    with pytest.raises(TransportError):
        client.login("user@example.com", "secret")


def test_rejected_credentials_still_raise_auth_error() -> None:
    client = SpGroupClient(
        transport=FixtureTransport(fail_login=True), clock=FixedClock()
    )
    with pytest.raises(AuthError):
        client.login("user@example.com", "wrong")


def test_non_json_body_on_required_read_reports_the_path() -> None:
    client = fixture_client(
        FixtureTransport(
            responses={JARVIS_ME_PATH: HttpResponse(200, b"<html>maintenance</html>")}
        )
    )
    with pytest.raises(UsageError) as raised:
        client.fetch_usage()
    assert "utility account" in str(raised.value)


def test_refresh_failure_keeps_the_reason() -> None:
    """The Auth0 verdict must survive into the error the user is shown."""
    client = SpGroupClient(
        clock=FixedClock(),
        transport=FixtureTransport(
            responses={
                OAUTH_TOKEN_PATH: HttpResponse(
                    403,
                    b'{"error":"invalid_grant",'
                    b'"error_description":"refresh token revoked"}',
                )
            }
        ),
        session=Session(
            access_token="stale",
            id_token="stale",
            refresh_token="revoked",
            expires_at=0,
        ),
    )
    with pytest.raises(AuthError) as raised:
        client.ensure_session()
    assert raised.value.error == "invalid_grant"
    assert "refresh token revoked" in raised.value.error_description
    assert isinstance(raised.value.__cause__, AuthError)


def test_transport_error_is_a_usage_error() -> None:
    """The coordinator and config flow route on UsageError; keep that true."""
    assert issubclass(TransportError, UsageError)


@pytest.mark.parametrize(
    "raised_by_urlopen",
    [
        URLError("name resolution failed"),
        TimeoutError("timed out"),
        SSLError("bad tls"),
    ],
)
def test_urllib_transport_names_the_call_it_could_not_make(
    monkeypatch: pytest.MonkeyPatch, raised_by_urlopen: Exception
) -> None:
    def _fail(*args: object, **kwargs: object) -> object:
        raise raised_by_urlopen

    monkeypatch.setattr(client_module, "urlopen", _fail)
    with pytest.raises(TransportError) as raised:
        UrllibTransport().request(
            "GET", f"{PUBLIC_HOST}{PRICEPLAN_PATH}?consumption=350", {}, None, timeout=8
        )
    message = str(raised.value)
    assert f"GET {PUBLIC_HOST}{PRICEPLAN_PATH}" in message
    assert "timeout 8s" in message
    # The query string carries the account-shaped parameters; keep it out of logs.
    assert "consumption=350" not in message


def test_urllib_transport_keeps_http_error_bodies() -> None:
    """A 4xx is a response, not a transport failure: the body must survive."""
    url = f"{IDENTITY_HOST}{OAUTH_TOKEN_PATH}"
    error = HTTPError(url, 403, "Forbidden", {}, BytesIO(b'{"error":"invalid_grant"}'))

    def _raise(*args: object, **kwargs: object) -> object:
        raise error

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(client_module, "urlopen", _raise)
        response = UrllibTransport().request("POST", url, {}, b"{}")
    assert response.status == 403
    assert b"invalid_grant" in response.body


class RotatingRefreshTransport(FixtureTransport):
    """Auth0 refresh-token rotation: a spent refresh token is rejected.

    Every exchange returns a distinct refresh token, and the second exchange of
    the same one is refused, which is what a real rotation does. A refresh
    sleeps briefly so concurrent callers overlap the window where the stored
    session still reads as expired.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._live_refresh = "starting-refresh-token"
        self.refresh_grants = 0

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        *,
        timeout: int | None = None,
    ) -> HttpResponse:
        if urlparse(url).path != OAUTH_TOKEN_PATH:
            return super().request(method, url, headers, body, timeout=timeout)
        request_body = json.loads(body.decode("utf-8")) if body else {}
        if request_body.get("grant_type") != AUTH0_REFRESH_GRANT:
            return super().request(method, url, headers, body, timeout=timeout)
        with self._lock:
            spent = self._live_refresh
            if spent != request_body.get("refresh_token"):
                return HttpResponse(
                    status=403,
                    body=b'{"error":"invalid_grant",'
                    b'"error_description":"refresh token revoked"}',
                )
            self.refresh_grants += 1
            self._live_refresh = f"rotated-refresh-token-{self.refresh_grants}"
            payload = {
                "access_token": f"access-token-{self.refresh_grants}",
                "id_token": f"id-token-{self.refresh_grants}",
                "refresh_token": self._live_refresh,
                "scope": "openid offline_access",
            }
        time.sleep(0.05)
        return HttpResponse(
            status=200, body=json.dumps(payload).encode("utf-8")
        )


def test_concurrent_fetches_spend_the_refresh_token_once() -> None:
    """Two polls racing an expired session must not rotate the token twice.

    The second exchange of an already-spent refresh token is rejected by Auth0,
    which would surface as a reauth prompt for a session that is still good, and
    its rejected retry would leave a dead refresh token stored for the next poll.
    """
    transport = RotatingRefreshTransport()
    client = SpGroupClient(
        transport=transport,
        session=Session(
            access_token="expired-access-token",
            id_token="expired-id-token",
            refresh_token="starting-refresh-token",
            scope=None,
            expires_at=0,
        ),
    )
    readers = 8
    start = threading.Barrier(readers)
    fetched: list[UsageReadings] = []
    failures: list[Exception] = []

    def _poll() -> None:
        start.wait()
        try:
            fetched.append(client.fetch_usage())
        except Exception as exc:  # reported below, not swallowed
            failures.append(exc)

    threads = [threading.Thread(target=_poll) for _ in range(readers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a poll thread hung"

    assert failures == []
    assert len(fetched) == readers
    assert transport.refresh_grants == 1
    assert client.session is not None
    assert client.session.refresh_token == "rotated-refresh-token-1"


def test_transport_error_keeps_account_identifiers_out_of_the_message() -> None:
    """The premise id and account number are appended to the route, not fixed."""

    def _raise(*args: object, **kwargs: object) -> object:
        raise URLError("unreachable")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(client_module, "urlopen", _raise)
        with pytest.raises(TransportError) as raised:
            UrllibTransport().request(
                "GET",
                f"{PUBLIC_HOST}{JARVIS_SMRD_PATH}/2001590888?ns=1234567890",
                {},
                None,
            )
    message = str(raised.value)
    assert f"{JARVIS_SMRD_PATH}/{client_module.IDENTIFIER_SEGMENT}" in message
    assert "2001590888" not in message
    assert "1234567890" not in message


def test_loggable_url_keeps_the_fixed_route() -> None:
    assert (
        _loggable_url(f"{PUBLIC_HOST}{PRICEPLAN_PATH}?consumption=350")
        == f"{PUBLIC_HOST}{PRICEPLAN_PATH}"
    )
    assert _loggable_url(f"{IDENTITY_HOST}{OAUTH_TOKEN_PATH}") == (
        f"{IDENTITY_HOST}{OAUTH_TOKEN_PATH}"
    )
    assert _loggable_url(f"{PUBLIC_HOST}/") == PUBLIC_HOST
