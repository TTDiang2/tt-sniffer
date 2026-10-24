"""FOMC SEP 自动抓取器：季度自动更新官方点阵，零人工干预。

数据链（全部本机实测验证，2026-10-05）：
1. 发现：GET fomccalendars.htm → 正则 fomcprojtabl(\\d{8}) → 取最新日期
   （实测：页面含 2021-2026 全部 23 期 SEP 链接，季度节奏 3/6/9/12）
2. 抓取：GET /monetarypolicy/fomcprojtabl{YYYYMMDD}.htm
   （注意：HTML 版在 monetarypolicy/ 根路径，files/ 子目录只有 PDF——
    实测 files/*.htm 404、根路径 200）
3. 解析：分布表（锚定 "Midpoint of target range or target level"：行=利率档位
   百分比、列=年份、格=该档官员人数——**官方逐点分布文本版，比媒体转述更全**）
   + 中位数表（锚定 "Variable Median"：GDP/失业率/PCE/核心PCE 年份中位）
4. 缓存：data/sep_cache.json；服务读取缓存优先、静态兜底（fed_sep.DATA）

⚠️ 礼貌爬取：仅在「启动时 + 每 12 小时」检查一次日历页；发现新会议才抓正文。
⚠️ 官网不可达时静默降级用静态数据（国内可达性实测通过，见模块测试记录）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from .config import data_dir

log = logging.getLogger("sep_fetch")

CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
SEP_URL = "https://www.federalreserve.gov/monetarypolicy/fomcprojtabl{}.htm"
_UA = {"User-Agent": "Mozilla/5.0 (personal research tool; contact: owner)"}
_TIMEOUT = 25

_DIST_ANCHOR = "Midpoint of target range or target level"


def _cells(row_html: str) -> list[str]:
    cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row_html, re.S)
    return [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", "").strip()
            for c in cells]


def discover_latest() -> str | None:
    """日历页找最新 SEP 日期（YYYYMMDD）；失败返回 None。"""
    r = httpx.get(CALENDAR_URL, headers=_UA, timeout=_TIMEOUT,
                  follow_redirects=True)
    r.raise_for_status()
    dates = re.findall(r"fomcprojtabl(\d{8})", r.text)
    return max(dates) if dates else None


def fetch_html(date: str) -> str:
    r = httpx.get(SEP_URL.format(date), headers=_UA, timeout=_TIMEOUT,
                  follow_redirects=True)
    r.raise_for_status()
    return r.text


# 年份列识别：SEP 窗口随发布季度变化——3/6 月期 3 年+longer（4 列），
# 9/12 月期 4 年+longer（5 列）。硬编码列数是 v6 的跨期致命伤（智者 M1：
# 2027-03 期发布之日链路静默失效），列必须从表头动态识别
_YEAR_COL = re.compile(r"^\d{4}$")


def _year_cols(header: list[str]) -> list[int]:
    """表头中年份列的下标（^\d{4}$ 或 Longer run 开头）。"""
    return [i for i, c in enumerate(header)
            if _YEAR_COL.match(c) or c.lower().startswith("long")]


def _parse_dist(html: str) -> tuple[dict, list[str]] | None:
    """分布表：行=利率档位（4.375 等百分比），列=年份（动态），格=人数。

    返回 ({年份键: {档位bp: 人数}}, 年份键列表)；表缺失/无年份列 = None。
    表结构实测：16 行，档位降序排列；跨期年份列数 4-5 不等。
    """
    for t in re.findall(r"<table.*?</table>", html, re.S):
        if _DIST_ANCHOR not in t:
            continue
        rows = re.findall(r"<tr.*?</tr>", t, re.S)
        if not rows:
            continue
        header = _cells(rows[0])
        year_idx = _year_cols(header)
        if not year_idx:
            continue
        years = [header[i] for i in year_idx]
        out: dict[str, dict] = {}
        for r in rows[1:]:
            cs = _cells(r)
            if not cs or not re.match(r"^\d\.\d+$", cs[0]):
                continue
            bp = int(round(float(cs[0]) * 1000))
            # 表头与数据行下标天然对齐（首格都是文本：'Midpoint...' vs 档位）
            for j, i in enumerate(year_idx):
                if i < len(cs):
                    c = cs[i]
                    if c.isdigit() and int(c) > 0:
                        key = ("longer" if years[j].lower().startswith("long")
                               else years[j])
                        out.setdefault(key, {})[bp] = int(c)
        if out:
            return out, years
    return None


def _parse_econ(html: str) -> dict:
    """中位数表中的经济预测（可选字段，解析失败返回空 dict 不阻断）。

    实测结构：表头两行——首行 Variable/Median/CT/Range 分组（colspan），
    第二行年份格在 Median/CT/Range 三组重复出现。解析：年份列动态识别且
    按首现去重（只取 Median 组）；数据行首格是变量名而年份表头行没有，
    列下标统一 +1（v6/M2：硬编码年份曾错位 1-2 年，三组同名覆盖曾以 CT
    值冒充 Median 值）。
    """
    want = {"Change in real GDP": "gdp", "Unemployment rate": "unemployment",
            "PCE inflation": "pce", "Core PCE inflation": "core_pce"}
    for t in re.findall(r"<table.*?</table>", html, re.S):
        # anchor：'Variable' 与 'Median' 分属两格（实测），故用双关键词定位
        if "Change in real GDP" not in t or "Median" not in t:
            continue
        rows = re.findall(r"<tr.*?</tr>", t, re.S)
        if len(rows) < 3:
            continue
        # 年份列动态识别（M2：硬编码年份曾在 2025-12 期错位一年、
        # 2024-12 期错位两年）；econ 表年份在第二表头行（rows[0] 是
        # 'Variable/Median/CT/Range' 分组行，无年份）
        # econ 表年份格在 Median/CT/Range 三组重复出现——同名列会互相覆盖
        # （CT 的 2026 曾覆盖 Median 的 2026），只保留每组年份首次出现的下标
        head2 = _cells(rows[1])
        year_idx, seen_y = [], set()
        for i, c in enumerate(head2):
            key = "longer" if c.lower().startswith("long") else (
                _YEAR_COL.match(c).group(0) if _YEAR_COL.match(c) else None)
            if key and key not in seen_y:
                seen_y.add(key)
                year_idx.append(i)
        if not year_idx:
            continue
        years = [head2[i] for i in year_idx]
        out: dict[str, dict] = {}
        for r in rows[1:]:
            cs = _cells(r)
            if not cs:
                continue
            # 行名带数字角标（实测 'Core PCE inflation4'），用前缀匹配
            key = next((k for name, k in want.items()
                        if cs[0].startswith(name)), None)
            if not key:
                continue
            # 数据行首格是变量名、年份表头行没有首格——列下标差 1，统一 +1
            row = {}
            for j, i in enumerate(year_idx):
                i += 1
                if i < len(cs):
                    try:
                        row[("longer" if years[j].lower().startswith("long")
                             else years[j])] = float(cs[i])
                    except ValueError:
                        pass
            if row:
                out[key] = row
        if out:
            return out
    return {}


def _participants(dist: dict) -> int:
    """参与者数 = 各年列和最大值（个别成员对远端年份可不给预测）。"""
    return max((sum(v.values()) for v in dist.values()), default=0)


def parse_sep_html(html: str, meeting: str) -> dict | None:
    """HTML → SEP 数据 dict（schema 与 fed_sep.DATA 同构）。分布表缺失=失败。"""
    parsed = _parse_dist(html)
    if not parsed:
        return None
    dist, _years = parsed
    median = {}
    for y, dd in dist.items():
        # 中位数 = 人数累计过半档位（降序排列后第 ⌈n/2⌉ 位所在档）
        pts = sorted(dd.items(), key=lambda kv: -kv[0])
        total = sum(n for _, n in pts)
        acc = 0
        for bp, n in pts:
            acc += n
            if acc * 2 >= total:
                median[y] = bp
                break
    econ = _parse_econ(html)
    date_iso = f"{meeting[:4]}-{meeting[4:6]}-{meeting[6:]}"
    return {
        "meeting": date_iso,
        "participants": _participants(dist),
        "dist": dist,
        "median": median,
        "sep_econ": econ,
        "origin": "live",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": f"FOMC SEP {date_iso}（federalreserve.gov 自动抓取）",
        "notes": ["自动抓取自美联储官网，分布为官方逐点文本数据"],
    }


def cache_path() -> Path:
    return data_dir() / "sep_cache.json"


def read_cache() -> dict | None:
    p = cache_path()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if d.get("dist") and d.get("meeting") else None
    except Exception as e:
        log.warning("sep 缓存损坏: %s", e)
        return None


def refresh(force: bool = False) -> dict | None:
    """发现→抓取→解析→写缓存。新会议才抓正文（除非 force）。返回新数据或 None。"""
    try:
        latest = discover_latest()
        if not latest:
            log.warning("日历页无 SEP 链接")
            return None
        cached = read_cache()
        if not force and cached and cached.get("meeting") == (
                f"{latest[:4]}-{latest[4:6]}-{latest[6:]}"):
            return cached
        html = fetch_html(latest)
        data = parse_sep_html(html, latest)
        if not data:
            log.warning("SEP 解析失败（结构变更？） date=%s", latest)
            return None
        cache_path().write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        log.info("SEP 已更新: %s（%d 位参与者）", data["meeting"],
                 data["participants"])
        return data
    except Exception as e:
        log.warning("SEP 抓取失败（保留现有数据）: %s", e)
        return None


def needs_check(last_check_iso: str | None) -> bool:
    """距上次检查 ≥12 小时才再查（礼貌爬取节流）。"""
    if not last_check_iso:
        return True
    try:
        last = datetime.fromisoformat(last_check_iso)
    except ValueError:
        return True
    return datetime.now() - last >= timedelta(hours=12)
