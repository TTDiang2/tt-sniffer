# TT_Sniffer 前端评审报告与 shadcn/ui 迁移方案

- **日期**：2026-10-19
- **评审对象**：`app/web/static/index.html`（v26.2b，a28ffdb）+ `app/web/static/marked.js` + `app/server.py` 静态托管层
- **审查方式**：整仓拷贝隔离实例（8788 端口，sqlite backup 式真实数据副本，LLM/webhook 关闭）+ Chrome 155 CDP 实际渲染，1920/1280/390（设备仿真）三档共 18 张截图全量走查（信息流/统计五卡/阅读弹窗/关注弹窗/AI 抽屉/左右抽屉/空状态）；vision 子代理逐张像素级审图；6 项运行时取证（PoC）、30 个选择器 WCAG 对比度实测、DOM 几何测量、全量代码通读。截图存档 `%TEMP%\tts_fe_review\shots_review\`（18 张，审查完即弃）。
- **未验证项**：真实触屏设备、Firefox/Safari、读屏器全程走查（焦点问题为 DOM/代码级证据）、Lighthouse、公网隧道下渲染。沙箱行情源探通率低，个别截图仅 1 张行情卡属取证环境网络所致，非产品缺陷（生产 13 标的）。

---

## 决议（本文档前提）

**2026-10-19：正式废止「零构建链」约束。** 该约束是早期快速迭代验证期的产物，现已完成历史使命；升级方向锁定 **shadcn/ui（React + Vite + TypeScript + Tailwind）**。已核实（2026-10-19 grep）：仓库 `.md/.yaml/.html` 内并无该约束的成文条款，仅存在于 git commit 历史与早期会话记忆中——commit 历史不改写，无需删改。

本机 Node 现状：PATH 为 v18.16.1 / npm 9.5.1；另有 v22.22.2 位于 `~/.workbuddy/binaries/node/versions/22.22.2-2/`。**建议统一到 Node 22**（最新 Vite 7 / Tailwind v4 / shadcn CLI 均要求 Node ≥20）；若暂不升级，锁 Vite 6 + Tailwind 3.4 + shadcn@2.3 亦可完成本方案，无功能差异。

---

## 一、总评

这不是 AI 模板脸项目，而是一个**有真实设计意图、被真实使用反馈驱动过多轮迭代（v10→v26.2b）、工程质量高于个人爱好作品但低于商业产品线**的单用户专业工具。视觉方向（夜盘终端、账本式信息流、红涨绿跌+蓝官方金关注的四色语义、衬线报头数字）辨识度明确，可访问性有实测数据支撑；但执行层存在系统性破口：桌面 8–9px 小字遍布、图标双体系（emoji × SVG）、颜色语义过载、自研弹层无焦点管理、一个可利用的 markdown XSS，以及 2471 行单文件无前端测试的架构天花板。当前处在「自己用很好、给第二个用户用会暴露问题」的位置。**置信度：高**（结论均有运行时或像素级证据）。

## 二、六维评分

| 维度 | 分数 | 简要理由 | 证据充分度 |
|---|---|---|---|
| 视觉审美 | **6.5** | 方向与辨识度真实（oklch token 化 + 对比度实测全过 AA），但字号纪律（8.5–9px 轴标/脚注/热力标签）、行情卡尾行拉伸破格、徽章胶囊双形态、阅读弹窗 2/3 空置、桌面图表绘图区仅占卡片 ~30% 拉低完成度 | 高 |
| 产品设计 | **7** | 筛→扫→读→AI 任务闭环、URL 即状态可深链、模板/折叠组/SSE 断线补偿/新消息浮条、移动 dock+手势；扣分：AI scope 与浏览日脱钩无提示、历史日仍显示当日行情无日期限定、空态四套文案 | 高 |
| 组件库运用 | **4** | 零组件库。原生 select 用得对；但两套自研弹窗行为分叉、自写 markdown 解析器带 XSS、tooltip 全靠 `title` 属性、`prompt()/alert()` 与自研弹窗并存 | 高 |
| 设计系统 | **6** | oklch 语义 token 层 + 半径/阴影/焦点分档真实落地；但 92 处 oklch 多为行内字面量、主蓝硬编码 ×5、热力矩阵 rgba 与 --rumor 不同色、7 处 !important、魔法数（264/316/292/340/132px）多份拷贝 | 高 |
| 工程质量 | **5** | 状态/异步纪律高于平均（竞态令牌、转义前置、URL 校验、超时重试、后端 219 测试）；但单文件三语言纠缠、前端零测试入库、15 处静默 catch、`<header2>` 非法元素、marked.js XSS、重复声明 | 高 |
| 交付完整度 | **7** | 加载占位/空态/降级/错误文案/reduced-motion/触控目标 34px/390–1920 无横向溢出/全程零 console 错误；扣分：键盘与读屏可达性缺口、2 个功能性 bug | 高 |

## 三、最值得肯定的三个地方

1. **领域驱动的视觉身份**：账本式行布局（3px 左色轨按分类着色、金色轨=关注命中）、四色语义（红涨绿跌/蓝官方/金关注）、Georgia 仅用于报头与 hero 大数字、14 日热度日历、FOMC 式利率点阵——去掉 Logo 仍认得出是「夜盘交易台」。v19–v26 全部由用户真实规格驱动。
2. **状态与容错纪律**：`reloadSeq` 时序令牌防竞态（index.html:1144/1171）、`highlight()` 先整体转义再匹配转义词（:917-927）、深链全字段白名单校验（:952-971）、fetchOk 8s 超时+重试、AbortError 转人话文案（:1186-1190）、行情探不通整格降级、`prefers-reduced-motion` 四处覆盖。实测 18 场景零 JS 报错。
3. **可访问性有实测而非自夸**：CDP `color-mix` 归一化实测 30 个选择器，最低 4.89:1 全过 AA（与 v25 声称的 4.87 吻合）；移动触控目标 34px、focus-visible 金环真实存在。

## 四、最严重的五个问题

### ① marked.js 链接路径 XSS + `title="undefined"`（P1·安全，已实测）

- **证据**：`marked.js:18-34` 链接占位还原时链接文本（p[0]）未经转义直接拼回 HTML。运行时 PoC（输出原文）：
  - `marked.parse('[<img src=x onerror=window.__xss=1>](https://ok.com)')` → `<p><a href="https://ok.com" title="undefined" ...><img src=x onerror=window.__xss=1></a></p>`（裸标签注入，经 index.html:1390 `bubble.innerHTML` 执行）
  - `[t](https://a.com"onmouseover=alert(1))` → `href="https://a.com"onmouseover=alert(1"`（属性注入）
  - 次生 bug：无 title 的链接全部渲染 `title="undefined"`（:21 undefined 参与字符串拼接）
- **影响**：AI 抽屉把 LLM 输出直接 innerHTML；股吧/TG 小道源文本可经提示注入污染模型回复 → 在已过 Basic Auth 的浏览器上下文执行脚本（公网域名暴露面）。
- **修复**：迁移期直接换 `react-markdown + rehype-sanitize` 根除；若迁移前热修：还原占位时对 p[0]/p[2] 做 `inlEsc`、href 白名单后再转义双引号。

### ② 自研弹层体系无焦点管理（P1·可访问性，已实测）

- **证据**：三个弹层三种行为——阅读弹窗有 Esc+`role="dialog"`（:763）但无焦点圈闭、关闭后焦点不还原（实测停留在已隐藏的 `.rd-x`）；关注弹窗（:1243-1288）无 Esc、无 role、无右上关闭；AI 抽屉裸 ✕ 无按钮态。移动端**关闭态**左抽屉屏外（`translateX(-273px)`）但内含 **35 个可聚焦控件、无 inert/aria-hidden**（实测）。另有 `<header2>` 非法自定义元素（:771，实测渲染为 HTMLUnknownElement）。
- **修复**：短期补 inert + Esc 统一 + 焦点还原；根治走 Radix Dialog/Sheet（迁移方案 P2 期）。

### ③ 2471 行单文件 + 前端零测试入库（P2·工程债最高息项）

- **证据**：单文件 132KB 三语言纠缠；commit 自述的「32 playwright 断言」不在仓库（grep 仅命中 docs 截图文件名），入库测试仅后端 219 pytest；15 处静默 `catch(e){}`（loadBrief/loadQuotes/loadDays 失败即静默）；重复声明（`.dl-dr` z-index:80→90 连写 :519-521；`brief.hidden=false` 连写两遍 :1210-1211）。
- **影响**：回归只能人工截图验收；v26 用户报障「右看板载入失败」正是静默 catch 掩盖问题的实例。
- **修复**：随迁移拆模块；Playwright 冒烟断言入库为 CI 脚本。

### ④ 视觉一致性体系破口（P2·视觉）

- **证据**（均实测/像素级）：字号纪律失守——`hlabels` 8.5px、热度日标签 9px、矩阵头 9px、AI 帮助文案 ~9px；图标双体系——dock 用 SVG 线性图标，声音 🔇/AI 🗒/设置 ⚙/资产 emoji 混用且资产行有无图标导致标签左缘参差；颜色语义过载——绿=利空+实时+折叠组、蓝=官方+原文链接+主按钮+新消息浮条、金=关注+品牌+激活态；行情卡数字左对齐无千分位小数位不齐（4192.8 / 26140.03 / 3716.284），flex 尾行单卡拉伸至 190px 破网格（1280 实测 5+1 且尾卡宽 1.5 倍）；主蓝 `oklch(55% .13 255)` 硬编码 ×5；热力矩阵红 `rgba(224,86,79)` 与 `--rumor` oklch 非同色。
- **修复**：字号 token 设 10px 下限、图标统一 lucide、数字右对齐+千分位+固定小数位、色彩角色收敛为「语义四色 + 中性 + 唯一主操作色」。

### ⑤ 功能微缺陷群（P2·已实测 4 项）

| 缺陷 | 证据 | 修复 |
|---|---|---|
| `d=any`（「有方向」）深链回读丢失 | URL 写 `?d=any`（:942）但校验白名单不收（:963）→ 刷新后静默变「全部」（实测；后端 `d=bull,bear` 有 136 条证明能力在） | 校验列表加 `any` 分支 |
| 声音开关跨会话静默失效 | `audio` 仅在点击 toggle 时创建（:2148），localStorage 恢复 `state.sound=true` 后 `audio=null`，告警 TypeError 被吞（实测） | init 时按 state.sound 预建 AudioContext（需用户手势后 resume） |
| `#newbar{top:132px}` 魔法数过期 | 实际 header 高 106.6px（实测），浮条脱离 header 26px 悬浮，未复用已有 `--hh` 机制（:427 vs :1908-1918） | 改 `top:calc(var(--hh) + 8px)` |
| 抽屉内重复标题与断行 | 左抽屉「资产」标题 ×2（:2403-2404 搬入的分区头 + hd2b 内联 .lb，实测计数 2）；「模板」控件行在 264px 抽屉内 1920/1280 恒定断行（行高 45px、按钮与下拉顶边差 10px） | 删 hd2b 内联标签；模板行改两行式布局或加宽抽屉 |

另：`prompt()` 存模板（:2265）、`alert()` 报错（:1285）与自研弹窗并存，随迁移 P2 期统一。

## 五、组件库专项审计

| 类别 | 现状 | 评价 |
|---|---|---|
| UI 组件库 | 无（零 npm） | 零构建链约束下的选择，代价集中在弹层可达性与 markdown 安全 |
| 表单控件 | 原生 select/input[date]/checkbox | 合理，无需替换 |
| 弹窗 | 自研两套（#reader、.wmodal） | 重复造轮子且行为分叉（Esc/role/关闭钮三态不一致，无焦点圈闭/滚动锁） |
| 抽屉/dock | 自研 translateX 抽屉 + 自定义 dock + 弹簧曲线 | 动效质感好（--ease-spring），关闭态可达性问题如上 |
| Markdown | 自写 94 行子集解析器，文件头自称「marked v12 - minimal subset」 | 最具风险的自研件：与真 marked 无关（名字误导）、链接路径 XSS 实锤 |
| 图表 | 自绘 SVG（散点/利率点阵/SEP）+ CSS（热度/分时/情绪条） | 合理保留项：FOMC 点阵/SEP 是领域资产；但缺图例色块与数值刻度、边缘气泡被裁、象限词被点压（1920/390 均实测） |
| 提示 | 全站 `title` 属性 tooltip | 无键盘/触控可达性，移动端完全失效 |

## 六、AI 模板感诊断

**整体不是模板脸**：无紫色渐变、无三等分卡片阵、无 hero 大图；信息架构（左右常驻抽屉+底部 dock+账本信息流）按盯盘任务特化，有多轮真实用户规格演进痕迹。

**「多轮 AI 迭代」拼接痕迹真实存在**：①注释工作日志腔写进产物（CSS 内 v19–v25 节点注释；AI 帮助文案把 `E:\TT_Sniffer\config.yaml`、`TTSNIFFER_LLM_API_KEY`、run.bat 步骤整段暴露给界面）；②同一语义多种表达并存（四种空态文案、`10/16` 与 `2026/10/16` 同屏、衬线数字只覆盖 hero 不覆盖行情大数字）；③「AI 未启用」仍保持可输入的发送按钮；④每图卡结尾同款「规则先验聚合，仅供参考」三层免责声明形成模板化节奏。属流程性缺陷，非审美问题。

## 七、结论三问

1. **能否交付真实用户？** 单用户自用已在交付且合格；给第二个人用之前须过三关：markdown 安全、弹层键盘可达性、视觉一致性纪律。修完 ①② 可放心给家人/同事用；修完 ④ 才谈得上「产品」。
2. **达到成熟商业水准最优先改什么？** 把自研基础设施件换成成熟件——弹层/抽屉/markdown 渲染/图标，正是 shadcn/ui + Radix 生态的领地；同时建立字号与色彩角色硬约束。
3. **只投一天的三件事？** ①修 marked XSS + `title="undefined"` + 声音恢复 + `d=any`（小时级）；②弹层统一补 Esc/role/焦点还原 + 移动抽屉关闭态 inert；③最小视觉收敛：图标统一 lucide、字号下限 10px、行情数字右对齐+千分位、空态文案统一。

---

## 八、shadcn/ui 迁移方案（强制基线 https://ui.shadcn.com/）

### 8.1 目标架构

```
app/web/
  src/            新增：React 18 + TypeScript + Vite + Tailwind + shadcn/ui 组件源码
  dist/           构建产物（提交入库，run.bat 部署零心智；本机已接受 Node，改动后 npm run build 再提交）
  static/         旧版迁移期经 /classic 保留，P4 期退役
```

- `server.py:351-366` 增两条路由：`/` → `dist/index.html`、`/assets/*` 白名单静态。**`/api/*` 一行不动**（17 个端点契约已稳定，SPA 全量消费现有 API——低风险迁移的前提）。
- 开发期 `vite dev` 代理目标：**2026-10-19 P0 决议改为默认 `127.0.0.1:8788`（隔离实例）**，环境变量 `TTS_DEV_PROXY` 可覆盖（如指回 8787）。理由：防 dev 会话向生产实例写关注列表/AI 对话记录；偏离本节原「代理 8787」条文经智者门裁定为准。工具链版本见文首 Node 决议。

### 8.2 Token 桥接（视觉零回退前提）

现有 oklch token 命名本就与 shadcn 语义变量同名（`--background/--card/--border/--ring`，index.html:11-38），直接注入为 shadcn CSS variables；领域四色（--rumor/--debunk/--official/--gold）+ 中性（--dim/--faint）注册进 Tailwind theme；半径/阴影分档照搬；`--serif` 保留报头与 hero。**借机清债**：主蓝收敛为 `--primary`（消 5 处硬编码）、~~矩阵色改 --rumor 同源~~（**2026-10-19 P0 决议：推迟至 P3 统计视图迁移时一并做**——P0/P1 期不动冻结的 classic 热力矩阵）、新增字号 token 并设 10px 下限。

### 8.3 组件映射

| 现有自研 | 迁移目标 | 免费获得的修复 |
|---|---|---|
| #reader 阅读弹窗 | `Dialog` | 焦点圈闭、Esc、滚动锁、焦点还原、aria |
| .wmodal 关注编辑 | `Dialog` + `Input` + `Button` + `Table` | 补 Esc/关闭/aria；按钮收敛 variants |
| AI 抽屉 | `Sheet side="right"` + `ScrollArea` + `Sonner` | 浮层语言统一；错误走 toast |
| AI markdown | `react-markdown` + `rehype-sanitize` | **XSS 根除**，title="undefined" 消失 |
| 移动左右抽屉 | `Sheet` | 关闭态自动不可聚焦（解决 35 控件幽灵焦点） |
| 桌面常驻侧栏 | `Sidebar` 或 aside 布局 | — |
| 视图切换/dock | `Tabs` + 自定义 dock 壳（保留金色 dock-pill） | aria-selected |
| 资产页签/方向开关 | `ToggleGroup` | aria-pressed；d=any 顺手修 |
| 模板存/删（prompt） | `Select` + `DropdownMenu` + 小 `Dialog` | 消灭原生 prompt |
| 来源/小道胶囊 | `Badge` + `Checkbox` | 单一胶囊语言 |
| 徽章体系 | `Badge` cva 变体（bull/bear/official/gold/hi/mid/low） | 胶囊与圆角矩形统一 |
| 新消息浮条 | 保留自绘，并入统一浮层 z 序/定位 token（或 `Sonner`） | 移动端不再压正文 |
| title tooltip | `Tooltip`（Radix） | 键盘/触控可达 |
| 分时/来源占比/情绪条 | shadcn/ui Charts（Recharts 封装） | 轴刻度/图例/悬浮卡 |
| 散点/利率点阵/SEP | **保留自绘 SVG**，包 `Card` + 统一图例组件 | 领域资产不重写；补图例/刻度/边缘 padding/移动端点按 |

### 8.4 分期任务书（每期独立可合入，验收含智者门）

| 期 | 内容 | 工时 | 验收标准 |
|---|---|---|---|
| **S0 热修（不依赖迁移，先行合入）** | marked XSS 三处转义补丁 + title=undefined + 声音恢复 + d=any + newbar 改 --hh + 「资产」重复标题/模板行断行 | 0.5 天 | XSS PoC 用例转绿；四项回归实测；pytest 219 全绿 |
| **P0 脚手架 + Token 桥** | Vite+React+TS 起步、shadcn init、token 注入、dist 路由、/classic 挂旧版 | 0.5 天 | 新旧双轨并存可回退；token 桥对色卡截图比对零漂移 |
| **P1 壳 + 信息流** | header/热度日历/行情条（数字右对齐+千分位+固定小数位、尾行不拉伸）/账本行（Badge/ToggleGroup/Tooltip）；SSE 插顶与竞态令牌照搬 | 1–1.5 天 | Playwright 几何断言入库为 CI；390/1280/1920 无溢出 |
| **P2 弹层与 AI** | Dialog/Sheet/Sonner/react-markdown 落地；prompt/alert 清退 | 1 天 | 键盘全程走查 Tab 进出弹层不断链；marked XSS 用例在新栈保持绿 |
| **P3 统计视图** | Charts 迁移 + 自绘图表包 Card/图例/刻度/移动端点按 | 1 天 | 图例色块+数值刻度齐；移动端无 hover 依赖 |
| **P4 移动 dock/手势 + 清退** | dock 壳接 Tabs、滑切保留、static/ 退役、run.bat 冒烟 | 0.5 天 | 智者门全量断言 + 视觉两轮像素审查 |

### 8.5 风险与回滚

- 每期独立 commit，`/classic` 保留至 P4 验收后才删——任何一期翻车可秒切旧版（路由一处改回）。
- SSE/轮询在 React 里的定时器与订阅清理（useEffect 卸载路径）需专门断言（防泄漏叠加请求）。**P1 智者门裁定（2026-10-19）：此断言为 P2 验收门硬项**——P2 的 Dialog/Sheet/AI 抽屉是首批真实挂卸面，须含 StrictMode 双挂载泄漏专项断言；P1 代码清理路径经审正确（close+clearTimeout+setConn(false)），允许推迟但不得静默消失。
- 中文数字字体栈注意：Tailwind preflight 会重置，`--serif`/tabular-nums 需显式配置。
- 不引状态管理库；现有 state+URL 同步模式平移（可选 nuqs），避免为迁移引入二次抽象。
- **P2 验收门第二条硬项（P1 智者门）：marked XSS 用例集（tests/fe_marked_poc.js 的语义等价集）须在 react-markdown+rehype-sanitize 新栈保持绿**。
- **bundle 债（P2 智者门登记，2026-10-19）**：react-markdown 入包后主包 gzip 137→~230kB，未做代码分割。计划 P4 做 AI 抽屉+markdown 链 dynamic import；**触发器：P3 图表入库后主包 gzip >280kB 则分包提前至 P3**。
- **watch 保存后 hero 计数（P2 智者门记档，不阻塞）**：保存关注列表后右栏「关注命中」卡即时刷新（key 重挂），但头部 hero「关注命中」计数仅靠 30s 轮询自愈（旧版 loadOverview 即时刷新）——残余差距，随 P3/P4 酌情补即时刷新。
- **中文 IME Enter（P2 智者门记档，不阻塞）**：AI 输入框 onKeyDown 未查 `e.nativeEvent.isComposing`，中文输入法按 Enter 确认候选词会误触发发送——旧版同款写法属平移非回归，中文产品建议 P4 补一行。
- **P3 智者门 D 项债表（2026-10-19，P4 酌情清）**：① ChartsView 用 store.curDay() 非订阅式读取（day 变更靠 Feed 全量订阅传导，P4 前改 useSyncExternalStore；顺带考虑气泡行不在当前页时的单条打开降级）；② SEP 卡头硬编码「FOMC 2026-09-16 · 18 位参与者」（旧版同病平移，12 月新 SEP 一发即过期——改吃 /api/sep 数据）；③ day 变更时 /api/sep、/api/assets 无关数据也重取（依赖数组粗粒度）；④ 弱网下右栏 lazy chunk「载入中…」→内容替换跳变（P4 验收顺带看桌面一眼）。

### 8.6 明确不做

不改任何 `/api`；不重写散点/SEP/利率点阵；不把 emoji 资产图标一次全换（lucide 先覆盖功能图标，资产 emoji 保留为特色但同列对齐）；不追求暗/亮双主题（夜盘单主题深化）。

---

## 附录：取证记录（要点）

| # | 取证 | 结果 |
|---|---|---|
| 1 | marked.parse XSS 两路径 | 裸 `<img onerror>` 注入成立；href 属性注入成立；普通文本转义正常 |
| 2 | `/?d=any` 回读 | state.dir='all'（丢失）；后端 `d=bull,bear` total=136 |
| 3 | localStorage 恢复声音 | state.sound=true 但 audio=null（静默失效），图标显示 🔊 |
| 4 | reader 关闭后焦点 | 停留在已隐藏的 .rd-x（不还原） |
| 5 | header 实高 vs newbar | 106.6px vs top:132px（脱离 26px） |
| 6 | 移动关闭抽屉 | translateX(-273px) 屏外，35 个可聚焦控件，inert=false |
| 7 | WCAG 对比度 30 项 | 最低 4.89:1 全过 AA；.title 15.89:1；hlabels 8.5px 属字号问题非对比问题 |
| 8 | 模板行几何 | 行高 45px（select 26px），按钮/下拉顶边差 ~10px，1920=1280 恒定断行 |
| 9 | 390 溢出 | scrollWidth=clientWidth=390（信息流/统计均无横滚）；1920 同样无溢出 |
| 10 | console 错误 | 18 场景全零 |
| 11 | 代码量化 | oklch×92（多为行内字面量）、主蓝硬编码×5、静默 catch×15、行内 style×38、!important×7、pytest 219、前端测试入库 0 |

## 附录 2：S0 热修智者门审查记录（2026-10-19，PASS）

> **迁移收官记录（2026-10-20，智者门 P4 终审 PASS）**：S0(66e4303)→P0(d0b1d6c+f382bf9)→P1(b2769dd+265a860)→P2(c6bf1db+80d8b49)→P3(9722c6e+4426f8a)→P4(02ca301) 六期全部过门。/api 十七端点一行未动；pytest 219→223；前端测试 0→80 项 CDP 断言（P1 22+P2 24+P3 15+P4 19）+ 4 个 pytest 包装；主包 gzip 314→127kB（ChartsView/StatsPanel/AIDrawer 惰性 chunk）；marked XSS→react-markdown+rehype-sanitize、自研弹窗→Radix、prompt/alert 清零、token 桥 22 色零漂移。
> **生产切换实录（修正智者门指出的失实表述）**：P4 删除 static/ 后、重启前，生产 8787 旧进程（2026-10-16 启动、内存中跑旧路由）的 `/` 曾处于 500（FileNotFoundError）约数小时——期间 /api/* 与采集不受影响。2026-10-20 按门后动作杀旧进程（PID 26184）并以 run.bat 重启（新 PID 43660），实测 / 200 且服务新 bundle（index-oFxGHyll.js）、/classic 404、CDP 只读加载 20 行信息流+SSE 实时+console 零错。**教训入册：清退类变更必须同步验证「存量未重启进程」的运行态，「未触碰进程」不等于「现场无恙」。**
> 已知项登记：pytest 四套 CDP 包装与 node 脚本背靠背跑偶发 1 failed（CDP 会话竞态，顺序执行恒绿，失败方向为假红非假绿）；移动端 sticky header 265px 屏占比（候选优化：DayNav 移动折叠）；Sheet pb-20 滚底 80px 空白（安全让位，保留）。

- **裁定 PASS（允许 commit）**，四项裁定问题（测试 monkeypatch 加固 / marked 修复思路 / AudioContext suspended 折衷 / .tbtns DOM 改动）全部通过，智者独立复验 pytest 220 与 PoC。
- **智者抓出的旁路 A1（已当场修复）**：攻击者输入字面 `\u0000L<预计算b64>\u0000L`（b64 解出 `text\u0001javascript:alert(1)\u0001`）可伪造占位符绕过编码时的 scheme 白名单。修法：还原回调对 `p[1]` 复验 `/^(https?:|\/|#)/`，不过降级 `inlEsc(p[0])` 纯文本（marked.js 还原处），PoC 第 13 条断言覆盖。
- **既有语义瑕疵（记档不阻塞，随 P2 react-markdown 整体消亡）**：code span 内的链接语法会被链接化（`` `[x](url)` `` 产出 `<code><a>`）——链接替换先于 code 识别的顺序问题，无安全影响。
- **知晓性备注**：AudioContext suspended 期间 schedule 的 beep 在首次交互解锁后会补响（边角体验，非错误）。
