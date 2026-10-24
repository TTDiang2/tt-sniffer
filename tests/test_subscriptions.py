"""声明式订阅测试（订阅化阶段一）：加载校验/热加载/重复 id/管道接线。

纪律：全部用 tmp_path 临时目录与内联 YAML，不触网。
"""
import asyncio
from datetime import datetime
from pathlib import Path

from app import subscriptions as sc
from app.pipeline import Pipeline
from app.store import Store

YAML_OK = """
apiVersion: ttsniffer/v1
kind: Subscription
metadata: {id: my-feed, name: 我的订阅}
spec:
  kind: rss
  url: https://example.com/feed
  source: my_feed
  tier: street
  max_age_days: 3
  interval: 300
"""


def _write(d: Path, name: str, text: str):
    (d / name).write_text(text, encoding="utf-8")


def test_load_valid_subscription_defaults(tmp_path):
    _write(tmp_path, "a.yaml", YAML_OK)
    subs = sc.load_subscriptions(tmp_path)
    assert len(subs) == 1
    s = subs[0]
    assert s["id"] == "my-feed" and s["source"] == "my_feed"
    assert s["tier"] == "street" and s["max_age_days"] == 3
    assert s["interval"] == 300 and s["enabled"] is True
    assert sc.as_feed_cfg(subs)["feeds"][0]["url"] == "https://example.com/feed"
    assert sc.cycle_seconds(subs) == 300


def test_load_minimal_uses_defaults(tmp_path):
    _write(tmp_path, "min.yaml", """
apiVersion: ttsniffer/v1
metadata: {id: min1}
spec: {url: "https://x.example/rss"}
""")
    s = sc.load_subscriptions(tmp_path)[0]
    assert s["source"] == "min1" and s["name"] == "min1"
    assert s["tier"] == "street" and s["max_age_days"] == 2
    assert s["interval"] == 600


def test_load_skips_invalid_and_disabled(tmp_path):
    bads = [
        ("no_apiver.yaml", "metadata: {id: x}\nspec: {url: 'https://x/1'}"),
        ("bad_id.yaml", "apiVersion: ttsniffer/v1\nmetadata: {id: '非法!id'}\n"
                        "spec: {url: 'https://x/1'}"),
        ("bad_url.yaml", "apiVersion: ttsniffer/v1\nmetadata: {id: x1}\n"
                         "spec: {url: 'ftp://x/1'}"),
        ("bad_kind.yaml", "apiVersion: ttsniffer/v1\nmetadata: {id: x2}\n"
                          "spec: {kind: adapter, url: 'https://x/1'}"),
        ("not_yaml Mapping is list", "- a\n- b"),
        ("broken.yaml", "apiVersion: [unclosed"),
        ("off.yaml", "apiVersion: ttsniffer/v1\nmetadata: {id: off1}\n"
                     "spec: {url: 'https://x/1', enabled: false}"),
    ]
    for name, text in bads:
        _write(tmp_path, name, text)
    assert sc.load_subscriptions(tmp_path) == []      # 全部被跳过，不抛错


def test_load_duplicate_id_first_wins(tmp_path):
    _write(tmp_path, "a.yaml", YAML_OK)
    _write(tmp_path, "b.yaml", YAML_OK)               # 同 id=my-feed
    subs = sc.load_subscriptions(tmp_path)
    assert len(subs) == 1


def test_hot_reload_on_file_change(tmp_path):
    _write(tmp_path, "a.yaml", YAML_OK)
    assert len(sc.load_subscriptions(tmp_path)) == 1
    # 目录没变 → 走缓存（同对象语义：内容一致）
    assert len(sc.load_subscriptions(tmp_path)) == 1
    # 新增文件（mtime 变化）→ 热加载进来
    _write(tmp_path, "b.yaml", "apiVersion: ttsniffer/v1\nmetadata: {id: b2}\n"
                               "spec: {url: 'https://x.example/2'}\n")
    subs = sc.load_subscriptions(tmp_path)
    assert [s["id"] for s in subs] == ["my-feed", "b2"]


def test_missing_dir_returns_empty(tmp_path):
    assert sc.load_subscriptions(tmp_path / "nope") == []


def test_subscriptions_cycle_ingests_with_sub_tier(tmp_path, monkeypatch):
    """订阅→feeds 透传→tier 落库：接线闭环（fetch 打桩，不触网）。"""
    from app.collectors import rss as rss_c

    async def fake_fetch(cfg, on_status=None, default_tier="authoritative"):
        rows = []
        for feed in cfg.get("feeds") or []:
            for raw in sc_rows(feed["source"]):
                raw["tier"] = feed.get("tier", default_tier)
                rows.append(raw)
            if on_status:
                on_status(feed["source"], True, "2条")
        return rows

    def sc_rows(source):
        return [{"source": source, "ts": datetime(2026, 10, 24, 9, 0),
                 "title": "标题一", "text": "正文", "url": "https://x/1"},
                {"source": source, "ts": datetime(2026, 10, 24, 9, 1),
                 "title": "标题二", "text": "正文", "url": "https://x/2"}]

    monkeypatch.setattr(rss_c, "fetch_rss", fake_fetch)
    p = Pipeline(Store(tmp_path / "t.db"))
    subs = [{"id": "s1", "name": "S1", "source": "sub_src",
             "url": "https://x/1", "tier": "street", "max_age_days": 2,
             "interval": 600, "enabled": True}]
    asyncio.run(p._rss_cycle(sc.as_feed_cfg(subs), default_tier="street"))
    rows = p.store.query(cls="all")["items"]
    assert len(rows) == 2 and rows[0]["source"] == "sub_src"
    assert rows[0]["tier"] == "street"
    assert p.collector_status.get("sub_src", {}).get("ok") is True
