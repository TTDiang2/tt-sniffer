"""外网新闻 RSS/Atom 采集器（20261021：彭博/WSJ/路透/AP/FT/CNBC 等）。

只读取公开 RSS 的标题、摘要、作者/时间和原文链接，不绕过付费墙，不抓正文。
每个 feed 在配置里独立指定 `source`、`tier`、`max_age_days`，失败只影响单 feed。

时间口径：parse_rss(..., to_local=True)，把 RSS 带时区的 pubDate 转成本机 naive，
与 news/street 采集器的 ts 保持一致。
"""
from __future__ import annotations

import logging
from collections.abc import Callable

import httpx

from .crypto_rss import parse_rss

log = logging.getLogger("collector")
StatusFn = Callable[[str, bool, str], None]


async def fetch_rss(cfg: dict, on_status: StatusFn | None = None,
                    default_tier: str = "authoritative") -> list[dict]:
    """逐 feed 容错拉取，返回已补齐 source/tier 的原始条目。

    `on_status(source, ok, note)` 由 pipeline 注入，用于让每个 feed
    在顶部可用性/左侧筛选中独立可见，而不是只显示一个聚合的 rss。
    `default_tier`：feed 漏写 tier 时的缺省——公众号段（wechat_rss）传
    'street'（小作文语义：漏写不能静默落 authoritative 进打扰通道）。
    """
    out: list[dict] = []
    proxy = cfg.get("proxy") or None
    max_age_days = int(cfg.get("max_age_days", 2))
    for feed in cfg.get("feeds") or []:
        if not feed.get("enabled", True):
            continue
        name = str(feed.get("source") or feed.get("name") or "feed")[:32]
        try:
            async with httpx.AsyncClient(
                    timeout=20, trust_env=False, proxy=proxy,
                    follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0"}) as client:
                response = await client.get(feed["url"])
                response.raise_for_status()
            raws = parse_rss(
                response.text, name,
                max_age_days=int(feed.get("max_age_days", max_age_days)),
                to_local=True)
            for raw in raws:
                raw["tier"] = feed.get("tier", default_tier)
            out.extend(raws)
            log.info("[rss:%s] %d 条", name, len(raws))
            if on_status:
                on_status(name, True, f"{len(raws)}条")
        except Exception as exc:  # noqa: BLE001
            log.warning("[rss:%s] 拉取失败: %s", name, exc)
            if on_status:
                on_status(name, False, str(exc)[:80])
    return out
