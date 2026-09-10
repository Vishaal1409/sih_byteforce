"""dashboard/app.py — the APIx demo dashboard.

Reads everything through the Phase 7 API rather than the database directly, so
what is on screen is exactly what the API serves. If the API is not running the
page says so, with the command to start it, instead of rendering blank charts.

Run both together with:   python run.py
Or the dashboard alone:   streamlit run dashboard/app.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard import theme  # noqa: E402

API_BASE = os.environ.get("APIX_API_BASE", "http://127.0.0.1:8000")
REQUEST_TIMEOUT = 20
SCRAPE_TIMEOUT = 180

#: Plotly toolbar: keep the useful controls, drop the clutter, and never
#: show the Plotly logo in a government-facing demo.
PLOTLY_CONFIG = {
    "displaylogo": False,
    "displayModeBar": False,
    "responsive": True,
}

st.set_page_config(
    page_title="APIx · Airfare Price Index — Live",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="collapsed",
    menu_items={
        "about": (
            "APIx — Real-time Airfare Price Index for India (SIH26056, MoSPI). "
            "Demo runs on simulated fare data; see docs/methodology.md."
        )
    },
)

# Dark aviation theme: CSS custom properties for the page, and a matching
# Plotly template so charts inherit the same palette instead of restating it.
st.markdown(theme.inject_theme(), unsafe_allow_html=True)
theme.register_plotly_theme()


# ---------------------------------------------------------------------------
# API access
# ---------------------------------------------------------------------------


class ApiUnavailable(RuntimeError):
    pass


@st.cache_data(ttl=60, show_spinner=False)
def api_get(path: str, **params) -> dict:
    try:
        r = requests.get(f"{API_BASE}{path}", params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise ApiUnavailable(str(exc)) from exc
    if r.status_code == 503:
        raise ApiUnavailable(r.json().get("detail", "pipeline not built"))
    r.raise_for_status()
    return r.json()


def api_post_scrape(origin: str, dest: str, days_ahead: int) -> dict:
    r = requests.post(
        f"{API_BASE}/scrape/live",
        json={"origin": origin, "dest": dest, "days_ahead": days_ahead, "store": True},
        timeout=SCRAPE_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Chrome
# ---------------------------------------------------------------------------

PALETTE = {
    "apix": theme.PALETTE["accent"],
    "reference": theme.PALETTE["warn"],
    "accent": theme.PALETTE["blue"],
}


def simulated_banner(provenance: dict | None = None) -> None:
    """The persistent, unmissable provenance banner (CLAUDE.md ground rule 5)."""
    counts = (provenance or {}).get("row_counts_by_source", {})
    detail = ", ".join(f"{k}: {v:,}" for k, v in sorted(counts.items())) or "—"
    st.markdown(
        f"""
        <div style="background:#8a1c1c;color:#fff;padding:14px 18px;
                    border-radius:6px;margin-bottom:18px;font-size:15px;
                    line-height:1.5;">
          <strong>⚠ SIMULATED DEMO DATA — NOT REAL AIRLINE FARES.</strong><br>
          Every fare behind these charts is synthetically generated. The index
          methodology, cleaning pipeline and scraper are real and would run
          unchanged on live data. See
          <code>docs/methodology.md</code> for the method and the real
          data-sourcing plan.<br>
          <span style="opacity:.85;font-size:13px;">Rows by source — {detail}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def api_down(exc: Exception) -> None:
    st.error(
        f"**Cannot reach the APIx API at `{API_BASE}`.**\n\n"
        f"`{exc}`\n\n"
        "Start everything with one command from the repo root:\n\n"
        "```\npython run.py\n```\n\n"
        "Or, if the pipeline has never been built:\n\n"
        "```\npython -m scraper.simulator backfill\n"
        "python -m etl.clean\n"
        "python -m index.apix\n"
        "python -m index.backtest\n```"
    )
    st.stop()


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def section_headline(index_data: dict, backtest: dict | None) -> None:
    current = index_data.get("current") or {}
    change = index_data.get("change_since_base_pct")

    cols = st.columns(4)
    cols[0].metric(
        "APIx (latest)",
        f"{current.get('index_value', float('nan')):.2f}",
        f"{change:+.2f}% since base" if change is not None else None,
    )
    cols[1].metric("Base period", index_data.get("base_period", "—"), "= 100")
    cols[2].metric("Daily observations", f"{index_data.get('points', 0)}")
    if backtest:
        cols[3].metric(
            "Backtest correlation",
            f"{backtest['correlation']:.3f}",
            f"MAPE {backtest['mape_pct']:.2f}%",
        )
    else:
        cols[3].metric("Backtest correlation", "—")


def section_trend() -> None:
    st.subheader("1 · APIx trend")
    st.caption(
        "Laspeyres fixed-basket index over 8 routes × 4 airlines × 6 booking "
        "windows. Base period = 100. Because the basket weights are fixed, a "
        "move here is a price move, not a change in what happened to be observed."
    )

    period = st.radio(
        "Aggregation", ["daily", "weekly", "monthly"],
        horizontal=True, key="trend_period",
    )
    data = api_get("/index", period=period)
    df = pd.DataFrame(data["series"])
    if df.empty:
        st.info(f"No {period} points available yet.")
        return

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["period"], y=df["index_value"], mode="lines+markers",
        name="APIx", line=dict(color=PALETTE["apix"], width=2.5),
        hovertemplate="%{x}<br>APIx %{y:.2f}<extra></extra>",
    ))
    fig.add_hline(
        y=100, line_dash="dot", line_color="#999",
        annotation_text="base = 100", annotation_position="bottom right",
    )
    fig.update_layout(
        height=420, margin=dict(t=30, b=40),
        xaxis_title="Query date (date the fare was observed)",
        yaxis_title="APIx",
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True, theme=None, config=PLOTLY_CONFIG)

    lo, hi = df["index_value"].min(), df["index_value"].max()
    st.caption(
        f"{len(df)} {period} points · range {lo:.2f} – {hi:.2f} · "
        f"latest {df['index_value'].iloc[-1]:.2f}"
    )


@st.cache_data(ttl=60, show_spinner=False)
def route_matrix() -> pd.DataFrame:
    """Route sub-indices as a route × date matrix for the heatmap."""
    from config_loader import load_routes

    frames = []
    for route in load_routes():
        try:
            d = api_get(f"/index/route/{route.origin}/{route.dest}")
        except Exception:
            continue
        sub = pd.DataFrame(d["series"])
        if sub.empty:
            continue
        sub["route"] = f"{route.key}  ({route.name})"
        frames.append(sub[["route", "period", "index_value"]])

    if not frames:
        return pd.DataFrame()
    return (
        pd.concat(frames, ignore_index=True)
        .pivot(index="route", columns="period", values="index_value")
        .sort_index()
    )


def section_heatmap() -> None:
    st.subheader("2 · Sector heatmap — fare index by city-pair")
    st.caption(
        "Each row is a route's own sub-index, 100 at its base period. Read "
        "across a row to see that sector heat up or cool; read down a column "
        "to compare sectors on a given day."
    )

    matrix = route_matrix()
    if matrix.empty:
        st.info("No route sub-indices available yet.")
        return

    fig = px.imshow(
        matrix,
        color_continuous_scale="RdYlGn_r",
        origin="lower",
        aspect="auto",
        labels=dict(x="Query date", y="Route", color="Index"),
    )
    fig.update_layout(height=420, margin=dict(t=30, b=40))
    fig.update_xaxes(tickangle=-45)
    st.plotly_chart(fig, use_container_width=True, theme=None, config=PLOTLY_CONFIG)

    latest = matrix.iloc[:, -1].sort_values(ascending=False)
    st.caption(
        f"Hottest sector on {matrix.columns[-1]}: **{latest.index[0]}** at "
        f"{latest.iloc[0]:.1f} · coolest: **{latest.index[-1]}** at {latest.iloc[-1]:.1f}"
    )


def section_elasticity() -> None:
    st.subheader("3 · Lead-time elasticity")
    st.caption(
        "How much a fare rises for each day closer to departure, per route. "
        "Fitted as ln(fare) on advance-purchase days with travel-date fixed "
        "effects, so festival premiums are not mistaken for a lead-time effect."
    )

    data = api_get("/index/elasticity")
    df = pd.DataFrame(data["routes"])
    if df.empty:
        st.info("No elasticity results available yet.")
        return

    df = df.sort_values("pct_change_per_day")
    fig = go.Figure(go.Bar(
        x=df["pct_change_per_day"], y=df["route"], orientation="h",
        marker_color=PALETTE["accent"],
        text=[f"{v:.2f}%/day" for v in df["pct_change_per_day"]],
        textposition="outside",
        hovertemplate="%{y}<br>%{x:.3f}%% per day<extra></extra>",
    ))
    fig.update_layout(
        height=420, margin=dict(t=30, b=40, r=80),
        xaxis_title="% fare rise per day closer to departure",
        yaxis_title="",
    )
    st.plotly_chart(fig, use_container_width=True, theme=None, config=PLOTLY_CONFIG)

    with st.expander("Fit detail"):
        st.dataframe(
            df.sort_values("pct_change_per_day", ascending=False)[
                ["route", "pct_change_per_day", "r_squared", "n_observations"]
            ].rename(columns={
                "route": "Route",
                "pct_change_per_day": "% per day",
                "r_squared": "R²",
                "n_observations": "Observations",
            }),
            hide_index=True,
            use_container_width=True,
        )
        st.caption(f"Method: {data['method']}")


def section_backtest(backtest: dict | None) -> None:
    st.subheader("4 · Backtest — APIx vs reference series")
    if backtest is None:
        st.info("No backtest has been run yet.")
        return

    st.warning(
        f"**The reference series is a synthetic stand-in, not real DGCA data.** "
        f"DGCA publishes monthly aggregates, not the daily series this "
        f"comparison needs. It is computed as an *unweighted* market mean — "
        f"deliberately a different estimator from APIx, so the comparison "
        f"tests something. Label: _{backtest['reference_label']}_",
        icon="⚠",
    )

    cols = st.columns(4)
    cols[0].metric("Correlation (r)", f"{backtest['correlation']:.4f}")
    cols[1].metric("Rank correlation", f"{backtest['rank_correlation']:.4f}")
    cols[2].metric("MAPE", f"{backtest['mape_pct']:.2f}%")
    cols[3].metric("Window", f"{backtest['n_days']} days")

    df = pd.DataFrame(backtest["series"])
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["query_date"], y=df["index_value"], mode="lines+markers",
        name="APIx (weighted index)", line=dict(color=PALETTE["apix"], width=2.5),
    ))
    fig.add_trace(go.Scatter(
        x=df["query_date"], y=df["reference_value"], mode="lines+markers",
        name="Reference (synthetic, unweighted)",
        line=dict(color=PALETTE["reference"], width=2, dash="dash"),
    ))
    fig.update_layout(
        height=420, margin=dict(t=30, b=40),
        xaxis_title="Query date", yaxis_title="Index (first common date = 100)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    st.plotly_chart(fig, use_container_width=True, theme=None, config=PLOTLY_CONFIG)

    st.info(
        f"**The gap is the point.** APIx moves "
        f"{backtest['apix_change_pct']:+.2f}% over the window; the naive "
        f"unweighted average moves {backtest['reference_change_pct']:+.2f}%. "
        f"Most of that difference is composition, not price: the 31–45 day "
        f"booking window supplies 34% of raw observations but carries 10% of "
        f"index weight, and it is the only window whose travel dates reach the "
        f"October festivals. A fixed basket removes that; a simple average "
        f"cannot. See `docs/methodology.md` §7a.",
        icon="💡",
    )


def section_live_scrape() -> None:
    st.subheader("5 · Live scrape")
    st.caption(
        "Runs the real Phase 3 scraper against Skyscanner through "
        "`POST /scrape/live`. It checks robots.txt first, rate-limits itself, "
        "and never attempts to bypass a block. If the target refuses, it "
        "returns clearly-labelled simulated data instead of an error."
    )

    from config_loader import load_routes

    routes = load_routes()
    cols = st.columns([3, 2, 2])
    labels = [f"{r.key} — {r.name}" for r in routes]
    picked = cols[0].selectbox("Route", labels, key="scrape_route")
    days_ahead = cols[1].number_input(
        "Days ahead", min_value=1, max_value=45, value=30, key="scrape_days"
    )
    cols[2].markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    go_clicked = cols[2].button("Run live scrape", type="primary", use_container_width=True)

    if go_clicked:
        route = routes[labels.index(picked)]
        with st.spinner(f"Scraping {route.key} … (robots check, rate limit, retries)"):
            try:
                result = api_post_scrape(route.origin, route.dest, int(days_ahead))
            except requests.RequestException as exc:
                st.error(f"Could not reach the API: {exc}")
                return
        st.session_state["scrape_result"] = result
        api_get.clear()
        route_matrix.clear()

    result = st.session_state.get("scrape_result")
    if not result:
        return

    if result["is_real"]:
        st.success(f"**REAL DATA** — {result['headline']}", icon="✅")
    else:
        st.error(
            f"**FELL BACK TO SIMULATED DATA — these are NOT real quotes.**\n\n"
            f"{result['headline']}",
            icon="⚠",
        )

    meta = st.columns(4)
    meta[0].metric("Status", result["status"])
    meta[1].metric("Real data?", "yes" if result["is_real"] else "no")
    meta[2].metric("Attempts", result["attempts"])
    meta[3].metric("Elapsed", f"{result['elapsed_seconds']:.1f}s")

    st.caption(f"Target: {result['target']} · `{result['url']}`")
    st.caption(f"Reason: {result['reason']}")

    if result["quotes"]:
        quotes = pd.DataFrame(result["quotes"])
        st.markdown("**Rows written to the database:**")
        st.dataframe(
            quotes.rename(columns={
                "source": "Source (provenance)", "airline": "Airline",
                "origin": "From", "dest": "To", "travel_date": "Travel date",
                "total_fare": "Total fare (₹)", "is_available": "Available",
            }),
            hide_index=True,
            use_container_width=True,
        )
        st.caption(
            "The `Source (provenance)` column is the audit trail: "
            "`live` = genuinely scraped, `fallback_simulated` = the scraper was "
            "blocked and the simulator answered instead."
        )


def section_data_quality(index_data: dict) -> None:
    dq = index_data.get("data_quality")
    if not dq:
        return
    with st.expander("Data quality — what the ETL dropped and why"):
        cols = st.columns(3)
        cols[0].metric("Rows in", f"{dq['rows_in']:,}")
        cols[1].metric("Rows out", f"{dq['rows_out']:,}")
        cols[2].metric("Retained", f"{dq['retention_rate']:.1%}")

        if dq.get("dropped"):
            st.markdown("**Dropped**")
            st.dataframe(
                pd.DataFrame(
                    sorted(dq["dropped"].items(), key=lambda kv: -kv[1]),
                    columns=["Reason", "Rows"],
                ),
                hide_index=True, use_container_width=True,
            )
        if dq.get("flagged"):
            st.markdown("**Flagged (kept, not dropped)**")
            st.dataframe(
                pd.DataFrame(list(dq["flagged"].items()), columns=["Reason", "Rows"]),
                hide_index=True, use_container_width=True,
            )
        for note in dq.get("notes", []):
            st.caption(f"· {note}")


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def main() -> None:
    st.title("APIx — Real-time Airfare Price Index for India")
    st.caption("SIH26056 · Ministry of Statistics and Programme Implementation · prototype")

    try:
        index_data = api_get("/index", period="daily")
    except ApiUnavailable as exc:
        simulated_banner()
        api_down(exc)
        return
    except requests.RequestException as exc:
        simulated_banner()
        api_down(exc)
        return

    simulated_banner(index_data.get("provenance"))

    try:
        backtest = api_get("/backtest/results")
    except Exception:
        backtest = None

    section_headline(index_data, backtest)
    st.divider()
    section_trend()
    st.divider()
    section_heatmap()
    st.divider()
    section_elasticity()
    st.divider()
    section_backtest(backtest)
    st.divider()
    section_live_scrape()
    st.divider()
    section_data_quality(index_data)

    st.caption(
        f"Served from {API_BASE} · methodology: `docs/methodology.md` · "
        f"anti-bot approach: `docs/anti-bot-strategy.md`"
    )


main()
