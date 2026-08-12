# NQ Agent — operations runbook

Everything needed to run this thing, in the order you need it. If you are
reading this to decide whether to go live, read **Go-live checklist** last and
do not skip the items marked **BLOCKING**.

---

## What this system does and does not do

It runs one strategy over one instrument, converts its signals into orders,
fans them out to several accounts, refuses to trade when a risk rule says no,
and writes down everything it did.

It does **not** know whether your strategy makes money. Nothing in this repo
has ever placed a real order or received a real market tick.

---

## Everyday commands

```bash
# Replay a fixture through the whole pipeline
uv run python -m nq_agent --config config/paper.yaml --replay tests/fixtures/2026-07-15.jsonl

# Simulate a mid-session crash, then resume
uv run python -m nq_agent --config config/paper.yaml --replay tests/fixtures/2026-07-15.jsonl --max-bars 60
uv run python -m nq_agent --config config/paper.yaml --replay tests/fixtures/2026-07-15.jsonl

# Backtest and print a P&L report
uv run python -m nq_agent.backtest --config config/paper.yaml \
    --fixture tests/fixtures/*.jsonl --strategy orb --out var/backtest

# Live (needs NQ_DATABENTO_API_KEY; no --replay)
uv run python -m nq_agent --config config/live.yaml
```

Tests, lint, types:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy --strict src/
```

---

## The kill switch

Create the file. That is the whole interface.

```bash
touch var/nq-agent.halt      # stop opening new positions
rm var/nq-agent.halt         # resume
```

It is checked before every entry and never cached, so it takes effect on the
next bar without a restart. **It vetoes entries only.** A FLATTEN always goes
through, deliberately: a kill switch that traps you in an open position is
worse than no kill switch.

It does **not** close an open position. If you need out now, flatten in the
broker's own platform and then create the halt file so the agent does not
re-enter.

Put this behind something you can reach from your phone. An SSH one-liner or a
Tailscale-reachable box is enough; the point is that the stop button must not
require you to be at your desk.

---

## Risk limits, and why the live config refuses to start without them

Four controls exist. Two count events, two count money.

| Control | Config key | Scope |
|---|---|---|
| Max trades per session | `risk.max_trades_per_day` | resets daily |
| Session cutoff | `session.cutoff` | daily |
| Daily loss limit | `risk.max_daily_loss` | resets daily |
| Trailing drawdown | `risk.max_trailing_drawdown` | **life of the account** |

Both money limits count the **open position**, marked to market on every bar,
not just closed trades. A position down $800 counts against the daily limit
immediately — which is what the firm is doing too.

`risk.trailing_drawdown_basis` decides how the high-water mark moves, and firms
genuinely differ. Get it wrong and your limit and theirs measure different
things:

- `equity` (default) — an unrealised peak raises the mark. Up $1,000 on an open
  trade and giving $600 back is a $600 drawdown, even though nothing was
  realised and the day is still green. Most intraday-trailing firms work this way.
- `closed` — only realised balance raises the mark. Open losses still count
  against it; open profits do not raise it.

**A breach on an open position emits a protective FLATTEN** (journaled as
`risk_flatten`) and halts further entries. Vetoing entries is the right response
when you are flat and no response at all when the position is already on.

The money limits are opt-in and default to null. A live config that omits them
would start happily with no loss limit at all, so `check_live_safety` refuses
to run any enabled `webhook` executor unless both are set. Set them **below**
your prop firm's own thresholds — the agent stopping first is the entire point.

Trailing drawdown is measured from the account's high-water mark and does not
reset overnight, because the firm is not resetting it either. Up 1000 then back
to 600 is a 400 drawdown even though the day is green.

**Known limitation:** the open position is marked on **bar closes**. A spike
that breaches the limit and retraces inside a single bar is never seen, and the
firm's own monitoring is tick-by-tick. Size positions so a single stop-out
cannot cross the limit on its own — that remains the real protection.

---

## Deployment

This machine is not a trading host. Before live money:

1. **A VPS near the exchange.** Chicago-area for CME. Latency matters less
   than uptime here, but a laptop that sleeps is disqualifying.
2. **Process supervision with restart.** systemd with `Restart=always` and
   `RestartSec=10`. The agent is built to resume: it persists state after every
   bar and re-adopts the session it was in. A crash loop is survivable; a
   process that stays dead is not.
3. **NTP.** Every session boundary, cutoff and bar timestamp is computed from
   the clock. Drift silently shifts the session window.
4. **Disk monitoring on `data_dir`.** A full disk means `state.db` writes fail,
   and a failed state write is re-raised deliberately rather than swallowed —
   the process dies rather than trading on state it could not persist.
5. **Log rotation.** One journal file per session date, indefinitely.

Example unit:

```ini
[Unit]
Description=NQ Agent
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/nq-agent
EnvironmentFile=/opt/nq-agent/.env
ExecStart=/opt/nq-agent/.venv/bin/python -m nq_agent --config config/live.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## Reading the journal

One JSON object per line, one file per session date, at
`{data_dir}/journal/{date}.jsonl`.

```bash
# What happened today
jq -r '"\(.ts) \(.event)"' var/journal/2026-07-15.jsonl

# Every trade with its P&L
jq -c 'select(.event=="position_closed")' var/journal/2026-07-15.jsonl

# Anything the risk layer refused, and why
jq -c 'select(.event=="risk_veto") | {reason, detail}' var/journal/2026-07-15.jsonl

# Orders that may or may not have filled -- see below
jq -c 'select(.event=="order_result" and .outcome=="UNKNOWN")' var/journal/*.jsonl
```

Events: `session_start`, `session_resumed`, `session_end`, `bar_gap`,
`feed_error`, `signal_emitted`, `signal_suppressed_backfill`, `risk_veto`,
`order_result`, `position_opened`, `position_open_rejected`, `position_closed`,
`backfill_skipped`, `strategy_error`, `executor_alert`.

---

## Reconciliation: agent belief vs broker truth

`PositionTracker` is a simulation — it opens a position because a signal was
*dispatched*, not because a fill was *confirmed*. Reconciliation is what closes
that gap. It is **opt-in**: without a `PositionSource` wired, nothing changes
and every replay and backtest behaves as before.

It runs at three moments:

| Trigger | Why |
|---|---|
| Startup, on a resumed session | The agent just restored a belief from its own database and has not checked it |
| Any `UNKNOWN` order outcome | The belief may have diverged in the last second |
| Every N bars (`risk.reconcile_interval_bars`, 0 = off) | Catches a broker-side stop or margin close the agent never saw |

What it does with the answer:

| Situation | Response |
|---|---|
| Agreement | Journal `reconciliation_ok`, carry on |
| Different fill price | Adopt the broker's price so P&L is measured against what was really paid. Not blocking — slippage is expected |
| Broker flat, agent holds | Drop the phantom, block entries, alert |
| Broker holds, agent flat | **Adopt it** so the cutoff flatten can close it, block entries, alert |
| Quantity or direction mismatch | Block entries, alert. No guessing |
| Query failed | Block entries. Not knowing carries the same risk as knowing it is wrong |

Blocking means `RECONCILIATION_REQUIRED` vetoes new **entries**. FLATTEN always
goes through, same rule as the kill switch. The block clears only when a later
pass actually agrees — never on a timer.

An adopted position carries **no stop and no target**, because the broker
reports what is held, never what the exit was meant to be. It cannot be stopped
out; the cutoff flatten is what closes it. That is deliberate — a fabricated
stop would exit at a price nobody chose.

**To wire it up**, implement `PositionSource.fetch_positions()` against your
broker's position endpoint and pass it to `run_from_config`. `StaticPositionSource`
exists for tests. This is the last vendor-specific piece, alongside
`_build_payload`.

---

## Incident: an order came back UNKNOWN

`outcome: "UNKNOWN"` means the agent does not know whether the broker filled
it. A timeout, a 5xx, or a connection reset mid-POST all produce this. It is
**not** a rejection.

If a `PositionSource` is wired, the agent has already blocked its own entries
and attempted a check — look for `reconciliation_divergence` in the journal.
If one is not wired, **do not let the agent keep trading that account until you
have reconciled it by hand.**

1. Open the broker's platform and read the actual position.
2. Compare against `var/state.db` — the agent's belief:
   ```bash
   sqlite3 var/state.db "select payload from session_state"
   ```
3. If they disagree, the broker is right. Flatten manually if you did not want
   the position.
4. Create the kill switch before doing any of this, so no new entry lands
   mid-reconciliation.

There is no automated reconciliation. `PositionTracker` is a *simulation* —
it opens a position because a signal was dispatched, not because a fill was
confirmed. Closing that gap is the largest single piece of unbuilt work.

---

## Incident: the feed dropped

`ReconnectingFeed` retries with exponential backoff (1s, 2s, 4s… capped at 60s,
5 attempts by default) and resumes from the last bar it delivered, not from the
start of the session. Each drop writes `feed_error`.

If it exhausts its attempts it raises, teardown runs, and the process exits —
which is why supervision matters. On restart the agent reloads the session,
replays the downtime window through the strategy in warmup, and dispatches
nothing from it.

If a position was open when the feed died, the agent cannot see stops being hit.
Check the broker.

---

## Incident: the strategy raised

Journalled as `strategy_error`, and the session is marked halted: no further
`on_bar` calls, but the loop keeps running so the cutoff flatten and
`session_end` still happen. `is_halted` is persisted, so a restart does not
silently un-halt it. Fix the bug, then clear state for that session date or
wait for the next one.

---

## Go-live checklist

**BLOCKING — do not skip:**

- [ ] A strategy exists that you have reason to believe has an edge. `orb` is a
      reference implementation, explicitly not a validated one.
- [ ] It has been backtested over **months** of real data, not one fixture, and
      the backtest was read with its stated caveats in mind (no slippage, no
      spread, closed-trade drawdown only).
- [ ] Commission is set to your broker's real round-turn cost, and the backtest
      is still profitable with it.
- [ ] `contract.point_value` matches the instrument (20 for NQ, 2 for MNQ).
- [ ] `risk.max_daily_loss` and `risk.max_trailing_drawdown` are set below the
      prop firm's own limits.
- [ ] It has run in **shadow mode** — real feed, dry-run executors — for at
      least several weeks, and you have read those journals.
- [ ] The webhook payload in `execution/webhook.py::_build_payload` has been
      checked against the vendor's documentation and tested against their
      paper endpoint.
- [ ] A whole-branch review has been run since the last change. The previous
      one found three Critical defects that 185 passing tests missed.

**Then, and only then:**

- [ ] One account. One micro contract. Smallest size the firm allows.
- [ ] A fixed evaluation window agreed in advance, and a number at which you
      stop regardless of how you feel on the day.
- [ ] Compare live fills against what the backtest predicted for the same
      sessions. Divergence there is your slippage estimate, and it is the
      number that decides whether the edge survives contact.

---

## Things known to be missing

Listed so nothing here is a surprise later.

- **Reconciliation has no broker adapter.** The layer is built and tested, but
  `PositionSource` needs an implementation against your broker's position
  endpoint before it does anything live.
- **Unrealised P&L is marked on bar closes only**, not tick by tick, so an
  intra-bar spike through a limit is invisible to it.
- **Contract roll.** `NQ.c.0` is volume-based continuous, so the roll happens
  mid-session. Do not hold a position through one.
- **Multi-day replay resume.** A replay killed and restarted resumes the
  fixture's *first* session, not the one it was in. Live is unaffected (its
  anchor is `now`); backtests are unaffected (they use a fresh state dir).
- **No second whole-branch review** since the resume-window fix wave.
- **`DatabentoFeed` has never connected to Databento.** The SDK surface is
  verified against the installed package; the behaviour is not.
