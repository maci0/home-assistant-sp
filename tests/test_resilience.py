"""A failing host degrades or reports; it never silently drops readings."""

from __future__ import annotations

import functools
import json
import logging
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from email.message import Message
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
    _login_cooldown,
)
from custom_components.sp_group.const import (
    AUTH0_REFRESH_GRANT,
    ERROR_VALUE_CHARS,
    EVA_LATEST_SESSION_PATH,
    IDENTITY_HOST,
    JARVIS_ME_PATH,
    JARVIS_SMRD_PATH,
    LOGIN_RETRY_COOLDOWN_SECONDS,
    MAX_RESPONSE_BYTES,
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


def test_rejected_optional_read_is_logged_not_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 401 on an optional read drops the sensor and must say why."""
    transport = FixtureTransport(
        responses={
            TYCHE_WALLET_PATH: HttpResponse(
                401, b'{"error":"invalid_token","error_description":"token expired"}'
            )
        }
    )
    with caplog.at_level("WARNING"):
        usage = fixture_client(transport).fetch_usage()

    assert usage.ev_wallet is None
    assert "401" in caplog.text
    assert "token expired" in caplog.text
    assert TYCHE_WALLET_PATH in caplog.text


def test_absent_optional_read_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A 404 is the endpoint saying the read has nothing, so it is not noise."""
    transport = FixtureTransport(responses={TYCHE_WALLET_PATH: HttpResponse(404, b"")})
    with caplog.at_level("WARNING"):
        usage = fixture_client(transport).fetch_usage()

    assert usage.ev_wallet is None
    assert caplog.text == ""


def test_identity_host_outage_is_not_reported_as_bad_credentials() -> None:
    """A 5xx from Auth0 must not push Home Assistant into a reauth flow."""
    client = SpGroupClient(
        clock=FixedClock(),
        transport=FixtureTransport(
            responses={OAUTH_TOKEN_PATH: HttpResponse(503, b"<html>gateway</html>")}
        ),
    )
    with pytest.raises(TransportError) as raised:
        client.login("user@example.com", "secret")
    assert "503" in str(raised.value)


def test_rate_limited_login_is_not_reported_as_bad_credentials() -> None:
    client = SpGroupClient(
        clock=FixedClock(),
        transport=FixtureTransport(
            responses={OAUTH_TOKEN_PATH: HttpResponse(429, b'{"error":"too_many"}')}
        ),
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
    error = HTTPError(
        url, 403, "Forbidden", Message(), BytesIO(b'{"error":"invalid_grant"}')
    )

    def _raise(*args: object, **kwargs: object) -> object:
        raise error

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(client_module, "urlopen", _raise)
        response = UrllibTransport().request("POST", url, {}, b"{}")
    assert response.status == 403
    assert b"invalid_grant" in response.body


class _FakeResponse(BytesIO):
    """The urlopen result the transport reads, as a context manager."""

    status = 200

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_urllib_transport_records_the_call_at_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The one line that says how long a dependency took and what it answered."""
    monkeypatch.setattr(
        client_module, "urlopen", lambda *a, **k: _FakeResponse(b'{"account":"12345"}')
    )
    with caplog.at_level("DEBUG", logger="custom_components.sp_group.client"):
        response = UrllibTransport().request(
            "GET", f"{PUBLIC_HOST}{PRICEPLAN_PATH}?consumption=350", {}, None, timeout=8
        )
    assert response.status == 200
    assert PRICEPLAN_PATH in caplog.text
    assert "HTTP 200" in caplog.text
    # The query string and the body stay out: neither helps an operator and
    # both carry account data.
    assert "consumption=350" not in caplog.text
    assert "12345" not in caplog.text


def test_optional_read_error_status_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 5xx takes the entities it feeds away, so it must be the one log line."""
    transport = FixtureTransport(
        responses={TYCHE_WALLET_PATH: HttpResponse(503, b"{}")}
    )
    with caplog.at_level("WARNING"):
        usage = fixture_client(transport).fetch_usage()
    assert usage.ev_wallet is None
    assert TYCHE_WALLET_PATH in caplog.text
    assert "503" in caplog.text


def test_optional_read_not_enrolled_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 4xx is the steady state for an account without that service."""
    transport = FixtureTransport(
        responses={TYCHE_WALLET_PATH: HttpResponse(404, b"{}")}
    )
    with caplog.at_level("WARNING"):
        fixture_client(transport).fetch_usage()
    assert [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ] == []


def test_optional_read_log_keeps_account_query_parameters_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = FixtureTransport(
        responses={NJORD_HISTORY_PATH: HttpResponse(503, b"{}")}
    )
    with caplog.at_level("WARNING"):
        fixture_client(transport).fetch_usage()
    assert NJORD_HISTORY_PATH in caplog.text
    assert "account_numbers" not in caplog.text


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
        return HttpResponse(status=200, body=json.dumps(payload).encode("utf-8"))


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


def test_urllib_transport_builds_one_tls_context() -> None:
    """Each new context re-reads the CA bundle, once per request otherwise."""
    built: list[ssl.SSLContext] = []
    real = ssl.create_default_context

    def _counting() -> ssl.SSLContext:
        context = real()
        built.append(context)
        return context

    responses = iter([b"{}", b"{}"])

    def _ok(*args: object, **kwargs: object) -> _Response:
        return _Response(next(responses))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(client_module, "urlopen", _ok)
        patch.setattr(client_module, "_ssl_context", _cached(_counting))
        transport = UrllibTransport()
        transport.request("GET", "https://example.invalid/a", {}, None)
        transport.request("GET", "https://example.invalid/b", {}, None)

    assert len(built) == 1, "the second request rebuilt the CA context"


class _Response:
    """The minimal urlopen result: a status and a readable body."""

    def __init__(self, body: bytes) -> None:
        self.status = 200
        self._body = body

    def read(self, amount: int | None = None) -> bytes:
        return self._body if amount is None else self._body[:amount]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _cached(factory: Callable[[], ssl.SSLContext]) -> Callable[[], ssl.SSLContext]:
    return functools.lru_cache(maxsize=None)(factory)


def test_urllib_transport_refuses_an_oversized_body() -> None:
    """An upstream streaming without an end must not grow the HA process."""
    url = f"{IDENTITY_HOST}{OAUTH_TOKEN_PATH}"

    def _fake_urlopen(*args: object, **kwargs: object) -> object:
        return _FakeResponse(b"x" * (MAX_RESPONSE_BYTES + 1))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(client_module, "urlopen", _fake_urlopen)
        with pytest.raises(TransportError) as raised:
            UrllibTransport().request("POST", url, {}, b"{}")
    assert str(MAX_RESPONSE_BYTES) in str(raised.value)


def test_urllib_transport_reads_a_body_at_the_cap() -> None:
    """A large legitimate body must still arrive whole."""
    url = f"{IDENTITY_HOST}{OAUTH_TOKEN_PATH}"
    payload = b"x" * MAX_RESPONSE_BYTES

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            client_module,
            "urlopen",
            lambda *a, **k: _FakeResponse(payload),
        )
        response = UrllibTransport().request("POST", url, {}, b"{}")
    assert response.body == payload


def test_a_rejected_password_blocks_the_next_attempt() -> None:
    """Auth0 bot detection locks the utility account, so a retry must wait."""
    transport = FixtureTransport(fail_login=True)
    client = SpGroupClient(transport=transport, clock=FixedClock())
    with pytest.raises(AuthError):
        client.login("user@example.com", "wrong")
    attempts = len(transport.requests)

    with pytest.raises(AuthError) as raised:
        SpGroupClient(transport=transport, clock=FixedClock()).login(
            "user@example.com", "wrong-again"
        )
    assert raised.value.error == "too_many_attempts"
    # No second request left the process: the block is local.
    assert len(transport.requests) == attempts


def test_the_cooldown_expires_and_a_good_password_then_signs_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mistyped password must not lock the account out for good."""
    with pytest.raises(AuthError):
        SpGroupClient(transport=FixtureTransport(fail_login=True)).login(
            "u@example.com", "a"
        )
    with pytest.raises(AuthError) as blocked:
        SpGroupClient(transport=FixtureTransport()).login("u@example.com", "b")
    assert blocked.value.error == "too_many_attempts"

    later = time.monotonic() + LOGIN_RETRY_COOLDOWN_SECONDS
    monkeypatch.setattr(time, "monotonic", lambda: later)
    SpGroupClient(transport=FixtureTransport()).login("u@example.com", "b")
    assert _login_cooldown("u@example.com") == 0


def test_an_mfa_challenge_is_not_a_rejected_password() -> None:
    """The password was right; the code step must not be throttled."""
    transport = FixtureTransport(require_mfa=True)
    with pytest.raises(AuthError) as raised:
        SpGroupClient(transport=transport, clock=FixedClock()).login(
            "user@example.com", "secret"
        )
    assert raised.value.error == "mfa_required"
    assert _login_cooldown("user@example.com") == 0


def test_expired_login_cooldowns_do_not_stay_in_the_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cooldown dict is process-wide; it cannot grow one name per attempt."""
    for index in range(20):
        with pytest.raises(AuthError):
            SpGroupClient(transport=FixtureTransport(fail_login=True)).login(
                f"user{index}@example.com", "a"
            )
    assert len(client_module._LOGIN_FAILURES) == 20

    later = time.monotonic() + LOGIN_RETRY_COOLDOWN_SECONDS
    monkeypatch.setattr(time, "monotonic", lambda: later)
    with pytest.raises(AuthError):
        SpGroupClient(transport=FixtureTransport(fail_login=True)).login(
            "user20@example.com", "a"
        )

    # Only the name that just failed is still throttled; the twenty before it
    # were past their cooldown and could not act on anything.
    assert list(client_module._LOGIN_FAILURES) == ["user20@example.com"]
    assert _login_cooldown("user0@example.com") == 0


def test_upstream_error_text_is_stripped_and_bounded() -> None:
    """Hostile error text reaches the log and the reauth dialog; cap it there."""
    hostile = "wrong password ‮" + "x" * 500 + "\x1b[31m"
    transport = FixtureTransport(
        responses={
            OAUTH_TOKEN_PATH: HttpResponse(
                403,
                json.dumps({"error": hostile, "error_description": hostile}).encode(),
            )
        }
    )
    with pytest.raises(AuthError) as raised:
        SpGroupClient(transport=transport, clock=FixedClock()).login(
            "user@example.com", "secret"
        )
    for text in (raised.value.error, raised.value.error_description, str(raised.value)):
        assert len(text) <= ERROR_VALUE_CHARS + 3
        assert "\x1b" not in text
        assert "‮" not in text
