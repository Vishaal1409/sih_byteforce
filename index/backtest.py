"""index/backtest.py — validate APIx against a reference average-fare series.

WHAT THE REFERENCE IS
---------------------
A SYNTHETIC STAND-IN, not DGCA data. DGCA publishes monthly PDF aggregates;
APIx here is a daily series over 35 days, so no real DGCA daily series exists
to validate against. config/backtest.yaml records exactly what was attempted
and why. TASKS.md Phase 6 permits a clearly-labelled stand-in.

WHY THE COMPARISON IS STILL WORTH RUNNING
-----------------------------------------
The reference is not APIx-with-noise — that would give a perfect correlation
and prove nothing. It is an UNWEIGHTED market mean, the way a simple published
average fare is actually computed, rebased to the same base date.

That makes the two estimators genuinely different: APIx weights by traffic and
market share and stratifies by booking window; the naive average does none of
that. So the gap between them is real and interpretable — it is precisely the
composition effect APIx exists to remove. Close-but-not-identical agreement is
the honest, expected outcome, and the divergence is a feature to point at
rather than an error to explain away.

CLI:
    python -m index.backtest            # run and persist
    python -m index.backtest --show     # ...and print the series
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from config_loader import CONFIG_DIR, _read_yaml  # noqa: E402
from index import apix  # noqa: E402

BACKTEST_CONFIG_PATH = CONFIG_DIR / "backtest.yaml"
RESULTS_JSON = Path(__file__).resolve().parents[1] / "data" / "backtest_results.json"
OVERLAY_HTML = Path(__file__).resolve().parents[1] / "docs" / "backtest_overlay.html"


def load_backtest_config() -> dict[str, Any]:
    return _read_yaml(BACKTEST_CONFIG_PATH)


# ---------------------------------------------------------------------------
# The reference series
# ---------------------------------------------------------------------------


def build_reference_series(
    df: pd.DataFrame, cfg: dict[str, Any] | None = None
) -> pd.DataFrame:
    """A DGCA-style average-fare series, rebased to 100.

    *** SYNTHETIC. NOT REAL DGCA DATA. ***

    Computed as an unweighted daily mean of every observed fare — deliberately
    a different estimator from APIx, so the comparison tests something.

    To swap in real DGCA data, replace this function with a loader returning
    the same two columns (`query_date`, `reference_value`). Nothing downstream
    changes.
    """
    cfg = cfg or load_backtest_config()
    ref_cfg = cfg["reference"]

    if df.empty:
        return pd.DataFrame(columns=["query_date", "reference_value"])

    daily = (
        df.groupby("query_date", sort=True, observed=True)["total_fare"]
        .mean()
        .reset_index()
        .rename(columns={"total_fare": "mean_fare"})
    )

    # Independent measurement noise — a published statistic is a survey, not a
    # census. Seeded so the demo is reproducible.
    rng = np.random.default_rng(int(ref_cfg["random_seed"]))
    noise = rng.normal(1.0, float(ref_cfg["noise_sd"]), size=len(daily))
    daily["mean_fare"] = daily["mean_fare"] * noise

    # Published aggregates are smoothed relative to live prices.
    window = int(ref_cfg.get("smoothing_days", 1))
    if window > 1:
        daily["mean_fare"] = (
            daily["mean_fare"].rolling(window, center=True, min_periods=1).mean()
        )

    base = daily["mean_fare"].iloc[0]
    daily["reference_value"] = apix.BASE_VALUE * daily["mean_fare"] / base
    return daily[["query_date", "reference_value"]]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class BacktestMetrics:
    """Agreement between APIx and the reference, over the common window."""

    n_days: int
    start_date: str
    end_date: str
    correlation: float          # Pearson, on levels
    rank_correlation: float     # Spearman — shape agreement, outlier-robust
    mape_pct: float             # mean absolute percentage error
    rmse: float
    mean_abs_diff: float
    max_abs_diff: float
    apix_change_pct: float      # total move over the window, APIx
    reference_change_pct: float # ...and the reference

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 6) if isinstance(v, float) else v)
                for k, v in asdict(self).items()}


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 2 or np.std(a) == 0 or np.std(b) == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Rank correlation without pulling in scipy — Pearson on the ranks."""
    if len(a) < 2:
        return float("nan")
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    return _pearson(ra, rb)


def compare(
    index_series: pd.DataFrame,
    reference: pd.DataFrame,
    cfg: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, BacktestMetrics]:
    """Align the two series on common dates and measure agreement."""
    cfg = cfg or load_backtest_config()

    merged = index_series.merge(reference, on="query_date", how="inner")
    merged = merged.sort_values("query_date").reset_index(drop=True)
    if merged.empty:
        raise ValueError("APIx and the reference share no dates")

    if cfg["comparison"].get("rebase_to_common_base", True):
        # Both to 100 at the first COMMON date, so levels are comparable even
        # if the two series start on different days.
        for col in ("index_value", "reference_value"):
            merged[col] = apix.BASE_VALUE * merged[col] / merged[col].iloc[0]

    a = merged["index_value"].to_numpy(dtype=float)
    b = merged["reference_value"].to_numpy(dtype=float)
    diff = a - b

    metrics = BacktestMetrics(
        n_days=len(merged),
        start_date=str(merged["query_date"].iloc[0]),
        end_date=str(merged["query_date"].iloc[-1]),
        correlation=_pearson(a, b),
        rank_correlation=_spearman(a, b),
        mape_pct=float(np.mean(np.abs(diff / b)) * 100.0),
        rmse=float(np.sqrt(np.mean(diff ** 2))),
        mean_abs_diff=float(np.mean(np.abs(diff))),
        max_abs_diff=float(np.max(np.abs(diff))),
        apix_change_pct=float((a[-1] / a[0] - 1.0) * 100.0),
        reference_change_pct=float((b[-1] / b[0] - 1.0) * 100.0),
    )
    merged["difference"] = diff
    return merged, metrics


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def write_overlay_chart(
    merged: pd.DataFrame, metrics: BacktestMetrics, out: Path, cfg: dict[str, Any]
) -> None:
    """APIx vs reference overlay, with the metrics on the chart itself."""
    import plotly.graph_objects as go

    ref_label = cfg["reference"]["short_label"]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=merged["query_date"], y=merged["index_value"],
        mode="lines+markers", name="APIx (weighted index)",
    ))
    fig.add_trace(go.Scatter(
        x=merged["query_date"], y=merged["reference_value"],
        mode="lines+markers", name=ref_label, line=dict(dash="dash"),
    ))
    fig.update_layout(
        title=(
            f"APIx vs {ref_label}<br>"
            f"<sub>r = {metrics.correlation:.3f} &nbsp;|&nbsp; "
            f"MAPE = {metrics.mape_pct:.2f}% &nbsp;|&nbsp; "
            f"{metrics.n_days} days &nbsp;|&nbsp; "
            f"BOTH SERIES DERIVED FROM SIMULATED FARE DATA</sub>"
        ),
        xaxis_title="Query date (date fare was observed)",
        yaxis_title="Index (first common date = 100)",
        template="plotly_white",
        hovermode="x unified",
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(out), include_plotlyjs="cdn")


_CREATE_TABLES = """
DROP TABLE IF EXISTS apix_backtest;
CREATE TABLE apix_backtest (
    query_date      TEXT PRIMARY KEY,
    index_value     REAL NOT NULL,
    reference_value REAL NOT NULL,
    difference      REAL NOT NULL
);

DROP TABLE IF EXISTS apix_backtest_metrics;
CREATE TABLE apix_backtest_metrics (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def run_backtest(
    db_path: str | Path = db.DEFAULT_DB_PATH,
    cfg: dict[str, Any] | None = None,
    write_chart: bool = True,
    results_json: str | Path | None = None,
    overlay_html: str | Path | None = None,
) -> tuple[pd.DataFrame, BacktestMetrics]:
    """Build the reference, compare, and persist everything.

    `results_json` / `overlay_html` default to the project paths but are
    overridable — otherwise a run against a throwaway database would still
    overwrite the real demo artifacts, which is exactly what happened the
    first time the tests were run.
    """
    cfg = cfg or load_backtest_config()
    results_json = Path(results_json) if results_json else RESULTS_JSON
    overlay_html = Path(overlay_html) if overlay_html else OVERLAY_HTML

    fares = apix.load_clean_fares(db_path)
    index_result = apix.compute_index(fares)
    reference = build_reference_series(fares, cfg)
    merged, metrics = compare(index_result.series, reference, cfg)

    min_days = int(cfg["comparison"]["min_days"])
    if metrics.n_days < min_days:
        raise ValueError(
            f"backtest needs at least {min_days} days, got {metrics.n_days}"
        )

    payload = {
        "metrics": metrics.to_dict(),
        "reference": {
            "label": cfg["reference"]["label"],
            "short_label": cfg["reference"]["short_label"],
            "is_real_data": bool(cfg["reference"]["is_real_data"]),
            "method": cfg["reference"]["method"],
        },
        "series": [
            {
                "query_date": str(r.query_date),
                "apix": round(float(r.index_value), 4),
                "reference": round(float(r.reference_value), 4),
                "difference": round(float(r.difference), 4),
            }
            for r in merged.itertuples()
        ],
        "generated_at": db.utc_now_iso(),
    }

    with db.connect(db_path) as conn:
        conn.executescript(_CREATE_TABLES)
        conn.executemany(
            "INSERT INTO apix_backtest (query_date, index_value, reference_value, "
            "difference) VALUES (?, ?, ?, ?)",
            [(str(r.query_date), float(r.index_value), float(r.reference_value),
              float(r.difference)) for r in merged.itertuples()],
        )
        conn.executemany(
            "INSERT INTO apix_backtest_metrics (key, value) VALUES (?, ?)",
            [(k, str(v)) for k, v in metrics.to_dict().items()]
            + [
                ("reference_label", cfg["reference"]["label"]),
                ("reference_is_real_data", "false"),
                ("generated_at", payload["generated_at"]),
            ],
        )

    results_json.parent.mkdir(parents=True, exist_ok=True)
    results_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if write_chart:
        write_overlay_chart(merged, metrics, overlay_html, cfg)

    return merged, metrics


def latest_backtest(db_path: str | Path = db.DEFAULT_DB_PATH) -> dict[str, Any] | None:
    """Stored backtest results, for the API and dashboard."""
    with db.connect(db_path) as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='apix_backtest'"
        ).fetchone()
        if not exists:
            return None
        metrics = dict(conn.execute("SELECT key, value FROM apix_backtest_metrics").fetchall())
        rows = conn.execute(
            "SELECT query_date, index_value, reference_value, difference "
            "FROM apix_backtest ORDER BY query_date"
        ).fetchall()
    if not rows:
        return None
    return {
        "metrics": metrics,
        "series": [dict(r) for r in rows],
    }


# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest APIx against a reference series")
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_backtest_config()
    merged, m = run_backtest(args.db, cfg)

    print("=" * 72)
    print("APIx BACKTEST")
    print("=" * 72)
    print(f"  reference        {cfg['reference']['label']}")
    print(f"  window           {m.start_date} .. {m.end_date}  ({m.n_days} days)")
    print(f"  correlation (r)  {m.correlation:.4f}")
    print(f"  rank corr (rho)  {m.rank_correlation:.4f}")
    print(f"  MAPE             {m.mape_pct:.2f}%")
    print(f"  RMSE             {m.rmse:.3f} index points")
    print(f"  mean |diff|      {m.mean_abs_diff:.3f} index points")
    print(f"  max  |diff|      {m.max_abs_diff:.3f} index points")
    print(f"  total move       APIx {m.apix_change_pct:+.2f}%   "
          f"reference {m.reference_change_pct:+.2f}%")
    print(f"\n  results  -> {RESULTS_JSON}")
    print(f"  overlay  -> {OVERLAY_HTML}")

    if args.show:
        print(f"\n  {'date':<12} {'APIx':>8} {'ref':>8} {'diff':>8}")
        for r in merged.itertuples():
            print(f"  {str(r.query_date):<12} {r.index_value:>8.2f} "
                  f"{r.reference_value:>8.2f} {r.difference:>+8.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
