"""Polymarket 公共 API 采集器（Gamma 事件/市场 + CLOB 盘口 + Data 持仓/成交）。

不绕过登录、不需要 API key；只使用 Polymarket 公开市场数据：
- Gamma: 活跃事件/市场元数据（按 24h 成交量排序，tags 过滤掉体育噪音）
- CLOB: 选中 token 的买价与 order book
- Data: 选中市场的持有人与最近成交

只在“隐含概率相对上次轮询变化 >= move_threshold”时产出一条快照，首次轮询只
建立基线，避免启动/重启时一次灌入几十条。基线保存在进程内，重启后重新建立。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger("collector")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"

# conditionId -> 上次看到的 Yes 概率；仅进程内，重启后重新 seed。
_LAST: dict[str, float] = {}


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except (TypeError, ValueError):
            return []
        return data if isinstance(data, list) else []
    return []


def pick_market(event: dict) -> dict | None:
    """事件可能含多个市场；取 24h 成交量最大的一个。"""
    markets = event.get("markets")
    if not isinstance(markets, list):
        return None
    best: dict | None = None
    best_volume = -1.0
    for market in markets:
        if not isinstance(market, dict):
            continue
        volume = _num(market.get("volume24hr") or market.get("volume")) or 0.0
        if best is None or volume > best_volume:
            best, best_volume = market, volume
    return best


def market_price(market: dict) -> float | None:
    """Yes/第一个 outcome 的当前概率。"""
    prices = _as_list(market.get("outcomePrices"))
    if prices:
        price = _num(prices[0])
        if price is not None:
            return price
    return _num(market.get("lastTradePrice"))


def market_token(market: dict) -> str | None:
    tokens = _as_list(market.get("clobTokenIds"))
    return str(tokens[0]) if tokens else None


def best_bid_ask(book: Any) -> tuple[float | None, float | None]:
    if not isinstance(book, dict):
        return None, None
    bids = [p for x in book.get("bids") or [] if isinstance(x, dict)
            if (p := _num(x.get("price"))) is not None]
    asks = [p for x in book.get("asks") or [] if isinstance(x, dict)
            if (p := _num(x.get("price"))) is not None]
    return (max(bids) if bids else None), (min(asks) if asks else None)


def _money(value: float | None) -> str:
    if value is None:
        return ""
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def _holder_text(holders: Any) -> str:
    if isinstance(holders, dict):
        holders = holders.get("holders")
    if not isinstance(holders, list):
        return ""
    bits = []
    for holder in holders[:3]:
        if not isinstance(holder, dict):
            continue
        name = (holder.get("pseudonym") or holder.get("name")
                or holder.get("proxyWallet") or "匿名")
        amount = _num(holder.get("amount"))
        if amount is not None:
            bits.append(f"{str(name)[:20]} {amount:.0f}")
    return "；".join(bits)


def _trade_text(trades: Any) -> str:
    if isinstance(trades, dict):
        trades = trades.get("trades") or trades.get("data")
    if not isinstance(trades, list):
        return ""
    bits = []
    for trade in trades[:2]:
        if not isinstance(trade, dict):
            continue
        side = str(trade.get("side") or "").upper()
        price = _num(trade.get("price"))
        size = _num(trade.get("size"))
        if price is not None and size is not None:
            bits.append(f"{side} {price:.2f} × {size:.0f}")
    return "；".join(bits)


def format_item(event: dict, market: dict, *, book: Any = None,
                price_payload: Any = None, holders: Any = None,
                trades: Any = None, now: datetime | None = None) -> dict | None:
    price = market_price(market)
    if price is None or not (0 <= price <= 1):
        return None
    event_title = str(event.get("title") or "").strip()
    question = str(market.get("question") or "").strip()
    title = f"Polymarket | {event_title}: {question}" if event_title else f"Polymarket | {question}"
    title = title[:200] or "Polymarket 市场信号"

    bits = [f"隐含概率 {price * 100:.1f}%"]
    change = _num(market.get("oneDayPriceChange"))
    if change is not None:
        bits.append(f"24h {change * 100:+.1f}pp")
    volume = _num(market.get("volume24hr"))
    if volume is not None:
        bits.append(f"24h量 {_money(volume)}")
    liquidity = _num(market.get("liquidity"))
    if liquidity is not None:
        bits.append(f"流动性 {_money(liquidity)}")

    clob_price = _num(price_payload.get("price")) if isinstance(price_payload, dict) else None
    bid, ask = best_bid_ask(book)
    if clob_price is not None:
        bits.append(f"CLOB买价 {clob_price:.2f}")
    if bid is not None or ask is not None:
        bits.append("买一/卖一 " + "/".join(
            f"{x:.2f}" if x is not None else "—" for x in (bid, ask)))

    holder_text = _holder_text(holders)
    if holder_text:
        bits.append(f"持仓 {holder_text}")
    trade_text = _trade_text(trades)
    if trade_text:
        bits.append(f"成交 {trade_text}")

    slug = str(event.get("slug") or "").strip()
    if slug:
        url = f"https://polymarket.com/event/{slug}"
    else:
        url = f"https://polymarket.com/markets/{market.get('conditionId') or ''}"
    return {
        "source": "polymarket",
        "ts": now or datetime.now(),
        "title": title,
        "text": "；".join(bits)[:2000],
        "url": url,
        "tier": "authoritative",
    }


async def _safe_json(client: httpx.AsyncClient, url: str,
                     params: dict | None = None) -> Any:
    try:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # noqa: BLE001
        log.debug("[polymarket] %s 失败: %s", url, exc)
        return None


async def _details(client: httpx.AsyncClient, market: dict) -> dict:
    token = market_token(market)
    condition = str(market.get("conditionId") or "").strip()
    book_task = (_safe_json(client, f"{CLOB}/book", {"token_id": token})
                 if token else None)
    price_task = (_safe_json(client, f"{CLOB}/price",
                             {"token_id": token, "side": "buy"})
                  if token else None)
    holders_task = (_safe_json(client, f"{DATA}/holders",
                               {"market": condition, "limit": 3})
                    if condition else None)
    trades_task = (_safe_json(client, f"{DATA}/trades",
                              {"market": condition, "limit": 3})
                   if condition else None)
    values = await asyncio.gather(
        book_task or asyncio.sleep(0, result=None),
        price_task or asyncio.sleep(0, result=None),
        holders_task or asyncio.sleep(0, result=None),
        trades_task or asyncio.sleep(0, result=None))
    return {"book": values[0], "price_payload": values[1],
            "holders": values[2], "trades": values[3]}


async def fetch_polymarket(cfg: dict) -> list[dict]:
    """拉取活跃事件，只在概率变化达到阈值时返回条目。"""
    proxy = cfg.get("proxy") or None
    tags = cfg.get("tags") or ["politics", "crypto"]
    max_events = max(1, int(cfg.get("max_events", 8)))
    min_volume = float(cfg.get("min_volume_24h", 50_000))
    threshold = float(cfg.get("move_threshold", 0.05))
    out: list[dict] = []
    seen: dict[str, float] = {}
    try:
        async with httpx.AsyncClient(
                timeout=20, trust_env=False, proxy=proxy,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"}) as client:
            for tag in tags:
                events = await _safe_json(client, f"{GAMMA}/events", {
                    "active": "true", "closed": "false",
                    "order": "volume24hr", "ascending": "false",
                    "tag_slug": tag, "limit": max_events * 3,
                })
                if not isinstance(events, list):
                    continue
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    volume = _num(event.get("volume24hr"))
                    if volume is not None and volume < min_volume:
                        continue
                    market = pick_market(event)
                    if not market:
                        continue
                    price = market_price(market)
                    if price is None or not (0 <= price <= 1):
                        continue
                    key = str(market.get("conditionId") or event.get("id") or "")
                    if not key or key in seen:
                        continue
                    seen[key] = price
                    previous = _LAST.get(key)
                    if previous is None or abs(price - previous) < threshold:
                        continue
                    details = await _details(client, market)
                    item = format_item(event, market, now=datetime.now(),
                                       **details)
                    if item:
                        out.append(item)
                        if len(out) >= max_events:
                            break
                if len(out) >= max_events:
                    break
    except Exception as exc:  # noqa: BLE001
        log.warning("[polymarket] 拉取失败: %s", exc)
    fresh = [key for key in seen if key not in _LAST]
    if fresh:
        log.info("[polymarket] baseline 初始化 %d 个市场", len(fresh))
    _LAST.update(seen)
    return out
