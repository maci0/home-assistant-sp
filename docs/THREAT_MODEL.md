# SP Group integration: threat model

Scope: the `sp_group` Home Assistant custom component, its two developer
scripts, and its CI. Every entry point, boundary, and mitigation below is
referenced to the code that implements it, so a later pass can re-verify it.

What this component is: a cloud-polling client. It opens no listening socket,
registers no service or event, and exposes no HTTP endpoint of its own. All
inbound data comes from Home Assistant (the operator typing into a config
flow) and all outbound data comes from three SP Group hosts over HTTPS. The
attacker positions that matter are therefore: someone who can reach the SP
Group or Auth0 hosts, and someone who can read the Home Assistant process or
its storage directory.

Last reviewed: 2026-09-29.

## Risk-ranked summary

| # | Risk | Impact | Exploitability | Status |
|---|------|--------|----------------|--------|
| R1 | The SP e-account password is persisted in cleartext on the config entry (`client.py:371`, `coordinator.py:125`) and re-sent on every reauth | Full account takeover of the user's utility account | Low: needs read access to the HA process or a `.storage` copy | Unmitigated by design, see G1 |
| R2 | Response bodies are read without a size limit (`client.py:170`, `client.py:173`) | Memory exhaustion of the Home Assistant process, so every integration goes down | Low: needs a hostile or compromised upstream host | G2 |
| R3 | Upstream-controlled error text reaches the HA log and the user-facing error dialog (`client.py:231`, `const.py:142`, `__init__.py:57`) | Log forging, terminal escape sequences in a log viewer, misleading the operator into a wrong credential action | Low: needs a hostile or compromised upstream host | G3 |
| R4 | Password logins are retried with no backoff (`config_flow.py:47`, `coordinator.py:101`) | Auth0 bot detection locks the account out (`README.md:134`), a self-inflicted denial of service | Medium: triggered by ordinary use, reauth loops, or a hostile upstream | G4 |
| R5 | The client presents itself as the official Android app (`const.py:88`) and ships a hardcoded public client id (`const.py:40`) | Upstream policy or detection treats the account as abuse; a client-id change breaks every install at once | Medium: a change upstream, not an attack | G5 |
| R6 | Premise address, account number, bill amounts, and EV charge history are exposed as entity attributes and recorder statistics to every HA user of the instance (`sensor.py:114`, `coordinator.py:197`) | Household occupancy, consumption pattern, and billing data leak to any other user of the HA instance, including a guest user | Medium: any HA account is enough | Accepted; the integration has no per-entity authorization to add |
| R7 | The paired-FCU read issues one request per `thingName` the upstream returns (`client.py:1460`) | A long list of FCU names stalls the poll executor for the sum of the per-request timeouts | Low: needs a hostile or compromised upstream | G6 |

Mitigations that are genuinely in the code, verified against the claim:
TLS certificate verification uses the stdlib default context
(`client.py:166`), so no host is reachable without a valid chain. Diagnostics
omit the password and every token (`diagnostics.py:21`); the README claim at
`README.md:125` is accurate. No credential, token, or response body is passed
to `print` or the logger by path; the only credential-echoing script reads
`SP_USERNAME`/`SP_PASSWORD` from the environment rather than argv
(`scripts/live_api.py:21`).

## Entry points

| Entry point | Kind | Reference |
|---|---|---|
| Config flow: username, password | HA operator input | `config_flow.py:19` |
| Config flow: MFA code (TOTP or out-of-band) | HA operator input | `config_flow.py:102` |
| Reauth and reconfigure flows | HA operator input | `config_flow.py:208`, `config_flow.py:222` |
| Options flow: electricity price | HA operator input | `config_flow.py:64` |
| Config entry storage, rewritten on every token change | Local file write | `coordinator.py:112` |
| Coordinator poll, every 30 minutes | Scheduled job | `coordinator.py:53`, `const.py:87` |
| Diagnostics download | HA admin API | `diagnostics.py:14` |
| Recorder statistics writes | Local database write | `coordinator.py:197` |
| `SP_USERNAME` / `SP_PASSWORD` environment variables | CLI input | `scripts/live_api.py:21` |
| HTTPS responses from `identity.spdigital.sg`, `b2c.api.spdigital.sg`, `public.api.spdigital.sg`, `identity.spdigital.auth0.com` | Remote input, parsed | `client.py:165`, `const.py:8` |
| CI on pull request | Build-time input | `.github/workflows/ci.yml:5` |

There is no listener, no webhook, no service call, no file upload parser, and
no message consumer in this repository. Everything in the last two inbound
categories is parsed by the client and mapper, and neither is defensive about
size (R2) but both are type-checked at every field
(`_select_premise`, `client.py`, and the parsers it calls).

## Trust boundaries

1. **Operator to config entry.** Credentials and prices enter through the
   config flow, validated only as a voluptuous schema of `str` and `float`
   (`config_flow.py:19`). The price is coerced and range-checked
   (`config_flow.py:81`); the credential fields are not length- or
   character-checked.
2. **Config entry to outbound request.** The stored password and tokens
   become request bodies and headers (`client.py:1016`, `client.py:1031`).
   This is the only place a secret leaves the process.
3. **SP Group and Auth0 responses to the process.** Every JSON body is
   untrusted remote input, parsed with `json.loads` on a decoded body
   (`client.py:196`) with no schema validation and no size cap.
4. **Process to Home Assistant storage and recorder.** Tokens and the
   password are written to the config entry, and readings are written to
   long-term statistics, which any HA user with database access can query.
5. **Repository to the operator's install.** HACS distributes this code
   (`hacs.json`), and CI runs untrusted pull-request code with
   `uv sync --extra dev --frozen` (`.github/workflows/ci.yml:14`). The
   component has no runtime third-party dependencies (`pyproject.toml:7`),
   so the supply chain is the integration files themselves.

Privilege transitions: none inside this component. It runs with the
Home Assistant process's own authority and holds no signing key, no service
account, and no SP write endpoint. Every write it performs (statistics,
config entry update) is within the operator's own instance.

## Assets

- SP Group e-account password, long lived and MFA-gated upstream (highest value).
- Access, id, and refresh tokens, all stored on the config entry
  (`client.py:371`); the refresh token outlives a restart (`README.md:125`).
- Premise identity: address, account number, premise id, account status
  (`models.py`, exposed via `sensor.py:114`).
- Financial data: billed amounts, payables, PPMS credit, EV wallet balance
  and charge receipts (`coordinator.py:197` writes them to statistics).
- The Home Assistant process itself: availability, since it is the only
  compute this component holds (R2, R7).

## Threats per boundary

**Operator to config entry.** Spoofing is out of scope (the operator is the
principal). Tampering applies: `.storage/core.config_entries` is a plain JSON
file, and the integration reads whatever password and tokens it finds there on
setup (`__init__.py:39`), re-logging in with it. A file that another process
or a restored backup can edit is the store this component trusts
unconditionally. Repudiation: nothing the operator does at the config flow is
recorded by this component, so a credential change leaves no local trace.

**Config entry to outbound request.** Information disclosure is the dominant
threat: the password and both tokens travel to the identity host on every
login and refresh, and the id token travels to the b2c host as the
`X-id-token` header (`const.py:86`). TLS with the default context
(`client.py:166`) is the only control on that path. Spoofing of the SP Group
servers is prevented by certificate verification; impersonation in the other
direction is R5.

**SP Group responses to the process.** Information disclosure through log
injection (R3) and denial of service through oversized bodies (R2) and
request fan-out (R7). Tampering is limited: the responses feed sensor values
and statistics, so a hostile upstream can write false meter readings into the
long-term statistics database (`coordinator.py:197`), and those become
permanent history that later exports trust. The client does not sign or
verify anything the upstream returns beyond TLS.

**Process to storage and recorder.** Information disclosure: every HA user
and every recorder query sees premise address, account number, and billed
amounts (R6). The diagnostics endpoint is the one place the design is right
(`diagnostics.py:21`).

**Repository to install.** Tampering: pull-request CI runs the contributor's
code with repository token access, which is the ordinary GitHub Actions risk,
not a property of this component. The `after_dependencies: recorder`
declaration (`manifest.json`) is a runtime coupling, not an attack surface.

## Named gaps

- G1: no at-rest protection of the persisted password or refresh token beyond
  the file permissions of `.storage`. A home-directory backup, a snapshot, or
  a support bundle that includes `core.config_entries` yields a live,
  MFA-gated utility credential.
- G2: `response.read()` and `exc.read()` (`client.py:170`, `client.py:173`)
  are unbounded.
- G3: `error_description` from the identity host is not truncated before it
  becomes a log line and a translation placeholder (`client.py:231`). The
  codebase does bound hostile values elsewhere (`ERROR_VALUE_CHARS`,
  `const.py:85`), so the inconsistency is local, not a missing idea.
- G4: no delay or cap between password-login attempts.
- G5: upstream-identity impersonation (R5).
- G6: unbounded per-`thingName` FCU fan-out (`client.py:1460`).

Each gap is a finding for a vulnerability review to fix, with a test. This
document records them; it does not change code.

## Abuse cases

- A user with any HA account on the instance reads `sp_group` attributes and
  the recorder database and learns the household's address, account number,
  and billed consumption (`sensor.py:114`, `coordinator.py:197`). There is no
  per-entity authorization in this design to lift.
- A hostile upstream returns a four-megabyte body or eight hundred FCU names;
  the poll stalls or the process grows, and no integration on the instance
  works while it does (R2, R7).
- A hostile upstream returns an `error_description` that makes the operator
  believe the account is locked, sending them into a reauth loop that in turn
  drives Auth0 bot detection (R3 feeding R4).
- An operator restores a backup of `.storage` containing a stale refresh
  token; the client silently retries the stored password until the account is
  flagged (`__init__.py:39`, R4).

## Not modelled here

Dependency CVEs, per-endpoint authorization behavior, and PII compliance
mapping are out of scope for this document and belong to their own reviews.
There is no `SECURITY.md` in this repository, so there are no disclosure
claims to correct; adding one means writing a process that does not yet exist.

## Review notes

The risk ranking above is the input for a vulnerability review pass. Gaps G1
through G4 are the ones with code references a fix can start from. Re-verify
every reference before acting on a row: this table is only as good as the
commit it was read at.
