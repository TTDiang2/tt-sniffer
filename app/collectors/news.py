"""快讯类采集器：财联社电报 / 东财 / 新浪 / 同花顺，统一归一化为原始条目 dict。

返回 dict 字段：source, ts(datetime), title, text, url。
健壮性约定（akshare 列名漂移/脏数据是常态）：
- 每行独立解析，单行坏数据跳过计数，不毒死整轮；
- 所有底层调用包 45s 超时，防止挂起的源永久占用线程池。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import akshare as ak

log = logging.getLogger("collector")
FETCH_TIMEOUT = 45


def _s(v) -> str:
    """akshare 列缺失/NaN 会产生 'nan' 字符串，统一清理。"""
    s = "" if v is None else str(v).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def _dt(*parts) -> datetime:
    """兼容 akshare 列类型漂移：str / datetime.date / datetime.time / datetime 混合。"""
    from datetime import date as date_t, time as time_t
    for p in parts:
        if isinstance(p, datetime):
            return p
    d = next((p for p in parts if isinstance(p, date_t)), None)
    t = next((p for p in parts if isinstance(p, time_t)), None)
    if d is not None and t is not None:
        return datetime.combine(d, t)
    s = " ".join(str(p) for p in parts if p not in (None, ""))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {s!r}")


def _mk(source, ts, title, text, url=""):
    return {"source": source, "ts": ts, "title": (title or "").strip(),
            "text": (text or "").strip(), "url": url or ""}


def _rows(source: str, df, parse) -> list[dict]:
    out, skipped = [], 0
    for _, r in df.iterrows():
        try:
            item = parse(r)
        except Exception:
            skipped += 1
            continue
        if item:
            out.append(item)
    if skipped:
        log.warning("[%s] 跳过 %d 行异常数据（列名漂移或脏数据）", source, skipped)
    return out


async def _call(fn, *args, **kwargs):
    return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs),
                                  timeout=FETCH_TIMEOUT)


def _parse_cls(r) -> dict | None:
    title, text = _s(r.get("标题")), _s(r.get("内容"))
    if not text:
        text = title
    if not (title or text):
        return None
    return _mk("cls", _dt(r["发布日期"], r["发布时间"]), title, text)


def _parse_em(r) -> dict | None:
    title = _s(r["标题"])
    if not title:
        return None
    ts = _dt(r["发布时间"])
    return _mk("em", ts, title, _s(r["摘要"]), _s(r.get("链接")))


def _parse_sina(r) -> dict | None:
    text = _s(r["内容"])
    if not text:
        return None
    return _mk("sina", _dt(r["时间"]), "", text)


def _parse_ths(r) -> dict | None:
    title, text = _s(r["标题"]), _s(r["内容"])
    if not (title or text):
        return None
    return _mk("ths", _dt(r["发布时间"]), title, text or title, _s(r.get("链接")))


async def fetch_cls() -> list[dict]:
    """财联社电报（symbol="重点" 可只取红色头条，这里取全部保证覆盖）。"""
    df = await _call(ak.stock_info_global_cls, symbol="全部")
    return _rows("cls", df, _parse_cls)


async def fetch_em() -> list[dict]:
    df = await _call(ak.stock_info_global_em)
    return _rows("em", df, _parse_em)


async def fetch_sina() -> list[dict]:
    df = await _call(ak.stock_info_global_sina)
    return _rows("sina", df, _parse_sina)


async def fetch_ths() -> list[dict]:
    df = await _call(ak.stock_info_global_ths)
    return _rows("ths", df, _parse_ths)


def parse_wscn_items(items: list) -> list[dict]:
    """华尔街见闻 items -> 归一化条目（纯函数，便于测试）。"""
    out = []
    for it in items:
        try:
            if it.get("type") not in (None, "live"):
                continue
            body = _s(it.get("content_text") or it.get("content"))
            title = _s(it.get("title")) or body[:60]
            if not it.get("display_time") or not (title or body):
                continue
            ts = datetime.fromtimestamp(int(it["display_time"]))
            uri = _s(it.get("uri"))
            url = f"https://wallstreetcn.com/{uri}" if uri and not uri.startswith("http") else uri
            out.append(_mk("wscn", ts, title, body or title, url))
        except Exception:
            continue
    return out


async def fetch_wscn() -> list[dict]:
    """华尔街见闻 7x24（直连官方 API，不依赖 akshare）。"""
    import httpx
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("https://api-one.wallstcn.com/apiv1/content/lives",
                        params={"channel": "global-channel", "client": "pc",
                                "limit": "50"},
                        headers={"Referer": "https://wallstreetcn.com/live/global"})
        r.raise_for_status()
        items = ((r.json() or {}).get("data") or {}).get("items") or []
    return parse_wscn_items(items)


NEWS_SOURCES = {"cls": fetch_cls, "em": fetch_em, "sina": fetch_sina,
                "ths": fetch_ths, "wscn": fetch_wscn}
SOURCE_LABELS = {"cls": "财联社", "em": "东财", "sina": "新浪", "ths": "同花顺",
                 "wscn": "华尔街见闻", "anomaly": "盘口异动", "ztpool": "涨停池",
                 "manual": "手动"}
