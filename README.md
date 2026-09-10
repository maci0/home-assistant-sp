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

Polls every 30 minutes. After the first successful poll, long-term statistics are written for electricity (and gas when present).

- Grid consumption: `sensor.sp_group_utilities_electricity` (**Electricity cumulative**) only. That series is AMI half-hours folded to clock hours when the premise has `ami_elec`, otherwise billed monthly kWh. It is the loaded window (~13 months), not today.
- Do not add **Electricity last billed**, **Electricity meter**, or **Electricity this month** as grid sources. They are a different number: last billed period, the physical register, and Green Goals month-to-date.
- Leave Water empty in Energy. SP bills water monthly. **Water** is the sum of billed months; **Water meter** is the lifetime register. Neither is an hourly series.
- Gas: `sensor.sp_group_utilities_gas` only if Jarvis returned billed gas periods.

AMI electricity lags a few hours. Empty future 30-minute slots are dropped. **Electricity last 30 min** is the last published slot, not the clock hour.

## Entities

Names below are the entity names. Unique id is `{premise_id}_{key}`. Optional rows are created only when that API returns data. New keys from a later poll are added without a reload.

### Usage

| Name | Key | What it is |
| --- | --- | --- |
| Electricity cumulative | `electricity` | AMI / billed total for the loaded window (~13 months). Energy grid source. Unique id stays `electricity` |
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
| Amount due | `amount_due` | Njord payable in SGD. Negative is a credit |
| Prepaid credit | `ppms_credit` | PPMS SGD when `/me` says a prepaid account exists |

### Meters and Green Goals

| Name | Key | What it is |
| --- | --- | --- |
| Electricity meter | `electricity_meter` | SMRD last_actual snapshot, kWh. `measurement`, not Energy |
| Water meter | `water_meter` | SMRD last_actual snapshot, m³. `measurement`, not Energy |
| Electricity this month | `electricity_goal` | Green Goals used kWh. Attributes: `goal_target`, `percent_difference`, `cost_difference_sgd` |
| Water this month | `water_goal` | Same for water when used or target is non-zero |

### Optional

| Name | Key | Created when |
| --- | --- | --- |
| GreenUP points | `greenup_points` | 1UP GraphQL account node exists |
| EV wallet | `ev_wallet` | Tyche points or dollars are non-zero |
| EV session | `ev_session` | Eva latest session has status or kWh |
| EV last charge | `ev_last_charge` | Eva receipts list is non-empty |
| EV unpaid | `ev_unpaid` | Eva unpaid orders list is non-empty |
| Unread notifications | `unread_notifications` | Notifications API returns a count |
| Bill delivery | `bill_delivery` | Skalbox preferences exist. State is `ebill` or `paper`, displayed through the translation catalog |
| FCU | `fcu_{thing}` | One sensor per Frosty paired Tengah coil. Room temperature, else `on` / `off` |
| SP tariff | `tariff` | Public priceplan `sp_kwh_price`. Query uses last billed kWh, else 350 |
| Account | `account` | Always. Status plus address, account number, AMI flag, retailer, next meter-reading window |

Shared attributes on usage sensors: `premise_id`, `address`, `account_number`, `last_period`, `last_period_amount`, `period_count`, `average_consumption`, `comparison`.

Reconfigure the entry if the password changes. Reauth starts when the stored session is rejected.

## Poll

Required, in order:

1. `POST https://identity.spdigital.sg/oauth/token` Auth0 password-realm, mfa-otp (authenticator) or mfa-oob (SMS/email) when MFA is required, or refresh_token. Scopes include `me me:uportal me:eva me:rbac`
2. `GET https://b2c.api.spdigital.sg/jarvis/v3/me`
3. `GET https://b2c.api.spdigital.sg/jarvis/v4/charts/{premise_id}`
4. `POST https://b2c.api.spdigital.sg/jarvis/v3/ami/charts` when `ami_elec` (`grouped_by` `day` then `month`)
5. `GET https://b2c.api.spdigital.sg/jarvis/v3/smrd-uportal/{premise_id}`
6. `GET https://b2c.api.spdigital.sg/jarvis/v3/ppms/balance/{premise_id}` only if prepaid exists
7. `GET https://b2c.api.spdigital.sg/njord/v4/payables` (integer cents)
8. `GET https://b2c.api.spdigital.sg/njord/v3/history?account_numbers={account}` latest `type=bill`. PDF URLs are not stored
9. `GET https://b2c.api.spdigital.sg/jarvis/v5/greengoals/targets`

Then optional reads (8s HTTP timeout each). 4xx or empty payloads skip the matching sensor:

- `POST /1up/authenticated/graphql` GreenUP account
- `GET /tyche/v1/wallet-summary`
- `GET /eva/v1/sessions/latest`, `/eva/v2/order/receipts`, `/eva/v1/order/unpaid`. A 403 `scope_not_found` on the session call skips the rest of Eva
- `GET /notifications/v1/notifications` unread count only (bodies are not stored)
- `GET /skalbox/b2c/account/v1/retrieveBillPreferences`
- `POST /frosty/graphql` paired FCUs, then `/frosty/fcu_status` per `thingName`
- `GET https://public.api.spdigital.sg/priceplan/v2/plans/price?consumption={last billed kWh or 350}`

The refresh token is stored on the config entry so restarts do not password-login every time. Diagnostics omit the password and tokens.

## Not included

Bill pay, GIRO setup, UniDollar pay, add card, start/stop EV charge, meter-reading submit, Singpass, GreenUP quests, FCU pairing.

## Troubleshooting

- **invalid_claim / rejected session token:** the client must request the `me:*` scopes. Use this repo, not a stale copy.
- **Suspicious request requires verification:** Auth0 bot detection after many password logins. Sign in once in the SP app, wait a few minutes, then reload or reauthenticate.
- **No Energy statistics:** wait for the first poll, hard-refresh Energy settings, then pick `sensor.sp_group_utilities_electricity`, not last-billed or meter sensors.
- **Missing optional sensor after upgrade:** wait for the next poll. New keys are added without a reload.

## Examples

Notify when amount due is positive:

```yaml
automation:
  - alias: SP bill due
    triggers:
      - trigger: numeric_state
        entity_id: sensor.sp_group_utilities_amount_due
        above: 0
    actions:
      - action: notify.notify
        data:
          message: "SP amount due {{ states('sensor.sp_group_utilities_amount_due') }} SGD"
```

## Development

```
uv sync --extra dev
uv run pytest
uv run python scripts/launch_client.py
```

Optional live call: `SP_USERNAME` and `SP_PASSWORD`.

`tests/test_fuzz.py` fuzzes the response parsers: it corrupts one node of a
recorded fixture per round and asserts that only `UsageError` or `AuthError`
escapes, that every float reaching a sensor is finite, and that every timestamp
converts to SGT. `FUZZ_SEED` fixes the mutation sequence, so a failure names the
seed and round that produced it; paste an offending payload into a case in
`tests/` to keep it as a regression.

### Modules

`custom_components/sp_group/`, listed in dependency order. Each module may import
those above it, never those below.

| Module | Holds |
|---|---|
| `const.py` | Hosts, API paths, Auth0 parameters, sensor keys, units |
| `models.py` | Frozen dataclasses for premise, usage, bills, meters, EV, FCU |
| `history.py` | Period arithmetic: fold, trim, merge, cumulative, monthly |
| `mapper.py` | Usage readings to sensor specs and entity attributes |
| `client.py` | Auth0 login, MFA, refresh, HTTP transport, JSON to models |
| `coordinator.py` | 30-minute poll, session persistence, statistics import |
| `entity.py` `sensor.py` `config_flow.py` `diagnostics.py` | Home Assistant surfaces |

`const.py` through `client.py` import no Home Assistant code and are what the
tests cover; `mypy --strict` gates them via `[tool.mypy] files`. The package
`__init__.py` imports Home Assistant inside `async_setup_entry` so the modules
above stay importable without it.
