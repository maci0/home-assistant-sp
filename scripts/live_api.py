#!/usr/bin/env python3
"""Optional live login+usage against SP hosts. Secrets stay in the environment."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.sp_group.client import (  # noqa: E402
    AuthError,
    SpGroupClient,
    UsageError,
)


def main() -> int:
    username = os.environ.get("SP_USERNAME", "").strip()
    password = os.environ.get("SP_PASSWORD", "")
    if not username or not password:
        print("SP_USERNAME/SP_PASSWORD not set; skipping live call")
        return 0
    client = SpGroupClient()
    try:
        session = client.login(username, password)
        print(f"login=ok access_token_len={len(session.access_token)}")
        usage = client.fetch_usage()
        print(f"premise_id={usage.premise_id}")
        print(f"electricity_kwh={usage.electricity_kwh}")
        print(f"water_m3={usage.water_m3}")
        return 0
    except AuthError as exc:
        print(f"auth_error={exc.error}")
        return 2
    except UsageError as exc:
        print(f"usage_error={exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
