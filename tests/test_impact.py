"""资产影响引擎测试：覆盖智者规格 §6 全部验收用例 + 负向词/两态/合并边界。"""
from app.impact import ASSETS, RULES, detect


def _dirs(full):
    return {x["a"]: x["d"] for x in detect(full)}


def test_fed_cut_rumor_bull_gold_guess():
    """验收1：传闻降息 → 传闻类 + 黄金/美股利多（预期阶段）。"""
    v = detect("网传美联储3月降息50bp")
    d = {x["a"]: x for x in v}
    assert d["gold"]["d"] == "bull" and d["gold"]["g"] == "guess"
    assert d["us_stock"]["d"] == "bull"


def test_rrr_cut_no_gold():
    """验收2：中国降准利好 A股/债市，但绝不顺手标黄金。"""
    d = _dirs("央行宣布降准0.5个百分点")
    assert d["a_stock"] == "bull" and d["bond"] == "bull"
    assert "gold" not in d
    assert d["a_stock"] != "rel"  # 落地词"宣布"不改变方向，但方向不是 rel


def test_rrr_cut_no_realestate():
    """v11：中国降准不再给 realestate 投票（宏观宽松≠地产直接利多，搭车票拆除）；
    地产相关文本若无 house_ease 触发词，只应得到 rel。"""
    d = _dirs("央行宣布降准0.5个百分点，市场关注流动性")
    assert d.get("realestate") in (None, "rel")
    assert d.get("realestate") != "bull"
    # 楼市新政专属规则不受影响：地产利多仍由 house_ease 独立支撑
    # （注：「降低首付」触发词自带 NEG_WEAKEN 词「降低」被负向守卫自我屏蔽——
    #   既有词表怪癖，挂在 house_ease 回看义务里，不在本批修）
    d2 = _dirs("多地出台楼市新政：认房不认贷、取消限购")
    assert d2.get("realestate") == "bull"


def test_pause_hike_not_inverted():
    """验收3：'暂停加息'不得被 cn_tight/fed_hawk 标反方向。"""
    d = _dirs("美联储暂停加息预期升温")
    assert d["gold"] == "bull" and d["us_stock"] == "bull"


def test_price_action_is_rel_only():
    """验收4：'COMEX黄金上涨1%' 是价格行为不是事件 → 仅 gold/rel。"""
    v = detect("COMEX黄金上涨1%")
    assert len(v) == 1 and v[0]["a"] == "gold" and v[0]["d"] == "rel"


def test_special_bond_supply_overrides():
    """验收5：特别国债增发 → bond bear（供给）压过 fiscal 的 rel，A股 bull。"""
    d = _dirs("特别国债增发支撑股市")
    assert d["bond"] == "bear" and d["a_stock"] == "bull"


def test_no_false_positive_on_loans():
    """验收6：还贷/回购类个股新闻零标注。"""
    assert detect("本公司偿还银行贷款并回购股份注销") == []


def test_bull_bear_conflict_downgrades():
    """验收7：同资产 bull+bear 并存 → 降级 rel/方向冲突。"""
    d = {x["a"]: x for x in detect("贸易战升级，但中美达成贸易协议")}
    assert d["a_stock"]["d"] == "rel" and d["a_stock"]["k"] == "方向冲突"


def test_central_bank_not_in_assets():
    """铁律4：美联储不出现在任何资产检测表。"""
    all_kws = [k for v in ASSETS.values() for k in v["kws"]]
    for w in ("美联储", "Fed", "央行", "鲍威尔"):
        assert w not in all_kws


def test_meetings_stay_rel():
    """用户口径：经济工作会议表态出来前一律相关。"""
    d = _dirs("中央经济工作会议召开在即，市场关注政策表态")
    assert d["a_stock"] == "rel" and d["bond"] == "rel"


def test_hike_blocked_by_negation():
    """'加息预期降温'不得触发 cn_tight/fed_hawk 的利空。"""
    v = detect("市场传闻央行加息预期降温，资金面转松")
    d = {x["a"]: x for x in v}
    assert d.get("a_stock", {}).get("d") != "bear"
    assert d.get("bond", {}).get("d") != "bear"


def test_rules_have_neg_guard():
    """结构断言：cn_tight/fed_hawk 必须挂 NEG_COMMON 负向词（智者重点核查项）。"""
    by_id = {r["id"]: r for r in RULES}
    assert "暂停" in by_id["cn_tight"]["neg"]
    assert "暂停" in by_id["fed_hawk"]["neg"]


def test_fact_vs_guess_stage():
    v = detect("美联储降息")
    assert all(x["g"] == "guess" for x in v)   # 无落地词
    v = detect("美联储正式宣布降息25个基点")
    assert all(x["g"] == "fact" for x in v)


def test_rel_never_overrides_direction():
    """方向优先于 rel：'人民币贬值'触发 cny_dn 的 fx bear，资产层 rel 不覆盖。"""
    d = _dirs("人民币贬值压力升温，在岸汇率走弱")
    assert d["fx"] == "bear"


# ---------- 智者对抗探针（审查 R1-R5 回归） ----------
def test_question_form_no_direction():
    """R1：'是否降息'前瞻问句不得给任何方向。"""
    d = _dirs("市场聚焦美联储6月是否降息")
    assert "bull" not in d.values()


def test_pause_cut_not_bull():
    """R2：'暂停降息'=收紧倾向，不得标利多。"""
    d = _dirs("美联储暂停降息以观察经济数据")
    assert d.get("gold") != "bull" and d.get("us_stock") != "bull"


def test_ambiguous_pivot_not_bull():
    """R3：'美联储转向鹰派'不得标利多（转向方向歧义，裸词已删）。"""
    d = _dirs("美联储转向鹰派，维持缩表节奏")
    assert d.get("gold") != "bull" and d.get("us_stock") != "bull"


def test_break7_needs_subject():
    """R4：裸'破7'不得误伤（金价跌破700元/克）。"""
    d = _dirs("金价跌破700元每克")
    assert d.get("fx") != "bear" and d.get("a_stock") != "bear"
    assert d.get("gold") == "rel"


def test_conflict_is_sticky():
    """R5：bull×bear 降级后的 rel 必须粘滞，后续同向规则不得再覆盖。"""
    d = {x["a"]: x for x in detect("贸易战升级，但中美达成贸易协议，汇金增持护盘")}
    assert d["a_stock"]["d"] == "rel" and d["a_stock"]["k"] == "方向冲突"
    assert d["us_stock"]["d"] == "rel"  # 同一句话两个资产口径一致


# ---------- v8 债市拆分：中债(bond)/美债(bond_us) ----------

def test_bond_split_us_bond_keywords():
    """美债词只进 bond_us，绝不误命中中债（用户任务书：区分中美债市）。"""
    for text in ("美债收益率触及4.2%", "美国国债拍卖遇冷", "10年期美债下跌",
                 "美国债券市场抛售"):
        d = _dirs(text)
        assert "bond_us" in d, text
        assert "bond" not in d, text


def test_bond_split_compound_prefixes():
    """v9 智者六例：连缀词（美国债市/美国债券/美国债）绝不双标中债。"""
    for text in ("美国债市", "美国债券", "美国债",
                 "美国债市收益率大幅上行"):
        d = _dirs(text)
        assert "bond_us" in d, text
        assert "bond" not in d, text
    d_cn = _dirs("中国债市收益率上行")
    assert "bond" in d_cn and "bond_us" not in d_cn
    d_both = _dirs("美债与中债走势分化")
    assert "bond" in d_both and "bond_us" in d_both


def test_bond_split_cn_bond_keywords():
    """中国债市词只进中债：'美国'前置词被负向断言挡住。"""
    d = _dirs("央行净投放3000亿元，资金面转松")
    assert d.get("bond") == "bull" and "bond_us" not in d
    d2 = _dirs("中国国债收益率小幅上行")
    assert "bond" in d2 and "bond_us" not in d2


def test_fed_ease_marks_bond_us_not_cn_bond():
    """v8：美联储宽松给美债 bull（宽松压收益率），中债不标。"""
    d = {x["a"]: x for x in detect("网传美联储3月降息50bp")}
    assert d["bond_us"]["d"] == "bull" and d["bond_us"]["g"] == "guess"
    assert "bond" not in d


def test_us_data_weak_marks_bond_us():
    """v8：非农不及预期 → 美债 bull（宽松预期升温），对称规则 bear。"""
    d = _dirs("美国非农新增就业大幅不及预期")
    assert d["bond_us"] == "bull"
    d2 = _dirs("美国非农新增就业大超预期")
    assert d2["bond_us"] == "bear"


def test_geo_war_wordlist_gap():
    """v10：20261020 夜漏检的 geo_war 字面缺口语义。"""
    for text in ("联军摧毁导弹平台", "俄首都遭大规模无人机袭击",
                 "沙特雇佣军未能攻占曼德海峡", "军事行动升级",
                 "双方激烈争夺港口"):
        d = _dirs(text)
        assert d.get("gold") == "bull" and d.get("oil") == "bull", text


def test_geo_war_destroy_guard_avoids_non_military():
    """v10：'摧毁'只接军事目标，不能把'摧毁信心'当战争。"""
    assert "gold" not in _dirs("公司摧毁了投资者信心")


def test_fin_sanction_russian_variants():
    """v10：'对俄罗斯实施新一轮制裁'不再因非连续串漏判。"""
    for text in ("英国宣布对俄罗斯实施新一轮制裁", "美国对俄制裁升级",
                 "欧盟对俄罗斯制裁方案落地"):
        assert _dirs(text).get("gold") == "bull", text


def test_crypto_off_english_ban_guard():
    """v10：英文 Ban 只在加密语境触发，P2P 借贷反例不误伤。"""
    assert _dirs("Russia to Fully Ban P2P").get("crypto") == "bear"
    assert _dirs("regulators move to ban crypto trading").get("crypto") == "bear"
    assert "crypto" not in _dirs("The government will Ban P2P lending")
