"""API 测试：关注页签的服务端映射、基础端点可用性。"""
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.models import Item
from app.pipeline import Pipeline
from app.server import create_app
from app.store import Store


def make_client(tmp_path):
    store = Store(tmp_path / "t.db")
    pipeline = Pipeline(store)
    return TestClient(create_app(store, pipeline)), store


def _seed(store):
    store.insert(Item(id="w1", source="cls", ts=datetime.now(), title="关注传闻",
                      text="市场传闻贵州茅台将提价", url="", cls="rumor", score=90,
                      keywords=["传闻"], targets=["贵州茅台"], watch=True))
    store.insert(Item(id="n1", source="sina", ts=datetime.now(), title="",
                      text="普通快讯一条", url="", cls="noise", score=10))


def test_watch_tab_mapping(tmp_path):
    """回归：'关注'页签发 cls=watch 曾查出0行。"""
    client, store = make_client(tmp_path)
    _seed(store)
    r = client.get("/api/items", params={"cls": "watch"}).json()
    assert len(r["items"]) == 1
    assert r["items"][0]["watch"] is True


def test_cls_filter(tmp_path):
    client, store = make_client(tmp_path)
    _seed(store)
    assert len(client.get("/api/items", params={"cls": "rumor"}).json()["items"]) == 1
    assert len(client.get("/api/items", params={"cls": "all"}).json()["items"]) == 2
    assert len(client.get("/api/items", params={"q": "普通"}).json()["items"]) == 1


def test_index_and_stats(tmp_path):
    client, _ = make_client(tmp_path)
    r = client.get("/")
    assert r.status_code == 200
    assert "TT_Sniffer" in r.text
    assert client.get("/api/stats").status_code == 200
    assert client.get("/api/hot").status_code == 200
    assert client.get("/api/anomaly").status_code == 200


def test_pwa_assets(tmp_path):
    client, _ = make_client(tmp_path)
    html = client.get("/").text
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert 'rel="apple-touch-icon"' in html
    assert 'name="apple-mobile-web-app-capable"' in html

    man = client.get("/manifest.webmanifest")
    assert man.status_code == 200
    assert man.headers["content-type"].startswith("application/manifest+json")
    data = man.json()
    assert data["display"] == "standalone" and data["start_url"] == "/"
    assert {i["sizes"] for i in data["icons"]} >= {"192x192", "512x512"}

    sw = client.get("/sw.js")
    assert sw.status_code == 200
    assert sw.headers["service-worker-allowed"] == "/"
    assert sw.headers["cache-control"] == "no-cache"
    assert "caches" not in sw.text

    icon = client.get("/icons/icon-192.png")
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/png"
    assert client.get("/icons/nope.png").status_code == 404


def test_limit_clamped(tmp_path):
    """回归A1-5：limit=-5 曾借 SQLite 负 LIMIT 语义返回全表；钳位后最多 1 条。"""
    client, store = make_client(tmp_path)
    _seed(store)
    assert len(client.get("/api/items", params={"limit": -5}).json()["items"]) == 1
    assert len(client.get("/api/items", params={"limit": 1}).json()["items"]) == 1
    assert len(client.get("/api/items", params={"limit": 0}).json()["items"]) == 1


def test_date_picker_max_in_html():
    """回归A1-6：date picker 必须由前端设 max=今天，禁选未来日期。
    P4 起前端源码在 app/web/src/（React，max 属性静态可断言）。"""
    src = (Path(__file__).resolve().parent.parent
           / "app" / "web" / "src" / "components" / "shell.tsx").read_text(encoding="utf-8")
    assert 'type="date"' in src and "max={today}" in src


def test_reload_sequence_token_in_html():
    """回归A1-4：reload 必须带单调时序令牌，过期响应整包丢弃（含错误分支）。
    P4 起时序令牌在 src/store.ts。"""
    src = (Path(__file__).resolve().parent.parent
           / "app" / "web" / "src" / "store.ts").read_text(encoding="utf-8")
    assert "reloadSeq" in src
    # 成功与失败两个分支都要有过期丢弃
    assert src.count("if (seq !== reloadSeq) return") >= 2


def test_items_group_and_scatter_endpoints(tmp_path):
    """R3：group=1 与 /api/scatter 的 HTTP 层覆盖。"""
    from datetime import datetime, timedelta
    client, store = make_client(tmp_path)
    now = datetime.now()
    store.insert(Item(id="s1", source="cls", ts=now, title="",
                      text="美国9月非农就业人数增加2.9万人 大幅低于预期", url="",
                      cls="rumor", score=88, keywords=[], targets=[], watch=False,
                      impacts=[{"a": "us_stock", "d": "bull", "k": "us_data_weak",
                                "g": "guess"}]))
    store.insert(Item(id="s2", source="em", ts=now, title="",
                      text="美国9月非农就业人数增加29000", url="",
                      cls="noise", score=10))
    r = client.get("/api/items", params={"group": 1, "day": str(now.date())}).json()
    assert r["total"] == 1 and r["total_raw"] == 2   # 两组→1 组（同稿）
    rep = r["items"][0]
    assert rep["group_size"] == 2 and rep["group_members"]
    r2 = client.get("/api/items", params={"day": str(now.date())}).json()  # group=0 不变
    assert "group_key" not in r2["items"][0] and r2["total"] == 2
    sc = client.get("/api/scatter", params={"day": str(now.date())}).json()
    assert "trail" in sc
    # v7 簇聚合：s1（有方向）成簇，s2（无 impacts）正确地不进散点
    assert sum(c["n"] for c in sc["clusters"]) == 1
    c1 = sc["clusters"][0]
    # v9 连续强度：单张方向票 = tanh(1/2)×100 ≈ 46（不再钉死轴端 ±100）
    assert c1["x"] == 46 and c1["y"] == 0 and c1["n"] == 1
    assert c1["items"][0]["id"] == "s1"



def test_watchlist_seed_then_user_layer(tmp_path):
    """S2：未设置时 GET 返回 config 种子；PUT 全量替换用户层并回读。"""
    client, store = make_client(tmp_path)
    r = client.get("/api/watchlist").json()
    assert r["source"] == "config"
    assert any(it["name"] == "贵州茅台" for it in r["items"])
    items = [{"code": "600519", "name": "贵州茅台"}, {"code": "", "name": "国债期货"}]
    r = client.put("/api/watchlist", json=items).json()
    assert r["source"] == "user" and len(r["items"]) == 2
    r = client.get("/api/watchlist").json()
    assert r["source"] == "user"
    assert {it["name"] for it in r["items"]} == {"贵州茅台", "国债期货"}


def test_watchlist_put_validation_and_empty(tmp_path):
    """S2：无 name 行拒收、去重、空列表合法（清空关注也是合法用户层）。"""
    client, store = make_client(tmp_path)
    r = client.put("/api/watchlist", json=[
        {"code": "1"},                 # 无 name → 丢弃
        {"name": "   "},               # 纯空白 name → 丢弃
        {"code": "600519", "name": "贵州茅台"},
        {"code": "600519", "name": "贵州茅台"},   # 重复 → 去重
    ]).json()
    assert r["items"] == [{"code": "600519", "name": "贵州茅台"}]
    r = client.put("/api/watchlist", json=[]).json()
    assert r["items"] == [] and r["source"] == "user"
    assert store.get_watchlist_user() == []      # 空列表也是"已设置"，不再回落种子


def test_watchlist_hot_update_scoring(tmp_path):
    """S2：PUT 后打分器热更新——新表名称命中即打 watch 标（无需重启）。"""
    client, store = make_client(tmp_path)
    pipe = client.app.state.pipeline
    assert "贵州茅台" in pipe.scorer.watch_names          # 种子
    client.put("/api/watchlist", json=[{"code": "", "name": "国债期货"}])
    assert pipe.scorer.watch_names == ["国债期货"]
    assert pipe.scorer.watch_codes == set()
    sc = pipe.scorer.score("快讯", "网传国债期货明日放量，机构称利好出尽")
    assert sc["watch"] is True and sc["targets"] == ["国债期货"]
    # 旧种子名称不再命中
    sc2 = pipe.scorer.score("快讯", "市场传闻贵州茅台提价")
    assert sc2["targets"] == [] and sc2["watch"] is False


def test_watchlist_restart_inheritance(tmp_path):
    """S2 门5 #1：kv 用户层在 Pipeline 构造时回灌 scorer——重启（新 Pipeline）不失忆。"""
    store = Store(tmp_path / "t.db")
    store.set_watchlist_user([{"code": "", "name": "国债期货"}])
    pipeline = Pipeline(store)
    assert pipeline.scorer.watch_names == ["国债期货"]
    assert "贵州茅台" not in pipeline.scorer.watch_names      # 种子被用户层覆盖
    # 无用户层的库：保持 config 种子（既有行为）
    store2 = Store(tmp_path / "t2.db")
    pipeline2 = Pipeline(store2)
    assert "贵州茅台" in pipeline2.scorer.watch_names


def test_watchlist_put_length_limits(tmp_path):
    """S2 门5 #3：超长拒收——name>60 / code>12 的行被丢弃，合法行保留。"""
    client, store = make_client(tmp_path)
    r = client.put("/api/watchlist", json=[
        {"code": "1" * 13, "name": "超长代码"},
        {"code": "x", "name": "名" * 61},
        {"code": "600519", "name": "贵州茅台"},
    ]).json()
    assert r["items"] == [{"code": "600519", "name": "贵州茅台"}]

def test_direction_filter_standalone(tmp_path):
    """v26：d 无 asset 时独立生效=只看带该方向标记的条目（用户需求：利多/利空全局筛选）。"""
    from datetime import datetime
    client, store = make_client(tmp_path)
    now = datetime.now()
    store.insert(Item(id="d1", source="cls", ts=now, title="", text="金价大涨创历史新高",
                      url="", cls="rumor", score=70,
                      impacts=[{"a": "gold", "d": "bull", "k": "fed_ease", "g": "fact"}]))
    store.insert(Item(id="d2", source="cls", ts=now, title="", text="金价大跌2%",
                      url="", cls="rumor", score=70,
                      impacts=[{"a": "gold", "d": "bear", "k": "r1", "g": "fact"}]))
    store.insert(Item(id="d3", source="cls", ts=now, title="", text="无影响标记的普通快讯",
                      url="", cls="noise", score=10))
    bull = client.get("/api/items", params={"d": "bull"}).json()
    assert [i["id"] for i in bull["items"]] == ["d1"]
    bear = client.get("/api/items", params={"d": "bear"}).json()
    assert [i["id"] for i in bear["items"]] == ["d2"]
    # 与资产联动（既有行为不回归）：gold+bear
    both = client.get("/api/items", params={"asset": "gold", "d": "bear"}).json()
    assert [i["id"] for i in both["items"]] == ["d2"]


def test_invalid_day_rejected_with_400(tmp_path):
    """回归 F-005：非法 day 曾一路进入 date.fromisoformat 变成 500。"""
    client, _ = make_client(tmp_path)
    paths = ("/api/items", "/api/dotplot", "/api/scatter", "/api/day",
             "/api/sentiment", "/api/summary", "/api/collide")
    for path in paths:
        for bad in ("2026-02-30", "2026-1-1", "not-a-day"):
            r = client.get(path, params={"day": bad})
            assert r.status_code == 400, (path, bad, r.status_code, r.text)


def test_security_headers_and_healthz(tmp_path):
    client, _ = make_client(tmp_path)
    r = client.get("/api/items")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "object-src 'none'" in csp
    assert "strict-transport-security" not in r.headers   # 明文测试客户端不加 HSTS
    h = client.get("/healthz")
    assert h.status_code == 200
    assert h.json() == {"ok": True, "store_ok": True}


def test_ai_chat_rejects_bad_json_and_rate_limits(tmp_path, monkeypatch):
    """回归 F-007/F-014：非法 JSON 400；每 IP 每分钟超过 10 次 429。"""
    from app.config import CONFIG
    monkeypatch.setitem(CONFIG, "llm", {**CONFIG.get("llm", {}), "enabled": False})
    client, _ = make_client(tmp_path)
    bad = client.post("/api/ai/chat", content=b"not json",
                      headers={"content-type": "application/json"})
    assert bad.status_code == 400
    assert client.post("/api/ai/chat", json=[]).status_code == 400
    assert client.post("/api/ai/chat", json={"messages": "x"}).status_code == 400
    r = client.post("/api/ai/chat", json={"messages": [{}] * 41})
    assert r.status_code == 200
    assert r.headers["x-history-truncated"] == "1"
    for _ in range(9):
        r = client.post("/api/ai/chat", json={"messages": []})
        assert r.status_code == 200
    r = client.post("/api/ai/chat", json={"messages": []})
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


def test_ai_status_endpoint(tmp_path, monkeypatch):
    """v26：/api/ai/status 恒 200，enabled=config.enabled 且有 key（未配=false 不炸）。
    S0 加固：机器配了真 key + llm.enabled=true 时不再吃环境——隔离 env 与
    CONFIG，断言「未启用」分支。"""
    monkeypatch.delenv("TTSNIFFER_LLM_API_KEY", raising=False)
    from app.config import CONFIG
    monkeypatch.setitem(CONFIG, "llm", {**CONFIG.get("llm", {}), "enabled": False})
    client, _ = make_client(tmp_path)
    r = client.get("/api/ai/status").json()
    assert set(r) >= {"enabled", "configured", "has_key", "model"}
    assert r["enabled"] is False
    assert set(r["channels"]) == {"translate", "comment", "chat"}
    for ch in r["channels"].values():
        assert set(ch) == {"model", "ready"}
    assert set(r["translation"]) == {"pending", "workers", "failed"}
    assert "base_url" not in r and "api_key" not in r


def test_ai_rate_table_drops_stale_ips():
    """F-209：ai_hits 超过 256 个 IP 时清掉无近期请求的条目，避免无限增长。"""
    from types import SimpleNamespace
    from app.server import _ai_rate_ok

    table = {f"ip{i}": [] for i in range(257)}
    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(ai_hits=table)),
        headers={}, client=SimpleNamespace(host="new-ip"))
    assert _ai_rate_ok(req) is True
    assert set(table) == {"new-ip"}


def test_calendar_and_rates_endpoints(tmp_path, monkeypatch):
    """F-201：宏观日历与 CME 利率快照接口可读，参数钳位并透传筛选。"""
    from app.collectors import calendar as cal
    from app.collectors import rates_snap as rates

    client, _ = make_client(tmp_path)
    seen = {}

    def fake_read_events(**kw):
        seen.update(kw)
        return [{"event_id": "e1", "ts_release": "2026-10-22 20:30:00",
                 "country": "US", "metric": "CPI"}]

    monkeypatch.setattr(cal, "read_events", fake_read_events)
    monkeypatch.setattr(cal, "fomc_missing_years", lambda a, b: [2028])
    r = client.get("/api/calendar", params={"days": 999, "country": "US"}).json()
    assert r["days"] == 60
    assert r["events"][0]["metric"] == "CPI"
    assert "2028" in r["fomc_schedule_warning"]
    assert seen["country"] == "US" and seen["limit"] == 500

    rows = [{"date": "2026-10-21", "settle_front": 96.2,
             "settle_next": 96.1, "implied_front": 3.8}]
    monkeypatch.setattr(rates, "read_snapshots", lambda limit=30: rows)
    r = client.get("/api/rates", params={"limit": 0}).json()
    assert r["rows"] == rows and r["latest"] == rows[0]
