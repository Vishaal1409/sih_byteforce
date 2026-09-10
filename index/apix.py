"""index/apix.py — the Airfare Price Index itself.

A Laspeyres-style fixed-basket index. In words: pick a basket of
(route x airline x booking-window) cells, fix its weights once from external
shares, and track how the average fare in each cell moves relative to a base
period. The weights never move, so a change in APIx is a change in PRICE, not a
change in what happened to be observed.

    APIx_t = 100 * sum_i ( w_i * P_it / P_i0 )

    i    a basket cell: (route, airline, advance-purchase bucket)
    w_i  route traffic share x airline market share x bucket weight,
         from config/routes.yaml, airlines.yaml, index.yaml
    P_it mean total fare in cell i on day t
    P_i0 mean total fare in cell i in the base period
    t    query_date, i.e. the day the fare was observed

Full write-up for a non-technical reader: docs/methodology.md.

CLI:
    python -m index.apix            # build all series and write them to the DB
    python -m index.apix --show     # ...and print a summary
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from config_loader import CONFIG_DIR, _read_yaml, load_airlines, load_routes  # noqa: E402

INDEX_CONFIG_PATH = CONFIG_DIR / "index.yaml"

#: The index is 100 at the base period, by construction.
BASE_VALUE = 100.0


def load_index_config() -> dict[str, Any]:
    return _read_yaml(INDEX_CONFIG_PATH)


# ---------------------------------------------------------------------------
# Basket construction
# ---------------------------------------------------------------------------


def bucket_labels(cfg: dict[str, Any]) -> list[str]:
    return [b["name"] for b in cfg["advance_purchase_buckets"]]


def assign_bucket(apd: pd.Series, cfg: dict[str, Any]) -> pd.Series:
    """Map advance_purchase_days onto its bucket label.

    Anything outside every configured range becomes NaN and is excluded from
    the index — the basket is fixed, so a lead time we did not define a weight
    for has no place in it.
    """
    buckets = cfg["advance_purchase_buckets"]
    out = pd.Series(pd.NA, index=apd.index, dtype="object")
    for b in buckets:
        in_range = (apd >= int(b["min_days"])) & (apd <= int(b["max_days"]))
        out = out.mask(in_range, b["name"])
    return out


def basket_weights(cfg: dict[str, Any] | None = None) -> pd.DataFrame:
    """Every basket cell and its fixed weight.

    weight = route traffic share x airline market share x bucket weight.
    Because each of the three sets sums to 1.0, the product does too — checked
    below rather than assumed.
    """
    cfg = cfg or load_index_config()
    rows = []
    for route in load_routes():
        for airline in load_airlines():
            for b in cfg["advance_purchase_buckets"]:
                rows.append({
                    "origin": route.origin,
                    "dest": route.dest,
                    "airline": airline.code,
                    "bucket": b["name"],
                    "weight": (
                        route.traffic_weight
                        * airline.market_share_weight
                        * float(b["weight"])
                    ),
                })
    weights = pd.DataFrame(rows)

    total = weights["weight"].sum()
    if abs(total - 1.0) > 1e-6:
        raise ValueError(
            f"basket weights sum to {total!r}, expected 1.0 — check "
            f"routes.yaml, airlines.yaml and index.yaml bucket weights"
        )
    return weights


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_clean_fares(
    db_path: str | Path = db.DEFAULT_DB_PATH, cfg: dict[str, Any] | None = None
) -> pd.DataFrame:
    """Cleaned fares, bucketed and ready for indexing."""
    cfg = cfg or load_index_config()
    with db.connect(db_path) as conn:
        df = db.query_fares_df(conn, table="fares_clean")
    return prepare(df, cfg)


def prepare(df: pd.DataFrame, cfg: dict[str, Any] | None = None) -> pd.DataFrame:
    """Attach bucket labels and drop rows that cannot enter the index."""
    cfg = cfg or load_index_config()
    if df.empty:
        return df.assign(bucket=pd.Series(dtype="object"))

    df = df.copy()
    df["bucket"] = assign_bucket(df["advance_purchase_days"], cfg)
    return df[df["bucket"].notna() & df["total_fare"].notna()].reset_index(drop=True)


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


@dataclass
class IndexResult:
    """A computed index series plus the diagnostics needed to defend it."""

    series: pd.DataFrame          # query_date, index_value, coverage, n_cells
    base_period: list[str]
    base_cell_count: int
    dropped_cells: int
    notes: list[str]

    @property
    def base_value(self) -> float:
        return float(self.series.iloc[0]["index_value"]) if len(self.series) else float("nan")


def cell_means(df: pd.DataFrame, group_extra: list[str] | None = None) -> pd.DataFrame:
    """Mean fare per (day, route, airline, bucket) cell."""
    keys = ["query_date", "origin", "dest", "airline", "bucket"] + (group_extra or [])
    return (
        df.groupby(keys, sort=True, observed=True)["total_fare"]
        .mean()
        .reset_index()
        .rename(columns={"total_fare": "cell_mean"})
    )


def compute_index(
    df: pd.DataFrame,
    cfg: dict[str, Any] | None = None,
    weights: pd.DataFrame | None = None,
) -> IndexResult:
    """Build the daily APIx series from prepared fare rows.

    Cells with no observation in the base period are dropped entirely: without
    P_i0 there is no relative to compute, and silently substituting a later
    period's price would bias the whole series.
    """
    cfg = cfg or load_index_config()
    weights = basket_weights(cfg) if weights is None else weights
    notes: list[str] = []

    if df.empty:
        return IndexResult(
            pd.DataFrame(columns=["query_date", "index_value", "coverage", "n_cells"]),
            [], 0, 0, ["no data"],
        )

    means = cell_means(df)
    cell_keys = ["origin", "dest", "airline", "bucket"]

    # --- base period ------------------------------------------------------
    all_dates = sorted(means["query_date"].unique())
    n_base = max(1, int(cfg["base_period"]["days"]))
    base_dates = all_dates[:n_base]

    base = (
        means[means["query_date"].isin(base_dates)]
        .groupby(cell_keys, sort=False, observed=True)["cell_mean"]
        .mean()
        .reset_index()
        .rename(columns={"cell_mean": "base_mean"})
    )
    base = base[base["base_mean"] > 0]

    full_basket = len(weights)
    basket = weights.merge(base, on=cell_keys, how="inner")
    dropped = full_basket - len(basket)
    if dropped:
        notes.append(
            f"{dropped} of {full_basket} basket cells had no base-period "
            f"observation and are excluded from the index"
        )
    if basket.empty:
        return IndexResult(
            pd.DataFrame(columns=["query_date", "index_value", "coverage", "n_cells"]),
            [str(d) for d in base_dates], 0, dropped,
            notes + ["no basket cell had a base-period price"],
        )

    # --- price relatives --------------------------------------------------
    merged = means.merge(basket, on=cell_keys, how="inner")
    merged["relative"] = merged["cell_mean"] / merged["base_mean"]
    merged["weighted"] = merged["relative"] * merged["weight"]

    per_day = merged.groupby("query_date", sort=True, observed=True).agg(
        weighted_sum=("weighted", "sum"),
        observed_weight=("weight", "sum"),
        n_cells=("weight", "size"),
    ).reset_index()

    # Redistribute the weight of cells missing on a given day across the cells
    # present, so the index measures price rather than availability.
    basket_weight = basket["weight"].sum()
    per_day["coverage"] = per_day["observed_weight"] / basket_weight

    if cfg["weighting"].get("renormalise_on_missing", True):
        per_day["index_value"] = BASE_VALUE * (
            per_day["weighted_sum"] / per_day["observed_weight"]
        )
    else:
        per_day["index_value"] = BASE_VALUE * per_day["weighted_sum"] / basket_weight

    min_cov = float(cfg["weighting"]["min_coverage"])
    thin = per_day[per_day["coverage"] < min_cov]
    if len(thin):
        notes.append(
            f"{len(thin)} day(s) had basket coverage below {min_cov:.0%} and are "
            f"flagged (lowest {per_day['coverage'].min():.1%})"
        )

    series = per_day[
        ["query_date", "index_value", "coverage", "n_cells"]
    ].sort_values("query_date").reset_index(drop=True)

    return IndexResult(series, [str(d) for d in base_dates], len(basket), dropped, notes)


def aggregate_index(series: pd.DataFrame, freq: str, cfg: dict[str, Any] | None = None) -> pd.DataFrame:
    """Roll the daily series up to weekly or monthly.

    The mean of the daily index, not a recomputation from pooled cells: pooling
    would mix days with different availability and produce levels that are not
    comparable to the daily series.
    """
    cfg = cfg or load_index_config()
    if series.empty:
        return pd.DataFrame(columns=["period", "index_value", "n_days", "coverage"])

    agg_cfg = cfg["aggregation"]
    if freq == "weekly":
        rule, min_days = agg_cfg["weekly_label"], int(agg_cfg["min_days_weekly"])
    elif freq == "monthly":
        rule, min_days = agg_cfg["monthly_label"], int(agg_cfg["min_days_monthly"])
    else:
        raise ValueError(f"unsupported frequency: {freq!r}")

    s = series.copy()
    s["query_date"] = pd.to_datetime(s["query_date"])
    rolled = (
        s.set_index("query_date")
        .resample(rule)
        .agg(index_value=("index_value", "mean"),
             coverage=("coverage", "mean"),
             n_days=("index_value", "size"))
        .reset_index()
        .rename(columns={"query_date": "period"})
    )
    rolled = rolled[rolled["n_days"] >= min_days].reset_index(drop=True)
    rolled["period"] = rolled["period"].dt.strftime("%Y-%m-%d")
    return rolled


def route_subindex(
    df: pd.DataFrame, origin: str, dest: str, cfg: dict[str, Any] | None = None
) -> IndexResult:
    """APIx restricted to a single city-pair.

    Same formula, same base period logic; the route weight drops out (it is
    constant within the route) leaving airline x bucket weights, renormalised.
    """
    cfg = cfg or load_index_config()
    origin, dest = origin.upper(), dest.upper()

    weights = basket_weights(cfg)
    weights = weights[(weights["origin"] == origin) & (weights["dest"] == dest)].copy()
    if weights.empty:
        raise KeyError(f"{origin}-{dest} is not in the basket (config/routes.yaml)")
    weights["weight"] = weights["weight"] / weights["weight"].sum()

    subset = df[(df["origin"] == origin) & (df["dest"] == dest)]
    return compute_index(subset, cfg, weights)


def all_route_subindices(
    df: pd.DataFrame, cfg: dict[str, Any] | None = None
) -> dict[str, IndexResult]:
    cfg = cfg or load_index_config()
    out: dict[str, IndexResult] = {}
    for route in load_routes():
        try:
            out[route.key] = route_subindex(df, route.origin, route.dest, cfg)
        except KeyError:
            continue
    return out


# ---------------------------------------------------------------------------
# Lead-time elasticity
# ---------------------------------------------------------------------------


@dataclass
class Elasticity:
    """How much fares rise per day closer to departure, on one route."""

    origin: str
    dest: str
    pct_change_per_day: float   # % rise for each day CLOSER to departure
    log_slope: float            # OLS slope of ln(fare) on advance_purchase_days
    r_squared: float
    n_observations: int

    @property
    def route(self) -> str:
        return f"{self.origin}-{self.dest}"


def lead_time_elasticity(
    df: pd.DataFrame, cfg: dict[str, Any] | None = None
) -> list[Elasticity]:
    """Fit ln(fare) ~ advance_purchase_days per route.

    Log-linear because the booking curve is multiplicative — a fare rising 4% a
    day compounds. A straight line in rupees would under-read short lead times
    and over-read long ones.

    By default this is a WITHIN estimator: ln(fare) and advance_purchase_days
    are demeaned within each travel_date first, so the slope comes only from
    comparing quotes for the same departure at different lead times. Pooling
    across travel dates instead lets festival premiums leak into the lead-time
    coefficient, because a long-lead quote is by construction a quote for a
    later departure. On this panel that bias was worth ~30% of the estimate.
    See config/index.yaml.

    The slope is negative (fares fall as lead time grows), so the reported
    figure is exp(-slope) - 1: the percentage increase for each day closer to
    departure, which is the direction a traveller experiences.
    """
    cfg = cfg or load_index_config()
    e_cfg = cfg["elasticity"]
    max_apd = int(e_cfg["max_advance_purchase_days"])
    min_obs = int(e_cfg["min_observations"])
    control = bool(e_cfg.get("control_for_travel_date", True))

    results: list[Elasticity] = []
    if df.empty:
        return results

    usable = df[
        (df["advance_purchase_days"] >= 1)
        & (df["advance_purchase_days"] <= max_apd)
        & (df["total_fare"] > 0)
    ]

    for (origin, dest), grp in usable.groupby(["origin", "dest"], sort=True, observed=True):
        if len(grp) < min_obs:
            continue

        x = grp["advance_purchase_days"].to_numpy(dtype=float)
        y = np.log(grp["total_fare"].to_numpy(dtype=float))
        if np.ptp(x) == 0:
            continue

        if control:
            # Demean both sides within travel_date, then fit through the
            # origin — the standard fixed-effects transform.
            frame = pd.DataFrame({"x": x, "y": y, "td": grp["travel_date"].to_numpy()})
            frame["x"] -= frame.groupby("td")["x"].transform("mean")
            frame["y"] -= frame.groupby("td")["y"].transform("mean")
            # Departures observed at only one lead time carry no information
            # once demeaned; they become exact zeros and are dropped.
            frame = frame[frame["x"].abs() > 1e-9]
            if len(frame) < min_obs:
                continue
            xd, yd = frame["x"].to_numpy(), frame["y"].to_numpy()
            slope = float((xd * yd).sum() / (xd * xd).sum())
            ss_res = float(((yd - slope * xd) ** 2).sum())
            ss_tot = float((yd ** 2).sum())
            n_used = len(frame)
        else:
            slope, intercept = np.polyfit(x, y, 1)
            slope = float(slope)
            ss_res = float(((y - (slope * x + intercept)) ** 2).sum())
            ss_tot = float(((y - y.mean()) ** 2).sum())
            n_used = len(grp)

        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

        results.append(Elasticity(
            origin=str(origin),
            dest=str(dest),
            pct_change_per_day=float((np.exp(-slope) - 1.0) * 100.0),
            log_slope=slope,
            r_squared=float(r2),
            n_observations=int(n_used),
        ))
    return results


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_CREATE_TABLES = """
DROP TABLE IF EXISTS apix_index;
CREATE TABLE apix_index (
    period_type TEXT    NOT NULL,   -- 'daily' | 'weekly' | 'monthly'
    period      TEXT    NOT NULL,
    index_value REAL    NOT NULL,
    coverage    REAL,
    n_cells     INTEGER,
    PRIMARY KEY (period_type, period)
);

DROP TABLE IF EXISTS apix_route_index;
CREATE TABLE apix_route_index (
    origin      TEXT NOT NULL,
    dest        TEXT NOT NULL,
    period_type TEXT NOT NULL,
    period      TEXT NOT NULL,
    index_value REAL NOT NULL,
    coverage    REAL,
    PRIMARY KEY (origin, dest, period_type, period)
);

DROP TABLE IF EXISTS apix_elasticity;
CREATE TABLE apix_elasticity (
    origin              TEXT NOT NULL,
    dest                TEXT NOT NULL,
    pct_change_per_day  REAL NOT NULL,
    log_slope           REAL NOT NULL,
    r_squared           REAL NOT NULL,
    n_observations      INTEGER NOT NULL,
    PRIMARY KEY (origin, dest)
);

DROP TABLE IF EXISTS apix_meta;
CREATE TABLE apix_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def build_and_store(
    db_path: str | Path = db.DEFAULT_DB_PATH, cfg: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Compute every series and persist it. Returns a summary."""
    cfg = cfg or load_index_config()
    df = load_clean_fares(db_path, cfg)

    overall = compute_index(df, cfg)
    weekly = aggregate_index(overall.series, "weekly", cfg)
    monthly = aggregate_index(overall.series, "monthly", cfg)
    routes = all_route_subindices(df, cfg)
    elasticities = lead_time_elasticity(df, cfg)

    with db.connect(db_path) as conn:
        conn.executescript(_CREATE_TABLES)

        rows = [
            ("daily", str(r.query_date), float(r.index_value),
             float(r.coverage), int(r.n_cells))
            for r in overall.series.itertuples()
        ]
        rows += [("weekly", str(r.period), float(r.index_value), float(r.coverage), None)
                 for r in weekly.itertuples()]
        rows += [("monthly", str(r.period), float(r.index_value), float(r.coverage), None)
                 for r in monthly.itertuples()]
        conn.executemany(
            "INSERT INTO apix_index (period_type, period, index_value, coverage, n_cells) "
            "VALUES (?, ?, ?, ?, ?)", rows,
        )

        route_rows = []
        for key, res in routes.items():
            origin, dest = key.split("-")
            route_rows += [
                (origin, dest, "daily", str(r.query_date),
                 float(r.index_value), float(r.coverage))
                for r in res.series.itertuples()
            ]
        conn.executemany(
            "INSERT INTO apix_route_index (origin, dest, period_type, period, "
            "index_value, coverage) VALUES (?, ?, ?, ?, ?, ?)", route_rows,
        )

        conn.executemany(
            "INSERT INTO apix_elasticity (origin, dest, pct_change_per_day, "
            "log_slope, r_squared, n_observations) VALUES (?, ?, ?, ?, ?, ?)",
            [(e.origin, e.dest, e.pct_change_per_day, e.log_slope,
              e.r_squared, e.n_observations) for e in elasticities],
        )

        conn.executemany(
            "INSERT INTO apix_meta (key, value) VALUES (?, ?)",
            [
                ("index_version", str(cfg["index_version"])),
                ("base_period", ", ".join(overall.base_period)),
                ("base_value", str(BASE_VALUE)),
                ("basket_cells", str(overall.base_cell_count)),
                ("dropped_cells", str(overall.dropped_cells)),
                ("built_at", db.utc_now_iso()),
                ("notes", " | ".join(overall.notes) or "none"),
            ],
        )

    return {
        "daily_points": len(overall.series),
        "weekly_points": len(weekly),
        "monthly_points": len(monthly),
        "routes": len(routes),
        "elasticities": len(elasticities),
        "base_period": overall.base_period,
        "basket_cells": overall.base_cell_count,
        "notes": overall.notes,
        "overall": overall,
        "elasticity_list": elasticities,
    }


# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the APIx index series")
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args(argv)

    summary = build_and_store(args.db)
    overall: IndexResult = summary["overall"]

    print("=" * 70)
    print("APIx — Laspeyres fixed-basket airfare price index")
    print("=" * 70)
    print(f"  base period      {', '.join(summary['base_period'])}  (= {BASE_VALUE:.0f})")
    print(f"  basket cells     {summary['basket_cells']}")
    print(f"  daily points     {summary['daily_points']}")
    print(f"  weekly points    {summary['weekly_points']}")
    print(f"  monthly points   {summary['monthly_points']}")
    print(f"  route subindices {summary['routes']}")
    for n in summary["notes"]:
        print(f"  note: {n}")

    if args.show and len(overall.series):
        s = overall.series
        print(f"\n  {'query_date':<12} {'APIx':>8}  {'coverage':>9}")
        for r in s.itertuples():
            bar = "#" * int(max(0.0, (r.index_value - 90)) / 2)
            print(f"  {str(r.query_date):<12} {r.index_value:>8.2f}  "
                  f"{r.coverage:>8.1%}  {bar}")
        print(f"\n  min {s['index_value'].min():.2f}   "
              f"max {s['index_value'].max():.2f}   "
              f"last {s['index_value'].iloc[-1]:.2f}")

        print("\n  Lead-time elasticity (% fare rise per day closer to departure):")
        for e in sorted(summary["elasticity_list"],
                        key=lambda x: -x.pct_change_per_day):
            print(f"    {e.route:<9} {e.pct_change_per_day:>6.2f}%/day   "
                  f"R2={e.r_squared:.3f}  n={e.n_observations:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
