"""规则打分引擎：小作文相关性识别（无需 LLM 即可独立工作）。

设计要点（V0.2 按审查+同源调研校准）：
1. 传闻词分强弱两层——"传闻/网传/纪要"等强词才能归类传闻；"据悉/透露/有望"
   等弱词只加分。词表部分吸收了另一实现基于真实采集数据的回测修正
   （如"知情人士"类是正规新闻标准信源表述，靠市场门槛而非删词来抑制误报）。
2. 市场关联门槛：命中强传闻词但与可交易标的/市场无关（国际政治类噪音）
   → 不入传闻流，分数打三折封顶45；辟谣/澄清豁免该门槛（天然高价值）。
3. 辟谣类不叠加传闻词分；各词表分数封顶，告警阈值（80）不被常规新闻刷穿。
4. 异动关联由流水线算好"与本文相关的异动标的"传入（linked），名称命中同样生效。
5. 正则按词长降序拼接；AI/ST 用词边界防英文误伤。
"""
from __future__ import annotations

import re

from .impact import detect

# ---- 强传闻词：命中任一（且过市场门槛）即可归类"传闻" ----
STRONG_RUMOR: dict[str, int] = {
    "传闻": 22, "传闻称": 24, "市场传闻": 24, "据传": 22, "传言": 22,
    "小作文": 30, "网传": 24, "坊间": 20, "消息人士": 24, "知情人士": 24,
    "接近人士": 20, "人士透露": 22, "有消息称": 22, "小道消息": 24,
    "未证实": 20, "尚未证实": 20, "无法证实": 20, "未经证实": 20,
    "求证": 14, "调研纪要": 18, "券商纪要": 18,
    "群里": 12, "截图流传": 16, "内部文件": 16, "真假难辨": 12,
    "传出": 16, "网曝": 18, "被曝": 18, "疯传": 16, "热传": 16,
    "独家获悉": 16, "独家消息": 16, "爆料": 16, "据说": 12,
    "产业链人士": 16, "不具名": 12, "一手消息": 16, "靠谱消息": 18,
}
# ---- 弱传闻词：只加分，不足以归类 ----
# 注：裸"纪要/会议纪要"在弱词层——它们同样是央行等官方会议纪要的标准措辞，
# 放强词层会把每次官方纪要发布日都误判成传闻满分（V0.2 终审 M3 回归）。
WEAK_RUMOR: dict[str, int] = {
    "据悉": 10, "据称": 8, "透露": 8, "拟": 4, "或将": 6, "或将于": 8,
    "有望": 6, "疑似": 8, "征求意见稿": 8, "专家会": 10, "电话会": 8,
    "截图显示": 8, "或为": 6, "不予置评": 10, "纪要": 8, "会议纪要": 8,
}
# ---- 辟谣/澄清：强词单独可归类；弱词需同时命中强传闻词 ----
DEBUNK_STRONG: dict[str, int] = {
    "辟谣": 26, "澄清": 22, "传闻不实": 26, "不实": 20, "假消息": 24,
    "严重不实": 26, "无中生有": 20, "不属实": 20, "谣言": 24,
    "纯属虚构": 22, "子虚乌有": 22, "从未发布": 20, "系不实": 26,
    "虚假信息": 22,
}
DEBUNK_WEAK: dict[str, int] = {
    "否认": 14, "回应称": 10, "回应：": 8, "不存在": 8, "误导": 8,
    "郑重提示": 10, "以官方公告为准": 12, "谨防": 8,
}
# ---- 官方/确认词（配合 impact/targets 才归类官方） ----
OFFICIAL: dict[str, int] = {
    "公告": 10, "官宣": 16, "证实": 14, "正式": 8, "央行": 12, "证监会": 14,
    "国务院": 12, "财政部": 12, "发改委": 10, "金融监管总局": 12, "交易所": 8,
    "新华社": 10, "人民日报": 10, "央视": 8,
}
# ---- 重磅市场事件词（小作文最爱炒作的主题） ----
IMPACT: dict[str, int] = {
    "降准": 18, "降息": 18, "加息": 14, "LPR": 10, "MLF": 10, "逆回购": 10,
    "平准基金": 22, "印花税": 18, "IPO": 12, "注册制": 10, "限售": 8,
    "解禁": 10, "重组": 12, "借壳": 16, "并购": 10, "收购": 10,
    "退市": 14, "国家队": 16, "汇金": 16, "社保基金": 10,
    "资金面": 14, "流动性": 10, "国债": 10, "特别国债": 20, "地方债": 10,
    "存量房贷": 16, "房地产": 10, "化债": 16, "城投": 12, "存单": 8,
    "美联储": 12, "关税": 14, "制裁": 14, "半导体": 8, "芯片": 8,
    # 债市纵深（搬运校准）
    "国债期货": 12, "现券": 10, "DR007": 10, "R007": 10, "资金价格": 10,
    "信用债": 10, "利率债": 10, "赎回": 10, "债基": 8, "理财": 6,
    # 加密族（20261004：crypto_rss 采集器配套——加密新闻也是可交易市场事件）
    "比特币": 12, "以太坊": 10, "加密货币": 10, "稳定币": 10, "区块链": 6,
    "币圈": 8, "代币": 8, "空投": 6, "爆仓": 10, "清仓": 6, "巨鲸": 8,
}

# ---- 市场关联词：强传闻词命中后，还需关联市场才归类传闻 ----
# （否则"阿联酋撤回航班授权"这类命中"有消息称"的国际新闻会刷屏；
#   辟谣豁免。覆盖股债两侧，价格行为词也算市场关联。）
MARKET_TERMS: tuple[str, ...] = (
    # 股市
    "A股", "沪指", "深指", "创业板", "科创板", "北交所", "港股", "美股",
    "上市公司", "股价", "涨停", "跌停", "板块", "券商", "市值", "收盘",
    "开盘", "北向资金", "龙虎榜", "个股", "增持", "减持", "定增", "举牌",
    "停牌", "复牌", "拉升", "跳水", "闪崩", "炸板", "封板", "放量", "急拉",
    # 债市
    "债市", "债券", "现券", "收益率", "资金面", "流动性", "国债期货",
    "信用债", "利率债", "转债", "可转债", "回购", "债基", "理财", "违约",
    # 机构与产品
    "基金", "私募", "公募", "信托", "保险资金", "社保",
    # 加密（20261004）
    "比特币", "以太坊", "加密货币", "币圈", "区块链", "稳定币", "代币",
    "TRX", "波场", "USDT", "USDC", "交易所公告",
)

# 词边界模式：AI 匹配 BAIDU/CHAIR 这类英文子串会误伤，ST 同理
_EXTRA_PATTERNS: list[tuple[re.Pattern, str, int]] = [
    (re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])"), "AI", 6),
    (re.compile(r"(?<![A-Za-z])ST(?![A-Za-z])"), "ST", 6),
]


def _compile(words: dict[str, int] | tuple) -> re.Pattern:
    keys = list(words)
    # 按词长降序拼接，长词优先命中，避免"传闻"吞掉"传闻称"
    return re.compile("|".join(map(re.escape, sorted(keys, key=len, reverse=True))))


STRONG_RE, WEAK_RE = _compile(STRONG_RUMOR), _compile(WEAK_RUMOR)
DEBUNK_S_RE, DEBUNK_W_RE = _compile(DEBUNK_STRONG), _compile(DEBUNK_WEAK)
OFFICIAL_RE, IMPACT_RE = _compile(OFFICIAL), _compile(IMPACT)

# A股/转债六位代码（含北交所 4/8、深市B股 2；无 7 开头代码）
CODE_RE = re.compile(r"(?<!\d)[0-68-9]\d{5}(?!\d)")


def literal_codes(text: str) -> set[str]:
    return set(CODE_RE.findall(text))


def _cap(total: int, limit: int) -> int:
    return min(total, limit)


class Scorer:
    def __init__(self, watchlist: list[dict]):
        self.watch_names = [w["name"] for w in watchlist if w.get("name")]
        self.watch_codes = {w["code"] for w in watchlist if w.get("code")}

    def score(self, title: str, text: str, linked: set[str] | None = None,
              known_codes: set[str] | None = None,
              tier: str = "authoritative") -> dict:
        """linked：流水线算好的"近期异动且在本文出现"的标的（代码或名称）。
        known_codes：已知标的全集（异动缓存∪关注列表）。文本中的 6 位数字只有
        命中已知标的才算"市场关联"——否则任意金额/人数/编号都会打穿门槛。
        tier：S4 来源可信度维度（'street'=股吧/TG 小道源），只影响 STRONG/WEAK
        传闻词分（×0.7 降档），不改变分类规则。"""
        full = f"{title}\n{text}"
        hits = {
            "strong": STRONG_RE.findall(full),
            "weak": WEAK_RE.findall(full),
            "debunk_s": DEBUNK_S_RE.findall(full),
            "debunk_w": DEBUNK_W_RE.findall(full),
            "official": OFFICIAL_RE.findall(full),
            "impact": IMPACT_RE.findall(full),
        }
        for pat, kw, _w in _EXTRA_PATTERNS:
            if pat.search(full):
                hits["impact"].append(kw)

        keywords = sorted({k for ks in hits.values() for k in ks})
        targets = sorted({n for n in self.watch_names if n in full}
                         | {c for c in literal_codes(full) if c in self.watch_codes})

        known = known_codes or set()
        code_known = bool(literal_codes(full) & known)
        market_linked = bool(targets or hits["impact"] or code_known
                             or linked or any(t in full for t in MARKET_TERMS))

        if hits["debunk_s"] or (hits["debunk_w"] and hits["strong"]):
            # 辟谣/澄清天然高价值，豁免市场门槛（分类不受限）
            cls, base = "debunk", 56
            score = base + _cap(sum(DEBUNK_STRONG.get(k, 0) for k in hits["debunk_s"])
                                + sum(DEBUNK_WEAK.get(k, 0) for k in hits["debunk_w"]), 26)
        elif hits["strong"] and market_linked:
            cls, base, score = "rumor", 55, 55
        elif hits["strong"]:
            # 强传闻词但与市场无关（国际政治等噪音）→ 不入传闻流，最后统一打折
            cls, base, score = "noise", 0, 0
        elif hits["official"] and (hits["impact"] or targets):
            cls, base, score = "official", 45, 45
        else:
            cls, base, score = "noise", 0, 0

        if cls in ("rumor", "noise"):
            # 传闻词加成只给传闻/噪音类；辟谣、官方发布稿里的"纪要/拟/有望"
            # 是其自身文体词汇，参与加成会把官方纪要发布日顶到告警线上
            # 小道源默认可疑（20261003 门7 已裁）：street 源 STRONG/WEAK 词分
            # ×0.7 降档——智者采纳（强词"传闻"22→15 与草案锚点恰合；系数统一
            # 优于强/弱非对称补丁）。封顶不缩放：多词命中会同达 cap、降档被吃，
            # 其危害载荷（打扰通道穿透）已由 street 退出 webhook/alert 裁断拆除，
            # 仅余 feed 排序影响，容忍。
            # 辟谣天然豁免（本分支不覆盖 debunk）；market 门槛不豁免（股吧
            # 噪声海啸防线照旧）；"异动关联"加分逻辑照旧不受系数影响。
            rumor_coef = 0.7 if tier == "street" else 1.0
            score += _cap(round(sum(STRONG_RUMOR.get(k, 0) for k in hits["strong"])
                                * rumor_coef), 26)
            score += _cap(round(sum(WEAK_RUMOR.get(k, 0) for k in hits["weak"])
                                * rumor_coef), 12)
        score += _cap(sum(OFFICIAL.get(k, 0) for k in hits["official"]), 10)
        score += _cap(sum(IMPACT.get(k, 0) for k in hits["impact"]), 18)

        if cls == "noise" and hits["strong"]:
            # 市场关联门槛：三折，保证国际噪音永远够不到告警线
            score = round(score * 0.3)
        # 与市场毫无关联的辟谣（地缘辟谣等）价值有限，压到告警线下，
        # 防止 7x24 流里"X方辟谣"类条目刷爆 webhook
        if cls == "debunk" and not market_linked:
            score = min(score, 75)

        if targets:
            score += 15
        if linked:
            # 异动关联只加分并打关键词，不并入 targets——"关注"语义严格等于
            # 关注列表命中，避免异动标的悄悄混进关注页签和 webhook 分支
            score += 12
            keywords = sorted(set(keywords) | {"异动关联"})
        impacts = detect(full)  # 资产影响方向：独立先验维度，不参与加减分
        return {"cls": cls, "score": min(100, score), "keywords": keywords,
                "targets": targets, "watch": bool(targets), "impacts": impacts}
