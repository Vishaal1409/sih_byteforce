"""Phase 8 tests: the dashboard degrades gracefully and never hides provenance.

Streamlit's AppTest runs the real script in-process. These cover the paths that
are awkward to demo by hand — chiefly "the API is down", which must produce an
actionable message rather than a stack trace or a blank page.

The full rendered-in-a-browser verification (all five sections, four charts,
clicking the live-scrape button) is a manual step recorded in TASKS.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

APP = str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py")

AppTest = pytest.importorskip(
    "streamlit.testing.v1", reason="streamlit testing harness unavailable"
).AppTest


def run_app(api_base: str, timeout: float = 60.0):
    """Run the dashboard against `api_base` and return the finished AppTest."""
    import os

    # API_BASE is read at module import, so set it before the script runs and
    # drop any cached copy of the module.
    os.environ["APIX_API_BASE"] = api_base
    sys.modules.pop("dashboard.app", None)

    at = AppTest.from_file(APP, default_timeout=timeout)
    return at.run()


def all_text(at) -> str:
    parts = []
    for coll in (at.markdown, at.error, at.warning, at.info, at.success,
                 at.caption, at.title, at.subheader):
        try:
            parts += [el.value for el in coll]
        except Exception:
            pass
    return "\n".join(str(p) for p in parts)


# --- the API-down path ------------------------------------------------------


@pytest.fixture(scope="module")
def down_app():
    """A port nothing is listening on."""
    return run_app("http://127.0.0.1:9")


def test_app_does_not_crash_when_the_api_is_down(down_app):
    assert not down_app.exception, f"unhandled exception: {down_app.exception}"


def test_api_down_message_is_actionable(down_app):
    text = all_text(down_app)
    assert "Cannot reach the APIx API" in text
    assert "python run.py" in text, "must tell the user how to start it"


def test_banner_is_shown_even_when_the_api_is_down(down_app):
    """Provenance must never depend on a service being reachable."""
    text = all_text(down_app)
    assert "SIMULATED DEMO DATA" in text
    assert "NOT REAL AIRLINE FARES" in text


def test_title_is_present(down_app):
    assert any("APIx" in t.value for t in down_app.title)


# --- the banner text itself -------------------------------------------------


def test_banner_names_the_methodology_doc():
    import dashboard.app as app

    import io
    src = io.open(app.__file__, encoding="utf-8").read()
    assert "docs/methodology.md" in src
    assert "SIMULATED DEMO DATA" in src


def test_dashboard_reads_api_base_from_the_environment(monkeypatch):
    monkeypatch.setenv("APIX_API_BASE", "http://example.invalid:1234")
    sys.modules.pop("dashboard.app", None)
    import dashboard.app as app

    assert app.API_BASE == "http://example.invalid:1234"
    sys.modules.pop("dashboard.app", None)


def test_scrape_timeout_exceeds_the_scrapers_deadline():
    """The dashboard must not give up before the scraper does."""
    sys.modules.pop("dashboard.app", None)
    import dashboard.app as app
    from scraper.live_scraper import load_scraper_config

    deadline = load_scraper_config()["retry"]["overall_deadline_seconds"]
    assert app.SCRAPE_TIMEOUT > deadline, (
        "the dashboard would time out mid-scrape and show a false failure"
    )
