"""What an operator can still learn after the log line has scrolled away.

An optional read that fails drops the sensors it feeds, but the poll itself
succeeds, so nothing else marks the entity unavailable or says which upstream
broke. These tests pin the two things that survive the log: one warning per
failed read, and the read recorded on the client for the diagnostics download.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

import pytest

from custom_components.sp_group.client import HttpResponse, TransportError
from custom_components.sp_group.const import B2C_HOST, JARVIS_SMRD_PATH

from .conftest import FixtureTransport, fixture_client

SMRD_PATH = f"{JARVIS_SMRD_PATH}/premise-001"
SMRD = f"{B2C_HOST}{SMRD_PATH}"
SMRD_LABEL = f"GET {B2C_HOST}{JARVIS_SMRD_PATH}/{{id}}"


def test_a_failed_optional_read_is_kept_for_the_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 5xx on the meter-register read is recorded with the reason it failed."""
    transport = FixtureTransport(
        responses={SMRD_PATH: HttpResponse(500, b'{"error":"boom"}')}
    )
    client = fixture_client(transport)
    with caplog.at_level(logging.WARNING):
        client.fetch_usage()
    assert list(client.read_failures) == [SMRD_LABEL]
    assert "HTTP 500" in next(iter(client.read_failures.values()))


def test_one_failed_read_writes_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The status and the host's own message share a line, not one each."""
    transport = FixtureTransport(
        responses={
            SMRD_PATH: HttpResponse(
                401, json.dumps({"error": "token expired"}).encode()
            )
        }
    )
    client = fixture_client(transport)
    with caplog.at_level(logging.WARNING):
        client.fetch_usage()
    lines = [record.getMessage() for record in caplog.records]
    assert len([line for line in lines if "smrd" in line]) == 1
    assert any("token expired" in line for line in lines)


def test_an_absent_optional_read_is_not_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 404 is the account not being enrolled, which every poll would repeat."""
    transport = FixtureTransport(responses={SMRD_PATH: HttpResponse(404, b"{}")})
    client = fixture_client(transport)
    with caplog.at_level(logging.WARNING):
        client.fetch_usage()
    assert client.read_failures == {}
    assert not [r for r in caplog.records if "smrd" in r.getMessage()]


def test_a_recovered_read_stops_being_reported() -> None:
    """Each poll reports on itself; last cycle's failure is not this one's."""
    failing = FixtureTransport(responses={SMRD_PATH: HttpResponse(503, b"{}")})
    client = fixture_client(failing)
    client.fetch_usage()
    assert client.read_failures
    client._transport = FixtureTransport()
    client.fetch_usage()
    assert client.read_failures == {}


def test_a_transport_failure_names_the_read_it_took_down() -> None:
    """An unreachable host is a failed read, not an absent one."""

    class BrokenMeterRegister:
        """The recorded responses, except the meter read, which cannot connect."""

        def __init__(self, inner: FixtureTransport) -> None:
            self._inner = inner

        def request(
            self,
            method: str,
            url: str,
            headers: Mapping[str, str],
            body: bytes | None,
            *,
            timeout: int | None = None,
        ) -> HttpResponse:
            if url.startswith(SMRD):
                raise TransportError(f"GET {SMRD} failed (timeout 8s): timed out")
            return self._inner.request(
                method,
                url,
                headers,
                body,
                timeout=timeout,
            )

    client = fixture_client()
    client._transport = BrokenMeterRegister(FixtureTransport())
    client.fetch_usage()
    recorded = client.read_failures
    assert len(recorded) == 1
    reason = next(iter(recorded.values()))
    assert "timed out" in reason
    # The key is the redacted read, so the premise id stays out of the
    # diagnostics download.
    assert "premise-001" not in next(iter(recorded))
