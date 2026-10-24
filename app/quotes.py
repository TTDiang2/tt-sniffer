"""行情快照（S3）：行情是状态不是事件——不进 items 表，内存缓存 + 8s TTL。

实时源（20261022 探针）：新浪 hq.sinajs.cn 批量接口。
- sh000001/sh000300：A 股盘中现价；hkHSI：港股现价（延迟约 15-20 分钟）；
- gb_ixic：纳指（美盘时段实时）；hf_GC/hf_OIL：COMEX 金/布伦特；
- fx_susdcnh：USDCNH 离岸人民币。
- 慢源（60s）：KOSPI 用 Naver 指数接口、中美 10Y 用 bond_zh_us_rate（日频）。
日线接口仅作首次兜底：实时源失败且缓存为空时才回退；已有缓存则保留上次成功值，
避免把盘中实时价覆盖成昨收。

降级铁律：逐标的 try/except，单标的失败保留上次成功值；全部失败时快照为空，
前端行情条收缩成一行提示，主功能零影响。
涨跌幅口径：接口自带涨跌幅字段优先；否则现价 vs 昨收/昨结。
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime

_AK = None


def _ak():
    global _AK
    if _AK is None:
        import akshare as ak
        _AK = ak
    return _AK

log = logging.getLogger("quotes")

TTL_SECONDS = 8
SLOW_TTL_SECONDS = 60
STALE_TTL_SECONDS = TTL_SECONDS * 3
FAIL_BACKOFF_SECONDS = (30, 60, 120, 300)
SINA_URL = "https://hq.sinajs.cn/list="
SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn/",
    "User-Agent": "Mozilla/5.0",
}


def _num(v) -> float | None:
    try:
        n = float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return None if math.isnan(n) else n


def _fmt_ts(date_s: str = "", time_s: str = "") -> str | None:
    """源时间 → 前端短标签；当天显示时分秒，跨日显示月日时分。"""
    date_s = (date_s or "").strip().replace("/", "-")
    time_s = (time_s or "").strip()
    if not time_s:
        return None
    if len(time_s) >= 8 and len(date_s) >= 10:
        stamp = f"{date_s[:10]} {time_s[:8]}"
    elif len(date_s) >= 10:
        stamp = f"{date_s[:10]} {time_s[:5]}"
    else:
        return time_s[:8]
    today = datetime.now().strftime("%Y-%m-%d")
    return stamp[11:19] if stamp[:10] == today else stamp[5:16]


def _sina_batch(codes: list[str], timeout: float = 5.0) -> dict[str, list[str]]:
    """批量拉新浪行情；任何网络/解析失败都返回空 dict，由调用方降级。"""
    if not codes:
        return {}
    import httpx

    r = httpx.get(SINA_URL + ",".join(codes), headers=SINA_HEADERS,
                  timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    text = r.content.decode("gbk", errors="replace")
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        left, _, right = line.partition('="')
        code = left.split("hq_str_")[-1].strip()
        if not code or not right:
            continue
        out[code] = right.rstrip('";').split(",")
    return out


def _sina_quote(code: str, f: list[str]) -> tuple[float, float | None, str | None]:
    """新浪字段 → (现价, 涨跌幅, 源时间)。字段差异按品种在此收口。"""
    if code in ("sh000001", "sh000300"):
        price, prev = _num(f[3]), _num(f[2])
        chg = _pct(price, prev) if price and prev else None
        ts = _fmt_ts(f[30] if len(f) > 30 else "", f[31] if len(f) > 31 else "")
    elif code == "hkHSI":
        price, prev = _num(f[6]), _num(f[3])
        chg = _num(f[8]) if len(f) > 8 else None
        if chg is None and price and prev:
            chg = _pct(price, prev)
        ts = _fmt_ts(f[17] if len(f) > 17 else "", f[18] if len(f) > 18 else "")
    elif code == "gb_ixic":
        price, chg = _num(f[1]), _num(f[2]) if len(f) > 2 else None
        ts = _fmt_ts(f[3][:10] if len(f) > 3 else "", f[3][11:19] if len(f) > 3 else "")
    elif code in ("hf_GC", "hf_OIL"):
        price, prev = _num(f[0]), _num(f[7]) if len(f) > 7 else None
        chg = _pct(price, prev) if price and prev else None
        ts = _fmt_ts(f[12] if len(f) > 12 else "", f[6] if len(f) > 6 else "")
    elif code == "fx_susdcnh":
        price = _num(f[8]) if len(f) > 8 else None
        chg = _num(f[10]) if len(f) > 10 else None
        ts = _fmt_ts(f[17] if len(f) > 17 else "", f[0] if f else "")
    else:
        raise LookupError(f"unsupported sina code: {code}")
    if price is None or price <= 0:
        raise ValueError(f"sina {code} bad price: {price}")
    return price, chg, ts


def _pct(last: float, prev: float) -> float | None:
    """较前一收涨跌幅（%）。prev<=0 或非数 → None（整格隐藏涨跌幅）。"""
    if prev is None or last is None:
        return None
    if math.isnan(prev) or math.isnan(last) or prev == 0:
        return None
    return round((last - prev) / prev * 100, 2)


def _hist_close_pct(df, col: str = "close") -> tuple[float, float | None]:
    """日线序列 → (最新收盘, 较前收 %)。跳过 NaN 行取最后两个有效收盘。"""
    s = df[col].dropna()
    last = float(s.iloc[-1])
    prev = float(s.iloc[-2]) if len(s) >= 2 else None
    return last, _pct(last, prev) if prev is not None else None


def _spot_global(codes: tuple[str, ...], name_kw: tuple[str, ...]):
    """东财全球指数现货：按代码优先、名称关键词兜底定位一行。
    返回 (最新价, 涨跌幅%)——接口自带涨跌幅字段，口径与源一致。接口抖时抛异常。"""
    for attempt in range(2):
        try:
            df = _ak().index_global_spot_em()
            break
        except Exception:
            if attempt:
                raise
            time.sleep(0.5)
    row = None
    for c in codes:
        hit = df[df["代码"].astype(str) == c]
        if len(hit):
            row = hit.iloc[0]
            break
    if row is None:
        for kw in name_kw:
            hit = df[df["名称"].astype(str).str.contains(kw, na=False)]
            if len(hit):
                row = hit.iloc[0]
                break
    if row is None:
        raise LookupError("global spot row not found")
    return float(row["最新价"]), float(row["涨跌幅"])


def _bond_rate(col: str) -> tuple[float, float | None]:
    """中美国债收益率（%）：列内取最后两个非 NaN 值（中国列假期常 NaN）。"""
    df = _ak().bond_zh_us_rate(start_date="20260901")
    s = df[col].dropna()
    last = float(s.iloc[-1])
    prev = float(s.iloc[-2]) if len(s) >= 2 else None
    return last, _pct(last, prev) if prev is not None else None


def _forex_cnh() -> tuple[float, float | None]:
    """USDCNH 兜底（东财外汇现货，接口抖）：找不到直接抛，整格隐藏。"""
    df = _ak().forex_spot_em()
    col_code = df.columns[0]
    hit = df[df[col_code].astype(str).str.upper().str.contains("CNH", na=False)]
    if not len(hit):
        raise LookupError("USDCNH row not found")
    row = hit.iloc[0]
    price_col = next((c for c in ("最新价", "现价", "价格") if c in df.columns), None)
    chg_col = next((c for c in ("涨跌幅", "涨跌幅%") if c in df.columns), None)
    if price_col is None:
        raise LookupError(f"price col not found: {list(df.columns)}")
    price = float(row[price_col])
    chg = float(row[chg_col]) if chg_col is not None and not math.isnan(float(row[chg_col])) else None
    return price, chg


def _naver_index(symbol: str = "KOSPI") -> tuple[float, float | None, str | None]:
    """Naver 指数实时接口（KOSPI 延迟 0），返回价格/涨跌幅/源时间。"""
    import httpx

    r = httpx.get(f"https://m.stock.naver.com/api/index/{symbol}/basic",
                  headers=SINA_HEADERS, timeout=5, follow_redirects=True)
    r.raise_for_status()
    d = r.json()
    price = _num(d.get("closePrice"))
    chg = _num(d.get("fluctuationsRatio"))
    stamp = str(d.get("localTradedAt") or "")
    try:
        local = datetime.fromisoformat(stamp).astimezone()
        ts = _fmt_ts(local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S"))
    except ValueError:
        ts = _fmt_ts(stamp[:10], stamp[11:19])
    return price, chg, ts


# 标的配置矩阵（key → 展示名 + 取数函数；顺序=前端渲染顺序）
# sina 字段存在时优先走实时批量源；fetch 仅首次兜底/慢源。
TARGETS: list[dict] = [
    {"key": "xau", "name": "COMEX黄金", "decimals": 2, "sina": "hf_GC",
     "fetch": lambda: _hist_close_pct(_ak().futures_foreign_hist(symbol="GC"))},
    {"key": "brent", "name": "布伦特原油", "decimals": 2, "sina": "hf_OIL",
     "fetch": lambda: _hist_close_pct(_ak().futures_foreign_hist(symbol="OIL"))},
    {"key": "nasdaq", "name": "纳指", "decimals": 2, "sina": "gb_ixic",
     "fetch": lambda: _hist_close_pct(_ak().index_us_stock_sina(symbol=".IXIC"))},
    {"key": "sh", "name": "上证指数", "decimals": 3, "sina": "sh000001",
     "fetch": lambda: _hist_close_pct(_ak().stock_zh_index_daily(symbol="sh000001"))},
    {"key": "hs300", "name": "沪深300", "decimals": 3, "sina": "sh000300",
     "fetch": lambda: _hist_close_pct(_ak().stock_zh_index_daily(symbol="sh000300"))},
    {"key": "hsi", "name": "恒生指数", "decimals": 2, "sina": "hkHSI",
     "fetch": lambda: _hist_close_pct(_ak().stock_hk_index_daily_sina(symbol="HSI"))},
    {"key": "usdcnh", "name": "USDCNH", "decimals": 4, "sina": "fx_susdcnh",
     "fetch": _forex_cnh},
    {"key": "kospi", "name": "KOSPI", "decimals": 2,
     "fetch": _naver_index},
    {"key": "cn10y", "name": "中债10Y", "decimals": 4,
     "fetch": lambda: _bond_rate("中国国债收益率10年")},
    {"key": "us10y", "name": "美债10Y", "decimals": 4,
     "fetch": lambda: _bond_rate("美国国债收益率10年")},
]


class Quotes:
    """行情快照缓存。refresh() 阻塞（在 to_thread 里跑），snapshot() 永不阻塞。

    落盘持久化（20261024）：每次成功刷新把快照原子写 data/quotes_snap.json，
    启动时预载——生产重启恰逢源接口故障时首屏是旧值而非空卡（stale 标记
    照常亮，前端仍展示数值）。
    """

    def __init__(self, targets: list[dict] | None = None, *, sina_fetch=None):
        self._targets = targets if targets is not None else TARGETS
        self._sina_fetch = sina_fetch or _sina_batch
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        self._ts: float = 0.0
        self._slow_ts: float = 0.0
        self._refreshing = False
        self._fail_until: dict[str, float] = {}
        self._fail_streak: dict[str, int] = {}
        self._batch_fail_until: float = 0.0
        self._batch_fail_streak: int = 0
        self._load_persisted()

    # ---------- 落盘持久化 ----------
    def _persist_file(self):
        from .config import data_dir
        return data_dir() / "quotes_snap.json"

    def _load_persisted(self) -> None:
        try:
            data = json.loads(self._persist_file().read_text(encoding="utf-8"))
            rows = data.get("rows") or []
            saved_at = float(data.get("saved_at") or 0)
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001  损坏文件不致命：按无快照冷启
            log.warning("quotes: 落盘快照读取失败: %r", e)
            return
        if not isinstance(rows, list) or saved_at <= 0:
            return
        # 按当前 targets 过滤：配置改标的后旧 key 不进缓存（幽灵卡），
        # 也让用自定义 targets 的实例（测试）不串生产快照
        allowed = {x["key"] for x in self._targets if isinstance(x, dict)}
        loaded = 0
        with self._lock:
            for r in rows:
                if isinstance(r, dict) and r.get("key") in allowed:
                    self._cache[r["key"]] = r
                    loaded += 1
            if loaded:
                self._ts = saved_at     # stale() 会判真→首个请求即触发刷新
        if loaded:
            log.info("quotes: 预载落盘快照 %d 标的（age %.0fs）",
                     loaded, max(0.0, time.time() - saved_at))

    def _persist(self) -> None:
        try:
            with self._lock:
                rows = [dict(v) for v in self._cache.values()]
            path = self._persist_file()
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"saved_at": time.time(), "rows": rows},
                                      ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)      # 原子替换：不会读到半截文件
        except Exception as e:  # noqa: BLE001
            log.warning("quotes: 快照落盘失败: %r", e)

    def stale(self) -> bool:
        """是否需要发起刷新（内部节流用，不代表快照不可展示）。"""
        return (time.time() - self._ts) > TTL_SECONDS

    def status(self) -> dict:
        """行情条状态：stale 仅在无缓存或严重过期时为真，避免每 8s 闪警告。"""
        with self._lock:
            age = time.time() - self._ts if self._ts else None
            has_cache = bool(self._cache)
            refreshing = self._refreshing
        return {
            "stale": (not has_cache) or (
                age is not None and age > STALE_TTL_SECONDS),
            "refreshing": refreshing,
            "age_seconds": round(age, 1) if age is not None else None,
        }

    def _fail_delay(self, streak: int) -> int:
        return FAIL_BACKOFF_SECONDS[min(max(streak, 1) - 1,
                                        len(FAIL_BACKOFF_SECONDS) - 1)]

    def _mark_fail(self, key: str, err: Exception, now: float) -> None:
        streak = self._fail_streak.get(key, 0) + 1
        delay = self._fail_delay(streak)
        self._fail_streak[key] = streak
        self._fail_until[key] = now + delay
        log.warning("quote %s refresh failed (%d): %r; retry in %ds",
                    key, streak, err, delay)

    def _clear_fail(self, key: str) -> None:
        if self._fail_streak.pop(key, 0):
            log.info("quote %s recovered", key)
        self._fail_until.pop(key, None)

    def begin_refresh(self) -> bool:
        """并发护栏：已有刷新在途时返回 False（防端点轮询堆积线程）。"""
        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True
            return True

    def end_refresh(self) -> None:
        with self._lock:
            self._refreshing = False

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(v) for v in self._cache.values()]

    def refresh(self) -> int:
        """实时源每轮拉取；无实时源的慢标的按 60s 节流。返回成功数。

        单标的失败按 30s→5min 指数退避，失败期间保留上次成功值；同一错误
        只会在退避到期后再次尝试，避免每 8s 轮询刷日志/打源站。
        """
        ok = 0
        now = time.time()
        slow_due = (now - self._slow_ts) > SLOW_TTL_SECONDS
        codes = [x["sina"] for x in self._targets if x.get("sina")]
        batch_tried = now >= self._batch_fail_until
        batch_ok = False
        sina: dict[str, list[str]] = {}
        if batch_tried:
            try:
                sina = self._sina_fetch(codes)
            except Exception as e:  # noqa: BLE001
                self._batch_fail_streak += 1
                delay = self._fail_delay(self._batch_fail_streak)
                self._batch_fail_until = now + delay
                log.warning("sina batch failed (%d): %r; retry in %ds",
                            self._batch_fail_streak, e, delay)
            else:
                batch_ok = True
                if self._batch_fail_streak:
                    log.info("sina batch recovered")
                self._batch_fail_streak = 0
                self._batch_fail_until = 0.0
        for item in self._targets:
            key = item["key"]
            if now < self._fail_until.get(key, 0.0):
                continue
            try:
                price = chg = ts = None
                code = item.get("sina")
                if code and not batch_ok:
                    continue
                if code and code in sina:
                    price, chg, ts = _sina_quote(code, sina[code])
                elif code:
                    with self._lock:
                        cached = key in self._cache
                    if cached:
                        self._mark_fail(key, LookupError("sina code missing"), now)
                        continue
                    price, chg = item["fetch"]()
                elif slow_due:
                    res = item["fetch"]()
                    price, chg = res[0], res[1]
                    ts = res[2] if len(res) > 2 else None
                else:
                    continue
                if price is None or (isinstance(price, float) and math.isnan(price)):
                    continue
                with self._lock:
                    self._cache[key] = {
                        "key": key, "name": item["name"],
                        "price": round(price, item.get("decimals", 2)), "chg_pct": chg,
                        "updated": ts or datetime.now().strftime("%m-%d %H:%M"),
                    }
                self._clear_fail(key)
                ok += 1
            except Exception as e:  # noqa: BLE001
                self._mark_fail(key, e, now)
        finished = time.time()
        with self._lock:
            if ok:
                self._ts = finished
            if slow_due:
                self._slow_ts = finished
        if ok:
            self._persist()
        return ok
