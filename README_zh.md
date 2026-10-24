# TT_Sniffer 情报台 · 7×24 小作文嗅探器

[English](README.md) · [简体中文](README_zh.md)

股市、债市的异常波动常常源于各种未经证实的"小作文"。它们先在微信群/QQ群里
发酵，几分钟后泄漏到公开平台（财联社电报、东财快讯、股吧、韭研公社…）。
TT_Sniffer 持续嗅探这些**公开二级传播源**，用规则引擎 + 可选 LLM 识别传闻
类信息，聚合到一个本地实时窗口，让你比"一两天后才知道"早得多。

<p align="center">
  <img src="docs/shots/feed.png" alt="TT_Sniffer 实时信息流" width="900">
</p>

## 为什么

小作文的传播路径是固定的：群内炮制 → 截图 → 股吧/韭研公社/公众号/TG 泄漏 →
财经媒体放大。等官方快讯上墙，价格已经动了。TT_Sniffer 盯的是**公开泄漏点**
（明确不碰私聊——见[合规边界](#合规边界)），并与官方快讯、辟谣声明交叉对撞。

## 功能全景

**采集 —— 20+ 源，一条流水线**

| 层 | 来源 | 说明 |
|---|---|---|
| 中文快讯 | 财联社 / 东财 / 新浪 7x24 / 同花顺 / 华尔街见闻 | 30–60 秒轮询，逐行容错 |
| 外网媒体 RSS | 彭博 ×5、WSJ ×4、MarketWatch、FT、CNBC、路透/AP（经 Google News） | 只收标题+摘要，不绕付费墙 |
| 小道源 ⚠️ | 股吧热帖（**详情补抓拿全文+精确时间**）、韭研公社（SSR 载荷解析+补全文）、Telegram 公开频道、加密媒体 RSS | `tier=street`：⚠️未证实标，永不进打扰通道 |
| 预测市场 | Polymarket（Gamma + CLOB + Data API） | 概率变动 ≥5 个百分点才发信号 |
| 盘口微观 | 盘口异动（封板/大笔买卖/高台跳水…）+ 涨停池 | 驱动"价格先动+传闻文本"联动加分 |
| 宏观 | 宏观日历（非农/CPI/FOMC/SEP…中美）、CME 联邦基金期货每日快照、官方 SEP 点阵自动抓取 | |
| 声明式订阅 | `subscriptions/*.yaml` 丢文件即订阅 | 热加载、schema 校验、零代码免重启 |

**信号引擎**

- 强弱两层传闻词表 + 市场关联门槛：没碰到真实市场实体的"传闻"按噪声三折处理；
- 关注列表（股票/债券/关键词）命中加分；盘中异动与传闻文本同现再加"价格先动"分；
- 小道源打分 ×0.7 且 ⚠️ 标记；辟谣条目不降档（小道渠道的辟谣是真金）；
- 资产情绪：利多/利空论据加权投票 + K=3 置信平滑；散点/矩阵用连续强度映射
  （票数 tanh），1 票和 3 票看得出差别；
- **同类聚合**：跨源 SimHash 指纹把"同文多帖/转载"合并成「N 条同类」；
  同日传闻与辟谣自动配对（辟谣对撞）。

**可选 LLM（自带 OpenAI 兼容端点）——四个自动 + 一个底座**

- **自动翻译**：外文新闻自动译成中文，译文**直接内嵌在信息流条目里**——
  彭博标题和它的中文译文在同一张卡片（空闲时自动补译积压）；
- **自动锐评**：高分/传闻类条目自动生成一句话戏谑短评，附在条目上、
  阅读器弹层里可见——毒舌但有分寸，专点荒诞处；
- **一键总结**：AI 对话抽屉对当日情报库即问即答——"今天最值得关注的传闻？"、
  "总结当前筛选下的利多/利空"、"哪些传闻已经被辟谣？"——流式 Markdown，
  只基于当天采集到的条目作答（不联网检索）；
- **自动预筛**：对达标条目给出 传闻/方向/涉及标的 判定，"非传闻"高置信
  可拦截噪声告警；
- **三通道独立底座**（translate / comment / chat），每通道自己的 base_url、
  模型与 **key 池**（轮换、60s 冷却、401 禁用、每轮配额上限），各通道可指向
  不同厂商。抓取文本一律当数据不当指令（prompt injection 防御）。

| 自动锐评 | 自动翻译 |
|---|---|
| <img src="docs/shots/reader-comment.png" width="780"> | <img src="docs/shots/reader-translate.png" width="780"> |

**界面**

<p align="center">
  <img src="docs/shots/stats.png" alt="统计视图" width="900">
</p>

- 单窗口双视图（信息流/统计），SSE 秒级推送、断线自动重连并补偿掉线期间条目；
- 按日浏览：14 日传闻/辟谣迷你柱状条带，任意历史日期直达；
- 筛选自由组合：分类/分数/方向/来源（分组 chip，整组开关）/资产/关键词正则/
  只看小道/折叠同类——全部同步进 URL，筛好的视图可直接分享；当前筛选可存模板；
- 阅读器弹层：全文 + 翻译 + LLM 短评 + 辟谣配对，键盘可达，Markdown 消毒防 XSS；
- **图表**：14 日传闻/辟谣趋势、24h 情报节奏（堆叠）、资产多空票数、来源分布、
  分类构成、热词、异动风向、风险×避险情绪散点（含每日轨迹）、
  **利率观点点阵**（类 FOMC dot plot，金环=共识档位）、**官方 SEP 点阵**、
  资产×日情绪矩阵；
- 宏观日历：国家/日期区间/关键词筛选、快捷词、列表+月历双视图、分页；
- 实时行情条：黄金/布伦特/纳指/上证/沪深300/恒指/USDCNH/KOSPI/中美 10Y
  共 10 项，8 秒刷新、单标的失败保留旧值、快照落盘（重启遇源故障不缺卡）；
- webhook 告警：企业微信/钉钉/飞书三格式，同标的 10 分钟节流，小道源不响铃；
- 可安装 PWA、深色主题、390px 移动端适配（底部 dock + 滑动手势）——
  手机比例是一等公民，不是缩水的附属品。

<p align="center">
  <img src="docs/shots/dotplot.png" alt="利率观点点阵" width="900">
</p>

| 移动端（390px，PWA） | AI 总结当日情报（真实对话） |
|---|---|
| <img src="docs/shots/mobile.png" width="360"> | <img src="docs/shots/ai-chat.png" width="780"> |

<p align="center">
  <img src="docs/shots/calendar.png" alt="宏观日历" width="900">
</p>

## 快速开始

需要 Python 3.11+。前端**已预构建**（`app/web/dist` 入库）——不改前端就
不需要 Node。

```bash
git clone https://github.com/TTDiang2/tt-sniffer.git
cd tt-sniffer
pip install -r requirements.txt
python -m app.main          # 或直接双击 run.bat
```

浏览器打开 <http://127.0.0.1:8787>。首轮静默回填最近条目，之后实时增量推送。
复制 `config.example.yaml` 为 `config.yaml` 按需修改（example 就是完整说明书：
每个开关、每个默认值、边界规则都写在注释里）。

## 测试与校验

```bash
pip install -r requirements-dev.txt
python -m pytest -q         # 337 个用例（浏览器 E2E 缺环境自动 skip）
python scripts/check.py     # 编译 + 导入 + 单测 + 门禁
python scripts/check.py --e2e   # 显式开启 P1–P4 Chrome CDP 套件
```

## 访问控制与公网部署

应用层 Basic Auth（PBKDF2 带盐哈希、配置残缺 fail-closed、连败 429）。默认仅
静态资源与 `/healthz` 公开，`/` 与 `/api/*` 均受保护；配合 Cloudflare Tunnel
是两道独立防线。哈希生成命令见 `config.example.yaml` 注释。

**游客只读模式（观察期）**：设 `auth.public_read: true` 后，任何人无需密码
即可 GET 公开数据（信息流/统计/行情），改配置与个人关注列表仍要凭据；再加
`auth.public_ai: true` 可把 AI 对话抽屉也对游客开放——对话端点自带
10 次/分钟每 IP 限速，真人畅聊、被当免费 LLM 代理薅则无利可图。每 IP 令牌
桶（`auth.rate_limit_rpm`，默认 300 次/分钟）防爬虫拖垮服务；所有请求按真实
访客 IP（`cf-connecting-ip`）逐条计数落本机 JSON。在服务器本机打开
`http://127.0.0.1:8787/api/access` 即可查看统计（仅回环可访问，其他人
得到 404）。用数据决策：唯一 IP 少、429 常年为零、没有扫描器狂打 API，
就说明没人恶意偷数据，游客模式可以一直开着——反正都是公开数据。

## 合规边界

这些红线是刻意画的，法律依据见 `docs/reports/` 调研报告：

- **不做私聊监控**：微信群/QQ 群协议破解、数据库解密、Hook、iPad 协议、群控
  一律不做（封号+刑责）。产品成立的前提是"小作文必须泄漏到公开平台才能影响
  价格"——公开二级传播层就够了；
- **不绕付费墙**、不抓登录后内容、不破验证码、不用代理轮换规避封禁、不做
  账号池、不转售内容、不用抓取文本训练模型（雪球条款明令禁止）；
- 采集礼貌优先：候选过滤、每轮上限、失败冷却、详情补抓 1 请求/秒、
  持续失败的源安静退避而不是硬撞。

## 目录结构

```
app/
  main.py            入口：uvicorn + 采集流水线单进程
  config.py          配置加载（config.yaml 优先，缺省回退 example）
  models.py          Item 统一数据模型
  store.py           SQLite（WAL）：按天查询、SimHash 聚类、保留期清理
  scoring.py         规则引擎（强弱词分层+市场门槛+tier 降档）
  impact.py          主题词表（版本化，幂等重算存量）
  sentiment.py       资产情绪聚合（K=3 平滑）
  pipeline.py        采集->去重->打分->落库->广播/告警
  subscriptions.py   声明式订阅加载器（热加载）
  quotes.py          实时行情（8s TTL + 落盘持久化）
  fed_sep.py         FOMC SEP 点阵数据
  llm.py / push.py   可选 LLM 三通道 / webhook 推送
  collectors/        news, rss, crypto_rss, street(股吧详情+韭研+TG),
                     polymarket, anomaly, calendar, rates_snap
  web/               React 19 + Vite + Tailwind 4 前端（dist 入库）
subscriptions/       订阅文件放这里（schema 见目录内 README）
tests/               pytest 全量 + P1–P4 Chrome CDP 断言脚本
scripts/check.py     交付前校验关卡
docs/                调研与设计、事件报告、截图
```

## 许可证

代码采用 [MIT License](LICENSE)。新闻、行情等第三方数据版权归各来源平台，
不在本项目许可范围内。仅供个人信息聚合参考，**不构成投资建议**。
