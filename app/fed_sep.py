"""FOMC 官方 SEP 数据存取：缓存优先（自动抓取）→ 静态兜底。

数据链：sep_fetch.refresh() 季度自动抓取官网 → data/sep_cache.json；
服务读取 get_current() = 缓存（较新）→ 静态 DATA（本文件，2026-09-16 期
完整分布，含来源与校验）。缓存损坏/缺失/抓取失败时静默降级静态。

⚠️ 静态兜底数据 = 官方 HTML 解析结果（可复现：sep_fetch.parse_sep_html
对 fomcprojtabl20260916.htm 的解析输出逐字段一致）。验证义务保留：
每次 SEP 发布后 sep_fetch 自动更新缓存；静态兜底仅需在重大结构变更时跟进。
数值仅作速筛参照，不构成投资建议。
"""
from __future__ import annotations

import logging

log = logging.getLogger("fed_sep")

# 静态兜底：FOMC 2026-09-16 SEP（sep_fetch 解析输出，官方源）
DATA = {
    "meeting": "2026-09-16",
    "participants": 18,          # 19 席位中主席不提交（连续两期惯例）
    "decision": "加息25bp至3.75%-4.00%（2023年7月以来首次加息，12-0 全票）",
    "source": "FOMC SEP 2026-09-16（federalreserve.gov 官网数据）",
    "origin": "static",
    # 逐档人数：{年份: {档位bp: 人数}}；档位=目标区间中点（4125=4.125%）
    "dist": {
        "2026": {4375: 4, 4125: 12, 3875: 2},
        "2027": {4375: 8, 4125: 6, 3625: 3, 3125: 1},
        "2028": {4125: 4, 3875: 5, 3625: 3, 3375: 1, 3125: 4},
        "2029": {3875: 3, 3625: 7, 3375: 2, 3125: 4, 2875: 1},
        "longer": {3875: 2, 3750: 1, 3625: 2, 3500: 2, 3375: 1, 3250: 2,
                   3125: 1, 3000: 6, 2875: 1},
    },
    # 中位数 = 分布累计过半档位（自算，与官方一位小数显示等价：4125→4.1）
    "median": {"2026": 4125, "2027": 4125, "2028": 3875, "2029": 3625,
               "longer": 3250},
    "sep_econ": {
        "gdp": {"2026": 2.3, "2027": 2.4, "2028": 2.2, "2029": 2.1, "longer": 2.0},
        "unemployment": {"2026": 4.1, "2027": 4.1, "2028": 4.1, "2029": 4.1,
                         "longer": 4.2},
        "pce": {"2026": 3.7, "2027": 2.3, "2028": 2.1, "2029": 2.0, "longer": 2.0},
        "core_pce": {"2026": 3.4, "2027": 2.5, "2028": 2.2, "2029": 2.0},
    },
    "notes": [
        "2027 起 4.1% 持平、2028 前无降息——较 6 月偏鹰 50bp（'higher for longer'）",
        "16/18 预期年内至少再加息一次；对应 10-02 市场定价 12月加息概率 72.6%，方向一致",
    ],
}


def validate(data: dict) -> list[str]:
    """数据自洽校验（测试用）：分布列和/中位数量级/参与者数。"""
    errs = []
    if not data.get("dist"):
        return ["dist 缺失"]
    if data.get("participants", 0) < 12:
        errs.append(f"participants 异常: {data.get('participants')}")
    for y, dd in data["dist"].items():
        s = sum(dd.values())
        if s == 0 or s > 19:
            errs.append(f"{y} 分布人数异常: {s}")
    med = data.get("median", {})
    if med.get("2026") and not (3000 <= med["2026"] <= 5000):
        errs.append(f"2026 中位数异常: {med['2026']}")
    return errs


def get_current() -> dict:
    """当前生效的 SEP 数据：缓存优先，静态兜底（缓存自洽校验不过则忽略）。"""
    try:
        from .sep_fetch import read_cache
        cached = read_cache()
        if cached and not validate(cached):
            # 官网抓取不含决策串（利率区间）：与静态同期（meeting 相同）时从
            # 静态补上；跨期留空——点阵前端降级动作轴，不拿旧决策冒充当前区间
            if not cached.get("decision") and cached.get("meeting") == DATA["meeting"]:
                cached = {**cached, "decision": DATA["decision"]}
            return cached
        if cached:
            log.warning("SEP 缓存校验不过，回退静态数据: %s", validate(cached))
    except Exception as e:
        log.debug("SEP 缓存读取异常（用静态兜底）: %s", e)
    return DATA
