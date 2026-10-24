"""资产情绪聚合测试：公式、票去重、fact 权重、rel 忽略、非农试金石方向。"""
from app.impact import detect
from app.sentiment import compute


def _by_a(rows):
    return {r["a"]: r for r in rows}


def test_basic_vote_and_score():
    """两票利多一票利空（等权 guess）→ 100*(2-1)/(2+1+K=3)≈17（K=3 置信平滑）。"""
    items = [{"impacts": [
        {"a": "gold", "d": "bull", "k": "r1", "g": "guess"},
        {"a": "gold", "d": "bull", "k": "r2", "g": "guess"},
        {"a": "gold", "d": "bear", "k": "r3", "g": "guess"},
    ]}]
    gold = _by_a(compute(items))["gold"]
    assert gold["score"] == 17
    assert gold["bull_n"] == 2 and gold["bear_n"] == 1


def test_same_rule_counts_once():
    """同规则同方向多源转载只计一票（防同稿转载刷分）；
    单票不再饱和：w=1.3×√2≈1.84 → 100×1.84/(1.84+3)≈38。"""
    items = [{"impacts": [{"a": "gold", "d": "bull", "k": "fed_ease", "g": "fact"}]},
             {"impacts": [{"a": "gold", "d": "bull", "k": "fed_ease", "g": "fact"}]}]
    gold = _by_a(compute(items))["gold"]
    assert gold["bull_n"] == 1 and gold["score"] == 38


def test_fact_heavier_than_guess():
    """落地票 1.3 > 预期票 1.0：1 fact bull vs 1 guess bear → 100*0.3/5.3≈6。"""
    items = [{"impacts": [{"a": "fx", "d": "bull", "k": "r1", "g": "fact"}]},
             {"impacts": [{"a": "fx", "d": "bear", "k": "r2", "g": "guess"}]}]
    fx = _by_a(compute(items))["fx"]
    assert fx["score"] == 6


def test_rel_ignored_and_empty_none():
    """rel 不计方向；无任何方向票 → 分数为 None。"""
    rows = compute([{"impacts": [{"a": "gold", "d": "rel", "k": "x", "g": "fact"}]}])
    assert rows == []
    rows = compute([{"impacts": []}])
    assert rows == []


def test_sort_by_abs_score():
    rows = compute([
        {"impacts": [{"a": "fx", "d": "bull", "k": "r1", "g": "guess"}]},   # 25
        {"impacts": [{"a": "gold", "d": "bull", "k": "r1", "g": "guess"},
                     {"a": "gold", "d": "bear", "k": "r2", "g": "guess"}]},  # 0（等权对冲）
    ])
    assert [r["a"] for r in rows] == ["fx", "gold"]


# ---------- 试金石：10-02 非农（真实文本，规则修复回归） ----------
NONFARM = ("【美国就业增长逊于预期 企业招聘保持谨慎】美国9月份新增就业人数低于预期，"
           "薪资增速放缓。数据公布后，交易员削减了对美联储10月加息的押注。")


def test_nonfarm_weak_data_is_bull():
    """用户试金石：非农不及预期 = 宽松预期 = 美股/黄金利多（v1 曾标反）。"""
    d = {x["a"]: x for x in detect(NONFARM)}
    assert d["us_stock"]["d"] == "bull" and d["us_stock"]["k"] == "us_data_weak"
    assert d["gold"]["d"] == "bull"


def test_cut_hike_bets_not_bear():
    """'削减加息押注'是宽松方向，fed_hawk 不得触发 bear（NEG_COMMON 削减）。"""
    d = {x["a"]: x for x in detect("交易员削减了对美联储10月加息的押注")}
    assert d.get("us_stock", {}).get("d") != "bear"
    assert d.get("gold", {}).get("d") != "bear"


def test_strong_data_bull_reversed():
    """对称规则：非农大超预期 → 美股/黄金利空。"""
    d = {x["a"]: x for x in detect("美国9月非农就业人数大超预期，失业率降至4.0%")}
    assert d["us_stock"]["d"] == "bear" and d["gold"]["d"] == "bear"


def test_question_form_no_data_direction():
    """问句前瞻（'是否不及预期'）不给方向。"""
    d = {x["a"]: x for x in detect("市场关注今晚非农是否会不及预期")}
    assert d.get("us_stock", {}).get("d") != "bull"


def test_override_beats_action_rule():
    """override：数据弱规则压过同文本的动作词 bear，不降级为方向冲突。"""
    d = {x["a"]: x for x in detect(
        "非农新增就业人数低于预期，此前美联储官员曾暗示可能加息")}
    assert d["us_stock"]["d"] == "bull"


def test_conflict_sticky_not_revived_by_override():
    """方向冲突粘滞态不被 override 复活（不确定就保持 rel）。"""
    d = {x["a"]: x for x in detect(
        "贸易战升级但中美达成贸易协议，非农新增就业人数低于预期")}
    assert d["a_stock"]["d"] == "rel" and d["a_stock"]["k"] == "方向冲突"


# ---------- VER=4 传播杠杆与负向词扩充（非农日教训回归） ----------
def test_cme_probability_not_bear():
    """CME 概率报道（'加息概率72.6%'）是预期陈述，不得当落地利空。"""
    d = {x["a"]: x for x in detect("【美联储12月加息概率为72.6%】据CME美联储观察")}
    assert d.get("us_stock", {}).get("d") != "bear"


def test_reduce_bets_not_bear():
    """'降低/缩减/减少对加息的押注'是宽松方向（非农日误报主力）。"""
    for v in ("降低", "缩减", "减少"):
        d = {x["a"]: x for x in detect(f"交易员{v}了对美联储12月加息的押注")}
        assert d.get("us_stock", {}).get("d") != "bear", v


def test_weak_data_easing_phrases_not_bear():
    """'缓解加息压力/加息路径复杂（疲弱语境）'是宽松解读，不得标 bear。"""
    d = {x["a"]: x for x in detect(
        "不及预期的就业数据缓解了美联储本月加息的压力，建筑商股票上涨")}
    assert d.get("us_stock", {}).get("d") != "bear"


def test_genuine_hawkish_view_stays_bear():
    """真紧缩观点（道明证券预计 12 月和 3 月加息）保留利空，不误伤。"""
    d = {x["a"]: x for x in detect(
        "道明证券预计美联储将于12月和3月加息，通胀粘性令决策者难以转向宽松")}
    assert d.get("us_stock", {}).get("d") == "bear"


def test_mentions_leverage():
    """传播杠杆：同规则 7 条独立条目 → 权重 ×min(3,√7)≈2.65，但论据数仍为 1。
    K=3 平滑下：单 guess 票 -25（不饱和）；7 条传播 100×2.65/5.65≈47。"""
    one = compute([{"impacts": [{"a": "gold", "d": "bear", "k": "geo_war", "g": "guess"}]}])
    many = compute([{"impacts": [{"a": "gold", "d": "bull", "k": "us_data_weak",
                                  "g": "guess"}]}] * 7)
    gold_one = _by_a(one)["gold"]
    gold_many = _by_a(many)["gold"]
    assert gold_one["score"] == -25
    assert gold_many["bull_n"] == 1        # 论据数不膨胀
    assert gold_many["score"] == 47        # 传播厚→幅度大，但仍不满格
    # 权重差异体现在与对手票的对冲里：
    mixed_one = compute([{"impacts": [{"a": "gold", "d": "bull", "k": "w", "g": "guess"},
                                      {"a": "gold", "d": "bear", "k": "l", "g": "guess"}]}])
    mixed_many = compute([{"impacts": [{"a": "gold", "d": "bull", "k": "w", "g": "guess"}]}] * 7 +
                         [{"impacts": [{"a": "gold", "d": "bear", "k": "l", "g": "guess"}]}])
    assert _by_a(mixed_many)["gold"]["score"] > _by_a(mixed_one)["gold"]["score"]


# ---------- K=3 置信平滑锚定（20261022 打分机制批，智者裁决要求） ----------
def test_single_guess_vote_not_saturated():
    """单张 guess 票：100*1/(1+3)=25——K 单位=guess 票权的锚点。"""
    rows = compute([{"impacts": [{"a": "gold", "d": "bull", "k": "r1", "g": "guess"}]}])
    assert _by_a(rows)["gold"]["score"] == 25


def test_single_fact_max_leverage_vote_57():
    """fact 满传播杠杆（≥9 条提及 → ×3）单票权 3.9：100*3.9/6.9≈57 而非 100。
    K 是 guess 票权单位：满权 fact 单票抵 3.9 张 guess 票，但不饱和。"""
    rows = compute([{"impacts": [{"a": "gold", "d": "bull", "k": "r1",
                                  "g": "fact"}]}] * 9)
    assert _by_a(rows)["gold"]["score"] == 57


def test_sign_follows_numerator_and_zero_when_balanced():
    """符号与分子一致（K 只进分母）；等权对冲 → 0。"""
    bull_wins = compute([{"impacts": [{"a": "fx", "d": "bull", "k": "w", "g": "guess"},
                                      {"a": "fx", "d": "bear", "k": "l", "g": "guess"}]},
                         {"impacts": [{"a": "fx", "d": "bull", "k": "w2", "g": "guess"}]}])
    assert _by_a(bull_wins)["fx"]["score"] > 0
    balanced = compute([{"impacts": [{"a": "fx", "d": "bull", "k": "w", "g": "guess"},
                                     {"a": "fx", "d": "bear", "k": "l", "g": "guess"}]}])
    assert _by_a(balanced)["fx"]["score"] == 0


# ---------- VER=5 智者对抗探针回归（R1-R4） ----------
def test_r1_non_us_employment_not_us_signal():
    """R1：中国/欧澳日就业数据与美国宽松逻辑无关，不得触发 us_data_* 全系。"""
    for text in ("中国城镇新增就业人数低于预期",
                 "7月全国城镇调查失业率升至5.3%，就业形势恶化",
                 "澳大利亚就业报告逊于预期",
                 "欧元区就业人数意外下滑"):
        d = {x["a"]: x for x in detect(text)}
        assert d.get("us_stock", {}).get("k") != "us_data_weak", text
        assert d.get("gold", {}).get("k") != "us_data_weak", text


def test_r1_us_employment_still_passes_guard():
    """R1 反向：美国原文含'调查'等字样不得被语境守卫误拦。"""
    d = {x["a"]: x for x in detect("美国9月非农新增就业人数低于预期")}
    assert d["us_stock"]["d"] == "bull"


def test_r1_rate_change_extent_not_inverted():
    """R1：'升幅低于预期'=数据偏强、'降幅低于预期'=数据偏强，都不得标宽松 bull。"""
    for text in ("美国失业率升幅低于预期", "美国9月非农就业降幅低于预期",
                 "ADP就业降幅低于预期"):
        d = {x["a"]: x for x in detect(text)}
        assert d.get("us_stock", {}).get("d") != "bull", text


def test_r2_landed_ease_survives_bets():
    """R2：已落地降息与'押注'同现 → 落地事件豁免头寸名词。"""
    d = {x["a"]: x for x in detect("美联储宣布降息25个基点，交易员押注12月再降一次")}
    assert d["us_stock"]["d"] == "bull" and d["us_stock"]["g"] == "fact"


def test_r2_bet_cut_still_blocked_even_with_fact_word():
    """R2 追加（豁免粒度回归）：'数据公布后降低对加息的押注'是宽松向句子，
    '公布'不得借 fact 豁免把 fed_hawk bear 放行（NEG_WEAKEN 永不豁免）。"""
    d = {x["a"]: x for x in detect(
        "非农数据公布后，交易员降低对美联储12月加息的押注")}
    assert d.get("us_stock", {}).get("d") != "bear"
    assert d.get("gold", {}).get("d") != "bear"


def test_r3_stage_monotonic_regardless_of_order():
    """R3：同票任一实例为 fact → 票按 fact 计，与条目顺序无关。"""
    imp_bull = {"a": "gold", "d": "bull", "k": "w", "g": "guess"}
    imp_bear = {"a": "gold", "d": "bear", "k": "l", "g": "fact"}
    a = compute([{"impacts": [imp_bull]}, {"impacts": [imp_bear]}])
    b = compute([{"impacts": [imp_bear]}, {"impacts": [imp_bull]}])
    assert _by_a(a)["gold"]["score"] == _by_a(b)["gold"]["score"]
    # 且 fact 权重确实生效（bear 1.3 vs bull 1.0 → 负分）
    assert _by_a(a)["gold"]["score"] < 0


def test_r4_override_wins_regardless_of_table_order():
    """R4：数据规则后于动作词规则命中（表序在后）仍须赢——不得降级方向冲突。"""
    d = {x["a"]: x for x in detect(
        "非农新增就业人数低于预期，美国宣布对华加征关税")}
    assert d["us_stock"]["d"] == "bull" and d["us_stock"]["k"] == "us_data_weak"
    assert d["em_stock"]["d"] == "bull"   # 同理不被 tariff bear 拖成冲突
