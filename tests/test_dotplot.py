"""利率观点抽取器测试（点阵图数据源）：动作/时点/概率/归因配对与噪音拦截。"""
from app.dotplot import extract, extract_items


def test_cme_probability_paired_to_action():
    """CME 标准文案：hold 的概率与 hike 的 bp 不得错挂（±20 字观点窗口）。"""
    v = extract("美联储到10月维持利率不变的概率为78.4%，加息25个基点的概率为21.6%",
                year=2026)
    assert v and v["action"] == "hold" and v["month"] == "2026-10"
    assert v["prob"] == 78.4 and v["bp"] is None      # 25bp 属于 hike 不属于 hold
    v2 = extract("美联储到12月加息25个基点的概率为72.6%", year=2026)
    assert v2["action"] == "hike" and v2["month"] == "2026-12"
    assert v2["bp"] == 25 and v2["prob"] == 72.6


def test_sellside_bank_forecast():
    v = extract("渣打银行预计，欧洲央行将在2028年第一季度降息25个基点", year=2026)
    assert v["cb"] == "ecb" and v["action"] == "cut"
    assert v["month"] == "2028-03" and v["bp"] == 25
    assert v["actor"] == "sellside"


def test_trader_bet():
    v = extract("交易员削减对美联储10月加息的押注", year=2026)
    # '削减押注'负向窗口拦截——观点退潮不是观点
    assert v is None


def test_negation_blocked():
    assert extract("央行加息预期降温", year=2026) is None
    assert extract("市场对降息的押注减少", year=2026) is None


def test_no_month_dropped():
    """无时点的观点不进点阵（点阵核心是时点，宁缺勿滥）。"""
    assert extract("美联储官员暗示可能加息", year=2026) is None


def test_unknown_central_bank_dropped():
    """央行归属不明（'英国利率'类歧义）不进点阵。"""
    assert extract("市场预计英国利率将在10月下调", year=2026) is None


def test_extract_items_dedup_and_full_text_actor():
    """同 (cb,action,month,bp) 去重；actor 用全文级（CME 分段无独立归因）。"""
    text = ("据CME美联储观察：美联储到10月维持利率不变的概率为78.4%，"
            "加息25个基点的概率为21.6%。到12月维持利率不变的概率为54.2%")
    vs = extract_items(text, year=2026)
    keys = [(v["action"], v["month"]) for v in vs]
    assert ("hold", "2026-10") in keys and ("hike", "2026-10") in keys
    assert ("hold", "2026-12") in keys
    assert all(v["actor"] == "market_cme" for v in vs)


def test_nonfarm_day_end_to_end():
    """试金石：10-02 真实文案端到端。"""
    vs = extract_items(
        "【美联储12月加息概率为72.6%】据CME美联储观察："
        "美联储到10月维持利率不变的概率为86.2%，加息25个基点的概率为13.8%",
        year=2026)
    pairs = {(v["action"], v["month"]): v for v in vs}
    assert pairs[("hike", "2026-12")]["prob"] == 72.6
    assert pairs[("hold", "2026-10")]["prob"] == 86.2


# ---------- v6/R1-R2 智者溯源整改回归 ----------
def test_r1_no_longer_pricing_blocked():
    """'不再定价加息'=反加息证据，不得抽成 hike 正观点。"""
    assert extract("掉期合约已不再定价今年内一次完整的加息（25个基点）",
                   year=2026) is None


def test_r1_unlikely_blocked():
    assert extract("高盛：10月加息现在不太可能", year=2026) is None
    assert extract("市场认为10月加息可能性不大", year=2026) is None


def test_r1_dual_action_escape_blocked():
    """'是否加息…押注进一步降温'——负向词在隔壁分段也不得逃逸。"""
    assert extract("美联储理事表示，她是否支持10月加息将取决于数据，"
                   "为市场对10月加息的押注进一步降温", year=2026) is None


def test_r1_withdrawn_forecast_not_revived():
    """'将此前预测的10月和1月加息推迟至12月和3月'——已撤销的10月Call不复活。"""
    vs = extract("道明证券：将此前预测的10月和1月加息推迟至12月和3月", year=2026)
    assert not vs or all(v["month"] != "2026-10" for v in vs)   # 全拦或无10月复活


def test_r2_dateline_month_rejected():
    """日期线'10月2日电'的月份不得充当会议时点。"""
    v = extract("财联社10月2日电，交易员预计美联储将于12月和3月加息", year=2026)
    assert v and v["month"] == "2026-12"   # 10月（日期线）被拒，12月（真时点）保留


def test_r2_data_month_rejected():
    """'9月就业报告'是数据月份，不得当会议时点。"""
    v = extract("9月就业报告显示，这让美联储未来的加息路径变得更加复杂", year=2026)
    assert v is None


def test_r1_multi_cb_segment_guard():
    """多央行句：央行词不在本段 → 拒（'欧洲央行9月降息'曾挂到 fed）。"""
    assert extract("欧洲央行9月降息的可能性上升，美联储官员则保持耐心", year=2026) is None


def test_r1_mutual_exclusive_month_inheritance():
    """互斥结果继承：'到10月维持78.4%，加息25bp 21.6%'——hike 继承 10月。"""
    vs = extract_items("美联储到10月维持利率不变的概率为78.4%，"
                       "加息25个基点的概率为21.6%", year=2026)
    keys = {(v["action"], v["month"]) for v in vs}
    assert ("hold", "2026-10") in keys and ("hike", "2026-10") in keys


# ---- v2 具体点位（用户规格：纵轴要有像 FOMC 点阵图一样的具体点位） ----

def test_base_from_sep_parses_decision():
    """SEP 决策串 → 当前区间（3.75%-4.00% → lo 3750 / hi 4000 / mid 3875）。"""
    from app.dotplot import base_from_sep
    base = base_from_sep({"decision": "加息25bp至3.75%-4.00%（2023年7月以来首次加息，12-0 全票）",
                          "meeting": "2026-09-16"})
    assert base == {"lo": 3750, "hi": 4000, "mid": 3875,
                    "label": "3.75-4.00%", "meeting": "2026-09-16"}


def test_base_from_sep_garbage_returns_none():
    """无区间可解析（维持不变/空/None/非 25bp 宽区间）→ None，前端回退动作轴。"""
    from app.dotplot import base_from_sep
    assert base_from_sep({"decision": "维持利率不变"}) is None
    assert base_from_sep({"decision": ""}) is None
    assert base_from_sep(None) is None
    assert base_from_sep({"decision": "下调至3.50%-4.00%"}) is None   # 非 25bp 宽，标签口径不成立


def test_cell_level_mapping():
    """观点档位 = 当前中点 ±bp×10（单位 4125=4.125%，1bp=10 单位）；缺幅度按 25bp 计。"""
    from app.dotplot import cell_level
    assert cell_level("cut", 25, 3875) == 3625     # 3.75-4.00 → 3.50-3.75
    assert cell_level("cut", None, 3875) == 3625
    assert cell_level("cut", 50, 3875) == 3375     # → 3.25-3.50
    assert cell_level("hold", None, 3875) == 3875  # 维持在 3.75-4.00
    assert cell_level("hike", 25, 3875) == 4125    # → 4.00-4.25
    assert cell_level("hike", None, 3875) == 4125
