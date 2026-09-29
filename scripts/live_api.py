#!/usr/bin/env python3
"""Optional live login+usage against SP hosts. Secrets stay in the environment."""

from __future__ import annotations

import os
import sys
from io import TextIOWrapper
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.sp_group.client import (
    AuthError,
    SpGroupClient,
    UsageError,
)


def main() -> int:
    # SP text is UTF-8 and a parse error quotes it back; on a C-locale
    # terminal the print would raise instead of reporting the failure.
    cast(TextIOWrapper, sys.stdout).reconfigure(
        encoding="utf-8", errors="backslashreplace"
    )
    username = os.environ.get("SP_USERNAME", "").strip()
    password = os.environ.get("SP_PASSWORD", "")
    if not username or not password:
        missing = [
            name
            for name, value in (("SP_USERNAME", username), ("SP_PASSWORD", password))
            if not value
        ]
        # Non-zero: a caller that set the variables expects a live call, and
        # exit 0 would read as a passing check that never reached the API.
        print(f"missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return 1
    client = SpGroupClient()
    try:
        session = client.login(username, password)
        print(f"login=ok access_token_len={len(session.access_token)}")
        usage = client.fetch_usage()
        # The premise id is the account number. It identifies the household, and
        # this output is what gets pasted into a bug report.
        print(f"has_premise_id={bool(usage.premise_id)}")
        print(f"electricity_kwh={usage.electricity_kwh}")
        print(f"water_m3={usage.water_m3}")
        return 0
    except AuthError as exc:
        print(f"auth_error={exc.error}", file=sys.stderr)
        return 2
    except UsageError as exc:
        print(f"usage_error={exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
