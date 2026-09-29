# SP Group for Home Assistant

[![hacs](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/maci0/home-assistant-sp)](https://github.com/maci0/home-assistant-sp/releases)

Unofficial HACS integration for Singapore Power e-accounts. It polls the same Auth0 + Jarvis + Njord APIs as Android app `sg.com.singaporepower.spservices` 15.10.0 and exposes usage, bills, meter registers, and optional EV / GreenUP / Tengah sensors.

Not affiliated with SP Group.

## Screenshots

Device name in the live UI is the SP premise address. The shots below use a placeholder.

![SP Group custom integration](images/integration.png)

![Sensors for usage, bill, meters, and Green Goals](images/device.png)

## Install

### HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=maci0&repository=home-assistant-sp&category=integration)

1. HACS → three dots → Custom repositories
2. URL `https://github.com/maci0/home-assistant-sp`, category Integration
3. Download **SP Group**, restart Home Assistant
4. Settings → Devices & services → Add integration → **SP Group**
5. Sign in with the same e-account email and password as the SP app. If Auth0 requires MFA, enter the code from SMS, email, or an authenticator app.

Until this is in the HACS default store, the custom repository step is required. HACS then tracks GitHub releases.

### Manual

Copy `custom_components/sp_group` into `<config>/custom_components/sp_group` and restart.

## Energy dashboard

Polls every 30 minutes. After the first successful poll, long-term statistics are written for electricity (and gas when present), plus one **SP Group bill** point per billed month (`sp_group:{premise_id}_last_bill`) when Njord returned bills.

- Grid consumption: the external statistic **SP Group electricity** (`sp_group:{premise_id}_electricity`), not a sensor. That series is AMI half-hours folded to clock hours, backed by the AMI daily points for days the half-hour feed does not cover, when the premise has `ami_elec`; otherwise billed monthly kWh. It is the loaded window (~13 months), not today.
- Cost: set **Electricity price** in the integration's options (SGD per kWh, incl. GST; SP publishes the regulated tariff quarterly). The integration then writes **SP Group electricity cost** (`sp_group:{premise_id}_electricity_cost`); pick it as the grid source's total-cost statistic. HA cannot derive cost from a fixed price for an external statistic on its own. Clearing the field, or entering zero, removes the option and stops writing the cost series.
- Do not pick **Electricity cumulative** as the grid source. The recorder compiles that sensor from its state with a sum that starts at zero, which would disagree with the imported AMI sums and show a negative day.
- Do not add **Electricity last billed**, **Electricity meter**, or **Electricity this month** as grid sources. They are a different number: last billed period, the physical register, and Green Goals month-to-date.
- Leave Water empty in Energy. SP bills water monthly. **Water** is the sum of billed months; **Water meter** is the lifetime register. Neither is an hourly series.
- Gas: **SP Group gas** (`sp_group:{premise_id}_gas`) only if Jarvis returned billed gas periods.
- Each poll writes only the points the recorder does not have yet, so a restart or a re-auth does not re-send the loaded window. A value SP later restates for an already imported point is not rewritten.

AMI electricity lags a few hours. Empty future 30-minute slots are dropped. **Electricity last 30 min** is the last published slot, not the clock hour.

## Entities

Names below are the entity names. Unique id is `{premise_id}_{key}`. Optional rows are created only when that API returns data. New keys from a later poll are added without a reload.

### Usage

| Name | Key | What it is |
| --- | --- | --- |
| Electricity cumulative | `electricity` | AMI / billed total for the loaded window (~13 months). Unique id stays `electricity`. Energy uses the `sp_group:` statistic, not this sensor |
| Electricity last billed | `electricity_last_period` | Latest billed month kWh |
| Electricity today | `electricity_today` | AMI kWh for today in SGT |
| Electricity last 30 min | `electricity_last_hour` | Last published AMI slot |
| Water | `water` | Sum of billed monthly m³. Not Energy hourly |
| Water last billed | `water_last_period` | Latest billed month m³ |
| Gas / Gas last billed | `gas`, `gas_last_period` | Only if Jarvis `gas.data` is non-empty |

### Bill

| Name | Key | What it is |
| --- | --- | --- |
| Last bill | `last_bill` | Latest Njord bill in SGD. History is one point per billed month |
| Amount due | `amount_due` | Njord payable, unit SGD unless the payload carries an ISO currency code. Negative is a credit |
| Prepaid credit | `ppms_credit` | PPMS SGD when `/me` says a prepaid account exists |

### Meters and Green Goals

| Name | Key | What it is |
| --- | --- | --- |
| Electricity meter | `electricity_meter` | SMRD last_actual snapshot, kWh. `total`, not Energy |
| Water meter | `water_meter` | SMRD last_actual snapshot, m³. `total`, not Energy |
| Electricity this month | `electricity_goal` | Green Goals used kWh. Attributes: `goal_target`, `percent_difference`, `cost_difference_sgd` |
| Water this month | `water_goal` | Same for water when used or target is non-zero |

### Optional

| Name | Key | Created when |
| --- | --- | --- |
| GreenUP points | `greenup_points` | 1UP GraphQL account node exists |
| EV wallet | `ev_wallet` | Tyche points or dollars are non-zero |
| EV session | `ev_session` | Eva latest session has a status, kWh, or order id |
| EV last charge | `ev_last_charge` | Eva receipts list returned kWh for its newest row |
| EV unpaid | `ev_unpaid` | Eva unpaid orders list is non-empty |
| Unread notifications | `unread_notifications` | Notifications API returns a count |
| Bill delivery | `bill_delivery` | Skalbox preferences exist. State is `ebill` or `paper`, displayed through the translation catalog |
| FCU | `fcu_{thing}` | One sensor per Frosty paired Tengah coil, keyed by the lowercased `thingName`. Room temperature, else `on` / `off` |
| SP tariff | `tariff` | Public priceplan `sp_kwh_price`. Query uses last billed kWh, else 350 |
| Account | `account` | Always. Status plus address, account number, AMI flag, retailer, next meter-reading window |

Only the **Account** sensor carries the premise identifiers: `premise_id`,
`address`, `account_number`, `account_status`, `account_type`, `premise_type`,
`utilities`, `ami_elec`, `retailer_name`. Usage sensors add `last_period`, `last_period_amount`,
`period_count`, `average_consumption`, `comparison`; **Electricity cumulative**
adds `ami_half_hour_count`, `ami_daily_count`, `today_kwh`, `last_interval`,
`last_interval_kwh`.

Reconfigure the entry if the password changes. Reauth starts when the stored session is rejected.

## Poll

Required, in order:

1. `POST https://identity.spdigital.sg/oauth/token` Auth0 password-realm, mfa-otp (authenticator) or mfa-oob (SMS/email) when MFA is required, or refresh_token. Scopes include `me me:uportal me:eva me:rbac`
2. `GET https://b2c.api.spdigital.sg/jarvis/v3/me`
3. `GET https://b2c.api.spdigital.sg/jarvis/v4/charts/{premise_id}`
4. `GET https://b2c.api.spdigital.sg/jarvis/v3/smrd-uportal/{premise_id}`
5. `GET https://b2c.api.spdigital.sg/jarvis/v3/ppms/balance/{premise_id}` only if prepaid exists
6. `POST https://b2c.api.spdigital.sg/jarvis/v3/ami/charts` when `ami_elec` (`grouped_by` `day` then `month`)
7. `GET https://b2c.api.spdigital.sg/njord/v3/history?account_numbers={account}` latest `type=bill`. PDF URLs are not stored
8. `GET https://b2c.api.spdigital.sg/njord/v4/payables` (integer cents)
9. `GET https://b2c.api.spdigital.sg/jarvis/v5/greengoals/targets`

A failed or empty `/jarvis/v3/me` or `/jarvis/v4/charts` aborts the poll. Steps
4 to 9 are best effort: an error or an empty payload skips whatever that call
feeds, so a missing entity means that one read returned nothing, not that the
whole update failed. Every skipped read except a 404 is logged with the route
and the status, so a sensor that never appears leaves a log line naming the
call that failed.

Then optional reads (8s HTTP timeout each). 4xx or empty payloads skip the matching sensor:

- `POST /1up/authenticated/graphql` GreenUP account
- `GET /tyche/v1/wallet-summary`
- `GET /eva/v1/sessions/latest`, `/eva/v2/order/receipts`, `/eva/v1/order/unpaid`. A 403 `scope_not_found` on the session call skips the rest of Eva
- `GET /notifications/v1/notifications` unread count only (bodies are not stored)
- `GET /skalbox/b2c/account/v1/retrieveBillPreferences`
- `POST /frosty/graphql` paired FCUs, then `/frosty/fcu_status` per `thingName`, for the first 8 coils
- `GET https://public.api.spdigital.sg/priceplan/v2/plans/price?consumption={last billed kWh or 350}`

Every response body is read under an 8 MB cap; a larger one fails the read instead of
growing the process.

The refresh token is stored on the config entry so restarts do not password-login every time. Diagnostics omit the password and tokens.

A rejected password blocks the next sign-in for the same account for 60 seconds, so a
mistyped password or a reauth loop cannot trip Auth0 bot detection. An MFA challenge does
not count as a rejection, and a successful sign-in clears the wait.

## Not included

Bill pay, GIRO setup, UniDollar pay, add card, start/stop EV charge, meter-reading submit, Singpass, GreenUP quests, FCU pairing.

## Troubleshooting

- **invalid_claim / rejected session token:** the client must request the `me:*` scopes. Use this repo, not a stale copy.
- **Suspicious request requires verification:** Auth0 bot detection after many password logins. Sign in once in the SP app, wait a few minutes, then reload or reauthenticate.
- **Too many sign-in attempts:** the client refused a password login within 60 seconds of a rejected one. Wait it out; the password is not the problem.
- **No Energy statistics:** wait for the first poll, hard-refresh Energy settings, then pick **SP Group electricity** (`sp_group:{premise_id}_electricity`), not a sensor.
- **Missing optional sensor after upgrade:** wait for the next poll. New keys are added without a reload.
- **Entities unavailable after a failed poll:** the reason is the `last_error` field in the config entry diagnostics, and the dependency that failed is named in the warning it logged. Set `logger: custom_components.sp_group` to debug for the per-request status and duration of each poll.

## Examples

Notify when amount due is positive. Entities take their name from the device,
which is the premise address, so substitute your own address:

```yaml
automation:
  - alias: SP bill due
    triggers:
      - trigger: numeric_state
        entity_id: sensor.<your premise>_amount_due
        above: 0
    actions:
      - action: notify.notify
        data:
          message: "SP amount due {{ states('sensor.<your premise>_amount_due') }} SGD"
```

## Development

```
uv sync --extra dev --frozen
uv run ruff check custom_components tests scripts
uv run ruff format --check custom_components tests scripts
uv run mypy
uv run pytest
uv run python scripts/launch_client.py
```

The first five are what CI runs. `scripts/launch_client.py` replays the
recorded fixtures through the shipped client and needs no credentials.

`--frozen` fails if `uv.lock` disagrees with `pyproject.toml`, so CI and a
local checkout install the same versions. Use `uv sync --extra dev` only when
intentionally changing dependencies, then commit the updated `uv.lock`.

Optional live call: `SP_USERNAME` and `SP_PASSWORD`. Both are required; the
script exits 1 naming the missing one, 2 on rejected credentials, 3 on a usage
failure, and 0 only when it read the API.

`tests/test_fuzz.py` fuzzes the response parsers: it corrupts one node of a
recorded fixture per round and asserts that only `UsageError` or `AuthError`
escapes, that every float reaching a sensor is finite, and that every timestamp
converts to SGT. The `FUZZ_SEED` constant at the top of that file, overridable
from the environment, fixes the mutation sequence, so a failure names the seed
and round that produced it; paste an offending payload into a case in `tests/`
to keep it as a regression.

### Determinism

A poll reads the network through `UrllibTransport` and the clock through the
`Clock` protocol in `models.py`; nothing else in the client, mapper, or history
layer calls `urlopen` or reads the system clock. Tests pass `FixedClock`
(`tests/conftest.py`), so the same fixtures and the same instant give the same
readings, the same AMI window, and the same sensor values on every run.
`tests/test_replay.py` holds that in place and moves the clock to show which
values follow it. Add a new time-dependent field to that path by threading the
`now` argument the mapper already takes, not by calling `datetime.now`.

### Modules

`custom_components/sp_group/` is layered, and `tests/test_layering.py` enforces
the import graph. Each module may import only the ones below it, and none of
them may import homeassistant:

| Module | May import | Role |
| --- | --- | --- |
| `const` | nothing | Hosts, paths, Auth0 and AMI constants, sensor keys |
| `models` | `const` | Frozen dataclasses for one poll's readings |
| `history` | `const`, `models` | Folds periods into hourly, monthly, and cost series for the recorder |
| `mapper` | `const`, `models`, `history` | Turns `UsageReadings` into `SensorSpec` values and attributes |
| `client` | `const`, `models` | Auth0 login, MFA, and every HTTP read |

The homeassistant-dependent modules (`__init__`, `coordinator`, `config_flow`,
`sensor`, `diagnostics`) sit above that graph.
