"""S4 传闻源测试：股吧/TG 解析（离线夹具）、tier 迁移幂等、打分降档、tier 筛选。

纪律：全部用 data/guba_sample.html 离线夹具或内联 HTML，不触网。
"""
import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path

from app.collectors import street
from app.collectors.street import parse_guba_detail, parse_guba_html, parse_tg_html
from app.models import Item
from app.pipeline import Pipeline
from app.scoring import Scorer
from app.store import Store

ROOT = Path(__file__).resolve().parent.parent
# 门7 条件2：夹具入库（data/ 整目录被 gitignore，原全量抓页不入库=新 clone 必红）；
# 此为裁剪版（含断言锚行 A/B 与高阅读行 C，1633 字节），原始全量页仍在 data/ 供比对
GUBA_FIXTURE = ROOT / "tests" / "fixtures" / "guba_sample.html"
# 详情页夹具：探针实页裁剪（20261023），只保留 var post_article JSON 段
GUBA_DETAIL_FIXTURE = ROOT / "tests" / "fixtures" / "guba_detail_sample.html"

WATCH = [{"code": "600519", "name": "贵州茅台"},
         {"code": "300750", "name": "宁德时代"}]
S = Scorer(WATCH)

# 解析锚定日期：夹具 update 均为 10-03 前后，now 固定使年份推断可断言
NOW = datetime(2026, 10, 3, 23, 0)


# ---------- 股吧解析（离线夹具） ----------

def test_parse_guba_rows_and_fields():
    rows = parse_guba_html(GUBA_FIXTURE.read_text(encoding="utf-8"),
                           "600519", min_reads=0, now=NOW)
    assert len(rows) > 0
    first = next(r for r in rows if r["title"] == "主力净流入的秘密：一个指标撕开主力伪装")
    assert first["source"] == "guba"
    assert first["ts"] == datetime(2026, 10, 3, 11, 53)   # update 年份推断=当年
    assert first["text"] == first["title"]                # 列表页无正文，text=标题
    # 协议相对 href 补 https:
    assert first["url"] == ("https://caifuhao.eastmoney.com/"
                            "news/20261003235332851247080")
    # 站内相对 href 补 guba 域名
    inner = next(r for r in rows if r["title"].startswith("我说一下科技发展基础"))
    assert inner["url"] == "https://guba.eastmoney.com/news,600519,1779587077.html"


def test_parse_guba_min_reads_filter():
    html = GUBA_FIXTURE.read_text(encoding="utf-8")
    assert len(parse_guba_html(html, "600519", min_reads=0, now=NOW)) \
         > len(parse_guba_html(html, "600519", min_reads=100, now=NOW))
    kept = parse_guba_html(html, "600519", min_reads=1_000_000, now=NOW)
    assert kept == []          # 阈值抬到天上全过滤
    top = parse_guba_html(html, "600519", min_reads=4000, now=NOW)
    assert len(top) == 1       # 夹具仅一条阅读≥4000（285678）


def test_parse_guba_prefers_article_list_publish_time():
    """12 小时制 update 会误导；article_list 的 post_publish_time 才是精确 24h 时间。"""
    html = '''
    <script>var article_list={"re":[{"post_id":"1785497881",
      "post_publish_time":"2026-10-21 23:21:40"}]};</script>
    <tr class="listitem"><td><div class="read">10</div></td>
    <td><div class="reply">0</div></td>
    <td><div class="title"><a href="//guba.eastmoney.com/news,600519,1785497881.html"
      data-postid="1785497881">晚间帖</a></div></td>
    <td><div class="update">10-21 09:34</div></td></tr>
    '''
    rows = parse_guba_html(html, "600519", min_reads=0, now=NOW)
    assert rows[0]["ts"] == datetime(2026, 10, 21, 23, 21, 40)


def test_parse_guba_midnight_post_never_future():
    """凌晨 00:58 的页面 update 显示 12:58；必须用 JSON 精确值，不能落成未来。"""
    html = '''
    <script>var article_list={"re":[{"post_id":"1785505984",
      "post_publish_time":"2026-10-22 00:58:50"}]};</script>
    <tr class="listitem"><td><div class="read">5</div></td>
    <td><div class="reply">0</div></td>
    <td><div class="title"><a href="//guba.eastmoney.com/news,600519,1785505984.html"
      data-postid="1785505984">凌晨帖</a></div></td>
    <td><div class="update">10-22 12:58</div></td></tr>
    '''
    now = datetime(2026, 10, 22, 9, 12)
    rows = parse_guba_html(html, "600519", min_reads=0, now=now)
    assert rows[0]["ts"] == datetime(2026, 10, 22, 0, 58, 50)
    assert rows[0]["ts"] <= now


def test_parse_guba_cross_year_inference():
    """跨年规则：MM-DD 比今天的月-日大（今年不可能的未来日期）→ 归为去年。"""
    # 夹具从 2027-01-02 视角解析：update 10-03 → 2026 年
    rows = parse_guba_html(GUBA_FIXTURE.read_text(encoding="utf-8"),
                           "600519", min_reads=0, now=datetime(2027, 1, 2))
    assert all(r["ts"].year == 2026 for r in rows)
    # 构造 update=12-31 场景：now 在 1 月 → 去年
    html_1231 = ('<tr class="listitem"><td><div class="read">5</div></td>'
                 '<td><div class="reply">0</div></td>'
                 '<td><div class="title"><a href="//guba.eastmoney.com/x">年终帖</a></div></td>'
                 '<td><div class="update">12-31 23:59</div></td></tr>')
    rows = parse_guba_html(html_1231, "600519", now=datetime(2027, 1, 1, 9, 0))
    assert rows[0]["ts"] == datetime(2026, 12, 31, 23, 59)
    # 同年 12 月视角：12-31 不大于当天月日 → 当年
    rows = parse_guba_html(html_1231, "600519", now=datetime(2026, 12, 31, 8, 0))
    assert rows[0]["ts"].year == 2026


# ---------- 股吧详情补抓（20261023 小作文扩充） ----------

def test_parse_guba_detail_fields():
    d = parse_guba_detail(GUBA_DETAIL_FIXTURE.read_text(encoding="utf-8"))
    assert d["title"] == "主力净流入的秘密：一个指标撕开主力伪装"
    assert d["ts"] == datetime(2026, 10, 3, 23, 53, 32)
    assert d["post_id"] == "1779587178"
    assert d["clicks"] == 3053 and d["comments"] == 9
    assert d["text"].startswith("我炒股这么多年")
    assert "\n" in d["text"]                 # </p> 已转换行


def test_parse_guba_detail_malformed():
    assert parse_guba_detail("<html>无标记</html>") is None
    assert parse_guba_detail("var post_article=不是JSON") is None
    assert parse_guba_detail('var post_article={"post_title":""}') is None


def test_parse_guba_detail_missing_time_dirty_counts_and_truncation():
    """缺 post_publish_time → ts=None（行保留 list ts）；脏计数按 0；正文 4000 截断。"""
    d = parse_guba_detail(
        'var post_article={"post_title":"t","post_click_count":"3,053",'
        '"post_comment_count":null,"post_id":9,'
        '"post_content":"<p>' + "长" * 6000 + "</p>\"}")
    assert d["ts"] is None
    assert d["clicks"] == 0 and d["comments"] == 0
    assert len(d["text"]) == 4000


def test_parse_guba_rows_carry_detail_meta():
    """列表行带 reads/post_id/code 元数据键（详情补抓选候选与拼详情 URL 用）。"""
    rows = parse_guba_html(GUBA_FIXTURE.read_text(encoding="utf-8"),
                           "600519", min_reads=0, now=NOW)
    assert all("reads" in r and "post_id" in r and "code" in r for r in rows)
    top = next(r for r in rows if r["title"].startswith("主力净流入"))
    assert top["post_id"] == "1779587178" and top["code"] == "600519"


class _FakeResp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


class _FakeClient:
    """pages 逐个弹出；None=该次请求抛错（模拟 403/网络失败）。"""
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls: list[str] = []

    async def get(self, url: str):
        self.calls.append(url)
        p = self.pages.pop(0) if self.pages else None
        if p is None:
            raise RuntimeError("http 403")
        return _FakeResp(p)


def _reset_detail_state():
    street._detail_cache.clear()
    street._detail_fail_streak = 0
    street._detail_pause_until = 0.0


def _row(pid: str, title: str, reads: int = 0) -> dict:
    return {"source": "guba", "ts": datetime(2026, 10, 23, 12, 0),
            "title": title, "text": title, "url": f"/news,600519,{pid}.html",
            "reads": reads, "post_id": pid}


DETAIL_HTML = 'var post_article={"post_title":"t","post_content":"<p>正文</p>",' \
              '"post_publish_time":"2026-10-23 11:00:00","post_id":1,' \
              '"post_click_count":5,"post_comment_count":2}'


def test_guba_enrich_applies_detail_and_caches():
    _reset_detail_state()
    rows = [_row("111", "盘后小结", reads=2000)]   # 阅读数达标入候选
    asyncio.run(street._guba_enrich_details(
        _FakeClient([DETAIL_HTML]), rows,
        {"enabled": True, "max_per_round": 4, "min_reads": 1000,
         "rumor_words": ["纪要"]}))
    assert rows[0]["text"] == "正文"
    assert rows[0]["ts"] == datetime(2026, 10, 23, 11, 0)
    # 二轮：缓存命中，零请求直接补全
    rows2 = [_row("111", "盘后小结")]
    asyncio.run(street._guba_enrich_details(_FakeClient([]), rows2, {}))
    assert rows2[0]["text"] == "正文"


def test_guba_enrich_candidate_rules():
    """候选=标题含传闻词 或 阅读数≥min_reads；非候选不发请求。"""
    _reset_detail_state()
    rows = [_row("1", "今天天气不错", reads=0),        # 非候选
            _row("2", "网传专家纪要流出", reads=0),    # 词命中
            _row("3", "盘后小结", reads=2000)]         # 阅读数达标
    client = _FakeClient([DETAIL_HTML, DETAIL_HTML])
    asyncio.run(street._guba_enrich_details(
        client, rows, {"max_per_round": 4, "min_reads": 1000,
                       "rumor_words": ["纪要"]}))
    assert len(client.calls) == 2                 # 只有候选被抓
    assert "1" not in street._detail_cache        # 非候选不留缓存（阅读数还会涨）
    assert rows[0]["text"] == "今天天气不错"       # 非候选保持标题兜底


def test_guba_enrich_budget_cap():
    _reset_detail_state()
    rows = [_row(str(i), f"网传纪要{i}", reads=5000) for i in range(6)]
    client = _FakeClient([DETAIL_HTML] * 10)
    asyncio.run(street._guba_enrich_details(
        client, rows, {"max_per_round": 2, "min_reads": 1000}))
    assert len(client.calls) == 2                 # 每轮上限 2，其余留待下轮


def test_guba_enrich_failure_marks_and_cooldowns():
    """失败分级：首败记 False 下轮可重试一次，二次失败才 None 永久；连续
    3 败进入冷却，后续候选跳过。"""
    _reset_detail_state()
    rows = [_row(str(i), "网传纪要", reads=5000) for i in range(1, 6)]
    client = _FakeClient([None, None, None, DETAIL_HTML])
    asyncio.run(street._guba_enrich_details(
        client, rows, {"max_per_round": 9, "min_reads": 1000, "cooldown_s": 600}))
    assert len(client.calls) == 3                 # 第 4 个因冷却未发
    assert all(street._detail_cache[str(i)] is False for i in (1, 2, 3))  # 首败可重试
    assert street._detail_pause_until > 0
    # 冷却结束后：1 号首败重试一次（再败→None 永久），9 号新帖正常抓
    street._detail_pause_until = 0.0
    rows2 = [_row("1", "网传纪要", reads=5000), _row("9", "网传纪要", reads=5000)]
    client2 = _FakeClient([None, DETAIL_HTML])
    asyncio.run(street._guba_enrich_details(client2, rows2, {"max_per_round": 9,
                                                             "min_reads": 1000}))
    assert [c.split(",")[-1] for c in client2.calls] == ["1.html", "9.html"]
    assert street._detail_cache["1"] is None      # 二次失败=永久放弃
    # 三败后再来一轮：1 号（None）不再撞，9 号缓存成功也不撞
    client3 = _FakeClient([])
    asyncio.run(street._guba_enrich_details(client3, rows2, {"max_per_round": 9,
                                                             "min_reads": 1000}))
    assert client3.calls == []


def test_guba_enrich_disabled_keeps_list_behavior():
    _reset_detail_state()
    rows = [_row("1", "网传纪要", reads=99999)]
    asyncio.run(street._guba_enrich_details(_FakeClient([]), rows,
                                            {"enabled": False}))
    assert rows[0]["text"] == "网传纪要"          # 纯列表模式


# ---------- TG 解析（内联夹具） ----------

TG_HTML = """
<div class="tgme_widget_message_wrap js-widget_message_wrap">
  <div class="tgme_widget_message" data-post="ch/1">
    <div class="tgme_widget_message_text js-message_text" dir="auto">
      快讯：<b>央行</b>开展&gt;4000亿操作<br/>详情见公告</div>
    <div class="tgme_widget_message_footer"><time datetime="2026-10-02T07:58:31+00:00"></time></div>
  </div>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
  <div class="tgme_widget_message" data-post="ch/2">
    <div class="tgme_widget_message_text js-message_text" dir="auto">第二条 &amp; 风险提示</div>
    <time datetime="2026-10-03T12:00:00+08:00"></time>
  </div>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
  <div class="tgme_widget_message" data-post="ch/3">
    <div class="tgme_widget_message_photo_wrap"></div>
    <div class="tgme_widget_message_footer"><time datetime="2026-10-03T13:00:00+00:00"></time></div>
  </div>
</div>
"""


def test_parse_tg_blocks():
    rows = parse_tg_html(TG_HTML, "somechannel")
    assert len(rows) == 2                    # 纯图消息（无 text 块）跳过
    a, b = rows
    assert a["source"] == "tg" and b["source"] == "tg"
    # 去 HTML 标签 + 实体反转义 + <br> 转换行
    assert a["text"] == "快讯：央行开展>4000亿操作\n详情见公告"
    assert b["text"] == "第二条 & 风险提示"
    assert a["title"] == "" and a["url"] == "https://t.me/somechannel"
    # datetime 属性带时区 → 折算本地 naive（与其余来源 ts 口径一致）
    expect = datetime.fromisoformat("2026-10-02T07:58:31+00:00"
                                    ).astimezone().replace(tzinfo=None)
    assert a["ts"] == expect
    assert b["ts"] == datetime.fromisoformat("2026-10-03T12:00:00+08:00"
                                             ).astimezone().replace(tzinfo=None)


def test_parse_tg_skips_malformed():
    html = ('<div class="tgme_widget_message_wrap"><div class="tgme_widget_message">'
            '<time datetime="not-a-date"></time></div></div>'
            '<div class="tgme_widget_message_wrap"><div class="tgme_widget_message">'
            '<div class="tgme_widget_message_text">无时间戳的块</div></div></div>')
    assert parse_tg_html(html, "ch") == []


def test_fetch_tg_empty_channels_no_op():
    """channels 默认空 = 不采集（不发请求）。"""
    from app.collectors.street import fetch_tg
    assert asyncio.run(fetch_tg([])) == []


# ---------- tier 迁移（旧库升级场景） ----------

def test_tier_migration_idempotent(tmp_path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
      CREATE TABLE items(id TEXT PRIMARY KEY, source TEXT, ts TEXT, title TEXT,
        text TEXT, url TEXT, cls TEXT, score INTEGER, keywords TEXT, targets TEXT,
        watch INTEGER, llm TEXT, backfill INTEGER, created TEXT);
    """)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO items VALUES('old1','cls',?, '旧库条目','x',' ',"
                 "'noise',0,'[]','[]',0,NULL,0,?)", (now, now))
    conn.commit()
    conn.close()
    s = Store(db)                            # 旧库打开 → 幂等迁移补 tier 列
    rows = s.query(cls="all")["items"]
    assert rows[0]["tier"] == "authoritative"    # 存量行落默认档
    s.insert(Item(id="s1", source="guba", ts=datetime.now(), title="网传A股",
                  text="网传A股", url="", cls="rumor", score=60, tier="street"))
    s2 = Store(db)                           # 二次打开：列已存在，迁移不炸
    assert s2.stats()["total"] == 2
    assert {r["id"] for r in s2.query(tier="street")["items"]} == {"s1"}


# ---------- 打分降档 ----------

def test_street_downgrade_lower_score_same_cls():
    a = S.score("", "网传A股某龙头将停产", tier="authoritative")
    st = S.score("", "网传A股某龙头将停产", tier="street")
    assert a["cls"] == st["cls"] == "rumor"      # 分类规则不变
    assert 0 < st["score"] < a["score"]
    # 系数语义抽查：基线 55 + 强词 24×0.7=17 → 72（authoritative 为 79）
    assert st["score"] == 72 and a["score"] == 79


def test_street_debunk_exempt():
    """小道源辟谣是真金：辟谣条目不降档（STRONG/WEAK 加成本就不覆盖 debunk）。"""
    a = S.score("辟谣声明", "公司澄清：传闻不实，郑重提示", tier="authoritative")
    st = S.score("辟谣声明", "公司澄清：传闻不实，郑重提示", tier="street")
    assert a["cls"] == st["cls"] == "debunk"
    assert st["score"] == a["score"]


def test_street_market_gate_not_exempt():
    """market 门槛不豁免：street 强词无市场关联仍是 noise 三折，不会翻身入传闻。"""
    a = S.score("", "网传某国际组织将改选", tier="authoritative")
    st = S.score("", "网传某国际组织将改选", tier="street")
    assert a["cls"] == st["cls"] == "noise"
    assert st["score"] < a["score"] < 50


def test_street_linked_keywords_logic_unchanged():
    """「异动关联」关键词与加分逻辑不随降档变化。"""
    base = S.score("网传A股某龙头将停产", "", tier="street")
    linked = S.score("网传A股某龙头将停产", "", tier="street", linked={"九阳股份"})
    assert "异动关联" in linked["keywords"]
    assert linked["score"] == base["score"] + 12


# ---------- store tier 筛选与 pipeline 注入 ----------

def test_store_query_tier(tmp_path):
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    s.insert(Item(id="st1", source="guba", ts=now, title="网传A股", text="网传A股",
                  url="", cls="rumor", score=60, tier="street"))
    s.insert(Item(id="au1", source="cls", ts=now, title="快讯", text="快讯",
                  url="", cls="noise", score=5))
    hits = s.query(tier="street")["items"]
    assert [r["id"] for r in hits] == ["st1"]
    assert hits[0]["tier"] == "street"
    assert {r["tier"] for r in s.query()["items"]} == {"street", "authoritative"}
    assert s.query()["total"] == 2            # 缺省不过滤=既有行为
    assert s.query(tier="authoritative")["total"] == 1


def test_guba_detail_text_change_no_duplicate(tmp_path):
    """P1 回归（审查 20261024）：详情补抓使同帖 text 跨轮变异（标题→全文），
    id 纳入 url 后必须保持稳定——第二轮全文版被 INSERT OR IGNORE 吸收，
    同帖双行/二次广播不再发生。"""
    p = Pipeline(Store(tmp_path / "t.db"))
    ts = datetime(2026, 10, 24, 0, 30)
    base = {"source": "guba", "ts": ts, "title": "网传某龙头专家纪要",
            "url": "https://guba.eastmoney.com/news,600519,123.html",
            "post_id": "123", "reads": 3000, "code": "600519"}
    r1 = dict(base, text="网传某龙头专家纪要")          # 轮1：标题兜底
    r2 = dict(base, text="纪要全文" * 200)              # 轮2：补抓到全文（同 ts 同 url）
    asyncio.run(p._ingest("guba", [r1]))
    asyncio.run(p._ingest("guba", [r2]))
    rows = p.store.query(cls="all")["items"]
    assert len(rows) == 1                               # 双行回归被堵死
    assert len(rows[0]["text"]) == len(r2["text"])      # update-on-enrich：全文升级
    # caifuhao 形态（pid 不在 url 中）同样堵死：稳定键用 pid 直键，不看 url
    base_cf = {"source": "guba", "ts": ts, "title": "网传另一帖",
               "url": "https://caifuhao.eastmoney.com/news/20261024003000123",
               "post_id": "456", "reads": 3000, "code": "600519"}
    asyncio.run(p._ingest("guba", [dict(base_cf, text="网传另一帖")]))
    asyncio.run(p._ingest("guba", [dict(base_cf, text="全文" * 100)]))
    assert len(p.store.query(cls="all")["items"]) == 2  # 每帖恰一行


def test_pipeline_street_tier_and_score(tmp_path):
    """guba/tg 条目入 pipeline 即 tier='street'，且降档分数真实落库。

    title/text 取不同文案（股吧条目 title==text 时 STRONG 词和翻倍会顶到
    26 封顶、降档被封顶吃掉——这是词分降档+封顶不变的既定语义）。
    """
    p = Pipeline(Store(tmp_path / "t.db"))
    raw = {"source": "guba", "ts": datetime.now(),
           "title": "网传A股某龙头将停产", "text": "据称涉及千亿项目",
           "url": "https://guba.eastmoney.com/news,600519,1.html"}
    asyncio.run(p._ingest("guba", [raw]))
    row = p.store.query(cls="all")["items"][0]
    assert row["tier"] == "street"
    assert row["score"] == 78                 # 55+强词24×0.7=17+弱词8×0.7=6
    assert row["score"] < p.scorer.score(
        raw["title"], raw["text"], tier="authoritative")["score"]  # 87
    assert row["source"] == "guba"


def test_server_tier_passthrough(tmp_path):
    from fastapi.testclient import TestClient
    from app.pipeline import Pipeline as P
    from app.server import create_app
    store = Store(tmp_path / "t.db")
    client = TestClient(create_app(store, P(store)))
    now = datetime.now()
    store.insert(Item(id="st1", source="guba", ts=now, title="网传A股",
                      text="网传A股", url="", cls="rumor", score=60,
                      tier="street"))
    store.insert(Item(id="au1", source="cls", ts=now, title="快讯", text="快讯",
                      url="", cls="noise", score=5))
    assert [r["id"] for r in
            client.get("/api/items", params={"tier": "street"}).json()["items"]] == ["st1"]
    assert client.get("/api/items").json()["total"] == 2      # 缺省不过滤


def test_street_never_webhook_candidate(tmp_path):
    """门7 条件1：street 双路径（高分/watch+辟谣）皆非 webhook 候选；正经源对照照旧。"""
    from app.config import CONFIG
    from app.models import Item
    p = Pipeline(Store(tmp_path / "t.db"))
    saved_url = (CONFIG.get("alert") or {}).get("webhook_url")
    CONFIG.setdefault("alert", {})["webhook_url"] = "https://example.test/hook"
    now = datetime.now()
    hi = Item(id="s-hi", source="guba", ts=now, title="市场传闻贵州茅台提价",
              text="市场传闻贵州茅台提价", url="", cls="rumor", score=87,
              targets=["贵州茅台"], watch=True, tier="street")
    wd = Item(id="s-wd", source="guba", ts=now, title="贵州茅台澄清公告",
              text="x", url="", cls="debunk", score=60,
              targets=["贵州茅台"], watch=True, tier="street")
    au = Item(id="a-hi", source="cls", ts=now, title="市场传闻贵州茅台提价",
              text="市场传闻贵州茅台提价", url="", cls="rumor", score=87,
              targets=["贵州茅台"], watch=True)
    try:
        assert p._webhook_candidate(hi) is False   # 高分路径被压
        assert p._webhook_candidate(wd) is False   # watch+辟谣路径被压
        assert p._webhook_candidate(au) is True    # 对照：正经源行为不变
    finally:
        CONFIG.setdefault("alert", {})["webhook_url"] = saved_url


def test_street_alert_broadcast_suppressed(tmp_path):
    """门7 条件1：street 同分不出 alert（蜂鸣）事件、item 广播保留；authoritative 对照出 alert。"""
    p = Pipeline(Store(tmp_path / "t.db"))
    events: list[str] = []
    p._broadcast = lambda event, payload: events.append(event)
    p._first_run["guba"] = False   # _first_run 语义="是首轮"→backfill 静默；置 False=非首轮
    p._first_run["cls"] = False
    asyncio.run(p._ingest("guba", [{"source": "guba", "ts": datetime.now(),
                                    "title": "市场传闻贵州茅台将提价",
                                    "text": "提到贵州茅台",
                                    "url": "https://guba.eastmoney.com/x"}]))
    row = p.store.query(cls="all")["items"][0]
    assert row["score"] >= 80                  # 确认跨过告警线（降档后仍会到线）
    assert "item" in events and "alert" not in events
    events.clear()
    asyncio.run(p._ingest("cls", [{"source": "cls", "ts": datetime.now(),
                                   "title": "市场传闻贵州茅台将涨价",
                                   "text": "提到贵州茅台", "url": "https://x.example/a"}]))
    assert "alert" in events                   # 对照：正经源照旧蜂鸣
