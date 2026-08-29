# SP Group for Home Assistant

Unofficial HACS integration for Singapore Power e-accounts. It polls the same Auth0 + Jarvis + Njord APIs as Android app `sg.com.singaporepower.spservices` 15.10.0 and exposes usage, bills, meter registers, and optional EV / GreenUP / Tengah sensors.

## Install

### HACS

1. HACS → Integrations → Custom repositories
2. URL `https://github.com/maci0/home-assistant-sp`, category Integration
3. Download **SP Group**, restart Home Assistant
4. Settings → Devices & services → Add integration → **SP Group**
5. Sign in with the same e-account email and password as the SP app

### Manual

Copy `custom_components/sp_group` into `<config>/custom_components/sp_group` and restart.

## Energy dashboard

Polls every 30 minutes. After the first successful poll, long-term statistics are written for electricity (and gas when present).

- Grid consumption: `sensor.sp_group_utilities_electricity` only. That series is AMI half-hours folded to clock hours when the premise has `ami_elec`, otherwise billed monthly kWh.
- Do not add **Electricity last billed**, **Electricity meter**, or **Electricity this month** as grid sources. They are a different number: last billed period, the physical register, and Green Goals month-to-date.
- Leave Water empty in Energy. SP bills water monthly. **Water** is the sum of billed months; **Water meter** is the lifetime register. Neither is an hourly series.
- Gas: `sensor.sp_group_utilities_gas` only if Jarvis returned billed gas periods.

AMI electricity lags a few hours. Empty future 30-minute slots are dropped. **Electricity last 30 min** is the last published slot, not the clock hour.

## Entities

Names below are the entity names. Unique id is `{premise_id}_{key}`. Optional rows are created only when that API returns data. Reload the integration after an upgrade if a new sensor is missing.

### Usage

| Name | Key | What it is |
| --- | --- | --- |
| Electricity | `electricity` | Cumulative kWh, `total_increasing`. AMI when `ami_elec`, else billed months |
| Electricity last billed | `electricity_last_period` | Latest billed month kWh |
| Electricity today | `electricity_today` | AMI kWh for today in SGT |
| Electricity last 30 min | `electricity_last_hour` | Last published AMI slot |
| Water | `water` | Sum of billed monthly m³. Not Energy hourly |
| Water last billed | `water_last_period` | Latest billed month m³ |
| Gas / Gas last billed | `gas`, `gas_last_period` | Only if Jarvis `gas.data` is non-empty |

### Bill

| Name | Key | What it is |
| --- | --- | --- |
| Last bill | `last_bill` | Latest Njord bill in SGD (cents / 100) |
| Amount due | `amount_due` | Njord payable in SGD. Negative is a credit |
| Prepaid credit | `ppms_credit` | PPMS SGD when `/me` says a prepaid account exists |

### Meters and Green Goals

| Name | Key | What it is |
| --- | --- | --- |
| Electricity meter | `electricity_meter` | Last actual SMRD register, kWh |
| Water meter | `water_meter` | Last actual SMRD register, m³ |
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
| Bill delivery | `bill_delivery` | Skalbox preferences exist (`e-bill` or `paper`) |
| FCU | `fcu` | Frosty reports a paired Tengah fan coil |
| SP tariff | `tariff` | Public priceplan host returns `sp_kwh_price` |
| Account | `account` | Always. Status plus address, account number, AMI flag, retailer, next meter-reading window |

Shared attributes on usage sensors: `premise_id`, `address`, `account_number`, `last_period`, `last_period_amount`, `period_count`, `average_consumption`, `comparison`.

Reconfigure the entry if the password changes. Reauth starts when the stored session is rejected.

## Poll

Required, in order:

1. `POST https://identity.spdigital.sg/oauth/token` Auth0 password-realm or refresh_token. Scopes include `me me:uportal me:eva me:rbac`
2. `GET https://b2c.api.spdigital.sg/jarvis/v3/me`
3. `GET https://b2c.api.spdigital.sg/jarvis/v4/charts/{premise_id}`
4. `POST https://b2c.api.spdigital.sg/jarvis/v3/ami/charts` when `ami_elec` (`grouped_by` `day` then `month`)
5. `GET https://b2c.api.spdigital.sg/jarvis/v3/smrd-uportal/{premise_id}`
6. `GET https://b2c.api.spdigital.sg/jarvis/v3/ppms/balance/{premise_id}` only if prepaid exists
7. `GET https://b2c.api.spdigital.sg/njord/v4/payables` (integer cents)
8. `GET https://b2c.api.spdigital.sg/njord/v3/history?account_numbers={account}` latest `type=bill`. PDF URLs are not stored
9. `GET https://b2c.api.spdigital.sg/jarvis/v5/greengoals/targets`

Then optional reads. 4xx or empty payloads skip the matching sensor:

- `POST /1up/authenticated/graphql` GreenUP account
- `GET /tyche/v1/wallet-summary`
- `GET /eva/v1/sessions/latest`, `/eva/v2/order/receipts`, `/eva/v1/order/unpaid`
- `GET /notifications/v1/notifications` unread count only (bodies are not stored)
- `GET /skalbox/b2c/account/v1/retrieveBillPreferences`
- `POST /frosty/graphql` paired FCUs, then `/frosty/fcu_status`
- `GET https://public.api.spdigital.sg/priceplan/v2/plans/price?consumption=350`

The refresh token is stored on the config entry so restarts do not password-login every time. Diagnostics omit the password and tokens.

## Not included

Bill pay, GIRO setup, UniDollar pay, add card, start/stop EV charge, meter-reading submit, Singpass, GreenUP quests, FCU pairing.

## Troubleshooting

- **invalid_claim / rejected session token:** the client must request the `me:*` scopes. Use this repo, not a stale copy.
- **Suspicious request requires verification:** Auth0 bot detection after many password logins. Sign in once in the SP app, wait a few minutes, then reload or reauthenticate.
- **No Energy statistics:** wait for the first poll, hard-refresh Energy settings, then pick `sensor.sp_group_utilities_electricity`, not last-billed or meter sensors.
- **Missing optional sensor after upgrade:** reload the SP Group integration so setup can create new entities.

## Development

```
uv sync --extra dev
uv run pytest
uv run python scripts/launch_client.py
```

Optional live call: `SP_USERNAME` and `SP_PASSWORD`.
