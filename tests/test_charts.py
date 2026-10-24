"""统计图表纯函数测试：桶完整性、news_point/day_point 坐标语义。"""
from app import charts
from app.impact import ASSETS


def test_buckets_cover_all_assets():
    """桶表必须完整覆盖 ASSETS（单一事实来源对齐）。"""
    for a in ASSETS:
        assert charts.bucket_of(a) in ("risk", "haven", "out"), a
    assert set(charts.RISK) | set(charts.HAVEN) | set(charts.OUT) == set(ASSETS)


def test_fx_and_oil_are_out():
    assert charts.bucket_of("fx") == "out"
    assert charts.bucket_of("oil") == "out"
    assert charts.bucket_of("gold") == "haven"
    assert charts.bucket_of("us_stock") == "risk"


def test_news_point_mean_and_rel_excluded():
    """x=风险侧强度（bull=+1/bear=-1，rel 不计），缺失侧为 0；净票为 0 也在轴心。"""
    p = charts.news_point([
        {"a": "us_stock", "d": "bull", "k": "x", "g": "guess"},
        {"a": "a_stock", "d": "bear", "k": "x", "g": "guess"},
        {"a": "gold", "d": "rel", "k": "x", "g": "fact"},     # rel 不计
    ])
    assert p == {"x": 0, "y": 0, "sides": "risk"}


def test_news_point_both_sides():
    """v9 连续强度：2 张同向票 = tanh(2/2)×100 ≈ 76，不再钉死 ±100。"""
    p = charts.news_point([
        {"a": "us_stock", "d": "bull", "k": "x", "g": "guess"},
        {"a": "crypto", "d": "bull", "k": "x", "g": "guess"},
        {"a": "gold", "d": "bull", "k": "x", "g": "guess"},
        {"a": "bond", "d": "bear", "k": "x", "g": "guess"},
    ])
    assert p["x"] == 76 and p["y"] == 0 and p["sides"] == "both"


def test_news_point_none_when_no_direction():
    assert charts.news_point([{"a": "gold", "d": "rel", "k": "x", "g": "f"}]) is None
    assert charts.news_point([]) is None


def test_news_point_out_assets_ignored():
    """fx/oil（out 桶）不参与坐标。"""
    p = charts.news_point([
        {"a": "fx", "d": "bull", "k": "x", "g": "guess"},
        {"a": "oil", "d": "bear", "k": "x", "g": "guess"},
    ])
    assert p is None


def test_day_point_averages_and_none_skip():
    rows = [{"a": "gold", "score": 60}, {"a": "bond", "score": 20},
            {"a": "us_stock", "score": None}, {"a": "fx", "score": 80}]
    p = charts.day_point(rows)
    assert p["x"] == 0 and p["y"] == 40 and p["sides"] == "haven"


def test_day_point_all_none():
    assert charts.day_point([{"a": "gold", "score": None}]) is None
    assert charts.day_point([]) is None


def test_bond_split_buckets():
    """v8 债市拆分：中债/美债都在避险侧，且 RISK|HAVEN|OUT 并集仍覆盖全表。"""
    assert charts.bucket_of("bond") == "haven"
    assert charts.bucket_of("bond_us") == "haven"


def test_us10y_in_haven_side_score():
    """美债的避险侧方向票参与 y 轴（与中债同侧）；风险侧无票归 0。"""
    p = charts.news_point([
        {"a": "bond_us", "d": "bull", "k": "x", "g": "guess"},
        {"a": "bond", "d": "bear", "k": "x", "g": "guess"},
    ])
    assert p["x"] == 0 and p["y"] == 0 and p["sides"] == "haven"


def test_news_point_continuous_strength_spread():
    """v9 连续强度：同方向票数不同 → 坐标分层；单票不再钉死 ±100 轴端。"""
    p1 = charts.news_point([{"a": "us_stock", "d": "bull", "k": "x"}])
    p2 = charts.news_point([{"a": "us_stock", "d": "bull", "k": "x"},
                            {"a": "a_stock", "d": "bull", "k": "x"}])
    p3 = charts.news_point([{"a": "us_stock", "d": "bull", "k": "x"},
                            {"a": "a_stock", "d": "bull", "k": "x"},
                            {"a": "crypto", "d": "bull", "k": "x"}])
    assert 0 < p1["x"] < p2["x"] < p3["x"] < 100
    assert abs(p1["x"] - 46) <= 1 and abs(p2["x"] - 76) <= 1
    assert p1["y"] == 0 and p1["sides"] == "risk"


def test_news_point_day_sample_not_edge_degenerate():
    """同日多样本回归（诊断口径）：坐标不全等于 ±100（贴边退化已修）。"""
    samples = [
        [{"a": "gold", "d": "bull", "k": "x"}],
        [{"a": "gold", "d": "bear", "k": "x"}],
        [{"a": "bond", "d": "bull", "k": "x"},
         {"a": "us_stock", "d": "bear", "k": "x"}],
        [{"a": "crypto", "d": "bull", "k": "x"},
         {"a": "gold", "d": "bear", "k": "x"}],
    ]
    pts = [charts.news_point(s) for s in samples]
    assert all(p is not None for p in pts)
    assert all(abs(p["x"]) != 100 and abs(p["y"]) != 100 for p in pts)
    assert len({(p["x"], p["y"]) for p in pts}) >= 3
