"""FOMC 官方 SEP 数据测试：静态自洽 + get_current 链路 + API 形状。"""
from pathlib import Path

from app import fed_sep, sep_fetch


def test_validate_clean():
    assert fed_sep.validate(fed_sep.DATA) == []


def test_distribution_and_median_consistency():
    """分布列和 ≤19；中位数 = 累计过半档位（与官方显示 4.1/4.1/3.9/3.6 等价）。"""
    d = fed_sep.DATA
    for y, dd in d["dist"].items():
        assert 0 < sum(dd.values()) <= 19, y
    assert sum(d["dist"]["2026"].values()) == d["participants"] == 18
    med = d["median"]
    assert med["2026"] == med["2027"] == 4125      # 两年持平="higher for longer"
    assert med["2028"] == 3875 and med["2029"] == 3625   # 2028 起下行
    assert med["longer"] == 3250


def test_econ_projection_keys():
    se = fed_sep.DATA["sep_econ"]
    for k in ("gdp", "unemployment", "pce", "core_pce"):
        assert "2026" in se[k], k


def test_api_sep_shape(tmp_path, monkeypatch):
    """无缓存时 API 返回静态兜底（monkeypatch 隔离真实缓存）。"""
    monkeypatch.setattr(sep_fetch, "cache_path",
                        lambda: tmp_path / "no_cache.json")
    from fastapi.testclient import TestClient
    from app.pipeline import Pipeline
    from app.server import create_app
    from app.store import Store
    client = TestClient(create_app(Store(tmp_path / "t.db"),
                                   Pipeline(Store(tmp_path / "t.db"))))
    r = client.get("/api/sep").json()
    assert r["meeting"] == "2026-09-16"
    assert r["participants"] == 18 and r["dist"]["2026"]
    assert "source" in r and "notes" in r


def test_get_current_fills_decision_from_static_when_same_meeting(tmp_path, monkeypatch):
    """官网缓存缺决策串：与静态同期 → 补上（点阵可得利率基准）；跨期 → 留空降级。"""
    import json

    from app.dotplot import base_from_sep
    cache = {**fed_sep.DATA, "origin": "fetched"}
    cache.pop("decision")
    p = tmp_path / "sep_cache.json"
    p.write_text(json.dumps(cache), encoding="utf-8")
    monkeypatch.setattr(sep_fetch, "cache_path", lambda: p)

    cur = fed_sep.get_current()
    assert cur["decision"] == fed_sep.DATA["decision"]
    assert base_from_sep(cur) and base_from_sep(cur)["mid"] == 3875

    p.write_text(json.dumps({**cache, "meeting": "2026-12-16"}),
                 encoding="utf-8")
    cur2 = fed_sep.get_current()
    assert cur2["meeting"] == "2026-12-16" and not cur2.get("decision")
    assert base_from_sep(cur2) is None
