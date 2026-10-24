"""calendar 采集器测试（全离线：归一化、PIT upsert、FOMC 静态日程）。"""
import asyncio
import sqlite3
from datetime import date, datetime

import app.collectors.calendar as cal

BAIDU_ROW = {
    "日期": "2026-10-14", "时间": "09:30", "地区": "中国",
    "事件": "中国9月CPI年率(%)", "公布": None, "预期": "1.0",
    "前值": "0.8", "重要性": 2,
}


def _item(**kw):
    row = dict(BAIDU_ROW)
    row.update(kw)
    return cal.normalize_rows([row], cal.SOURCE_BAIDU,
                              now=datetime(2026, 10, 14, 10, 0, 0))[0]


def test_normalize_baidu_fields_and_stable_id():
    it = _item()
    assert it["ts_release"] == "2026-10-14 09:30:00"
    assert it["country"] == "CN"
    assert it["period"] == "9月"
    assert it["unit"] == "%"
    assert it["prev"] == 0.8 and it["forecast"] == 1.0
    assert it["actual"] is None
    assert it["event_id"] == _item(预期="9.9")["event_id"]   # 预测变化不改事件身份


def test_normalize_wscn_datetime_time_column():
    rows = [{
        "时间": "2026-10-22 20:30:00", "地区": "美国",
        "事件": "美国10月17日当周首次申请失业救济人数(万人)",
        "今值": None, "预期": "20.0", "前值": "19.9",
    }]
    it = cal.normalize_rows(rows, cal.SOURCE_WSCN)[0]
    assert it["ts_release"] == "2026-10-22 20:30:00"
    assert it["country"] == "US"
    assert it["unit"] == "万人"
    assert it["actual"] is None and it["forecast"] == 20.0


def test_num_cleaning():
    assert cal._num("--") is None
    assert cal._num("1,234.5") == 1234.5
    assert cal._num("0.3%") == 0.3
    assert cal.normalize_rows([], cal.SOURCE_BAIDU) == []


def test_fetch_day_falls_back_to_wscn(monkeypatch):
    def boom(day):
        raise RuntimeError("baidu down")

    monkeypatch.setattr(cal, "_fetch_baidu", boom)
    monkeypatch.setattr(cal, "_fetch_wscn", lambda day: [{"event_id": "w1"}])
    assert cal.fetch_day("20261021") == [{"event_id": "w1"}]


def test_fetch_calendar_dedup_and_fomc(monkeypatch):
    monkeypatch.setattr(cal, "fetch_day", lambda day: [
        {"event_id": f"e{day}", "source": cal.SOURCE_BAIDU}])
    rows = asyncio.run(cal.fetch_calendar(
        days_back=0, days_ahead=7, today=date(2026, 10, 21)))
    ids = [r["event_id"] for r in rows]
    assert len(ids) == len(set(ids)) == 9          # 8 天 + 1 条 FOMC
    assert "e20261021" in ids and "e20261028" in ids
    fomc = [r for r in rows if r["source"] == cal.SOURCE_FED]
    assert fomc and fomc[0]["metric"] == "FOMC利率决议"
    assert fomc[0]["ts_release"] == "2026-10-29 02:00:00"


def test_fomc_sep_marker():
    rows = cal.fomc_events(date(2026, 10, 21), date(2026, 12, 31))
    metrics = [r["metric"] for r in rows]
    assert "FOMC利率决议" in metrics
    assert "FOMC经济预测摘要(SEP)点阵图" in metrics
    dec = next(r for r in rows if r["period"] == "2026-12"
               and "SEP" in r["metric"])
    assert dec["ts_release"] == "2026-12-10 03:00:00"   # 冬令时 14:00 ET -> 次日 03:00


def test_fomc_2027_and_missing_year_warning(caplog):
    """F-206：跨年后 2027 日程与 SEP 标记仍生成；缺 2028 数据告警而非静默。"""
    rows = cal.fomc_events(date(2027, 1, 1), date(2027, 12, 31))
    assert sum(1 for r in rows if r["metric"] == "FOMC利率决议") == 8
    assert sum(1 for r in rows if "SEP" in r["metric"]) == 4
    cal._FOMC_WARNED.discard(2028)
    assert cal.fomc_events(date(2028, 1, 1), date(2028, 12, 31)) == []
    assert cal.fomc_missing_years(2026, 2028) == [2028]
    assert "缺 2028 年 FOMC 日程" in caplog.text


def test_upsert_pit_freezes_forecast_after_actual(tmp_path):
    p = tmp_path / "macro.db"
    assert cal.upsert_events([_item()], p) == 1
    assert cal.upsert_events([_item(预期="1.1")], p) == 1
    row = cal.read_events(p)[0]
    assert row["forecast"] == 1.1

    assert cal.upsert_events([_item(预期="9.9", 公布="0.9")], p) == 1
    row = cal.read_events(p)[0]
    assert row["forecast"] == 1.1          # actual 首次到达时不得用新预测覆盖
    assert row["actual"] == 0.9
    assert row["revised_at"] is None

    assert cal.upsert_events([_item(预期="8.8", 公布="1.0", 前值="0.7")], p) == 1
    row = cal.read_events(p)[0]
    assert row["actual"] == 1.0 and row["revised_at"] is not None
    assert row["forecast"] == 1.1 and row["prev"] == 0.8


def test_schema_and_country_filter(tmp_path):
    p = tmp_path / "macro.db"
    us = _item(地区="美国", 事件="美国9月CPI年率(%)")
    assert cal.upsert_events([_item(), us], p) == 2
    conn = sqlite3.connect(p)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(macro_events)")]
    conn.close()
    assert cols == ["event_id", "ts_release", "country", "metric", "period",
                    "prev", "forecast", "actual", "unit", "revised_at",
                    "source", "created"]
    assert len(cal.read_events(p, country="CN")) == 1
    assert len(cal.read_events(p, country="US")) == 1
    assert len(cal.read_events(p, country="CN,US")) == 2
