"""宏观日历采集器：前瞻事件 + 公布后实际值，落独立 data/macro.db。

数据源（本机实测 20261021）：
- 主源：akshare.news_economic_baidu（百度股市通，列含 日期/时间/地区/事件/公布/预期/前值）。
  7 日窗口实测 509 条，美国 257 条、其中 9 条带预期；中国事件在历史窗口带预期。
- 备源：akshare.macro_info_ws（华尔街见闻），主源失败或无数据时兜底。
- FOMC 会议日程来自美联储官网 fomccalendars.htm（2026 日程已核对），SEP 季标记
  来自同页 Projection Materials 链接；官网不可达时用内置 2026 日程静默兜底。

PIT 纪律：actual 首次写入后，forecast/prev 不再改写；actual 修订只更新 actual 并写
revised_at，避免回测看到“事后修正的预期”。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
from datetime import date as date_t
from datetime import datetime, time as time_t, timedelta
from pathlib import Path

from ..config import data_dir

_AK = None


def _ak():
    global _AK
    if _AK is None:
        import akshare as ak
        _AK = ak
    return _AK

log = logging.getLogger("collector")

DB_NAME = "macro.db"
SOURCE_BAIDU = "baidu_calendar"
SOURCE_WSCN = "wallstreetcn_calendar"
SOURCE_FED = "fed_fomc"
FETCH_TIMEOUT = 30

_COUNTRY = {
    "美国": "US", "中国": "CN", "欧元区": "EU", "日本": "JP", "英国": "UK",
    "德国": "DE", "法国": "FR", "瑞士": "CH", "加拿大": "CA",
    "澳大利亚": "AU", "新西兰": "NZ", "韩国": "KR", "新加坡": "SG",
    "印度": "IN", "巴西": "BR", "俄罗斯": "RU", "南非": "ZA",
    "墨西哥": "MX", "沙特阿拉伯": "SA", "土耳其": "TR", "阿根廷": "AR",
    "印度尼西亚": "ID", "越南": "VN", "中国香港": "HK", "中国台湾": "TW",
}
_UNIT_RE = re.compile(r"[（(]([^（）()]*)[）)]\s*$")
_PERIOD_RE = re.compile(
    r"(?:(\d{4})年)?(\d{1,2}月|第[一二三四]季度|Q[1-4]|上半年|下半年|全年)")

# 美联储 FOMC 日程（官网 fomccalendars.htm 20261022 核对）。
# (月, 开始日, 结束日, 是否发布 SEP)；声明时间为结束日 14:00 美东，
# 换算北京时间为次日 02:00（夏令时）或 03:00（冬令时）。
_FOMC_SCHEDULE = {
    2026: (
        (1, 27, 28, False), (3, 17, 18, True), (4, 28, 29, False),
        (6, 16, 17, True), (7, 28, 29, False), (9, 15, 16, True),
        (10, 27, 28, False), (12, 8, 9, True),
    ),
    2027: (
        (1, 26, 27, False), (3, 16, 17, True), (4, 27, 28, False),
        (6, 8, 9, True), (7, 27, 28, False), (9, 14, 15, True),
        (10, 26, 27, False), (12, 7, 8, True),
    ),
}
_FOMC_WARNED: set[int] = set()


def _s(v) -> str:
    s = "" if v is None else str(v).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def _num(v) -> float | None:
    s = _s(v).replace(",", "").replace("%", "")
    if not s or s in ("-", "--", "—", "待定"):
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def _unit(event: str) -> str | None:
    m = _UNIT_RE.search(event)
    return m.group(1).strip() if m else None


def _period(event: str) -> str | None:
    m = _PERIOD_RE.search(event)
    return m.group(0) if m else None


def _country(raw: str) -> str:
    raw = raw.strip()
    return _COUNTRY.get(raw, raw.upper() if raw.isascii() else raw)


def _date(v, fallback: str | None = None) -> date_t | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date_t):
        return v
    s = _s(v) or (fallback or "")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(s[:10] if fmt != "%Y%m%d" else s[:8], fmt).date()
        except ValueError:
            continue
    return None


def _time(v) -> time_t:
    if isinstance(v, datetime):
        return v.time()
    if isinstance(v, time_t):
        return v
    s = _s(v)
    if " " in s:
        s = s.split(" ", 1)[1]
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return time_t(0, 0)


def _ts(row: dict, fallback: str | None) -> datetime | None:
    raw = row.get("时间")
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None)
    d = _date(row.get("日期"), fallback)
    if d is None:
        d = _date(raw, fallback)
    if d is None:
        return None
    return datetime.combine(d, _time(raw))


def _event_id(source: str, country: str, metric: str, period: str | None,
              ts: datetime) -> str:
    key = f"{source}|{country}|{metric}|{period or ts.date().isoformat()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def normalize_rows(rows, source: str, fallback_date: str | None = None,
                   now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    created = now.isoformat(timespec="seconds")
    out: list[dict] = []
    for r in rows:
        event = _s(r.get("事件"))
        country_raw = _s(r.get("地区"))
        if not event or not country_raw:
            continue
        ts = _ts(r, fallback_date)
        if ts is None:
            continue
        country = _country(country_raw)
        actual = _num(r.get("公布") if r.get("公布") is not None else r.get("今值"))
        item = {
            "event_id": _event_id(source, country, event, _period(event), ts),
            "ts_release": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "country": country,
            "metric": event,
            "period": _period(event),
            "prev": _num(r.get("前值")),
            "forecast": _num(r.get("预期")),
            "actual": actual,
            "unit": _unit(event),
            "revised_at": None,
            "source": source,
            "created": created,
        }
        out.append(item)
    return out


def _fetch_baidu(day: str) -> list[dict]:
    df = _ak().news_economic_baidu(date=day)
    return normalize_rows(df.to_dict("records"), SOURCE_BAIDU, fallback_date=day)


def _fetch_wscn(day: str) -> list[dict]:
    df = _ak().macro_info_ws(date=day)
    return normalize_rows(df.to_dict("records"), SOURCE_WSCN, fallback_date=day)


def fetch_day(day: str) -> list[dict]:
    """拉单日（YYYYMMDD）；主源失败或无数据时切备源，两源都失败返回空。"""
    try:
        rows = _fetch_baidu(day)
        if rows:
            return rows
    except Exception as e:
        log.warning("[calendar] baidu %s 失败: %s", day, e)
    try:
        return _fetch_wscn(day)
    except Exception as e:
        log.warning("[calendar] wscn %s 失败: %s", day, e)
        return []


def fomc_missing_years(start_year: int, end_year: int) -> list[int]:
    """返回静态日程未覆盖的年份，供接口显式提示，避免只写日志。"""
    return [y for y in range(start_year, end_year + 1)
            if y not in _FOMC_SCHEDULE]


def fomc_events(start: date_t, end: date_t) -> list[dict]:
    out: list[dict] = []
    created = datetime.now().isoformat(timespec="seconds")
    for year in range(start.year, end.year + 1):
        meetings = _FOMC_SCHEDULE.get(year)
        if meetings is None:
            if year not in _FOMC_WARNED:
                _FOMC_WARNED.add(year)
                log.warning("[calendar] 缺 %d 年 FOMC 日程，请更新 _FOMC_SCHEDULE", year)
            continue
        for month, d1, d2, sep in meetings:
            final = date_t(year, month, d2)
            ts = datetime.combine(final + timedelta(days=1),
                                  time_t(2 if 3 <= month <= 10 else 3, 0))
            if not (start <= final <= end):
                continue
            period = f"{year}-{month:02d}"
            for metric in (["FOMC利率决议"]
                           + (["FOMC经济预测摘要(SEP)点阵图"] if sep else [])):
                out.append({
                    "event_id": _event_id(SOURCE_FED, "US", metric, period, ts),
                    "ts_release": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "country": "US", "metric": metric, "period": period,
                    "prev": None, "forecast": None, "actual": None,
                    "unit": None, "revised_at": None, "source": SOURCE_FED,
                    "created": created,
                })
    return out


async def fetch_calendar(days_back: int = 0, days_ahead: int = 7,
                         today: date_t | None = None,
                         timeout: float = FETCH_TIMEOUT) -> list[dict]:
    base = today or datetime.now().date()
    out: list[dict] = []
    seen: set[str] = set()
    for offset in range(-days_back, days_ahead + 1):
        d = base + timedelta(days=offset)
        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(fetch_day, d.strftime("%Y%m%d")), timeout=timeout)
        except Exception as e:
            log.warning("[calendar] %s 拉取失败: %s", d, e)
            continue
        for row in rows:
            if row["event_id"] not in seen:
                seen.add(row["event_id"])
                out.append(row)
    for row in fomc_events(base - timedelta(days=days_back),
                           base + timedelta(days=days_ahead)):
        if row["event_id"] not in seen:
            seen.add(row["event_id"])
            out.append(row)
    return out


def db_path(path=None) -> Path:
    return Path(path) if path else data_dir() / DB_NAME


_SCHEMA = """
CREATE TABLE IF NOT EXISTS macro_events (
  event_id TEXT PRIMARY KEY,
  ts_release TEXT NOT NULL,
  country TEXT NOT NULL,
  metric TEXT NOT NULL,
  period TEXT,
  prev REAL, forecast REAL, actual REAL,
  unit TEXT, revised_at TEXT,
  source TEXT, created TEXT
);
CREATE INDEX IF NOT EXISTS idx_macro_events_ts ON macro_events(ts_release);
"""


def connect(path=None) -> sqlite3.Connection:
    p = db_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


_UPSERT = """
INSERT INTO macro_events
  (event_id, ts_release, country, metric, period, prev, forecast, actual,
   unit, revised_at, source, created)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(event_id) DO UPDATE SET
  ts_release=excluded.ts_release,
  country=excluded.country,
  metric=excluded.metric,
  period=COALESCE(excluded.period, macro_events.period),
  prev=CASE WHEN macro_events.actual IS NULL AND excluded.actual IS NULL
            THEN COALESCE(excluded.prev, macro_events.prev)
            ELSE macro_events.prev END,
  forecast=CASE WHEN macro_events.actual IS NULL AND excluded.actual IS NULL
                THEN COALESCE(excluded.forecast, macro_events.forecast)
                ELSE macro_events.forecast END,
  actual=COALESCE(excluded.actual, macro_events.actual),
  unit=COALESCE(excluded.unit, macro_events.unit),
  revised_at=CASE WHEN macro_events.actual IS NOT NULL
                   AND excluded.actual IS NOT NULL
                   AND excluded.actual != macro_events.actual
                  THEN excluded.created ELSE macro_events.revised_at END,
  source=excluded.source
"""


def upsert_events(events: list[dict], path=None) -> int:
    if not events:
        return 0
    with connect(path) as conn:
        before = conn.total_changes
        conn.executemany(_UPSERT, [
            (e["event_id"], e["ts_release"], e["country"], e["metric"],
             e.get("period"), e.get("prev"), e.get("forecast"), e.get("actual"),
             e.get("unit"), e.get("revised_at"), e.get("source"), e.get("created"))
            for e in events
        ])
        return conn.total_changes - before


async def sync_calendar(days_back: int = 0, days_ahead: int = 7, path=None,
                        today: date_t | None = None) -> tuple[int, int]:
    rows = await fetch_calendar(days_back=days_back, days_ahead=days_ahead, today=today)
    changed = await asyncio.to_thread(upsert_events, rows, path)
    return len(rows), changed


def read_events(path=None, *, country: str | None = None,
                since: str | None = None, until: str | None = None,
                limit: int = 100) -> list[sqlite3.Row]:
    where: list[str] = []
    args: list = []
    if country:
        countries = [c.strip() for c in country.split(",") if c.strip()]
        if len(countries) == 1:
            where.append("country = ?")
            args.append(countries[0])
        elif countries:
            where.append(f"country IN ({','.join('?' * len(countries))})")
            args.extend(countries)
    if since:
        where.append("ts_release >= ?")
        args.append(since)
    if until:
        where.append("ts_release <= ?")
        args.append(until)
    sql = "SELECT * FROM macro_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts_release, event_id LIMIT ?"
    args.append(limit)
    with connect(path) as conn:
        return conn.execute(sql, args).fetchall()
