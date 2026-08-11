"""Generate a deterministic synthetic trading day of NQ ticks.

Run: uv run python tests/fixtures/make_fixture.py
Deterministic by seed, so the committed fixture is reproducible.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

SESSION_OPEN = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)  # 09:30 America/New_York
SESSION_MINUTES = 420  # 09:30 to 16:30
TICKS_PER_MINUTE = 12
TICK_SIZE = Decimal("0.25")


def main() -> None:
    rng = random.Random(20260715)
    price = Decimal("20100.00")
    out = Path(__file__).parent / "2026-07-15.jsonl"

    with out.open("w", encoding="utf-8") as handle:
        for minute in range(SESSION_MINUTES):
            for slot in range(TICKS_PER_MINUTE):
                ts = SESSION_OPEN + timedelta(
                    minutes=minute, seconds=slot * (60 // TICKS_PER_MINUTE)
                )
                price += TICK_SIZE * rng.randint(-4, 4)
                handle.write(
                    json.dumps(
                        {
                            "ts": ts.isoformat(),
                            "price": str(price),
                            "size": rng.randint(1, 5),
                        }
                    )
                    + "\n"
                )

    print(f"wrote {out}")


if __name__ == "__main__":
    main()
