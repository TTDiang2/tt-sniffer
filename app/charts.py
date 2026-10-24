"""统计图表纯函数：风险/避险散点坐标与资产分桶（无 IO，可测）。

分桶依据（智者规格 v1）：
- fx 剔除：本库 fx 方向是人民币本位（fed_ease→人民币被动升），且与风险资产
  规则高度共线，放任何一侧都是双计；
- oil 剔除：战争语境下油是避险/供给溢价不是风险偏好（geo_war 油涨股不涨），
  先验引擎不可分，画进风险侧会把地缘冲突放进错误象限；
- commodity 留风险侧：需求敏感（fiscal/house_ease 与 a_stock 同向）。
"""
from __future__ import annotations

import math

RISK = ("us_stock", "a_stock", "hk_stock", "em_stock", "crypto",
        "realestate", "commodity")
HAVEN = ("gold", "bond", "bond_us")   # v8：bond 拆中债，美债单列入避险侧
OUT = {"fx": "相对价格（人民币本位），与风险/避险轴语义不重合",
       "oil": "供给冲击与需求冲击方向相反，先验引擎不可分"}


def bucket_of(asset: str) -> str:
    if asset in RISK:
        return "risk"
    if asset in HAVEN:
        return "haven"
    return "out"


def _side_score(impacts: list[dict], assets: tuple[str, ...]) -> float | None:
    """该侧连续强度（v9）：净票方向 × 证据量收缩 tanh(n/2)×100。

    旧口径 sum/n×100 在「该侧只要有票必 ±100」上退化——单边新闻全部落在
    (±100,0)/(0,±100) 四条轴射线上，散点贴框无信息量（2026-10-23 实测：
    29 点 100% 贴边、去重仅 5 个坐标）。新口径 1 票≈±46、2 票≈±76、
    3 票≈±91，证据越足越接近 ±100；混合票向净方向收敛；无票返回 None。
    """
    votes = [1 if x.get("d") == "bull" else -1
             for x in impacts or []
             if x.get("a") in assets and x.get("d") in ("bull", "bear")]
    if not votes:
        return None
    net = sum(votes) / len(votes)
    return net * math.tanh(len(votes) / 2) * 100


def news_point(impacts: list[dict]) -> dict | None:
    """单条新闻散点坐标：x=风险侧强度，y=避险侧强度（缺失侧按 0）。

    双侧全无方向票 → None（不画）。sides 供前端区分实心（both）/空心（单边）。
    """
    x = _side_score(impacts, RISK)
    y = _side_score(impacts, HAVEN)
    if x is None and y is None:
        return None
    sides = "both" if (x is not None and y is not None) else (
        "risk" if x is not None else "haven")
    return {"x": round(x or 0), "y": round(y or 0), "sides": sides}


def day_point(sent_rows: list[dict]) -> dict | None:
    """单日汇总点：day_sentiment 各资产分按桶取均值（None 资产跳过）。"""
    def avg(assets):
        vals = [r["score"] for r in sent_rows or []
                if r.get("a") in assets and r.get("score") is not None]
        return round(sum(vals) / len(vals)) if vals else None
    x = avg(RISK)
    y = avg(HAVEN)
    if x is None and y is None:
        return None
    sides = "both" if (x is not None and y is not None) else (
        "risk" if x is not None else "haven")
    return {"x": x or 0, "y": y or 0, "sides": sides}
