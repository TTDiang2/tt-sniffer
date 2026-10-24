"""V0.2 新增能力测试：市场关联门槛、按天查询、日历索引、推送格式、wscn 解析。"""
import asyncio
from datetime import datetime, timedelta

from app.collectors.news import parse_wscn_items
from app.config import CONFIG
from app.models import Item
from app.pipeline import Pipeline
from app.push import build_payload
from app.scoring import Scorer
from app.store import Store

S = Scorer([])


# ---------- 市场关联门槛 ----------
def test_market_gate_blocks_international_noise():
    """命中强传闻词但与市场无关的国际新闻：不入传闻流、分数三折封顶45。"""
    v = S.score("", "消息人士称，阿联酋撤回对某国航班的降落授权")
    assert v["cls"] == "noise"
    assert v["score"] <= 45


def test_market_gate_keeps_financial_rumor():
    v = S.score("", "市场传闻某创业板公司将被借壳，股价拉升")
    assert v["cls"] == "rumor"
    assert v["score"] >= 55


def test_debunk_exempt_from_market_gate():
    v = S.score("", "辟谣：网传某国际组织解散的消息不实")
    assert v["cls"] == "debunk"


def test_market_gate_not_punched_by_numbers():
    """回归M1：任意 6 位数字（金额/人数）不得充当市场关联打穿门槛。"""
    v = S.score("", "网传该地紧急转移300000人，知情人士称情况严峻")
    assert v["cls"] == "noise"
    assert v["score"] <= 45
    # 命中已知标的全集里的代码才开门
    v2 = S.score("", "网传300000大单砸盘，知情人士称情况严峻",
                 known_codes={"300000"})
    assert v2["cls"] == "rumor"


def test_unlinked_debunk_capped_below_alert():
    """回归M2：与市场无关的辟谣（地缘类）压到告警线下。"""
    v = S.score("", "伊朗方面辟谣称有关核设施的消息系不实谣言")
    assert v["cls"] == "debunk"
    assert v["score"] < 80
    # 关联市场的辟谣不受影响
    v2 = S.score("", "贵州茅台辟谣：网传提价消息不实，股价应声回落", known_codes={"600519"})
    assert v2["cls"] == "debunk"
    assert v2["score"] >= 80


def test_official_minutes_not_rumor():
    """回归M3：官方会议纪要发布稿不得因'纪要'字样被判传闻满分。"""
    v = S.score("", "央行公布货币政策会议纪要：会议纪要显示流动性合理充裕，逆回购操作利率不变")
    assert v["cls"] == "official"
    assert v["score"] < 80
    # 卖方泄露类纪要仍是强传闻信号
    v2 = S.score("", "券商纪要显示某公司订单超预期，股价拉升")
    assert v2["cls"] == "rumor"


def test_market_terms_bond_side():
    """债市传闻常无代码，靠市场词表放行。"""
    v = S.score("", "传闻称央行将加大逆回购投放，资金面转松")
    assert v["cls"] == "rumor"


# ---------- 按天查询与日历索引 ----------
def _seed_days(store: Store):
    # 门4 移交项（20261004）：锚定当日正午——原先裸 now() 在 00:00–01:00 之间
    # 会把"今天"种子（today - 0天 - i分钟）滚进昨天，跨零点跑 check.py 必红。
    today = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    rows = [
        (f"t{day}_{cls}_{i}", today - timedelta(days=day, minutes=i), cls)
        for day, n, cls in [(0, 3, "rumor"), (0, 1, "noise"), (1, 2, "debunk"),
                            (3, 1, "official")]
        for i in range(n)
    ]
    for iid, ts, cls in rows:
        store.insert(Item(id=iid, source="cls", ts=ts, title=f"标题{iid}",
                          text="正文", url="", cls=cls, score=60,
                          keywords=["传闻"] if cls == "rumor" else []))


def test_query_by_day(tmp_path):
    s = Store(tmp_path / "t.db")
    _seed_days(s)
    today = str(datetime.now().date())
    yesterday = str((datetime.now() - timedelta(days=1)).date())
    assert len(s.query(day=today)["items"]) == 4
    assert len(s.query(day=yesterday)["items"]) == 2
    assert len(s.query(day=today, cls="rumor")["items"]) == 3
    assert s.query()["total"] == 7  # 不传 day 取全部


def test_day_index_and_overview(tmp_path):
    s = Store(tmp_path / "t.db")
    _seed_days(s)
    idx = s.day_index(days=5)   # 返回按时间正序：idx[-1] 为今天
    assert len(idx) == 5
    assert idx[-1]["rumor"] == 3         # 今天
    assert idx[-2]["debunk"] == 2        # 昨天
    assert idx[0]["total"] == 0          # 无数据日期补零
    ov = s.day_overview(str(datetime.now().date()))
    assert ov["counts"]["rumor"] == 3
    assert ("传闻", 3) in ov["keywords"]


# ---------- 推送格式 ----------
def _mk_item():
    return Item(id="x", source="cls", ts=datetime(2026, 10, 2, 10, 0, 0),
                title="茅台提价传闻", text="市场传闻贵州茅台将上调出厂价",
                url="https://example.com/a", cls="rumor", score=92,
                keywords=["市场传闻", "贵州茅台"])


def test_push_wecom_markdown():
    p = build_payload("wecom", _mk_item())
    assert p["msgtype"] == "markdown"
    assert "92分" in p["markdown"]["content"] and "原文" in p["markdown"]["content"]


def test_push_dingtalk_and_feishu():
    p = build_payload("dingtalk", _mk_item())
    assert p["msgtype"] == "markdown" and p["markdown"]["title"]
    p = build_payload("feishu", _mk_item())
    assert p["msg_type"] == "text" and "92分" in p["content"]["text"]


# ---------- 华尔街见闻解析 ----------
def test_parse_wscn_items():
    now = int(datetime.now().timestamp())
    items = [
        {"type": "live", "title": "", "content_text": "美联储宣布维持利率不变",
         "display_time": now, "uri": "live/123", "id": 1},
        {"type": "advert", "title": "广告", "content_text": "x",
         "display_time": now, "id": 2},                       # 应跳过
        {"type": "live", "title": "缺时间", "content_text": "x", "id": 3},  # 应跳过
    ]
    rows = parse_wscn_items(items)
    assert len(rows) == 1
    assert rows[0]["source"] == "wscn"
    assert rows[0]["url"] == "https://wallstreetcn.com/live/123"
