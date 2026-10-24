"""资产影响方向规则引擎（先验映射，非模型）。

⚠️ 校准警告（务必了解再使用）：
本模块的方向判定是教科书一阶先验（如"美联储降息→黄金利多"），不含幅度预期、
不含"利好出尽"类反讽、不含资金流现实；仅用于信息速筛与归类，不构成投资建议。
规则表必须靠真实数据持续回看校准（最激进三条规则在 RULES note 里写明回看义务），
错得多的规则应整条降为 rel 或删除——诚实比自信值钱。

双层表设计（职责分离，禁止 if-else 堆叠）：
- ASSETS 资产检测表：命中 → 该资产标 rel（相关/待定）。覆盖"有资产无事件"
  的价格快讯，也是前端按资产筛选的召回基础。
- RULES 事件方向规则：命中 → 按规则 effects 给方向，覆盖同资产的 rel。
合并：同资产 bull+bear 并存 → 降级 rel（k="方向冲突"）；rel 不覆盖已有方向。

防误伤铁律（改表必须遵守）：
1. 禁裸字：金（资金/本金）、币（人民币）、油（加油站）、债（还债）、股。
2. "股市"禁入 A 股检测（"美国股市"会误命中），用 A股/沪指 等专属词。
3. 铜/铝/镍 单字允许（期货语境基本必指金属）。
4. 美联储/Fed/央行/鲍威尔 是事件源不是资产，只出现在 RULES 触发词里，
   绝不进 ASSETS——否则每条央行新闻都给全资产刷"相关"。
"""
from __future__ import annotations

import re

# 引擎版本：词表/规则语义变更时 +1；store 据此幂等重算存量 impacts
# v9：债市拆分连缀修补（智者 F-1）——'美国债市'中"国债"仅前 1 字符'美'，
#     (?<!美国) 退化为"不等'美国'"而放行；负向断言对任意前缀连缀原理上不完备。
#     词表快修（国债加 (?<!美)、债市加 (?<!美国)）+ detect rel 兜底层加专名优先
#     消歧（中债泛词命中全部连缀在美债专名上时跳过 bond rel，真双谈保留）；
#     bond 增"中债"词，bond_us 增"美国债"省称
# v8：债市拆分中债(bond)/美债(bond_us)（用户 20261006 任务书——"债市到底是中国
#     债市还是美债？是不是没有区分"）；fed_ease/fed_hawk/us_data_weak/
#     us_data_strong 给 bond_us 定向（宽松压收益率=美债 bull，市场惯例，回看）
# v10：geo_war 补军事行动/无人机袭击/遭袭/激烈争夺/摧毁目标/攻占；fin_sanction
#     补对俄制裁变体（非连续串）；crypto_off 补英文 Ban（加密语境守卫）。新增词
#     均标记回看义务，上线两周复核误报率。
# v11：cn_ease 不再给 realestate 投票（20261022 打分机制批+智者裁决：宏观降准
#     降息≠地产直接利多，搭车票曾占地产票数大头 40/58 条；拆除后地产 15 个有票
#     日中 5 天诚实变无票，其余由 house_ease 独立支撑）
VER = 11

DIRS = ("bull", "bear", "rel")

# 负向词三档（±12 字窗口；v5/R2 教训：两档豁免粒度太粗——"数据公布后，交易员
# 降低对加息的押注"是宽松向句子，fact 豁免曾把它放回 fed_hawk bear）：
# NEG_FLIP 方向反转词——事件方向本身反了（"暂停加息"），永不豁免；
# NEG_WEAKEN 预期减弱动词——"降低/削减…押注(压力)"=该方向在退潮，永不豁免；
# NEG_BET 预期头寸名词（押注/概率）——句子讲头寸而非事件，仅当同窗口命中
#   落地词（宣布/公布…）时豁免："宣布降息25基点，交易员押注12月再降"。
NEG_FLIP = ("暂缓", "放缓", "降温", "暂停", "停止", "不加息", "落空",
            "推迟", "接近尾声", "预期减弱")
NEG_WEAKEN = ("削减", "打消", "减少", "降低", "缩减", "下降", "下行",
              "缓解", "疲弱", "疲软")
NEG_BET = ("押注", "概率")
# ±12 字窗口含这些词 → 该方向标记为 fact（已落地），否则 guess（预期阶段）
_FACT_WORDS = ("落地", "宣布", "公布", "正式", "出炉", "通过", "出台",
               "确认", "签署", "批准")
# 中国政策规则的海外央行排除词（v7：欧/英/日央行降息与中国资产方向无关，
# "欧洲央行降息25基点"曾误触 cn_ease 给 A股/债市标利多）
_NON_CN = ("美联储", "Fed", "联储", "鲍威尔", "欧洲央行", "欧央行", "ECB",
           "拉加德", "英国央行", "英格兰银行", "BOE", "日本央行", "日央行",
           "BOJ", "植田")


def R(rid: str, kws: tuple[str, ...], fx: dict[str, str],
      note: str = "", neg: tuple[str, ...] = (), base_neg: bool = True,
      override: bool = False) -> dict:
    """base_neg=False 用于触发词本身就是"被负向词拦下后接住"的规则（fed_pause），
    或数据解读族（触发词内含 预期差 词，防自我屏蔽）。
    override=True 的规则（数据解读族）方向更具体：detect 先跑 override 遍
    锁定方向，动作词规则不得反转/降级（v5 修复：原实现只覆盖表序在前者）；
    "方向冲突"粘滞态仍不可复活。"""
    return {"id": rid, "kws": kws, "fx": fx, "note": note,
            "neg": (NEG_FLIP + NEG_WEAKEN + NEG_BET + tuple(neg))
            if base_neg else tuple(neg),
            "neg_bet": NEG_BET if base_neg else (),
            "override": override}


# ---------------- 资产检测表（命中即 rel，事件源词严禁入内） ----------------
ASSETS: dict[str, dict] = {
    "a_stock": {"name": "A股", "icon": "🇨🇳", "kws": (
        "A股", "沪指", "上证", "深成指", "创业板", "科创板", "北交所",
        "沪深两市", "大盘", "两融", "北向资金")},
    "bond": {"name": "中债", "icon": "🇨🇳", "kws": (
        "债市", "(?<!美国)债券", "(?<!美国)(?<!美)国债", "特别国债", "现券",
        "(?<!美债)收益率", "中债", "信用债", "利率债", "城投", "地方债",
        "化债", "转债", "可转债", "DR007", "R007", "资金面", "同业存单",
        "存单", "净投放", "净回笼", "MLF", "逆回购", "LPR")},
    "bond_us": {"name": "美债", "icon": "🇺🇸", "kws": (
        "美债", "美国国债", "美国债券", "美国债市", "美国债")},
    "hk_stock": {"name": "港股", "icon": "🇭🇰", "kws": (
        "港股", "恒生", "恒指", "南向资金", "港股通", "港交所", "香港股市")},
    "us_stock": {"name": "美股", "icon": "🇺🇸", "kws": (
        "美股", "纳指", "纳斯达克", "标普", "道指", "道琼斯", "美国股市",
        "华尔街")},
    "gold": {"name": "黄金", "icon": "🥇", "kws": (
        "黄金", "金价", "伦敦金", "沪金", "COMEX", "白银", "贵金属",
        "金银", "金条")},
    "oil": {"name": "原油", "icon": "🛢️", "kws": (
        "原油", "油价", "OPEC", "欧佩克", "布伦特", "WTI", "成品油", "石油")},
    "commodity": {"name": "工业品", "icon": "🏭", "kws": (
        "有色", "工业金属", "铜", "铝", "镍", "螺纹", "铁矿", "焦煤",
        "焦炭", "黑色系", "双焦", "化工品", "PVC", "甲醇", "玻璃",
        "纯碱", "尿素")},
    "fx": {"name": "汇率", "icon": "💱", "kws": (
        "汇率", "人民币", "美元", "离岸", "在岸", "中间价", "外汇",
        "美元指数", "CNH")},
    "realestate": {"name": "地产", "icon": "🏠", "kws": (
        "房地产", "楼市", "房价", "房贷", "存量房贷", "限购", "收储",
        "土地市场", "售楼")},
    "em_stock": {"name": "新兴市场", "icon": "🌏", "kws": (
        "新兴市场", "MSCI新兴", "越南股市", "印度股市", "印尼",
        "东南亚股市")},
    "crypto": {"name": "加密货币", "icon": "🪙", "kws": (
        "比特币", "加密货币", "虚拟货币", "稳定币", "数字货币", "以太坊",
        "币圈", "USDT", "比特币ETF")},
}

# ---------------- 事件方向规则（表序即优先级；rel=相关/待定，不猜方向） ----------------
# 非美语境守卫（v5/R1）：就业触发词是通用的，中国/欧澳日就业数据弱=中国经济弱
# =中国资产利空，与美国宽松逻辑无关，绝不能误触 us_data_* 全系 bull
_NON_US = ("中国", "统计局", "城镇", "调查失业率", "澳大利亚", "欧元区",
           "英国", "日本", "青年")
RULES: list[dict] = [
    # 中国货币政策与资金面
    # 注：cn_* 挂海外央行排除词（v7）——"美联储降息"含裸"降息"、"欧洲央行
    # 降息25基点"曾误触 cn_ease 给中国资产标反方向；非中央行归属 other_cb
    R("cn_ease", ("降准", "降息", "LPR下调", "政策利率下调", "OMO利率下调",
                  "下调准备金率"),
      {"a_stock": "bull", "bond": "bull", "fx": "bear"},
      note="中国宽松≠黄金利多（黄金只挂美联储规则）；fx bear=人民币贬值压力；"
           "v11 拆 realestate 搭车票（宏观宽松≠地产直接利多，地产只认 house_ease）",
      neg=_NON_CN + ("是否", "与否", "悬念")),  # 问句前瞻稿不得给方向
    R("cn_tight", ("加息", "上调利率", "政策利率上调", "升准", "上调准备金率"),
      {"a_stock": "bear", "bond": "bear", "fx": "bull"},
      note="'暂停加息/加息预期降温'由负向词拦截",
      neg=_NON_CN),
    R("dep_rate", ("存款利率下调", "下调存款利率", "存款利率补降"),
      {"bond": "bull"}, note="比价效应/存款搬家",
      neg=_NON_CN),
    R("liq_loose", ("净投放", "超额续作", "加量续作"), {"bond": "bull"}),
    R("liq_tight", ("净回笼", "缩量续作", "资金面收紧"), {"bond": "bear"}),
    # 美联储与海外央行
    R("fed_ease", ("美联储.{0,6}降息", "Fed.{0,6}降息", "联储.{0,6}降息",
                   "鲍威尔放鸽", "美联储放鸽", "美联储转向宽松"),
      {"gold": "bull", "us_stock": "bull", "a_stock": "bull",
       "em_stock": "bull", "fx": "bull", "crypto": "bull", "bond_us": "bull"},
      note="fx bull=人民币被动升值/美元弱；中债不标（中美利差传导不稳），"
           "美债 bull=宽松压收益率（v8 常规传导）。"
           "全表激进规则之一，上线两周回看误报率",
      neg=("是否", "与否", "悬念")),
    R("fed_hawk", ("美联储.{0,8}加息", "Fed.{0,8}加息", "美联储放鹰",
                   "鲍威尔放鹰", "缩表加速"),
      {"gold": "bear", "us_stock": "bear", "em_stock": "bear",
       "fx": "bear", "crypto": "bear", "a_stock": "rel", "bond_us": "bear"},
      note="A股传导不稳故只 rel；'暂停加息'类由负向词拦截+fed_pause 接住"),
    R("fed_pause", ("暂停加息", "停止加息", "加息接近尾声", "最后一次加息"),
      {"gold": "bull", "us_stock": "bull"}, base_neg=False, neg=("落空",),
      note="接住被 fed_hawk 负向词拦下的条目，方向按宽松预期；"
           "base_neg=False——触发词本身含负向词，否则自我屏蔽；"
           "刻意不含'暂停降息'（那是收紧倾向）与裸'美联储转向'（方向歧义）"),
    R("other_cb", ("日本央行", "日央行", "BOJ", "欧央行", "欧洲央行", "英国央行"),
      {"gold": "rel", "us_stock": "rel", "fx": "rel"},
      note="套息/全球流动性传导方向不稳，只标相关"),
    # 美国经济数据解读（override：数据+预期差的方向比动作词更具体，
    # 命中时覆盖同文本误触发的动作规则——试金石：10-02 非农不及预期→美股大涨）
    # 非美语境守卫（v5/R1）：就业触发词是通用的，中国/欧澳日就业数据弱=中国经济
    # 弱=中国资产利空，与美国宽松逻辑无关，绝不能误触 us_data_* 全系 bull；
    # "升幅/降幅低于预期"=数据强于/弱于预期，方向相反，同样拦下
    R("us_data_weak", (r"(非农|新增就业|就业人数|就业报告|ADP|失业率升)"
                       r".{0,14}(不及预期|低于预期|逊于预期|大幅放缓|意外下滑|疲弱|恶化|降温)",),
      {"us_stock": "bull", "gold": "bull", "em_stock": "bull",
       "crypto": "bull", "fx": "bull", "bond_us": "bull"},
      note="美国数据弱=宽松预期升温→风险资产与黄金利多、美元弱（人民币被动升）、"
           "美债 bull（v8）。fx bull 同 fed_ease 口径；中债不标（传导不稳）",
      base_neg=False,
      neg=("是否", "与否", "悬念", "预期减弱") + _NON_US + ("升幅", "降幅"),
      override=True),
    R("us_data_strong", (r"(非农|新增就业|就业人数|就业报告|ADP|失业率降)"
                         r".{0,14}(超预期|强于预期|大超预期|意外强劲|大幅回升|强劲增长)",),
      {"us_stock": "bear", "gold": "bear", "em_stock": "bear",
       "crypto": "bear", "fx": "bear", "bond_us": "bear"},
      note="美国数据强=紧缩预期升温，与 us_data_weak 全对称（含 v8 美债 bear）",
      base_neg=False,
      neg=("是否", "与否", "悬念", "预期减弱") + _NON_US + ("降幅",),
      override=True),
    # 贸易、地缘、原油
    R("tariff_up", ("加征关税", "上调关税", "贸易战", "贸易摩擦升级",
                    "301调查", "关税大棒"),
      {"a_stock": "bear", "us_stock": "bear", "gold": "bull", "fx": "bear",
       "em_stock": "bear", "commodity": "bear"},
      note="fx bear=人民币贬值压力（2018 主逻辑）。全表激进规则之一，回看义务"),
    R("tariff_dn", ("关税豁免", "取消关税", "下调关税", "关税缓和", "贸易协议",
                    "磋商取得进展"),
      {"a_stock": "bull", "us_stock": "bull", "fx": "bull", "em_stock": "bull",
       "gold": "bear"},
      note="避险退潮利空黄金；与 tariff_up 拆两条，绝不共用'关税'一词"),
    R("export_ctrl", ("实体清单", "出口管制", "芯片禁令", "技术封锁"),
      {"a_stock": "rel"},
      note="国产替代利多 vs 供应链利空，方向真分歧，只标 rel"),
    R("geo_war", ("空袭", "导弹袭击", "军事冲突", "爆发战争", "交火", "遇袭",
                  "军事行动", "无人机袭击", "遭袭", "激烈争夺", "攻占",
                  r"摧毁.{0,12}(?:平台|阵地|基地|设施|目标|导弹|无人机|军事|装备|舰|坦克|哨所|据点)"),
      {"gold": "bull", "oil": "bull", "a_stock": "rel"},
      note="oil bull 是供给风险默认值，非产油区冲突会误标；新增词回看义务"
           "（重点复核'摧毁/攻占'在非军事语境的误报）"),
    R("fin_sanction", ("金融制裁", "SWIFT", "冻结资产", "制裁俄罗斯",
                       "制裁伊朗", "对俄罗斯实施制裁", "对俄制裁",
                       r"对(?:俄罗斯|俄).{0,12}制裁"),
      {"gold": "bull"}, note="去美元化叙事；新增对俄制裁变体回看义务"),
    R("oil_supply", ("OPEC减产", "减产延长", "霍尔木兹", "红海", "胡塞",
                     "中东局势"),
      {"oil": "bull"}),
    R("oil_glut", ("OPEC增产", "原油增产", "释放石油储备", "抛储"),
      {"oil": "bear"}, note="'增产'禁裸词，防'粮食增产'"),
    # 财政与债市供给
    R("bond_supply", ("国债增发", "增发国债", "特别国债发行", "供给放量"),
      {"bond": "bear", "a_stock": "rel"},
      note="供给冲击利空债市；财政发力对股市方向不稳故 rel"),
    R("huazhai", ("化债", "债务置换", "城投化债"),
      {"bond": "bull", "a_stock": "rel"}, note="城投风险缓释"),
    R("fiscal", ("财政刺激", "特别国债", "专项债", "赤字率上调", "财政发力"),
      {"a_stock": "bull", "commodity": "bull"},
      note="'特别国债'会同时命中 bond_supply（bond bear）——bear 压过 rel"),
    # 地产
    R("house_ease", ("取消限购", "限购放开", "降低首付", "房贷利率下调",
                     "收储", "认房不认贷", "楼市新政"),
      {"realestate": "bull", "a_stock": "bull", "commodity": "bull",
       "bond": "bear"},
      note="bond bear 是全表最激进一条：地产宽松=宽信用预期压制利率债。"
           "上线两周重点回看，误报多就降 rel"),
    R("house_tight", ("恢复限购", "限购升级"),
      {"realestate": "bear", "bond": "bull"}),
    # 股市制度与资金
    R("nat_team", ("平准基金", "国家队", "汇金增持", "汇金买入",
                   "中长期资金入市", "险资入市"),
      {"a_stock": "bull", "hk_stock": "bull"}, note="汇金也买港股 ETF"),
    R("stamp_tax", ("印花税下调", "调降印花税", "印花税减半"),
      {"a_stock": "bull"}, note="只做下调向"),
    R("ipo_up", ("IPO重启", "IPO提速", "恢复IPO"), {"a_stock": "bear"},
      note="抽血叙事"),
    R("ipo_dn", ("IPO收紧", "暂缓IPO", "IPO放缓"), {"a_stock": "bull"}),
    R("cny_dn", ("人民币跌破", "贬值压力", "人民币贬值",
                 "汇率破7", "人民币破7", "离岸破7"),
      {"fx": "bear", "a_stock": "bear"},
      note="2015/2018 贬值恐慌逻辑；'破7'必须带主语（裸破7会误伤金价跌破700）；"
           "正常波动勿放大，靠 score 门挡"),
    R("cny_up", ("人民币升值", "中间价超预期"), {"fx": "bull"}),
    # 会议、数据、加密（方向未知区）
    R("meetings", ("政治局会议", "经济工作会议", "国常会", "两会", "财经委",
                   "深改委", "企业座谈会"),
      {"a_stock": "rel", "bond": "rel"},
      note="用户口径：'看会议表态'——表态出来前一律相关"),
    R("data_rel", ("CPI", "PPI", "PMI", "GDP", "社融", "金融数据"),
      {"a_stock": "rel", "bond": "rel"},
      note="方向取决于数值 vs 预期，规则引擎不判数值，一律相关"),
    R("crypto_on", ("比特币ETF获批", "现货ETF", "虚拟资产牌照"),
      {"crypto": "bull"}),
    R("crypto_off", ("打击虚拟货币", "清退", "禁止加密",
                     r"(?=.*(?:[Cc]rypto|[Bb]itcoin|BTC|虚拟货币|加密货币|稳定币|USDT|区块链|币圈|P2P))"
                     r"(?!.*(?:P2P\s*(?:lending|网贷)|网贷\s*P2P)).*?\b[Bb]ans?\b"),
      {"crypto": "bear"},
      note="英文 Ban 仅在加密语境守卫内触发；P2P 借贷类显式排除；回看义务"),
]

# 表结构保留扩展位：某规则预期阶段方向常错时置 True 即降级（V1 无实例）
for _r in RULES:
    _r.setdefault("flip_on_guess", False)

_WINDOW = 20   # 央行全称4字+日期跨度，12曾把欧洲央行切断成洲央行  # 负向词/落地词的前后探测窗口（字符）


def _compile(kws) -> re.Pattern:
    # 关键词即正则片段（允许 .{0,6} 这类弹性连接），表内禁用正则元字符需自律。
    # 注意：负向词窗口是字符串切片（跨 \n），而 . 不跨行——不对称但偏保守，无害
    return re.compile("|".join(sorted(kws, key=len, reverse=True)))


_RULE_RES = [_compile(r["kws"]) for r in RULES]
_ASSET_RES = {a: _compile(v["kws"]) for a, v in ASSETS.items()}


def _near(full: str, m: re.Match, words) -> bool:
    w = full[max(0, m.start() - _WINDOW): m.end() + _WINDOW]
    return any(x in w for x in words)


def _neg_blocked(full: str, m: re.Match, rule: dict) -> bool:
    """负向词三档裁决（v5/R2）：
    - 命中 NEG_FLIP / NEG_WEAKEN / 语境守卫 / 问句词 → 一律拦截；
    - 仅命中 NEG_BET（押注/概率）且同窗口有落地词 → 豁免（落地事件与
      头寸讨论同现时事件有效）；无落地词 → 拦（CME 概率陈述）。"""
    if not rule["neg"]:
        return False
    w = full[max(0, m.start() - _WINDOW): m.end() + _WINDOW]
    hits = [x for x in rule["neg"] if x in w]
    if not hits:
        return False
    bet = set(rule.get("neg_bet") or ())
    if _near(full, m, _FACT_WORDS):
        return any(x not in bet for x in hits)
    return True


def _merge(out: dict, a: str, d: str, k: str, g: str, override: bool = False):
    cur = out.get(a)
    if cur is None:
        out[a] = {"a": a, "d": d, "k": k, "g": g, "ov": override}
        return
    if cur["k"] == "方向冲突":  # 粘滞：见过 bull+bear 就永远 rel（override 也不复活）
        return
    if cur.get("ov") and not override:
        # v5/R4：override（数据解读）方向已锁定——后到的动作词规则
        # 不反转也不降级（谁更具体谁赢，与表序无关）
        return
    if override and cur["d"] != d and cur["d"] != "rel":
        cur["d"], cur["k"], cur["g"], cur["ov"] = d, k, g, True
        return
    if {cur["d"], d} == {"bull", "bear"}:  # 只有 bull×bear 冲突才降级
        cur["d"], cur["k"] = "rel", "方向冲突"
        cur["ov"] = False
        return
    if cur["d"] == "rel" and d != "rel":   # 方向覆盖 rel，但不反向覆盖
        cur["d"], cur["k"], cur["g"] = d, k, g


def _bond_shadowed_by_us(full: str) -> bool:
    """v9 专名优先消歧（智者 F-1）：中债泛词命中若全部"连缀"在美债专名上——
    命中区间落在专名区间内部（'美国国债'内的'国债'）或与专名无缝紧邻
    （'美国债市|收益率'零间隔）——则该文的中债证据只是专名字母本身，
    rel 兜底跳过 bond；存在带分隔字的独立命中（'美债与中债分化'）则保留。
    负向断言对任意前缀连缀原理上不完备（v8→v9 教训），此为区间级消歧终点。"""
    us_spans = [m.span() for m in _ASSET_RES["bond_us"].finditer(full)]
    if not us_spans:
        return False
    cn = list(_ASSET_RES["bond"].finditer(full))
    if not cn:
        return False

    def shadowed(m: re.Match) -> bool:
        s, e = m.start(), m.end()
        for us, ue in us_spans:
            if us <= s and e <= ue:      # 专名内部（连缀）
                return True
            if e == us or s == ue:       # 无缝紧邻（连缀，零间隔）
                return True
        return False

    return all(shadowed(m) for m in cn)


def detect(full: str) -> list[dict]:
    """返回 [{"a": asset_key, "d": bull|bear|rel, "k": 触发来源, "g": fact|guess}]

    两遍扫描（v5/R4）：先跑 override（数据解读）遍锁定方向，再跑动作词遍——
    数据信号与表序无关地赢过动作词；普通规则间的 bull×bear 冲突降级粘滞不变。
    已知边界：同文本同时命中 us_data_weak 与 us_data_strong（罕见）时，
    两遍扫描内表序恒定 strong 胜——同为数据规则，强数据的紧缩信号更罕见
    更具体，可接受；如需精细裁决再加互斥表。
    """
    out: dict[str, dict] = {}

    def try_rule(rule, rex):
        m = rex.search(full)  # 一规则一次命中即够
        if m and not _neg_blocked(full, m, rule):
            stage = "fact" if _near(full, m, _FACT_WORDS) else "guess"
            return m, stage
        return None

    for pass_override in (True, False):
        for rule, rex in zip(RULES, _RULE_RES):
            if bool(rule.get("override")) != pass_override:
                continue
            r = try_rule(rule, rex)
            if r:
                m, stage = r
                for a, d in rule["fx"].items():
                    _merge(out, a, d, rule["id"], stage,
                           override=pass_override)
    skip_bond_rel = _bond_shadowed_by_us(full)   # v9 专名优先消歧（只豁免 rel 兜底，
    for a, rex in _ASSET_RES.items():            # 规则定向不受影响——降准+美债同文仍各归各）
        if a == "bond" and skip_bond_rel:
            continue
        m = rex.search(full)
        if m:
            _merge(out, a, "rel", m.group(0), "fact")
    for v in out.values():
        v.pop("ov", None)  # 内部标记不进库
    return list(out.values())
