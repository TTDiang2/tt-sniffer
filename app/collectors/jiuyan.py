"""韭研公社采集器（S4 传闻源，20261023 小作文扩充第二站）。

jiuyangongshe.com：A股研究社区，「纪要转载/个股研究」类文章是小作文的
二级传播重镇（调研纪要、专家会、产业链爆料多首发于此）。robots.txt 为
`User-agent: * Disallow:`（空=允许），公开 SSR 页面无需登录。

页面形态（探针实测 20261023）：
- 列表 /square_publish（最新发布）：Nuxt SSR，文章列表在 __NUXT__ 载荷的
  searchData.list 数组——article_id/title/create_time/content（约 250 字
  预览）/stock_list（关联标的）。载荷是函数调用混淆格式（值可能是函数参数
  引用，如 content:d），只能解析字面量，标识符引用一律按缺失处理。
- 详情 /a/<article_id>：标题/发布时间/关联标的是 SSR HTML；全文在 __NUXT__
  载荷 data[0].data.content——完整 HTML 文档字符串（\\u003C 转义）。

纪律（与 street.py 同款）：
- parse_* 纯函数，离线夹具（tests/fixtures/jiuyan_*.html）可测；fetch 只做
  网络与容错，单源失败不毒死整轮；
- 列表 content 是 250 字截断预览：命中候选（标题含传闻词 或 预览过短）的
  文章补抓详情页拿全文；非候选用预览兜底（缺失时 text=标题）；
- 防反爬三件套与 guba 同款：article_id 缓存（失败也标记）、每轮补抓上限、
  连续 3 次失败冷却。
"""
from __future__ import annotations

import asyncio
import html as htmllib
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta

log = logging.getLogger("collector")
HTTP_TIMEOUT = 20
FETCH_TIMEOUT = 45
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0 Safari/537.36"}
SQUARE_URL = "https://www.jiuyangongshe.com/square_publish"
ARTICLE_URL = "https://www.jiuyangongshe.com/a/{aid}"

_JIUYAN_DETAIL_WORDS = ("小作文", "传闻", "纪要", "电话会", "专家", "调研",
                        "问询", "减持", "收购", "重组", "内幕", "爆料", "交流")
_DETAIL_CACHE_MAX = 512
_detail_cache: OrderedDict[str, dict | None] = OrderedDict()
_detail_fail_streak = 0
_detail_pause_until = 0.0

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_PARA_RE = re.compile(r"(?i)</p\s*>|</div\s*>|<br\s*/?>")


# ---------- __NUXT__ 载荷解析（JS 值字面量的最小递归下降） ----------

def _js_string(src: str, i: int) -> tuple[str, int]:
    """从 s[i]=='\"' 起解析 JS 字符串字面量（\\uXXXX/\\n/\\\"/\\\\ 转义）。

    代理对按 JS 语义合并（\\uD83D\\uDE80 → 🚀）；孤立代理项替换 U+FFFD——
    否则 norm_id 的 key.encode('utf-8') 会抛 UnicodeEncodeError 杀掉整轮
    street_loop（小作文恰是 emoji 高发区，必须防御）。
    """
    out: list[str] = []
    i += 1
    n = len(src)
    while i < n:
        c = src[i]
        if c == '"':
            return "".join(out), i + 1
        if c == "\\":
            if i + 1 >= n:
                break
            e = src[i + 1]
            if e == "u" and i + 6 <= n:
                try:
                    cp = int(src[i + 2:i + 6], 16)
                except ValueError:
                    raise ValueError("bad \\u escape") from None
                i += 6
                if 0xD800 <= cp <= 0xDBFF and src[i:i + 2] == "\\u":
                    try:
                        lo = int(src[i + 2:i + 6], 16)
                    except ValueError:
                        lo = -1
                    if 0xDC00 <= lo <= 0xDFFF:
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)
                        i += 6
                if 0xD800 <= cp <= 0xDFFF:      # 孤立代理项：sanitize 防编码崩
                    cp = 0xFFFD
                out.append(chr(cp))
            elif e in "ntbrfv0":
                out.append({"n": "\n", "t": "\t", "b": "\b", "r": "\r",
                            "f": "\f", "v": "\v", "0": "\0"}[e])
                i += 2
            else:                      # \\ \" \' \/ 等一律还原为字符本体
                out.append(e)
                i += 2
        else:
            out.append(c)
            i += 1
    raise ValueError("unterminated JS string")


def _js_value(src: str, i: int) -> tuple[object, int]:
    """解析 JS 值：字符串/数字/true|false|null|undefined/对象/数组。

    载荷混淆的单字母标识符（函数参数引用）无法静态解出 → 返回 None；
    对象构建时值为 None 的键直接丢弃（null 与不可解在此场景等价=缺失）。
    """
    n = len(src)
    while i < n and src[i] in " \t\r\n":
        i += 1
    if i >= n:
        raise ValueError("eof")
    c = src[i]
    if c == '"':
        return _js_string(src, i)
    if c == "{":
        obj: dict = {}
        i += 1
        while i < n:
            while i < n and src[i] in " \t\r\n,":
                i += 1
            if i >= n:
                raise ValueError("unterminated object")   # 截断页/半包 EOF 防御
            if src[i] == "}":
                return obj, i + 1
            if src[i] == '"':
                key, i = _js_string(src, i)
            else:
                m = re.match(r"[A-Za-z_$][\w$]*", src[i:])
                if not m:
                    raise ValueError(f"bad key at {i}")
                key = m.group(0)
                i += len(key)
            while i < n and src[i] in " \t\r\n":
                i += 1
            if i >= n or src[i] != ":":
                raise ValueError(f"expect ':' at {i}")
            val, i = _js_value(src, i + 1)
            if val is not None:
                obj[key] = val
        raise ValueError("unterminated object")
    if c == "[":
        arr: list = []
        i += 1
        while i < n:
            while i < n and src[i] in " \t\r\n,":
                i += 1
            if i < n and src[i] == "]":
                return arr, i + 1
            val, i = _js_value(src, i)
            if val is not None:
                arr.append(val)
        raise ValueError("unterminated array")
    m = re.match(r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", src[i:])
    if m:
        s = m.group(0)
        i += len(s)
        return (float(s) if any(x in s for x in ".eE") else int(s)), i
    m = re.match(r"[A-Za-z_$][\w$]*", src[i:])
    if m:
        i += len(m.group(0))
        return {"true": True, "false": False}.get(m.group(0)), i  # 其余标识符=None
    raise ValueError(f"bad value at {i}")


def _html_to_text(html: str) -> str:
    """正文 HTML → 纯文本：块级结束标签/br 换行、剥标签、反转义、压空白。"""
    s = _PARA_RE.sub("\n", html or "")
    s = _TAG_RE.sub("", s)
    s = htmllib.unescape(s)
    lines = [_WS_RE.sub(" ", ln).strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


def parse_jiuyan_square(html: str, *, max_age_days: int = 2,
                        now: datetime | None = None) -> list[dict]:
    """广场「最新发布」页 → 归一化条目（纯函数）。

    锚点：__NUXT__ 载荷 searchData 之后的 list:[（广场页还有别的 list 字段，
    必须锚定 searchData 防串位）。置顶旧帖（create_time 两年前）与超
    max_age_days 的条目过滤——首轮回灌截断，防旧文刷屏。
    """
    now = now or datetime.now()
    base = html.find("__NUXT__")
    if base < 0:
        return []
    sd = html.find("searchData:{", base)
    start = html.find("list:[", sd if sd > -1 else base)
    if start < 0:
        return []
    try:
        items, _ = _js_value(html, start + len("list:"))
    except (ValueError, RecursionError) as e:
        log.warning("[jiuyan] 列表载荷解析失败: %s", e)
        return []
    out: list[dict] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        aid = it.get("article_id")
        title = str(it.get("title") or "").strip()
        if not aid or not title:
            continue
        try:
            ts = datetime.strptime(str(it.get("create_time") or "").strip(),
                                   "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts > now + timedelta(minutes=5) or \
                (now - ts) > timedelta(days=max_age_days):
            continue
        preview = str(it.get("content") or "").strip()
        out.append({"source": "jiuyan", "ts": ts, "title": title[:200],
                    "text": (preview or title)[:2000],
                    "url": ARTICLE_URL.format(aid=aid),
                    "article_id": aid, "preview_len": len(preview)})
    return out


def parse_jiuyan_article(html: str) -> dict | None:
    """文章页 → {title, text, ts}（纯函数；解析失败返回 None）。

    全文：__NUXT__ 载荷的 content:"…"——完整 HTML 文档（<html><head>…），
    非 HTML 的 content 命中（评论区等）跳过后继续找，最多试 5 处。
    标题/时间：SSR 的 fs28-bold 标题与 date 时间行，缺失时逐步降级。
    """
    title = ""
    m = re.search(r'class="fs28-bold[^"]*"[^>]*>([^<]+)</div>', html)
    if m:
        title = htmllib.unescape(m.group(1)).strip()
    if not title:
        m = re.search(r"<title>([^<]*)</title>", html)
        if m:
            title = re.sub(r"-韭研公社$", "",
                           htmllib.unescape(m.group(1)).strip())
    ts = None
    m = re.search(r'class="date[^"]*"[^>]*>\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})',
                  html)
    if m:
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    text = ""
    pos = html.find("__NUXT__")
    pos = html.find('content:"', pos if pos > -1 else 0)
    for _ in range(5):
        if pos < 0:
            break
        try:
            raw, _next = _js_string(html, pos + len('content:"') - 1)
        except ValueError:
            raw = ""
        if "<p" in raw or "<html" in raw:      # 只有完整正文才是 HTML 文档形态
            text = _html_to_text(raw)[:4000]
            if text:
                break
        pos = html.find('content:"', pos + 9)
    if not title and not text:
        return None
    return {"title": title, "text": text, "ts": ts}


# ---------- 采集 ----------

def _detail_candidate(row: dict, words: tuple[str, ...], min_len: int) -> bool:
    if row.get("preview_len", 0) < min_len:
        return True
    title = row.get("title") or ""
    return any(w in title for w in words)


async def _jiuyan_enrich_details(client, rows: list[dict], d: dict | None) -> int:
    """原地补全候选行全文（与 guba 详情补抓同款纪律：缓存/上限/冷却）。

    返回本轮发出的详情请求数（成功与否都算——短文的全文可能与预览等长，
    按"结果更长"计数会漏报）。"""
    global _detail_fail_streak, _detail_pause_until
    d = d if isinstance(d, dict) else {}
    if not d.get("enabled", True) or not rows:
        return 0
    words = tuple(str(w) for w in (d.get("rumor_words") or _JIUYAN_DETAIL_WORDS) if w)
    min_len = int(d.get("min_len") or 60)
    cap = max(0, int(d.get("max_per_round") or 3))
    cooldown = int(d.get("cooldown_s") or 1800)

    for row in rows:
        aid = row.get("article_id") or ""
        hit = _detail_cache.get(aid) if aid else None
        if hit:
            _detail_cache.move_to_end(aid)
            row["text"] = hit["text"] or row["text"]
            if hit.get("ts"):
                row["ts"] = hit["ts"]

    if time.monotonic() < _detail_pause_until:
        return 0
    budget = cap
    fetched = 0
    for row in rows:
        if budget <= 0 or time.monotonic() < _detail_pause_until:
            break
        aid = row.get("article_id") or ""
        hit = _detail_cache.get(aid)
        if not aid or (aid in _detail_cache and hit is not False):
            continue      # 已成功（dict）或二次失败（None）跳过；False=首败可重试一次
        if not _detail_candidate(row, words, min_len):
            continue
        budget -= 1
        fetched += 1
        try:
            r = await client.get(ARTICLE_URL.format(aid=aid))
            r.raise_for_status()
            info = parse_jiuyan_article(r.text)
        except Exception as e:
            info = None
            log.info("[jiuyan:detail] %s 补抓失败: %s", aid, e)
        if info is None:
            # 失败分级：首败 False 下轮可再试一次（网络抖动自愈），二次失败
            # 才 None 永久放弃
            _detail_cache[aid] = None if _detail_cache.get(aid) is False else False
            _detail_fail_streak += 1
            if _detail_fail_streak >= 3:
                _detail_pause_until = time.monotonic() + cooldown
                _detail_fail_streak = 0
                log.warning("[jiuyan:detail] 连续失败，详情通道冷却 %ds", cooldown)
            await asyncio.sleep(1.0)   # 失败（多为被拦）更要慢
            continue
        info["title"] = info["title"] or row["title"]
        _detail_cache[aid] = info
        _detail_fail_streak = 0
        row["text"] = info["text"] or row["text"]
        if info["ts"]:
            row["ts"] = info["ts"]
        await asyncio.sleep(1.0)

    while len(_detail_cache) > _DETAIL_CACHE_MAX:
        _detail_cache.popitem(last=False)
    return fetched


async def fetch_jiuyan(cfg: dict) -> list[dict]:
    """广场最新发布一页 + 候选详情补抓；任何失败只记日志返回已收部分。"""
    import httpx
    out: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=_HEADERS,
                                     follow_redirects=True) as c:
            r = await asyncio.wait_for(c.get(SQUARE_URL), FETCH_TIMEOUT)
            r.raise_for_status()
            rows = parse_jiuyan_square(
                r.text, max_age_days=int(cfg.get("max_age_days") or 2))
            fetched = await _jiuyan_enrich_details(c, rows, cfg.get("detail"))
            out.extend(rows)
            log.info("[jiuyan] 收 %d 条（详情请求 %d 次）", len(rows), fetched)
    except Exception as e:
        log.warning("[jiuyan] 采集失败: %s", e)
    return out
