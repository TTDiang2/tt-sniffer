"""crypto_rss 采集器测试（全离线：内联 RSS 夹具，不触网）。"""
import asyncio
from datetime import datetime, timezone

from app.collectors.crypto_rss import parse_rss
from app.models import Item
from app.pipeline import Pipeline
from app.scoring import Scorer
from app.store import Store

RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>WuBlockchain</title>
<item>
  <title>Market Rumor: Justin Sun reportedly safe, shooting claim unverified</title>
  <link>https://wublock.substack.com/p/rumor-justin-sun</link>
  <description>&lt;p&gt;网传&lt;b&gt;孙宇晨&lt;/b&gt;遭枪击，多方信源未证实；TRX 短时波动。&lt;/p&gt;</description>
  <pubDate>Sat, 04 Oct 2026 03:00:00 +0000</pubDate>
</item>
<item>
  <title>Weekly Report: Ethereum evolves</title>
  <link>https://wublock.substack.com/p/weekly</link>
  <description>Weekly digest.</description>
  <pubDate>Sat, 04 Oct 2026 01:00:00 +0000</pubDate>
</item>
<item>
  <title>太旧的帖子</title>
  <link>https://example/old</link>
  <description>2020 年的内容。</description>
  <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
</item>
</channel></rss>"""


def test_parse_rss_fields_and_age_filter():
    rows = parse_rss(RSS_FIXTURE, "rss_wu", now=datetime(2026, 10, 4, 12, 0))
    assert [x["source"] for x in rows] == ["rss_wu"] * len(rows)
    titles = [r["title"] for r in rows]
    assert any("Justin Sun" in t for t in titles)
    assert not any("太旧" in t for t in titles)          # max_age 过滤
    rumor = next(r for r in rows if "Justin" in r["title"])
    assert "孙宇晨" in rumor["text"]                     # HTML 实体/标签已清洗
    assert rumor["url"].startswith("https://wublock")
    # A2 时区修复：GMT 03:00 先转本机（本机 Asia/Shanghai 为 11:00）再剥 tzinfo；
    # 用 astimezone() 计算期望值，避免 CI 在 UTC 机器上误判。
    expected = datetime(2026, 10, 4, 3, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert rumor["ts"] == expected


def test_parse_rss_gmt_pubdate_is_localized():
    """A2 回归：GMT 11:53:43 在本机（中国标准时间）应落为 19:53:43。"""
    body = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><item>
      <title>Russia to Fully Ban P2P</title>
      <link>https://wublock.substack.com/p/ban-p2p</link>
      <description>Crypto exchange rules tighten.</description>
      <pubDate>Tue, 20 Oct 2026 11:53:43 GMT</pubDate>
    </item></channel></rss>"""
    rows = parse_rss(body, "rss_wu", max_age_days=10000,
                     now=datetime(2026, 10, 20, 23, 0))
    assert len(rows) == 1
    expected = datetime(2026, 10, 20, 11, 53, 43,
                        tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert rows[0]["ts"] == expected


def test_crypto_rumor_passes_market_gate():
    """加密市场词配套：强词+币圈词 → rumor 不再被市场门槛打成噪音。"""
    s = Scorer([])
    sc = s.score("网传孙宇晨遭枪击", "网传孙宇晨遇袭，TRX 与比特币短时波动")
    assert sc["cls"] == "rumor"
    assert sc["score"] >= 55
    # 反向：无加密词、无其他市场词的纯人名传闻仍是 noise（门槛语义不放松）
    sc2 = s.score("网传孙宇晨私人行程", "网传孙宇晨出行细节")
    assert sc2["cls"] == "noise"


def test_pipeline_item_tier_override():
    """crypto_rss 的 feed 级 tier 经 raw.tier 覆盖 TIER_BY_SOURCE 缺省。"""
    import tempfile, pathlib
    p = Pipeline(Store(pathlib.Path(tempfile.mkdtemp()) / "t.db"))
    raw = {"source": "rss_wu", "ts": datetime.now(),
           "title": "网传孙宇晨遭枪击", "text": "TRX 比特币波动",
           "url": "https://x/y", "tier": "street"}
    asyncio.run(p._ingest("rss_wu", [raw]))
    row = p.store.query(cls="all")["items"][0]
    assert row["tier"] == "street" and row["source"] == "rss_wu"
