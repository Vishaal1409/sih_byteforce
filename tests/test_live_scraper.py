"""Phase 3 tests: the live scraper is polite, honest, and never explodes.

All offline — the network is stubbed out, so these are deterministic and do not
hammer the target. The genuine end-to-end run against Skyscanner is a separate
manual step (`python -m scraper.live_scraper --verbose`), recorded in TASKS.md.
"""

from __future__ import annotations

import sys
import urllib.robotparser as robotparser
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from scraper import live_scraper as ls  # noqa: E402

ROBOTS_ALLOW_ALL = "User-agent: *\nAllow: /\n"
ROBOTS_DISALLOW_FLIGHTS = "User-agent: *\nDisallow: /transport/flights/\n"

TRAVEL, QUERY = date(2026, 10, 15), date(2026, 9, 10)


@pytest.fixture()
def cfg():
    return ls.load_scraper_config()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Default every test to an allow-all robots and no rate-limit sleeping."""
    def fake_robots(_cfg):
        p = robotparser.RobotFileParser()
        p.parse(ROBOTS_ALLOW_ALL.splitlines())
        return p

    monkeypatch.setattr(ls, "_fetch_robots", fake_robots)
    monkeypatch.setattr(ls.time, "sleep", lambda _s: None)
    ls.reset_rate_limiter()


def a_page(*lines: str) -> str:
    """A page long enough to clear the interstitial threshold."""
    return "\n".join(lines) + "\n" + ("filler content. " * 200)


# --- robots.txt -------------------------------------------------------------


def test_url_is_built_from_the_template(cfg):
    url = ls.build_url("DEL", "BOM", TRAVEL, cfg)
    assert url == "https://www.skyscanner.co.in/transport/flights/del/bom/261015/"


def test_robots_disallow_stops_the_scrape(monkeypatch, cfg):
    def disallowing(_cfg):
        p = robotparser.RobotFileParser()
        p.parse(ROBOTS_DISALLOW_FLIGHTS.splitlines())
        return p

    monkeypatch.setattr(ls, "_fetch_robots", disallowing)
    called = False

    def must_not_fetch(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("fetched a page robots.txt disallows")

    monkeypatch.setattr(ls, "_fetch_page_text", must_not_fetch)

    result = ls.scrape_route("DEL", "BOM", TRAVEL, QUERY, cfg)
    assert not called
    assert result.status == "fallback_simulated"
    assert "robots.txt disallows" in result.reason
    assert not result.is_real


def test_unreachable_robots_fails_closed(monkeypatch, cfg):
    """An unreachable robots.txt is not permission."""
    monkeypatch.setattr(ls, "_fetch_robots", lambda _cfg: None)
    monkeypatch.setattr(
        ls, "_fetch_page_text",
        lambda *a, **k: pytest.fail("scraped without reading robots.txt"),
    )
    result = ls.scrape_route("DEL", "BOM", TRAVEL, QUERY, cfg)
    assert "unreachable" in result.reason
    assert not result.is_real


def test_robots_is_rechecked_at_runtime(cfg):
    """Not just once at build time — permission can be withdrawn."""
    allowed, _ = ls.robots_allows("https://example.com/anything", cfg)
    assert allowed is True


# --- block detection --------------------------------------------------------


def test_challenge_phrase_is_a_block(cfg):
    assert ls._detect_block(a_page("Please complete the CAPTCHA"), cfg) is not None
    assert ls._detect_block(a_page("We detected unusual traffic"), cfg) is not None


def test_short_interstitial_is_a_block(cfg):
    """The real Skyscanner failure: HTTP 200, 312 chars, just a UUID."""
    block = ls._detect_block("87b7ffd0-ad32-11f1-9040-76668eb97254", cfg)
    assert block is not None
    assert "interstitial" in block


def test_normal_page_is_not_a_block(cfg):
    assert ls._detect_block(a_page("IndiGo", "Rs 5,400"), cfg) is None


def test_block_is_not_retried(monkeypatch, cfg):
    """Retrying a refusal is exactly the hammering we must not do."""
    calls = 0

    def challenged(*a, **k):
        nonlocal calls
        calls += 1
        return "cf-challenge token"

    monkeypatch.setattr(ls, "_fetch_page_text", challenged)
    result = ls.scrape_route("DEL", "BOM", TRAVEL, QUERY, cfg)
    assert calls == 1, "a block must not be retried"
    assert result.attempts == 1
    assert not result.is_real


# --- parsing ----------------------------------------------------------------


def test_parses_fares_for_known_carriers():
    text = a_page(
        "IndiGo  ₹5,499  06:10 - 08:20",
        "Air India  Rs. 7,200  09:00 - 11:15",
        "SpiceJet  INR 4850  14:30 - 16:40",
    )
    quotes = ls.parse_fares(text, "DEL", "BOM", TRAVEL, QUERY)
    assert {q.airline for q in quotes} == {"6E", "AI", "SG"}
    assert {q.total_fare for q in quotes} == {5499.0, 7200.0, 4850.0}
    assert all(q.source == db.SOURCE_LIVE for q in quotes)
    assert all(q.origin == "DEL" and q.dest == "BOM" for q in quotes)


def test_ignores_prices_without_a_known_carrier():
    assert ls.parse_fares(a_page("Some Other Airline ₹5,499"), "DEL", "BOM", TRAVEL, QUERY) == []


def test_ignores_implausible_amounts():
    """Guards against parsing a phone number or a mileage figure as a fare."""
    text = a_page("IndiGo ₹12", "Air India ₹9,99,99,999")
    assert ls.parse_fares(text, "DEL", "BOM", TRAVEL, QUERY) == []


def test_unparseable_page_yields_nothing_rather_than_a_guess():
    assert ls.parse_fares(a_page("no fares here"), "DEL", "BOM", TRAVEL, QUERY) == []


# --- the live path ----------------------------------------------------------


def test_successful_scrape_is_marked_real(monkeypatch, cfg):
    monkeypatch.setattr(
        ls, "_fetch_page_text",
        lambda *a, **k: a_page("IndiGo ₹5,499", "Air India ₹7,200"),
    )
    result = ls.scrape_route("DEL", "BOM", TRAVEL, QUERY, cfg)
    assert result.status == "live"
    assert result.is_real
    assert len(result.quotes) == 2
    assert all(q.source == db.SOURCE_LIVE for q in result.quotes)
    assert "LIVE" in result.headline


# --- the fallback path ------------------------------------------------------


def test_transient_error_is_retried_then_falls_back(monkeypatch, cfg):
    calls = 0

    def flaky(*a, **k):
        nonlocal calls
        calls += 1
        raise TimeoutError("connection stalled")

    monkeypatch.setattr(ls, "_fetch_page_text", flaky)
    result = ls.scrape_route("DEL", "BOM", TRAVEL, QUERY, cfg)
    assert calls == cfg["retry"]["max_attempts"], "transient errors should retry"
    assert result.status == "fallback_simulated"
    assert not result.is_real


def test_recovers_on_a_later_attempt(monkeypatch, cfg):
    calls = 0

    def second_time_lucky(*a, **k):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("stalled")
        return a_page("IndiGo ₹5,499")

    monkeypatch.setattr(ls, "_fetch_page_text", second_time_lucky)
    result = ls.scrape_route("DEL", "BOM", TRAVEL, QUERY, cfg)
    assert result.is_real
    assert result.attempts == 2


def test_fallback_quotes_are_tagged_and_never_look_real(monkeypatch, cfg):
    monkeypatch.setattr(
        ls, "_fetch_page_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    result = ls.scrape_route("DEL", "BOM", TRAVEL, QUERY, cfg)

    assert result.quotes, "fallback must still return usable data"
    assert all(q.source == db.SOURCE_FALLBACK for q in result.quotes)
    assert not result.is_real
    assert "NOT real quotes" in result.headline
    assert result.summary()["is_real"] is False


def test_fallback_covers_every_carrier_in_the_basket(cfg):
    from config_loader import load_airlines

    quotes = ls.simulated_fallback("DEL", "BOM", TRAVEL, QUERY)
    assert {q.airline for q in quotes} == {a.code for a in load_airlines()}
    assert all(q.source == db.SOURCE_FALLBACK for q in quotes)


def test_never_raises_whatever_goes_wrong(monkeypatch, cfg):
    """The caller must never see an exception — Phase 7 exposes this over HTTP."""
    for boom in (RuntimeError("x"), ValueError("y"), OSError("z"), MemoryError()):
        monkeypatch.setattr(
            ls, "_fetch_page_text",
            lambda *a, _b=boom, **k: (_ for _ in ()).throw(_b),
        )
        result = ls.scrape_route("DEL", "BOM", TRAVEL, QUERY, cfg)
        assert isinstance(result, ls.ScrapeResult)
        assert not result.is_real


def test_allow_fallback_false_returns_no_quotes(monkeypatch, cfg):
    monkeypatch.setattr(
        ls, "_fetch_page_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    result = ls.scrape_route("DEL", "BOM", TRAVEL, QUERY, cfg, allow_fallback=False)
    assert result.quotes == []
    assert result.status == "blocked"


# --- persistence ------------------------------------------------------------


def test_scrape_and_store_writes_tagged_rows(monkeypatch, tmp_path, cfg):
    monkeypatch.setattr(
        ls, "_fetch_page_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked"))
    )
    db_path = tmp_path / "scrape.db"
    result = ls.scrape_and_store("DEL", "BOM", TRAVEL, db_path=db_path, query_date=QUERY, cfg=cfg)

    with db.connect(db_path) as conn:
        assert db.counts_by_source(conn) == {db.SOURCE_FALLBACK: len(result.quotes)}


# --- politeness -------------------------------------------------------------


def test_rate_limiter_waits_between_requests(monkeypatch, cfg):
    slept: list[float] = []
    monkeypatch.setattr(ls.time, "sleep", lambda s: slept.append(s))

    ls.reset_rate_limiter()
    ls._respect_rate_limit(cfg)   # first call: no wait
    assert slept == []
    ls._respect_rate_limit(cfg)   # second call: must wait
    assert slept and slept[0] > 0


def test_config_declares_a_real_delay(cfg):
    assert cfg["politeness"]["min_delay_seconds"] >= 1.0
