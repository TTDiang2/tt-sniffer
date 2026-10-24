"""韭研公社采集器测试（20261023 小作文扩充第二站）：载荷解析、过滤、详情、tier。

纪律：全部用 tests/fixtures/jiuyan_*.html 离线夹具或内联 HTML，不触网。
列表夹具时间锚定 2026-10-23（实页裁剪），now 固定传入——夹具永不随真实日期过期。
"""
import asyncio
from datetime import datetime
from pathlib import Path

from app.collectors import jiuyan
from app.collectors.jiuyan import (_js_value, parse_jiuyan_article,
                                   parse_jiuyan_square)

ROOT = Path(__file__).resolve().parent.parent
SQUARE_FIXTURE = ROOT / "tests" / "fixtures" / "jiuyan_square_sample.html"
ARTICLE_FIXTURE = ROOT / "tests" / "fixtures" / "jiuyan_article_sample.html"
NOW = datetime(2026, 10, 23, 20, 0)


# ---------- __NUXT__ 载荷 JS 值解析 ----------

def test_js_value_scalars_and_escapes():
    v, _ = _js_value('"a\\u003Cb\\n\\"q\\\\"', 0)
    assert v == 'a<b\n"q\\'
    assert _js_value("[1,2.5,-3,true,false,null,xyz]", 0)[0] == \
        [1, 2.5, -3, True, False]        # null/混淆标识符 → 丢弃
    # 前导点小数（integral:.57073789——整数部分为 0 时服务端这么序列化，
    # 生产 20261024 早间实页踩过：不认会整页解析失败收 0 条）
    assert _js_value("[.5,.25,1.,2e3]", 0)[0] == [0.5, 0.25, 1.0, 2000.0]
    obj, _ = _js_value('{a:"x",b:c,arr:[{k:"v"}]}', 0)
    assert obj == {"a": "x", "arr": [{"k": "v"}]}   # b 引用不可解 → 键丢弃


def test_js_value_malformed_raises():
    for bad in ('"unterminated', "{a:", "[1,", "{a:1,", "{a:1 "):
        try:
            _js_value(bad, 0)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应抛 ValueError: {bad}")


def test_js_string_surrogate_pairs():
    """代理对按 JS 语义合并；孤立代理项 sanitize 成 U+FFFD（防 encode 崩杀轮）。"""
    v, _ = _js_value('"\\ud83d\\ude80\\u53d1\\u5c04"', 0)
    assert v == "🚀发射"
    # 高代理后不接 \u 转义（孤高）；孤低；合法对边界（D800+DFFF）
    v2, _ = _js_value('"\\ud83d孤\\ud800x"', 0)
    assert v2 == "\ufffd孤\ufffdx"
    v3, _ = _js_value('"\\udfff"', 0)
    assert v3 == "\ufffd"
    v4, _ = _js_value('"\\ud800\\udfff"', 0)
    assert v4 == chr(0x103FF)


def test_parse_square_deep_nesting_recursion_safe():
    """异常深嵌套触发 RecursionError 时按解析失败容错（返回空，不炸进程）。"""
    html = "__NUXT__=...searchData:{...list:" + "[" * 30000
    assert parse_jiuyan_square(html, now=NOW) == []


# ---------- 广场列表解析 ----------

def test_parse_square_rows_fields_and_filter():
    rows = parse_jiuyan_square(SQUARE_FIXTURE.read_text(encoding="utf-8"),
                               max_age_days=2, now=NOW)
    assert len(rows) == 24                       # 30 项 - 置顶旧帖（2023/2025）
    first = rows[0]
    assert first["source"] == "jiuyan"
    assert first["ts"] == datetime(2026, 10, 23, 19, 41, 1)
    assert first["title"] == "下周请别上头！"
    assert first["url"] == "https://www.jiuyangongshe.com/a/" + first["article_id"]
    assert first["preview_len"] == 250           # 预览被 250 字截断（补详情的动机）
    # 置顶旧帖被 max_age 过滤（create_time 2025/2023 的两条）
    assert all(r["title"] != "韭研公社六大工具助力A股研究" for r in rows)
    # 无预览条目（content 字段是混淆引用）：text 退回标题
    miss = next(r for r in rows if r["preview_len"] == 0)
    assert miss["text"] == miss["title"]


def test_parse_square_age_and_future_filter():
    """max_age_days 截断首轮回灌；未来时间（脏数据）丢弃。"""
    html = ('__NUXT__=(function(a){return {data:[{searchData:{},list:['
            '{article_id:"f1",title:"未来帖",create_time:"2026-10-23 21:00:00"},'
            '{article_id:"o1",title:"旧帖",create_time:"2026-10-10 00:00:00"},'
            '{article_id:"n1",title:"新帖",create_time:"2026-10-23 18:00:00"}'
            ']}}]})(1);')
    rows = parse_jiuyan_square(html, max_age_days=2, now=NOW)
    assert [r["article_id"] for r in rows] == ["n1"]


def test_parse_square_malformed_tolerated():
    assert parse_jiuyan_square("无载荷", now=NOW) == []
    assert parse_jiuyan_square("__NUXT__=(bad", now=NOW) == []


# ---------- 文章页解析 ----------

def test_parse_article_fields():
    d = parse_jiuyan_article(ARTICLE_FIXTURE.read_text(encoding="utf-8"))
    assert d["title"] == "新主线！物理AI！谁是核心？"
    assert d["ts"] == datetime(2026, 10, 23, 15, 56, 25)
    assert d["text"].startswith("直接说答案")
    assert "机器人不缺身体，缺的是教材" in d["text"]
    assert "\n" in d["text"]                     # 段落换行保留


def test_parse_article_skips_non_html_content_hits():
    """content:" 命中评论区等纯文本时跳过，只认 HTML 文档形态的正文。"""
    html = ('<div class="fs28-bold mb28">标题</div>'
            '<div class="date fs14">2026-10-23 10:00:00 广东</div>'
            '__NUXT__=...content:"纯文本评论"...content:"<html><body>'
            '<p>真正文第一段</p><p>第二段</p></body></html>"...')
    d = parse_jiuyan_article(html)
    assert d["text"].startswith("真正文第一段")
    assert "纯文本评论" not in d["text"]


def test_parse_article_title_fallbacks():
    """fs28-bold 缺失退 <title>（去站名后缀）；全空返回 None。"""
    d = parse_jiuyan_article("<title>备用标题-韭研公社</title>")
    assert d["title"] == "备用标题" and d["text"] == ""
    assert parse_jiuyan_article("<html></html>") is None


def test_parse_article_all_content_non_html():
    """5 处 content 命中全非 HTML（纯文本/损坏）→ text 空退标题（不返回 None）。"""
    html = ('<div class="fs28-bold mb28">只有标题</div>'
            '__NUXT__=...content:"纯文本一"...content:"纯文本二"...content:"纯文本三"'
            '...content:"纯文本四"...content:"纯文本五"...content:"纯文本六"...')
    d = parse_jiuyan_article(html)
    assert d is not None and d["title"] == "只有标题" and d["text"] == ""


# ---------- 详情补抓纪律（与 guba 同款） ----------

class _FakeResp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls: list[str] = []

    async def get(self, url: str):
        self.calls.append(url)
        p = self.pages.pop(0) if self.pages else None
        if p is None:
            raise RuntimeError("http error")
        return _FakeResp(p)


def _reset():
    jiuyan._detail_cache.clear()
    jiuyan._detail_fail_streak = 0
    jiuyan._detail_pause_until = 0.0


def _row(aid, title, preview_len):
    return {"source": "jiuyan", "ts": datetime(2026, 10, 23, 12, 0),
            "title": title, "text": "x" * preview_len or title,
            "url": f"https://www.jiuyangongshe.com/a/{aid}",
            "article_id": aid, "preview_len": preview_len}


ARTICLE_OK = ('<div class="fs28-bold mb28">标题</div>'
              '<div class="date fs14">2026-10-23 11:00:00 广东</div>'
              '__NUXT__=...content:"<html><body><p>全文正文</p></body></html>"...')


def test_jiuyan_enrich_candidates_and_budget():
    """候选=预览过短 或 标题含传闻词；上限截断；成功后缓存。"""
    _reset()
    rows = [_row("a", "长预览普通帖", 250),          # 非候选
            _row("b", "长预览但标题带纪要", 250),    # 词命中
            _row("c", "短预览帖", 10)]               # 预览过短
    client = _FakeClient([ARTICLE_OK, ARTICLE_OK, ARTICLE_OK])
    asyncio.run(jiuyan._jiuyan_enrich_details(
        client, rows, {"max_per_round": 2, "min_len": 60,
                       "rumor_words": ["纪要"]}))
    assert len(client.calls) == 2                  # 上限 2：b、c 入选，c 被截
    assert rows[0]["text"].startswith("xxx")       # 非候选不动
    assert rows[1]["text"] == "全文正文"
    assert rows[1]["ts"] == datetime(2026, 10, 23, 11, 0)
    # 缓存：二轮零请求
    rows2 = [_row("b", "长预览但标题带纪要", 250)]
    asyncio.run(jiuyan._jiuyan_enrich_details(_FakeClient([]), rows2, {}))
    assert rows2[0]["text"] == "全文正文"


def test_jiuyan_enrich_failure_cooldown_and_no_retry():
    _reset()
    rows = [_row(str(i), "短预览", 5) for i in (1, 2, 3, 4)]
    client = _FakeClient([None, None, None, ARTICLE_OK])
    asyncio.run(jiuyan._jiuyan_enrich_details(
        client, rows, {"max_per_round": 9, "min_len": 60, "cooldown_s": 600}))
    assert len(client.calls) == 3                  # 连续 3 败 → 冷却，第 4 不发
    assert all(jiuyan._detail_cache[str(i)] is False for i in (1, 2, 3))  # 首败可重试
    assert jiuyan._detail_pause_until > 0
    jiuyan._detail_pause_until = 0.0
    rows2 = [_row("1", "短预览", 5), _row("9", "短预览", 5)]
    client2 = _FakeClient([None, ARTICLE_OK])
    asyncio.run(jiuyan._jiuyan_enrich_details(client2, rows2,
                                              {"max_per_round": 9, "min_len": 60}))
    # 1 号重试一次再败→None 永久；9 号新帖成功
    assert [c.rsplit("/", 1)[-1] for c in client2.calls] == ["1", "9"]
    assert jiuyan._detail_cache["1"] is None


# ---------- pipeline 接线 ----------

def test_jiuyan_enrich_disabled_no_request():
    _reset()
    rows = [_row("a", "短预览", 5)]
    fetched = asyncio.run(jiuyan._jiuyan_enrich_details(
        _FakeClient([]), rows, {"enabled": False, "min_len": 60}))
    assert fetched == 0 and rows[0]["text"].startswith("xxx")   # 纯列表模式


def test_pipeline_jiuyan_tier_street(tmp_path):
    """jiuyan 条目入 pipeline 即 tier='street'（⚠️标+不打扰通道语义同 guba）。"""
    from app.pipeline import Pipeline as P
    from app.store import Store
    p = P(Store(tmp_path / "t.db"))
    asyncio.run(p._ingest("jiuyan", [{
        "source": "jiuyan", "ts": datetime.now(),
        "title": "网传某龙头专家纪要", "text": "纪要全文提到扩产",
        "url": "https://www.jiuyangongshe.com/a/x"}]))
    row = p.store.query(cls="all")["items"][0]
    assert row["tier"] == "street" and row["source"] == "jiuyan"
