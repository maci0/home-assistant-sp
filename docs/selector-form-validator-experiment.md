# Experiment: native HA selectors with server-side validation

## Design

`form_fields.ValidatedTextSelector` derives from HA's generic `Selector` base,
uses the native text selector's configuration schema and serializes as a
normal text selector. Its server-side `__call__` executes the supplied rule,
converting `ValueError` to `vol.Invalid`.

Both reviewed HA custom serializers recognize Selector instances before the
underlying serializer's normal dispatch. They call `serialize()` without
inspecting the stored rule. No frontend registration, custom widget, Length
subclass, Coerce workaround, global monkey-patch or version branch is needed.

`config_flow.py` uses the helper for username and price fields. Username type
and UTF-8 byte limits remain in the submitted schema. Price validation and
saved representations remain unchanged from v1.8.0: numeric and text inputs
are accepted, blank/whitespace becomes `""`, and zero becomes `0.0`. The existing
reader treats those as disabled. Unrelated options are preserved. The price's
suggested value is formatted as text, or an empty string when unset, to match
the frontend input while retaining numeric API input acceptance server-side.

This replaces the initial handler-validation implementation in the fork's
review PR. It is not deployed: the live installation still uses the original
handler-validation hotfix. It does not address the pre-existing reauth username
prefill, MFA identity, token persistence or statistics issues.

## Verification — 2026-11-13

- Ruff lint/format and mypy passed (31 checked source files).
- Lightweight pytest: **280 passed, 4 skipped**. The new cases need an HA runtime.
- All four runtime unittest cases passed with candidate code loaded only in
  memory in a separate process inside **HA 2026.11.0 / probatio 0.13.0**.
- The same four cases passed with the **voluptuous-serialize 2.7.0 converter**
  and the historical HA Selector hook, using the current HA classes/runtime.
- An independent local check used **real voluptuous 0.15.2**, **real
  voluptuous-serialize 2.7.0**, source-extracted **HA 2026.8.3 Selector/TextSelector
  classes**, the candidate helper, and the candidate's actual schema expressions.
  Serialization and valid/invalid username and price cases passed. Only the
  selector registry decorator was omitted: direct instantiation of a native
  text field does not need registration. This was **not a full old-HA boot**.

Runtime regression coverage:

- Setup, reauth, reconfigure, MFA and options serialization.
- Username type, empty input, UTF-8 byte bounds and invalid Unicode; password
  length bounds.
- Native frontend text-selector reconstruction: its value stays unchanged,
  while the original server-side field still runs the custom rule.
- Options with/without a saved price, raw numeric and numeric text values,
  whitespace, zero, invalid text, negative/nonfinite values, bool/None rejection,
  omitted option clearing and preservation of unrelated options.

No real SP authentication was attempted. The tests do not exercise a browser
or the full HTTP flow endpoint. The candidate's four runtime tests can run in
an actual supported legacy HA environment as the next compatibility gate:

```sh
python -m unittest tests.test_config_flow -v
```

The tests select the serializer referenced by HA's config-validation module,
not by whether another serializer package happens to be installed.

## Assessment

This candidate is more promising than Coerce-only: both serializer paths
recognize the field through HA's existing Selector hook. It retains domain
validation inside the schema without implying that a price rule is a length
rule, and avoids dual legacy/modern flow implementations.

Keep the fork review PR in draft until actual legacy HA and browser submission
checks pass. The standalone experiment is also retained on
`fix/selector-form-validators`. A version-specific compatibility adapter is not
currently needed by the implementation; introduce one only if the test matrix
demonstrates a concrete incompatibility.
