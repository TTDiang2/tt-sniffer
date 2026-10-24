/* 统计视图（P3）：五卡——风险×避险气泡/14日轨迹（自绘 SVG 平移，补图例+
 * 数值刻度）、利率观点点阵、官方 SEP 点阵、资产×日情绪矩阵（矩阵色
 * color-mix --rumor/--debunk 同源=P0 登记清债）。移动端点按：矩阵格切日
 * 切资产、气泡点开 Reader（无 hover 依赖）。 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react"
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, ComposedChart, Line,
  Pie, PieChart, XAxis, YAxis,
} from "recharts"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"
import * as store from "@/store"
import { fetchJson } from "@/lib/api"
import { CLS_LABEL, SRC_LABEL } from "@/lib/types"
import { Skeleton } from "@/components/ui/skeleton"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Toggle } from "@/components/ui/toggle"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { cn } from "@/lib/utils"

/* ---------- 图例（评审缺口：缺图例色块） ---------- */
export function Legend({ items }: { items: Array<[string, string]> }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
      {items.map(([label, color]) => (
        <span key={label} className="flex items-center gap-1">
          <i
            className={cn("inline-block h-2 w-2 rounded-[2px]", color === "transparent" && "border border-gold")}
            style={color === "transparent" ? undefined : { background: color }}
          />
          {label}
        </span>
      ))}
    </div>
  )
}

/* ---------- 载入占位 ---------- */
function ChartSkeleton() {
  return <Skeleton className="pointer-events-none h-[280px] w-full rounded-lg" />
}

function ChartCard({ title, note, legend, children, sub, className }: {
  title: string; note?: string; legend?: React.ReactNode; children: React.ReactNode
  sub?: string; className?: string
}) {
  return (
    <Card className={cn("mb-4 min-w-0 gap-0 overflow-x-auto py-0 shadow-none", className)}>
      <CardHeader className="flex-row items-baseline gap-2 px-5 pt-5 pb-3">
        <CardTitle className="text-base font-semibold">{title}</CardTitle>
        {note && <span className="ml-auto text-xs font-normal text-muted-foreground">{note}</span>}
      </CardHeader>
      <CardContent className="px-5 pt-0 pb-5">
        {children}
        {legend && <div className="mt-3">{legend}</div>}
        {sub && <p className="mt-2 text-xs leading-relaxed text-muted-foreground">{sub}</p>}
      </CardContent>
    </Card>
  )
}

/* ---------- 风险×避险 散点（SVG 平移 + 轴刻度） ---------- */
interface ScatterCluster { x: number; y: number; sides?: string; items: Array<{ id: string; cls: string; ts: string; source: string; disp?: string }> }
interface ScatterResp { day?: string; clusters: ScatterCluster[]; trail: Array<{ day: string; x: number; y: number }> }

const CLS_COLOR: Record<string, string> = {
  rumor: "var(--rumor)", debunk: "var(--debunk)",
  official: "var(--official)", noise: "var(--faint)",
}
const QUAD = { tr: "双多·宽松", br: "进攻·风险偏好升", tl: "避险", bl: "双杀·紧缩" }

function ScatterSvg({ d, mode, onOpen }: {
  d: ScatterResp; mode: "news" | "trail"; onOpen: (id: string) => void
}) {
  const W = 460, H = 340, PAD = 34
  // v9：值域 [-110,110]——±100 档位永不贴框（轴端留白）；后端坐标改连续强度
  const cx = (v: number) => PAD + (v + 110) / 220 * (W - 2 * PAD)
  const cy = (v: number) => H - PAD - (v + 110) / 220 * (H - 2 * PAD)

  const bubbles = new Map<string, { x: number; y: number; cls: string; sides: string; n: number; items: ScatterCluster["items"] }>()
  if (mode === "news") {
    ;(d.clusters || []).forEach((c) => c.items.forEach((n) => {
      const key = `${c.x},${c.y},${n.cls}`
      if (!bubbles.has(key)) bubbles.set(key, { x: c.x, y: c.y, cls: n.cls, sides: c.sides || "both", n: 0, items: [] })
      const bp = bubbles.get(key)!
      bp.n++
      bp.items.push(n)
    }))
  }
  const lanes = new Map<string, Array<{ x: number; y: number; cls: string; sides: string; n: number; items: ScatterCluster["items"] }>>()
  bubbles.forEach((bp) => {
    const k = `${bp.x},${bp.y}`
    if (!lanes.has(k)) lanes.set(k, [])
    lanes.get(k)!.push(bp)
  })
  const dots = [...lanes.values()].map((group) => {
    group.sort((a, b) => b.n - a.n)
    return group.map((bp, j) => {
      const r = Math.min(13, 4 + Math.round(Math.sqrt(bp.n) * 2))
      const dx = group.length > 1 ? (j - (group.length - 1) / 2) * 30 : 0
      const col = CLS_COLOR[bp.cls] || CLS_COLOR.noise
      // 单边观点=空心（缺失侧坐标为 0，天然在轴上）；双侧观点=实心
      const single = bp.sides !== "both"
      const list = bp.items.slice(0, 6).map((m) =>
        `${(m.ts || "").slice(11, 16)} ${SRC_LABEL[m.source] || m.source} | ${m.disp || ""}`).join("\n")
      const more = bp.n > 6 ? `\n…共 ${bp.n} 条` : ""
      return (
        <circle
          key={`${bp.x}-${bp.y}-${bp.cls}-${j}`}
          cx={cx(bp.x) + dx} cy={cy(bp.y)} r={single ? r - 0.5 : r}
          fill={single ? "none" : col} fillOpacity={single ? 0 : 0.26}
          stroke={col} strokeWidth={single ? 1.8 : 1.4} strokeOpacity={0.9}
          strokeDasharray={single ? "3 2" : undefined}
          className="cursor-pointer"
          role="button"
          aria-label={`${CLS_LABEL[bp.cls] || bp.cls}气泡 ${bp.n} 条${single ? "（单边观点）" : ""}`}
          onClick={() => onOpen(bp.items[0].id)}
        >
          <title>{`${CLS_LABEL[bp.cls] || bp.cls}气泡 · ${bp.n} 条 @(${bp.x},${bp.y}) · ${single ? "单边" : "双侧"}观点\n${list}${more}`}</title>
        </circle>
      )
    })
  })

  const trail = d.trail || []
  const nT = trail.length
  const trailDots = trail.map((t, i) => {
    const k = nT <= 1 ? 1 : i / (nT - 1)
    return (
      <circle key={t.day} cx={cx(t.x)} cy={cy(t.y)} r={(4 + 2 * k).toFixed(1)}
        fill="var(--chart-1)" fillOpacity={(0.18 + 0.82 * k).toFixed(2)}>
        <title>{`${t.day} 汇总 (${t.x},${t.y})`}</title>
      </circle>
    )
  })
  const cur = trail.find((t) => t.day === (d.day || "")) || trail[trail.length - 1]
  const curRing = cur ? (
    <circle cx={cx(cur.x)} cy={cy(cur.y)} r={7} fill="none" stroke="var(--chart-1)" strokeWidth={2}>
      <title>{`${cur.day} 当日汇总 (${cur.x},${cur.y})`}</title>
    </circle>
  ) : null

  const ticks = [-100, -50, 0, 50, 100]
  const clipId = `sc-clip-${mode}`
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="mx-auto block w-full max-w-[560px]" role="img"
      aria-label={`风险×避险情绪${mode === "news" ? "当日新闻点" : "14日轨迹"}图`}>
      <defs>
        {/* 边缘避让（P3 vision②）：气泡/轨迹裁剪到绘图区，不再压轴刻度 */}
        <clipPath id={clipId}>
          <rect x={PAD} y={PAD} width={W - 2 * PAD} height={H - 2 * PAD} />
        </clipPath>
      </defs>
      <rect x={PAD} y={PAD} width={W - 2 * PAD} height={H - 2 * PAD}
        fill="var(--background)" stroke="var(--border)" />
      <line x1={W / 2} y1={PAD} x2={W / 2} y2={H - PAD} stroke="var(--border)" />
      <line x1={PAD} y1={H / 2} x2={W - PAD} y2={H / 2} stroke="var(--border)" />
      {/* 数值刻度（评审缺口补齐） */}
      {ticks.map((v) => (
        <text key={"x" + v} x={cx(v)} y={H - PAD + 14} textAnchor="middle"
          className="fill-[var(--muted-foreground)] text-[10px] tabular-nums">{v}</text>
      ))}
      {ticks.map((v) => (
        <text key={"y" + v} x={PAD - 6} y={cy(v) + 3} textAnchor="end"
          className="fill-[var(--muted-foreground)] text-[10px] tabular-nums">{v}</text>
      ))}
      {/* 象限词移出绘图区四角（不再与角部气泡争位） */}
      <text x={W - PAD + 4} y={PAD + 4} textAnchor="end" dominantBaseline="hanging"
        className="fill-[var(--muted-foreground)] text-[10.5px]">{QUAD.tr}</text>
      <text x={W - PAD + 4} y={H - PAD + 4} textAnchor="end"
        className="fill-[var(--muted-foreground)] text-[10.5px]">{QUAD.br}</text>
      <text x={PAD - 6} y={PAD + 4} textAnchor="start" dominantBaseline="hanging"
        className="fill-[var(--muted-foreground)] text-[10.5px]">{QUAD.tl}</text>
      <text x={PAD - 6} y={H - PAD + 4} textAnchor="start"
        className="fill-[var(--muted-foreground)] text-[10.5px]">{QUAD.bl}</text>
      <text x={W / 2} y={H - 2} textAnchor="middle" className="fill-[var(--muted-foreground)] text-[10.5px]">风险资产情绪 →</text>
      <text x={8} y={H / 2} textAnchor="middle" transform={`rotate(-90 8 ${H / 2})`}
        className="fill-[var(--muted-foreground)] text-[10.5px]">避险资产情绪 →</text>
      <g clipPath={`url(#${clipId})`}>
        {mode === "news" ? dots : <>{trailDots}{curRing}</>}
      </g>
    </svg>
  )
}

/* ---------- 容器宽度测量：SVG 自适应（viewBox 宽 = 容器宽 → 缩放恒为 1） ----------
   教训（2026-10-23 紧急修复，两轮）：
   ① 只设 minWidth 会被容器拉伸（巨图）；钉死自然宽度又会挤在左侧留半边空。
      正确做法是几何随容器走——viewBox 与渲染宽一致，列宽按可用宽分配。
   ② 测量钩子必须用 callback ref：useEffect([])+useRef 在「首帧无数据早退」
      时拿不到节点（wrapper 尚未渲染），observer 永不挂上 → 宽度永远停在
      fallback，10-17~10-23 这类空数据日切到有数据日即复现「挤在左侧」。 */
function useContainerWidth<T extends HTMLElement>(fallback = 720) {
  const [w, setW] = useState(0)
  const roRef = useRef<ResizeObserver | null>(null)
  const ref = useCallback((el: T | null) => {
    roRef.current?.disconnect()
    roRef.current = null
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      const cw = entries[0]?.contentRect?.width ?? 0
      if (cw > 0) setW(Math.floor(cw))
    })
    ro.observe(el)
    roRef.current = ro
  }, [])
  useEffect(() => () => roRef.current?.disconnect(), [])
  return [ref, w || fallback] as const
}

/* ---------- 利率观点点阵（v4：响应式，viewBox 随容器 → 缩放恒 1） ---------- */
const DP_ACT: Record<string, string> = { hike: "加息", hold: "维持", cut: "降息" }
const DP_COLOR: Record<string, string> = {
  hike: "var(--rumor)", hold: "var(--low)", cut: "var(--debunk)",
}
interface DpCell {
  month: string; action: keyof typeof DP_ACT; bp?: number | null; n: number
  prob?: number | null; sources?: string[]; level?: number | null
}
function Dotplot({ d }: { d: { base?: { mid?: number; label: string; meeting?: string } | null; cells: DpCell[] } }) {
  const [wrapRef, availW] = useContainerWidth<HTMLDivElement>(720)
  const cells = d.cells || []
  const mw = (x: string) => (x === "near" ? -1 : parseInt(x, 10) * 100 + parseInt(x.slice(5), 10))
  const months = [...new Set(cells.map((c) => c.month))].sort((a, b) => mw(a) - mw(b))
  const mlbl = (m: string) => (m === "near" ? "下次会议" : m)
  const base = d.base && d.base.mid ? d.base : null
  // 纵轴：有基准=利率档位（25bp 分档，含当前档）；缺基准=动作三行降级
  const rows: Array<{ key: string; label: string; lv?: number }> = base
    ? (() => {
        const lvs = [...new Set(cells.map((c) => c.level ?? base.mid!))]
        if (!lvs.includes(base.mid!)) lvs.push(base.mid!)
        lvs.sort((a, b) => b - a)
        return lvs.map((lv) => ({
          key: "lv" + lv,
          label: `${((lv - 125) / 1000).toFixed(2)}-${((lv + 125) / 1000).toFixed(2)}%` + (lv === base.mid ? "（当前）" : ""),
          lv,
        }))
      })()
    : (["hike", "hold", "cut"] as const).map((a) => ({ key: a, label: DP_ACT[a] }))
  const cellsOf = (m: string, row: { key: string; lv?: number }) =>
    cells.filter((c) => c.month === m && (base ? (c.level ?? base.mid!) === row.lv : c.action === row.key))
  // 每月共识：概率均值最高的档位标金环（无概率数据则不标）
  const consensus = new Map<string, { row: number; prob: number }>()
  months.forEach((m) => {
    let best: { row: number; prob: number } | null = null
    rows.forEach((row, ri) => {
      cellsOf(m, row).forEach((c) => {
        if (c.prob != null && (!best || c.prob > best.prob)) best = { row: ri, prob: c.prob }
      })
    })
    if (best) consensus.set(m, best)
  })

  /* 布局：绘图区铺满容器宽（viewBox 宽 = 渲染宽 → 缩放恒 1，字号自然） */
  const L = 108, R = 16, T = 30, B = 30
  const rowH = 42
  const nCols = Math.max(1, months.length)
  const MIN_COL = 96
  const colW = Math.max(MIN_COL, Math.floor((availW - L - R) / nCols))
  const W = L + nCols * colW + R          // 恰好铺满容器（列数多时溢出→外层滚动）
  const H = T + rows.length * rowH + B
  // 点间距随列宽线性放大：宽列时点群铺开（像官方 SEP 图），不显得"空一半"
  const gap = Math.max(10, Math.min(26, Math.round(colW / 8)))
  const rowGap = Math.max(8, Math.min(12, Math.round(gap * 0.8)))
  const cyF = (i: number) => T + i * rowH + rowH / 2
  const cxM = (i: number) => L + i * colW + colW / 2
  const parts: React.ReactNode[] = []
  rows.forEach((row, ri) => {
    parts.push(<line key={"g" + row.key} x1={L} y1={cyF(ri)} x2={L + nCols * colW} y2={cyF(ri)} stroke="var(--muted)" strokeWidth={1} />)
    parts.push(<text key={"l" + row.key} className="sep-y" x={L - 6} y={cyF(ri) + 3} textAnchor="end">{row.label}</text>)
  })
  months.forEach((m, mi) => {
    parts.push(<text key={"h" + m} className="sep-x" x={cxM(mi)} y={T - 10} textAnchor="middle">{mlbl(m)}</text>)
    rows.forEach((row, ri) => {
      const cs = cellsOf(m, row)
      if (!cs.length) return
      const total = cs.reduce((s2, c) => s2 + c.n, 0)
      const k2 = total > 14 ? 7 : 5
      const maxDots = k2 * 2 - 1   // 两行内最多示 k2*2-1 点，超出标「+N」
      let j = 0
      cs.forEach((c) => {
        const srcs = (c.sources || []).map((x) => SRC_LABEL[x] || x).join("/")
        const tip = `${mlbl(c.month)} ${DP_ACT[c.action]}${c.bp ? " " + c.bp + "bp" : "（累计）"}\n${c.n} 条 · ${srcs}` + (c.prob != null ? `\n概率均值 ${Math.round(c.prob)}%` : "")
        const shown = Math.min(c.n, Math.max(0, maxDots - j))
        for (let k = 0; k < shown; k++, j++) {
          const row2 = Math.floor(j / k2)
          const col2 = j % k2
          const inRow = Math.min(k2, maxDots - row2 * k2)
          parts.push(
            <circle key={`${m}-${row.key}-${j}`} r={3.6}
              fill={DP_COLOR[c.action] || DP_COLOR.hold}
              cx={cxM(mi) + (col2 - (inRow - 1) / 2) * gap}
              cy={cyF(ri) + (row2 - 0.5) * rowGap}>
              <title>{`${tip} · 第${j + 1}点`}</title>
            </circle>)
        }
      })
      if (j < total) {
        parts.push(<text key={`more-${m}-${row.key}`} className="sep-note" x={cxM(mi)} y={cyF(ri) + 18} textAnchor="middle">+{total - j}</text>)
      }
    })
    const cons = consensus.get(m)
    if (cons) {
      parts.push(
        <circle key={`c-${m}`} r={10} cx={cxM(mi)} cy={cyF(cons.row)} fill="none"
          stroke="var(--gold)" strokeWidth={1.6}>
          <title>{`${mlbl(m)} 共识档位 · 概率均值 ${Math.round(cons.prob)}%（该月最高）`}</title>
        </circle>)
    }
  })

  /* 摘要条：只给数字与共识档位（色键交给图例，避免三处重复） */
  const total = cells.reduce((s2, c) => s2 + c.n, 0)
  const actN = (["hike", "hold", "cut"] as const)
    .map((a) => ({ a, n: cells.filter((c) => c.action === a).reduce((s2, c) => s2 + c.n, 0) }))
    .filter((x) => x.n > 0)
  const consList = months.map((m) => ({ m, cons: consensus.get(m) })).filter((x) => x.cons)
  // 共识档位去掉纵轴上的「（当前）」后缀，避免读成「共识=当前值」
  const lvlText = (i: number) => rows[i].label.replace("（当前）", "")
  if (!cells.length) return <div className="text-sm text-muted-foreground">当日无可抽取的利率观点</div>
  return (
    <div>
      <div ref={wrapRef} className="overflow-x-auto">
        {/* viewBox 宽 = 渲染宽 → 缩放恒为 1：文字/点大小始终自然，
            同时绘图区按容器宽度铺满，不会挤在左侧留半边空 */}
        <svg viewBox={`0 0 ${W} ${H}`} style={{ width: W, minWidth: W }} className="block" role="img"
          aria-label="利率观点点阵（类 FOMC dot plot）">{parts}</svg>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span>共 <b className="tabular-nums text-foreground">{total}</b> 条观点</span>
        {actN.map((x) => (
          <span key={x.a}>{DP_ACT[x.a]} <b className="tabular-nums text-foreground">{x.n}</b> 条</span>
        ))}
        {consList.map((x) => (
          <span key={x.m}>{mlbl(x.m)} 共识档位 <b className="text-foreground">{lvlText(x.cons!.row)}</b></span>
        ))}
      </div>
      <p className="mt-1.5 text-sm leading-snug text-muted-foreground">
        {base
          ? <>纵轴=观点指向的利率区间（当前 {base.label}，据 FOMC {base.meeting || "SEP"} 决策）；金环=该月概率均值最高的档位（共识）。</>
          : <>基准利率未载入（SEP 缺失）——纵轴降级为动作分类。</>}
      </p>
    </div>
  )
}

/* ---------- 官方 SEP 点阵（v2：响应式，viewBox 随容器 → 缩放恒 1） ---------- */
const SEP_YEARS: Array<[string, string]> = [["2026", "2026"], ["2027", "2027"], ["2028", "2028"], ["2029", "2029"], ["longer", "长期"]]
function Sep({ sep }: { sep: { median: Record<string, number>; dist?: Record<string, Record<string, number>>; meeting?: string; participants?: number; decision?: string; source?: string; notes?: string[] } }) {
  const [wrapRef, availW] = useContainerWidth<HTMLDivElement>(720)
  if (!sep || !sep.median) return <div className="text-sm text-muted-foreground">SEP 数据未载入</div>
  const L = 78, R = 18, T = 26, B = 44
  // 随容器铺开：viewBox 宽 = 渲染宽 → 缩放恒 1（与利率点阵同口径，不再被拉伸成巨图）
  const W = Math.max(680, availW)
  const H = 380
  const lows = [4750, 4500, 4250, 4000, 3750, 3500, 3250, 3000, 2750]
  const y0 = 2800, y1 = 4800
  const cyF = (v: number) => T + (y1 - v) / (y1 - y0) * (H - T - B)
  const colW = (W - L - R) / SEP_YEARS.length
  const cx = (i: number, k: number, n: number) => L + colW * i + colW / 2 + (k - (n - 1) / 2) * 10
  const parts: React.ReactNode[] = []
  lows.forEach((lo) => {
    const v = lo + 125
    parts.push(<line key={"g" + lo} x1={L} y1={cyF(v)} x2={W - R} y2={cyF(v)} stroke="var(--muted)" strokeWidth={1} />)
    parts.push(<text key={"t" + lo} className="sep-y" x={L - 6} y={cyF(v) + 3} textAnchor="end">
      {(lo / 1000).toFixed(2)}-{((lo + 250) / 1000).toFixed(2)}%
    </text>)
  })
  SEP_YEARS.forEach(([k, lbl], i) => {
    parts.push(<text key={"h" + k} className="sep-x" x={L + colW * i + colW / 2} y={T - 8} textAnchor="middle">{lbl}</text>)
    const dist = (sep.dist || {})[k] || {}
    const entries = Object.entries(dist).sort((a, b) => Number(a[0]) - Number(b[0]))
    const total = entries.reduce((s2, [, n]) => s2 + n, 0)
    let drawn = 0
    for (const [bp, n] of entries) {
      const v = parseInt(bp, 10)
      for (let j = 0; j < n; j++) {
        const k2 = total > 12 ? 7 : 5
        const row = Math.floor(j / k2), col = j % k2
        const nInRow = Math.min(k2, n - row * k2)
        const vTxt = (v / 1000).toFixed(3).replace(/0+$/, "").replace(/\.$/, "")
        parts.push(<circle key={`${k}-${bp}-${j}`} className="sep-dot" r={3.4}
          cx={cx(i, col, nInRow)} cy={cyF(v) + row * 9}>
          <title>{`${k === "longer" ? "长期" : k} ${vTxt}% · 第${j + 1}位`}</title>
        </circle>)
        drawn++
      }
    }
    const med = sep.median[k]
    if (med) {
      const medTxt = (med / 1000).toFixed(3).replace(/0+$/, "").replace(/\.$/, "")
      parts.push(<circle key={"m" + k} className="sep-med" r={7} cx={cx(i, 0, 1)} cy={cyF(med)}>
        <title>{`${k === "longer" ? "长期" : k} 中位数 ${medTxt}%（档位中点口径）`}</title>
      </circle>)
    }
    if (drawn > 0 && sep.participants && drawn < sep.participants) {
      parts.push(<text key={"n" + k} className="sep-note" x={cx(i, 0, 1)} y={H - B + 14} textAnchor="middle">已示 {drawn}/{sep.participants} 位</text>)
    }
    if (drawn === 0 && med) {
      parts.push(<text key={"om" + k} className="sep-note" x={cx(i, 0, 1)} y={cyF(med) - 12} textAnchor="middle">仅中位数</text>)
    }
  })
  const notes = (sep.notes || []).map((n, i) => <div key={i}>· {n}</div>)
  return (
    <div>
      <div ref={wrapRef} className="overflow-x-auto">
        <svg viewBox={`0 0 ${W} ${H}`} style={{ width: W, minWidth: W }} className="block" role="img"
          aria-label={`FOMC 官方 SEP 点阵 ${sep.meeting || ""}`}>{parts}</svg>
      </div>
      <p className="mt-1.5 text-sm leading-snug text-muted-foreground">
        {sep.decision ? `决策：${sep.decision}（` : "（"}{sep.meeting}，{sep.participants}/19 位参与者提交，主席不提交）<br />
        {notes}<br />来源：{sep.source} · 点=每位官员年末利率预测，大环=中位数路径
      </p>
    </div>
  )
}

/* ---------- 资产×日 情绪矩阵（矩阵色 --rumor/--debunk 同源清债） ---------- */
interface HistRow { a: string; name: string; icon: string; scores: Array<number | null>; ns?: Array<[number, number] | null> }
function Matrix({ hist, buckets }: { hist: { days: string[]; rows: HistRow[] }; buckets: Record<string, string> }) {
  const { days, rows } = hist
  const grid = (list: HistRow[]) => (
    <div className="mgrid" style={{ gridTemplateColumns: `var(--mlabel, 86px) repeat(${days.length}, 1fr)` }}>
      <div className="hcell" style={{ gridColumn: 1 }}>资产</div>
      {days.map((d2, i) => (
        <div key={d2} className="hcell num" style={{ gridColumn: i + 2 }}>{d2.slice(5).replace(/^0/, "")}</div>
      ))}
      {list.map((r) => (
        <div key={r.a} className="contents">
          <div className="acell" style={{ gridColumn: 1 }} title={r.name}>{r.icon} {r.name}</div>
          {(r.scores || []).map((s, i) => {
            const dd = days[i] || ""
            const n = r.ns?.[i] ?? null
            if (s == null) return <div key={i} className="empty-cell" />
            const pct = Math.min(85, Math.round(Math.abs(s) / 100 * 85))
            // ns 缺失（旧后端）不降饱和不标票，只按分数渲染
            const thin = n != null && (n[0] + n[1]) < 2   // 不足 2 票=薄证据
            return (
              <button
                key={i}
                className="hcell num"
                style={{
                  background: `color-mix(in srgb, ${s > 0 ? "var(--rumor)" : "var(--debunk)"} ${pct}%, transparent)`,
                  color: Math.abs(s) >= 55 ? "#000" : "var(--dim)",
                  opacity: thin ? 0.5 : 1,
                }}
                title={`${dd} ${r.name} ${s > 0 ? "+" : ""}${s}${n ? `（利多 ${n[0]} · 利空 ${n[1]} 票${thin ? "，薄证据" : ""}）` : ""}`}
                aria-label={`${dd} ${r.name} 情绪 ${s > 0 ? "+" : ""}${s}，点按切到该日该资产`}
                onClick={() => { store.setDay(dd); store.setPartial({ asset: r.a }, { resetPage: true }); store.setView("feed") }}
              >{s > 0 ? "+" : ""}{s}</button>
            )
          })}
        </div>
      ))}
    </div>
  )
  const grp = (key: string, title: string, note: string, first = false) => {
    const list = rows.filter((r) => (buckets[r.a] || "out") === key)
    if (!list.length) return null
    return (
      <div>
        <div className={cn("mx-h", first && "first")}>{title}<span className="rt">{note} · {list.length} 项</span></div>
        {grid(list)}
      </div>
    )
  }
  return (
    <div>
      {grp("risk", "风险资产", "股/加密/地产/工业品", true)}
      {grp("haven", "避险资产", "金/中债/美债")}
      {grp("out", "观察（不归侧）", "汇率/原油，先验不可分")}
    </div>
  )
}

/* ---------- 情报节奏（recharts） ---------- */
interface DayRow { day: string; rumor: number; debunk: number; total: number }
interface SummaryResp {
  hourly: Array<{ h: number; total: number; rumor: number; debunk: number; official: number }>
  by_source: Record<string, number>
}
interface SentRow { a: string; name: string; icon: string; bull_n: number; bear_n: number; score: number | null }

const CLS_CHART: ChartConfig = {
  rumor: { label: "传闻", color: "var(--rumor)" },
  debunk: { label: "辟谣", color: "var(--debunk)" },
  official: { label: "官方", color: "var(--official)" },
  noise: { label: "快讯", color: "var(--muted-foreground)" },
  total: { label: "入库总量", color: "var(--muted-foreground)" },
}

const SENT_CHART: ChartConfig = {
  bull_n: { label: "利多票", color: "var(--rumor)" },
  bear_n: { label: "利空票", color: "var(--debunk)" },
}

const SRC_COLORS = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)", "var(--muted-foreground)"]

function ChartEmpty({ text }: { text: string }) {
  return <div className="flex h-[210px] items-center justify-center rounded-lg border border-dashed border-border text-xs text-muted-foreground">{text}</div>
}

function TrendChart({ days }: { days: DayRow[] }) {
  if (!days.length) return <ChartEmpty text="近 14 日暂无入库" />
  const data = days.map((d) => ({ ...d, label: d.day.slice(5) }))
  return (
    <ChartContainer config={CLS_CHART} className="aspect-auto h-[210px] w-full">
      <ComposedChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: -22 }}>
        <CartesianGrid vertical={false} strokeDasharray="3 3" />
        <XAxis dataKey="label" tickLine={false} axisLine={false} tickMargin={8} minTickGap={16} />
        <YAxis yAxisId="l" tickLine={false} axisLine={false} width={32} allowDecimals={false} />
        <YAxis yAxisId="r" hide />
        <ChartTooltip content={<ChartTooltipContent />} />
        <ChartLegend content={<ChartLegendContent />} />
        <Bar yAxisId="l" dataKey="rumor" fill="var(--color-rumor)" radius={[3, 3, 0, 0]} />
        <Bar yAxisId="l" dataKey="debunk" fill="var(--color-debunk)" radius={[3, 3, 0, 0]} />
        <Line yAxisId="r" dataKey="total" type="monotone" stroke="var(--color-total)"
          strokeWidth={1.4} dot={false} strokeDasharray="4 3" />
      </ComposedChart>
    </ChartContainer>
  )
}

function HourlyChart({ hourly }: { hourly: SummaryResp["hourly"] }) {
  const data = Array.from({ length: 24 }, (_, h) => {
    const r = hourly.find((x) => x.h === h)
    const total = r?.total ?? 0
    const official = r?.official ?? 0
    return {
      h,
      rumor: r?.rumor ?? 0,
      debunk: r?.debunk ?? 0,
      official,
      noise: Math.max(0, total - (r?.rumor ?? 0) - (r?.debunk ?? 0) - official),
    }
  })
  return (
    <ChartContainer config={CLS_CHART} className="aspect-auto h-[210px] w-full">
      <AreaChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: -22 }}>
        <CartesianGrid vertical={false} strokeDasharray="3 3" />
        <XAxis dataKey="h" tickLine={false} axisLine={false} tickMargin={8}
          ticks={[0, 6, 12, 18, 23]} tickFormatter={(v) => `${v}时`} />
        <YAxis tickLine={false} axisLine={false} width={32} allowDecimals={false} />
        <ChartTooltip content={<ChartTooltipContent indicator="line" labelFormatter={(v) => `${v} 时`} />} />
        <ChartLegend content={<ChartLegendContent />} />
        <Area dataKey="rumor" type="monotone" stackId="h" stroke="var(--color-rumor)" fill="var(--color-rumor)" fillOpacity={0.18} />
        <Area dataKey="debunk" type="monotone" stackId="h" stroke="var(--color-debunk)" fill="var(--color-debunk)" fillOpacity={0.18} />
        <Area dataKey="official" type="monotone" stackId="h" stroke="var(--color-official)" fill="var(--color-official)" fillOpacity={0.18} />
        <Area dataKey="noise" type="monotone" stackId="h" stroke="var(--color-noise)" fill="var(--color-noise)" fillOpacity={0.1} />
      </AreaChart>
    </ChartContainer>
  )
}

function SentimentBars({ rows }: { rows: SentRow[] }) {
  if (!rows.length) return <ChartEmpty text="当日暂无方向票" />
  const data = rows.slice(0, 8)
  return (
    <ChartContainer config={SENT_CHART} className="aspect-auto h-[264px] w-full">
      <BarChart data={data} layout="vertical" margin={{ top: 0, right: 8, bottom: 0, left: 0 }} barGap={2}>
        <CartesianGrid horizontal={false} strokeDasharray="3 3" />
        <XAxis type="number" hide />
        <YAxis type="category" dataKey="name" width={64} tickLine={false} axisLine={false} />
        <ChartTooltip content={<ChartTooltipContent />} />
        <ChartLegend content={<ChartLegendContent />} />
        <Bar dataKey="bull_n" fill="var(--color-bull_n)" radius={[0, 3, 3, 0]} />
        <Bar dataKey="bear_n" fill="var(--color-bear_n)" radius={[0, 3, 3, 0]} />
      </BarChart>
    </ChartContainer>
  )
}

function SourceDonut({ bySource }: { bySource: Record<string, number> }) {
  const rows = Object.entries(bySource).sort((a, b) => b[1] - a[1])
  const total = rows.reduce((n, [, v]) => n + v, 0)
  if (!rows.length) return <ChartEmpty text="当日暂无来源数据" />
  const top = rows.slice(0, 5)
  const rest = rows.slice(5).reduce((n, [, v]) => n + v, 0)
  const data = [
    ...top.map(([k, v], i) => ({ label: SRC_LABEL[k] || k, value: v, fill: SRC_COLORS[i] })),
    ...(rest ? [{ label: "其他", value: rest, fill: SRC_COLORS[5] }] : []),
  ]
  return (
    <div className="relative">
      <ChartContainer config={{}} className="aspect-auto h-[210px] w-full">
        <PieChart>
          <ChartTooltip content={<ChartTooltipContent hideLabel nameKey="label" />} />
          <Pie data={data} dataKey="value" nameKey="label" innerRadius={54} outerRadius={82} paddingAngle={2} strokeWidth={0}>
            {data.map((d) => <Cell key={d.label} fill={d.fill} />)}
          </Pie>
        </PieChart>
      </ChartContainer>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <b className="text-xl leading-none tabular-nums">{total}</b>
        <span className="mt-1 text-xs text-muted-foreground">条入库</span>
      </div>
      <div className="mt-2 flex flex-wrap justify-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        {data.map((d) => (
          <span key={d.label} className="flex items-center gap-1">
            <i className="inline-block h-2 w-2 rounded-[2px]" style={{ background: d.fill }} />
            {d.label} <b className="font-medium tabular-nums text-foreground">{d.value}</b>
          </span>
        ))}
      </div>
    </div>
  )
}

/* ---------- 宏观日历 / 利率快照 (F-201；日历 v2：分页+筛选+搜索+月历视图) ---------- */
interface MacroEvent {
  event_id: string; ts_release: string; country: string; metric: string
  period?: string | null; prev?: number | null; forecast?: number | null
  actual?: number | null; unit?: string | null; source?: string | null
}
interface RateSnap {
  date: string; settle_front?: number | null; settle_next?: number | null
  implied_front?: number | null; ts_snap?: string
}

const COUNTRY_LABEL: Record<string, string> = {
  US: "美国", CN: "中国", EU: "欧元区", JP: "日本", UK: "英国",
  DE: "德国", FR: "法国", CA: "加拿大", AU: "澳大利亚", KR: "韩国",
}

function numText(v: number | null | undefined, unit?: string | null) {
  if (v == null) return "—"
  return `${v}${unit ? unit : ""}`
}

function rateText(v: number | null | undefined, suffix = "") {
  if (v == null || !Number.isFinite(v)) return "—"
  return `${v.toFixed(4)}${suffix}`
}

/* 重要宏观数据快捷词：单选（再点一次取消），与搜索框共用同一关键词状态 */
const QUICK_KW: Array<[string, string]> = [
  ["非农", "非农"], ["CPI", "CPI"], ["失业率", "失业"], ["PMI", "PMI"],
  ["PPI", "PPI"], ["GDP", "GDP"], ["社融", "社融"], ["FOMC", "FOMC"], ["SEP", "SEP"],
]
const MACRO_PAGE = 20

function FomcBadge({ metric }: { metric: string }) {
  if (!/(FOMC|SEP)/i.test(metric)) return null
  return (
    <span className="ml-2 rounded border border-gold/50 px-1.5 py-0.5 text-[10px] text-gold">
      {/SEP/i.test(metric) ? "SEP" : "FOMC"}
    </span>
  )
}

function MacroCalendar({ events, warning }: { events: MacroEvent[]; warning?: string | null }) {
  const [country, setCountry] = useState<"all" | "US" | "CN">("all")
  const [kw, setKw] = useState("")
  const [from, setFrom] = useState("")
  const [to, setTo] = useState("")
  const [page, setPage] = useState(1)
  const [mode, setMode] = useState<"list" | "cal">("list")
  const [selDay, setSelDay] = useState("")

  // 列表与日历共享同一份筛选状态（切视图不丢条件）
  const filtered = useMemo(() => events.filter((e) => {
    if (country !== "all" && e.country !== country) return false
    if (kw && !(e.metric || "").toLowerCase().includes(kw.toLowerCase())) return false
    const d = (e.ts_release || "").slice(0, 10)
    if (from && d < from) return false
    if (to && d > to) return false
    return true
  }), [events, country, kw, from, to])
  useEffect(() => { setPage(1) }, [country, kw, from, to])

  if (!events.length) {
    return (
      <div>
        {warning && <p className="mb-2 text-xs text-gold">{warning}</p>}
        <ChartEmpty text={warning ? "日历暂无数据（见上方提示）" : "宏观日历暂未载入"} />
      </div>
    )
  }
  const pages = Math.max(1, Math.ceil(filtered.length / MACRO_PAGE))
  const cur = Math.min(page, pages)
  const rowsPage = filtered.slice((cur - 1) * MACRO_PAGE, cur * MACRO_PAGE)

  const byDay = new Map<string, MacroEvent[]>()
  filtered.forEach((e) => {
    const d = (e.ts_release || "").slice(0, 10)
    if (!d) return
    if (!byDay.has(d)) byDay.set(d, [])
    byDay.get(d)!.push(e)
  })
  const monthKeys = [...new Set([...byDay.keys()].map((d) => d.slice(0, 7)))].sort().slice(0, 3)
  const selEvents = selDay ? byDay.get(selDay) || [] : []
  const hasFilter = country !== "all" || !!kw || !!from || !!to

  return (
    <div>
      {warning && <p className="mb-2 text-xs text-gold">{warning}</p>}
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <ToggleGroup type="single" value={country} variant="outline" size="sm"
          onValueChange={(v) => v && setCountry(v as "all" | "US" | "CN")} aria-label="地区筛选">
          <ToggleGroupItem value="all" className="font-normal">全部</ToggleGroupItem>
          <ToggleGroupItem value="US" className="font-normal">美国</ToggleGroupItem>
          <ToggleGroupItem value="CN" className="font-normal">中国</ToggleGroupItem>
        </ToggleGroup>
        <Input placeholder="搜事件…" spellCheck={false} value={kw}
          onChange={(e) => setKw(e.target.value)} aria-label="事件关键词搜索"
          className="h-8 w-[110px] px-2 text-xs" />
        <span className="flex items-center gap-1">
          <Input type="date" value={from} max={to || undefined} aria-label="起始日期"
            onChange={(e) => setFrom(e.target.value)}
            className="h-8 w-[124px] px-2 text-xs [color-scheme:dark]" />
          <span className="text-xs text-muted-foreground">–</span>
          <Input type="date" value={to} min={from || undefined} aria-label="结束日期"
            onChange={(e) => setTo(e.target.value)}
            className="h-8 w-[124px] px-2 text-xs [color-scheme:dark]" />
        </span>
        <ToggleGroup type="single" value={mode} variant="outline" size="sm"
          onValueChange={(v) => v && setMode(v as "list" | "cal")} aria-label="视图切换"
          className="hidden md:inline-flex">
          <ToggleGroupItem value="list" className="font-normal">列表</ToggleGroupItem>
          <ToggleGroupItem value="cal" className="font-normal">日历</ToggleGroupItem>
        </ToggleGroup>
      </div>
      <div className="mb-3 flex flex-wrap items-center gap-1.5">
        {QUICK_KW.map(([label, k]) => (
          <Toggle key={k} variant="outline" size="sm" pressed={kw === k}
            onPressedChange={(on) => setKw(on ? k : "")} className="font-normal">{label}</Toggle>
        ))}
        {hasFilter && (
          <Button variant="ghost" size="sm" className="text-muted-foreground"
            onClick={() => { setCountry("all"); setKw(""); setFrom(""); setTo("") }}>
            ✕ 清除筛选
          </Button>
        )}
      </div>

      {filtered.length === 0 ? <ChartEmpty text="没有符合筛选条件的宏观数据" /> : mode === "list" ? (
        <div>
          {/* 手机（<md）每行一卡：实际/预期/前值拉回屏内；桌面保留 6 列表格 */}
          <ul className="divide-y divide-border/60 md:hidden">
            {rowsPage.map((e) => (
              <li key={e.event_id} className="py-2.5">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-xs tabular-nums text-muted-foreground">
                    {e.ts_release.slice(5, 16)} · {COUNTRY_LABEL[e.country] || e.country}
                  </span>
                  <span className="text-sm font-medium tabular-nums">
                    <span className="mr-1 text-[11px] font-normal text-muted-foreground">实际</span>{numText(e.actual, e.unit)}
                  </span>
                </div>
                <div className="mt-0.5 text-sm">
                  <span className="break-words">{e.metric}</span>
                  <FomcBadge metric={e.metric} />
                </div>
                <div className="mt-0.5 text-xs tabular-nums text-muted-foreground">
                  预期 {numText(e.forecast, e.unit)} · 前值 {numText(e.prev, e.unit)}
                </div>
              </li>
            ))}
          </ul>
          <div className="hidden overflow-x-auto md:block">
            <table className="w-full min-w-[680px] text-sm">
              <thead className="text-xs text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="py-2 pr-3 text-left font-medium">时间</th>
                  <th className="py-2 pr-3 text-left font-medium">地区</th>
                  <th className="py-2 pr-3 text-left font-medium">事件</th>
                  <th className="py-2 pr-3 text-right font-medium">实际</th>
                  <th className="py-2 pr-3 text-right font-medium">预期</th>
                  <th className="py-2 text-right font-medium">前值</th>
                </tr>
              </thead>
              <tbody>
                {rowsPage.map((e) => (
                  <tr key={e.event_id} className="border-b border-border/60 last:border-0">
                    <td className="py-2 pr-3 whitespace-nowrap tabular-nums text-muted-foreground">{e.ts_release.slice(5, 16)}</td>
                    <td className="py-2 pr-3 whitespace-nowrap">{COUNTRY_LABEL[e.country] || e.country}</td>
                    <td className="py-2 pr-3">
                      <span className="break-words">{e.metric}</span>
                      <FomcBadge metric={e.metric} />
                    </td>
                    <td className="py-2 pr-3 text-right font-medium tabular-nums">{numText(e.actual, e.unit)}</td>
                    <td className="py-2 pr-3 text-right tabular-nums text-muted-foreground">{numText(e.forecast, e.unit)}</td>
                    <td className="py-2 text-right tabular-nums text-muted-foreground">{numText(e.prev, e.unit)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>共 <b className="tabular-nums text-foreground">{filtered.length}</b> 条
              {filtered.length !== events.length ? <>（筛自 {events.length}）</> : null} · 每页 {MACRO_PAGE} 条</span>
            <span className="flex items-center gap-1">
              <Button variant="outline" size="sm" disabled={cur <= 1} onClick={() => setPage(cur - 1)}>上一页</Button>
              <b className="px-1 tabular-nums text-foreground">{cur}</b>
              <span>/ {pages} 页</span>
              <Button variant="outline" size="sm" disabled={cur >= pages} onClick={() => setPage(cur + 1)}>下一页</Button>
            </span>
          </div>
        </div>
      ) : (
        <div>
          {monthKeys.map((ym) => (
            <CalMonth key={ym} ym={ym} byDay={byDay} selDay={selDay}
              onPick={(d) => setSelDay(d === selDay ? "" : d)} />
          ))}
          {selDay ? (
            <div className="mt-3">
              <div className="mb-1.5 text-sm font-medium">{selDay} · {selEvents.length} 条</div>
              <div className="space-y-1.5">
                {selEvents.map((e) => (
                  <div key={e.event_id} className="flex flex-wrap items-baseline gap-x-2 text-sm">
                    <span className="tabular-nums text-muted-foreground">{(e.ts_release || "").slice(11, 16)}</span>
                    <span className="text-xs text-muted-foreground">{COUNTRY_LABEL[e.country] || e.country}</span>
                    <span className="min-w-0 flex-1 break-words">
                      {e.metric}
                      <FomcBadge metric={e.metric} />
                    </span>
                    <span className="font-medium tabular-nums">{numText(e.actual, e.unit)}</span>
                    <span className="tabular-nums text-muted-foreground">预期 {numText(e.forecast, e.unit)}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">点按有数据的日期查看当日详情。</p>
          )}
        </div>
      )}
    </div>
  )
}

/* 自建简易月历（周一开头）：格内=当日条数 + 前 2 条 metric 简讯，点选看当日详情 */
function CalMonth({ ym, byDay, selDay, onPick }: {
  ym: string; byDay: Map<string, MacroEvent[]>; selDay: string; onPick: (d: string) => void
}) {
  const [y, mo] = ym.split("-").map(Number)
  const days = new Date(Date.UTC(y, mo, 0)).getUTCDate()
  const startWeekday = (new Date(Date.UTC(y, mo - 1, 1)).getUTCDay() + 6) % 7
  const cells: Array<string | null> = [
    ...Array.from({ length: startWeekday }, () => null),
    ...Array.from({ length: days }, (_, i) => `${ym}-${String(i + 1).padStart(2, "0")}`),
  ]
  return (
    <div className="mb-3">
      <div className="mb-1 text-xs font-medium text-muted-foreground">{y} 年 {mo} 月</div>
      <div className="grid grid-cols-7 gap-1 text-center">
        {["一", "二", "三", "四", "五", "六", "日"].map((w) => (
          <span key={w} className="text-[10px] text-muted-foreground">{w}</span>
        ))}
        {cells.map((d, i) => {
          if (!d) return <span key={"e" + i} />
          const es = byDay.get(d) || []
          const sel = d === selDay
          return (
            <button key={d} type="button"
              onClick={() => es.length && onPick(d)}
              className={cn(
                "min-h-[44px] rounded-md border p-1 text-left align-top transition-colors",
                es.length ? "cursor-pointer border-border hover:border-gold/60" : "cursor-default border-transparent text-muted-foreground/50",
                sel && "border-gold ring-1 ring-gold",
              )}
              title={es.length ? `${d} · ${es.length} 条：${es.slice(0, 3).map((e) => e.metric).join(" / ")}` : d}
              aria-label={`${d}${es.length ? ` ${es.length} 条数据` : " 无数据"}`}>
              <span className={cn("text-[11px] tabular-nums", es.length && "font-medium text-foreground")}>
                {Number(d.slice(8))}
                {es.length ? <i className="ml-1 not-italic text-[10px] text-gold">{es.length}</i> : null}
              </span>
              {es.slice(0, 2).map((e) => (
                <span key={e.event_id} className="block truncate text-[10px] leading-tight text-muted-foreground">{e.metric}</span>
              ))}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function RatesSnapshot({ rows }: { rows: RateSnap[] }) {
  if (!rows.length) return <ChartEmpty text="ZQ 结算快照暂未载入" />
  const latest = rows[rows.length - 1]
  return (
    <div>
      <div className="mb-3 flex items-baseline gap-2">
        <span className="text-2xl font-semibold tabular-nums">{rateText(latest.implied_front, "%")}</span>
        <span className="text-xs text-muted-foreground">近月隐含利率 · {latest.date}</span>
      </div>
      {/* 手机（<md）卡片式：隐含利率列拉回屏内；桌面保留 4 列表格 */}
      <ul className="divide-y divide-border/60 md:hidden">
        {rows.slice(-8).reverse().map((r) => (
          <li key={r.date} className="flex items-baseline justify-between gap-2 py-2">
            <span>
              <span className="block text-sm tabular-nums text-muted-foreground">{r.date}</span>
              <span className="mt-0.5 block text-xs tabular-nums text-muted-foreground">
                近月 {rateText(r.settle_front)} · 次月 {rateText(r.settle_next)}
              </span>
            </span>
            <span className="text-sm font-medium tabular-nums">{rateText(r.implied_front, "%")}</span>
          </li>
        ))}
      </ul>
      <div className="hidden overflow-x-auto md:block">
        <table className="w-full min-w-[420px] text-sm">
          <thead className="text-xs text-muted-foreground">
            <tr className="border-b border-border">
              <th className="py-2 pr-3 text-left font-medium">交易日</th>
              <th className="py-2 pr-3 text-right font-medium">近月结算</th>
              <th className="py-2 pr-3 text-right font-medium">次月结算</th>
              <th className="py-2 text-right font-medium">隐含利率</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(-8).reverse().map((r) => (
              <tr key={r.date} className="border-b border-border/60 last:border-0">
                <td className="py-2 pr-3 tabular-nums text-muted-foreground">{r.date}</td>
                <td className="py-2 pr-3 text-right tabular-nums">{rateText(r.settle_front)}</td>
                <td className="py-2 pr-3 text-right tabular-nums">{rateText(r.settle_next)}</td>
                <td className="py-2 text-right font-medium tabular-nums">{rateText(r.implied_front, "%")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/* ---------- 视图装配 ---------- */
export function ChartsView() {
  // D①（P3 智者门）：day 用订阅式读取，不再依赖 Feed 的全量订阅传导
  const day = useSyncExternalStore(store.subscribe, () => store.curDay())
  const [sc, setSc] = useState<ScatterResp | null>(null)
  const [hist, setHist] = useState<{ days: string[]; rows: HistRow[] } | null>(null)
  const [buckets, setBuckets] = useState<Record<string, string>>({})
  const [dp, setDp] = useState<Parameters<typeof Dotplot>[0]["d"] | null>(null)
  const [sep, setSep] = useState<Parameters<typeof Sep>[0]["sep"] | null>(null)
  const [trend, setTrend] = useState<DayRow[] | null>(null)
  const [sm, setSm] = useState<SummaryResp | null>(null)
  const [sent, setSent] = useState<SentRow[] | null>(null)
  const [macro, setMacro] = useState<MacroEvent[] | null>(null)
  const [macroWarn, setMacroWarn] = useState<string | null>(null)
  const [rates, setRates] = useState<RateSnap[] | null>(null)

  // 随 day 变化的取数（scatter/history/dotplot）
  useEffect(() => {
    void (async () => {
      try {
        const [s2, h2] = await Promise.all([
          fetchJson<ScatterResp>(`/api/scatter?day=${day}`),
          fetchJson<{ days: string[]; rows: HistRow[] }>(`/api/sentiment/history?days=7`),
        ])
        setSc(s2); setHist(h2)
      } catch { /* 卡各自降级 */ }
    })()
    void fetchJson<Parameters<typeof Dotplot>[0]["d"]>(`/api/dotplot?day=${day}&cb=fed`)
      .then(setDp).catch(() => {})
  }, [day])

  // 14 日趋势与 day 无关：只在挂载时取，切日不重复请求
  useEffect(() => {
    void fetchJson<{ days: DayRow[] }>("/api/days?days=14")
      .then((d) => setTrend(d.days || [])).catch(() => setTrend([]))
  }, [])

  // 当日来源与多空票随 day 变化
  useEffect(() => {
    void fetchJson<SummaryResp>(`/api/summary?day=${day}`).then(setSm).catch(() => setSm(null))
    void fetchJson<{ sentiment: SentRow[] }>(`/api/sentiment?day=${day}`)
      .then((d) => setSent(d.sentiment || [])).catch(() => setSent([]))
  }, [day])

  // D③：与 day 无关的取数（assets/sep）只在挂载时取一次
  useEffect(() => {
    void fetchJson<{ assets: Array<{ a: string; bucket?: string }> }>("/api/assets")
      .then((assets) => setBuckets(Object.fromEntries(assets.assets.map((a) => [a.a, a.bucket || "out"]))))
      .catch(() => {})
    void fetchJson<Parameters<typeof Sep>[0]["sep"]>("/api/sep").then(setSep).catch(() => {})
    void fetchJson<{ events: MacroEvent[]; fomc_schedule_warning?: string | null }>("/api/calendar?days=14&country=US,CN")
      .then((d) => { setMacro(d.events || []); setMacroWarn(d.fomc_schedule_warning || null) })
      .catch(() => { setMacro([]); setMacroWarn(null) })
    void fetchJson<{ rows: RateSnap[] }>("/api/rates?limit=10")
      .then((d) => setRates(d.rows || [])).catch(() => setRates([]))
  }, [])

  const openItem = (id: string) => {
    // 旧版行为：跳回信息流定位该行（P3 减负：去掉 100 条预取，避免大查询挤占并发池）
    store.setView("feed")
    requestAnimationFrame(() => {
      const row = document.querySelector(`.feedrow[data-id="${id}"]`)
      if (row) {
        row.scrollIntoView({ block: "center" })
        row.classList.add("row-flash")
        window.setTimeout(() => row.classList.remove("row-flash"), 1700)
      }
    })
  }

  const clsLegend = (
    <Legend items={[["传闻", "var(--rumor)"], ["辟谣", "var(--debunk)"], ["官方", "var(--official)"], ["快讯", "var(--muted-foreground)"]]} />
  )

  const trendTotal = (trend || []).reduce((n, d) => n + d.total, 0)
  const trendRumor = (trend || []).reduce((n, d) => n + d.rumor, 0)
  const peak = (sm?.hourly || []).reduce(
    (best, x) => (x.total > best.total ? x : best), { h: 0, total: 0 })
  const bull = (sent || []).reduce((n, r) => n + r.bull_n, 0)
  const bear = (sent || []).reduce((n, r) => n + r.bear_n, 0)
  const srcCount = Object.keys(sm?.by_source || {}).length

  return (
    <div className="mt-4">
      <div className="grid gap-4 lg:grid-cols-2">
        <ChartCard title="传闻 / 辟谣 · 近 14 日" note={trend ? `${trendTotal} 条` : "载入中"}
          sub={trendTotal ? `红/绿柱=传闻/辟谣；虚线=当日入库总量（含快讯）。近 14 日传闻占 ${Math.round((trendRumor / trendTotal) * 100)}%。` : undefined}
          className="mb-0">
          {trend ? <TrendChart days={trend} /> : <ChartSkeleton />}
        </ChartCard>
        <ChartCard title="24 小时情报节奏" note={sm ? `峰值 ${peak.h} 时 · ${peak.total} 条` : "载入中"}
          sub="按入库时间堆叠：传闻 / 辟谣 / 官方 / 快讯，观察当日信息释放节奏。"
          className="mb-0">
          {sm ? (
            sm.hourly.length ? <HourlyChart hourly={sm.hourly} /> : <ChartEmpty text="当日暂无入库" />
          ) : <ChartSkeleton />}
        </ChartCard>
        <ChartCard title="资产多空票数 · 当日" note="按分歧排序"
          sub={`利多 ${bull} 票 · 利空 ${bear} 票。票数来自规则先验，仅作速览。`}
          className="mb-0">
          {sent ? <SentimentBars rows={sent} /> : <ChartSkeleton />}
        </ChartCard>
        <ChartCard title="来源分布 · 当日" note={sm ? `${srcCount} 个来源` : "载入中"}
          sub="前 5 个来源 + 其他。用于判断当日情报的渠道集中度。"
          className="mb-0">
          {sm ? <SourceDonut bySource={sm.by_source} /> : <ChartSkeleton />}
        </ChartCard>
      </div>

      <ChartCard title="风险 × 避险 · 当日新闻点" note={`${day.slice(5)} 气泡图`} legend={clsLegend}
        className="mt-4"
        sub="实心泡=双侧观点，空心泡=单边观点（落在轴上）；泡大小=条数，离中心越远证据越足。点按气泡跳源条目。规则先验聚合，仅供参考。">
        {sc ? <ScatterSvg d={sc} mode="news" onOpen={openItem} /> : <ChartSkeleton />}
      </ChartCard>
      <ChartCard title="风险 × 避险 · 14日轨迹" note="近 14 日逐日汇总"
        legend={<Legend items={[["每日汇总（浅=14日前 → 深=今日）", "var(--chart-1)"], ["当日（大环标记）", "transparent"]]} />}
        sub="蓝点=每日汇总，大环=当日。规则先验聚合，仅供参考。">
        {sc ? <ScatterSvg d={sc} mode="trail" onOpen={openItem} /> : <ChartSkeleton />}
      </ChartCard>
      <ChartCard title="利率观点点阵" note="类 FOMC dot plot · 美联储"
        legend={<Legend items={[["加息", "var(--rumor)"], ["维持", "var(--low)"], ["降息", "var(--debunk)"], ["共识（金环）", "var(--gold)"]]} />}
        sub="一个点=当日快讯中的利率观点（CME 概率/机构预测/交易员押注/官员表态）；点色表动作，幅度未明示按 25bp 计。正则抽取，宁缺勿滥。">
        {dp ? <Dotplot d={dp} /> : <ChartSkeleton />}
      </ChartCard>
      <ChartCard title="宏观日历 · 未来 14 日" note={macro ? `${macro.length} 条` : "载入中"}
        sub="覆盖美国/中国的非农、CPI、PPI、PMI、GDP、社融与 FOMC 会议；支持地区/日期/关键词筛选与日历视图；实际值公布后回填，预期在首次公布后冻结。">
        {macro ? <MacroCalendar events={macro} warning={macroWarn} /> : <ChartSkeleton />}
      </ChartCard>
      <ChartCard title="联邦基金利率快照" note={rates?.length ? `${rates.length} 个交易日` : "载入中"}
        sub="CME ZQ 近月/次月结算，隐含利率=100-结算价。每个交易日 05:10 抓取上一交易日结算。">
        {rates ? <RatesSnapshot rows={rates} /> : <ChartSkeleton />}
      </ChartCard>
      <ChartCard title="官方 SEP 点阵"
        note={sep?.meeting ? `FOMC ${sep.meeting} · ${sep.participants ?? "-"} 位参与者` : "FOMC SEP"}
        legend={<Legend items={[["官员预测（点）", "var(--gold)"], ["中位数路径（大环）", "transparent"]]} />}>
        {sep ? <Sep sep={sep} /> : <ChartSkeleton />}
      </ChartCard>
      <ChartCard title="资产 × 日 情绪矩阵" note="近 7 日 · 点格子=切日+切资产"
        legend={<Legend items={[["利多（红）", "var(--rumor)"], ["利空（绿）", "var(--debunk)"], ["深浅=强度", "transparent"]]} />}
        sub="风险/避险分图（横线分隔）；颜色与信息流色轨同源（--rumor/--debunk）。点格子跳转该日该资产筛选。">
        {hist ? <Matrix hist={hist} buckets={buckets} /> : <ChartSkeleton />}
      </ChartCard>
    </div>
  )
}
