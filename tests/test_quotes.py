"""S3 行情快照测试：缓存/降级/口径（mock fetch，全程不触网）。"""
import math
from datetime import datetime

import pandas as pd

from app.quotes import (STALE_TTL_SECONDS, TARGETS, Quotes, _hist_close_pct,
                        _pct, _sina_quote)


def test_pct_helper():
    assert _pct(110, 100) == 10.0
    assert _pct(100, 100) == 0.0
    assert _pct(99, 100) == -1.0
    assert _pct(100, 0) is None          # prev=0 防除零
    assert _pct(float("nan"), 100) is None
    assert _pct(100, float("nan")) is None


def test_hist_close_pct_skips_nan_rows():
    df = pd.DataFrame({"close": [100.0, float("nan"), 105.0, 102.9]})
    last, chg = _hist_close_pct(df)
    assert last == 102.9
    assert chg == round((102.9 - 105.0) / 105.0 * 100, 2)


def test_quotes_refresh_cache_and_degrade():
    """成功覆盖缓存 / 异常与 NaN 整格隐藏（降级铁律：单标的失败不影响其余）。"""
    def boom():
        raise RuntimeError("down")
    targets = [
        {"key": "a", "name": "A", "fetch": lambda: (100.0, 1.5)},
        {"key": "b", "name": "B", "fetch": boom},
        {"key": "c", "name": "C", "fetch": lambda: (float("nan"), None)},
    ]
    q = Quotes(targets)
    assert q.snapshot() == []
    assert q.stale() is True
    assert q.refresh() == 1
    snap = q.snapshot()
    assert [x["key"] for x in snap] == ["a"]
    assert snap[0]["chg_pct"] == 1.5
    assert snap[0]["price"] == 100.0
    assert q.stale() is False
    assert q.begin_refresh() is True and q.begin_refresh() is False  # 并发护栏
    q.end_refresh()
    assert q.begin_refresh() is True


def test_quotes_keeps_last_good_value_and_throttles_slow_source():
    """慢源 60s 内不重复拉；刷新失败 → 保留上次成功值（行情条不闪空）。"""
    state = {"fail": False, "calls": 0}
    def fetch():
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("net down")
        return (10.0, 0.5)
    q = Quotes([{"key": "x", "name": "X", "fetch": fetch}])
    q.refresh()
    q.refresh()
    assert state["calls"] == 1
    state["fail"] = True
    q._slow_ts = 0
    q.refresh()
    assert state["calls"] == 2
    q._slow_ts = 0
    q.refresh()                       # 失败退避期内不再打源
    assert state["calls"] == 2
    snap = q.snapshot()
    assert len(snap) == 1 and snap[0]["price"] == 10.0 and snap[0]["chg_pct"] == 0.5
    st = q.status()
    assert st["stale"] is False and st["age_seconds"] is not None
    q._ts -= STALE_TTL_SECONDS + 1
    assert q.status()["stale"] is True


def test_sina_quote_realtime_fields():
    # 源日期用动态当天：_fmt_ts 跨日显示「月日 时分」，写死日期隔天必挂
    today = datetime.now().strftime("%Y-%m-%d")
    sh = ["上证指数", "3714.47", "3713.0277", "3678.7146", "3714.47",
          "3649.86"] + [""] * 24 + [today, "14:08:41", "00"]
    price, chg, ts = _sina_quote("sh000001", sh)
    assert price == 3678.7146
    assert chg == round((3678.7146 - 3713.0277) / 3713.0277 * 100, 2)
    assert ts == "14:08:41"

    hk = ["HSI", "恒生指数", "23513.790", "23651.871", "23513.790",
          "22897.320", "22910.090", "-741.781", "-3.136"] + [""] * 9 + [today, "14:08:41"]
    assert _sina_quote("hkHSI", hk)[0] == 22910.09
    assert _sina_quote("hkHSI", hk)[1] == -3.136

    fx = ["14:08:41", "", "", "", "", "", "", "", "6.673600", "离岸人民币",
          "-0.010000", "-0.000100"] + [""] * 5 + [today]
    assert _sina_quote("fx_susdcnh", fx)[0] == 6.6736
    assert _sina_quote("fx_susdcnh", fx)[1] == -0.01


def test_quotes_prefers_sina_batch_and_keeps_cache():
    def fetch():
        raise AssertionError("fallback should not run when sina is present")
    targets = [{"key": "sh", "name": "上证指数", "decimals": 3, "sina": "sh000001",
                "fetch": fetch}]
    f = ["上证指数", "3714.47", "3713.0277", "3678.7146", "3714.47",
         "3649.86"] + [""] * 24 + [datetime.now().strftime("%Y-%m-%d"), "14:08:41", "00"]
    q = Quotes(targets, sina_fetch=lambda codes: {"sh000001": f})
    assert q.refresh() == 1
    assert q.snapshot()[0]["price"] == 3678.715
    assert q.snapshot()[0]["updated"] == "14:08:41"


def test_quotes_batch_fail_backs_off_single_record():
    """批量源整败只记一次退避：退避期内再 refresh 不打源站（日志不刷）；恢复即清。"""
    state = {"calls": 0}

    def boom(codes):
        state["calls"] += 1
        raise RuntimeError("sina down")

    def good(codes):
        state["calls"] += 1
        row = ["上证指数", "3714.47", "3713.0277", "3678.7146", "3714.47",
               "3649.86"] + [""] * 24 + [
            datetime.now().strftime("%Y-%m-%d"), "14:08:41", "00"]
        return {"sh000001": row}

    q = Quotes([{"key": "sh", "name": "上证指数", "decimals": 3,
                 "sina": "sh000001", "fetch": lambda: (1.0, None)}],
               sina_fetch=boom)
    assert q.refresh() == 0
    assert q._batch_fail_streak == 1 and q._batch_fail_until > 0
    q.refresh()                                  # 退避期内：不再打批量源
    assert state["calls"] == 1
    q._batch_fail_until = 0.0                    # 退避到期
    q._sina_fetch = good
    assert q.refresh() == 1
    assert q._batch_fail_streak == 0 and q._batch_fail_until == 0.0
    assert q.snapshot()[0]["price"] == 3678.715


def test_targets_keep_ten_and_use_usdcnh_not_n225():
    keys = [t["key"] for t in TARGETS]
    assert len(keys) == 10
    assert "usdcnh" in keys and "n225" not in keys
    assert next(t for t in TARGETS if t["key"] == "usdcnh")["sina"] == "fx_susdcnh"
    assert not any(t.get("fast") for t in TARGETS)  # KOSPI 也走 60s 慢源
    assert "sina" not in next(t for t in TARGETS if t["key"] == "kospi")


def test_api_quotes_endpoint(tmp_path):
    """/api/quotes 恒回缓存：seeded 缓存直接返回；空缓存 stale=True 不阻塞。"""
    from datetime import datetime
    from pathlib import Path

    from fastapi.testclient import TestClient

    from app.pipeline import Pipeline
    from app.quotes import Quotes
    from app.server import create_app
    from app.store import Store

    store = Store(tmp_path / "t.db")
    pipeline = Pipeline(store)
    seeded = Quotes(targets=[{"key": "t", "name": "测试标的", "fetch": lambda: (1.0, None)}])
    seeded.refresh()
    client = TestClient(create_app(store, pipeline, quotes=seeded))
    r = client.get("/api/quotes").json()
    assert r["stale"] is False
    assert r["refreshing"] is False and r["age_seconds"] is not None
    assert r["quotes"][0]["key"] == "t" and r["quotes"][0]["price"] == 1.0
    # 空缓存实例：stale=True、quotes=[]，端点不触网不阻塞
    empty = Quotes(targets=[])
    client2 = TestClient(create_app(store, pipeline, quotes=empty))
    r2 = client2.get("/api/quotes").json()
    assert r2["quotes"] == [] and r2["stale"] is True
    assert r2["age_seconds"] is None


def test_quotes_snapshot_persistence_roundtrip(tmp_path, monkeypatch):
    """落盘持久化（20261024）：成功刷新→写 data/quotes_snap.json（原子）；
    新实例启动预载旧值（按当前 targets 过滤，配置改标的后旧 key 不复活），
    预载后 stale=True 触发刷新而非当成新鲜数据。"""
    import json

    snap_file = tmp_path / "quotes_snap.json"
    monkeypatch.setattr(Quotes, "_persist_file", lambda self: snap_file)

    q1 = Quotes(targets=[
        {"key": "gold", "name": "黄金", "fetch": lambda: (2000.0, 0.5)},
        {"key": "old", "name": "旧标的", "fetch": lambda: (1.0, None)},
    ])
    assert q1.refresh() == 2
    assert snap_file.exists()
    payload = json.loads(snap_file.read_text(encoding="utf-8"))
    assert payload["saved_at"] > 0 and len(payload["rows"]) == 2

    # 把落盘时间改到 1 小时前，模拟"重启时快照已老"的真实场景
    import time as _time
    payload["saved_at"] = _time.time() - 3600
    snap_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    # 新实例：只认当前 targets 里的 gold——old 被过滤，不产生幽灵卡
    q2 = Quotes(targets=[{"key": "gold", "name": "黄金",
                          "fetch": lambda: (1.0, None)}])
    snap = q2.snapshot()
    assert [x["key"] for x in snap] == ["gold"]
    assert snap[0]["price"] == 2000.0            # 预载的是落盘值
    assert q2.stale() is True                    # 老快照不当新鲜：首个请求即刷新

    # 全部失败的一轮不落盘（ok=0 不写，旧文件保留）
    q3 = Quotes(targets=[{"key": "gold", "name": "黄金",
                          "fetch": lambda: (_ for _ in ()).throw(RuntimeError("x"))}],
                )
    monkeypatch.setattr(Quotes, "_persist_file", lambda self: snap_file)
    before = snap_file.read_text(encoding="utf-8")
    q3._load_persisted()
    assert q3.refresh() == 0
    assert snap_file.read_text(encoding="utf-8") == before
