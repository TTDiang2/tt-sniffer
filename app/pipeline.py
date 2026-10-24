"""流水线：采集循环 -> 归一化 -> 去重 -> 打分 -> 落库 -> 广播/告警。

告警决策（V0.1.1 按审查修订）：
- SSE 高亮（alert 事件）在打分后即时广播；
- webhook 推送需满足 (score>=阈值 或 关注标的且属传闻/辟谣类)，且同 targets
  10 分钟节流；启用 LLM 时等待 LLM 判定，is_rumor=false 且置信度>0.7 则拦截。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import deque
from datetime import datetime, timedelta

from .collectors import news as news_c
from .collectors import street as street_c
from .collectors import crypto_rss as crypto_c
from .collectors import jiuyan as jiuyan_c
from .collectors import rss as rss_c
from .collectors import polymarket as pm_c
from .collectors import calendar as calendar_c
from .collectors import rates_snap as rates_snap_c
from .collectors.anomaly import (AnomalyTracker, fetch_anomaly, fetch_ztpool,
                                 in_trading_hours)
from .config import CONFIG
from .models import Item
from .scoring import Scorer, literal_codes
from .store import RETENTION_DAYS, Store

log = logging.getLogger("pipeline")

_WS = re.compile(r"\s+")
_CJK = re.compile(r"[\u4e00-\u9fff]")
TRANSLATE_FAILED_TTL = 3600
TRANSLATE_FAILED_MAX = 1024
_LATIN = re.compile(r"[A-Za-z]")
WEBHOOK_THROTTLE_S = 600  # 同 targets 告警合并窗口（秒）
# S4：小道源→tier='street'（可信度正交维度，打分降档+前端⚠️标）；其余 authoritative
TIER_BY_SOURCE = {"guba": "street", "tg": "street", "jiuyan": "street"}
FOREIGN_SOURCES = {
    "bbg_markets", "bbg_econ", "bbg_politics", "bbg_tech", "bbg_google",
    "wsj_markets", "wsj_world", "wsj_business", "wsj_tech", "marketwatch",
    "reuters", "ap", "ft", "cnbc", "google_news", "polymarket",
}


def needs_translation(item: Item) -> bool:
    """外网新条目才翻译；已知源直接命中，其余按拉丁字母/汉字比例兜底。"""
    if item.source in FOREIGN_SOURCES:
        return True
    sample = f"{item.title or ''} {item.text or ''}"
    cjk = len(_CJK.findall(sample))
    latin = len(_LATIN.findall(sample))
    return latin >= 12 and cjk * 2 < latin


def norm_id(source: str, title: str, text: str, stable: str = "") -> str:
    # 不能截断：不同新闻常共享前 120 字（转载/模板），截断会让 INSERT OR IGNORE
    # 静默丢掉第二条。去掉空白保证同一内容的不同排版仍得到同一 id。
    # stable 参数（20261024）：仅 guba/jiuyan 传（调用方保证形如 post:<pid>/
    # art:<aid>，帖级唯一键）——详情补抓会让同一帖的 text 跨轮变异（标题→全文），
    # 此时 text 退出 key、stable 作主键，防止补抓成功轮生成新 id 造成同帖双行
    # 入库+二次广播。其余源不传（含 text 的旧 key 构造逐字节不变）。
    if stable:
        key = f"{source}|{_WS.sub('', title or '')}|{stable}"
    else:
        key = f"{source}|{_WS.sub('', title or '')}|{_WS.sub('', text or '')}"
    return hashlib.md5(key.encode("utf-8", "replace")).hexdigest()


class Pipeline:
    def __init__(self, store: Store):
        self.store = store
        self.scorer = Scorer(CONFIG.get("watchlist", []))
        # S2 门5 #1：启动回灌——kv 用户层存在则覆盖 config 种子。没有这一步，
        # 重启后打分回退种子而 GET/UI 仍按用户层展示，持久契约静默断裂。
        user_watchlist = store.get_watchlist_user()
        if user_watchlist is not None:
            self.update_watchlist(user_watchlist)
        self.tracker = AnomalyTracker()
        self.subscribers: set[asyncio.Queue] = set()
        self.collector_status: dict[str, dict] = {}
        self.alert_threshold = CONFIG["scoring"].get("alert_threshold", 80)
        self.broadcast_cap = CONFIG["scoring"].get("broadcast_cap", 60)
        self.llm_min_score = CONFIG.get("llm", {}).get("min_score", 55)
        self.llm_enabled = CONFIG.get("llm", {}).get("enabled", False)
        self.llm_round_cap = CONFIG.get("llm", {}).get("round_cap", 20)
        self._seen_last_ts: dict[str, datetime] = {}   # 每来源上次水位
        self._first_run: dict[str, bool] = {}
        self._bg: set[asyncio.Task] = set()            # 后台任务强引用，防 GC
        self._wh_last: dict[str, float] = {}           # webhook 节流: key -> monotonic
        self.translate_sem = asyncio.Semaphore(3)
        self._translate_backlog: deque[Item] = deque()
        self._translate_drains: set[asyncio.Task] = set()
        self._translate_failed: dict[str, float] = {}

    # ---------- 后台任务 ----------
    def update_watchlist(self, items: list[dict]) -> None:
        """S2 热更新：UI 保存关注列表后重算 Scorer 词表（构造快照 → 可刷新引用）。
        边界（晨报声明）：仅新采集条目按新表打分，历史条目不重算——
        回填重算涉及改写用户数据，禁做。"""
        names = [w["name"] for w in items if w.get("name")]
        codes = {w["code"] for w in items if w.get("code")}
        # 局部算完再紧邻赋值，收窄工作线程读到半新半旧表的窗口（门5 观察项）
        self.scorer.watch_names = names
        self.scorer.watch_codes = codes

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._bg.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("后台任务失败: %s", task.get_name(),
                      exc_info=(type(exc), exc, exc.__traceback__))

    # ---------- 广播 ----------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self.subscribers.discard(q)

    def _broadcast(self, event: str, payload: dict):
        for q in list(self.subscribers):
            try:
                q.put_nowait({"event": event, "data": payload})
            except asyncio.QueueFull:
                pass

    # ---------- 主流程 ----------
    def _linked_codes(self, full: str, snap: dict) -> set[str]:
        """近30分钟异动、且确实出现在本文中的标的（代码字面命中或名称命中）。
        只使用快照数据，线程安全。"""
        recent = snap["recent"]
        if not recent:
            return set()
        by_name = {c for n, c in snap["name2code"].items() if n and n in full}
        linked = (recent & literal_codes(full)) | (recent & by_name)
        # 用已知名称展示，比裸代码友好
        return {snap["code2name"].get(c, c) for c in linked}

    async def _ingest(self, source: str, raws: list[dict]):
        # 打分+批量入库是 CPU/IO 密集的同步段，挪到线程池执行，
        # 避免首轮回填大结果集时阻塞事件循环（API/SSE 会饿死数秒）。
        # tracker 在进入线程前做快照：deque/dict 不能边遍历边被事件循环 mutate。
        snap = {
            "recent": self.tracker.recent_codes(30),
            "name2code": dict(self.tracker.name2code),
            "code2name": dict(self.tracker.code2name),
        }
        if source not in self._seen_last_ts:
            saved = self.store.get_kv(f"pipeline.watermark:{source}")
            if saved:
                try:
                    self._seen_last_ts[source] = datetime.fromisoformat(saved)
                except ValueError:
                    log.warning("[%s] 忽略损坏的采集水位: %r", source, saved)
                else:
                    self._first_run[source] = False
        last_ts = self._seen_last_ts.get(source)
        items, max_ts = await asyncio.to_thread(
            self._ingest_sync, source, raws, last_ts, snap)
        if max_ts is not None:
            self._seen_last_ts[source] = max_ts
            try:
                self.store.set_kv(
                    f"pipeline.watermark:{source}",
                    max_ts.replace(tzinfo=None).isoformat(sep=" "))
            except Exception:
                log.exception("[%s] 持久化采集水位失败", source)
        self._first_run[source] = False

        # 首轮回填只入库不广播；此后按 broadcast_cap 限额广播（高分条目豁免，
        # 防止断线恢复大批量时排在后面的重要条目静默丢失告警）
        # LLM 精筛独立于广播上限：被 cap 挤掉的 55-79 分条目只要达到
        # llm_min_score 仍要上卡（V0.2 遗留漏斗债）；非候选项每轮限
        # round_cap（按分取前 N，超出记日志）防爆发轮烧穿用户 LLM 配额；
        # webhook 候选（关注+传闻/辟谣 或 高分）豁免限额——它们本就稀少、
        # webhook 另有 10 分钟节流，配额保护不能丢掉推送通道（A1 门条件2）
        new_items = [it for it, ok, _ in items if ok]
        emitted = [it for it, ok, _ in items if ok and not it.backfill]
        ginfo_by_id = {it.id: g for it, ok, g in items if g}
        llm_pool = [it for it in emitted
                    if self.llm_enabled and it.score >= self.llm_min_score]
        candidates = [it for it in llm_pool if self._webhook_candidate(it)]
        non_candidates = [it for it in llm_pool if not self._webhook_candidate(it)]
        for item in candidates:
            self._spawn(self._enrich_and_alert(item))
        for item in sorted(non_candidates, key=lambda it: -it.score)[:self.llm_round_cap]:
            self._spawn(self._enrich_and_alert(item))
        if len(non_candidates) > self.llm_round_cap:
            log.warning("[LLM] 本轮精筛超额 %d 条跳过（round_cap=%d，按分截取，条目已正常入库）",
                        len(non_candidates) - self.llm_round_cap, self.llm_round_cap)
        # 翻译独立于评分/传闻漏斗：外网条目普遍拿不到高分，等 55 分线等于永不翻译。
        if self.llm_enabled:
            translating = [it for it in emitted if needs_translation(it)]
            self._queue_translations(translating)
            if translating:
                log.info("[翻译] 本轮 %d 条外网条目（待译 %d）",
                         len(translating), len(self._translate_backlog))
        for idx, item in enumerate(emitted):
            if idx < self.broadcast_cap or item.score >= self.alert_threshold:
                row = item.to_row()
                # 附组信息（SSE 直播行显示「N 条同类」徽章——修既有债；
                # group_of 已在 _ingest_sync 线程段预取，此处零查询）
                g = ginfo_by_id.get(item.id)
                if g:
                    row.update(g)
                self._broadcast("item", row)
                # 门7 条件1：street 抑制 alert（蜂鸣事件）；item 广播保留=页面可见性不受损
                if item.score >= self.alert_threshold and item.tier != "street":
                    self._broadcast("alert", item.to_row())
            # webhook 候选在精筛完成后决策（走 enrich 通道）；未达精筛线的
            # 候选直发——与 LLM 分支互斥的语义保持不变
            if not (self.llm_enabled and item.score >= self.llm_min_score) \
                    and self._webhook_candidate(item):
                self._spawn(self._send_webhook_throttled(item))
        log.info("[%s] 新增 %d 条（广播 %d，告警候选 %d）", source, len(new_items),
                 len(emitted), sum(1 for i in emitted if self._webhook_candidate(i)))

    def _ingest_sync(self, source: str, raws: list[dict],
                     last_ts: datetime | None, snap: dict):
        """同步段：水位过滤 -> 打分 -> 批量入库。返回 ((item, inserted), max_ts)。"""
        known_codes = set(snap["code2name"]) | self.scorer.watch_codes
        out, max_ts = [], None
        tier = TIER_BY_SOURCE.get(source, "authoritative")  # 采集器可按 raw.tier 覆盖（crypto_rss feed 级）
        for raw in sorted(raws, key=lambda r: r["ts"]):
            # 水位用 <（而非 <=）：同一秒常有多条快讯，重复入库交给
            # INSERT OR IGNORE 兜底，不能在水位处丢弃
            if last_ts is not None and raw["ts"] < last_ts:
                continue
            if max_ts is None or raw["ts"] > max_ts:
                max_ts = raw["ts"]
            full = raw["title"] + raw["text"]
            verdict = self.scorer.score(
                raw["title"], raw["text"],
                linked=self._linked_codes(full, snap), known_codes=known_codes,
                tier=raw.get("tier") or tier)
            # guba/jiuyan 的 id 用帖级唯一键（post:/art: 前缀）且 text 退出 key：
            # 详情补抓使 text 跨轮变异，含 text 的 id 会在补抓成功轮分裂成新条目
            # （同帖双行+二次广播）。pid/aid 是帖级唯一，无撞键面；垃圾 href、
            # caifuhao 域名（pid 不在 url 中）一律无豁免
            stable = ""
            if source == "guba" and raw.get("post_id"):
                stable = f"post:{raw['post_id']}"
            elif source == "jiuyan" and raw.get("article_id"):
                stable = f"art:{raw['article_id']}"
            item = Item(
                id=norm_id(source, raw["title"], raw["text"], stable),
                source=source, ts=raw["ts"], title=raw["title"], text=raw["text"],
                url=raw["url"], cls=verdict["cls"], score=verdict["score"],
                keywords=verdict["keywords"], targets=verdict["targets"],
                watch=verdict["watch"],
                impacts=verdict.get("impacts", []),
                backfill=self._first_run.get(source, True),
                tier=raw.get("tier") or tier,
            )
            out.append((item, None))
        flags = self.store.insert_many([it for it, _ in out])
        pairs = [(it, ok) for (it, _), ok in zip(out, flags)]
        # update-on-enrich：同帖后轮补到全文会被 INSERT OR IGNORE 吸收——对
        # 已存在的 guba/jiuyan 行尝试"更长则更新"，库里标题版升级为全文版
        for it, ok in pairs:
            if not ok and it.source in ("guba", "jiuyan") and len(it.text) > 100:
                self.store.update_text_if_longer(it.id, it.title, it.text)
        backfill_ids = [it.id for it, ok in pairs if ok and it.backfill]
        if backfill_ids:
            self.store.schedule_backfill_grouping(backfill_ids)
        # 组信息同线程预取（group_of 含窗扫，42ms/条——放事件循环会阻塞
        # API/SSE）；首轮回填不广播，不预取
        ginfo = {it.id: self.store.group_of(it.id)
                 for it, ok in pairs if ok and not it.backfill}
        return [(it, ok, ginfo.get(it.id)) for it, ok in pairs], max_ts

    # ---------- 告警 ----------
    def _webhook_candidate(self, item: Item) -> bool:
        # 门7 条件1：street 恒非候选（双路径皆压）——降档在 base+watch 架构下可被
        # 架空（关注股全名+强词=87≥80），打扰权只属于正经源；street 的价值在
        # feed 的 ⚠️ 与 ?tier=street 浏览里兑现，不在用户手机上震动
        if item.tier == "street":
            return False
        if not (CONFIG.get("alert") or {}).get("webhook_url"):
            return False
        if item.score >= self.alert_threshold:
            return True
        return item.watch and item.cls in ("rumor", "debunk")

    def _throttled(self, item: Item) -> bool:
        key = ",".join(item.targets) or f"{item.source}|{item.cls}|{item.title[:20]}"
        now = time.monotonic()
        if now - self._wh_last.get(key, 0) < WEBHOOK_THROTTLE_S:
            return True
        self._wh_last[key] = now
        return False

    async def _enrich_and_alert(self, item: Item):
        """LLM 精筛上卡；若同时是 webhook 候选，等判定后再决策。"""
        verdict = await self._llm_enrich(item)
        if not self._webhook_candidate(item):
            return
        if verdict and verdict.get("is_rumor") is False \
                and (verdict.get("confidence") or 0) > 0.7:
            log.info("[告警拦截] LLM 判定非传闻: %s", item.title[:40])
            return
        if self._throttled(item):
            return
        await self._webhook(item)

    async def _send_webhook_throttled(self, item: Item):
        if self._throttled(item):
            return
        await self._webhook(item)

    async def _llm_enrich(self, item: Item) -> dict | None:
        from . import llm
        verdict = await llm.enrich(item.title, item.text)
        if verdict:
            self.store.set_llm(item.id, verdict)
            self._broadcast("llm", {"id": item.id, **verdict})
        return verdict

    def _queue_translations(self, items: list[Item]) -> None:
        """翻译进待办列表，最多 3 个当前事件循环的排空任务消费。

        不为每条新闻 create_task（爆发时会堆无界任务）；列表最终全部消费，
        空闲时自动继续。跨 asyncio.run 的测试/重启会丢弃旧 loop 的任务，
        但 backlog 保留，下一次 ingest 继续排空。
        """
        if not items:
            return
        self._translate_backlog.extend(items)
        if len(self._translate_backlog) > 500:
            log.warning("[翻译] 待译积压 %d 条，仍全量保留、限 3 并发消费",
                        len(self._translate_backlog))
        loop = asyncio.get_running_loop()
        self._translate_drains = {t for t in self._translate_drains
                                  if not t.done() and t.get_loop() is loop}
        while len(self._translate_drains) < 3 and self._translate_backlog:
            task = asyncio.create_task(self._translate_drain())
            self._translate_drains.add(task)
            self._bg.add(task)
            task.add_done_callback(self._task_done)
            task.add_done_callback(self._translate_drains.discard)

    async def _translate_drain(self) -> None:
        while self._translate_backlog:
            item = self._translate_backlog.popleft()
            for attempt in range(3):
                try:
                    if await self._translate_and_broadcast(item) is not False:
                        break
                except Exception:
                    log.exception("[翻译] %s 失败（第 %d 次）", item.id, attempt + 1)
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
            else:
                self._mark_translate_failed(item.id)
                log.warning("[翻译] 放弃 %s：连续 3 次失败", item.id)

    def _mark_translate_failed(self, item_id: str) -> None:
        """失败集合按 1h TTL + 1024 上限收缩，避免长期运行无限占内存。"""
        now = time.time()
        cutoff = now - TRANSLATE_FAILED_TTL
        for key, ts in list(self._translate_failed.items()):
            if ts < cutoff:
                del self._translate_failed[key]
        self._translate_failed[item_id] = now
        while len(self._translate_failed) > TRANSLATE_FAILED_MAX:
            self._translate_failed.pop(next(iter(self._translate_failed)))

    def _translate_failed_recently(self, item_id: str) -> bool:
        ts = self._translate_failed.get(item_id)
        return ts is not None and (time.time() - ts) < TRANSLATE_FAILED_TTL

    def _untranslated_candidates(self) -> list[Item]:
        rows = self.store.untranslated_items(
            sources=sorted(FOREIGN_SOURCES), limit=200)
        out: list[Item] = []
        for r in rows:
            if self._translate_failed_recently(r["id"]):
                continue
            item = Item(id=r["id"], source=r["source"],
                        ts=datetime.fromisoformat(r["ts"]),
                        title=r["title"] or "", text=r["text"] or "",
                        url=r["url"] or "")
            if needs_translation(item):
                out.append(item)
        return out

    async def translation_loop(self) -> None:
        """空闲时补译历史未翻译外网条目；有积压时让 drain 先消费。"""
        while True:
            await asyncio.sleep(120)
            if (not self.llm_enabled or self._translate_backlog
                    or any(not t.done() for t in self._translate_drains)):
                continue
            try:
                items = await asyncio.to_thread(self._untranslated_candidates)
            except Exception as e:
                log.warning("[翻译] 空闲补译扫描失败: %s", e)
                continue
            if items:
                log.info("[翻译] 空闲补译 %d 条历史未翻译条目", len(items))
                self._queue_translations(items)

    async def _translate_and_broadcast(self, item: Item) -> bool:
        from . import llm
        async with self.translate_sem:
            payload = await llm.translate(item.title, item.text)
        if not payload:
            return False
        await asyncio.to_thread(self.store.set_translation, item.id, payload)
        self._broadcast("translation", {"id": item.id, **payload})
        return True

    async def _webhook(self, item: Item):
        from . import push
        url = (CONFIG.get("alert") or {}).get("webhook_url")
        if not url:
            return
        kind = (CONFIG.get("alert") or {}).get("kind", "wecom")
        await push.send(url, kind, item)

    # ---------- 采集循环 ----------
    def _status(self, name: str, ok: bool, note: str = ""):
        self.collector_status[name] = {"ok": ok, "note": note,
                                       "updated": datetime.now().strftime("%H:%M:%S")}

    async def news_loop(self, name: str):
        interval = CONFIG["collect"][name].get("interval", 60)
        fetcher = news_c.NEWS_SOURCES[name]
        while True:
            try:
                raws = await fetcher()   # 超时已在 fetcher 内部兜底
                await self._ingest(name, raws)
                self._status(name, True, f"{len(raws)}条")
            except Exception as e:
                log.warning("[%s] 拉取失败: %s", name, e)
                self._status(name, False, str(e)[:80])
            await asyncio.sleep(interval)

    async def street_loop(self, name: str):
        """S4 小道源（guba/tg/jiuyan，tier='street'）采集循环：news_loop 同款容错形态。"""
        if name == "crypto_rss":
            cfg = CONFIG["collect"].get("crypto_rss") or {}
            fetcher = crypto_c.fetch_crypto_rss
        elif name == "jiuyan":
            cfg = CONFIG["collect"]["street"][name]
            fetcher = jiuyan_c.fetch_jiuyan
        else:
            cfg = CONFIG["collect"]["street"][name]
            fetcher = street_c.STREET_SOURCES[name]
        while True:
            try:
                raws = await fetcher(cfg)   # 超时与逐源容错已在 fetcher 内部兜底
                await self._ingest(name, raws)
                self._status(name, True, f"{len(raws)}条")
            except Exception as e:
                log.warning("[%s] 拉取失败: %s", name, e)
                self._status(name, False, str(e)[:80])
            await asyncio.sleep(cfg.get("interval", 600))

    async def rss_loop(self, section: str = "rss"):
        """RSS 段循环（rss=外网媒体、wechat_rss=公众号）：按 feed 的 source 分组
        入库，保留各自水位与前端来源标签。per-feed on_status 让每个 feed 在
        顶部可用性/左侧筛选独立可见。公众号段 tier 缺省 street（小作文语义，
        漏写不能静默落 authoritative 进打扰通道）。"""
        cfg = CONFIG["collect"].get(section) or {}
        default_tier = "street" if section == "wechat_rss" else "authoritative"
        while True:
            await self._rss_cycle(cfg, default_tier)
            await asyncio.sleep(cfg.get("interval", 300))

    async def _rss_cycle(self, cfg: dict, default_tier: str = "authoritative") -> None:
        """单轮 RSS 采集+分组入库（rss_loop 与 subscriptions_loop 共用）。"""
        try:
            raws = await rss_c.fetch_rss(cfg, on_status=self._status,
                                         default_tier=default_tier)
            groups: dict[str, list[dict]] = {}
            for raw in raws:
                groups.setdefault(raw["source"], []).append(raw)
            for source, rows in groups.items():
                await self._ingest(source, rows)
        except Exception as e:
            log.warning("[rss] 拉取失败: %s", e)
            for feed in cfg.get("feeds") or []:
                if feed.get("enabled", True):
                    name = str(feed.get("source") or feed.get("name") or "feed")[:32]
                    self._status(name, False, str(e)[:80])

    async def subscriptions_loop(self):
        """声明式订阅循环（订阅化阶段一，20261024）：subscriptions/*.yaml 丢文件
        即订阅。每轮重读目录（mtime 未变走缓存），目录空则安静待机。"""
        from . import subscriptions as subs_c
        cfg_all = CONFIG["collect"].get("subscriptions") or {}
        while True:
            try:
                subs = subs_c.load_subscriptions()
                if subs:
                    await self._rss_cycle(subs_c.as_feed_cfg(subs),
                                          default_tier="street")
                    log.info("[subs] %d 个订阅本轮完成", len(subs))
            except Exception as e:  # noqa: BLE001
                log.warning("[subs] 订阅轮失败: %s", e)
            await asyncio.sleep(subs_c.cycle_seconds(subs) if subs
                                else int(cfg_all.get("interval", 60)))

    async def polymarket_loop(self):
        cfg = CONFIG["collect"].get("polymarket") or {}
        while True:
            try:
                raws = await pm_c.fetch_polymarket(cfg)
                await self._ingest("polymarket", raws)
                self._status("polymarket", True, f"{len(raws)}条")
            except Exception as e:
                log.warning("[polymarket] 拉取失败: %s", e)
                self._status("polymarket", False, str(e)[:80])
            await asyncio.sleep(cfg.get("interval", 300))

    async def anomaly_loop(self):
        cfg = CONFIG["collect"]["anomaly"]
        tracker = self.tracker
        while True:
            if in_trading_hours():
                try:
                    fresh = await fetch_anomaly(tracker, cfg.get("types", []))
                    self._status("anomaly", True, f"{len(fresh)}条")
                    self._broadcast("anomaly", {"summary": tracker.summary()})
                except Exception as e:
                    self._status("anomaly", False, str(e)[:80])
                await asyncio.sleep(cfg.get("interval", 60))
            else:
                self._status("anomaly", True, "非交易时段待机")
                await asyncio.sleep(300)

    async def ztpool_loop(self):
        interval = CONFIG["collect"]["ztpool"].get("interval", 1800)
        while True:
            try:
                n = await fetch_ztpool(self.tracker)
            except Exception as e:
                # 任何异常都不允许杀死本 task（gather 上抛会让整个进程退出）
                log.warning("涨停池采集异常: %s", e)
                n = -1
            # -1=接口失败（区别于休市）；0 可能是休市或当日无涨停
            self._status("ztpool", n >= 0,
                         f"{n}家涨停" if n >= 0 else "接口失败重试中")
            await asyncio.sleep(interval)

    async def calendar_loop(self):
        """宏观日历：首轮回填近 N 天+前瞻，之后每日前瞻、每轮补近两日实际值。"""
        cfg = CONFIG["collect"].get("calendar") or {}
        interval = cfg.get("interval", 1800)
        ahead = int(cfg.get("days_ahead", 7))
        backfill = int(cfg.get("backfill_days", 7))
        first, last_forward = True, None
        while True:
            try:
                today = datetime.now().date()
                if first:
                    rows = await calendar_c.fetch_calendar(
                        days_back=backfill, days_ahead=ahead)
                    note = "首轮回填"
                    first, last_forward = False, today
                elif last_forward != today:
                    rows = await calendar_c.fetch_calendar(
                        days_back=0, days_ahead=ahead)
                    note = "每日前瞻"
                    last_forward = today
                else:
                    rows = await calendar_c.fetch_calendar(
                        days_back=1, days_ahead=0)
                    note = "实际值回填"
                await asyncio.to_thread(calendar_c.upsert_events, rows)
                self._status("calendar", True, f"{len(rows)}条({note})")
            except Exception as e:
                log.warning("[calendar] 同步失败: %s", e)
                self._status("calendar", False, str(e)[:80])
            await asyncio.sleep(interval)

    async def rates_snap_loop(self):
        cfg = CONFIG["collect"].get("rates_snap") or {}
        hour = int(cfg.get("hour", 5))
        minute = int(cfg.get("minute", 10))
        while True:
            try:
                rec = await rates_snap_c.snapshot_async()
                self._status("rates_snap", True,
                             f"{rec['date']} 近月 {rec['settle_front']}")
            except Exception as e:
                log.warning("[rates_snap] 快照失败: %s", e)
                self._status("rates_snap", False, str(e)[:80])
            now = datetime.now()
            target = now.replace(hour=hour, minute=minute,
                                 second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())

    async def sep_loop(self):
        """FOMC SEP 季度自动更新：启动检查一次，此后每 12 小时节流检查。

        发现新会议才抓官网正文（礼貌爬取）；官网不可达静默降级用静态数据，
        绝不影响采集主链路。"""
        from .sep_fetch import needs_check, refresh
        last = None
        while True:
            if needs_check(last):
                try:
                    await asyncio.to_thread(refresh)
                except Exception as e:
                    log.warning("SEP 自动更新异常（用现有数据）: %s", e)
                last = datetime.now().isoformat()
            await asyncio.sleep(3600)

    async def maintenance_loop(self):
        """每小时滚动清理旧条目（启动时的那次在 Store.__init__）。"""
        while True:
            await asyncio.sleep(3600)
            try:
                deleted = await asyncio.to_thread(self.store.cleanup)
                if deleted:
                    log.info("[清理] 删除 %d 条超 %d 天旧条目", deleted, RETENTION_DAYS)
            except Exception as e:
                log.warning("[清理] 失败: %s", e)

    async def run(self):
        tasks = []
        for name in ("cls", "em", "sina", "ths", "wscn"):
            if CONFIG["collect"][name].get("enabled", True):
                tasks.append(asyncio.create_task(self.news_loop(name)))
        if CONFIG["collect"]["anomaly"].get("enabled", True):
            tasks.append(asyncio.create_task(self.anomaly_loop()))
        if CONFIG["collect"]["ztpool"].get("enabled", True):
            tasks.append(asyncio.create_task(self.ztpool_loop()))
        calendar_cfg = CONFIG["collect"].get("calendar") or {}
        if calendar_cfg.get("enabled", True):
            tasks.append(asyncio.create_task(self.calendar_loop()))
        rates_cfg = CONFIG["collect"].get("rates_snap") or {}
        if rates_cfg.get("enabled", True):
            tasks.append(asyncio.create_task(self.rates_snap_loop()))
        tasks.append(asyncio.create_task(self.sep_loop()))
        if self.llm_enabled:
            tasks.append(asyncio.create_task(self.translation_loop()))
        # S4 小道源：股吧/TG/韭研公社（tg channels 默认空=采集器空转不发请求）
        street_cfg = CONFIG["collect"].get("street") or {}
        for name in ("guba", "tg", "jiuyan"):
            if street_cfg.get(name, {}).get("enabled", True):
                tasks.append(asyncio.create_task(self.street_loop(name)))
        # 加密垂直媒体 RSS（20261004：孙宇晨传言覆盖缺口；proxy 位给境外源）
        crypto_cfg = CONFIG["collect"].get("crypto_rss") or {}
        if crypto_cfg.get("enabled", True):
            tasks.append(asyncio.create_task(self.street_loop("crypto_rss")))
        rss_cfg = CONFIG["collect"].get("rss") or {}
        if rss_cfg.get("enabled", True):
            tasks.append(asyncio.create_task(self.rss_loop("rss")))
        # 公众号 RSS（20261023 小作文扩充第三站；消费现成 RSS，不自建抓取。
        # feeds 默认空=零请求，用户配了自建 we-mp-rss/Wechat2RSS/本机 RSSHub 才生效）
        wechat_cfg = CONFIG["collect"].get("wechat_rss") or {}
        if wechat_cfg.get("enabled", True) and (wechat_cfg.get("feeds") or []):
            tasks.append(asyncio.create_task(self.rss_loop("wechat_rss")))
        # 声明式订阅（订阅化阶段一）：subscriptions/*.yaml 丢文件即订阅
        subs_cfg = CONFIG["collect"].get("subscriptions") or {}
        if subs_cfg.get("enabled", True):
            tasks.append(asyncio.create_task(self.subscriptions_loop()))
        pm_cfg = CONFIG["collect"].get("polymarket") or {}
        if pm_cfg.get("enabled", True):
            tasks.append(asyncio.create_task(self.polymarket_loop()))
        tasks.append(asyncio.create_task(self.maintenance_loop()))
        await asyncio.gather(*tasks)
