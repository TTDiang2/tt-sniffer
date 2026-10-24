"""FastAPI 服务：REST 查询 + SSE 实时推送 + 静态页面。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .config import CONFIG, ROOT, resolve_channel
from .pipeline import Pipeline
from . import auth
from .quotes import Quotes
from .store import Store

app = FastAPI(title="TT_Sniffer", docs_url=None, redoc_url=None)
log = logging.getLogger("server")

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AI_RATE_MAX = 10
_AI_RATE_WINDOW = 60.0


def _valid_day(day: str) -> str:
    if not _DAY_RE.fullmatch(day or ""):
        raise HTTPException(status_code=400, detail="day 需为 YYYY-MM-DD")
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status_code=400, detail="day 不是有效日期")
    return day


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") \
        or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def _ai_rate_ok(request: Request) -> bool:
    now = time.monotonic()
    table: dict[str, list[float]] = request.app.state.ai_hits
    hits = table.setdefault(_client_ip(request), [])
    hits[:] = [t for t in hits if now - t < _AI_RATE_WINDOW]
    if len(hits) >= _AI_RATE_MAX:
        return False
    hits.append(now)
    if len(table) > 256:
        for ip, ts in list(table.items()):
            fresh = [t for t in ts if now - t < _AI_RATE_WINDOW]
            if fresh:
                table[ip] = fresh
            else:
                table.pop(ip, None)
    return True


@app.middleware("http")
async def basic_auth_gate(request: Request, call_next):  # noqa: A002 - starlette 惯例
    """应用层密码挡（CF 上线第二道防线）+ 观察期访问统计/公网读限速。

    顺序：入口计数 → 公网 /api/* 读限速（429）→ 静态公开直通 → auth
    （游客只读豁免在 auth.check 内）→ 记状态码。本机直连（loopback 三条件）
    既不计数也不限速——主人不是观察对象。
    """
    from . import access_stats
    access_stats.observe_request(request)
    rl = auth.rate_limit(request)
    if rl is not None:
        access_stats.observe_status(request, rl.status_code)
        access_stats.maybe_flush()
        return rl
    path = request.url.path
    if request.method in ("GET", "HEAD") and (
        path in ("/manifest.webmanifest", "/sw.js", "/healthz")
        or path.startswith("/icons/")
    ):
        resp = await call_next(request)
        access_stats.observe_status(request, resp.status_code)
        access_stats.maybe_flush()
        return resp
    resp = await auth.check_async(request)
    if resp is not None:
        access_stats.observe_status(request, resp.status_code)
        access_stats.maybe_flush()
        return resp
    resp = await call_next(request)
    access_stats.observe_status(request, resp.status_code)
    access_stats.maybe_flush()
    return resp


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "connect-src 'self'; font-src 'self' data:; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'")
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if request.url.scheme == "https" or proto == "https":
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp



# 2026-10-16：同步 DB 端点一律 def（FastAPI 自动入线程池）——此前 async def
# 里跑同步查询，任何一次慢查询都会冻结整个事件循环（SSE 掉线/全站「载入
# 失败」，配合 _attach_clusters 旧热循环酿成 40s 级全站冻结事故）。
# 仅 SSE/流式/自调度端点保持 async。
@app.get("/api/items")
def api_items(cls: str = "all", q: str = "", watch: int = 0,
              limit: int = 50, offset: int = 0, day: str = "",
              sort: str = "time", min_score: int = -1, max_score: int = -1,
              sources: str = "", asset: str = "all", d: str = "all",
              group: int = 0, tier: str = ""):
    # 前端"关注"页签语义是跨类别的 watch 过滤，不是 cls 值
    if cls == "watch":
        cls, watch = "all", 1
    store: Store = app.state.store
    # 下限钳 1：负数 limit 会绕过 LIMIT 直接返回全表（SQLite 负 LIMIT 不设上限）
    src_list = [s.strip() for s in sources.split(",") if s.strip()] or None
    # d：与 asset 联动时限定该资产方向；无 asset 时独立生效=只看带该方向
    # 标记的条目（2026-10-16 用户需求：利多/利空全局筛选）
    dir_list = ([x.strip() for x in d.split(",") if x.strip() in ("bull", "bear", "rel")]
                if d and d != "all" else None)
    if day:
        _valid_day(day)
    # tier=S4 小道源筛选（前端「小道」开关传 street）；空=不过滤（既有行为）
    return store.query(cls=cls, q=q or None, watch_only=bool(watch),
                       limit=max(1, min(limit, 200)), offset=max(0, offset),
                       day=day or None, sort=sort if sort in ("time", "score") else "time",
                       min_score=min_score if min_score >= 0 else None,
                       max_score=max_score if max_score >= 0 else None,
                       sources=src_list,
                       asset=asset if asset and asset != "all" else None,
                       direction=dir_list, group=bool(group),
                       tier=tier or None)


@app.get("/api/assets")
def api_assets():
    """资产类别单一事实来源（前端页签禁止硬编码第二份表）。"""
    from . import charts
    from .impact import ASSETS
    return {"assets": [{"a": k, "name": v["name"], "icon": v["icon"],
                        "bucket": charts.bucket_of(k)}
                       for k, v in ASSETS.items()]}


@app.get("/api/dotplot")
def api_dotplot(day: str, cb: str = "fed"):
    """利率观点点阵（用户需求：类 FOMC dot plot 的信息流版）。"""
    from . import fed_sep
    from .dotplot import base_from_sep, cell_level, extract_items
    _valid_day(day)
    store: Store = app.state.store
    rows = store.day_items(day)
    # 当前利率区间（v2 用户规格：纵轴具体点位）；解析失败 base=None 前端回退动作轴。
    # 仅 fed 有 SEP 基准，cb≠fed 置 None 防患（未来非美联储点阵不得错挂美联储档位）。
    try:
        base = base_from_sep(fed_sep.get_current()) if cb == "fed" else None
    except Exception:
        base = None
    dots = []
    for r in rows:
        for v in extract_items((r["title"] or "") + "。" + (r["text"] or "")):
            if v["cb"] != cb:
                continue
            dots.append({"id": r["id"], "ts": r["ts"], "source": r["source"],
                         "cls": r["cls"], "score": r["score"], **v})
    # 同格子同来源同稿折叠：cell=(month,action,bp) 内按 source 去重计数
    cells: dict[tuple, dict] = {}
    for d in dots:
        key = (d["month"], d["action"], d["bp"])
        cell = cells.setdefault(key, {"month": d["month"], "action": d["action"],
                                      "bp": d["bp"], "n": 0,
                                      "sources": set(), "actors": set(),
                                      "probs": [], "items": []})
        cell["n"] += 1
        cell["sources"].add(d["source"])
        cell["actors"].add(d["actor"])
        if d["prob"] is not None:
            cell["probs"].append(d["prob"])
        cell["items"].append({"id": d["id"], "source": d["source"],
                              "ts": d["ts"], "actor": d["actor"],
                              "prob": d["prob"]})
    out = [{"month": c["month"], "action": c["action"], "bp": c["bp"],
            "level": cell_level(c["action"], c["bp"], base["mid"]) if base else None,
            "n": c["n"], "sources": sorted(c["sources"]),
            "actors": sorted(c["actors"]),
            "prob": round(sum(c["probs"]) / len(c["probs"]), 1)
            if c["probs"] else None,
            "items": c["items"]}
           for c in sorted(cells.values(),
                           key=lambda c: (c["month"], c["action"], c["bp"] or 0))]
    return {"day": day, "cb": cb, "cells": out, "base": base}


@app.get("/api/sentiment/history")
def api_sentiment_history(days: int = 14):
    store: Store = app.state.store
    return store.sentiment_history(days=min(max(days, 1), 60))


@app.get("/api/sep")
def api_sep():
    """FOMC 官方 SEP 点阵：缓存优先（季度自动抓取），静态兜底。"""
    from .fed_sep import get_current
    return get_current()


@app.get("/api/calendar")
def api_calendar(days: int = 14, country: str | None = None):
    """宏观日历：未来 N 日 + 昨日实际值；country=US/CN 可选。"""
    from datetime import date, timedelta
    from .collectors import calendar as calendar_c
    days = min(max(days, 1), 60)
    today = date.today()
    rows = calendar_c.read_events(
        country=country or None,
        since=(today - timedelta(days=1)).isoformat(),
        until=(today + timedelta(days=days)).isoformat() + " 23:59:59",
        limit=500,
    )
    missing = calendar_c.fomc_missing_years(today.year, today.year + 1)
    warning = None
    if missing:
        years = "、".join(str(y) for y in missing)
        warning = f"缺少 {years} 年 FOMC 日程，相关会议可能未展示"
    return {"days": days, "events": [dict(r) for r in rows],
            "fomc_schedule_warning": warning}


@app.get("/api/rates")
def api_rates(limit: int = 30):
    """CME ZQ 近月/次月结算快照（本地 JSONL，只读）。"""
    from .collectors import rates_snap as rates_c
    rows = rates_c.read_snapshots(limit=min(max(limit, 1), 120))
    return {"rows": rows, "latest": rows[-1] if rows else None}


@app.get("/api/scatter")
def api_scatter(day: str):
    """风险×避险散点：当日单新闻点 + 近14日日点轨迹。"""
    import json as _json
    from . import charts
    _valid_day(day)
    store: Store = app.state.store
    rows = store.day_items(day, impacts=True)
    news = []
    for r in rows:
        p = charts.news_point(_json.loads(r["impacts"] or "[]"))
        if p:
            news.append({"id": r["id"], "x": p["x"], "y": p["y"],
                         "sides": p["sides"], "score": r["score"],
                         "cls": r["cls"], "source": r["source"],
                         "ts": r["ts"],
                         "disp": (r["title"] or (r["text"] or ""))[:48]})
    # 观点簇聚合（v7，用户裁定方案）：同坐标多点合并为一个簇点——
    # 同方向新闻坐标天然相同（如非农日全部 us_data_weak 都在 (100,100)），
    # 逐点渲染会重叠不可见；簇点保留全部成员供悬停展开
    clusters: dict[tuple, dict] = {}
    for n in news:
        key = (n["x"], n["y"])
        c = clusters.setdefault(key, {"x": n["x"], "y": n["y"],
                                      "sides": n["sides"], "n": 0,
                                      "items": []})
        c["n"] += 1
        c["items"].append(n)
    cells = sorted(clusters.values(), key=lambda c: -c["n"])
    hist = store.sentiment_history(days=14)
    # 直接复用 history 已算好的每日资产分：逐日再调 day_sentiment 会把同一批
    # impacts 重复计算 14 次，慢到前端 8s 超时后图表永久空白。
    trail = []
    for i, d in enumerate(hist["days"]):
        rows = [{"a": r["a"], "score": r["scores"][i]}
                for r in hist["rows"] if r["scores"][i] is not None]
        p = charts.day_point(rows)
        if p:
            trail.append({"day": d, "x": p["x"], "y": p["y"],
                          "sides": p["sides"]})
    return {"day": day, "clusters": cells, "trail": trail}


@app.get("/api/watchlist")
def api_watchlist_get():
    """当前生效关注列表（S2）：DB 用户层优先，无则 config 种子（UI 据此提示）。"""
    store: Store = app.state.store
    user = store.get_watchlist_user()
    if user is not None:
        return {"items": user, "source": "user"}
    return {"items": CONFIG.get("watchlist", []) or [], "source": "config"}


@app.put("/api/watchlist")
def api_watchlist_put(items: list[dict]):
    """全量替换用户层（S2）：name 必填 code 可空、去重、超长截断拒收；
    只写 DB 不回写 config.yaml；保存后热更新打分器（新条目立即生效，历史不重算）。"""
    clean: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", "")).strip()
        code = str(it.get("code", "")).strip()
        if not name or len(name) > 60 or len(code) > 12:
            continue
        key = (code, name)
        if key in seen:
            continue
        seen.add(key)
        clean.append({"code": code, "name": name})
    store: Store = app.state.store
    store.set_watchlist_user(clean)
    app.state.pipeline.update_watchlist(clean)
    return {"items": clean, "source": "user"}


@app.get("/api/days")
def api_days(days: int = 14):
    store: Store = app.state.store
    return {"days": store.day_index(days=min(max(days, 1), 60))}


@app.get("/api/day")
def api_day(day: str):
    store: Store = app.state.store
    return store.day_overview(_valid_day(day))


@app.get("/api/sentiment")
def api_sentiment(day: str):
    """当日资产情绪分（-100..+100，None=无方向票）。"""
    store: Store = app.state.store
    return {"day": day, "sentiment": store.day_sentiment(_valid_day(day))}


@app.get("/api/summary")
def api_summary(day: str):
    """单日统计聚合：分时趋势/来源占比/分类占比（统计面板）。"""
    store: Store = app.state.store
    return store.day_summary(_valid_day(day))


@app.get("/api/collide")
def api_collide(day: str):
    """辟谣对撞：当日传闻与其辟谣的配对（速览块）。"""
    store: Store = app.state.store
    return {"pairs": store.day_collisions(_valid_day(day))}


@app.get("/api/stats")
def api_stats():
    store: Store = app.state.store
    pipeline: Pipeline = app.state.pipeline
    return {"stats": store.stats(), "collectors": pipeline.collector_status,
            "alert_threshold": pipeline.alert_threshold}


@app.get("/api/access")
def api_access(request: Request):
    """公网访问统计（观察期）：仅本机直连可看，经隧道/代理一律 404 不显存在。"""
    from .auth import _loopback_exempt
    if not _loopback_exempt(request):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    from . import access_stats
    return access_stats.snapshot()


@app.get("/healthz")
def healthz():
    try:
        app.state.store.stats()
        return {"ok": True, "store_ok": True}
    except Exception:
        log.exception("healthz 检查失败")
        return JSONResponse(status_code=503,
                            content={"ok": False, "store_ok": False})


@app.get("/api/hot")
def api_hot(hours: int = 24):
    return {"hot": app.state.store.hot_keywords(hours=hours)}


@app.get("/api/quotes")
async def api_quotes():
    """行情快照（S3）：恒回缓存（永不阻塞等待网络）；过期则后台刷新，
    本次返回上次成功值。空快照=行情源整体不可用，前端收缩成一行提示。"""
    quotes: Quotes = app.state.quotes
    if quotes.stale() and quotes.begin_refresh():
        async def _bg():
            try:
                await asyncio.to_thread(quotes.refresh)
            finally:
                quotes.end_refresh()
        asyncio.create_task(_bg())
    return {"quotes": quotes.snapshot(), **quotes.status()}


@app.get("/api/anomaly")
def api_anomaly():
    pipeline: Pipeline = app.state.pipeline
    return {"anomaly": pipeline.tracker.summary()}
@app.get("/api/stream")
async def api_stream(request: Request):
    pipeline: Pipeline = app.state.pipeline
    q = pipeline.subscribe()

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'], ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            pipeline.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/ai/status")
async def api_ai_status():
    """AI 启用状态（前端抽屉据此显示配置指引，免得用户摸不到启用入口）。

    多 key 扩展为增量式：平铺字段 enabled/configured/has_key/model 原样保留
    （前端只读这些，形状变更会弄坏抽屉）；新增 channels 按通道报模型/就绪状态，
    不返回 base_url、密钥数量或环境变量名。"""
    import os
    cfg = CONFIG.get("llm") or {}
    key = os.environ.get(str(cfg.get("api_key_env") or "TTSNIFFER_LLM_API_KEY"), "")
    enabled_cfg = bool(cfg.get("enabled"))
    channels = {}
    for name in ("translate", "comment", "chat"):
        ch = resolve_channel(cfg, name)
        envs = ch.get("keys_env") or []
        channels[name] = {"model": ch.get("model") or "",
                          "ready": any(os.environ.get(e) for e in envs)}
    pipeline = getattr(app.state, "pipeline", None)
    translation = {
        "pending": len(pipeline._translate_backlog) if pipeline else 0,
        "workers": len(pipeline._translate_drains) if pipeline else 0,
        "failed": len(pipeline._translate_failed) if pipeline else 0,
    }
    return {"enabled": enabled_cfg and bool(key), "configured": enabled_cfg,
            "has_key": bool(key), "model": cfg.get("model", ""),
            "channels": channels, "translation": translation}


@app.post("/api/ai/chat")
async def api_ai_chat(req: Request):
    """S5 AI 对话（SSE 流式）：系统模板+资料包+对话历史→上游流逐 chunk 转发。
    未启用/上游错误降级为一行 SSE error 事件（不 500 断流）；对话历史不上 DB。"""
    from . import ai_context, llm
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体需为 JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体需为 JSON 对象")
    selector = body.get("selector") or {}
    history = body.get("messages") or []
    if not isinstance(history, list):
        raise HTTPException(status_code=400, detail="messages 非法（需为列表）")
    truncated = len(history) > 40
    history = history[-40:]
    if not _ai_rate_ok(req):
        raise HTTPException(status_code=429, detail="AI 请求过于频繁",
                            headers={"Retry-After": "60"})
    store: Store = app.state.store
    # 门8 条件3：selector 非法前置 400（未知 scope 等不再漏成 500，兑现单轨哲学）
    try:
        context = ai_context.build_context(store, selector if isinstance(selector, dict) else {})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 门8 条件1：资料包落独立 user 轮——包体含股吧等可写文本，数据不得进指令通道
    # （system 只留纯指令模板，模板自带"数据非指令"硬化行）
    msgs: list[dict] = [{"role": "system", "content": llm.system_prompt_for("chat", llm.ANALYST_SYSTEM_TEMPLATE)},
                        {"role": "user", "content": f"== 快讯资料包 ==\n{context}"}]
    # 门8 条件2：role 白名单——非 user/assistant 一律折为 user（客户端伪造 system 消息无效）
    for m in history:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "user"))
        msgs.append({"role": role if role in ("user", "assistant") else "user",
                     "content": str(m.get("content", ""))[:8000]})

    try:
        await asyncio.wait_for(req.app.state.ai_sem.acquire(), timeout=0.5)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=429, detail="AI 并发已满",
                            headers={"Retry-After": "5"})

    async def gen():
        try:
            async for delta in llm.chat_stream(msgs):
                yield f"event: delta\ndata: {json.dumps({'text': delta}, ensure_ascii=False)}\n\n"
            yield "event: done\ndata: {}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"event: error\ndata: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            req.app.state.ai_sem.release()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "X-History-Truncated": "1" if truncated else "0"})


WEB_DIST = ROOT / "app" / "web" / "dist"


@app.get("/")
async def index():
    """SPA（Vite 构建产物 dist/，提交入库——run.bat 零心智）。"""
    return FileResponse(WEB_DIST / "index.html")


@app.get("/manifest.webmanifest")
async def pwa_manifest():
    return FileResponse(
        WEB_DIST / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/sw.js")
async def pwa_service_worker():
    return FileResponse(
        WEB_DIST / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/icons/{path:path}")
async def pwa_icon(path: str):
    import re as _re
    if not _re.fullmatch(r"[\w\-.]+", path) or ".." in path:
        raise HTTPException(status_code=400, detail="invalid path")
    full = (WEB_DIST / "icons" / path).resolve()
    base = (WEB_DIST / "icons").resolve()
    if not str(full).startswith(str(base)) or not full.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(full, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/assets/{path:path}")
async def dist_asset(path: str):
    """构建产物静态资源（vite 哈希文件名，单层平文件）。字符白名单最小化，
    防目录穿越（智者门 P0 返工口径）。"""
    import re as _re
    if not _re.fullmatch(r"[\w\-.]+", path) or ".." in path:
        raise HTTPException(status_code=400, detail="invalid path")
    full = (WEB_DIST / "assets" / path).resolve()
    base = (WEB_DIST / "assets").resolve()
    if not str(full).startswith(str(base)) or not full.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(full)


def create_app(store: Store, pipeline: Pipeline, quotes: Quotes | None = None) -> FastAPI:
    app.state.store = store
    app.state.pipeline = pipeline
    app.state.quotes = quotes or Quotes()
    app.state.ai_sem = asyncio.Semaphore(2)
    app.state.ai_hits: dict[str, list[float]] = {}
    return app
