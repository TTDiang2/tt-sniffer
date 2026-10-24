"""加密垂直媒体 RSS 采集器（20261004：孙宇晨枪击传言覆盖缺口驱动）。

背景：五路权威源+股吧/TG 均不覆盖加密生态；加密圈传言（孙宇晨类）首发于 X
（无免费通道，挂账）与加密媒体/TG 频道。本采集器抓加密媒体 RSS。

探针实测（20261004 上午）：
- WuBlockchain substack RSS（wublock.substack.com/feed）：可用，**需走本机代理**
  （直连被掐）；这正是 config 里 proxy 配置位的由来。
- BlockBeats rss.theblockbeats.info：此刻代理/直连双向 SSL EOF（挂账待复测）。
- TG 频道预览页 t.me/s/<ch>：间歇可达（昨夜通、今晨空包），channels 配置位留给
  用户填（street.py 的 tg 采集器即可吃，无需本模块）。

tier：per-feed 配置。默认 'street'（加密媒体传言密度高，⚠️ 未证实+不打扰通道
保护先开着）；确信源可改 'authoritative'。
"""
from __future__ import annotations

import html as html_lib
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

log = logging.getLogger("collector")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
ATOM = "{http://www.w3.org/2005/Atom}"


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", html_lib.unescape(_TAG_RE.sub(" ", text or ""))).strip()


def parse_rss(body: str, source: str, *, max_age_days: int = 3,
              now: datetime | None = None, to_local: bool = True) -> list[dict]:
    """RSS/Atom → raw dicts（title/text/url/ts/tier 由调用方补）。
    纯函数：测试用内联 fixture，不触网。max_age_days 过滤旧文（首轮回灌只留近几日）。

    时间口径（A2 时区修复）：带时区的 pubDate 必须先用 astimezone() 转到本机时区，
    再剥 tzinfo；否则 GMT 钟面会被当成北京时间，整体早 8 小时。默认 to_local=True，
    与 items.ts 的本地 naive 口径一致；只有显式需要 UTC naive 的调用方才传 False。
    """
    root = ET.fromstring(body)
    items = root.findall(".//item") or root.findall(f".//{ATOM}entry")
    now = now or datetime.now()
    out: list[dict] = []
    for it in items:
        title = _clean(it.findtext("title") or it.findtext(f"{ATOM}title") or "")
        link = (it.findtext("link") or "").strip() or (
            it.findtext(f"{ATOM}link") or "").strip()
        # link 元素式（substack 用 <link href=...>）
        if not link:
            le = it.find(f"{ATOM}link")
            if le is not None:
                link = le.get("href", "")
        desc = _clean(it.findtext("description")
                      or it.findtext(f"{ATOM}summary")
                      or it.findtext(f"{ATOM}content") or "")
        pub = (it.findtext("pubDate") or it.findtext(f"{ATOM}updated")
               or it.findtext(f"{ATOM}published") or "")
        try:
            ts = parsedate_to_datetime(pub) if pub else now
            if ts.tzinfo is not None:
                ts = (ts.astimezone() if to_local else ts).replace(tzinfo=None)
        except (TypeError, ValueError):
            ts = now
        if ts > now + timedelta(minutes=5):
            ts = now
        if not title and not desc:
            continue
        if (now - ts) > timedelta(days=max_age_days):
            continue
        out.append({"source": source, "ts": ts,
                    "title": title[:200], "text": desc[:2000] or title,
                    "url": link})
    return out


async def fetch_crypto_rss(cfg: dict) -> list[dict]:
    """逐 feed 容错拉取（单 feed 失败不影响其余）；proxy 可选（境外源需要）。"""
    import asyncio
    out: list[dict] = []
    proxy = cfg.get("proxy") or None
    feeds = cfg.get("feeds") or []
    for f in feeds:
        if not f.get("enabled", True):
            continue
        name = str(f.get("name", "feed"))[:16]
        source = f"rss_{name}"
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False,
                                         proxy=proxy,
                                         headers={"User-Agent": "Mozilla/5.0"}) as c:
                r = await c.get(f["url"])
                r.raise_for_status()
            raws = parse_rss(r.text, source,
                             max_age_days=cfg.get("max_age_days", 3),
                             to_local=True)
            for x in raws:
                x["tier"] = f.get("tier", "street")
            out.extend(raws)
        except Exception as e:  # noqa: BLE001
            log.warning("[crypto_rss:%s] 拉取失败: %s", name, e)
    return out
