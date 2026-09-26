#!/usr/bin/env python3
"""Fresh consumer of the shipped SP Group client against recorded fixtures."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.sp_group.client import SpGroupClient  # noqa: E402
from tests.conftest import FixtureTransport  # noqa: E402


def main() -> None:
    client = SpGroupClient(transport=FixtureTransport())
    client.login("user@example.com", "secret")
    usage = client.fetch_usage()
    print(f"electricity_kwh={usage.electricity_kwh}")
    print(f"water_m3={usage.water_m3}")
    print(f"premise_id={usage.premise_id}")
    print(
        json.dumps(
            {"electricity_kwh": usage.electricity_kwh, "water_m3": usage.water_m3}
        )
    )


if __name__ == "__main__":
    main()
