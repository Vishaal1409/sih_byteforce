# APIx Architecture

**Airfare Price Index for India — SIH26056 (MoSPI)**

How the system fits together, and an honest account of which parts run on real
data and which do not.

---

## 1. Data flow

```mermaid
flowchart TB
    subgraph COLLECT["1 · Collection"]
        direction LR
        SIM["scraper/simulator.py<br/><b>SIMULATED</b><br/>synthetic fare model"]
        LIVE["scraper/live_scraper.py<br/><b>REAL</b><br/>Playwright + robots.txt"]
    end

    subgraph CFG["config/ — every weight and threshold"]
        direction LR
        C1["routes.yaml<br/>airlines.yaml"]
        C2["simulator.yaml<br/>scraper.yaml"]
        C3["etl.yaml<br/>index.yaml<br/>backtest.yaml"]
    end

    SCHEMA["<b>One schema for every row</b><br/>db.py · FareQuote<br/>source: simulated / live / fallback_simulated"]

    RAW[("fare_quotes<br/>50,400 rows")]
    CLEAN[("fares_clean<br/>48,688 rows")]
    QUAL[("data_quality_log")]

    ETL["etl/clean.py<br/>dedupe · bounds · peer-ratio outliers"]
    IDX["index/apix.py<br/>Laspeyres fixed basket"]
    BT["index/backtest.py<br/>correlation · MAPE"]

    OUT[("apix_index<br/>apix_route_index<br/>apix_elasticity<br/>apix_backtest")]

    API["api/main.py — FastAPI<br/>every response carries <b>provenance</b>"]
    DASH["dashboard/app.py — Streamlit<br/>persistent SIMULATED banner"]

    SIM --> SCHEMA
    LIVE -- "blocked → fallback_simulated" --> SIM
    LIVE --> SCHEMA
    SCHEMA --> RAW
    RAW --> ETL
    ETL --> CLEAN
    ETL --> QUAL
    CLEAN --> IDX
    CLEAN --> BT
    IDX --> OUT
    BT --> OUT
    OUT --> API
    QUAL --> API
    API --> DASH
    LIVE -. "POST /scrape/live" .-> API

    CFG -.-> SIM
    CFG -.-> LIVE
    CFG -.-> ETL
    CFG -.-> IDX
    CFG -.-> BT

    classDef sim fill:#ffe6e6,stroke:#c00,color:#000
    classDef real fill:#e6f4ea,stroke:#2e7d32,color:#000
    classDef store fill:#eef2f7,stroke:#4a6fa5,color:#000
    class SIM sim
    class LIVE real
    class RAW,CLEAN,QUAL,OUT store
```

**The single most important thing in that diagram** is the `source` column on
every row. It is `CHECK`-constrained in SQLite to exactly three values, so a
row physically cannot exist without declaring where it came from. Everything
downstream inherits that guarantee.

---

## 2. The simulator / real-scraper split

This is the part to be straight about, because it is the first thing an
evaluator should ask.

### What is simulated

**All the fare data the index runs on.** `scraper/simulator.py` generates
50,400 quotes from an explicit pricing model (`config/simulator.yaml`):
a booking-window curve, day-of-week effects, carrier positioning, taxes, random
sell-outs and festival demand shocks. Every row is tagged `source='simulated'`.

### What is real

Everything else:

| Component | Status |
|---|---|
| Database schema and provenance constraint | Real |
| ETL cleaning, outlier detection, quality logging | Real |
| Laspeyres index mathematics and weighting | Real |
| Lead-time elasticity estimator | Real |
| Backtest comparison machinery | Real |
| FastAPI service | Real |
| Streamlit dashboard | Real |
| **Live scraper** | **Real — and it really runs** |

`scraper/live_scraper.py` genuinely opens headless Chromium, genuinely fetches
`robots.txt` and obeys it, genuinely rate-limits, and genuinely attempts
Skyscanner. It is not a stub.

### Why the demo does not run on live data

Two reasons, both honest:

1. **The index needs a panel, not a snapshot.** APIx measures fares across 8
   routes × 4 airlines × 45 booking windows × 35 observation days — 50,400
   observations. Collecting that legitimately means running a collector daily
   for weeks. It cannot be produced on demand.
2. **Every accessible target blocks automated access.** Measured 2026-09-10:
   Skyscanner answers HTTP 200 with a 312-character body containing only a
   challenge UUID; Ixigo returns 403 *"too many requests"*; Goibibo fails with
   `ERR_HTTP2_PROTOCOL_ERROR`. Kayak and Cleartrip disallow their search paths
   in `robots.txt`, so they were never attempted. See
   [anti-bot-strategy.md](anti-bot-strategy.md).

### What happens when the scraper is blocked

It does not error, and it does not pretend:

```mermaid
sequenceDiagram
    participant D as Dashboard
    participant A as API
    participant S as live_scraper
    participant T as Skyscanner

    D->>A: POST /scrape/live {DEL, BOM}
    A->>S: scrape_route()
    S->>T: GET /robots.txt
    T-->>S: rules
    Note over S: allowed? if not, stop here
    S->>S: rate limit (min 6s gap)
    S->>T: headless Chromium GET route page
    T-->>S: HTTP 200, 312-char challenge
    Note over S: block detected at first paint<br/>NOT retried — a refusal is an answer
    S->>S: simulator.generate_quote(source='fallback_simulated')
    S-->>A: ScrapeResult(is_real=False, reason=...)
    A-->>D: 200 OK, is_real=false
    Note over D: red banner:<br/>"these are NOT real quotes"
```

The fallback rows are tagged `fallback_simulated` — deliberately distinct from
ordinary `simulated` — so "the scraper was blocked and we substituted" stays
visible in the database permanently rather than blending into the backfill.

---

## 3. Where real data plugs in

Nothing architectural changes. `fare_quotes` starts receiving rows with
`source='live'`, and every stage downstream is already source-agnostic:

- the ETL cleans them with the same rules;
- the index weights them by the same basket;
- the API reports them with `is_real_data: true`;
- the dashboard banner changes by itself, because it reads row counts by source.

The two things that would need real work are **scale** (a scheduled collector
with per-target throttling) and **access** (commercial API agreements or GDS
feeds rather than scraping — see the anti-bot doc).

---

## 4. Component reference

| Path | Role |
|---|---|
| `config/` | Every weight, threshold and model parameter. Nothing tuneable is hardcoded. |
| `db.py` | The only database layer. `FareQuote` mirrors the table; all writes go through it. |
| `config_loader.py` | The only reader of `config/`. Validates that weight sets sum to 1.0 at load. |
| `data/schema.sql` | Canonical schema, including the `source` CHECK constraint. |
| `scraper/simulator.py` | Synthetic fare generator. Deterministic per quote. |
| `scraper/live_scraper.py` | Real scrape path with robots checks, rate limiting, fallback. |
| `etl/clean.py` | Cleaning pipeline and data-quality log. |
| `index/apix.py` | The index, route sub-indices, elasticity. |
| `index/backtest.py` | Reference series and agreement metrics. |
| `api/main.py` | FastAPI service. |
| `dashboard/app.py` | Streamlit dashboard. |
| `run.py` | One command: builds, serves, opens the dashboard. |
| `tests/` | 179 tests. |

---

## 5. Design decisions worth defending

**One schema for both sources.** The simulator and the scraper emit the same
`FareQuote`. Nothing downstream branches on provenance, so there is no code
path that only works for fake data.

**Config over constants.** "Why these routes and weights?" has a one-file
answer. `config_loader` rejects a basket whose weights do not sum to 1.0
rather than silently rescaling the index.

**Persist computed results.** Phases 5 and 6 write their output to tables; the
API reads those. The dashboard and the API therefore cannot disagree about
what the index says.

**Fail loudly on data problems, quietly on collection problems.** A
mis-normalised basket raises at load. A blocked scrape returns a labelled
result. The distinction is deliberate: bad *statistics* must stop the pipeline,
while a hostile website must not take the demo down.

**SQLite, Streamlit, one process each.** A prototype that must run from a fresh
clone in one command. See CLAUDE.md ground rule 7.

---

## 6. Running it

```bash
python run.py
```

Builds the dataset if absent, starts the API on `:8000`, opens the dashboard on
`:8501`. See the [README](../README.md) for details and
[methodology.md](methodology.md) for the index mathematics.
