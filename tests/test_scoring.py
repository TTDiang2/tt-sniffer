"""打分引擎测试：覆盖分类边界、双计回归、正则优先级、英文误伤、异动关联。"""
from app.scoring import Scorer, literal_codes

WATCH = [{"code": "600519", "name": "贵州茅台"},
         {"code": "430047", "name": "诺思兰德"}]
S = Scorer(WATCH)


def test_strong_rumor_classified():
    v = S.score("茅台提价传闻", "市场传闻贵州茅台将提价")
    assert v["cls"] == "rumor"
    assert v["watch"] and "贵州茅台" in v["targets"]


def test_weak_words_alone_stay_noise():
    v = S.score("", "据悉，日本金融厅将对某保险公司下达业务停止命令")
    assert v["cls"] == "noise"
    assert v["score"] < 50


def test_debunk_no_double_count():
    """回归：辟谣句曾同时计传闻词分导致满分刷屏。"""
    v = S.score("辟谣声明", "辟谣：市场传闻不实，公司郑重提示")
    assert v["cls"] == "debunk"
    assert v["score"] < 95


def test_weak_debunk_alone_not_classified():
    v = S.score("", "某公司回应称，市场秩序不存在问题")
    assert v["cls"] == "noise"


def test_longest_word_first():
    """回归：正则左优先导致'传闻称'永远被'传闻'抢占。"""
    v = S.score("", "传闻称某公司将被收购")
    assert "传闻称" in v["keywords"]


def test_english_no_st_false_positive():
    """回归：'st' 曾匹配 first/boost 等英文子串。"""
    v = S.score("", "The chairman first boosted investment in BASF shares")
    assert "st" not in [k.lower() for k in v["keywords"]]


def test_ai_boundary():
    assert "AI" not in S.score("", "BAIDU and CHAIRMAN rose")["keywords"]
    assert "AI" in S.score("", "AI板块全线大涨")["keywords"]


def test_bse_code_recognized():
    """回归：正则漏掉 4/8 开头（北交所）与 2 开头（深市B股）代码。"""
    assert "430047" in literal_codes("430047放量涨停")
    assert "200011" in literal_codes("200011异动")
    v = S.score("", "网传430047将被大基金收购")
    assert v["cls"] == "rumor"
    assert "430047" in v["targets"]  # watch 代码命中


def test_linked_anomaly_boost():
    """关联异动加分但不得污染"关注"语义（targets/watch 严格等于关注列表命中）。"""
    base = S.score("", "市场传闻贵州茅台将提价")
    linked = S.score("", "市场传闻贵州茅台将提价", linked={"九阳股份"})
    assert "异动关联" in linked["keywords"]
    assert linked["score"] > base["score"]
    assert linked["watch"] is base["watch"] is True
    assert "九阳股份" not in linked["targets"]
    # 未命中关注列表的纯异动标的不得触发 watch
    no_watch = S.score("", "传闻称某题材获政策支持", linked={"九阳股份"})
    assert no_watch["watch"] is False


def test_ordinary_news_low_score():
    v = S.score("", "腾讯于10月2日回购了价值1.003亿港元的股份")
    assert v["score"] < 50


def test_official_capped_below_alert():
    v = S.score("央行公告", "央行宣布降准0.5个百分点")
    assert v["cls"] == "official"
    assert 45 <= v["score"] < 80


def test_rumor_with_impact_hits_alert():
    v = S.score("", "市场传闻央行降准")
    assert v["cls"] == "rumor"
    assert v["score"] >= 80
