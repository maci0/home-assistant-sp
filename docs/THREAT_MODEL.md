# SP Group integration: threat model

Scope: the `sp_group` Home Assistant custom component, its two developer
scripts, and its CI. Every entry point, boundary, and mitigation below names
the code that implements it, so a later pass can re-verify it.

What this component is: a cloud-polling client. It opens no listening socket,
registers no service, and exposes no HTTP endpoint of its own. Inbound data
comes from Home Assistant (the operator typing into a config flow); outbound
data goes to four SP Group hosts and one Auth0 tenant over HTTPS
(`const.py:13`, `const.py:14`, `const.py:15`, `const.py:58`). The attacker
positions that matter are therefore someone who can reach those hosts, and
someone who can read the Home Assistant process or its storage directory.

Last reviewed: 2026-09-29.

## Risk-ranked summary

| # | Risk | Impact | Exploitability | Status |
|---|------|--------|----------------|--------|
| R1 | Access, id, and refresh tokens are persisted in cleartext on the config entry (`client.py:635`, `coordinator.py:198`) | Full account takeover of the user's utility account; the refresh token alone re-mints sessions indefinitely | Low: needs read access to the HA process or a `.storage` copy | G1 |
| R2 | The upstream premise id is interpolated unescaped into request path segments (`client.py:1698`, `client.py:1765`, `client.py:1935`) and into the HA unique id, device identifier, and recorder statistic id (`sensor.py:69`, `history.py:60`) | A hostile or compromised upstream picks which path the client requests on an authenticated SP connection, and which entity and statistic ids the user's history and automations are keyed by | Low: needs a hostile or compromised upstream | G2 |
| R3 | Premise identity and financial data reach every HA user of the instance: address as the device name (`sensor.py:72`), account number and billed amounts as entity attributes (`sensor.py:121`, `mapper.py:191`), and readings written to long-term statistics (`coordinator.py:257`) | Household occupancy, consumption pattern, and billing data leak to any HA account on the instance, including a guest | Medium: any HA account is enough | Accepted; the integration has no per-entity authorization to add |
| R4 | `AuthError.error_description` is cleaned but not length-capped (`client.py:184`) and reaches a log line (`config_flow.py:115`) and a translation placeholder (`const.py:271`) | A hostile identity host can push a megabyte of text into the HA log and into the user-facing dialog, and control how a rejection reads | Low: needs a hostile or compromised identity host | G3 |
| R5 | The client presents itself as the official Android app (`const.py:103`) and ships a hardcoded public client id (`const.py:47`) | Upstream policy or bot detection treats the account as abuse; a client-id change breaks every install at once | Medium: a change upstream, not an attack | G4 |
| R6 | The password stays in the flow instance across the MFA step (`config_flow.py:187`) until Home Assistant drops the flow | A long-lived MFA or reauth flow holds the plaintext password in the HA process memory for as long as the operator takes to answer | Low: needs process memory access | Accepted; the flow needs it to finish the exchange |
| R7 | Upstream values become permanent recorder history (`coordinator.py:257`) | A hostile upstream writes false meter readings into the database that later exports and energy dashboards trust; there is no signature or plausibility check on a reading | Low: needs a hostile or compromised upstream | Accepted; TLS is the only control on this path |

## Mitigations verified in the code

Each row was read against the code, not taken from a claim.

- Response bodies are read under a cap: `MAX_RESPONSE_BYTES` (`const.py:155`)
  enforced by `_read_bounded` (`client.py:288`) on both the success and the
  error path. A hostile body fails the read instead of growing the process.
- Remote text that reaches a log line or a dialog is cleaned of control and
  bidi characters and truncated: `_clean_text` (`client.py:161`), `_safe_text`
  (`client.py:171`), `ERROR_VALUE_CHARS` (`const.py:147`), and
  `TRANSPORT_ERROR_CHARS` (`const.py:150`) for transport failures.
- The password is not persisted. `session_entry_data` (`client.py:635`) writes
  the account name and the tokens only, and `_forget_password`
  (`__init__.py:86`) strips a password an older version left on the entry
  during setup. The README claim at `README.md:142` is accurate.
- A rejected password starts a 60 second cooldown for that account, so a
  mistyped password or a reauth loop cannot trip Auth0 bot detection:
  `LOGIN_RETRY_COOLDOWN_SECONDS` (`const.py:161`), `_login_cooldown`
  (`client.py:1289`), enforced before the login at `client.py:1346`. The
  failure map is swept on every write (`client.py:1298`), so it is bounded by
  the cooldown window rather than growing for the process lifetime.
- Paired-FCU fan-out is capped at 8 reads and each has its own 8 second
  timeout: `MAX_FCU_STATUS_READS` (`const.py:166`), enforced at
  `client.py:1891`, `OPTIONAL_HTTP_TIMEOUT_SECONDS` (`const.py:113`). The
  overflow is logged, so a truncated list is visible.
- The config entry is written on a token change only, and only with the token
  set (`coordinator.py:198`).
- Config-flow input is length-bounded and the options price is validated as a
  finite non-negative number (`config_flow.py:29`, `config_flow.py:33`,
  `config_flow.py:72`). A price edited in `.storage` is caught and reported
  rather than used (`coordinator.py:108`).
- Query strings carry account numbers, thing names, and consumption values, so
  only the redacted path reaches a log line or an error message:
  `_request_label` (`client.py:223`), `_loggable_url` (`client.py:262`).
- Diagnostics report the premise as presence flags and no token value:
  `diagnostics.py:26`, `diagnostics.py:34`, `diagnostics.py:37`. The README
  claim at `README.md:145` is accurate.
- CI actions are pinned by commit, `persist-credentials` is off, and both
  workflows request only `contents: read` (`.github/workflows/ci.yml:27`,
  `.github/workflows/validate.yml:26`).

## Entry points

| Entry point | Kind | Reference |
|---|---|---|
| Config flow: username, password | HA operator input | `config_flow.py:33`, `config_flow.py:239` |
| Config flow: MFA code (TOTP or out-of-band) | HA operator input | `config_flow.py:160`, `config_flow.py:202` |
| Reauth and reconfigure flows | HA operator input | `config_flow.py:270`, `config_flow.py:282` |
| Options flow: electricity price | HA operator input | `config_flow.py:121` |
| Config entry storage, rewritten on every token change | Local file write | `coordinator.py:198`, `__init__.py:86` |
| Coordinator poll, every 30 minutes | Scheduled job | `coordinator.py:69`, `const.py:168` |
| Billed-history import into the recorder | Local database write | `coordinator.py:212`, `coordinator.py:257` |
| Diagnostics download | HA admin API | `diagnostics.py:14` |
| `SP_USERNAME` / `SP_PASSWORD` environment variables | CLI input | `scripts/live_api.py:28` |
| HTTPS responses from the four SP Group hosts and the Auth0 tenant | Remote input, parsed | `const.py:13`, `const.py:14`, `const.py:15`, `const.py:58` |
| CI on pull request and on push | Build-time input | `.github/workflows/ci.yml:3`, `.github/workflows/validate.yml:3` |

There is no listener, no webhook, no service call, no file upload parser, and
no message consumer in this repository. The remote bodies are parsed by the
client and mapper; both are size-capped at the transport and type-checked at
every field (`_parse_premise`, `client.py:828`).

## Trust boundaries

1. **Operator to config entry.** Credentials and the price enter through the
   config flow (`config_flow.py:33`, `config_flow.py:72`). The password reaches
   the login body (`client.py:1352`) and nothing else.
2. **Config entry to outbound request.** The stored tokens become the
   `Authorization` bearer and the `X-id-token` header (`client.py:1614`,
   `client.py:1615`, `const.py:104`). This is the only place a secret leaves
   the process.
3. **SP Group and Auth0 responses to the process.** Every JSON body is untrusted
   remote input, parsed with `json.loads` on a decoded body (`client.py:381`)
   under the size cap of R-mitigations above, with no schema validation beyond
   per-field type checks.
4. **Process to Home Assistant storage and recorder.** Tokens are written to the
   config entry, and readings to long-term statistics, which any HA user with
   database access can query (R1, R3).
5. **Repository to the operator's install.** HACS distributes this code
   (`hacs.json`), and CI runs pull-request code (`.github/workflows/ci.yml:4`).
   The component has no runtime third-party dependencies (`pyproject.toml:9`),
   so the supply chain
   is the integration files themselves.

Privilege transitions: none inside this component. It runs with the Home
Assistant process's own authority and holds no signing key, no service account,
and no SP write endpoint. Every write it performs is within the operator's own
instance.

## Assets

- Access, id, and refresh tokens, stored on the config entry (`client.py:635`).
  The refresh token outlives a restart (`README.md:141`) and rotates on each
  exchange (`client.py:1333`).
- Premise identity: address, account number, premise id, account status
  (`models.py:76`), exposed as the device name and as entity attributes.
- Financial data: billed amounts, payables, PPMS credit, EV wallet balance and
  charge receipts, written to statistics as cost series (`history.py:41`).
- The Home Assistant process itself: availability, since it is the only compute
  this component holds.

## Threats per boundary

**Operator to config entry.** Spoofing is out of scope, the operator is the
principal. Tampering applies: `.storage/core.config_entries` is a plain JSON
file, and the component reads whatever tokens and price it finds
(`__init__.py:43`, `coordinator.py:105`). A restored backup or another process
that can write that file is a store this component trusts; the price is
range-checked on read, the tokens are not, because a wrong token fails closed
at the first authenticated read. Repudiation: a credential change leaves no
local trace beyond the token values themselves.

**Config entry to outbound request.** Information disclosure is the dominant
threat: the tokens travel to the identity host on refresh and the id token
travels to the b2c host on every read. TLS with the default context
(`client.py:246`) is the only control on that path. Spoofing of the SP Group
servers is prevented by certificate verification; impersonation in the other
direction is R5.

**SP Group responses to the process.** Denial of service through oversized
bodies is capped, and request fan-out is capped. Information disclosure through
log injection is capped and cleaned (R4 is the residual). Tampering is the
stronger class here: the responses drive sensor values, entity ids, statistic
ids, and the values written to long-term statistics (R2, R7), and nothing the
upstream returns is signed or sanity-checked beyond type.

**Process to storage and recorder.** Information disclosure: every HA user and
every recorder query sees the premise address, the account number, and billed
amounts (R3). The diagnostics endpoint is the one place the design is right.

**Repository to install.** Tampering: pull-request CI runs the contributor's
code. The actions are commit-pinned and the token is `contents: read`, so the
ordinary GitHub Actions risk is reduced but not eliminated. The
`after_dependencies: recorder` declaration (`manifest.json`) is a runtime
coupling, not an attack surface.

## Named gaps

- G1: no at-rest protection of the tokens beyond the file permissions of
  `.storage`. A home-directory backup, a snapshot, or a support bundle that
  includes `core.config_entries` yields a session that re-mints itself. The
  password is no longer there; the refresh token is the credential now.
- G2: the upstream premise id reaches URL path segments, unique ids, and
  statistic ids without escaping or a character allowlist
  (`client.py:1698`, `sensor.py:69`, `history.py:60`). The host is fixed by the
  f-string prefix, so the blast radius is one host's path space and the local id
  namespace, not another server.
- G3: `AuthError.error_description` gets `_clean_text` but no length cap
  (`client.py:184`), unlike every other remote-text path in the file. The
  inconsistency is local, not a missing idea.
- G4: upstream-identity impersonation (R5).

Each gap is input for a vulnerability review, which owns the fix. This
document records them and changes no code.

## Abuse cases

- A user with any HA account on the instance reads the device name, the entity
  attributes, and the recorder database and learns the household's address,
  account number, and billed consumption (R3). There is no per-entity
  authorization in this design to lift.
- A hostile upstream returns a premise id containing path separators: the
  client requests a different path on the same authenticated host, and writes
  readings under an attacker-chosen statistic id, where they persist and are
  picked up by the energy dashboard and by database exports (R2, R7).
- A hostile upstream returns a megabyte of `error_description`: the log and the
  reauth dialog carry it, and an operator reading the dialog believes whatever
  the text says (R4).
- An operator whose entry was restored from a backup with a retired refresh
  token retries until the read is rejected, then reauths; the retry itself is
  bounded by the cooldown, so the cost is the operator's time, not a lockout.

## Not modelled here

Dependency CVEs, per-endpoint authorization behavior, and PII compliance
mapping are out of scope for this document and belong to their own reviews.
There is no `SECURITY.md` in this repository, so there are no disclosure
claims to correct; adding one means writing a process that does not yet exist.

## Review notes

The ranking above is the input for a vulnerability pass. G1 to G3 have the
concrete code references a fix can start from. Re-verify every reference before
acting on a row: this table is only as good as the commit it was read at.
