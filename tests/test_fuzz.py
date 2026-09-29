"""Structure-aware fuzzing of the parsers that read SP Group responses.

Every payload in this integration comes off a remote host, so each parser is a
trust boundary. The seeds are the recorded fixtures; each round corrupts one
node of the decoded JSON with a hostile value and asserts the contract the
coordinator relies on:

* the only exceptions that escape are ``UsageError`` and ``AuthError``, the two
  the coordinator turns into ``UpdateFailed`` / ``ConfigEntryAuthFailed``,
* every float that reaches a sensor is finite (json.loads accepts the NaN and
  Infinity literals, and "1e400" parses to inf),
* every timestamp is timezone-aware and convertible to SGT, which every history
  fold does.

Mutations are drawn from a seeded ``random.Random``, so a failure reproduces
from the seed and round printed in the assertion message. ``FUZZ_SEED`` in the
environment picks the seed; without it the recorded default runs.
"""

from __future__ import annotations

import json
import math
import os
import random
from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import pytest

from custom_components.sp_group import client as client_module
from custom_components.sp_group.client import (
    AuthError,
    HttpResponse,
    SpGroupClient,
    UsageError,
)
from custom_components.sp_group.const import (
    JARVIS_AMI_PATH,
    JARVIS_CHARTS_PATH,
    JARVIS_GREEN_GOALS_PATH,
    JARVIS_ME_PATH,
    JARVIS_SMRD_PATH,
    NJORD_HISTORY_PATH,
    NJORD_PAYABLES_PATH,
)
from custom_components.sp_group.mapper import extra_attributes, sensors_from_usage
from custom_components.sp_group.models import SG_TZ

from .conftest import FIXED_NOW, FixedClock, FixtureTransport, load_fixture

DEFAULT_FUZZ_SEED = 20240315
FUZZ_SEED = int(os.environ.get("FUZZ_SEED", DEFAULT_FUZZ_SEED))
STRUCTURE_ROUNDS = 250
BYTE_ROUNDS = 400
END_TO_END_ROUNDS = 60

# Values a hostile or broken host can put in any JSON position. All survive a
# json.dumps/loads round trip, so the end-to-end fuzz can serve them on the wire.
HOSTILE: tuple[object, ...] = (
    None,
    True,
    False,
    0,
    -1,
    10**309,  # overflows float()
    -(10**309),
    1e308,
    float("nan"),
    float("inf"),
    float("-inf"),
    "",
    " ",
    "abc",
    "NaN",
    "Infinity",
    "1e400",
    "-",
    "0x10",
    "１２３",  # fullwidth digits: float() accepts these
    "١٢٣",  # arabic-indic digits: float() accepts these too
    "\x00\x01",
    "‮﻿",
    "A" * 4096,
    "9" * 5000,  # past the int/str conversion limit
    "9999-12-31T23:59:59Z",
    "0001-01-01T00:00:00+14:00",
    "2024-13-45T99:99:99Z",
    "2024-01-01T00:00:00+99:00",
    "-0001-01-01",
    [],
    {},
    [{}],
    [[[[[[[[[[None]]]]]]]]]],
    {"data": {"data": {"data": []}}},
)


def _paths(node: object, prefix: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    """Every addressable position in a decoded JSON document, root included."""
    found = [prefix]
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(_paths(value, (*prefix, key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_paths(value, (*prefix, index)))
    return found


def _replace(node: Any, path: tuple[object, ...], value: object) -> Any:
    """Copy ``node`` with ``path`` set to ``value``; an empty path is the root."""
    if not path:
        return value
    head, rest = path[0], path[1:]
    if isinstance(node, dict) and head in node:
        return {
            key: _replace(item, rest, value) if key == head else item
            for key, item in node.items()
        }
    if isinstance(node, list) and isinstance(head, int) and head < len(node):
        return [
            _replace(item, rest, value) if index == head else item
            for index, item in enumerate(node)
        ]
    return node


def _drop(node: Any, path: tuple[object, ...]) -> Any:
    if not path:
        return None
    head, rest = path[0], path[1:]
    if not rest:
        if isinstance(node, dict) and head in node:
            return {key: item for key, item in node.items() if key != head}
        if isinstance(node, list) and isinstance(head, int) and head < len(node):
            return [item for index, item in enumerate(node) if index != head]
        return node
    return _replace(node, (head,), _drop(node[head], rest))  # type: ignore[index]


def mutate(seed: Any, rng: random.Random) -> Any:
    """One structure-aware edit: swap a node's type, delete it, or repeat it."""
    path = rng.choice(_paths(seed))
    roll = rng.random()
    if roll < 0.65:
        return _replace(seed, path, rng.choice(HOSTILE))
    if roll < 0.85:
        return _drop(seed, path)
    node = seed
    for step in path:
        node = node[step]
    return _replace(seed, path, [node, node])


def check_invariants(value: object, where: str) -> None:
    """Nothing a parser returns may carry NaN, inf, or an unshiftable datetime."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return
    if isinstance(value, float):
        assert math.isfinite(value), f"{where}: non-finite float {value!r}"
        return
    if isinstance(value, datetime):
        assert value.tzinfo is not None, f"{where}: naive datetime {value!r}"
        try:
            value.astimezone(SG_TZ)
        except (OverflowError, OSError) as exc:
            raise AssertionError(f"{where}: {value!r} has no SGT form: {exc}") from exc
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            check_invariants(item, f"{where}[{key!r}]")
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            check_invariants(item, f"{where}[{index}]")
        return
    if is_dataclass(value):
        for field in fields(value):
            check_invariants(getattr(value, field.name), f"{where}.{field.name}")
        return
    raise AssertionError(f"{where}: unexpected value type {type(value).__name__}")


def _charts(body: object) -> list[object]:
    if not isinstance(body, dict):
        body = {}
    return [
        client_module._parse_utility(body.get(kind), kind)
        for kind in ("elec", "water", "gas")
    ]


def _premise(body: object) -> object:
    if not isinstance(body, dict):
        raise UsageError("me payload is not an object")
    client = SpGroupClient(transport=FixtureTransport(), clock=FixedClock())
    return client_module._parse_premise(client._select_premise(body))


# (fixture, parser) pairs. Each parser is fed the fixture's decoded body the way
# the client feeds it, so the fuzz drives the shipped call, not a stand-in.
TARGETS: tuple[tuple[str, Callable[[object], object]], ...] = (
    ("jarvis_charts.json", _charts),
    ("jarvis_charts_gas.json", _charts),
    ("jarvis_me.json", _premise),
    ("jarvis_ami_day.json", client_module._parse_ami_rows),
    ("jarvis_ami_month.json", client_module._parse_ami_rows),
    ("njord_history.json", client_module._parse_bills),
    (
        "njord_payables.json",
        lambda body: client_module._parse_payable(body, "1234567890", "0012345678"),
    ),
    (
        "jarvis_greengoals.json",
        lambda body: client_module._parse_green_goals(body, "1234567890"),
    ),
    ("jarvis_smrd.json", client_module._parse_meter_reading),
    ("jarvis_smrd.json", client_module._parse_meter_registers),
    (
        "oauth_token_success.json",
        lambda body: client_module._session_from_oauth(
            body if isinstance(body, dict) else {}, None
        ),
    ),
)


# Optional payloads have no recorded fixture. These seeds carry the field names
# and types the parsers read, so a mutation lands on a field that is used.
SYNTHETIC_TARGETS: tuple[tuple[str, object, Callable[[object], object]], ...] = (
    (
        "greenup",
        {
            "data": {
                "account": {
                    "node": {
                        "totalPoints": 1200,
                        "tier": {
                            "node": {
                                "level": 2,
                                "name": "Green",
                                "pointsToLevelUp": 300,
                            }
                        },
                    }
                }
            }
        },
        client_module._parse_greenup,
    ),
    (
        "ev_wallet",
        {"points_balance": 500, "dollar_balance": "12.30", "current_tier_id": 2},
        client_module._parse_ev_wallet,
    ),
    (
        "ev_session",
        {
            "data": {
                "status": "charging",
                "kwh": "7.5",
                "total_cost": "3.20",
                "start_datetime": "2024-03-01T10:00:00Z",
                "order_id": "A1",
            }
        },
        client_module._parse_ev_session,
    ),
    (
        "ev_last_charge",
        {
            "data": [
                {
                    "total_consumption": "7.5",
                    "connector_kwh": "7.4",
                    "transaction_amount": "1230",
                    "created_at": "2024-03-01T10:00:00Z",
                    "transaction_status": "PAID",
                    "address": "1 Example Road",
                }
            ]
        },
        client_module._parse_ev_last_charge,
    ),
    (
        "ev_unpaid",
        {"data": {"orders": [{"amount": "1230"}, {"amount": 50}]}},
        client_module._parse_ev_unpaid,
    ),
    (
        "unread",
        {"total_unread_notifications": 3},
        client_module._parse_unread,
    ),
    (
        "bill_delivery",
        {
            "preferences": [
                {"accountNo": "0012345678", "isSoftCopy": True, "isHardCopy": False}
            ]
        },
        lambda body: client_module._parse_bill_delivery(body, "0012345678"),
    ),
    (
        "paired_fcus",
        {"data": {"getPairedFCUs": [{"thingName": "fcu-1", "displayName": "Living"}]}},
        client_module._parse_paired_fcus,
    ),
    (
        "fcu_status",
        {
            "is_on": True,
            "is_online": True,
            "room_temperature": 24.5,
            "temperature": 23,
            "operation_mode": "cool",
            "display_name": "Living",
        },
        lambda body: client_module._parse_fcu_status(body, "fcu-1", "Living"),
    ),
    (
        "tariff",
        {"sp_kwh_price": "0.2994", "sp_monthly_price": "104.79", "consumption": "350"},
        client_module._parse_tariff,
    ),
)


def run_fuzz(label: str, seed: object, parser: Callable[[object], object]) -> None:
    """Mutate the seed STRUCTURE_ROUNDS times and hold the parser to its contract."""
    rng = random.Random(f"{FUZZ_SEED}-{label}")
    for round_number in range(STRUCTURE_ROUNDS):
        payload = mutate(seed, rng)
        where = f"{label} seed={FUZZ_SEED} round={round_number}"
        try:
            result = parser(payload)
        except (UsageError, AuthError):
            continue
        except Exception as exc:  # a bare crash is exactly what this looks for
            raise AssertionError(
                f"{where}: {type(exc).__name__}: {exc}\n{json.dumps(payload)[:2000]}"
            ) from exc
        check_invariants(result, where)


@pytest.mark.parametrize(
    ("fixture", "parser"),
    TARGETS,
    ids=[f"{name}-{index}" for index, (name, _) in enumerate(TARGETS)],
)
def test_fuzz_parser_holds_its_contract(
    fixture: str, parser: Callable[[object], object]
) -> None:
    run_fuzz(fixture, json.loads(load_fixture(fixture)), parser)


@pytest.mark.parametrize(
    ("label", "seed", "parser"),
    SYNTHETIC_TARGETS,
    ids=[label for label, _, _ in SYNTHETIC_TARGETS],
)
def test_fuzz_optional_parser_holds_its_contract(
    label: str, seed: object, parser: Callable[[object], object]
) -> None:
    """Optional payloads still fail the poll when they crash: _fetch_optional
    only catches transport failures, not a parse that raises."""
    run_fuzz(label, seed, parser)


def test_fuzz_scalar_readers_reject_hostile_values() -> None:
    """Every scalar reader takes a value the host chose; none may crash on one."""
    scalar_readers = (
        client_module._optional_float,
        client_module._cents_to_sgd,
        client_module._eva_sgd,
        client_module._optional_str,
        client_module._optional_bool,
        client_module._parse_period_start,
    )
    for value in HOSTILE:
        where = f"value={value!r}"[:120]
        for reader in scalar_readers:
            check_invariants(reader(value), where)
        check_invariants(
            client_module._parse_unread({"total_unread_notifications": value}), where
        )
        try:
            check_invariants(client_module._float(value), where)
        except UsageError:
            pass


def test_fuzz_json_decoding_never_leaks_a_raw_exception() -> None:
    """Truncated and byte-flipped bodies decode or raise UsageError, nothing else."""
    seed = load_fixture("jarvis_charts.json")
    rng = random.Random(f"{FUZZ_SEED}-bytes")
    for round_number in range(BYTE_ROUNDS):
        body = bytearray(seed)
        for _ in range(rng.randint(1, 4)):
            roll = rng.random()
            if roll < 0.4 and body:
                del body[rng.randrange(len(body)) :]
            elif roll < 0.8 and body:
                body[rng.randrange(len(body))] = rng.randrange(256)
            else:
                at = rng.randrange(len(body) + 1)
                body[at:at] = bytes(
                    rng.randrange(256) for _ in range(rng.randint(1, 8))
                )
        response = HttpResponse(status=200, body=bytes(body))
        where = f"bytes seed={FUZZ_SEED} round={round_number}"
        try:
            client_module._require_json(response, "charts")
        except UsageError:
            pass
        except Exception as exc:  # a bare crash is exactly what this looks for
            raise AssertionError(f"{where}: _require_json {exc!r}") from exc
        try:
            client_module._optional_json(response)
            client_module._eva_scope_denied(HttpResponse(status=403, body=bytes(body)))
        except Exception as exc:  # a bare crash is exactly what this looks for
            raise AssertionError(f"{where}: optional read {exc!r}") from exc


def test_fuzz_jwt_expiry_never_raises() -> None:
    """Session expiry is read from a token the identity host controls."""
    rng = random.Random(f"{FUZZ_SEED}-jwt")
    alphabet = "eyJhbGciOiJ.-_0123456789=+/ \x00é"
    for _ in range(BYTE_ROUNDS):
        token = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
        expiry = client_module._jwt_exp(token)
        assert expiry is None or isinstance(expiry, int)


class MutatingTransport:
    """Fixture transport that corrupts one JSON node of one response path."""

    def __init__(self, marker: str, rng: random.Random) -> None:
        self.inner = FixtureTransport()
        self.marker = marker
        self.rng = rng
        self.mutated: object = None

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        *,
        timeout: int | None = None,
    ) -> HttpResponse:
        response = self.inner.request(method, url, headers, body, timeout=timeout)
        if response.status != 200 or self.marker not in urlparse(url).path:
            return response
        try:
            decoded = json.loads(response.body)
        except ValueError:
            return response
        self.mutated = mutate(decoded, self.rng)
        return HttpResponse(
            status=response.status,
            body=json.dumps(self.mutated).encode("utf-8"),
        )


MUTATED_PATHS = (
    JARVIS_ME_PATH,
    JARVIS_CHARTS_PATH,
    JARVIS_AMI_PATH,
    JARVIS_SMRD_PATH,
    JARVIS_GREEN_GOALS_PATH,
    NJORD_HISTORY_PATH,
    NJORD_PAYABLES_PATH,
)


@pytest.mark.parametrize("marker", MUTATED_PATHS)
def test_fuzz_usage_read_survives_a_corrupt_response(marker: str) -> None:
    """A corrupt response fails the poll or produces sensors, never a raw crash.

    The assertions run on both sides of the parse: the readings the client
    builds, and the sensor values and attributes the mapper derives from them.
    """
    rng = random.Random(f"{FUZZ_SEED}-{marker}")
    for round_number in range(END_TO_END_ROUNDS):
        transport = MutatingTransport(marker, rng)
        client = SpGroupClient(transport=transport, clock=FixedClock())
        client.login("user@example.com", "secret")
        where = f"{marker} seed={FUZZ_SEED} round={round_number}"
        try:
            usage = client.fetch_usage()
        except (UsageError, AuthError):
            continue
        except Exception as exc:  # a bare crash is exactly what this looks for
            raise AssertionError(
                f"{where}: {type(exc).__name__}: {exc}\n"
                f"{json.dumps(transport.mutated)[:2000]}"
            ) from exc
        check_invariants(usage, f"{where} usage")
        specs = sensors_from_usage(usage, FIXED_NOW)
        check_invariants(specs, f"{where} sensors")
        for spec in specs:
            check_invariants(
                extra_attributes(usage, spec.key, FIXED_NOW),
                f"{where} attrs[{spec.key}]",
            )
