"""TFR pass 2: flow aggregates -> walk-forward decision files.

Consumes pass-1 per-minute aggregates (scripts/precompute_flow.py) and
walks forward session by session, maintaining ONLY trailing state:

  - rolling 60-session distributions for every z-scored feature
  - trailing 20-session P95 trade size -> the large-trade cutoff (F4)
  - trailing 60-session |F1_5| samples -> Q_entry/Q_hf percentile tables
  - trailing 120 sessions of X_t = [z_rv30, z_f1_5, z_eff, z_vol] -> the
    GMM, refit at the first session of each calendar month (or week),
    K=3 (or 4), fixed seed, persisted with its fit window
  - trailing 200 bars of regime labels -> the Markov transition matrix

Deterministic label mapping, fixed in advance (spec section 4): the
component whose centroid has the highest |z_f1_5| is Active-Flow; of the
rest, the lowest z_rv30 centroid is Quiet; the remainder are Chop (K=4:
both remaining are Chop, split by z_vol for the journal only -- the
strategy only distinguishes Active-Flow).

Output: one JSON per session with per-5m-bar records (features, regime,
T[current][Active-Flow], Mahalanobis distance) plus the session's
percentile tables and model id. The strategy layer does no statistics --
it reads this file and applies entry/exit logic, exactly as NAIM read its
noise curves. Zero lookahead: every value for session d derives from
sessions strictly before d, except intra-day quantities (session
cumulative delta, VWAP-free features) that use only bars up to t.

Usage:
  uv run python scripts/fit_regimes.py --flow var/flow --out var/decisions/k3m \
      --k 3 --refit monthly
"""

from __future__ import annotations

import argparse
import json
from collections import deque
from datetime import date
from pathlib import Path
from statistics import fmean, pstdev

import numpy as np
from sklearn.mixture import GaussianMixture

PCTS = (55, 60, 65, 70, 75, 80, 85)
DECISION_START = 5  # first 5m bar closing 09:35
DECISION_END = 390  # last 5m bar closing 16:00 (strategy applies its own window)
Z_WINDOW = 60  # sessions, feature z-scores
SIZE_WINDOW = 20  # sessions, P95 trade size
GMM_WINDOW = 120  # sessions, regime fit
GMM_MIN = 40  # minimum sessions before the first fit
TRANS_BARS = 200  # trailing bars for the transition matrix
SIZE_CUTS = (3, 5, 10, 20)


def percentile(samples: list[float], pct: int) -> float:
    ordered = sorted(samples)
    rank = max(1, -(-len(ordered) * pct // 100))
    return ordered[rank - 1]


class Rolling:
    """Per-feature trailing-window store with z-scoring."""

    def __init__(self, sessions: int) -> None:
        self._days: deque[list[float]] = deque(maxlen=sessions)
        self._today: list[float] = []

    def z(self, value: float) -> float:
        flat = [v for day in self._days for v in day]
        if len(flat) < 50:
            return 0.0
        mean = fmean(flat)
        sd = pstdev(flat)
        if sd == 0:
            return 0.0
        return (value - mean) / sd

    def observe(self, value: float) -> None:
        self._today.append(value)

    def roll(self) -> None:
        if self._today:
            self._days.append(self._today)
        self._today = []

    @property
    def ready(self) -> bool:
        return sum(len(d) for d in self._days) >= 50


def five_minute_series(minutes: dict[str, dict]) -> list[dict]:
    """Collapse per-minute aggregates into 5m bars ending at indices 5..390."""
    bars = []
    for end in range(5, 391, 5):
        window = [minutes[str(i)] for i in range(end - 4, end + 1) if str(i) in minutes]
        if not window:
            continue
        close = window[-1]["close"]
        bars.append(
            {
                "idx": end,
                "close": float(close),
                "vol": sum(m["vol"] for m in window),
                "buy": sum(m["buy"] for m in window),
                "sell": sum(m["sell"] for m in window),
                **{
                    f"buy_ge{c}": sum(m[f"buy_ge{c}"] for m in window) for c in SIZE_CUTS
                },
                **{
                    f"sell_ge{c}": sum(m[f"sell_ge{c}"] for m in window) for c in SIZE_CUTS
                },
            }
        )
    return bars


def flow_over(minutes: dict[str, dict], end: int, span: int) -> float:
    """F1 over the trailing `span` minutes ending at minute index `end`."""
    buy = sell = vol = 0
    for i in range(max(1, end - span + 1), end + 1):
        m = minutes.get(str(i))
        if m is None:
            continue
        buy += m["buy"]
        sell += m["sell"]
        vol += m["vol"]
    if vol == 0:
        return 0.0
    return (buy - sell) / vol


def label_map(model: GaussianMixture) -> dict[int, str]:
    """Deterministic centroid -> name mapping. X = [z_rv, z_f1, z_eff, z_vol]."""
    means = model.means_
    af = int(max(range(len(means)), key=lambda i: abs(means[i][1])))
    rest = [i for i in range(len(means)) if i != af]
    quiet = min(rest, key=lambda i: means[i][0])
    mapping = {af: "AF", quiet: "QUIET"}
    for i in rest:
        if i != quiet:
            mapping[i] = "CHOP"
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--refit", choices=["monthly", "weekly"], default="monthly")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "models").mkdir(exist_ok=True)
    sessions = sorted(args.flow.glob("*.json"))
    print(f"{len(sessions)} flow sessions")

    z_stores = {name: Rolling(Z_WINDOW) for name in ("rv", "f1_5", "eff", "vol", "dcd", "dp")}
    f1_abs: deque[list[float]] = deque(maxlen=Z_WINDOW)
    size_p95: deque[int] = deque(maxlen=SIZE_WINDOW)
    x_rows: deque[list[list[float]]] = deque(maxlen=GMM_WINDOW)
    labels: deque[int] = deque(maxlen=TRANS_BARS)  # 1 = AF, 0 = not
    model: GaussianMixture | None = None
    mapping: dict[int, str] = {}
    model_id: str | None = None
    fit_key: str | None = None
    mahal_cut: float | None = None

    for path in sessions:
        session = json.loads(path.read_text())
        if session["qa"]["excluded"]:
            print(f"{path.stem}: excluded by QA, skipped")
            continue
        day = date.fromisoformat(path.stem)
        iso = day.isocalendar()
        if args.refit == "monthly":
            key = f"{day.year}-{day.month:02d}"
        else:
            key = f"{iso.year}-{iso.week:02d}"

        # Refit at the first session of a new period, on trailing data only.
        if key != fit_key and len(x_rows) >= GMM_MIN:
            data = np.array([row for day_rows in x_rows for row in day_rows])
            model = GaussianMixture(
                n_components=args.k, covariance_type="full", random_state=7, n_init=3
            ).fit(data)
            mapping = label_map(model)
            # Model-health cut: 99.9th percentile Mahalanobis on the fit data.
            distances = _mahalanobis(model, data)
            mahal_cut = float(np.quantile(distances, 0.999))
            model_id = f"{args.k}-{args.refit}-{path.stem}"
            (args.out / "models" / f"{model_id}.json").write_text(
                json.dumps(
                    {
                        "means": model.means_.tolist(),
                        "covariances": model.covariances_.tolist(),
                        "weights": model.weights_.tolist(),
                        "mapping": {str(k): v for k, v in mapping.items()},
                        "mahal_cut": mahal_cut,
                        "fit_sessions": len(x_rows),
                    }
                ),
                encoding="utf-8",
            )
            fit_key = key
        elif key != fit_key:
            fit_key = key  # not enough history yet; stay unfitted

        minutes = session["minutes"]
        bars = five_minute_series(minutes)
        cut = 5
        if size_p95:
            p95 = sorted(size_p95)[len(size_p95) // 2]
            cut = min(SIZE_CUTS, key=lambda c: abs(c - p95))

        cd = 0.0
        closes: list[float] = []
        cds: list[float] = []
        out_bars: dict[str, dict] = {}
        today_x: list[list[float]] = []
        for bar in bars:
            idx = bar["idx"]
            cd += bar["buy"] - bar["sell"]
            closes.append(bar["close"])
            cds.append(cd)

            f1_5 = flow_over(minutes, idx, 5)
            f1_2 = flow_over(minutes, idx, 2)
            f1_15 = flow_over(minutes, idx, 15)
            aggr = bar["buy"] + bar["sell"]
            dp5 = abs(closes[-1] - closes[-2]) if len(closes) > 1 else 0.0
            eff = dp5 / aggr if aggr else 0.0
            rv = pstdev(
                [
                    (closes[i] - closes[i - 1]) / closes[i - 1]
                    for i in range(max(1, len(closes) - 6), len(closes))
                ]
            ) if len(closes) > 2 else 0.0
            dcd6 = cds[-1] - cds[-7] if len(cds) > 6 else 0.0
            dp6 = closes[-1] - closes[-7] if len(closes) > 6 else 0.0
            vol_ge = bar[f"buy_ge{cut}"] + bar[f"sell_ge{cut}"]
            large_share = vol_ge / bar["vol"] if bar["vol"] else 0.0
            large_imb = (
                (bar[f"buy_ge{cut}"] - bar[f"sell_ge{cut}"]) / vol_ge if vol_ge else 0.0
            )

            z_rv = z_stores["rv"].z(rv)
            z_f1 = z_stores["f1_5"].z(f1_5)
            z_eff = z_stores["eff"].z(eff)
            z_vol = z_stores["vol"].z(bar["vol"])
            divergence = z_stores["dcd"].z(dcd6) - z_stores["dp"].z(dp6)

            x = [z_rv, z_f1, z_eff, z_vol]
            today_x.append(x)

            regime = None
            t_af = None
            mahal = None
            if model is not None:
                arr = np.array([x])
                component = int(model.predict(arr)[0])
                regime = mapping[component]
                mahal = float(_mahalanobis(model, arr)[0])
                labels.append(1 if regime == "AF" else 0)
                t_af = _transition_to_af(labels, current_af=regime == "AF")

            out_bars[str(idx)] = {
                "close": bar["close"],
                "f1_2": round(f1_2, 5),
                "f1_5": round(f1_5, 5),
                "f1_15": round(f1_15, 5),
                "div": round(divergence, 3),
                "z_eff": round(z_eff, 3),
                "large_share": round(large_share, 4),
                "large_imb": round(large_imb, 4),
                "z_vol": round(z_vol, 3),
                "z_rv": round(z_rv, 3),
                "regime": regime,
                "t_af": None if t_af is None else round(t_af, 4),
                "mahal": None if mahal is None else round(mahal, 3),
            }

            # Observe raw values for FUTURE sessions' z-scores.
            z_stores["rv"].observe(rv)
            z_stores["f1_5"].observe(f1_5)
            z_stores["eff"].observe(eff)
            z_stores["vol"].observe(bar["vol"])
            z_stores["dcd"].observe(dcd6)
            z_stores["dp"].observe(dp6)

        # Session's percentile tables come from PRIOR sessions only.
        flat_f1 = [v for day_vals in f1_abs for v in day_vals]
        q_table = (
            {str(p): percentile(flat_f1, p) for p in PCTS} if len(flat_f1) >= 500 else None
        )
        (args.out / f"{path.stem}.json").write_text(
            json.dumps(
                {
                    "model": model_id,
                    "mahal_cut": mahal_cut,
                    "q_f1": q_table,
                    "size_cut": cut,
                    "bars": out_bars,
                }
            ),
            encoding="utf-8",
        )

        # Roll the trailing stores.
        for store in z_stores.values():
            store.roll()
        f1_abs.append([abs(b["f1_5"]) for b in (out_bars[k] for k in out_bars)])
        size_p95.append(session["qa"]["size_p95"])
        x_rows.append(today_x)

    print("done")


def _mahalanobis(model: GaussianMixture, data: np.ndarray) -> np.ndarray:
    """Distance to the NEAREST component, which is what off-the-map means."""
    out = np.full(len(data), np.inf)
    for mean, cov in zip(model.means_, model.covariances_, strict=True):
        inv = np.linalg.inv(cov)
        delta = data - mean
        d = np.sqrt(np.einsum("ij,jk,ik->i", delta, inv, delta))
        out = np.minimum(out, d)
    return out


def _transition_to_af(labels: deque[int], current_af: bool) -> float | None:
    """P(next bar AF | current state), from the trailing labeled bars."""
    if len(labels) < 30:
        return None
    seq = list(labels)
    from_state = [i for i in range(len(seq) - 1) if bool(seq[i]) == current_af]
    if not from_state:
        return None
    return sum(seq[i + 1] for i in from_state) / len(from_state)


if __name__ == "__main__":
    main()
