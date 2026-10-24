"""SEP 自动抓取器测试：真实 HTML fixture 解析 + 缓存优先/降级链路。"""
from pathlib import Path

from app import fed_sep, sep_fetch

FIXTURE = Path(__file__).parent / "fixtures" / "sep_20260916.html"


def _parsed():
    html = FIXTURE.read_text(encoding="utf-8")
    d = sep_fetch.parse_sep_html(html, "20260916")
    assert d, "fixture 解析失败（结构回归？）"
    return d


def test_parse_distribution_full_five_years():
    """官方逐点分布：五年全档位（比媒体转述更全）。"""
    d = _parsed()
    assert d["dist"]["2026"] == {4375: 4, 4125: 12, 3875: 2}       # 总和=18
    assert d["dist"]["2027"] == {4375: 8, 4125: 6, 3625: 3, 3125: 1}
    assert d["dist"]["2028"] == {4125: 4, 3875: 5, 3625: 3, 3375: 1, 3125: 4}
    assert d["dist"]["2029"] == {3875: 3, 3625: 7, 3375: 2, 3125: 4, 2875: 1}
    assert len(d["dist"]["longer"]) == 9


def test_parse_median_matches_official_display():
    """自算中位数（累计过半档位）与官方一位小数显示等价。"""
    d = _parsed()
    assert d["median"]["2026"] == 4125      # 官方显示 4.1
    assert d["median"]["2027"] == 4125
    assert d["median"]["2028"] == 3875      # 4.0→官方 3.9 显示档位 3.875
    assert d["median"]["2029"] == 3625
    assert d["participants"] == 18


def test_parse_econ_medians():
    d = _parsed()
    assert d["sep_econ"]["gdp"]["2026"] == 2.3
    assert d["sep_econ"]["unemployment"]["2026"] == 4.1
    assert d["sep_econ"]["pce"]["2026"] == 3.7
    assert d["sep_econ"]["core_pce"]["2026"] == 3.4    # 行名带角标的前缀匹配


def test_parse_missing_table_returns_none():
    assert sep_fetch.parse_sep_html("<html><body>no tables</body></html>",
                                    "20260101") is None


def test_static_data_self_consistent():
    assert fed_sep.validate(fed_sep.DATA) == []


def test_cache_preferred_over_static(tmp_path, monkeypatch):
    """缓存优先：有效缓存 > 静态兜底；无效缓存回落静态。"""
    import json
    fake = dict(fed_sep.DATA)
    fake["meeting"] = "2026-12-16"
    cache = tmp_path / "sep_cache.json"
    cache.write_text(json.dumps(fake, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sep_fetch, "cache_path", lambda: cache)
    assert fed_sep.get_current()["meeting"] == "2026-12-16"
    # 坏缓存（分布异常）→ 回落静态
    bad = dict(fake)
    bad["dist"] = {"2026": {"4125": 99}}
    cache.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    assert fed_sep.get_current()["meeting"] == fed_sep.DATA["meeting"]


def test_needs_check_throttle():
    from datetime import datetime, timedelta
    assert sep_fetch.needs_check(None) is True
    assert sep_fetch.needs_check(
        (datetime.now() - timedelta(hours=1)).isoformat()) is False
    assert sep_fetch.needs_check(
        (datetime.now() - timedelta(hours=13)).isoformat()) is True


def test_parse_four_column_window_period():
    """跨期健壮性（智者 M1）：3/6 月期只有 3 年+longer（4 列），不得解析失败。"""
    html = (Path(__file__).parent / "fixtures" / "sep_20260318.html"
            ).read_text(encoding="utf-8")
    d = sep_fetch.parse_sep_html(html, "20260318")
    assert d and d["participants"] >= 12
    assert set(d["dist"].keys()) == {"2026", "2027", "2028", "longer"}
    assert d["median"]["2026"] and d["median"]["longer"]
    # econ 年份键随窗口（2026/2027/2028/longer，无 2029）
    assert d["sep_econ"]["gdp"]["2026"] == 2.4
    assert "2029" not in d["sep_econ"]["gdp"]


def test_parse_year_alignment_seven_periods():
    """智者 M2 回归：econ 年份键与官方 Median 行逐年吻合（错位 1-2 年的根治）。"""
    cases = {  # 期 -> {年: GDP 中位}（官方 Median 行原值，期望值经独立实测）
        "20241218": {"2024": 2.5, "2025": 2.1, "2026": 2.0, "2027": 1.9,
                     "longer": 1.8},
        "20250319": {"2025": 1.7, "2026": 1.8, "2027": 1.8, "longer": 1.8},
        "20250618": {"2025": 1.4, "2026": 1.6, "2027": 1.8, "longer": 1.8},
        "20250917": {"2025": 1.6, "2026": 1.8, "2027": 1.9, "2028": 1.8,
                     "longer": 1.8},
        "20251210": {"2025": 1.7, "2026": 2.3, "2027": 2.0, "2028": 1.9,
                     "longer": 1.8},
        "20260318": {"2026": 2.4, "2027": 2.3, "2028": 2.1, "longer": 2.0},
        "20260617": {"2026": 2.2, "2027": 2.3, "2028": 2.2, "longer": 2.0},
    }
    import re
    hd = Path(__file__).parent / "fixtures" / "hist"
    assert hd.exists(), "历史期 HTML 未入库（契约件不得静默跳过）"
    for d, expect in cases.items():
        f = hd / f"sep{d}.html"
        assert f.exists(), f"历史件缺失: {f}"
        r = sep_fetch.parse_sep_html(
            f.read_text(encoding="utf-8", errors="ignore"), d)
        assert r["sep_econ"].get("gdp") == expect, d

