"""simhash：64 位近重复文本指纹（A3 最小数据层版）。

用途：识别同一快讯的多源转载/洗稿（标题+正文的字符级近似），
供 UI「n 条转载」badge 与后续事件聚类用。

设计：
- 分词：CJK 字符二元组（bigram）+ ASCII 词（小写）。中文无分词器依赖，
  bigram 对短快讯（几十字）的近重复识别足够；二元组对换行/标点扰动不敏感
  （先剥离非 CJK/字母字符）。
- 加权：token 频次（tf）。短文本无需更复杂的权重。
- 距离判据：hamming ≤ 12 视为近重复候选，**但必须再过数字槽一致性守卫**
  （same_digit_slots）。标定（V0.3 夜，实库 454 行全对扫描）：
  - 无关文本对：实测 33+，远离候选；
  - 真转载（多源同稿）：实测 ≤12，且数字槽逐字一致；
  - **同模板不同事实档（"X机构上调Y目标价至N元"换标的换数字、非农不同
    分项）：实测落 10-12，与真转载档完全重叠**——hamming 单判据在此档
    假阳性率约 10%（101 对中 8-11 对）。数字槽守卫（digit_conflict：
    两侧有数字且交集为空=冲突）剔除其中数字互斥的类别（目标价对、
    2026/2027 预算对）；**残差如实记录**：共享日期/月份数字的同模板对
    （"美国9月非农4.6万" vs "美国9月非农2.9万"共享'9'）与数字集相同
    仅方向反转的对（"从280下调至250" vs "从250上调至280"）守卫原理上
    不可分，实库风险带残差约 6/36 对（全量 ~6%）——badge 定位是转载数
    提示而非精确断言，残差档可接受；后续若需收紧须引入槽位类型/
    方向语义（超出 A3 最小版范围）。全等比较方案实库复扫过杀 72 对真
    转载（省数字/标题多号），已弃用——守卫语义以 digit_conflict 注释为准。
- hamming 阈值对短文本敏感（token 少则位累加和方差小），不能沿用长文
  惯用的 3；若入库文本量级改变（长文/长标题），重新标定。
"""
from __future__ import annotations

import hashlib
import re

CLUSTER_HAMMING = 12

# 同类分组算法版本：判据语义变更时 +1（store 据此全库重放分组）
GROUP_ALGO_VER = 2

_ASCII_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_DIGITS = re.compile(r"\d+(?:\.\d+)?")

# 来源前缀与数字写法归一（V2 判据引入：编辑改写是同稿 hamming 偏远的主因）
_SRC_PREFIX = re.compile(
    r"^(财联社|新浪财经|同花顺|东财|华尔街见闻)[^，,]{0,14}[电讯][:：，,]?")
_CN_WAN = re.compile(r"\d+(?:\.\d+)?万")


def norm_text(t: str) -> str:
    """同稿归一化：去来源前缀、中文万数字→纯数字、去千分位/全角逗号与空白。"""
    t = _SRC_PREFIX.sub("", t or "")

    def _wan(m):
        try:
            return str(int(float(m.group(0)[:-1]) * 10000))
        except ValueError:
            return m.group(0)
    t = _CN_WAN.sub(_wan, t)
    return re.sub(r"[\s，,]+", "", t)

# SQLite INTEGER 是有符号 64 位：指纹以带符号形式存储，位型不变，
# hamming 前先用掩码还原成无符号位型再异或（MASK64 为公开别名，
# store 的内联快速路径同用一份位掩码）
_MASK64 = (1 << 64) - 1
MASK64 = _MASK64


def to_signed64(x: int) -> int:
    x &= _MASK64
    return x - (1 << 64) if x & (1 << 63) else x


def digit_slots(text: str) -> list[str]:
    """文本中的数字集合（原样字符串）。

    数字是事实槽。判定语义见 digit_conflict()。
    """
    return sorted(set(_DIGITS.findall(text or "")))


def digit_conflict(a: list[str], b: list[str]) -> bool:
    """数字槽冲突判定（C2 守卫的最终语义）。

    冲突 = 两侧都有数字且交集为空 → 同模板换事实（辉瑞35 vs 耐克90、
    2026 vs 2027）。
    非冲突 = 任一侧无数字，或有共同数字（阿根廷"650基点/10个月" vs
    "650基点"、联合国2200人 vs 正文截断——真转载放行；已知残差：共享
    月份数字的同模板对与方向反转同数字集对按此规则放行，构成 ~6% 残余
    假阳性，模块注释有案）。
    全等比较会误杀省数字/标题多号的真转载（实库复扫 72 对过杀，已弃）。
    """
    if not a or not b:
        return False
    return not set(a) & set(b)


def tokens(text: str) -> list[str]:
    """CJK 二元组 + ASCII 小写词；无有效字符返回空表。"""
    t = text or ""
    out: list[str] = [w.lower() for w in _ASCII_WORD.findall(t)]
    cjk = "".join(_CJK.findall(t))
    out += [cjk[i:i + 2] for i in range(len(cjk) - 1)]
    return out


def _token_hash(token: str) -> int:
    return int.from_bytes(
        hashlib.md5(token.encode("utf-8")).digest()[:8], "big")


def simhash64(text: str) -> int:
    """64 位 simhash（带符号形式，SQLite INTEGER 兼容）；空文本返回 0。"""
    toks = tokens(text)
    if not toks:
        return 0
    v = [0] * 64
    for tok in toks:                      # 频次即重复加权
        h = _token_hash(tok)
        for i in range(64):
            v[i] += 1 if (h >> i) & 1 else -1
    return to_signed64(sum(1 << i for i in range(64) if v[i] > 0))


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & _MASK64).count("1")


def bigram_set(text: str) -> set[str]:
    """归一化文本的 token 集（containment 判据用）。"""
    return set(tokens(text))


def containment(a: set[str], b: set[str]) -> float:
    """方向性包含率：|A∩B| / min(|A|,|B|)。

    同稿常见形态是"短摘要 ⊂ 全句"（东财短句 vs 财联社全句），simhash 均值
    被长文本其余部分拉远（实测 hamming 14-26），包含率对子集关系=1.0。
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


CONTAIN_TH = 0.8


def near_dup(a_norm: str, b_norm: str, ha: int, hb: int,
             ba: set[str] | None = None, bb: set[str] | None = None,
             da: list[str] | set[str] | None = None,
             db: list[str] | set[str] | None = None) -> bool:
    """同类判据（V2 组合，语义与初版逐条等价）：
    1) hamming ≤ 12 且数字槽不冲突（V1 标定判据，原样保留）；
    2) 或 包含率 ≥ 0.8 且数字守卫通过——数字守卫语义（v6/R4）：
       双侧均有数字时要求短侧 ⊆ 长侧；短侧无数字即拒（'终止重组'vs
       '推进重组' c=0.80 语义相反曾被合并）。
    ba/bb：预算好的 token 集；da/db：预算好的数字槽（list 或 set）。
    批量逐对调用必须传——否则每对两次全文正则+分词，50×6k 候选即
    60 万次，实测量级 40s+/页（2026-10-16 生产全站冻结事故根因）。
    快速路径（2026-10-16，与原判据等价的重排）：数字槽冲突（双侧有
    数字且交集空）时判据 1 的守卫与判据 2 的 ⊆ 守卫皆必死，直接 False；
    任一侧无数字时判据 2 的短侧必空亦必死——只剩 hamming。随机新闻对
    几乎全走这两条提前出，配对成本从「两次全文集合交」降到一次小集合交。
    """
    da = da if isinstance(da, (set, frozenset)) else (
        set(da) if da is not None else digit_slots(a_norm))
    da = frozenset(da)
    db = db if isinstance(db, (set, frozenset)) else (
        set(db) if db is not None else digit_slots(b_norm))
    db = frozenset(db)
    if not da or not db:
        # 任一侧无数字：判据 1 守卫（无冲突）恒过、判据 2 短侧必空恒拒
        return hamming(ha, hb) <= CLUSTER_HAMMING
    if not (da & db):
        return False                       # 冲突：两判据皆死
    if hamming(ha, hb) <= CLUSTER_HAMMING:
        return True
    A = ba if ba is not None else bigram_set(a_norm)
    B = bb if bb is not None else bigram_set(b_norm)
    if containment(A, B) >= CONTAIN_TH:
        small, big = (da, db) if len(da) <= len(db) else (db, da)
        # 双侧均有数字且已证有交集，small 必非空；短侧数字必须全在长侧
        if small <= big:
            return True
    return False
