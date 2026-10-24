"""外网 RSS 采集器测试：全部离线，不触网。"""
import asyncio
from datetime import datetime, timezone

from app.collectors import rss
from app.config import DEFAULTS

RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item>
  <title>Bloomberg Markets headline</title>
  <link>https://www.bloomberg.com/news/articles/example</link>
  <description><![CDATA[<p>Bloomberg summary text.</p>]]></description>
  <pubDate>Sun, 04 Oct 2026 03:00:00 +0000</pubDate>
</item>
<item>
  <title>Second headline</title>
  <link>https://example.com/second</link>
  <description>Second summary.</description>
  <pubDate>Sun, 04 Oct 2026 02:00:00 +0000</pubDate>
</item>
</channel></rss>"""


class _Response:
    text = RSS_FIXTURE

    def raise_for_status(self):
        return None


class _Client:
    calls: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        self.calls.append(url)
        if "bad" in url:
            raise RuntimeError("feed down")
        return _Response()


def test_parse_rss_localizes_pubdate():
    rows = rss.parse_rss(
        RSS_FIXTURE, "bbg_markets", max_age_days=10000,
        now=datetime(2026, 10, 4, 12), to_local=True)
    assert rows
    expected = datetime(2026, 10, 4, 3, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert rows[0]["ts"] == expected
    assert rows[0]["text"] == "Bloomberg summary text."


def test_fetch_rss_uses_feed_source_and_tier(monkeypatch):
    _Client.calls = []
    monkeypatch.setattr(rss.httpx, "AsyncClient", _Client)
    cfg = {
        "max_age_days": 10000,
        "feeds": [
            {"source": "bbg_markets", "url": "https://example.test/bbg",
             "tier": "authoritative", "enabled": True},
            {"source": "disabled", "url": "https://example.test/off",
             "enabled": False},
        ],
    }
    rows = asyncio.run(rss.fetch_rss(cfg))
    assert len(rows) == 2
    assert {row["source"] for row in rows} == {"bbg_markets"}
    assert {row["tier"] for row in rows} == {"authoritative"}
    assert _Client.calls == ["https://example.test/bbg"]


def test_fetch_rss_reports_each_feed_status(monkeypatch):
    monkeypatch.setattr(rss.httpx, "AsyncClient", _Client)
    statuses: list[tuple[str, bool, str]] = []
    cfg = {
        "max_age_days": 10000,
        "feeds": [
            {"source": "bbg_markets", "url": "https://example.test/ok", "enabled": True},
            {"source": "wsj_markets", "url": "https://example.test/bad", "enabled": True},
        ],
    }
    rows = asyncio.run(rss.fetch_rss(cfg, on_status=lambda *a: statuses.append(a)))
    assert len(rows) == 2
    assert statuses == [("bbg_markets", True, "2条"), ("wsj_markets", False, "feed down")]


def test_default_config_contains_confirmed_foreign_feeds():
    feeds = DEFAULTS["collect"]["rss"]["feeds"]
    sources = {feed["source"] for feed in feeds}
    assert {
        "bbg_markets", "bbg_econ", "bbg_politics", "bbg_tech", "bbg_google",
        "wsj_markets", "wsj_world", "wsj_business", "wsj_tech", "marketwatch",
        "reuters", "ap", "ft", "cnbc", "google_news",
    } <= sources
    assert DEFAULTS["collect"]["polymarket"]["tags"] == ["politics", "crypto"]
    assert DEFAULTS["collect"]["polymarket"]["enabled"] is True
