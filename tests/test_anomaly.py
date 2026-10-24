"""盘口异动采集测试：行级容错（列漂移只丢单行，不炸整轮采集）。"""
import asyncio

import pandas as pd

from app.collectors.anomaly import AnomalyTracker, fetch_anomaly


def test_fetch_anomaly_survives_missing_time_column(monkeypatch):
    """回归A1-2：'时间'列漂移时旧行级 try 没兜住（取列在 try 外），KeyError 轮轮炸。"""
    def fake_df(symbol):
        return pd.DataFrame([{"代码": "600519", "名称": "贵州茅台"}])  # 缺"时间"列
    monkeypatch.setattr("app.collectors.anomaly.ak.stock_changes_em", fake_df)
    tracker = AnomalyTracker()
    fresh = asyncio.run(fetch_anomaly(tracker, ["火箭发射"]))
    assert fresh == []


def test_fetch_anomaly_parses_rows(monkeypatch):
    def fake_df(symbol):
        return pd.DataFrame([{"代码": "600519", "名称": "贵州茅台", "时间": "10:01:02"}])
    monkeypatch.setattr("app.collectors.anomaly.ak.stock_changes_em", fake_df)
    tracker = AnomalyTracker()
    fresh = asyncio.run(fetch_anomaly(tracker, ["火箭发射"]))
    assert len(fresh) == 1
    assert fresh[0]["code"] == "600519" and fresh[0]["time"] == "10:01:02"
    assert tracker.name2code["贵州茅台"] == "600519"
