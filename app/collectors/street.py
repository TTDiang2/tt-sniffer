"""小道消息源采集器（S4 传闻源）：东财股吧个股热帖 + Telegram 公开频道预览。

两族源统一归一化为原始条目 dict（source/ts/title/text/url），入流水线时打
tier='street'（来源可信度正交维度，见 store._MIGRATIONS 与 scoring 降档）。

纪律（与 news.py 同款约定）：
- parse_* 是纯函数，离线夹具（data/guba_sample.html / 内联 TG HTML）可测，
  fetch_* 只做网络、超时与逐源容错——单源失败不毒死整轮；
- 股吧列表页 update 只有 MM-DD HH:MM 没有年份，且实际是 12 小时制（凌晨
  00:58 显示 12:58，下午 21:34 显示 09:34）；不能直接当 24 小时制解析。
  页面内嵌的 article_list JSON 带 post_publish_time 精确时间，优先按
  data-postid 映射；仅当 JSON 缺该 id 时才退回 update。跨年规则：MM-DD 比
  今天的月-日还大（今年不可能出现的未来日期）→ 归为去年。
- guba 列表页拿不到正文（正文在帖子详情页），text=标题；TG 消息无标题，
  沿 sina 惯例 title=''、text=正文。
- TG channels 默认空 = 不发任何请求（不采集），用户配置后生效。

股吧详情补抓（20261023 小作文扩充第一站）：
- 列表行只是标题；命中候选（标题含传闻词 或 阅读数达标）的帖子补抓详情页，
  拿全文（正文在内嵌 `var post_article={...}` JSON 的 post_content 字段）、
  精确发布时间与阅读数。非候选保持 text=标题（既有行为）。
- 防反爬三件套：post_id 进程内缓存（含失败标记，绝不反复撞同一帖）；
  每轮补抓上限 max_per_round；连续 3 次失败进入冷却期（cooldown_s）。
  探针实测单次详情请求 2-4 秒，礼貌节奏 1 请求/秒以内。
"""
from __future__ import annotations

import asyncio
import html as htmllib
import json
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime

log = logging.getLogger("collector")
FETCH_TIMEOUT = 45          # 整轮采集的兜底超时（与 news.py 一致）
HTTP_TIMEOUT = 20           # 单页请求超时（防反爬挂起）
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0 Safari/537.36"}

# ---- 股吧列表页行结构（spike 实测 20261003，夹具 data/guba_sample.html）----
_ROW_RE = re.compile(r'<tr class="listitem"[^>]*>(.*?)</tr>', re.S)
_READ_RE = re.compile(r'<div class="read">(\d+)</div>')
_TITLE_RE = re.compile(r'<div class="title"><a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.S)
_UPDATE_RE = re.compile(r'<div class="update">([^<]*)</div>')
_TAG_RE = re.compile(r"<[^>]+>")

# ---- TG 公开预览页：按 message_wrap 分块（块内 time 属性 + 可选正文块）----
_TG_MSG_SPLIT = re.compile(r'(?=<div class="tgme_widget_message_wrap)')
_TG_TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TG_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')


def _mk(source: str, ts: datetime, title: str, text: str, url: str = "") -> dict:
    return {"source": source, "ts": ts, "title": (title or "").strip(),
            "text": (text or "").strip(), "url": url or ""}


def _clean_text(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = _TAG_RE.sub("", s)
    return htmllib.unescape(s).strip()


def _abs_url(href: str, code: str) -> str:
    """股吧 href 补 https: 前缀：'//host/x' 协议相对；'/news,600519,...' 站内相对。"""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return f"https://guba.eastmoney.com{href}"
    if href.startswith("http"):
        return href
    return f"https://guba.eastmoney.com/list,{code}.html"


def _update_ts(update: str, now: datetime) -> datetime | None:
    """'MM-DD HH:MM' -> 本地 naive datetime。

    跨年规则：MM-DD 大于今天的月-日（今年不可能出现的未来日期）→ 归为去年。
    """
    m = re.match(r"(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", (update or "").strip())
    if not m:
        return None
    mm, dd, hh, mi = (int(x) for x in m.groups())
    year = now.year - 1 if (mm, dd) > (now.month, now.day) else now.year
    try:
        return datetime(year, mm, dd, hh, mi)
    except ValueError:
        return None


def _guba_publish_times(html: str) -> dict[str, datetime]:
    """article_list JSON 的 post_id -> 精确发布时间；缺失/损坏时返回空映射。"""
    marker = "var article_list="
    start = html.find(marker)
    if start < 0:
        return {}
    try:
        payload, _ = json.JSONDecoder().raw_decode(html[start + len(marker):])
    except (TypeError, ValueError):
        return {}
    posts = payload.get("re", []) if isinstance(payload, dict) else []
    out: dict[str, datetime] = {}
    for post in posts if isinstance(posts, list) else []:
        if not isinstance(post, dict):
            continue
        pid = post.get("post_id")
        raw = post.get("post_publish_time")
        if pid is None or not isinstance(raw, str):
            continue
        try:
            out[str(pid)] = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return out


def parse_guba_html(html: str, code: str, *, min_reads: int = 0,
                    now: datetime | None = None) -> list[dict]:
    """股吧列表页 HTML -> 归一化条目（纯函数）。

    行级容错：缺标题/时间的行跳过；阅读数缺失按 0 计，< min_reads 过滤。
    now 参数供测试锚定（年份推断依赖当天日期）。
    附带 reads/post_id 元数据键（pipeline 不消费，供详情补抓选候选）。
    """
    now = now or datetime.now()
    publish_times = _guba_publish_times(html)
    out: list[dict] = []
    for row in _ROW_RE.findall(html):
        try:
            title_m = _TITLE_RE.search(row)
            update = _UPDATE_RE.search(row)
            if not title_m or not update:
                continue
            reads_m = _READ_RE.search(row)
            reads = int(reads_m.group(1)) if reads_m else 0
            if reads < min_reads:
                continue
            pid_m = re.search(r'data-postid="(\d+)"', title_m.group(0))
            pid = pid_m.group(1) if pid_m else ""
            ts = publish_times.get(pid) if pid else None
            if ts is None:
                ts = _update_ts(update.group(1), now)
            if ts is None:
                continue
            title = _clean_text(title_m.group(2))
            if not title:
                continue
            item = _mk("guba", ts, title, title,
                       _abs_url(title_m.group(1), code))
            item["reads"] = reads
            item["post_id"] = pid
            item["code"] = code      # 详情页 URL 需要（caifuhao 行的 url 里没有）
            out.append(item)
        except Exception:
            continue
    return out


# ---- 股吧详情补抓（探针实测 20261023：正文在内嵌 post_article JSON）----
_DETAIL_PARA_RE = re.compile(r"(?i)</p\s*>")
_GUBA_DETAIL_WORDS = ("小作文", "传闻", "内幕", "纪要", "调研", "问询", "立案",
                      "减持", "收购", "重组", "提价", "涨价", "停产", "断供",
                      "处罚", "退市", "合并", "注入", "爆料", "劲爆")
_DETAIL_CACHE_MAX = 512
# 进程级状态：post_id -> 详情 dict（None=抓过但失败，绝不重试）；
# 连续失败计数与冷却截止（time.monotonic 口径）
_detail_cache: OrderedDict[str, dict | None] = OrderedDict()
_detail_fail_streak = 0
_detail_pause_until = 0.0


def parse_guba_detail(html: str) -> dict | None:
    """股吧详情页 HTML -> {title, text, ts, post_id, clicks, comments}（纯函数）。

    正文在 `var post_article={...}`（严格 JSON）的 post_content 字段——完整
    HTML 字符串，</p> 转换行后剥标签；截断 4000 字（小作文全文够用，防超长
    灌水帖撑爆入库与 simhash）。解析失败返回 None（调用方保留标题兜底）。
    """
    marker = "var post_article="
    i = html.find(marker)
    if i < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(html[i + len(marker):])
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    title = str(payload.get("post_title") or "").strip()
    text = _clean_text(_DETAIL_PARA_RE.sub("\n", str(payload.get("post_content") or "")))[:4000]
    if not title and not text:
        return None
    ts = None
    try:
        ts = datetime.strptime(str(payload.get("post_publish_time") or "").strip(),
                               "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass

    def _count(key: str) -> int:
        try:
            return int(payload.get(key) or 0)
        except (TypeError, ValueError):       # "3,053" 这类脏计数按 0 计
            return 0

    return {"title": title, "text": text, "ts": ts,
            "post_id": str(payload.get("post_id") or ""),
            "clicks": _count("post_click_count"),
            "comments": _count("post_comment_count")}


def _detail_candidate(row: dict, words: tuple[str, ...], min_reads: int) -> bool:
    if row.get("post_id") and row.get("reads", 0) >= min_reads:
        return True
    title = row.get("title") or ""
    return bool(row.get("post_id")) and any(w in title for w in words)


async def _guba_enrich_details(client, rows: list[dict], d: dict | None) -> None:
    """原地补全候选行的正文与精确发布时间（失败保留标题兜底，不抛错）。

    候选=标题含传闻词 或 阅读数≥min_reads；每轮补抓上限 max_per_round；
    连续 3 次失败进入 cooldown_s 冷却（期间纯列表模式）。缓存已含该 post_id
    （成功或失败）的行不再发请求——热帖会连续挂榜多轮，靠缓存避免反复撞。
    """
    global _detail_fail_streak, _detail_pause_until
    d = d if isinstance(d, dict) else {}
    if not d.get("enabled", True) or not rows:
        return
    words = tuple(str(w) for w in (d.get("rumor_words") or _GUBA_DETAIL_WORDS) if w)
    min_reads = int(d.get("min_reads") or 1000)
    cap = max(0, int(d.get("max_per_round") or 4))
    cooldown = int(d.get("cooldown_s") or 1800)

    # 1) 缓存应用：已抓成功的直接补全（零请求）；命中即续命（真 LRU）
    for row in rows:
        pid = row.get("post_id") or ""
        hit = _detail_cache.get(pid) if pid else None
        if hit:
            _detail_cache.move_to_end(pid)
            row["text"] = hit["text"] or row["text"]
            if hit.get("ts"):
                row["ts"] = hit["ts"]

    # 2) 补抓未抓过的候选（预算耗尽或冷却生效即停）
    if time.monotonic() < _detail_pause_until:
        return
    budget = cap
    for row in rows:
        if budget <= 0 or time.monotonic() < _detail_pause_until:
            break
        pid = row.get("post_id") or ""
        hit = _detail_cache.get(pid)
        if not pid or (pid in _detail_cache and hit is not False):
            continue      # 已成功（dict）或二次失败（None）跳过；False=首败可重试一次
        if not _detail_candidate(row, words, min_reads):
            continue
        budget -= 1
        code = str(row.get("code") or "0")
        try:
            r = await client.get(f"https://guba.eastmoney.com/news,{code},{pid}.html")
            r.raise_for_status()
            info = parse_guba_detail(r.text)
        except Exception as e:
            info = None
            log.info("[guba:detail] %s 补抓失败: %s", pid, e)
        if info is None:
            # 失败分级：首败记 False（下轮可再试一次——网络抖动自愈），二次失败
            # 才记 None 永久放弃（被拦 403 类至多多挨一次，防反爬纪律不破）
            _detail_cache[pid] = None if _detail_cache.get(pid) is False else False
            _detail_fail_streak += 1
            if _detail_fail_streak >= 3:
                _detail_pause_until = time.monotonic() + cooldown
                _detail_fail_streak = 0
                log.warning("[guba:detail] 连续失败，详情通道冷却 %ds", cooldown)
            await asyncio.sleep(1.0)   # 失败（多为被拦）更要慢，不睡等于加速撞墙
            continue
        _detail_cache[pid] = info
        _detail_fail_streak = 0
        row["text"] = info["text"] or row["text"]
        if info["ts"]:
            row["ts"] = info["ts"]
        await asyncio.sleep(1.0)   # 礼貌间隔（探针实测单请求 2-4s，1s 间隔已足够克制）

    while len(_detail_cache) > _DETAIL_CACHE_MAX:
        _detail_cache.popitem(last=False)


async def fetch_guba(codes: list[str], min_reads: int = 100,
                     detail_cfg: dict | None = None) -> list[dict]:
    """逐代码抓股吧 list 页热帖（阅读≥min_reads）+ 候选帖详情补抓；单代码失败仅记日志。"""
    import httpx
    out: list[dict] = []
    codes = [str(c).strip() for c in codes or [] if str(c).strip()]
    if not codes:
        return out

    async def _one(c: httpx.AsyncClient, code: str) -> list[dict]:
        r = await c.get(f"https://guba.eastmoney.com/list,{code}.html")
        r.raise_for_status()
        rows = parse_guba_html(r.text, code, min_reads=min_reads)
        log.info("[guba] %s 收 %d 条（阅读≥%d）", code, len(rows), min_reads)
        return rows

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=_HEADERS,
                                     follow_redirects=True) as c:
            for code in codes:
                try:
                    rows = await asyncio.wait_for(_one(c, code), FETCH_TIMEOUT)
                    out.extend(rows)
                except Exception as e:
                    log.warning("[guba] %s 拉取失败: %s", code, e)
            # 详情补抓统一在各代码收齐后做：候选池跨代码共享 max_per_round 预算
            await _guba_enrich_details(c, out, detail_cfg)
    except Exception as e:      # 客户端构造/连接层异常也不毒死整轮
        log.warning("[guba] 采集失败: %s", e)
    return out


def parse_tg_html(html: str, channel: str, *, now: datetime | None = None) -> list[dict]:
    """t.me/s/<channel> 预览页 HTML -> 归一化条目（纯函数）。

    按 message_wrap 分块逐块解析：有 time 属性才收（半成品块跳过）；
    无 tgme_widget_message_text 的块（纯图/纯媒体消息）跳过。
    datetime 属性带时区，统一折算为本地 naive（与其余来源 ts 口径一致）。
    now 参数预留（与 parse_guba_html 对齐，当前解析不依赖当天日期）。
    """
    out: list[dict] = []
    for block in _TG_MSG_SPLIT.split(html)[1:]:
        try:
            t = _TG_TIME_RE.search(block)
            if not t:
                continue
            text_m = _TG_TEXT_RE.search(block)
            if not text_m:
                continue
            text = _clean_text(text_m.group(1))
            if not text:
                continue
            ts = datetime.fromisoformat(t.group(1))
            if ts.tzinfo is not None:
                ts = ts.astimezone().replace(tzinfo=None)
            out.append(_mk("tg", ts, "", text,
                           f"https://t.me/{channel}"))
        except Exception:
            continue
    return out


async def fetch_tg(channels: list[str]) -> list[dict]:
    """逐频道抓 t.me/s/<channel> 公开预览页；channels 空=不发请求不采集。"""
    import httpx
    chans = [str(c).strip().lstrip("@") for c in channels or [] if str(c).strip()]
    if not chans:
        return []
    out: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=_HEADERS,
                                     follow_redirects=True) as c:
            for ch in chans:
                try:
                    r = await c.get(f"https://t.me/s/{ch}")
                    r.raise_for_status()
                    rows = parse_tg_html(r.text, ch)
                    out.extend(rows)
                    log.info("[tg] %s 收 %d 条", ch, len(rows))
                except Exception as e:
                    log.warning("[tg] %s 拉取失败: %s", ch, e)
    except Exception as e:
        log.warning("[tg] 采集失败: %s", e)
    return out


async def _fetch_guba_cfg(cfg: dict) -> list[dict]:
    return await fetch_guba(cfg.get("codes", []),
                            int(cfg.get("min_reads") or 100),
                            detail_cfg=cfg.get("detail"))


async def _fetch_tg_cfg(cfg: dict) -> list[dict]:
    return await fetch_tg(cfg.get("channels", []))


# pipeline 的 street_loop 按名字取 fetcher（传 config 小节，news_loop 同款形态）
STREET_SOURCES = {"guba": _fetch_guba_cfg, "tg": _fetch_tg_cfg}
