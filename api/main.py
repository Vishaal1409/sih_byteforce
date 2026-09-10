"""api/main.py — FastAPI service exposing APIx.

Thin by design: Phases 5 and 6 compute everything and persist it, so these
endpoints mostly read tables. That keeps responses fast and means the API
cannot disagree with the dashboard about what the index says.

Every response carries a `provenance` block stating whether the numbers behind
it come from real or simulated fares (CLAUDE.md ground rule 5). It is a
required field on every model, not an optional extra, so a caller cannot
receive APIx numbers without also receiving the caveat.

Run:
    uvicorn api.main:app --reload
    open http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Path as PathParam, Query
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from config_loader import load_routes  # noqa: E402
from etl.clean import latest_quality_report  # noqa: E402
from index.backtest import latest_backtest, load_backtest_config  # noqa: E402
from scraper.live_scraper import scrape_and_store, scrape_route  # noqa: E402

PeriodType = Literal["daily", "weekly", "monthly"]

DB_PATH = db.DEFAULT_DB_PATH

app = FastAPI(
    title="APIx — Real-time Airfare Price Index (India)",
    version="0.1.0",
    description=(
        "Prototype airfare price index for SIH26056 (MoSPI).\n\n"
        "**The fares behind these numbers are simulated.** Every response "
        "carries a `provenance` block saying so. The index mathematics, "
        "cleaning pipeline and collection machinery are real and would run "
        "unchanged on live fares — see `docs/methodology.md`.\n\n"
        "`POST /scrape/live` genuinely attempts a real scrape, respects "
        "`robots.txt`, and returns clearly-labelled simulated data when the "
        "target blocks it."
    ),
)


# ---------------------------------------------------------------------------
# Provenance — attached to every response
# ---------------------------------------------------------------------------


class Provenance(BaseModel):
    """Where the numbers came from. Present on every response."""

    is_real_data: bool = Field(
        ..., description="True only if any underlying fare was genuinely scraped."
    )
    row_counts_by_source: dict[str, int] = Field(
        default_factory=dict,
        description="Rows behind the index, by provenance tag.",
    )
    warning: str = Field(..., description="Plain-English caveat, safe to display verbatim.")


SIMULATED_WARNING = (
    "SIMULATED DATA — these figures are computed from synthetically generated "
    "fares, not real airline prices. The methodology is real; the inputs are "
    "not. See docs/methodology.md."
)


def _provenance() -> Provenance:
    try:
        with db.connect(DB_PATH) as conn:
            counts = db.counts_by_source(conn)
    except Exception:
        counts = {}

    real = counts.get(db.SOURCE_LIVE, 0)
    if real:
        warning = (
            f"MIXED DATA — {real:,} genuinely scraped row(s) alongside simulated "
            f"ones. Rows are tagged by source; see docs/methodology.md."
        )
    else:
        warning = SIMULATED_WARNING
    return Provenance(
        is_real_data=bool(real), row_counts_by_source=counts, warning=warning
    )


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class IndexPoint(BaseModel):
    period: str = Field(..., description="ISO date. For weekly/monthly, the period start.")
    index_value: float
    coverage: float | None = Field(
        None, description="Share of basket weight observed. 1.0 = full coverage."
    )


class IndexResponse(BaseModel):
    period_type: PeriodType
    base_period: str
    base_value: float
    current: IndexPoint | None
    change_since_base_pct: float | None
    points: int
    series: list[IndexPoint]
    data_quality: dict[str, Any] | None = Field(
        None, description="Latest ETL summary: rows in, rows dropped, reasons."
    )
    provenance: Provenance


class RouteIndexResponse(BaseModel):
    origin: str
    dest: str
    route: str
    name: str
    period_type: PeriodType
    current: IndexPoint | None
    change_since_base_pct: float | None
    points: int
    series: list[IndexPoint]
    provenance: Provenance


class ElasticityItem(BaseModel):
    route: str
    origin: str
    dest: str
    pct_change_per_day: float = Field(
        ..., description="Percentage fare rise for each day closer to departure."
    )
    log_slope: float
    r_squared: float
    n_observations: int


class ElasticityResponse(BaseModel):
    method: str
    routes: list[ElasticityItem]
    provenance: Provenance


class BacktestResponse(BaseModel):
    reference_label: str
    reference_is_real_data: bool
    correlation: float
    rank_correlation: float
    mape_pct: float
    rmse: float
    n_days: int
    start_date: str
    end_date: str
    apix_change_pct: float
    reference_change_pct: float
    series: list[dict[str, Any]]
    provenance: Provenance


class ScrapeRequest(BaseModel):
    origin: str = Field("DEL", min_length=3, max_length=3, examples=["DEL"])
    dest: str = Field("BOM", min_length=3, max_length=3, examples=["BOM"])
    days_ahead: int = Field(
        35, ge=1, le=45, description="Travel date, as days from today."
    )
    store: bool = Field(True, description="Write the resulting quotes to the database.")


class ScrapedQuote(BaseModel):
    source: str
    airline: str
    origin: str
    dest: str
    travel_date: str
    total_fare: float | None
    is_available: bool


class ScrapeResponse(BaseModel):
    status: str = Field(..., description="'live', 'fallback_simulated' or 'blocked'.")
    is_real: bool = Field(
        ..., description="True ONLY if these are genuinely scraped fares."
    )
    headline: str = Field(..., description="Display verbatim — states if data is simulated.")
    target: str
    url: str
    reason: str
    attempts: int
    elapsed_seconds: float
    quotes: list[ScrapedQuote]
    provenance: Provenance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_table(conn, name: str) -> None:
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    if not exists:
        raise HTTPException(
            status_code=503,
            detail=(
                f"'{name}' has not been built yet. Run the pipeline first: "
                f"python -m scraper.simulator backfill && python -m etl.clean "
                f"&& python -m index.apix && python -m index.backtest"
            ),
        )


def _change_pct(series: list[IndexPoint]) -> float | None:
    if len(series) < 2 or series[0].index_value == 0:
        return None
    return round((series[-1].index_value / series[0].index_value - 1.0) * 100.0, 4)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/index", response_model=IndexResponse, tags=["index"],
         summary="Current and historical APIx")
def get_index(
    period: PeriodType = Query("daily", description="Aggregation level."),
) -> IndexResponse:
    """The headline index: current level plus the full history."""
    with db.connect(DB_PATH) as conn:
        _require_table(conn, "apix_index")
        rows = conn.execute(
            "SELECT period, index_value, coverage FROM apix_index "
            "WHERE period_type = ? ORDER BY period",
            (period,),
        ).fetchall()
        meta = dict(conn.execute("SELECT key, value FROM apix_meta").fetchall())

    if not rows:
        raise HTTPException(404, f"no {period} index points available")

    series = [
        IndexPoint(period=r["period"], index_value=round(r["index_value"], 4),
                   coverage=r["coverage"])
        for r in rows
    ]
    return IndexResponse(
        period_type=period,
        base_period=meta.get("base_period", ""),
        base_value=float(meta.get("base_value", 100.0)),
        current=series[-1],
        change_since_base_pct=_change_pct(series),
        points=len(series),
        series=series,
        data_quality=latest_quality_report(DB_PATH),
        provenance=_provenance(),
    )


@app.get("/index/elasticity", response_model=ElasticityResponse, tags=["index"],
         summary="Lead-time elasticity by route")
def get_elasticity() -> ElasticityResponse:
    """How much fares rise for each day closer to departure, per route."""
    with db.connect(DB_PATH) as conn:
        _require_table(conn, "apix_elasticity")
        rows = conn.execute(
            "SELECT origin, dest, pct_change_per_day, log_slope, r_squared, "
            "n_observations FROM apix_elasticity ORDER BY pct_change_per_day DESC"
        ).fetchall()

    if not rows:
        raise HTTPException(404, "no elasticity results available")

    return ElasticityResponse(
        method=(
            "OLS of ln(total_fare) on advance_purchase_days with travel-date "
            "fixed effects, per route; reported as exp(-slope) - 1"
        ),
        routes=[
            ElasticityItem(
                route=f"{r['origin']}-{r['dest']}",
                origin=r["origin"], dest=r["dest"],
                pct_change_per_day=round(r["pct_change_per_day"], 4),
                log_slope=round(r["log_slope"], 6),
                r_squared=round(r["r_squared"], 4),
                n_observations=r["n_observations"],
            )
            for r in rows
        ],
        provenance=_provenance(),
    )


@app.get("/index/route/{origin}/{dest}", response_model=RouteIndexResponse,
         tags=["index"], summary="APIx for a single city-pair")
def get_route_index(
    origin: str = PathParam(..., min_length=3, max_length=3, examples=["DEL"]),
    dest: str = PathParam(..., min_length=3, max_length=3, examples=["BOM"]),
    period: PeriodType = Query("daily"),
) -> RouteIndexResponse:
    """Route sub-index. Each route is 100 at its own base period."""
    origin, dest = origin.upper(), dest.upper()
    route_names = {r.key: r.name for r in load_routes()}
    key = f"{origin}-{dest}"
    if key not in route_names:
        raise HTTPException(
            404,
            f"{key} is not in the basket. Available: {', '.join(sorted(route_names))}",
        )

    with db.connect(DB_PATH) as conn:
        _require_table(conn, "apix_route_index")
        rows = conn.execute(
            "SELECT period, index_value, coverage FROM apix_route_index "
            "WHERE origin = ? AND dest = ? AND period_type = ? ORDER BY period",
            (origin, dest, period),
        ).fetchall()

    if not rows:
        raise HTTPException(404, f"no {period} index points for {key}")

    series = [
        IndexPoint(period=r["period"], index_value=round(r["index_value"], 4),
                   coverage=r["coverage"])
        for r in rows
    ]
    return RouteIndexResponse(
        origin=origin, dest=dest, route=key, name=route_names[key],
        period_type=period,
        current=series[-1],
        change_since_base_pct=_change_pct(series),
        points=len(series),
        series=series,
        provenance=_provenance(),
    )


@app.get("/backtest/results", response_model=BacktestResponse, tags=["backtest"],
         summary="APIx vs the reference series")
def get_backtest() -> BacktestResponse:
    """Correlation, MAPE and the overlay series.

    The reference is a clearly-labelled synthetic stand-in — DGCA publishes
    monthly aggregates, not the daily series this would need.
    """
    stored = latest_backtest(DB_PATH)
    if stored is None:
        raise HTTPException(
            503,
            "no backtest has been run yet. Run: python -m index.backtest",
        )

    m = stored["metrics"]
    ref_cfg = load_backtest_config()["reference"]
    return BacktestResponse(
        reference_label=m.get("reference_label", ref_cfg["label"]),
        reference_is_real_data=False,
        correlation=float(m["correlation"]),
        rank_correlation=float(m["rank_correlation"]),
        mape_pct=float(m["mape_pct"]),
        rmse=float(m["rmse"]),
        n_days=int(float(m["n_days"])),
        start_date=m["start_date"],
        end_date=m["end_date"],
        apix_change_pct=float(m["apix_change_pct"]),
        reference_change_pct=float(m["reference_change_pct"]),
        series=stored["series"],
        provenance=_provenance(),
    )


@app.post("/scrape/live", response_model=ScrapeResponse, tags=["scraper"],
          summary="Trigger a real scrape for one route")
def post_scrape_live(req: ScrapeRequest) -> ScrapeResponse:
    """Attempt a genuine live scrape, falling back to labelled simulated data.

    Defined as a normal `def` so FastAPI runs it in a worker thread — the
    scraper is blocking and can take several seconds.

    This never returns an error for a failed scrape: a block is an expected
    outcome, and the caller gets `is_real=False` with an explanatory
    `headline` instead. Check `is_real` before presenting anything as real.
    """
    travel_date = date.today() + timedelta(days=req.days_ahead)
    try:
        if req.store:
            result = scrape_and_store(
                req.origin, req.dest, travel_date, db_path=DB_PATH
            )
        else:
            result = scrape_route(req.origin, req.dest, travel_date)
    except KeyError as exc:
        # Raised when the route is not in the basket, so the fallback
        # simulator has no parameters for it.
        raise HTTPException(404, str(exc)) from exc

    summary = result.summary()
    return ScrapeResponse(
        status=summary["status"],
        is_real=summary["is_real"],
        headline=summary["headline"],
        target=summary["target"],
        url=summary["url"],
        reason=summary["reason"],
        attempts=summary["attempts"],
        elapsed_seconds=summary["elapsed_seconds"],
        quotes=[
            ScrapedQuote(
                source=q.source, airline=q.airline, origin=q.origin, dest=q.dest,
                travel_date=q.travel_date, total_fare=q.total_fare,
                is_available=bool(q.is_available),
            )
            for q in result.quotes
        ],
        provenance=_provenance(),
    )
