"""TFR shadow-forward session runner (declaration: TFR-SHADOW-DECLARATION.md).

Wires the production engine for live-paper: Databento live trades ->
tick tap -> FlowEngine (the SAME code the research pipeline used) ->
decision book -> TickFlowRegime fc_t13 (parameters frozen by the
declaration) -> dryrun executor. Persists the session's flow file
afterwards so the nightly calibration and the drift audit have the record.

Run daily before the 09:30 ET open (see RUNBOOK):
  uv run python scripts/run_shadow.py
Nightly after the close:
  uv run python scripts/calibrate_flow.py --flow var/flow --out var/calibration.json
  uv run python scripts/audit_drift.py --ticks <recorded> --decisions <recomputed> ...
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nq_agent.clock import RealClock
from nq_agent.config import load_settings
from nq_agent.feed.databento import DatabentoFeed
from nq_agent.feed.reconnecting import ReconnectingFeed
from nq_agent.flow import FlowEngine
from nq_agent.main import FeedBinding, run_from_config
from nq_agent.strategy.tfr import TickFlowRegime

CONFIG = Path("config/shadow.yaml")
CALIBRATION = Path("var/calibration.json")
FOMC = Path("config/fomc_dates.json")
FLOW_STORE = Path("var/flow")


def fomc_dates() -> set[date]:
    payload = json.loads(FOMC.read_text(encoding="utf-8"))
    return {date.fromisoformat(d) for d in payload["dates"]}


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings(CONFIG)
    if not settings.databento_api_key:
        raise SystemExit("NQ_DATABENTO_API_KEY is not set (.env)")

    calibration: dict[str, Any] = json.loads(CALIBRATION.read_text(encoding="utf-8"))
    book: dict[str, Any] = {}
    engine = FlowEngine(calibration, book)

    feed = ReconnectingFeed(
        DatabentoFeed(
            api_key=settings.databento_api_key,
            symbol=settings.databento_symbol,
            dataset=settings.databento_dataset,
            tick_tap=engine.on_tick,
        ),
        max_attempts=settings.feed.max_reconnect_attempts,
        initial_backoff=settings.feed.initial_backoff_seconds,
        max_backoff=settings.feed.max_backoff_seconds,
    )
    clock = RealClock()
    now = clock.now()
    binding = FeedBinding(feed=feed, clock=clock, anchor=now, backfill_until=now)

    # The declared variant, exactly. Changing anything here between the
    # declaration commit and the end of the shadow window voids the gate.
    strategy = TickFlowRegime(
        decisions=book,
        exit_mode="t13",
        regime_required=False,
        fomc_dates=fomc_dates(),
    )

    # The live stream never ends on its own; the session does. Stop at
    # 16:35 ET (cutoff flatten is 16:30, strategy flat since 15:55) so the
    # flow record persists and tomorrow's calibration can run.
    end_et = datetime.now(ZoneInfo("America/New_York")).replace(
        hour=16, minute=35, second=0, microsecond=0
    )
    remaining = (end_et - datetime.now(ZoneInfo("America/New_York"))).total_seconds()
    if remaining <= 0:
        raise SystemExit("started after 16:35 ET; nothing left of today's session")
    try:
        await asyncio.wait_for(
            run_from_config(
                CONFIG, None, "tfr", None, strategy_override=strategy, binding=binding
            ),
            timeout=remaining,
        )
    except TimeoutError:
        logging.info("16:35 ET: session over, stopping the stream")
    finally:
        # Persist the session's flow record for calibration + drift audit.
        payload = engine.aggregator.session_payload()
        if payload["minutes"]:
            session = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
            FLOW_STORE.mkdir(parents=True, exist_ok=True)
            out = FLOW_STORE / f"{session.isoformat()}.json"
            out.write_text(json.dumps(payload), encoding="utf-8")
            print(f"flow record persisted: {out}")
            from nq_agent.flow import compute_calibration

            CALIBRATION.write_text(
                json.dumps(compute_calibration(FLOW_STORE), indent=2), encoding="utf-8"
            )
            print(f"calibration refreshed for the next session: {CALIBRATION}")


if __name__ == "__main__":
    asyncio.run(main())
