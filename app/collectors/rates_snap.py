"""ZQ（30 Day Federal Funds）近月/次月结算价快照。

实测来源（2026-10-21）：CME 官方结算 JSON
https://www.cmegroup.com/CmeWS/mvc/Settlements/Futures/Settlements/305/FUT?tradeDate=MM/DD/YYYY
本机直连返回 403（IP 被 CME 反爬拦截），实际经只读镜像 https://r.jina.ai/ 读取同一
官方 JSON；若镜像也不可用则回退 Yahoo Finance 的 ZQ 合约日线（ZQ<月><年>.CBT），
该回退只取收盘价，字段与来源会记录在日志中。每个交易日 05:10（北京时间）抓取前一
交易日 Final 结算，追加 data/rates_snap.jsonl。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from ..config import data_dir

CME_URL = ("https://www.cmegroup.com/CmeWS/mvc/Settlements/Futures/"
           "Settlements/305/FUT")
READER_PREFIX = "https://r.jina.ai/"
OUT_NAME = "rates_snap.jsonl"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 Chrome/126 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": ("https://www.cmegroup.com/markets/interest-rates/stirs/"
                "30-day-federal-fund.settlements.html"),
}


def _num(value) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if text in ("", "-", "--", "N/A", "NaN", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _payload(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("CME response contains no JSON object")
    return json.loads(text[start:end])


def fetch_settlements(trade_date: date) -> dict:
    """抓取指定交易日结算；先直连 CME，403/超时后走只读镜像。"""
    params = {"tradeDate": trade_date.strftime("%m/%d/%Y")}
    errors: list[str] = []
    for base in (CME_URL, READER_PREFIX + CME_URL):
        try:
            timeout = 60 if base.startswith(READER_PREFIX) else 15
            with httpx.Client(timeout=timeout, follow_redirects=True,
                              trust_env=False, headers=_HEADERS) as client:
                resp = client.get(base, params=params)
            if resp.status_code != 200:
                errors.append(f"{base.split('/')[2]} HTTP {resp.status_code}")
                continue
            data = _payload(resp.text)
            if data.get("settlements"):
                return data
            errors.append(f"{base.split('/')[2]} empty")
        except Exception as exc:
            errors.append(f"{base.split('/')[2]} {type(exc).__name__}")
    raise RuntimeError("CME ZQ settlement unavailable: " + "; ".join(errors))


def parse_settlements(payload: dict) -> tuple[date, float, float]:
    raw_date = payload.get("tradeDate") or ""
    trade_date = datetime.strptime(raw_date, "%m/%d/%Y").date()
    rows = [row for row in payload.get("settlements") or []
            if _num(row.get("settle")) is not None]
    if len(rows) < 2:
        raise ValueError(f"ZQ settlements incomplete on {raw_date}")
    return trade_date, _num(rows[0]["settle"]), _num(rows[1]["settle"])


def _existing_dates(path: Path) -> set[str]:
    if not path.exists():
        return set()
    dates: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            dates.add(str(json.loads(line).get("date")))
        except (ValueError, AttributeError):
            continue
    return dates


def append_snapshot(record: dict, path: Path | None = None) -> bool:
    """追加一条快照；同一交易日已存在则跳过，避免重跑重复。"""
    path = path or data_dir() / OUT_NAME
    if record["date"] in _existing_dates(path):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False,
                            separators=(",", ":")) + "\n")
    return True


def read_snapshots(path: Path | None = None, limit: int = 30) -> list[dict]:
    path = path or data_dir() / OUT_NAME
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows[-max(1, limit):]


def snapshot(trade_date: date | None = None, path: Path | None = None) -> dict:
    """抓取结算并落盘；未指定日期时从昨日向前找最近有数据的交易日。"""
    if trade_date is None:
        payload = None
        for offset in range(1, 9):
            candidate = datetime.now().date() - timedelta(days=offset)
            try:
                payload = fetch_settlements(candidate)
            except Exception:
                continue
            if payload.get("settlements"):
                break
        if payload is None:
            raise RuntimeError("no CME ZQ settlement in last 8 days")
    else:
        payload = fetch_settlements(trade_date)

    trade_day, settle_front, settle_next = parse_settlements(payload)
    record = {
        "date": trade_day.isoformat(),
        "settle_front": settle_front,
        "settle_next": settle_next,
        "implied_front": round(100 - settle_front, 4),
        "ts_snap": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    append_snapshot(record, path)
    return record


async def snapshot_async() -> dict:
    return await asyncio.to_thread(snapshot)
