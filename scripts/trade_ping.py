#!/usr/bin/env python3
"""Trade ping: macOS notification for every shadow-engine entry signal.

Standalone journal reader -- deliberately outside the engine (the shadow
declaration pins the engine to dryrun-only execution, so notification lives
here, not in an executor). Tails var/shadow/journal/<ET-date>.jsonl and fires
`osascript display notification` for each signal_emitted with intent ENTRY
and each position_closed (with realised P&L).

Offsets are persisted per journal file so a watcher restart re-pings only
what was appended while it was down, never the whole day. On first-ever
sight of a file (no saved offset) it starts from EOF: installing the watcher
mid-session must not replay the morning's trades.

Run under launchd (com.thelab.trade-ping, KeepAlive) or by hand:
    python3 scripts/trade_ping.py [--journal-dir DIR] [--interval SECS]
    python3 scripts/trade_ping.py --test   # fire one test notification
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def notify(title: str, message: str) -> None:
    script = (
        f'display notification "{message}" '
        f'with title "{title}" sound name "Glass"'
    )
    subprocess.run(["osascript", "-e", script], check=False, timeout=10)


def _when(record: dict) -> str:
    try:
        ts = datetime.fromisoformat(record["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(ET).strftime("%H:%M:%S ET")
    except (KeyError, ValueError):
        return ""


def format_entry(record: dict) -> str:
    direction = record.get("direction", "?")
    quantity = record.get("quantity", "?")
    reason = record.get("reason") or record.get("source") or ""
    parts = [f"{direction} x{quantity}", _when(record), reason]
    return " -- ".join(str(p) for p in parts if p)


def format_exit(record: dict) -> str:
    direction = record.get("direction", "?")
    quantity = record.get("quantity", "?")
    exit_price = record.get("exit_price", "?")
    reason = record.get("exit_reason") or ""
    pnl = ""
    try:
        value = float(record["realised_pnl"])
        pnl = f"P&L {'+' if value >= 0 else '-'}${abs(value):,.2f}"
    except (KeyError, TypeError, ValueError):
        pass
    parts = [f"{direction} x{quantity} out @ {exit_price}", pnl, _when(record), reason]
    return " -- ".join(str(p) for p in parts if p)


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state))


def drain(journal: Path, offset: int) -> tuple[list[dict], int]:
    """Read complete new lines past offset; return entry records and new offset."""
    entries: list[dict] = []
    with journal.open("rb") as fh:
        fh.seek(offset)
        while True:
            line_start = fh.tell()
            line = fh.readline()
            if not line:
                break
            if not line.endswith(b"\n"):  # partial write in flight; retry next poll
                offset = line_start
                break
            offset = fh.tell()
            try:
                record = json.loads(line)
            except ValueError:
                continue
            event = record.get("event")
            if event == "signal_emitted" and record.get("intent") == "ENTRY":
                entries.append(record)
            elif event == "position_closed":
                entries.append(record)
    return entries, offset


def watch(journal_dir: Path, state_path: Path, interval: float) -> None:
    state = load_state(state_path)
    while True:
        session = datetime.now(ET).strftime("%Y-%m-%d")
        journal = journal_dir / f"{session}.jsonl"
        if journal.exists():
            key = journal.name
            if key not in state:
                # First sight of this file: start at EOF, don't replay history.
                state = {key: journal.stat().st_size}
                save_state(state_path, state)
            else:
                entries, new_offset = drain(journal, state[key])
                for record in entries:
                    if record.get("event") == "position_closed":
                        notify("TFR Shadow: trade closed", format_exit(record))
                    else:
                        notify("TFR Shadow: trade entered", format_entry(record))
                if new_offset != state[key]:
                    state = {key: new_offset}
                    save_state(state_path, state)
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-dir", type=Path,
                        default=Path(__file__).resolve().parent.parent / "var/shadow/journal")
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--test", action="store_true",
                        help="fire one test notification and exit")
    args = parser.parse_args()

    if args.test:
        notify("TFR Shadow: trade entered", "LONG x1 -- 09:41:00 ET -- test ping")
        notify("TFR Shadow: trade closed",
               "LONG x1 out @ 30250.00 -- P&L +$100.00 -- 09:42:00 ET -- test ping")
        return 0

    state_path = args.state_file or args.journal_dir / "trade_ping_state.json"
    try:
        watch(args.journal_dir, state_path, args.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
