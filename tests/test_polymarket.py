"""Polymarket 采集器测试：纯函数 + 离线种子/变化检测。"""
import asyncio
from datetime import datetime

from app.collectors import polymarket as pm


def _event(price: str = "0.4") -> dict:
    return {
        "id": "event-1",
        "title": "Will the Fed cut rates in December?",
        "slug": "fed-cut-december",
        "volume24hr": 1_000_000,
        "markets": [{
            "conditionId": "condition-1",
            "question": "Fed cuts rates in December?",
            "outcomePrices": f'["{price}", "{1 - float(price):.2f}"]',
            "clobTokenIds": '["token-yes", "token-no"]',
            "volume24hr": 900_000,
            "liquidity": 250_000,
            "oneDayPriceChange": "0.03",
        }],
    }


def test_polymarket_helpers_pick_liquid_market_and_book():
    event = _event()
    event["markets"].append({
        "conditionId": "condition-low",
        "question": "Low volume",
        "volume24hr": 10,
    })
    market = pm.pick_market(event)
    assert market["conditionId"] == "condition-1"
    assert pm.market_price(market) == 0.4
    assert pm.market_token(market) == "token-yes"
    assert pm.best_bid_ask({
        "bids": [{"price": "0.38"}, {"price": "0.39"}],
        "asks": [{"price": "0.42"}, {"price": "0.41"}],
    }) == (0.39, 0.41)


def test_format_item_contains_probability_and_link():
    event = _event()
    market = event["markets"][0]
    item = pm.format_item(
        event, market, now=datetime(2026, 10, 21, 12),
        book={"bids": [{"price": "0.39"}], "asks": [{"price": "0.41"}]},
        price_payload={"price": "0.40"},
        holders=[{"pseudonym": "whale", "amount": 12345}],
        trades=[{"side": "BUY", "price": "0.40", "size": 500}],
    )
    assert item is not None
    assert item["source"] == "polymarket"
    assert item["url"] == "https://polymarket.com/event/fed-cut-december"
    assert "隐含概率 40.0%" in item["text"]
    assert "买一/卖一 0.39/0.41" in item["text"]
    assert "whale" in item["text"]
    assert item["tier"] == "authoritative"


def test_fetch_polymarket_seeds_then_emits_on_move(monkeypatch):
    pm._LAST.clear()
    event = _event("0.40")

    async def fake_safe(client, url, params=None):
        return [event]

    async def fake_details(client, market):
        return {"book": None, "price_payload": None, "holders": None, "trades": None}

    monkeypatch.setattr(pm, "_safe_json", fake_safe)
    monkeypatch.setattr(pm, "_details", fake_details)
    cfg = {
        "tags": ["politics"], "max_events": 8,
        "min_volume_24h": 1000, "move_threshold": 0.05,
    }
    try:
        assert asyncio.run(pm.fetch_polymarket(cfg)) == []
        event["markets"][0]["outcomePrices"] = '["0.50", "0.50"]'
        rows = asyncio.run(pm.fetch_polymarket(cfg))
        assert len(rows) == 1
        assert rows[0]["source"] == "polymarket"
        assert "隐含概率 50.0%" in rows[0]["text"]
    finally:
        pm._LAST.clear()


def test_baseline_survives_market_temporarily_missing(monkeypatch):
    """掉榜一轮不能清空基线，否则回归后首次变化被当成 seed 丢掉。"""
    pm._LAST.clear()
    event = _event("0.40")
    other = _event("0.30")
    other["id"] = "event-2"
    other["markets"][0]["conditionId"] = "condition-2"
    responses = [[event], [other], [event]]

    async def fake_safe(client, url, params=None):
        return responses.pop(0)

    async def fake_details(client, market):
        return {"book": None, "price_payload": None, "holders": None, "trades": None}

    monkeypatch.setattr(pm, "_safe_json", fake_safe)
    monkeypatch.setattr(pm, "_details", fake_details)
    cfg = {
        "tags": ["politics"], "max_events": 8,
        "min_volume_24h": 1000, "move_threshold": 0.05,
    }
    try:
        assert asyncio.run(pm.fetch_polymarket(cfg)) == []
        assert asyncio.run(pm.fetch_polymarket(cfg)) == []
        event["markets"][0]["outcomePrices"] = '["0.50", "0.50"]'
        rows = asyncio.run(pm.fetch_polymarket(cfg))
        assert len(rows) == 1
        assert "隐含概率 50.0%" in rows[0]["text"]
    finally:
        pm._LAST.clear()
