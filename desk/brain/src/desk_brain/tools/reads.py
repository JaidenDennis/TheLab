"""Deterministic composite scorecards (spec §13 + addendum A15): level_read,
day_read, manage_read.

Pure functions — the LLM never computes factor values itself. Every factor is
returned with its tag from factors.yaml; the validated three cite studies,
everything else is labeled discretionary framework. The heuristics for the
lean are themselves framework: what is validated is each *input*, per its tag.

The extra signal inputs (tape doc, footprint cells, daily aggregates, plan,
journal rows) are optional keywords: when a store or table is unavailable the
read degrades to the original core rather than failing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .. import redis_keys as rk
from ..factors import Factor
from . import ToolContext, tool
from .market import read_vbp_minutes
from .signals import behavior as behavior_sig
from .signals import flow as flow_sig
from .signals import footprint as fp_sig
from .signals import journalstats as journal_sig
from .signals import levels as levels_sig
from .signals import manage as manage_sig
from .signals import profile as profile_sig
from .signals import session as session_sig
from .signals import vwap as vwap_sig

ET = ZoneInfo("America/New_York")
MIN_STOP_PTS = 15.0  # MAE-1 minimum viable stop
Q_CONFIRM = 90.0  # fc_t13 confirm threshold
FP_LOOKBACK_MIN = 30  # footprint window the reads consider


def _f(factors: dict[str, Factor], key: str, value: Any, note: str | None = None) -> dict[str, Any]:
    fac = factors.get(key)
    return {
        "key": key,
        "name": fac.name if fac else key,
        "tag": fac.tag if fac else "discretionary",
        "study": fac.study if fac else None,
        "value": value,
        "note": note,
    }


def _latest_flow(bars_5m: list[dict]) -> tuple[float | None, float | None]:
    """(f1_5, impulse_q) of the last closed 5m bar."""
    if not bars_5m:
        return None, None
    b = bars_5m[-1]
    return b.get("impulse"), b.get("impulse_q")


def _p(params: dict | None, section: str, key: str, default: Any) -> Any:
    return ((params or {}).get(section) or {}).get(key, default)


# -- level_read -------------------------------------------------------------


def compute_level_read(
    price: float,
    market: dict | None,
    bars_5m: list[dict],
    levels: dict | None,
    regime: dict | None,
    factors: dict[str, Factor],
    *,
    tape: dict | None = None,
    fp_minutes: list[dict[float, dict[str, float]]] | None = None,
    journal_trades: list[dict] | None = None,
    plan: dict | None = None,
    params: dict | None = None,
) -> dict[str, Any]:
    out_factors: list[dict[str, Any]] = []

    f1, q = _latest_flow(bars_5m)
    flow_dir = None if not f1 else ("long" if f1 > 0 else "short")
    confirmed = q is not None and q >= Q_CONFIRM
    out_factors.append(
        _f(
            factors,
            "flow_impulse_qrank",
            {"f1_5": f1, "q": q, "direction": flow_dir},
            f"needs Q{Q_CONFIRM:.0f}+ to confirm" if not confirmed else "confirming",
        )
    )

    gamma_sign = (regime or {}).get("gamma_sign")
    out_factors.append(
        _f(
            factors,
            "positive_gamma_penalty",
            gamma_sign,
            "EV-negative for breakout-style entries in all sightings" if gamma_sign == "POS" else None,
        )
    )

    out_factors.append(
        _f(
            factors,
            "stop_viability",
            {
                "min_stop_pts": MIN_STOP_PTS,
                "long_stop": round(price - MIN_STOP_PTS, 2),
                "short_stop": round(price + MIN_STOP_PTS, 2),
            },
            "stops tighter than 15 pts are inside noise per MAE-1",
        )
    )

    near_pts = float(_p(params, "levels", "near_level_pts", 20))
    near: list[dict[str, Any]] = []
    for lv in (levels or {}).get("levels", []):
        if lv.get("price") is None:
            continue
        dist = abs(float(lv["price"]) - price)
        if dist <= near_pts:
            near.append({**lv, "distance_pts": round(dist, 2)})
    near.sort(key=lambda x: x["distance_pts"])
    out_factors.append(_f(factors, "level_hierarchy", near[:5] or f"no tracked level within {near_pts:g} pts"))
    if near:
        conf = levels_sig.confluence(
            (levels or {}).get("levels", []), price, float(_p(params, "levels", "confluence_band_pts", 3.0))
        )
        if conf["count"] >= 2:
            out_factors.append(_f(factors, "level_hierarchy", conf, "level confluence at this price"))

    va = (market or {}).get("prior_value_area") or {}
    va_pos = None
    if va.get("vah") is not None and va.get("val") is not None:
        va_pos = "above VAH" if price > va["vah"] else "below VAL" if price < va["val"] else "inside value"
    out_factors.append(_f(factors, "value_area_position", va_pos))

    # -- addendum signals (all discretionary/descriptive context) -----------

    fp_note = None
    if fp_minutes:
        band = 10.0
        cells = {
            p: c
            for p, c in fp_sig.merge_cells(fp_minutes).items()
            if abs(p - price) <= band
        }
        if cells:
            ratio = float(_p(params, "footprint", "diagonal_imbalance_ratio", 3.0))
            tick = float(_p(params, "footprint", "tick", 0.25))
            stacks = fp_sig.stacked_imbalances(
                fp_sig.diagonal_imbalances(cells, ratio, tick),
                int(_p(params, "footprint", "stacked_imbalance_min", 3)),
                tick,
            )
            total = sum(c["buy"] + c["sell"] for c in cells.values())
            delta = sum(c["buy"] - c["sell"] for c in cells.values())
            absorption_score = 1.0 - (abs(delta) / total) if total > 0 else None
            fp_note = {
                "band_pts": band,
                "volume": round(total),
                "delta": round(delta),
                "absorption_score": round(absorption_score, 3) if absorption_score is not None else None,
                "imbalance_stacks": stacks,
                "auction": fp_sig.auction_state(cells),
            }
            out_factors.append(_f(factors, "value_area_position", fp_note, "footprint at the level (last 30m)"))

    if tape:
        ab = tape.get("absorption_at_last")
        if ab and abs((ab.get("center") or price) - price) <= 3:
            out_factors.append(
                _f(factors, "day_type_classifier", ab, "live absorption at this price (tick window)")
            )
        out_factors.append(
            _f(
                factors,
                "day_type_classifier",
                {
                    "speed_per_s": tape.get("speed_per_s"),
                    "speed_spike": tape.get("speed_spike"),
                    "delta_rate": tape.get("delta_rate"),
                    "print_clusters": tape.get("print_clusters"),
                },
                "tape context",
            )
        )

    stats = None
    if journal_trades is not None:
        stats = journal_sig.setup_stats(journal_trades, int(_p(params, "journal", "min_n_for_stats", 5)))
        out_factors.append(
            _f(factors, "day_type_classifier", stats, f"your history within 10 pts of this price (n={stats['n']})")
        )

    plan_note = None
    if plan and plan.get("htf_bias"):
        plan_note = plan["htf_bias"]

    if q is None:
        lean, reason = "no view", "flow unavailable"
    elif not confirmed:
        lean, reason = "no-go", f"flow not confirming (Q{q:.0f} < Q{Q_CONFIRM:.0f})"
    elif gamma_sign == "POS":
        lean, reason = f"{flow_dir} (reduced)", "flow confirms but positive gamma penalizes"
    else:
        lean, reason = flow_dir or "no view", "flow confirming"

    flip = None
    if lean == "no-go":
        flip = "the proven size of one-sided flow showing up, either direction"
    elif lean not in ("no view",):
        flip = "that flow fading, or the level starting to soak up the pressure"

    return {
        "price": price,
        "lean": lean,
        "reason": reason,
        "flip_condition": flip,
        "plan_bias": plan_note,
        "factors": out_factors,
    }


# -- day_read ---------------------------------------------------------------


def compute_day_read(
    market: dict | None,
    bars_5m: list[dict],
    levels: dict | None,
    regime: dict | None,
    factors: dict[str, Factor],
    *,
    bars_1m: list[dict] | None = None,
    daily: dict | None = None,
    events: list[dict] | None = None,
    params: dict | None = None,
    now_et: datetime | None = None,
) -> dict[str, Any]:
    m = market or {}
    last = m.get("last")
    out_factors: list[dict[str, Any]] = []
    votes: list[tuple[str, int, str]] = []  # (factor key, vote, flip description)

    cum_delta = m.get("cum_delta_rth")
    if cum_delta is not None:
        v = 1 if cum_delta > 0 else -1 if cum_delta < 0 else 0
        votes.append(("cum_delta", v, "the session's net buying/selling flipping sign"))
    out_factors.append(_f(factors, "htf_sweep", {"cum_delta_rth": cum_delta}, "session aggressor balance"))

    vwap = m.get("vwap")
    if last is not None and vwap is not None:
        v = 1 if last > vwap else -1
        votes.append(("vwap", v, f"price closing back {'below' if v > 0 else 'above'} the day's average ({vwap})"))
    out_factors.append(_f(factors, "value_area_position", {"last": last, "vwap": vwap,
                                                          "prior_value_area": m.get("prior_value_area")}))

    va = m.get("prior_value_area") or {}
    if last is not None and va.get("poc") is not None:
        v = 1 if last > va["poc"] else -1
        votes.append(("poc", v, f"trading back {'below' if v > 0 else 'above'} yesterday's busiest price ({va['poc']})"))

    on_h, on_l = m.get("on_high"), m.get("on_low")
    if last is not None and on_h is not None and on_l is not None:
        v = 1 if last > on_h else -1 if last < on_l else 0
        if v:
            votes.append(("on_range", v, f"re-entering the overnight range ({on_l}–{on_h})"))
        out_factors.append(_f(factors, "level_hierarchy", {"on_high": on_h, "on_low": on_l,
                                                           "position": "above" if v == 1 else "below" if v == -1 else "inside"}))

    f1, q = _latest_flow(bars_5m)
    flow_dir = None if not f1 else ("long" if f1 > 0 else "short")
    out_factors.append(_f(factors, "flow_impulse_qrank", {"f1_5": f1, "q": q, "direction": flow_dir},
                          "confirming" if (q or 0) >= Q_CONFIRM else f"below Q{Q_CONFIRM:.0f}"))

    gamma_sign = (regime or {}).get("gamma_sign")
    out_factors.append(_f(factors, "positive_gamma_penalty", gamma_sign,
                          "expect mean-reversion pressure" if gamma_sign == "POS" else None))

    # -- addendum: session structure (A9), value migration (A6.4), CVD slope --

    atr = (daily or {}).get("atr")
    gap = session_sig.gap_read(m.get("rth_open"), m.get("prior_close"), atr, m.get("session_high"), m.get("session_low"))
    if gap is None and m.get("gap_vs_prior_close") is not None:
        gap = {"points": m["gap_vs_prior_close"], "direction": "up" if m["gap_vs_prior_close"] > 0 else "down", "filled": None}
    if gap:
        out_factors.append(_f(factors, "day_type_classifier", gap, "gap"))

    open_typ = ib = ext = None
    if bars_1m:
        rth_1m = [b for b in bars_1m if _is_rth(b)]
        open_typ = session_sig.open_type(rth_1m, int(_p(params, "session", "open_type_window_min", 15)))
        ib = levels_sig.initial_balance(
            bars_1m, int(_p(params, "levels", "initial_balance_min", 60)), (daily or {}).get("ib_ranges")
        )
        ext = session_sig.range_extension(ib, m.get("session_high"), m.get("session_low"))
        if open_typ:
            out_factors.append(_f(factors, "day_type_classifier", open_typ, "open type"))
            if open_typ["type"] == "open-drive":
                votes.append(("open_type", 1 if open_typ["direction"] == "up" else -1,
                              "the open-drive failing back through the open"))
        if ib:
            out_factors.append(_f(factors, "level_hierarchy", ib, "initial balance"))
        if ext and ext.get("extended") and ext.get("side") in ("up", "down"):
            votes.append(("range_extension", 1 if ext["side"] == "up" else -1,
                          "range extension failing back inside the first hour's range"))
            out_factors.append(_f(factors, "day_type_classifier", ext, "range extension"))
        rng_vs_atr = session_sig.range_vs_atr(m.get("session_high"), m.get("session_low"), atr)
        if rng_vs_atr:
            out_factors.append(_f(factors, "day_type_classifier", rng_vs_atr, "range used vs ATR"))

    migration = profile_sig.value_migration(m.get("developing_value_area"), m.get("prior_value_area"))
    if migration:
        out_factors.append(_f(factors, "value_area_position", {"value_migration": migration}))
        if migration != "overlapping":
            votes.append(("value_migration", 1 if migration == "higher" else -1,
                          "developing value slipping back to overlap yesterday's"))

    slope = flow_sig.cvd_slope(bars_5m, int(_p(params, "flow", "cvd_slope_bars", 10)))
    div = flow_sig.cvd_divergence(bars_5m, int(_p(params, "flow", "cvd_divergence_bars", 15)))
    out_factors.append(_f(factors, "htf_sweep", {"cvd_slope": slope, "cvd_divergence": div}, "initiative trend"))

    va_pos = None
    if last is not None and va.get("vah") is not None and va.get("val") is not None:
        va_pos = "above" if last > va["vah"] else "below" if last < va["val"] else "inside"
    day_probs = session_sig.day_type(open_typ, ext, gap, slope, va_pos)
    out_factors.append(_f(factors, "day_type_classifier", day_probs, "trend/range/reversal guess"))

    next_event = None
    if events and (now_et or True):
        next_event = session_sig.minutes_to_event(events, now_et or datetime.now(timezone.utc).astimezone(ET))
        if next_event:
            out_factors.append(_f(factors, "day_type_classifier", next_event, "next high-impact event"))

    score = sum(v for _, v, _ in votes)
    n_votes = sum(1 for _, v, _ in votes if v != 0)
    if n_votes < 2 or abs(score) < max(2, n_votes - 1):
        lean = "no bias"
        flips = [d for _, v, d in votes if v != 0]
        flip = "factors disagree; a bias needs near-unanimity — watch: " + "; ".join(flips[:3]) if flips else None
    else:
        lean = "long" if score > 0 else "short"
        against = [d for _, v, d in votes if (v > 0) == (score < 0) and v != 0]
        with_ = [d for _, v, d in votes if v != 0 and (v > 0) == (score > 0)]
        flip = with_[0] if with_ else None
        flip = f"would flip on: {flip}" if flip else None
        if against:
            flip = (flip + "; already against: " + "; ".join(against)) if flip else None

    return {
        "lean": lean,
        "votes": [{"factor": k, "vote": v, "flip": d} for k, v, d in votes],
        "flip_condition": flip,
        "day_type_probs": day_probs,
        "next_event": next_event,
        "factors": out_factors,
        "note": "composite is discretionary framework; only tagged-validated inputs carry tested edge",
    }


def _is_rth(bar: dict) -> bool:
    try:
        et = datetime.fromisoformat(bar["t"]).astimezone(ET)
    except (KeyError, ValueError):
        return False
    return (9, 30) < (et.hour, et.minute) <= (16, 0) and (et.hour, et.minute) > (9, 30)


# -- manage_read ------------------------------------------------------------


def compute_manage_read(
    position: dict | None,
    market: dict | None,
    bars_1m: list[dict],
    bars_5m: list[dict],
    levels: dict | None,
    factors: dict[str, Factor],
    *,
    tape: dict | None = None,
    events: list[dict] | None = None,
    plan: dict | None = None,
    journal_median_min: float | None = None,
    observations: list[str] | None = None,
    headroom: dict | None = None,
    params: dict | None = None,
    now_et: datetime | None = None,
) -> dict[str, Any]:
    """A15 manage_read: hold / trail / reduce / exit with the plain reason.
    The verdict heuristic is framework; the stop-viability input is MAE-1."""
    positions = (position or {}).get("positions") or []
    if not positions:
        return {"in_trade": False, "verdict": None, "headroom": headroom,
                "note": "flat — nothing to manage"}

    p0 = positions[0]
    side = p0.get("side", "long")
    entry = float(p0.get("avg_price") or 0.0)
    last = (market or {}).get("last")
    d = 1 if side == "long" else -1

    stop_price = target_price = None
    for o in (position or {}).get("working_orders") or []:
        if o.get("contract") != p0.get("contract") or o.get("price") is None:
            continue
        if str(o.get("type", "")).lower().startswith("stop"):
            stop_price = float(o["price"])
        elif str(o.get("type", "")).lower() == "limit":
            target_price = float(o["price"])

    since = _bars_since(bars_1m, p0.get("entered_at"))
    approx = p0.get("entered_at") is None

    exc = manage_sig.live_excursions(entry, side, since, last)
    stop = manage_sig.stop_viability(entry, stop_price, float(_p(params, "manage", "min_stop_pts", MIN_STOP_PTS)))
    r_mult = manage_sig.r_multiple(entry, side, last, stop_price)
    flow_e = manage_sig.flow_since_entry(since, side)
    dists = manage_sig.distances(last, stop_price, target_price)
    minutes_in = None
    tit = None
    if p0.get("entered_at"):
        try:
            t0 = datetime.fromisoformat(str(p0["entered_at"]).replace("Z", "+00:00"))
            minutes_in = (datetime.now(timezone.utc) - t0.astimezone(timezone.utc)).total_seconds() / 60.0
            tit = manage_sig.time_in_trade(minutes_in, journal_median_min, float(_p(params, "manage", "overstay_mult", 2.0)))
        except ValueError:
            pass

    exhaustion = behavior_sig.exhaustion(
        bars_1m[-10:],
        int(_p(params, "behavior", "exhaustion_falling_bars", 3)),
        float(_p(params, "behavior", "exhaustion_final_vol_mult", 0.5)),
    ) if bars_1m else None
    absorption = (tape or {}).get("absorption_at_last")
    room = levels_sig.nearest_levels((levels or {}).get("levels", []), last) if last is not None else None
    next_event = session_sig.minutes_to_event(events or [], now_et or datetime.now(timezone.utc).astimezone(ET)) if events else None
    align = manage_sig.plan_alignment(side, (plan or {}).get("htf_bias"))

    # -- verdict heuristic (framework) --------------------------------------
    flow_against = flow_e is not None and not flow_e["with_position"]
    exhausting_with = exhaustion is not None and (
        (exhaustion["direction"] == "up") == (d > 0)
    )
    absorbed_against = bool(absorption and absorption.get("absorbing_side") == ("sell" if d > 0 else "buy"))

    if next_event and next_event["minutes"] <= 5:
        verdict, reason = "exit", f"news in {next_event['minutes']} minutes — don't hold through it"
    elif stop["has_stop"] and stop["viable"] is False:
        verdict, reason = "exit", "your stop is inside the noise band; it gets hit whether you're right or not"
    elif flow_against and (exhausting_with or absorbed_against):
        verdict, reason = "exit", "the flow that got you in has left and the push is stalling"
    elif r_mult is not None and r_mult >= 1.5 and flow_against:
        verdict, reason = "reduce", "you're well paid and the buying behind it is fading"
    elif tit and tit.get("overstaying"):
        verdict, reason = "reduce", "you've been in far longer than this setup usually takes to work"
    elif r_mult is not None and r_mult >= 1.5:
        verdict, reason = "trail", "the trade has paid; protect it without choking it"
    else:
        verdict, reason = "hold", "nothing has changed against you"

    factors_out = [
        _f(factors, "stop_viability", stop, "MAE-1: the one validated input here"),
        _f(factors, "flow_impulse_qrank", flow_e, "flow since entry"),
        _f(factors, "day_type_classifier", {"exhaustion": exhaustion, "absorption": absorption}, "near-price behavior"),
        _f(factors, "level_hierarchy", room, "next level each way"),
    ]

    return {
        "in_trade": True,
        "position": {"contract": p0.get("contract"), "side": side, "size": p0.get("size"),
                     "entry": entry, "unrealized": p0.get("unrealized")},
        "verdict": verdict,
        "reason": reason,
        "excursions": exc,
        "excursions_approximate": approx,
        "r_multiple": r_mult,
        "distances": dists,
        "time_in_trade": tit or {"minutes": minutes_in},
        "plan_alignment": align,
        "next_event": next_event,
        "headroom": headroom,
        "factors": factors_out,
        "note": "verdict heuristic is discretionary framework; stop viability is MAE-1",
    }


def _bars_since(bars_1m: list[dict], entered_at: Any) -> list[dict]:
    if not entered_at:
        return bars_1m[-30:]
    try:
        t0 = datetime.fromisoformat(str(entered_at).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return bars_1m[-30:]
    out = []
    for b in bars_1m:
        try:
            if datetime.fromisoformat(b["t"]).astimezone(timezone.utc) >= t0:
                out.append(b)
        except (KeyError, ValueError):
            continue
    return out


# -- gathering + tool wrappers ---------------------------------------------


async def _gather(ctx: ToolContext) -> tuple[dict | None, list[dict], list[dict], dict | None, dict | None]:
    market = await rk.read_json(ctx.redis, rk.MARKET_STATE)
    bars5 = await rk.read_json(ctx.redis, rk.BARS_5M)
    bars1 = await rk.read_json(ctx.redis, rk.BARS_1M)
    levels = await rk.read_json(ctx.redis, rk.LEVELS)
    regime = await rk.read_json(ctx.redis, rk.REGIME)
    return market, (bars5 or {}).get("bars", []), (bars1 or {}).get("bars", []), levels, regime


async def _today_plan(ctx: ToolContext) -> dict | None:
    today = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
    try:
        res = await ctx.db.table("sessions").select("htf_bias, key_levels, hunting, invalidation").eq(
            "session_date", today).maybe_single().execute()
        return getattr(res, "data", None)
    except Exception:  # noqa: BLE001 — plan is optional context
        return None


async def _today_events(ctx: ToolContext) -> list[dict]:
    today = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
    try:
        res = await ctx.db.table("calendar_events").select("event_time_et, name, impact").eq(
            "event_date", today).execute()
        return res.data or []
    except Exception:  # noqa: BLE001
        return []


@tool(
    "level_read",
    {
        "description": (
            "Deterministic composite scorecard for a specific price: flow impulse Q-rank, "
            "gamma regime, stop viability per MAE-1, nearby levels and confluence, footprint "
            "(imbalance stacks, absorption, auction state) over the last 30 minutes, live "
            "tape context, your journal history near this price, and plan alignment — every "
            "factor with its validated/discretionary tag. Computed by code, not by the model."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"price": {"type": "number"}},
            "required": ["price"],
            "additionalProperties": False,
        },
    },
)
async def level_read(ctx: ToolContext, args: dict[str, Any]) -> Any:
    price = float(args["price"])
    market, bars5, _bars1, levels, regime = await _gather(ctx)
    if market is None:
        raise RuntimeError("no market state — engine offline")
    tape_doc = await rk.read_json(ctx.redis, rk.TAPE)
    now_min = int(datetime.now(timezone.utc).timestamp() // 60)
    fp_minutes = await read_vbp_minutes(ctx, now_min - FP_LOOKBACK_MIN + 1, now_min)
    plan = await _today_plan(ctx)
    journal_trades = None
    try:
        band = float(_p(ctx.params, "journal", "similar_price_band_pts", 10.0))
        res = await ctx.db.table("trades").select("net_pnl, entry_price, entry_at").gte(
            "entry_price", price - band).lte("entry_price", price + band).order(
            "entry_at", desc=True).limit(50).execute()
        journal_trades = res.data or []
    except Exception:  # noqa: BLE001
        pass
    return compute_level_read(
        price, market, bars5, levels, regime, ctx.factors,
        tape=tape_doc, fp_minutes=fp_minutes, journal_trades=journal_trades,
        plan=plan, params=ctx.params,
    )


@tool(
    "day_read",
    {
        "description": (
            "Deterministic composite scorecard for directional day bias: session aggressor "
            "balance, VWAP/value position, overnight range, gap and fill, open type, initial "
            "balance and range extension, value migration, CVD slope/divergence, a rule-based "
            "trend/range/reversal guess, and the next high-impact event. Returns 'no bias' "
            "when factors disagree and states what would flip it. Computed by code, not by "
            "the model."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
)
async def day_read(ctx: ToolContext, args: dict[str, Any]) -> Any:
    market, bars5, bars1, levels, regime = await _gather(ctx)
    if market is None:
        raise RuntimeError("no market state — engine offline")
    daily = await rk.read_json(ctx.redis, rk.DAILY)
    events = await _today_events(ctx)
    return compute_day_read(
        market, bars5, levels, regime, ctx.factors,
        bars_1m=bars1, daily=daily, events=events, params=ctx.params,
    )


@tool(
    "manage_read",
    {
        "description": (
            "Deterministic composite for the OPEN position — should you hold, trail, reduce "
            "or exit: live MAE/MFE, R multiple, stop viability per MAE-1, flow since entry, "
            "exhaustion/absorption near price, room to the next levels, minutes to the next "
            "high-impact event, time-in-trade vs your journal, plan alignment, and governor "
            "headroom. Returns in_trade=false when flat. Computed by code, not by the model."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
)
async def manage_read(ctx: ToolContext, args: dict[str, Any]) -> Any:
    market, bars5, bars1, levels, _regime = await _gather(ctx)
    position = await rk.read_json(ctx.redis, rk.POSITION)
    tape_doc = await rk.read_json(ctx.redis, rk.TAPE)
    events = await _today_events(ctx)
    plan = await _today_plan(ctx)

    headroom = None
    journal_median = None
    try:
        from .position import MAX_TRADES_PER_DAY, SHUTOFF_ET

        now_et = datetime.now(timezone.utc).astimezone(ET)
        today = now_et.date().isoformat()
        res = await ctx.db.table("checklist_entries").select("id", count="exact").eq("session_date", today).execute()
        shutoff = now_et.replace(hour=SHUTOFF_ET[0], minute=SHUTOFF_ET[1], second=0, microsecond=0)
        headroom = {
            "trades_left": max(0, MAX_TRADES_PER_DAY - (res.count or 0)),
            "minutes_to_shutoff": max(0, int((shutoff - now_et).total_seconds() // 60)) if now_et < shutoff else 0,
        }
        tr = await ctx.db.table("trades").select("entry_at, exit_at").order("entry_at", desc=True).limit(50).execute()
        journal_median = journal_sig.median_hold_minutes(tr.data or [])
    except Exception:  # noqa: BLE001
        pass

    return compute_manage_read(
        position, market, bars1, bars5, levels, ctx.factors,
        tape=tape_doc, events=events, plan=plan, journal_median_min=journal_median,
        headroom=headroom, params=ctx.params,
    )
