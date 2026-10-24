"""每日资产情绪聚合（规则票加权，可解释、无黑盒）。

对某日全部条目的 impacts 做投票聚合：
- 一票 = (资产, 方向, 触发规则) 三元组，**同规则同方向只计一票**——多源转载
  同稿（cluster_size>1）不重复计票，不同规则（fed_ease + us_data_weak）各算
  一票独立论据；
- 票权：已落地（g=fact）1.3，预期阶段（g=guess）1.0；
- 资产情绪分 = round(100 * (B-S) / (B+S+K))，B=利多票权和，S=利空票权和，
  K=3 为中性先验票权（20261022 打分机制批：原纯度公式 46% 资产日 ±100 饱和、
  单票与厚证据不可分 → 置信平滑；21 天回测票权中位数 4.9，K≈0.6×中位证据量）。
  ⚠️ K 与票权同单位（guess 票权）：单张 guess 票 ≈ ±25；fact 票满传播杠杆
  （×3）单票权 3.9 → ±57 而非 100——高传播本身是证据厚度，设计使然。
  无任何方向票 → None（显示"-"，不猜）。

⚠️ 这是"当日报道情绪"，不是行情预测模型：它汇总的是新闻里出现了哪些方向的
规则论据及其传播广度。样本少的日子分数噪声很大，仅供速筛参考，不构成投资建议。
后续积攒"评分 vs 次日实际涨跌"的对照数据后，再考虑升级模型（用户规划）。
"""
from __future__ import annotations

from .impact import ASSETS

FACT_W = 1.3
GUESS_W = 1.0
K = 3.0   # 中性先验票权；单位=guess 票权（见模块 docstring，勿当票数理解）


def compute(items: list[dict]) -> list[dict]:
    """items 至少含 impacts；返回按 |score| 降序的资产情绪列表。

    票权 = (fact 1.3 / guess 1.0) × 传播杠杆 min(3, √传播条数)——
    同一规则被越多条目提及（传播条数，含同组转载；传播广度=市场关注度），
    论据越有分量，上限 3 倍防单话题刷分。
    """
    agg: dict[str, dict] = {}
    votes: dict[tuple, list] = {}   # (a,d,k) -> [g, count]
    for it in items:
        for x in it.get("impacts") or []:
            a, d, k = x.get("a"), x.get("d"), x.get("k")
            if d not in ("bull", "bear") or a not in ASSETS:
                continue  # rel 是"相关"不是方向；未知资产跳过
            vote = (a, d, k)
            if vote not in votes:
                votes[vote] = [x.get("g"), 0]
            elif x.get("g") == "fact":
                votes[vote][0] = "fact"   # v5/R3 单调：任一实例落地 → 票按落地计
            votes[vote][1] += 1
    for (a, d, k), (g, count) in votes.items():
        rec = agg.setdefault(a, {"bull": 0.0, "bear": 0.0,
                                 "bull_n": 0, "bear_n": 0})
        w = (FACT_W if g == "fact" else GUESS_W) * min(3.0, count ** 0.5)
        rec[d] += w
        rec[f"{d}_n"] += 1
    out = []
    for a, rec in agg.items():
        b, s = rec["bull"], rec["bear"]
        out.append({
            "a": a, "name": ASSETS[a]["name"], "icon": ASSETS[a]["icon"],
            "bull_n": rec["bull_n"], "bear_n": rec["bear_n"],
            "score": round(100 * (b - s) / (b + s + K)) if (b + s) > 0 else None,
        })
    out.sort(key=lambda r: (r["score"] is None, -abs(r["score"] or 0)))
    return out
