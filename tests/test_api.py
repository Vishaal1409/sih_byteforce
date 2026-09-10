"""Phase 7 tests: every endpoint returns valid JSON and never hides provenance.

Offline — the scrape endpoint's network call is stubbed. The genuine live run
against the real server is recorded in TASKS.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from api import main as api_main  # noqa: E402
from scraper import live_scraper as ls  # noqa: E402

DB_MISSING_TABLES = 503


@pytest.fixture(scope="module")
def client():
    return TestClient(api_main.app)


def _pipeline_built() -> bool:
    """Has the project database been through Phases 2, 4, 5 and 6?"""
    if not db.DEFAULT_DB_PATH.exists():
        return False
    with db.connect(db.DEFAULT_DB_PATH) as conn:
        names = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    return {"apix_index", "apix_route_index", "apix_elasticity", "apix_backtest"} <= names


built = pytest.mark.skipif(
    not _pipeline_built(), reason="project database has not been built yet"
)


# --- OpenAPI ----------------------------------------------------------------


def test_openapi_schema_lists_every_required_endpoint(client):
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    for required in (
        "/index",
        "/index/route/{origin}/{dest}",
        "/index/elasticity",
        "/backtest/results",
        "/scrape/live",
    ):
        assert required in paths, f"{required} missing from the OpenAPI schema"
    assert "post" in paths["/scrape/live"]


def test_docs_page_renders(client):
    r = client.get("/docs")
    assert r.status_code == 200
    assert "swagger-ui" in r.text
    assert "APIx" in r.text


def test_api_description_declares_simulated_data(client):
    """A caller reading only the docs page must still learn the data is fake."""
    info = client.get("/openapi.json").json()["info"]
    assert "simulated" in info["description"].lower()


# --- /index -----------------------------------------------------------------


@built
def test_index_returns_the_daily_series(client):
    r = client.get("/index")
    assert r.status_code == 200
    d = r.json()

    assert d["period_type"] == "daily"
    assert d["base_value"] == 100.0
    assert d["points"] == len(d["series"]) > 0
    assert d["series"][0]["index_value"] == pytest.approx(100.0, abs=1e-6)
    assert d["current"] == d["series"][-1]


@built
@pytest.mark.parametrize("period", ["daily", "weekly", "monthly"])
def test_index_supports_every_period(client, period):
    r = client.get("/index", params={"period": period})
    assert r.status_code == 200
    assert r.json()["period_type"] == period
    assert r.json()["points"] > 0


def test_index_rejects_an_unknown_period(client):
    assert client.get("/index", params={"period": "hourly"}).status_code == 422


@built
def test_index_includes_the_data_quality_summary(client):
    dq = client.get("/index").json()["data_quality"]
    assert dq["rows_in"] > 0
    assert dq["rows_in"] == dq["rows_out"] + dq["total_dropped"]


# --- provenance is non-negotiable -------------------------------------------


@built
@pytest.mark.parametrize("path", [
    "/index", "/index/elasticity", "/index/route/DEL/BOM", "/backtest/results",
])
def test_every_response_carries_provenance(client, path):
    """CLAUDE.md ground rule 5, enforced at the API boundary."""
    p = client.get(path).json()["provenance"]
    assert p["is_real_data"] is False
    assert "SIMULATED" in p["warning"].upper()
    assert p["row_counts_by_source"]


@built
def test_provenance_is_a_required_schema_field(client):
    """It must not be droppable — a caller can never get numbers without it."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    for model in ("IndexResponse", "RouteIndexResponse", "ElasticityResponse",
                  "BacktestResponse", "ScrapeResponse"):
        assert "provenance" in schemas[model]["required"], model


# --- /index/route -----------------------------------------------------------


@built
def test_route_subindex(client):
    d = client.get("/index/route/DEL/BOM").json()
    assert (d["origin"], d["dest"], d["route"]) == ("DEL", "BOM", "DEL-BOM")
    assert d["name"]
    assert d["series"][0]["index_value"] == pytest.approx(100.0, abs=1e-6)


@built
def test_route_code_is_case_insensitive(client):
    assert client.get("/index/route/del/bom").json()["route"] == "DEL-BOM"


def test_unknown_route_returns_404_listing_the_basket(client):
    r = client.get("/index/route/XXX/YYY")
    assert r.status_code == 404
    assert "DEL-BOM" in r.json()["detail"]


def test_malformed_route_code_is_rejected(client):
    assert client.get("/index/route/D/BOM").status_code == 422


# --- /index/elasticity ------------------------------------------------------


@built
def test_elasticity_endpoint(client):
    d = client.get("/index/elasticity").json()
    assert len(d["routes"]) == 8
    assert "fixed effects" in d["method"]
    for e in d["routes"]:
        assert e["pct_change_per_day"] > 0, "fares must rise closer to departure"
        assert 0.0 <= e["r_squared"] <= 1.0
        assert e["n_observations"] > 0
    values = [e["pct_change_per_day"] for e in d["routes"]]
    assert values == sorted(values, reverse=True)


# --- /backtest/results ------------------------------------------------------


@built
def test_backtest_endpoint(client):
    d = client.get("/backtest/results").json()
    assert d["n_days"] >= 30
    assert 0.70 <= d["correlation"] <= 0.995
    assert 0.2 <= d["mape_pct"] <= 15.0
    assert len(d["series"]) == d["n_days"]
    assert d["series"][0].keys() >= {"query_date", "index_value", "reference_value"}


@built
def test_backtest_declares_the_reference_is_not_real(client):
    d = client.get("/backtest/results").json()
    assert d["reference_is_real_data"] is False
    assert "not real dgca" in d["reference_label"].lower()


# --- POST /scrape/live ------------------------------------------------------


@pytest.fixture()
def stub_blocked(monkeypatch):
    """Force the scraper down its fallback path without touching the network."""
    def blocked(*a, **k):
        return "cf-challenge"
    monkeypatch.setattr(ls, "_fetch_page_text", blocked)
    monkeypatch.setattr(ls.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ls, "robots_allows", lambda *a, **k: (True, "allowed"))
    ls.reset_rate_limiter()


def test_scrape_live_returns_labelled_fallback(client, stub_blocked, tmp_path, monkeypatch):
    monkeypatch.setattr(api_main, "DB_PATH", db.init_db(tmp_path / "scrape.db"))

    r = client.post("/scrape/live", json={"origin": "DEL", "dest": "BOM",
                                          "days_ahead": 30, "store": True})
    assert r.status_code == 200
    d = r.json()

    assert d["is_real"] is False
    assert d["status"] in ("fallback_simulated", "blocked")
    assert "NOT real quotes" in d["headline"]
    assert d["quotes"]
    assert {q["source"] for q in d["quotes"]} == {db.SOURCE_FALLBACK}


def test_scrape_live_succeeding_is_marked_real(client, monkeypatch, tmp_path):
    monkeypatch.setattr(api_main, "DB_PATH", db.init_db(tmp_path / "live.db"))
    monkeypatch.setattr(ls, "robots_allows", lambda *a, **k: (True, "allowed"))
    monkeypatch.setattr(ls.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        ls, "_fetch_page_text",
        lambda *a, **k: "IndiGo ₹5,499\nAir India ₹7,200\n" + ("x " * 1500),
    )
    ls.reset_rate_limiter()

    d = client.post("/scrape/live", json={"origin": "DEL", "dest": "BOM",
                                          "days_ahead": 30}).json()
    assert d["is_real"] is True
    assert d["status"] == "live"
    assert "LIVE" in d["headline"]
    assert {q["source"] for q in d["quotes"]} == {db.SOURCE_LIVE}


def test_scrape_live_never_500s_on_a_scraper_crash(client, monkeypatch, tmp_path):
    monkeypatch.setattr(api_main, "DB_PATH", db.init_db(tmp_path / "crash.db"))
    monkeypatch.setattr(ls, "robots_allows", lambda *a, **k: (True, "allowed"))
    monkeypatch.setattr(ls.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        ls, "_fetch_page_text",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("browser exploded")),
    )
    ls.reset_rate_limiter()

    r = client.post("/scrape/live", json={"origin": "DEL", "dest": "BOM"})
    assert r.status_code == 200
    assert r.json()["is_real"] is False


def test_scrape_live_rejects_a_route_outside_the_basket(client, stub_blocked):
    r = client.post("/scrape/live", json={"origin": "XXX", "dest": "YYY"})
    assert r.status_code == 404


def test_scrape_live_validates_its_input(client):
    assert client.post("/scrape/live", json={"origin": "D", "dest": "BOM"}).status_code == 422
    assert client.post("/scrape/live", json={"origin": "DEL", "dest": "BOM",
                                             "days_ahead": 999}).status_code == 422


# --- behaviour before the pipeline has been run -----------------------------


def test_endpoints_explain_themselves_when_tables_are_missing(client, monkeypatch, tmp_path):
    """A fresh clone should get a helpful 503, not a stack trace."""
    monkeypatch.setattr(api_main, "DB_PATH", db.init_db(tmp_path / "bare.db"))

    for path in ("/index", "/index/elasticity", "/index/route/DEL/BOM",
                 "/backtest/results"):
        r = client.get(path)
        assert r.status_code == DB_MISSING_TABLES, path
        assert "python -m" in r.json()["detail"], path
