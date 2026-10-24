"""盘口异动采集 + 代码<->名称缓存。

异动本身不进入主信息流（噪音太多），而是：
1. 聚合为右侧栏的"异动风向"统计；
2. 提供近30分钟异动代码集合，供打分引擎做"价格先动+传闻文本"关联加分。
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, time as dtime

import akshare as ak

log = logging.getLogger("collector")


class AnomalyTracker:
    def __init__(self):
        self.events: deque[dict] = deque(maxlen=5000)  # {time, code, name, type}
        self.name2code: dict[str, str] = {}
        self.code2name: dict[str, str] = {}

    def recent_codes(self, minutes: int = 30) -> set[str]:
        now = datetime.now()
        horizon = now.timestamp() - minutes * 60
        codes = set()
        for e in self.events:  # deque 按时间序，倒序更快但量小无所谓
            if e["_ts"] >= horizon:
                codes.add(e["code"])
        return codes

    def summary(self, minutes: int = 30, top: int = 15) -> list[dict]:
        """近N分钟按代码聚合的异动次数（右侧栏展示）。"""
        horizon = datetime.now().timestamp() - minutes * 60
        agg: dict[str, dict] = {}
        for e in self.events:
            if e["_ts"] < horizon:
                continue
            a = agg.setdefault(e["code"], {"code": e["code"], "name": e["name"],
                                           "count": 0, "types": set()})
            a["count"] += 1
            a["types"].add(e["type"])
        out = sorted(agg.values(), key=lambda x: -x["count"])[:top]
        for a in out:
            a["types"] = "、".join(sorted(a["types"]))
        return out

    def register_name(self, code: str, name: str):
        if code and name:
            self.code2name[code] = name
            self.name2code[name] = code

    def codes_in_text(self, text: str) -> set[str]:
        """文本里出现的异动标的（按名称匹配，用于关联加分）。"""
        return {c for n, c in self.name2code.items() if n and n in text}


def in_trading_hours(now: datetime | None = None) -> bool:
    """A股交易时段（粗略：工作日 9:15-11:35 / 12:55-15:10），节假日会白跑一轮但无害。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (dtime(9, 15) <= t <= dtime(11, 35)) or (dtime(12, 55) <= t <= dtime(15, 10))


async def fetch_anomaly(tracker: AnomalyTracker, types: list[str]) -> list[dict]:
    """轮询各类型盘口异动，注册代码名称，返回新增事件。"""
    fresh: list[dict] = []
    now_ts = datetime.now().timestamp()
    for typ in types:
        try:
            df = await asyncio.wait_for(
                asyncio.to_thread(ak.stock_changes_em, symbol=typ), timeout=45)
        except Exception as e:
            log.warning("盘口异动[%s]拉取失败: %s", typ, e)
            continue
        for _, r in df.iterrows():
            # 行级容错覆盖所有取列动作：列漂移只丢单行，不能炸整轮采集
            try:
                code, name = str(r["代码"]).zfill(6), str(r["名称"])
                ev_time = str(r["时间"])
            except Exception as e:
                log.debug("盘口异动[%s]单行解析失败: %s", typ, e)
                continue
            tracker.register_name(code, name)
            ev = {"time": ev_time, "code": code, "name": name, "type": typ,
                  "_ts": now_ts}
            fresh.append(ev)
    tracker.events.extend(fresh)
    return fresh


async def fetch_ztpool(tracker: AnomalyTracker) -> int:
    """当日涨停股池：主要用来补充代码名称缓存。返回涨停家数。"""
    date = datetime.now().strftime("%Y%m%d")
    try:
        df = await asyncio.wait_for(
            asyncio.to_thread(ak.stock_zt_pool_em, date=date), timeout=45)
    except Exception as e:
        log.debug("涨停池拉取失败(可能非交易日): %s", e)
        return -1
    for _, r in df.iterrows():
        tracker.register_name(str(r["代码"]).zfill(6), str(r["名称"]))
    return len(df)
