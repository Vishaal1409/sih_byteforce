"""scraper/live_scraper.py — the real scrape path, with an honest fallback.

WHAT THIS IS FOR
----------------
This module exists to prove the real-collection path works end to end. It is
NOT what powers the demo -- the index runs on the Phase 2 simulator's panel.
Nothing else in the project depends on this succeeding, by design: if it is
blocked, times out, or the page layout changes, the rest of the prototype is
unaffected.

WHAT IT WILL AND WILL NOT DO
----------------------------
Will:  check robots.txt at runtime before every fetch and refuse if disallowed;
       rate-limit itself; retry with exponential backoff on transient failure;
       detect that it has been blocked or challenged, log it, and stop.
Won't: solve or bypass a CAPTCHA, patch navigator.webdriver, install stealth
       plugins, randomise fingerprints, or rotate IPs to evade blocking.
       See CLAUDE.md ground rule 6 and docs/anti-bot-strategy.md.

Being blocked is a legitimate, expected outcome. When it happens the caller gets
a clearly-labelled simulated result (source = 'fallback_simulated'), never an
exception and never a silent pretence that it was real.

    from scraper.live_scraper import scrape_route

    result = scrape_route("DEL", "BOM", date(2026, 10, 15))
    print(result.status)      # 'live' | 'fallback_simulated' | 'blocked'
    print(result.is_real)     # False unless genuinely scraped
    print(result.quotes)      # list[db.FareQuote]

CLI:
    python -m scraper.live_scraper --origin DEL --dest BOM --days-ahead 35
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser as robotparser
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from config_loader import CONFIG_DIR, _read_yaml, load_airlines, route_by_key  # noqa: E402
from scraper import simulator  # noqa: E402

log = logging.getLogger("apix.live_scraper")

SCRAPER_CONFIG_PATH = CONFIG_DIR / "scraper.yaml"

#: Process-wide timestamp of the last request to the target, for rate limiting.
_last_request_at: float = 0.0

#: Cached robots.txt parser, refetched when older than this.
_robots_cache: tuple[float, robotparser.RobotFileParser] | None = None
_ROBOTS_TTL_SECONDS = 900.0


def load_scraper_config() -> dict[str, Any]:
    return _read_yaml(SCRAPER_CONFIG_PATH)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScrapeResult:
    """Outcome of one scrape attempt. Always returned; never raised past."""

    status: str                       # 'live' | 'fallback_simulated' | 'blocked'
    quotes: list[db.FareQuote] = field(default_factory=list)
    target: str = ""
    url: str = ""
    reason: str = ""                  # why we fell back, in plain English
    attempts: int = 0
    elapsed_seconds: float = 0.0

    @property
    def is_real(self) -> bool:
        """True only for genuinely scraped data.

        The dashboard and the API gate their "this is real" labelling on this,
        so it is deliberately a single obvious flag rather than a string
        comparison scattered across callers.
        """
        return self.status == "live" and bool(self.quotes)

    @property
    def headline(self) -> str:
        """One line suitable for showing a user verbatim."""
        if self.is_real:
            return (
                f"LIVE: scraped {len(self.quotes)} real fare(s) from {self.target}"
            )
        return (
            f"SIMULATED FALLBACK: {self.target} returned no usable data "
            f"({self.reason}). Showing simulated fares instead - these are NOT "
            f"real quotes."
        )

    def summary(self) -> dict[str, Any]:
        """JSON-friendly form, for the Phase 7 API."""
        return {
            "status": self.status,
            "is_real": self.is_real,
            "target": self.target,
            "url": self.url,
            "reason": self.reason,
            "attempts": self.attempts,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "quote_count": len(self.quotes),
            "headline": self.headline,
        }


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


def _fetch_robots(cfg: dict[str, Any]) -> robotparser.RobotFileParser | None:
    """Fetch and parse the target's robots.txt. None if unreachable."""
    global _robots_cache
    now = time.monotonic()
    if _robots_cache is not None and (now - _robots_cache[0]) < _ROBOTS_TTL_SECONDS:
        return _robots_cache[1]

    url = cfg["target"]["robots_url"]
    ua = cfg["robots_user_agent"]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("could not fetch robots.txt (%s): %s", url, exc)
        return None

    parser = robotparser.RobotFileParser()
    parser.parse(body.splitlines())
    _robots_cache = (now, parser)
    return parser


def robots_allows(url: str, cfg: dict[str, Any] | None = None) -> tuple[bool, str]:
    """May we fetch `url`? Returns (allowed, reason).

    Checked at runtime before every fetch, not just once when the target was
    chosen -- a site can change its rules, and a build-time check would let us
    keep scraping after permission was withdrawn.

    Fails CLOSED: if robots.txt cannot be fetched we do not scrape. An
    unreachable robots.txt is not permission.
    """
    cfg = cfg or load_scraper_config()
    parser = _fetch_robots(cfg)
    if parser is None:
        return False, "robots.txt unreachable - refusing to scrape without it"

    ua = cfg["robots_user_agent"]
    if not parser.can_fetch(ua, url):
        return False, f"robots.txt disallows {url} for user-agent {ua!r}"
    return True, "allowed by robots.txt"


def robots_crawl_delay(cfg: dict[str, Any]) -> float:
    """Crawl-delay from robots.txt, or 0.0 if none is declared."""
    parser = _fetch_robots(cfg)
    if parser is None:
        return 0.0
    try:
        delay = parser.crawl_delay(cfg["robots_user_agent"])
    except Exception:
        return 0.0
    return float(delay) if delay else 0.0


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def _respect_rate_limit(cfg: dict[str, Any]) -> None:
    """Block until the configured minimum gap since the last request has passed."""
    global _last_request_at
    min_delay = float(cfg["politeness"]["min_delay_seconds"])
    if cfg["politeness"].get("respect_crawl_delay", True):
        min_delay = max(min_delay, robots_crawl_delay(cfg))

    waited = time.monotonic() - _last_request_at
    if _last_request_at and waited < min_delay:
        sleep_for = min_delay - waited
        log.info("rate limit: sleeping %.1fs before next request", sleep_for)
        time.sleep(sleep_for)
    _last_request_at = time.monotonic()


def reset_rate_limiter() -> None:
    """For tests — forget the last request time."""
    global _last_request_at
    _last_request_at = 0.0


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------


def build_url(origin: str, dest: str, travel_date: date, cfg: dict[str, Any]) -> str:
    return cfg["target"]["url_template"].format(
        origin=origin.lower(),
        dest=dest.lower(),
        yymmdd=travel_date.strftime("%y%m%d"),
    )


class BlockedError(RuntimeError):
    """The target served a challenge or block page rather than content."""


def _detect_block(text: str, cfg: dict[str, Any]) -> str | None:
    """Return a description of the block, or None if the page looks normal.

    Two signals:

    1. An explicit challenge phrase ("captcha", "unusual traffic", ...).
    2. A page far too short to be a search result. Some targets answer HTTP 200
       with a tiny interstitial carrying only a challenge token -- Skyscanner
       served us a 312-character body containing a bare UUID. That is a refusal
       dressed as a success, and it must be classified as a block: read as
       "nothing parsed" instead, the scraper would retry and back off against a
       site that has already declined.
    """
    lowered = text.lower()
    for marker in cfg["block_indicators"]:
        if marker.lower() in lowered:
            return f"challenge phrase {marker!r} on page"

    min_chars = int(cfg.get("min_content_chars", 0))
    if min_chars and len(text.strip()) < min_chars:
        return (
            f"page was only {len(text.strip())} chars "
            f"(< {min_chars}) - interstitial or challenge, not content"
        )
    return None


def _fetch_page_text(url: str, cfg: dict[str, Any]) -> str:
    """Render `url` in headless Chromium and return its visible text.

    Playwright is used because these pages assemble their fares in JavaScript;
    a plain HTTP GET returns a shell with no prices in it. We run a real,
    unmodified Chromium -- no stealth patches, no fingerprint spoofing.
    """
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    b = cfg["browser"]
    timeout_ms = int(float(b["page_timeout_seconds"]) * 1000)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=bool(b["headless"]))
        try:
            context = browser.new_context(
                viewport={"width": int(b["viewport_width"]),
                          "height": int(b["viewport_height"])},
                locale=b.get("locale", "en-IN"),
            )
            page = context.new_page()
            try:
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                # Fares stream in after first paint; give the network a moment
                # to settle, but do not fail the scrape if it never fully idles.
                try:
                    page.wait_for_load_state("networkidle", timeout=timeout_ms)
                except PWTimeout:
                    log.info("network did not reach idle; parsing what rendered")
                return page.inner_text("body")
            finally:
                context.close()
        finally:
            browser.close()


def parse_fares(
    text: str, origin: str, dest: str, travel_date: date, query_date: date
) -> list[db.FareQuote]:
    """Extract fare quotes from the rendered page text.

    Deliberately conservative: it only emits a quote when it can tie a price to
    one of the carriers in our basket. A page that renders but yields nothing
    recognisable returns [] and the caller falls back -- which is the honest
    outcome, rather than inventing a number and calling it live.

    Page layouts change without notice, so this is expected to be the most
    fragile part of the module. It is isolated here for exactly that reason.
    """
    import re

    quotes: list[db.FareQuote] = []
    airlines = {a.name.lower(): a for a in load_airlines()}
    # Rupee amounts: optional symbol/word, then 3-6 digits with optional commas.
    price_re = re.compile(r"(?:₹|Rs\.?|INR)\s?([\d,]{3,9})")

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        matched_airline = next(
            (a for name, a in airlines.items() if name in stripped.lower()), None
        )
        if matched_airline is None:
            continue
        price_match = price_re.search(stripped)
        if price_match is None:
            continue
        try:
            total = float(price_match.group(1).replace(",", ""))
        except ValueError:
            continue
        # Domestic economy sanity band; anything outside it is a mis-parse
        # (a phone number, a mileage figure, a hotel rate).
        if not (1000.0 <= total <= 100_000.0):
            continue

        quotes.append(
            db.FareQuote(
                source=db.SOURCE_LIVE,
                airline=matched_airline.code,
                origin=origin,
                dest=dest,
                travel_date=travel_date,
                query_date=query_date,
                base_fare=None,
                taxes=None,
                total_fare=total,
                is_available=True,
            )
        )
    return quotes


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


def simulated_fallback(
    origin: str, dest: str, travel_date: date, query_date: date
) -> list[db.FareQuote]:
    """Simulator-backed quotes for every carrier, tagged 'fallback_simulated'.

    Tagged distinctly from ordinary simulated rows so that "the scraper was
    blocked and we substituted" stays visible in the data forever, rather than
    blending into the backfill.
    """
    route = route_by_key(f"{origin}-{dest}")
    return [
        simulator.generate_quote(
            airline, route, travel_date, query_date,
            source=db.SOURCE_FALLBACK,
        )
        for airline in load_airlines()
    ]


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def scrape_route(
    origin: str,
    dest: str,
    travel_date: date,
    query_date: date | None = None,
    cfg: dict[str, Any] | None = None,
    allow_fallback: bool = True,
) -> ScrapeResult:
    """Scrape one route/date. Never raises; always returns a ScrapeResult.

    Order of events: check robots.txt, rate-limit, fetch with retry/backoff,
    detect blocks, parse. Any failure at any step falls through to a clearly
    labelled simulated result.
    """
    cfg = cfg or load_scraper_config()
    query_date = query_date or date.today()
    origin, dest = origin.strip().upper(), dest.strip().upper()
    target_name = cfg["target"]["name"]
    started = time.monotonic()

    url = build_url(origin, dest, travel_date, cfg)
    retry_cfg = cfg["retry"]
    deadline = started + float(retry_cfg["overall_deadline_seconds"])

    def fall_back(status: str, reason: str, attempts: int) -> ScrapeResult:
        log.warning("falling back to simulated data: %s", reason)
        quotes = (
            simulated_fallback(origin, dest, travel_date, query_date)
            if allow_fallback else []
        )
        return ScrapeResult(
            status=status if not allow_fallback else "fallback_simulated",
            quotes=quotes,
            target=target_name,
            url=url,
            reason=reason,
            attempts=attempts,
            elapsed_seconds=time.monotonic() - started,
        )

    # 1. robots.txt, every time.
    allowed, robots_reason = robots_allows(url, cfg)
    if not allowed:
        return fall_back("blocked", robots_reason, 0)

    # 2. Fetch, with retry and exponential backoff.
    backoff = float(retry_cfg["initial_backoff_seconds"])
    last_reason = "unknown failure"
    attempts = 0

    for attempt in range(1, int(retry_cfg["max_attempts"]) + 1):
        attempts = attempt
        if time.monotonic() > deadline:
            return fall_back("blocked", f"deadline exceeded after {attempt - 1} attempts", attempt - 1)

        try:
            _respect_rate_limit(cfg)
            log.info("attempt %d/%s: %s", attempt, retry_cfg["max_attempts"], url)
            text = _fetch_page_text(url, cfg)

            block = _detect_block(text, cfg)
            if block is not None:
                # A challenge is a definitive answer, not a transient error.
                # Retrying it would be exactly the hammering we must not do.
                return fall_back(
                    "blocked", f"target blocked the request: {block}", attempt
                )

            quotes = parse_fares(text, origin, dest, travel_date, query_date)
            if quotes:
                log.info("scraped %d live quote(s) from %s", len(quotes), target_name)
                return ScrapeResult(
                    status="live",
                    quotes=quotes,
                    target=target_name,
                    url=url,
                    reason="ok",
                    attempts=attempt,
                    elapsed_seconds=time.monotonic() - started,
                )
            last_reason = (
                "page rendered but no fares could be parsed from it "
                "(layout change, or fares not shown for this route/date)"
            )
            log.info("attempt %d: %s", attempt, last_reason)

        except ImportError as exc:
            return fall_back("blocked", f"Playwright unavailable: {exc}", attempt)
        except Exception as exc:  # noqa: BLE001 - the caller must never see this
            last_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.warning("attempt %d failed: %s", attempt, last_reason)

        if attempt < int(retry_cfg["max_attempts"]):
            wait = min(backoff, max(0.0, deadline - time.monotonic()))
            if wait > 0:
                log.info("backing off %.1fs", wait)
                time.sleep(wait)
            backoff *= float(retry_cfg["backoff_multiplier"])

    return fall_back("blocked", last_reason, attempts)


def scrape_and_store(
    origin: str,
    dest: str,
    travel_date: date,
    db_path: str | Path = db.DEFAULT_DB_PATH,
    **kwargs: Any,
) -> ScrapeResult:
    """scrape_route(), with the resulting quotes written to the database."""
    result = scrape_route(origin, dest, travel_date, **kwargs)
    if result.quotes:
        db.init_db(db_path)
        with db.connect(db_path) as conn:
            db.insert_quotes(conn, result.quotes)
    return result


# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scrape one route from the configured target, or fall back."
    )
    parser.add_argument("--origin", default="DEL")
    parser.add_argument("--dest", default="BOM")
    parser.add_argument("--days-ahead", type=int, default=35)
    parser.add_argument("--store", action="store_true", help="write quotes to the DB")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-8s %(name)s: %(message)s",
    )

    travel_date = date.today() + timedelta(days=args.days_ahead)
    fn = scrape_and_store if args.store else scrape_route
    result = fn(args.origin, args.dest, travel_date)

    print("=" * 72)
    print(f"target        {result.target}")
    print(f"url           {result.url}")
    print(f"travel_date   {travel_date}")
    print(f"status        {result.status}")
    print(f"is_real       {result.is_real}")
    print(f"attempts      {result.attempts}")
    print(f"elapsed       {result.elapsed_seconds:.1f}s")
    print(f"reason        {result.reason}")
    print("=" * 72)
    print(result.headline)
    for q in result.quotes:
        print(f"  {q.source:<20} {q.airline}  {q.origin}-{q.dest}  "
              f"{q.travel_date}  total={q.total_fare}")
    # A clean fallback is a successful run: the caller got usable, labelled data.
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
