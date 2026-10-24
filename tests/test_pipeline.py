"""流水线测试：去重水位、同秒多条不丢、backfill 静默、广播上限、告警节流。"""
import asyncio
import time
from datetime import datetime

import pytest

from app.config import CONFIG
from app.models import Item
from app.pipeline import (TRANSLATE_FAILED_MAX, TRANSLATE_FAILED_TTL, Pipeline,
                          needs_translation, norm_id)
from app.store import Store


def mk_raw(ts_str, title):
    return {"source": "cls", "ts": datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S"),
            "title": title, "text": "", "url": ""}


@pytest.fixture
def pipeline(tmp_path):
    return Pipeline(Store(tmp_path / "t.db"))


def test_norm_id_stable_and_whitespace_insensitive():
    a = norm_id("cls", "标题", "正文内容")
    b = norm_id("cls", " 标  题 ", "正 文\n内容")
    assert a == b
    assert norm_id("em", "标题", "正文") != a


def test_norm_id_distinguishes_shared_long_prefix():
    """回归：只取正文前120字时，同前缀不同尾部会被 INSERT OR IGNORE 静默丢弃。"""
    prefix = "同一段很长的新闻正文" * 20
    assert len(prefix) > 120
    a = norm_id("cls", "同一标题", prefix + "结尾甲")
    b = norm_id("cls", "同一标题", prefix + "结尾乙")
    assert a != b


def test_needs_translation_known_and_detected_sources():
    assert needs_translation(Item(id="x", source="bbg_markets", ts=datetime.now(),
                                 title="中文标题", text="", url=""))
    assert needs_translation(Item(id="x", source="custom", ts=datetime.now(),
                                 title="Fed holds rates steady", text="", url=""))
    assert not needs_translation(Item(id="x", source="cls", ts=datetime.now(),
                                     title="央行开展逆回购操作", text="", url=""))


def test_watermark_keeps_same_second_items(pipeline):
    """回归：水位 <= 曾把同秒到达的第3条快讯永久丢弃。"""
    pipeline._seen_last_ts.pop("cls", None)
    asyncio.run(pipeline._ingest("cls", [mk_raw("2026-10-02 10:00:00", "第一条")]))
    assert pipeline.store.stats()["total"] == 1
    asyncio.run(pipeline._ingest("cls", [mk_raw("2026-10-02 10:00:00", "同秒第二条")]))
    assert pipeline.store.stats()["total"] == 2


def test_duplicate_not_reinserted(pipeline):
    asyncio.run(pipeline._ingest("cls", [mk_raw("2026-10-02 10:00:00", "同一条")]))
    asyncio.run(pipeline._ingest("cls", [mk_raw("2026-10-02 10:00:00", "同一条")]))
    assert pipeline.store.stats()["total"] == 1


def test_backfill_silent_then_broadcast(pipeline):
    q = pipeline.subscribe()
    asyncio.run(pipeline._ingest("cls", [mk_raw("2026-10-02 09:00:00", "回填旧数据")]))
    assert q.empty()  # 首轮回填不广播
    asyncio.run(pipeline._ingest("cls", [mk_raw("2026-10-02 10:00:00", "新条目")]))
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert [e["event"] for e in events] == ["item"]
    assert events[0]["data"]["backfill"] is False


def test_restart_loads_persisted_watermark(tmp_path):
    """回归：重启后首个真实批次不能又被当成 backfill 而静默吞掉告警。"""
    store = Store(tmp_path / "t.db")
    first = Pipeline(store)
    asyncio.run(first._ingest("cls", [mk_raw("2026-10-02 09:00:00", "首轮")]))
    assert store.get_kv("pipeline.watermark:cls") == "2026-10-02 09:00:00"

    restarted = Pipeline(store)
    q = restarted.subscribe()
    asyncio.run(restarted._ingest("cls", [mk_raw("2026-10-02 09:00:01", "重启后")]))
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert [e["event"] for e in events] == ["item"]
    assert events[0]["data"]["backfill"] is False


def test_broadcast_cap(pipeline):
    pipeline.broadcast_cap = 1
    # 首轮回填本身静默，先消耗掉
    asyncio.run(pipeline._ingest("cls", [mk_raw("2026-10-02 09:00:00", "回填")]))
    q = pipeline.subscribe()
    raws = [mk_raw(f"2026-10-02 10:00:0{i}", f"条目{i}") for i in range(3)]
    asyncio.run(pipeline._ingest("cls", raws))
    assert q.qsize() == 1                        # 只广播第一条
    assert pipeline.store.stats()["total"] == 4  # 全部入库


def test_broadcast_cap_exempts_high_score(pipeline):
    """断线恢复大批量时，排位靠后的高分条目不因广播上限丢失。"""
    pipeline.broadcast_cap = 1
    asyncio.run(pipeline._ingest("cls", [mk_raw("2026-10-02 09:00:00", "回填")]))
    q = pipeline.subscribe()
    raws = [mk_raw("2026-10-02 10:00:00", "普通快讯"),
            mk_raw("2026-10-02 10:00:01", "市场传闻央行降准")]  # 高分排第二
    asyncio.run(pipeline._ingest("cls", raws))
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    # 噪音被cap截断只算第一条，但高分条目豁免：两条 item 都到，高分附带 alert
    assert sum(1 for e in events if e["event"] == "item") == 2
    assert sum(1 for e in events if e["event"] == "alert") == 1


async def _drain_ingest(p, *raws, settle=0.15):
    await p._ingest("cls", list(raws))
    await asyncio.sleep(settle)  # 让 spawn 出的后台任务跑完


def test_llm_enrich_runs_without_webhook(pipeline, monkeypatch):
    """回归C1：未配置 webhook 时，分数达标的条目仍要触发 LLM 精筛上卡。"""
    monkeypatch.setitem(CONFIG["alert"], "webhook_url", "")
    pipeline.llm_enabled = True
    enriched = []

    async def fake_enrich(item):
        enriched.append(item.id)
        return None
    pipeline._llm_enrich = fake_enrich
    asyncio.run(_drain_ingest(pipeline, mk_raw("2026-10-02 09:00:00", "回填")))
    asyncio.run(_drain_ingest(pipeline,
                              mk_raw("2026-10-02 10:00:00", "市场传闻央行降准")))
    assert len(enriched) == 1


def test_llm_verdict_gates_webhook(pipeline, monkeypatch):
    monkeypatch.setitem(CONFIG["alert"], "webhook_url", "http://x")
    pipeline.llm_enabled = True
    pipeline.llm_min_score = 55
    sent = []

    async def fake_webhook(item):
        sent.append(item.id)
    pipeline._webhook = fake_webhook

    def verdict(is_rumor, conf):
        async def f(item):
            return {"is_rumor": is_rumor, "confidence": conf}
        return f

    asyncio.run(_drain_ingest(pipeline, mk_raw("2026-10-02 09:00:00", "回填")))
    # 高置信"非传闻"→拦截
    pipeline._llm_enrich = verdict(False, 0.9)
    asyncio.run(_drain_ingest(pipeline,
                              mk_raw("2026-10-02 10:00:00", "市场传闻贵州茅台将提价")))
    assert sent == []
    # 低置信或判为传闻→放行
    pipeline._llm_enrich = verdict(False, 0.5)
    asyncio.run(_drain_ingest(pipeline,
                              mk_raw("2026-10-02 10:01:00", "市场传闻贵州茅台再度提价")))
    assert len(sent) == 1


def test_webhook_candidate_rules(pipeline, monkeypatch):
    monkeypatch.setitem(CONFIG["alert"], "webhook_url", "http://x")
    item = Item(id="x", source="cls", ts=datetime.now(), title="t", text="t", url="",
                cls="rumor", score=70, watch=True)
    assert pipeline._webhook_candidate(item) is True   # 关注+传闻
    item2 = Item(id="y", source="cls", ts=datetime.now(), title="t", text="t", url="",
                 cls="noise", score=70, watch=True)
    assert pipeline._webhook_candidate(item2) is False  # 关注但非传闻/辟谣
    item3 = Item(id="z", source="cls", ts=datetime.now(), title="t", text="t", url="",
                 cls="rumor", score=90, watch=False)
    assert pipeline._webhook_candidate(item3) is True   # 高分
    monkeypatch.setitem(CONFIG["alert"], "webhook_url", "")
    assert pipeline._webhook_candidate(item3) is False  # 未配置 webhook


def test_webhook_throttle(pipeline):
    item = Item(id="x", source="cls", ts=datetime.now(), title="t", text="t", url="",
                cls="rumor", score=90, targets=["贵州茅台"])
    assert pipeline._throttled(item) is False
    assert pipeline._throttled(item) is True   # 同标的10分钟内合并


def test_llm_reaches_cap_skipped_items(pipeline, monkeypatch):
    """回归A1-1：broadcast_cap 挤掉的 55-79 分条目仍要上 LLM 精筛（旧代码 continue 连带跳过）。"""
    monkeypatch.setitem(CONFIG["alert"], "webhook_url", "")
    pipeline.broadcast_cap = 1
    pipeline.llm_enabled = True
    enriched = []

    async def fake_enrich(item):
        enriched.append(item.id)
        return None
    pipeline._llm_enrich = fake_enrich
    asyncio.run(pipeline._ingest("cls", [mk_raw("2026-10-02 09:00:00", "回填")]))
    q = pipeline.subscribe()
    asyncio.run(pipeline._ingest("cls", [
        mk_raw("2026-10-02 10:00:00", "传闻A股"),
        mk_raw("2026-10-02 10:00:01", "传闻债市")]))   # 均为 55-79 档
    assert q.qsize() == 1          # 第二条被 cap 挤掉，不广播
    assert len(enriched) == 2      # 但两条都上精筛


def test_llm_round_cap_takes_top_by_score(pipeline, monkeypatch, caplog):
    """回归A1-1b：爆发轮按分截取 round_cap 条，超额跳过并记日志（防配额烧穿）。"""
    import logging
    monkeypatch.setitem(CONFIG["alert"], "webhook_url", "")
    pipeline.llm_enabled = True
    pipeline.llm_round_cap = 2
    enriched = []

    async def fake_enrich(item):
        enriched.append(item.id)
        return None
    pipeline._llm_enrich = fake_enrich
    asyncio.run(pipeline._ingest("cls", [mk_raw("2026-10-02 09:00:00", "回填")]))
    titles = ["传闻A股", "传闻债市", "市场传闻降准落地"]   # 分数依次 ~77/~77/~95
    with caplog.at_level(logging.INFO, logger="pipeline"):
        asyncio.run(pipeline._ingest("cls", [
            mk_raw(f"2026-10-02 10:00:0{i}", t) for i, t in enumerate(titles)]))
    assert len(enriched) == 2
    assert norm_id("cls", "市场传闻降准落地", "") in enriched   # 最高分必须在场
    assert any("超额" in r.message for r in caplog.records)


def test_llm_round_cap_exempts_webhook_candidates(pipeline, monkeypatch):
    """A1门条件2：webhook 候选（关注+传闻）豁免 round_cap，推送通道不因配额保护丢失。"""
    monkeypatch.setitem(CONFIG["alert"], "webhook_url", "http://x")
    pipeline.llm_enabled = True
    pipeline.llm_round_cap = 1
    enriched = []

    async def fake_enrich(item):
        enriched.append(item.id)
        return None
    pipeline._llm_enrich = fake_enrich
    asyncio.run(pipeline._ingest("cls", [mk_raw("2026-10-02 09:00:00", "回填")]))
    # 候选：关注标的（茅台在默认 watchlist）+传闻；非候选两条 55-79 档
    asyncio.run(pipeline._ingest("cls", [
        mk_raw("2026-10-02 10:00:00", "传闻A股"),
        mk_raw("2026-10-02 10:00:01", "传闻债市"),
        mk_raw("2026-10-02 10:00:02", "市场传闻贵州茅台将提价")]))
    # 候选1条全精筛 + 非候选按分取前1 = 2条
    assert len(enriched) == 2
    assert norm_id("cls", "市场传闻贵州茅台将提价", "") in enriched   # 候选在场
    assert pipeline.scorer.score("市场传闻贵州茅台将提价", "")["watch"] is True  # 前提成立


def test_sse_broadcast_carries_group_fields(tmp_path):
    """R3（规格锁死）：SSE item 广播必含组字段——直播行「N 条同类」徽章的
    唯一数据源，回归即静默失效。"""
    q = asyncio.Queue()
    p = Pipeline(Store(tmp_path / "t.db"))
    p.subscribers.add(q)
    asyncio.run(p._ingest("cls", [mk_raw("2026-10-02 09:00:00", "回填")]))
    asyncio.run(p._ingest("cls", [
        mk_raw("2026-10-02 10:00:00", "美国9月非农就业人数增加2.9万人大幅低于预期")]))
    asyncio.run(p._ingest("cls", [
        mk_raw("2026-10-02 10:01:00",
               "美国9月非农就业人数增加2.9万人大幅低于预期。")]))
    # 第一条广播（新组）
    ev1 = q.get_nowait()
    assert ev1["event"] == "item"
    d1 = ev1["data"]
    assert isinstance(d1.get("group_key"), str) and d1["group_key"]
    assert isinstance(d1.get("group_size"), int) and d1["group_size"] >= 1
    assert isinstance(d1.get("cluster_size"), int) and d1["cluster_size"] >= 1
    # 第二条（同稿转载）组命中：组键一致、组大小 +1
    ev2 = q.get_nowait()
    d2 = ev2["data"]
    assert d2["group_key"] == d1["group_key"]
    assert d2["group_size"] == d1["group_size"] + 1


def mk_foreign(ts_str, title, source="bbg_markets"):
    return {"source": source, "ts": datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S"),
            "title": title, "text": "English summary", "url": "https://example.com"}


async def _drain_source(p, source, *raws, settle=0.15):
    await p._ingest(source, list(raws))
    await asyncio.sleep(settle)


def test_translation_only_for_new_foreign_items(pipeline, monkeypatch):
    pipeline.llm_enabled = True
    seen = []

    async def fake_translate(item):
        seen.append(item.title)

    monkeypatch.setattr(pipeline, "_translate_and_broadcast", fake_translate)
    asyncio.run(_drain_source(pipeline, "bbg_markets",
                              mk_foreign("2026-10-02 09:00:00", "Seed")))
    assert seen == []                       # 首轮 backfill 不翻译
    asyncio.run(_drain_source(pipeline, "bbg_markets",
                              mk_foreign("2026-10-02 10:00:00", "Fresh")))
    assert seen == ["Fresh"]
    asyncio.run(_drain_source(pipeline, "cls", mk_raw("2026-10-02 10:01:00", "中文快讯")))
    assert seen == ["Fresh"]                # 中文新条目不走翻译


def test_translation_stores_and_broadcasts(pipeline, monkeypatch):
    from app import llm
    item = Item(id="trans1", source="bbg_markets", ts=datetime.now(),
                title="Fed holds rates", text="Policy unchanged", url="https://example.com")
    pipeline.store.insert(item)
    q = pipeline.subscribe()

    async def fake_translate(title, text):
        return {"title_zh": "美联储维持利率不变", "summary_zh": "政策未变"}

    monkeypatch.setattr(llm, "translate", fake_translate)
    asyncio.run(pipeline._translate_and_broadcast(item))
    ev = q.get_nowait()
    assert ev["event"] == "translation"
    assert ev["data"]["title_zh"] == "美联储维持利率不变"
    row = pipeline.store.items_by_ids([item.id])[0]
    assert row["translation"]["summary_zh"] == "政策未变"


def test_translation_backlog_consumes_all_with_bounded_workers(pipeline, monkeypatch):
    """F-106：突发条目全量进 backlog，最多 3 个 worker，最终全部消费。"""
    seen = []

    async def fake_translate(item):
        seen.append(item.id)
        return True

    monkeypatch.setattr(pipeline, "_translate_and_broadcast", fake_translate)
    items = [Item(id=f"q{i}", source="bbg_markets", ts=datetime.now(),
                  title=f"Title {i}", text="Body", url="") for i in range(50)]

    async def run():
        pipeline._queue_translations(items)
        tasks = list(pipeline._translate_drains)
        assert len(tasks) <= 3
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert sorted(seen) == sorted(i.id for i in items)
    assert not pipeline._translate_backlog
    for i in range(TRANSLATE_FAILED_MAX + 20):
        pipeline._mark_translate_failed(f"f{i}")
    assert len(pipeline._translate_failed) == TRANSLATE_FAILED_MAX
    pipeline._translate_failed["old"] = time.time() - TRANSLATE_FAILED_TTL - 1
    pipeline._mark_translate_failed("fresh")
    assert "old" not in pipeline._translate_failed


def test_idle_scan_picks_untranslated_foreign_items(pipeline):
    """F-106：重启后靠数据库扫描补回未翻译外网条目；已译/中文条目不再入队。"""
    now = datetime.now()
    pending = Item(id="keep1", source="bbg_markets", ts=now,
                   title="Fed cuts rates", text="Policy easing", url="")
    chinese = Item(id="keep2", source="cls", ts=now,
                   title="中文快讯", text="政策放松", url="")
    done = Item(id="keep3", source="bbg_markets", ts=now,
                title="ECB holds", text="No change", url="")
    for item in (pending, chinese, done):
        pipeline.store.insert(item)
    pipeline.store.set_translation(done.id, {"title_zh": "欧洲央行维持利率"})

    assert [i.id for i in pipeline._untranslated_candidates()] == ["keep1"]
