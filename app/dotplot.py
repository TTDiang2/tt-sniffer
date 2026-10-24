"""利率观点抽取器：从快讯文本抽 (央行, 时点, 动作, 幅度, 概率, 归因)——
"信息流观点点阵图"的数据源（用户需求：类似 FOMC 点阵图的自主版）。

⚠️ 校准警告：正则抽取是粗粒度信息抽取，只认高置信句式（概率/预计/押注），
抽不出观点的条目直接不进点阵（宁缺勿滥）；句子级歧义（"降息预期降温"）
由负向窗口拦截。历史经验：非农日（2026-10-02）单日可抽 ~10 点。

点阵语义（对齐 FOMC dot plot）：
- 横轴 = 时点（动作指向的会议月，标准化 YYYY-MM）
- 纵轴 = 动作（降息/维持/加息，bp 幅度细分）
- 一个点 = 一条含利率观点的快讯；颜色 = 归因类型
  （market=CME/Kalshi 等市场定价、sellside=机构预测、trader=交易员押注、
   official=美联储官员/传声筒）
"""
from __future__ import annotations

import re

# 央行识别
_CBS = [
    ("fed", ("美联储", "联储", "Fed", "FOMC", "鲍威尔")),
    ("ecb", ("欧洲央行", "欧央行", "ECB", "拉加德")),
    ("boe", ("英国央行", "英格兰银行", "BOE")),
    ("boj", ("日本央行", "日央行", "BOJ", "植田")),
]

# 归因主体（顺序即优先级：市场定价 > 预测市场 > 官方 > 卖方 > 交易员）
_ACTORS = [
    ("market_cme", ("CME", "美联储观察")),
    ("market_kalshi", ("Kalshi", "预测市场")),
    ("official", ("鲍威尔", "美联储官员", "票委", "理事", "传声筒", "美联储纪要")),
    ("sellside", ("银行", "证券", "资本", "经济学家", "机构", "首席")),
    ("trader", ("交易员", "投资者", "期货市场")),
]

# 时点抽取：X月（默认本年内）/ YYYY年X月 / 明年X月 / X季度
# 月词后随'日'（日期线'10月2日电'）在正则层拒绝；'月+数据名词'在 _month_of 拒收
_MONTH = re.compile(r"(?:(\d{4})年|明年)?(\d{1,2})月(?!\d|日)")   # 后随数字/日=日期线
_DATA_MONTH = re.compile(r"\d{1,2}月\s*(?:份?\s*)?(?:就业|非农|CPI|PPI|经济数据|报告|数据)")
_QUARTER = re.compile(r"(?:(\d{4})年)?第?([一二三四1-4])季度")
_ACTION_W = re.compile(r"(降息|加息|维持利率不变|按兵不动|维持不变)")
_BP = re.compile(r"(\d{2,3})\s*(?:个基点|bp)")
_PROB = re.compile(r"概率(?:为|升至|降至|达到)?\s*(\d{1,3}(?:\.\d+)?)%")
# 整句级负向：句中出现任一 → 全句不产观点（v6/R1：'不再定价''不太可能'
# '推迟至''此前预计'曾复活撤销观点/反向点；句级预检同时封死双动作分段
# 窗口的逃逸——'是否加息…押注进一步降温'）
_NEG_SENT = ("不再定价", "不太可能", "可能性不大", "概率不大", "推迟至",
             "此前预计", "此前预测", "预期降温", "押注减少", "押注降低",
             "押注回落", "概率下降", "概率回落", "不再押注", "打消", "搁置",
             "削减", "减少", "降低", "缩减", "下调", "降温", "缓解")
# 段级负向（方向反转：'暂停加息'仍由段级拦）
_NEG = ("暂停", "停止")

# 句切：按句号/分号/叹号/问号拆句（观点是句子级属性）
_SENT = re.compile(r"[。；;！!？]")


def _cb_candidates(text: str) -> set[str]:
    return {key for key, words in _CBS if any(w in text for w in words)}


def _cb_of(text: str) -> str:
    cbs = _cb_candidates(text)
    return next(iter(cbs)) if len(cbs) == 1 else ""


def _actor_of(text: str) -> str:
    for key, words in _ACTORS:
        if any(w in text for w in words):
            return key
    return ""


def _now_year() -> int:
    from datetime import date
    return date.today().year


def _month_of(text: str, year: int) -> str:
    q = _QUARTER.search(text)
    if q:
        y = int(q.group(1) or year)
        qi = {"一": 1, "二": 2, "三": 3, "四": 4}.get(q.group(2), 0) or int(q.group(2))
        return f"{y}-{int(qi) * 3:02d}"
    m = _MONTH.search(text)
    if m:
        if _DATA_MONTH.search(text):   # '9月就业报告'=数据月份，非会议时点
            return ""
        y = int(m.group(1) or year)
        mo = int(m.group(2))
        if 1 <= mo <= 12:
            return f"{y}-{mo:02d}"
    if re.search(r"下次会议|本月|本周", text):
        return "near"
    return ""


def extract_all(sentence: str, year: int = 2026,
                actor_full: str = "", cb_full: str = "") -> list[dict]:
    """动作分段抽取（v3）：每个动作词归属"上一动作词尾→下一动作词头"的
    完整分段（月份修饰常在动作词前——'美联储12月加息'，v2 曾把 12 月切掉）。
    概率/幅度只认本段（防 CME 文案把 hike 的 25bp 错挂给 hold）。
    负向词窗口 = 动作词前 10 字 + 本段。返回段内全部有效观点。"""
    out = []
    acts = list(_ACTION_W.finditer(sentence))
    if not acts:
        return out
    # 整句级负向预检（v6/R1）：'不再定价''推迟至''押注…降温'等句全句不产观点
    if any(n in sentence for n in _NEG_SENT):
        return []
    cbs_in_sent = _cb_candidates(sentence)
    prev_month = ""   # CME 句式："到10月维持78.4%，加息25bp 21.6%"——互斥结果共享时点
    for i, act_m in enumerate(acts):
        seg_start = acts[i - 1].end() if i > 0 else 0
        seg_end = acts[i + 1].start() if i + 1 < len(acts) else len(sentence)
        seg = sentence[seg_start:seg_end]          # month 修饰在动作词前，全段取
        own = sentence[act_m.start():seg_end]      # 概率/幅度/负向只从本词起
        pre = sentence[max(0, act_m.start() - 10): act_m.start()]
        if any(n in own or n in pre for n in _NEG):
            continue
        action = {"降息": "cut", "加息": "hike"}.get(act_m.group(1), "hold")
        prob = _PROB.search(own)
        bp = _BP.search(own)
        month = _month_of(seg, year) or prev_month   # 互斥结果继承前段时点
        if not month:
            continue
        prev_month = month
        # 多央行句：央行词不在本段 → 归属不明拒（'欧洲央行9月降息'挂 fed 的修复）
        if len(cbs_in_sent) > 1:
            seg_cbs = {k for k, words in _CBS if any(w in seg for w in words)}
            if len(seg_cbs) != 1:
                continue
            cb = next(iter(seg_cbs))
        else:
            cb = next(iter(cbs_in_sent)) if cbs_in_sent else cb_full
        if not cb:      # 央行归属不明（CME 续句/英国利率等歧义）不进阵
            continue
        out.append({"cb": cb, "action": action,
                    "bp": int(bp.group(1)) if bp else None,
                    "prob": float(prob.group(1)) if prob else None,
                    "month": month,
                    "actor": _actor_of(sentence) or actor_full or "news"})
    return out


def extract(sentence: str, year: int | None = None,
            actor_full: str = "") -> dict | None:
    """单值便捷入口（首个有效观点）。"""
    vs = extract_all(sentence,
                     year if year is not None else _now_year(), actor_full)
    return vs[0] if vs else None


def extract_items(text: str, year: int | None = None) -> list[dict]:
    """整条快讯 = 逐句抽取；归因主体用全文级（CME 文案的逗号分段无独立
    归因词，句级判定会稀释成 news）；同 (cb,action,month,bp) 去重。"""
    text = text or ""
    year = year or _now_year()
    actor_full = _actor_of(text)
    cb_full = _cb_of(text)
    out, seen = [], set()
    for sent in _SENT.split(text):
        for v in extract_all(sent, year, actor_full, cb_full):
            key = (v["cb"], v["action"], v["month"], v["bp"])
            if key not in seen:
                seen.add(key)
                out.append(v)
    return out


# ---- 具体点位（v2，用户规格：纵轴要有像 FOMC 点阵图一样的具体点位）----

_BP_RANGE = re.compile(r"(\d\.\d+)\s*%-\s*(\d\.\d+)\s*%")


def base_from_sep(sep: dict | None) -> dict | None:
    """从 SEP 决策串解析当前利率区间（'加息25bp至3.75%-4.00%' → lo 3750 / hi 4000）。

    单位与 fed_sep.DATA.dist 一致（4125 = 4.125%）。解析失败返回 None，
    前端回退动作轴（加息/维持/降息三行）。
    """
    if not sep:
        return None
    m = _BP_RANGE.search(sep.get("decision") or "")
    if not m:
        return None
    lo = round(float(m.group(1)) * 1000)
    hi = round(float(m.group(2)) * 1000)
    if not (0 < lo < hi < 20000):
        return None
    if hi - lo != 250:   # 档位标签按 ±125（25bp 宽）计；非 25bp 宽区间回退动作轴
        return None
    return {"lo": lo, "hi": hi, "mid": (lo + hi) // 2,
            "label": f"{m.group(1)}-{m.group(2)}%",
            "meeting": sep.get("meeting") or ""}


def cell_level(action: str, bp: int | None, mid: int) -> int:
    """观点指向的利率档位中点：降息 −bp / 加息 +bp / 维持 0。

    单位与 fed_sep.DATA.dist 一致（4125 = 4.125%），1bp = 10 个单位；
    幅度未明示（bp=None）按 25bp 计——FOMC 惯例步长，口径声明落在图注。
    """
    step = (bp if bp else 25) * 10
    if action == "cut":
        return mid - step
    if action == "hike":
        return mid + step
    return mid
