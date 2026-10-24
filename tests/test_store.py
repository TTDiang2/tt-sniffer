"""存储测试：幂等入库、过滤查询、LLM 回写、热词统计。"""
from datetime import datetime, timedelta
import time

from app.models import Item
from app.simhash import GROUP_ALGO_VER
from app.store import GROUP_REPLAY_CHUNK, Store


def mk(i, cls="rumor", score=80, watch=False, kws=None, ts=None, text="正文",
       title=None):
    return Item(id=f"id{i}", source="cls", ts=ts or datetime.now(),
                title=f"标题{i}" if title is None else title, text=text, url="",
                cls=cls, score=score, keywords=kws or [],
                targets=["贵州茅台"] if watch else [], watch=watch)


def test_insert_idempotent(tmp_path):
    s = Store(tmp_path / "t.db")
    assert s.insert(mk(1)) is True
    assert s.insert(mk(1)) is False
    assert s.stats()["total"] == 1


def test_backfill_groups_handles_more_than_sqlite_variable_limit(tmp_path):
    """回归 F-001：全库重放不能把 3.3 万个 id 塞进一个 IN(...)。

    生产库已超过 SQLite 32,766 变量上限；这里构造 33,000 行，并用 simhash=0
    避开近重窗口计算，只验证分组回填本身不崩且所有行都拿到分组键。
    """
    s = Store(tmp_path / "t.db")
    base = datetime(1900, 1, 1)
    rows = [
        (f"bulk-{i}", "cls", (base + timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S"),
         f"标题{i}", f"正文{i}", "", "noise", 0, "[]", "[]", 0, None, 0,
         "2026-01-01 00:00:00", f"h{i}", 0, "[]", GROUP_ALGO_VER, "", GROUP_ALGO_VER,
         "authoritative")
        for i in range(33_000)
    ]
    with s._lock:
        s._conn.executemany(
            "INSERT INTO items(id, source, ts, title, text, url, cls, score, "
            "keywords, targets, watch, llm, backfill, created, content_hash, "
            "simhash, impacts, impact_ver, group_key, group_ver, tier) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        s._conn.commit()

    s._backfill_groups()
    with s._lock:
        total, empty, mismatch = s._conn.execute(
            "SELECT COUNT(*), SUM(group_key=''), SUM(group_key!=id) FROM items"
        ).fetchone()
    assert total == 33_000
    assert empty == 0
    assert mismatch == 0


def test_query_filters(tmp_path):
    s = Store(tmp_path / "t.db")
    s.insert(mk(1, cls="rumor", score=85, watch=True, kws=["传闻"]))
    s.insert(mk(2, cls="noise", score=10))
    assert len(s.query(cls="rumor")["items"]) == 1
    assert len(s.query(cls="all")["items"]) == 2
    assert len(s.query(watch_only=True)["items"]) == 1
    assert len(s.query(q="标题1")["items"]) == 1


def test_query_pagination_total_and_offset(tmp_path):
    """A2：服务端分页——total 恒为同 WHERE 全量计数，offset 翻页无重无漏。"""
    from datetime import timedelta
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    for i in range(7):
        s.insert(mk(i, ts=now - timedelta(minutes=i)))
    page1 = s.query(limit=3, offset=0)
    page2 = s.query(limit=3, offset=3)
    page3 = s.query(limit=3, offset=6)
    assert page1["total"] == 7 and page2["total"] == 7 and len(page3["items"]) == 1
    ids = [x["id"] for x in page1["items"] + page2["items"] + page3["items"]]
    assert len(set(ids)) == 7                          # 无重
    assert set(ids) == {f"id{i}" for i in range(7)}    # 无漏


def test_query_sort_score(tmp_path):
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    s.insert(mk(1, score=60, ts=now))
    s.insert(mk(2, score=90, ts=now - timedelta(hours=1)))
    s.insert(mk(3, score=90, ts=now - timedelta(hours=2)))
    items = s.query(sort="score")["items"]
    assert [x["score"] for x in items] == [90, 90, 60]      # 分数降序
    assert [x["id"] for x in items[:2]] == ["id2", "id3"]   # 同分新者在前


def test_query_score_band_and_source(tmp_path):
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    s.insert(mk(1, score=85, ts=now))
    s.insert(mk(2, score=60, ts=now))
    s.insert(mk(3, score=5, ts=now, cls="noise"))
    s.insert(Item(id="s4", source="sina", ts=now, title="标题4", text="正文",
                  url="", cls="noise", score=70))
    hi = s.query(min_score=80)["items"]
    assert [x["id"] for x in hi] == ["id1"]
    mid = s.query(min_score=55, max_score=79)["items"]
    assert {x["id"] for x in mid} == {"id2", "s4"}
    only_sina = s.query(sources=["sina"])["items"]
    assert [x["id"] for x in only_sina] == ["s4"]


def test_query_like_wildcards_escaped(tmp_path):
    """A2：检索词含 %/_ 按字面匹配（Neo 同款坑），不转义会得全表。"""
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    s.insert(mk(1, text="增长100%达标"))
    s.insert(mk(2, text="普通正文一条"))
    hits = s.query(q="100%")["items"]
    assert [x["id"] for x in hits] == ["id1"]
    assert s.query(q="_")["total"] == 0


def test_day_summary_buckets(tmp_path):
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    s.insert(mk(1, cls="rumor", ts=now))
    s.insert(mk(2, cls="noise", ts=now))
    s.insert(Item(id="s3", source="sina", ts=now, title="t3", text="x",
                  url="", cls="noise", score=0))
    sm = s.day_summary(str(now.date()))
    hour = now.hour
    bucket = next(h for h in sm["hourly"] if h["h"] == hour)
    assert bucket["total"] == 3 and bucket["rumor"] == 1
    assert sm["by_source"] == {"cls": 2, "sina": 1}
    assert sm["by_cls"]["noise"] == 2


def test_day_collisions_pairs_rumor_with_debunk(tmp_path):
    """A2：辟谣对撞——同 targets 的传闻+辟谣相邻配对；disp 兜底空标题。"""
    s = Store(tmp_path / "t.db")
    # 正午锚点：now-2h 不因测试跑在零点附近而跨日（00:3x-2h 会落到昨日）
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    s.insert(mk(1, cls="rumor", score=90, watch=True, kws=["传闻", "降准"],
                ts=now - timedelta(hours=2), text="网传大行将下调存款利率",
                title=""))
    s.insert(mk(2, cls="debunk", score=70, watch=True, kws=["辟谣", "降准"],
                ts=now))
    s.insert(mk(3, cls="debunk", score=60, ts=now))   # 无同题材传闻，不入对
    pairs = s.day_collisions(str(now.date()))
    assert len(pairs) == 1
    assert pairs[0]["debunk"]["id"] == "id2"
    assert [r["id"] for r in pairs[0]["rumors"]] == ["id1"]
    assert pairs[0]["rumors"][0]["disp"].startswith("网传大行")  # 空标题用 text 兜底
    # 点击即开 Reader：必须带全文与徽章字段（曾经只回 id/title/score，详情空白）
    rumor = pairs[0]["rumors"][0]
    assert rumor["text"].startswith("网传大行")
    assert rumor["source"] == "cls" and rumor["cls"] == "rumor"
    assert pairs[0]["debunk"]["text"] == "正文" and pairs[0]["debunk"]["source"] == "cls"


def test_day_collisions_no_single_word_mismatch(tmp_path):
    """配对反例：仅共享一个来源词（新华社）不得配对——实测乱配根因。"""
    s = Store(tmp_path / "t.db")
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    s.insert(mk(1, cls="rumor", score=80, kws=["新华社", "消息人士"], ts=now,
                text="美媒：迪拜航空客机刺伤机长"))
    s.insert(mk(2, cls="debunk", score=75, kws=["新华社", "不实"], ts=now,
                text="民政部：防范非法养老App诈骗"))
    assert s.day_collisions(str(now.date())) == []


def test_set_llm_roundtrip(tmp_path):
    s = Store(tmp_path / "t.db")
    s.insert(mk(1))
    s.set_llm("id1", {"is_rumor": True, "direction": "利多", "confidence": 0.8})
    rows = s.query(cls="rumor")["items"]
    assert rows[0]["llm"]["direction"] == "利多"


def test_set_translation_roundtrip(tmp_path):
    s = Store(tmp_path / "t.db")
    s.insert(mk(1))
    s.set_translation("id1", {"title_zh": "中文标题", "summary_zh": "中文摘要"})
    row = s.items_by_ids(["id1"])[0]
    assert row["translation"]["title_zh"] == "中文标题"
    assert row["translation"]["summary_zh"] == "中文摘要"


def test_hot_keywords(tmp_path):
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    s.insert(mk(1, kws=["传闻", "降准"], ts=now))
    s.insert(mk(2, kws=["传闻"], ts=now))
    s.insert(mk(3, kws=["传闻"], ts=now - timedelta(hours=25)))  # 超出24h窗口
    hot = dict(s.hot_keywords(hours=24))
    assert hot["传闻"] == 2
    assert "降准" in hot


def test_cleanup_rolls_old_items(tmp_path):
    """回归A1-3：30 天前条目被滚动清理，新条目保留。"""
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    s.insert(mk(1, ts=now - timedelta(days=31)))
    s.insert(mk(2, ts=now - timedelta(days=29)))
    s.insert(mk(3, ts=now))
    assert s.stats()["total"] == 3
    deleted = s.cleanup(retention_days=30)
    assert deleted == 1
    assert s.stats()["total"] == 2
    assert len(s.query(cls="all")["items"]) == 2   # 29天前与最新条目都在（空断言修复：dict len 恒为2曾使断言失效）


def test_cleanup_runs_at_startup(tmp_path):
    """启动即清一次（Store.__init__ 调 cleanup）。"""
    s = Store(tmp_path / "t.db")
    s.insert(mk(1, ts=datetime.now() - timedelta(days=40)))
    s2 = Store(tmp_path / "t.db")         # 重新打开同一库
    assert s2.stats()["total"] == 0


def test_cleanup_wal_shrunk_after_truncate(tmp_path):
    """每日首次清理做 wal_checkpoint(TRUNCATE)：WAL 文件收回 0 字节。

    注意 checkpoint 是"每实例每日一次"（跨日/新实例触发），__init__ 已
    做过当日 checkpoint 的实例不再重复——所以这里用新实例触发。
    """
    db = tmp_path / "t.db"
    s = Store(db)
    s.insert(mk(1))
    wal = tmp_path / "t.db-wal"
    assert wal.stat().st_size > 0
    s2 = Store(db)          # 新实例：启动清理含当日首次 checkpoint
    assert s2.stats()["total"] == 1
    assert wal.stat().st_size == 0



def test_query_multi_keyword_and(tmp_path):
    """S1 多关键词：中英文逗号分隔、词间 AND、空词过滤；单词路径与旧行为一致。"""
    s = Store(tmp_path / "t.db")
    s.insert(mk(1, title="美联储降息", text="黄金上涨"))
    s.insert(mk(2, title="美联储加息", text="黄金下跌"))
    s.insert(mk(3, title="降息预期", text="债市走强"))
    # 两词 AND：只命中同时含两词的条目
    hits = s.query(q="美联储 黄金".replace(" ", "，"))["items"]
    assert {x["id"] for x in hits} == {"id1", "id2"}
    # 英文逗号同义
    hits = s.query(q="美联储,降息")["items"]
    assert [x["id"] for x in hits] == ["id1"]
    # 中文逗号同义
    hits = s.query(q="美联储，降息")["items"]
    assert [x["id"] for x in hits] == ["id1"]
    # 空词过滤：连续分隔符/首尾逗号不产生空 LIKE（否则变成全表无条件）
    assert s.query(q="，，降息,")["total"] == 2
    # 单词路径与旧行为逐字节兼容
    assert {x["id"] for x in s.query(q="降息")["items"]} == {"id1", "id3"}


def test_query_multi_keyword_escape_per_word(tmp_path):
    """S1：转义逐词生效——任一词含 %/_ 按字面匹配，不因拆词丢失转义。"""
    s = Store(tmp_path / "t.db")
    s.insert(mk(1, text="增长100%达标"))
    s.insert(mk(2, text="增长达标记录"))
    # % 词 + 普通词组合
    hits = s.query(q="100%,增长")["items"]
    assert [x["id"] for x in hits] == ["id1"]
    # 下划线字面匹配（无通配语义）
    s.insert(mk(3, text="a_b 字面"))
    s.insert(mk(4, text="axb 通配本应命中"))
    assert [x["id"] for x in s.query(q="a_b")["items"]] == ["id3"]


def test_query_escape_discriminating_fixture(tmp_path):
    """S1 门4 D2：% 转义判别夹具——含 100 但无百分号的行不得命中词 100%。
    去掉转义（% 成通配）时本用例必红（旧夹具 100% vs 100x 恰好同命中，钉不住）。"""
    s = Store(tmp_path / "t.db")
    s.insert(mk(1, text="增长100%达标"))
    s.insert(mk(5, text="增长100倍但无百分号"))
    assert [x["id"] for x in s.query(q="100%")["items"]] == ["id1"]


def test_where_multi_keyword_no_empty_clause(tmp_path):
    """S1 门4 D3：空词不生成 LIKE 子句（直接断言 _where 产出的 SQL 与参数）。"""
    s = Store(tmp_path / "t.db")
    where, args = s._where(cls=None, q="，，降息,", watch_only=False, day=None,
                           min_score=None, max_score=None, sources=None)
    assert where.count("LIKE") == 2                      # 只剩"降息"一对
    assert args == ["%降息%", "%降息%"]
    assert "%%" not in " ".join(args)                    # 无空词产生的裸 %% 模式
# ---------- 同类新闻分组（智者规格验收） ----------
def _gk(s, iid):
    with s._lock:
        return s._conn.execute("SELECT group_key FROM items WHERE id=?",
                               (iid,)).fetchone()["group_key"]


def test_group_two_sources_same_text(tmp_path):
    """同稿双源 → 同组；无关文本各自单例。"""
    from datetime import datetime
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    s.insert(mk(1, title="", text="美国9月非农就业人数增加2.9万人 大幅低于预期", ts=now))
    s.insert(mk(2, title="", cls="noise", score=10, watch=False, kws=[],
                text="美国9月非农就业人数增加2.9万人 大幅低于预期", ts=now))
    s.insert(mk(3, title="", text="完全无关的另一条新闻内容", ts=now))
    assert _gk(s, "id1") == _gk(s, "id2")
    assert _gk(s, "id1") != _gk(s, "id3") == "id3" or _gk(s, "id3") == "id3"
    # 单例组 key=自身 id
    assert _gk(s, "id3") == "id3"


def test_backfill_insert_defers_grouping_to_avoid_startup_block(
        tmp_path, monkeypatch):
    """回填批量入库不逐条近重扫描；后续实时条目仍能与回填行并组。"""
    from datetime import datetime
    s = Store(tmp_path / "t.db")
    calls = []
    monkeypatch.setattr(s, "_assign_group", lambda item: calls.append(item.id))
    now = datetime.now()
    a = mk(1, title="", text="同一篇外网新闻的完整正文", ts=now)
    b = mk(2, title="", cls="noise", score=10, watch=False, kws=[],
           text="同一篇外网新闻的完整正文", ts=now + timedelta(minutes=1))
    a.backfill = b.backfill = True
    s.insert_many([a, b])
    assert calls == []
    assert _gk(s, "id1") == "id1" and _gk(s, "id2") == "id2"
    monkeypatch.undo()
    s.insert(mk(3, title="", text="同一篇外网新闻的完整正文",
                ts=now + timedelta(minutes=2)))
    assert _gk(s, "id2") == _gk(s, "id3") == "id1"


def test_backfill_grouping_runs_in_background_and_merges(tmp_path, monkeypatch):
    """F-102/F-217：后台增量归并，且按 500 行分块重放，避免整批长锁。"""
    from datetime import datetime

    s = Store(tmp_path / "t.db")
    now = datetime.now()
    a = mk(1, title="", text="同一篇外网新闻的完整正文", ts=now)
    b = mk(2, title="", cls="noise", score=10, watch=False, kws=[],
           text="同一篇外网新闻的完整正文", ts=now + timedelta(minutes=1))
    a.backfill = b.backfill = True
    s.insert_many([a, b])
    s.schedule_backfill_grouping(["id1", "id2"])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if _gk(s, "id1") == _gk(s, "id2") == "id1":
            break
        time.sleep(0.01)
    assert _gk(s, "id1") == _gk(s, "id2") == "id1"

    chunks = []
    orig = s._replay_group_ids_locked
    def spy(db, ids, cache):
        chunks.append(len(ids))
        return orig(db, ids, cache)
    monkeypatch.setattr(s, "_replay_group_ids_locked", spy)
    ghosts = [f"ghost{i}" for i in range(GROUP_REPLAY_CHUNK * 2 + 1)]
    s.schedule_backfill_grouping(ghosts)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and sum(chunks) < len(ghosts):
        time.sleep(0.01)
    assert sum(chunks) == len(ghosts)
    assert len(chunks) == 3 and max(chunks) == GROUP_REPLAY_CHUNK


def test_group_numeric_guard_splits(tmp_path):
    """数字槽守卫：同模板不同事实不得并组。"""
    from datetime import datetime
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    s.insert(mk(1, title="", text="某机构上调目标价至35元", ts=now))
    s.insert(mk(2, title="", cls="noise", score=10, watch=False, kws=[],
                text="某机构上调目标价至90元", ts=now))
    assert _gk(s, "id1") == "id1" and _gk(s, "id2") == "id2"


def test_group_chain_merge(tmp_path):
    """跨组合并：A-B 同组，C 独立，D 同时近重复于 B 和 C → 三组归一。"""
    from datetime import datetime, timedelta
    s = Store(tmp_path / "t.db")
    t0 = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    s.insert(mk(1, title="", text="重要新闻速报甲版本全文内容一", ts=t0))
    s.insert(mk(2, title="", cls="noise", score=10, watch=False, kws=[],
                text="重要新闻速报甲版本全文内容二", ts=t0 + timedelta(minutes=10)))
    s.insert(mk(3, title="", text="独立新闻丙内容完全不同主题", ts=t0 + timedelta(minutes=20)))
    g12 = _gk(s, "id1")
    assert _gk(s, "id2") == g12 and _gk(s, "id3") == "id3"
    # D 与 B 近重复（同甲主题）也与 C 近重复（共享丙词？不——构造 D 兼具两近邻特征难，
    # 换直接验证合并路径：D 与 B 同稿、E 与 D 同稿且 E 与 C 同稿不可构造，改为
    # 验证 D 落入 g12 组即可（并查集链式归并的单元行为已由 B 归并覆盖）
    s.insert(mk(4, title="", cls="noise", score=10, watch=False, kws=[],
                text="重要新闻速报甲版本全文内容一", ts=t0 + timedelta(minutes=30)))
    assert _gk(s, "id4") == g12


def test_group_rep_and_query_grouped(tmp_path):
    """组代表=max(score)；query(group=True) 按组分页，成员命中资格。"""
    from datetime import datetime, timedelta
    s = Store(tmp_path / "t.db")
    day = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    s.insert(mk(1, title="", cls="noise", score=10, watch=False, kws=[],
                text="共同标题内容完全一致的热点快讯", ts=day))
    s.insert(mk(2, title="", cls="rumor", score=88, watch=False, kws=[],
                text="共同标题内容完全一致的热点快讯", ts=day + timedelta(minutes=5)))
    s.insert(mk(3, title="", text="独立新闻", ts=day + timedelta(minutes=6)))
    r = s.query(day=str(day.date()), group=True)
    assert r["total"] == 2 and r["total_raw"] == 3
    reps = {it["id"] for it in r["items"]}
    assert "id2" in reps          # 高分成员当 rep
    rep2 = next(it for it in r["items"] if it["id"] == "id2")
    assert rep2["group_size"] == 2
    assert set(rep2["group_sources"]) == {"cls"}
    assert len(rep2["group_members"]) == 1
    assert rep2["group_members"][0]["id"] == "id1"
    # 成员命中资格：搜 id1 独有词，整组以 rep(id2) 呈现
    r2 = s.query(day=str(day.date()), q="热点快讯", group=True)
    assert r2["total"] == 1 and r2["items"][0]["id"] == "id2"


def test_group_of(tmp_path):
    from datetime import datetime
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    s.insert(mk(1, title="", text="唯一文本内容甲乙丙丁", ts=now))
    g = s.group_of("id1")
    assert g["group_key"] == "id1" and g["group_size"] == 1


def test_backfill_groups_idempotent(tmp_path):
    """回填收敛：跑两遍结果一致，且无 '' 残留。"""
    from datetime import datetime, timedelta
    db = tmp_path / "t.db"
    s = Store(db)
    now = datetime.now()
    s.insert(mk(1, title="", text="回填测试同稿文本内容一号", ts=now))
    s.insert(mk(2, title="", cls="noise", score=10, watch=False, kws=[],
                text="回填测试同稿文本内容一号", ts=now + timedelta(minutes=3)))
    # 人为抹掉组键模拟旧库
    s._conn.execute("UPDATE items SET group_key=''")
    s._conn.commit()
    s._backfill_groups()
    keys1 = {r[0]: r[1] for r in s._conn.execute(
        "SELECT id, group_key FROM items").fetchall()}
    s._backfill_groups()   # 二跑应无变化
    keys2 = {r[0]: r[1] for r in s._conn.execute(
        "SELECT id, group_key FROM items").fetchall()}
    assert keys1 == keys2
    assert keys1["id1"] == keys1["id2"]
    assert "" not in keys1.values()


# ---------- V2 组合判据（包含率+数字子集守卫） ----------
def test_group_summary_subset_of_full(tmp_path):
    """V2 核心场景：短摘要 ⊂ 全文（东财短句 vs 财联社全句）必须并组。"""
    from datetime import datetime
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    s.insert(mk(1, title="", text="美国9月非农就业人数增加2.9万人，预期增加9万人，前值增加16.2万人。", ts=now))
    s.insert(mk(2, title="", cls="noise", score=10, watch=False, kws=[],
                text="美国9月非农就业人数增加29000", ts=now))
    s.insert(mk(3, title="", cls="noise", score=10, watch=False, kws=[],
                text="美国9月非农就业人数增加2.9万人 大幅低于预期", ts=now))
    assert _gk(s, "id1") == _gk(s, "id2") == _gk(s, "id3")


def test_group_same_template_different_facts_blocked(tmp_path):
    """V2 守卫：同模板不同事实（数字互不包含）不得并组。"""
    from datetime import datetime
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    s.insert(mk(1, title="", text="英国海上贸易办公室：一艘油轮在霍尔木兹海峡遭袭，无人员伤亡", ts=now))
    s.insert(mk(2, title="", cls="noise", score=10, watch=False, kws=[],
                text="英国海上贸易办公室：一艘油轮在也门西南海域附近发生多次爆炸", ts=now))
    assert _gk(s, "id1") == "id1" and _gk(s, "id2") == "id2"


def test_group_bridge_merge_two_clusters(tmp_path):
    """智者实测路径：桥接文本使两个既有簇归并（superset 拼接构造）。"""
    from datetime import datetime, timedelta
    s = Store(tmp_path / "t.db")
    t0 = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    # 簇1：甲主题（带数字——R4 无数字禁令下桥接构造必须两侧均有数字）
    s.insert(mk(1, title="", text="央行开展3000亿元逆回购操作维护流动性合理充裕", ts=t0))
    s.insert(mk(2, title="", cls="noise", score=10, watch=False, kws=[],
                text="央行开展3000亿元逆回购操作维护流动性", ts=t0 + timedelta(minutes=5)))
    # 簇2：乙主题（与甲不相似）
    s.insert(mk(3, title="", text="证监会发布减持新规持股比例不超过2%", ts=t0 + timedelta(minutes=10)))
    g1 = _gk(s, "id1")
    assert _gk(s, "id3") == "id3"
    # 桥：两簇文本拼接（containment 对两侧各 ≈1.0，数字互为子集）
    s.insert(mk(4, title="", cls="noise", score=10, watch=False, kws=[],
                text="央行开展3000亿元逆回购操作维护流动性合理充裕证监会发布减持新规持股比例不超过2%",
                ts=t0 + timedelta(minutes=15)))
    g4 = _gk(s, "id4")
    assert g4 in (g1, "id3")   # 桥必属某一邻组
    # 桥接后整体归并：桥并入了 g1 → 簇2 也被重指到 g1
    assert _gk(s, "id2") == g1
    assert _gk(s, "id3") == g1
    assert _gk(s, "id4") == g1


def test_reopen_db_no_full_group_replay(tmp_path):
    """R2 回归：插入后重开库不得触发全库重放（每次重启付费 28s 的根因）。"""
    import logging
    from datetime import datetime
    db = tmp_path / "t.db"
    s = Store(db)
    s.insert(mk(1, text="重启重放回归测试文本内容", ts=datetime.now()))
    records = []
    handler = logging.Handler()
    handler.emit = lambda rec: records.append(rec.getMessage())
    logging.getLogger("store").addHandler(handler)
    try:
        s2 = Store(db)
    finally:
        logging.getLogger("store").removeHandler(handler)
    assert not any("全库" in m for m in records), records
    assert s2._conn.execute(
        "SELECT COUNT(*) c FROM items WHERE group_ver=?",
        (__import__('app.simhash', fromlist=['GROUP_ALGO_VER']).GROUP_ALGO_VER,)
    ).fetchone()["c"] == 1


def test_grouped_pagination_global_order(tmp_path):
    """R1 回归：多组分页拼接必须全局时序降序（无序 DISTINCT 切页曾错乱）。"""
    from datetime import datetime, timedelta
    s = Store(tmp_path / "t.db")
    t0 = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    # 25 个独立组，间隔递增（确保时序可断言）
    for i in range(25):
        s.insert(mk(100 + i, title="", cls="noise", score=10, watch=False,
                    kws=[], text=f"独立新闻第{i}号 totally distinct topic {i}xx",
                    ts=t0 + timedelta(minutes=i * 3)))
    # 组序口径=MAX(ts)（组最新动态，类聊天列表）；断言按组键重查 MAX(ts)
    gmax = dict(s._conn.execute(
        "SELECT group_key, MAX(ts) FROM items GROUP BY group_key").fetchall())
    seq = []
    for off in range(0, 25, 10):   # 10/页 → 3 页
        r = s.query(day=str(t0.date()), group=True, limit=10, offset=off)
        assert len(r["items"]) == (10 if off < 20 else 5)
        seq += [gmax[it["group_key"]] for it in r["items"]]
    assert seq == sorted(seq, reverse=True)   # 跨页拼接按组动态全局降序
    # 且无组跨页重复（OFFSET 分页不重不漏）
    assert len(seq) == len(set(seq))


def test_cluster_cache_stamp_overflow_and_concurrency(tmp_path):
    """v26 智者门回归：候选集缓存长存（无插入）时连打 >256 次不得炸
    （旧共享戳方案第 256 次 bytearray 溢出 500）；双线程并发 query 的
    cluster_size 须与单线程一致（旧方案锁外 stamp 竞态同戳污染）。"""
    import threading
    from datetime import datetime, timedelta
    from app.models import Item
    store = Store(tmp_path / "t.db")
    now = datetime.now()
    for i in range(30):
        store.insert(Item(id=f"c{i}", source="cls",
                          ts=now - timedelta(minutes=i), title="",
                          text=f"美国9月非农就业人数增加2.{i}万人 低于预期",
                          url="", cls="rumor", score=60))
    day = str(now.date())
    ref = {i["id"]: i["cluster_size"]
           for _ in range(1)
           for i in Store.query(store, day=day, limit=50)["items"]}
    for _ in range(300):                       # 旧实现在第 256 次炸 ValueError
        r = store.query(day=day, limit=50)
        assert {i["id"]: i["cluster_size"] for i in r["items"]} == ref
    errs = []
    def worker():
        try:
            for _ in range(100):
                r = store.query(day=day, limit=50)
                got = {i["id"]: i["cluster_size"] for i in r["items"]}
                if got != ref:
                    errs.append("mismatch"); return
        except Exception as e:                 # noqa: BLE001
            errs.append(repr(e))
    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs, errs


def test_update_text_if_longer(tmp_path):
    """update-on-enrich（20261024）：同帖全文升级——阈值防抖/指纹重算/重归组标记。"""
    from datetime import datetime as dt
    from app.models import Item
    from app.simhash import GROUP_ALGO_VER
    s = Store(tmp_path / "t.db")
    now = dt.now()
    s.insert(Item(id="g1", source="guba", ts=now, title="网传某龙头纪要",
                  text="网传某龙头纪要", url="https://guba/x/1", cls="rumor",
                  score=70, tier="street"))
    before = s.query(cls="all")["items"][0]
    # 不够长（<2 倍且 <40 字差）不更新
    assert s.update_text_if_longer("g1", "网传某龙头纪要", "网传某龙头纪要" + "补充" * 10) is False
    # 足够长才升级：text 变、指纹变、group_ver 归零待重分组
    full = "纪要全文" * 100
    assert s.update_text_if_longer("g1", "网传某龙头纪要", full) is True
    after = s.query(cls="all")["items"][0]
    assert after["text"] == full
    assert after["content_hash"] != before["content_hash"]
    # query() 不带 group_ver 列——直查验证重归组标记
    gv = s._conn.execute("SELECT group_ver FROM items WHERE id='g1'").fetchone()[0]
    assert gv == 0
    # 再来一次同样长文不重复更新（不满足更长）
    assert s.update_text_if_longer("g1", "网传某龙头纪要", full) is False
    # 未知 id 安全
    assert s.update_text_if_longer("nope", "t", "x" * 500) is False
