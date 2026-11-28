"""
Tests for news-aggregator's source health tracking (added 2026-10-24).

Why these exist
---------------
The 2026-04-05 grader report (`grader-report-r1.md`) found the skill had no
automated test that covered its actual promise.  Six months later there was
still no test file at all — only the report.  The source-health feature added
on 2026-10-24 is exactly the kind of thing that silently regresses: if
`fetch_rss_feed` goes back to `except: return []`, the script still "works",
the briefing still gets written, and nobody notices that "source died" and
"source has no news" have become indistinguishable again.

Design constraint
------------------
These tests must NOT touch the network.  They exercise the parsing and health
classification logic against fixed XML fixtures, so they are deterministic
and runnable offline.

Run:
    python3 -m pytest skills/news-aggregator/tests/ -q
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

# Make the scripts/ directory importable as a package-less module path.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import rss_parser  # noqa: E402
import fetch_news
import opencli_fetch
import requests


def response(body=b"", status=200, content_type="application/rss+xml", data=None):
    result = requests.Response()
    result.status_code = status
    result.headers["Content-Type"] = content_type
    result._content = body if data is None else json.dumps(data).encode()
    result.url = "https://example.test/feed"
    return result


@pytest.mark.parametrize("status,content_type,body", [
    (404, "text/html", b"<html>Not found</html>"),
    (403, "application/xml", b"<rss><channel/></rss>"),
    (200, "text/html", b"<html>Access denied</html>"),
    (200, "application/xml", b"<html>Access denied</html>"),
])
def test_http_and_html_errors_are_failed(monkeypatch, status, content_type, body):
    monkeypatch.setattr(rss_parser.requests, "get", lambda *a, **k: response(body, status, content_type))
    assert rss_parser.fetch_rss_feed("https://example.test/feed", "Bad") == []
    assert rss_parser.source_health_report()[0]["status"] == "failed"


@pytest.mark.parametrize("summary", ["short summary", "x" * 300, "x" * 301, "<b>brief</b>" * 40])
def test_cdata_title_and_summary_preserved(summary):
    xml = f'<rss><channel><item><title><![CDATA[BBC & Substack]]></title><link>https://example.test/a</link><description><![CDATA[{summary}]]></description></item></channel></rss>'
    item = rss_parser.parse_rss_content(xml.encode(), "BBC")[0]
    assert item["title"] == "BBC & Substack"
    clean = "brief " * 39 + "brief" if summary.startswith("<b>") else summary
    assert item["summary"] == clean[:300] + ("..." if len(clean) > 300 else "")


def test_atom_and_rdf_namespaces():
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom</title><link rel="self" href="https://example.test/feed"/><link rel="alternate" href="https://example.test/article"/><updated>2026-11-28T01:00:00Z</updated><summary>Brief</summary></entry></feed>'
    item = rss_parser.parse_rss_content(atom, "Atom")[0]
    assert item["url"] == "https://example.test/article"
    assert item["summary"] == "Brief"
    rdf = b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><item><title>Nature</title><link>https://example.test/nature</link><dc:date>2026-11-28T01:00:00Z</dc:date></item></rdf:RDF>'
    assert rss_parser.parse_rss_content(rdf, "Nature")[0]["time"] == "2026-11-28T01:00:00Z"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_xml_dtd_entities_are_rejected(encoding):
    xml = '<!DOCTYPE rss [<!ENTITY secret "untrusted">]><rss><channel><item><title>&secret;</title><link>https://example.test</link></item></channel></rss>'
    assert rss_parser.parse_rss_content(xml.encode(encoding), "Unsafe") == []
    assert rss_parser.source_health_report()[0]["status"] == "failed"


@pytest.mark.parametrize("days,status", [(7, "ok"), (13, "ok"), (14, "stale")])
def test_nejm_weekly_staleness_window(days, status):
    date = format_datetime(datetime.now(timezone.utc) - timedelta(days=days, minutes=1))
    xml = f'<rss><channel><item><title>NEJM</title><link>https://example.test</link><pubDate>{date}</pubDate></item></channel></rss>'
    rss_parser.parse_rss_content(xml.encode(), "NEJM Alerts")
    assert rss_parser.source_health_report()[0]["status"] == status


def test_all_tracks_keeps_each_source_quota_and_health(monkeypatch, capsys):
    def fake_get(url, **kwargs):
        if url.endswith("topstories.json"):
            return response(data=[42])
        if url.endswith("item/42.json"):
            return response(data={"id": 42, "time": 1795824000, "title": "Ask HN", "score": 5})
        if url == "https://github.com/trending":
            return response(b'<article class="Box-row"><h2><a href="/org/repo">org/repo</a></h2></article>', content_type="text/html")
        if url == "https://feeds.feedburner.com/rsscna/intworld":
            return response(RSS_WITH_ITEMS.encode())
        if url == "https://www.nature.com/nm.rss":
            return response(RSS_WITH_ITEMS.encode())
        if url == "https://www.statnews.com/feed/":
            return response(RSS_WITH_ITEMS.encode())
        if url == "https://www.esa.int/rssfeed/TopNews":
            return response(RSS_WITH_ITEMS.encode())
        if url == "https://www.astronomy.com/feed/":
            return response(RSS_WITH_ITEMS.encode())
        if url in ["https://www.aljazeera.com/xml/rss/all.xml", "https://feeds.bbci.co.uk/news/world/rss.xml", "https://www.nejm.org/action/showFeed?jc=nejm&type=etoc&feed=rss", "https://www.nasa.gov/news-release/feed/"]:
            return response(RSS_WITH_ITEMS.encode())
        raise AssertionError(f"Unexpected URL: {url}")
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(sys, "argv", ["fetch_news.py", "--all-tracks", "--limit", "1"])
    # No OpenCLI process, network or filesystem writes in this end-to-end CLI path.
    monkeypatch.setattr(opencli_fetch, "run", lambda *a, **k: '[{"title":"Missing id/time"}]')
    fetch_news.main()
    tracks = json.loads(capsys.readouterr().out)
    assert [i["source"] for i in tracks["international"]] == ["Al Jazeera", "BBC World", "CNA International"]
    assert [i["source"] for i in tracks["medical"]] == ["NEJM Alerts", "Nature Medicine", "STAT News"]
    assert [i["source"] for i in tracks["astronomy"]] == ["NASA News", "ESA News", "Astronomy.com"]
    health = {r["source"]: r for r in tracks["_source_health"]}
    assert len(health) == 11
    assert all(r["status"] == "ok" and r["item_count"] == 1 for r in health.values())
    assert tracks["tech"][0]["hn_url"] == "https://news.ycombinator.com/item?id=42"
    assert tracks["tech"][0]["url"] == tracks["tech"][0]["hn_url"]
    assert tracks["tech"][0]["time"] == "2026-11-28T00:00:00+00:00"


@pytest.mark.parametrize("fetcher,name", [(fetch_news.fetch_github, "GitHub Trending"), (opencli_fetch.fetch_hackernews, "Hacker News")])
@pytest.mark.parametrize("kind", ["http", "connection", "empty", "malformed"])
def test_tech_source_health(monkeypatch, fetcher, name, kind):
    def get(*args, **kwargs):
        if kind == "connection":
            raise requests.ConnectionError("offline")
        if kind == "http":
            return response(b"blocked", 403)
        if name == "Hacker News":
            return response(data=[] if kind == "empty" else {"error": "wrong shape"})
        return response(b"<html></html>" if kind == "empty" else b"Access denied", content_type="text/html")
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(opencli_fetch, "run", lambda *a, **k: "")
    assert fetcher() == []
    rec = rss_parser.source_health_report()[0]
    assert rec["source"] == name
    assert rec["status"] == ("empty" if kind == "empty" else "failed")
    assert rec["item_count"] == 0


def test_hn_partial_failure_keeps_valid_stories_and_reports_failure(monkeypatch):
    def get(url, **kwargs):
        if url.endswith("topstories.json"):
            return response(data=[41, 42, 43])
        if url.endswith("item/41.json"):
            return response(data={"id": 41, "time": 1795824000, "title": "Article", "url": "https://example.test/story"})
        if url.endswith("item/42.json"):
            return response(data={"id": 42, "title": "Missing time"})
        return response(status=503)

    monkeypatch.setattr(requests, "get", get)
    items = opencli_fetch.fetch_hackernews(3)
    assert len(items) == 1
    assert items[0]["url"] == "https://example.test/story"
    assert items[0]["hn_url"] == "https://news.ycombinator.com/item?id=41"
    rec = rss_parser.source_health_report()[0]
    assert rec["status"] == "failed"
    assert rec["item_count"] == 1
    assert "missing id/time/title" in rec["detail"]
    assert "503" in rec["detail"]


def test_filtered_out_tech_news_is_still_healthy(monkeypatch):
    def get(url, **kwargs):
        if url.endswith("topstories.json"):
            return response(data=[42])
        if url.endswith("item/42.json"):
            return response(data={"id": 42, "time": 1795824000, "title": "Article"})
        return response(b'<article class="border"><h3><a href="/org/repo">org/repo</a></h3></article>', content_type="text/html")
    monkeypatch.setattr(requests, "get", get)
    assert opencli_fetch.fetch_hackernews(1, "no match") == []
    # Topic matches remain usable even when their titles do not repeat the topic.
    assert fetch_news.fetch_github(1, "python")[0]["url"] == "https://github.com/org/repo"
    assert {r["source"]: r["status"] for r in rss_parser.source_health_report()} == {
        "Hacker News": "ok", "GitHub Trending": "ok",
    }


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_health_state():
    """Clear the module-level SOURCE_HEALTH dict around every test.

    SOURCE_HEALTH is module-level global state (it has to be — the fetcher
    functions are called from many places).  That means tests leak into each
    other unless every test cleans up.  An autouse fixture makes the cleanup
    automatic so no individual test can forget it.
    """
    rss_parser.clear_source_health()
    yield
    rss_parser.clear_source_health()

# Relative to now: a hard-coded "fresh" date goes stale after 7 days and
# turns the ok-path test red on its own.
_NOW = datetime.now(timezone.utc)
_FRESH_1 = format_datetime(_NOW - timedelta(days=1))
_FRESH_2 = format_datetime(_NOW - timedelta(days=2))

RSS_WITH_ITEMS = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Fresh headline</title>
      <link>https://example.com/a</link>
      <pubDate>{_FRESH_1}</pubDate>
      <description>Something happened today.</description>
    </item>
    <item>
      <title>Older headline</title>
      <link>https://example.com/b</link>
      <pubDate>{_FRESH_2}</pubDate>
      <description>Something happened yesterday.</description>
    </item>
  </channel>
</rss>
"""

RSS_EMPTY = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
  </channel>
</rss>
"""

RSS_STALE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Ancient headline</title>
      <link>https://example.com/old</link>
      <pubDate>Sun, 08 Aug 2026 09:00:00 +0200</pubDate>
      <description>Long ago.</description>
    </item>
  </channel>
</rss>
"""

RSS_MALFORMED = "<rss><channel><item><title>broken"


# ── Health recording ────────────────────────────────────────────────────────

def test_record_source_health_stores_all_fields():
    rss_parser.clear_source_health()
    rec = rss_parser.record_source_health("Test Source", "ok", "", 3)
    assert rec["source"] == "Test Source"
    assert rec["status"] == "ok"
    assert rec["detail"] == ""
    assert rec["item_count"] == 3
    assert rss_parser.source_health_report() == [rec]


def test_clear_source_health_resets_between_runs():
    rss_parser.record_source_health("A", "ok", "", 1)
    rss_parser.record_source_health("B", "failed", "boom", 0)
    assert len(rss_parser.source_health_report()) == 2
    rss_parser.clear_source_health()
    assert rss_parser.source_health_report() == []


def test_health_summary_line_mentions_each_source():
    rss_parser.clear_source_health()
    rss_parser.record_source_health("Alpha Feed", "ok", "", 5)
    rss_parser.record_source_health("Beta Feed", "stale", "newest item is 76 days old", 2)
    summary = rss_parser.health_summary_line()
    assert "Alpha Feed" in summary
    assert "Beta Feed" in summary
    assert "stale" in summary
    assert "76 days old" in summary


# ── Classification: the whole point of the feature ─────────────────────────

def test_fresh_feed_is_ok():
    rss_parser.clear_source_health()
    items = rss_parser.parse_rss_content(RSS_WITH_ITEMS.encode("utf-8"), "Fresh Feed", 5)
    assert len(items) == 2
    rec = rss_parser.source_health_report()[0]
    assert rec["status"] == "ok"
    assert rec["item_count"] == 2


def test_empty_feed_is_empty_not_failed():
    """An empty feed is NOT a broken feed — this distinction is the feature."""
    rss_parser.clear_source_health()
    items = rss_parser.parse_rss_content(RSS_EMPTY.encode("utf-8"), "Empty Feed", 5)
    assert items == []
    rec = rss_parser.source_health_report()[0]
    assert rec["status"] == "empty"
    assert rec["item_count"] == 0
    assert "zero items" in rec["detail"]


def test_old_feed_is_stale():
    """A feed that works but stopped publishing is 'stale', not 'ok'."""
    rss_parser.clear_source_health()
    rss_parser.parse_rss_content(RSS_STALE.encode("utf-8"), "Stale Feed", 5)
    rec = rss_parser.source_health_report()[0]
    assert rec["status"] == "stale"
    assert "days old" in rec["detail"]


def test_malformed_feed_is_failed():
    rss_parser.clear_source_health()
    rss_parser.parse_rss_content(RSS_MALFORMED.encode("utf-8"), "Broken Feed", 5)
    rec = rss_parser.source_health_report()[0]
    assert rec["status"] == "failed"
    assert rec["item_count"] == 0


def test_fetch_rss_feed_records_failure_instead_of_silently_returning_empty(monkeypatch):
    """The regression this test exists to prevent.

    Before 2026-10-24, a network failure produced `[]` and a stderr line that
    vanished under the 08:00 cron.  The briefing then had no evidence to
    distinguish "dead source" from "quiet source", and the author guessed.
    """
    rss_parser.clear_source_health()

    def boom(*args, **kwargs):
        raise ConnectionError("dns failure")

    monkeypatch.setattr(rss_parser.requests, "get", boom)
    result = rss_parser.fetch_rss_feed("https://example.invalid/feed", "Dead Feed", 5)

    assert result == []
    rec = rss_parser.source_health_report()[0]
    assert rec["status"] == "failed"
    assert "ConnectionError" in rec["detail"]


# ── Regression guard for the 2026-10-24 incident ───────────────────────────

def test_clean_text_still_exists():
    """Regression: on 2026-10-24 a rename of clean_text -> clear_text during
    an unrelated refactor broke every parse with
    "name 'clean_text' is not defined".  The health records correctly said
    'failed' — which is how the bug was caught — but the rename itself should
    never have happened.
    """
    assert callable(rss_parser.clean_text)
    assert rss_parser.clean_text("  hi  ") == "hi"
    assert rss_parser.clean_text(None) == ""


def test_parse_pub_datetime_handles_missing_and_bad_input():
    assert rss_parser._parse_pub_datetime("") is None
    assert rss_parser._parse_pub_datetime("not a date") is None
    dt = rss_parser._parse_pub_datetime("Mon, 20 Oct 2026 09:00:00 +0200")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 10 and dt.day == 20


# ── Precondition guard scope (added 2026-10-24) ────────────────────────────

def _guard():
    """Resolve the skill's run_bash guard the same way the harness does."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    # Import the guard directly: exercising it must never record trusted hashes.
    import importlib.util
    path = SCRIPTS_DIR.parent / "checks.py"
    spec = importlib.util.spec_from_file_location("news_checks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.scripts_only_bash


def _check(command):
    guard = _guard()

    class _Call:
        def __init__(self, c):
            self.args = {"command": c}

    # Python 3.14 removed the implicit event loop — use asyncio.run.
    return asyncio.run(guard(_Call(command)))


def test_guard_allows_the_morning_briefing_pipeline():
    """The 08:00 briefing runs unattended.  A guard that blocks it is worse
    than no guard.  These are the exact commands the schedule uses.
    """
    assert _check("cd skills/news-aggregator/scripts && python fetch_news.py --all-tracks --limit 8")
    assert _check("cd skills/news-aggregator/scripts && python3 fetch_news.py --all-tracks --limit 8")
    assert not _check("mkdir -p news/2026-10-24")
    assert not _check("mkdir news/2026-10-24")


def test_guard_denies_arbitrary_commands():
    assert not _check("rm -rf /")
    assert not _check("ls news/")
    assert not _check("cat ~/.loom/pursuits/dk-taiwan-stocks.md")
    assert not _check("mkdir -p /etc/foo")


def test_guard_denies_path_traversal():
    """`news/../etc/x` normalizes to `etc/x` — must not slip through."""
    assert not _check("mkdir -p news/../etc/x")


# ── Frontmatter contract ───────────────────────────────────────────────────

def test_skill_frontmatter_uses_ref_not_check():
    """Regression: news-aggregator's precondition_checks used the key
    'check:' while Loom's parser only accepts 'ref:'.  The guard was silently
    dropped for four months — it looked like protection and was not.

    This asserts the frontmatter shape so the mismatch cannot come back.
    """
    skill_md = (Path(__file__).resolve().parent.parent / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "precondition_checks:" in skill_md
    assert "- ref: checks.scripts_only_bash" in skill_md
    assert "- check:" not in skill_md, (
        "precondition_checks must use 'ref:' — Loom's parser only accepts 'ref:' "
        "and silently discards entries with any other key"
    )
