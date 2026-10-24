/* 壳组件：Header（品牌 + 连接态 + 来源状态 + topbar 视图切换 + hero）/
   FilterBar（检索筛选）/ AssetTabs（ToggleGroup）/ DayNav（14 日热度）。
   shadcn 基线：默认字号/间距/圆角，语义色只做信息提示。 */
import { useEffect, useReducer, useState, useSyncExternalStore } from "react"
import * as store from "@/store"
import { SRC_LABEL } from "@/lib/types"
import { fetchJson } from "@/lib/api"
import { usePolling } from "@/hooks/usePolling"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Progress } from "@/components/ui/progress"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Toggle } from "@/components/ui/toggle"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { TemplateControls } from "@/components/template-controls"
import { dayOffset, todayStr } from "@/lib/format"
import type { AssetDef, DayCount } from "@/lib/types"
import {
  ChevronLeft, ChevronRight, MessagesSquare, PanelLeftClose, PanelLeftOpen,
  PanelRightClose, PanelRightOpen, Radar, RotateCcw, Volume2, VolumeX,
} from "lucide-react"
import { cn } from "@/lib/utils"

function useStore<T>(selector: (s: store.FeedState) => T): T {
  return useSyncExternalStore(store.subscribe, () => selector(store.getState()))
}

const SOURCE_ORDER = [
  "cls", "em", "sina", "ths", "wscn",
  "bbg_markets", "bbg_econ", "bbg_politics", "bbg_tech", "bbg_google",
  "wsj_markets", "wsj_world", "wsj_business", "wsj_tech", "marketwatch",
  "reuters", "ap", "ft", "cnbc", "google_news", "polymarket",
  "guba", "jiuyan", "tg",
]
const SORTED_SOURCES = new Map(SOURCE_ORDER.map((k, i) => [k, i]))
function sortSources(keys: string[]): string[] {
  return [...keys].sort((a, b) => {
    const ai = SORTED_SOURCES.get(a) ?? 999
    const bi = SORTED_SOURCES.get(b) ?? 999
    return ai - bi || a.localeCompare(b)
  })
}

function LoadingBar({ active }: { active: boolean }) {
  if (!active) return null
  return (
    <div className="absolute inset-x-0 bottom-0">
      <Progress value={null} aria-label="正在加载" className="h-0.5 rounded-none bg-primary/15" />
    </div>
  )
}

/* ============ Header ============ */
interface HeroCounts { rumor: number; total: number; watch: number }

export function Header({ onAI, aiRef, leftCollapsed, rightCollapsed, onToggleLeft, onToggleRight }: {
  onAI: () => void
  aiRef: React.RefObject<HTMLButtonElement | null>
  leftCollapsed: boolean
  rightCollapsed: boolean
  onToggleLeft: () => void
  onToggleRight: () => void
}) {
  const connOK = useStore((s) => s.connOK)
  const sound = useStore((s) => s.sound)
  const view = useStore((s) => s.view)
  const loading = useStore((s) => s.loading)
  const [viewBusy, setViewBusy] = useState(false)
  const [hero] = useHero()
  const chips = useChips()

  const switchView = (v: string) => {
    if (v === view) return
    setViewBusy(true)
    store.setView(v)
    window.setTimeout(() => setViewBusy(false), 500)
  }

  return (
    <header className="sticky top-0 z-40 border-b border-border bg-background/85 backdrop-blur-md">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2.5 md:px-6">
        <div className="flex items-center gap-2.5">
          <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Radar className="size-5" />
          </div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold tracking-tight">TT_Sniffer 情报台</div>
            <div className="text-xs text-muted-foreground">7×24 传闻雷达</div>
          </div>
        </div>

        <Badge variant="outline" className="gap-1.5 bg-transparent font-normal text-muted-foreground" aria-live="polite">
          <span className={cn("size-1.5 rounded-full", connOK ? "bg-debunk" : "bg-rumor")} />
          {connOK ? "实时" : "重连中"}
        </Badge>

        <Tabs value={view} onValueChange={switchView} className="shrink-0 max-[760px]:hidden">
          <TabsList aria-label="视图切换">
            <TabsTrigger value="feed">信息流</TabsTrigger>
            <TabsTrigger value="chart">统计</TabsTrigger>
          </TabsList>
        </Tabs>

        <div className="hidden min-w-0 flex-1 items-center gap-1.5 overflow-x-auto whitespace-nowrap lg:flex">
          {chips}
        </div>

        <div className="ml-auto flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 max-[760px]:ml-0 max-[760px]:w-full max-[760px]:justify-between">
          <div className="flex items-center gap-5">
            <HeroCell value={hero.rumor} label="今日传闻" tone="rumor" />
            <HeroCell value={hero.total} label="今日入库" />
            <HeroCell value={hero.watch} label="关注命中" className="max-[480px]:hidden sm:block" />
          </div>
          <div className="flex items-center gap-1">
            <div className="hidden items-center gap-0.5 min-[761px]:flex">
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={onToggleLeft}
                aria-label={leftCollapsed ? "展开左侧栏" : "收起左侧栏"}
                title={leftCollapsed ? "展开左侧栏" : "收起左侧栏"}
                className="text-muted-foreground hover:text-foreground"
              >
                {leftCollapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={onToggleRight}
                aria-label={rightCollapsed ? "展开右侧栏" : "收起右侧栏"}
                title={rightCollapsed ? "展开右侧栏" : "收起右侧栏"}
                className="text-muted-foreground hover:text-foreground"
              >
                {rightCollapsed ? <PanelRightOpen /> : <PanelRightClose />}
              </Button>
            </div>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => store.toggleSound()}
                  aria-label="高分提醒声音"
                  className="text-muted-foreground hover:text-foreground"
                >
                  {sound ? <Volume2 /> : <VolumeX />}
                </Button>
              </TooltipTrigger>
              <TooltipContent>高分提醒声音（跨会话记忆）</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  ref={aiRef}
                  variant="outline"
                  size="sm"
                  onClick={onAI}
                  aria-label="AI 分析对话"
                  className="gap-1.5"
                >
                  <MessagesSquare />
                  <span className="max-[480px]:hidden">AI</span>
                </Button>
              </TooltipTrigger>
              <TooltipContent>AI 分析对话</TooltipContent>
            </Tooltip>
          </div>
        </div>
      </div>
      <DayNav />
      <LoadingBar active={loading || viewBusy} />
    </header>
  )
}

function HeroCell({ value, label, tone, className }: {
  value: number; label: string; tone?: "rumor"; className?: string
}) {
  return (
    <div className={cn("min-w-[62px]", className)}>
      <b className={cn("block text-xl leading-none font-semibold tabular-nums", tone === "rumor" ? "text-rumor" : "text-foreground")}>
        {value}
      </b>
      <span className="mt-1 block text-xs text-muted-foreground">{label}</span>
    </div>
  )
}

function useHero(): [HeroCounts, (h: HeroCounts) => void] {
  const [hero, setHeroState] = useLocalState<HeroCounts>({ rumor: 0, total: 0, watch: 0 })
  usePolling(() => {
    void (async () => {
      try {
        const day = todayStr()
        const [ov, w] = await Promise.all([
          fetchJson<{ counts: Record<string, number> }>(`/api/day?day=${day}`),
          fetchJson<{ total: number }>(`/api/items?cls=watch&limit=1&day=${day}`),
        ])
        const c = ov.counts || {}
        setHeroState({
          rumor: c.rumor || 0,
          total: (c.rumor || 0) + (c.debunk || 0) + (c.official || 0) + (c.noise || 0),
          watch: w.total || 0,
        })
      } catch { /* hero 是辅助信息，静默降级（轮询自愈） */ }
    })()
  }, 30000)
  return [hero, setHeroState]
}

function useLocalState<T>(init: T): [T, (v: T) => void] {
  const [v, dispatch] = useReducer((_: T, n: T) => n, init)
  return [v, dispatch as (v: T) => void]
}

/* 来源可用性：中性描边 + 状态点，不用蓝底 */
function useChips() {
  const [chips, setChips] = useLocalState<Array<{ k: string; ok: boolean; note: string }>>([])
  usePolling(() => {
    void (async () => {
      try {
        const st = await fetchJson<{ collectors: Record<string, { ok: boolean; note: string }> }>("/api/stats")
        setChips(sortSources(Object.keys(st.collectors || {})).map((k) => {
          const v = st.collectors[k]
          return { k, ok: v.ok, note: v.note || "" }
        }))
      } catch { /* chips 缺席不影响主链路 */ }
    })()
  }, 30000)
  return chips.map((c) => (
    <Badge
      key={c.k}
      variant="outline"
      className="shrink-0 gap-1.5 bg-transparent font-normal text-muted-foreground"
      title={c.note || (c.ok ? "来源正常" : "来源异常")}
    >
      <span className={cn("size-1.5 rounded-full", c.ok ? "bg-debunk" : "bg-rumor")} />
      {SRC_LABEL[c.k] || c.k}
      {!c.ok && <span className="text-rumor">异常</span>}
    </Badge>
  ))
}

/* ============ FilterBar ============ */
export function FilterBar() {
  const s = useStore((st) => st)
  const [q, setQ] = useLocalState(s.q)

  return (
    <div className="flex flex-col gap-3">
      <Input
        id="q"
        placeholder="搜索标题 / 正文，逗号分隔多关键词…"
        spellCheck={false}
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onBlur={() => q.trim() !== s.q && store.setPartial({ q: q.trim() }, { resetPage: true })}
        onKeyDown={(e) => {
          if (e.key === "Enter") store.setPartial({ q: q.trim() }, { resetPage: true })
        }}
      />

      <div className="grid grid-cols-2 gap-2">
        <label className="flex flex-col gap-1.5">
          <Label className="text-xs text-muted-foreground">分类</Label>
          <Select value={s.cls} onValueChange={(v) => store.setPartial({ cls: v }, { resetPage: true })}>
            <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              <SelectItem value="rumor">传闻</SelectItem>
              <SelectItem value="debunk">辟谣</SelectItem>
              <SelectItem value="official">官方</SelectItem>
              <SelectItem value="watch">关注</SelectItem>
            </SelectContent>
          </Select>
        </label>
        <label className="flex flex-col gap-1.5">
          <Label className="text-xs text-muted-foreground">分数</Label>
          <Select value={s.band} onValueChange={(v) => store.setPartial({ band: v }, { resetPage: true })}>
            <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              <SelectItem value="hi">≥80 告警</SelectItem>
              <SelectItem value="mid">55-79 观察</SelectItem>
              <SelectItem value="low">1-54 弱信号</SelectItem>
            </SelectContent>
          </Select>
        </label>
        <label className="flex flex-col gap-1.5">
          <Label className="text-xs text-muted-foreground">方向</Label>
          <Select value={["all", "any", "bull", "bear"].includes(s.dir) ? s.dir : "all"}
            onValueChange={(v) => store.setPartial({ dir: v }, { resetPage: true })}>
            <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              <SelectItem value="any">有方向</SelectItem>
              <SelectItem value="bull">利多</SelectItem>
              <SelectItem value="bear">利空</SelectItem>
            </SelectContent>
          </Select>
        </label>
        <label className="flex flex-col gap-1.5">
          <Label className="text-xs text-muted-foreground">排序</Label>
          <Select value={s.sort} onValueChange={(v) => store.setPartial({ sort: v }, { resetPage: true })}>
            <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="time">最新</SelectItem>
              <SelectItem value="score">最高分</SelectItem>
            </SelectContent>
          </Select>
        </label>
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        <Toggle variant="outline" size="sm" pressed={s.tierOnly}
          onPressedChange={(v) => store.setPartial({ tierOnly: v }, { resetPage: true })}
          className="gap-1.5 font-normal">
          ⚠️ 小道
        </Toggle>
        <Toggle variant="outline" size="sm" pressed={s.fold}
          onPressedChange={() => store.setFold(!s.fold)}
          className="font-normal">
          折叠同类
        </Toggle>
      </div>

      <TemplateControls />

      <div className="flex items-center gap-2">
        <Button
          variant="ghost"
          size="sm"
          className={cn("text-muted-foreground", (s.q || s.srcs.size || s.band !== "all" || s.cls !== "all" || s.sort !== "time" || s.asset !== "all" || s.tierOnly) ? "inline-flex" : "hidden")}
          onClick={() => store.clearFilters()}
        >
          ✕ 清除筛选
        </Button>
        <span className="ml-auto text-xs whitespace-nowrap text-muted-foreground">
          <b className="tabular-nums text-foreground">{Math.min(s.page, Math.max(1, Math.ceil(s.total / store.PAGE)))}</b>
          {" / "}
          <b className="tabular-nums text-foreground">{Math.max(1, Math.ceil(s.total / store.PAGE))}</b> 页 ·
          共 <b className="tabular-nums text-foreground">{s.fold && s.totalRaw ? `${s.total} 组 · ${s.totalRaw} 条` : s.total}</b> 条
        </span>
      </div>
    </div>
  )
}

const NON_FEED_COLLECTORS = new Set(["anomaly", "ztpool", "rss", "crypto_rss"])

/* ============ 来源（分组展示；组头点击=整组全选/清除） ============ */
const SRC_GROUPS: Array<[string, string[]]> = [
  ["国内快讯", ["cls", "em", "sina", "ths", "wscn"]],
  ["国际媒体", ["bbg_markets", "bbg_econ", "bbg_politics", "bbg_tech", "bbg_google",
    "wsj_markets", "wsj_world", "wsj_business", "wsj_tech", "marketwatch",
    "reuters", "ap", "ft", "cnbc", "google_news"]],
  ["预测市场", ["polymarket"]],
  ["社群", ["guba", "jiuyan", "tg"]],
]

function useAvailSources(): string[] {
  const [avail, setAvail] = useLocalState<string[]>([])
  usePolling(() => {
    void (async () => {
      try {
        const st = await fetchJson<{ collectors: Record<string, { ok: boolean }> }>("/api/stats")
        setAvail(sortSources(Object.keys(st.collectors || {}).filter((k) => !NON_FEED_COLLECTORS.has(k))))
      } catch { /* 来源列表缺失时保持空态，不影响主链路 */ }
    })()
  }, 30000)
  return avail
}

export function SourcePanel() {
  const srcs = useStore((s) => s.srcs)
  const avail = useAvailSources()

  const grouped = SRC_GROUPS
    .map(([label, ks]) => [label, ks.filter((k) => avail.includes(k))] as [string, string[]])
    .filter(([, ks]) => ks.length)
  const known = new Set(SRC_GROUPS.flatMap(([, ks]) => ks))
  const rest = avail.filter((k) => !known.has(k))
  if (rest.length) grouped.push(["其他", rest])

  const toggleGroup = (ks: string[]) => {
    const next = new Set(store.getState().srcs)
    const clear = ks.every((k) => next.has(k))
    ks.forEach((k) => (clear ? next.delete(k) : next.add(k)))
    store.setSources(next)
  }

  if (!grouped.length) return null
  return (
    <div className="flex flex-col gap-2.5">
      {srcs.size > 0 && (
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>已选 <b className="tabular-nums text-foreground">{srcs.size}</b> 个来源</span>
          <Button variant="ghost" size="sm" className="h-7 px-2 text-muted-foreground"
            onClick={() => store.setSources(new Set())}>✕ 清除</Button>
        </div>
      )}
      {grouped.map(([label, ks]) => {
        const picked = ks.filter((k) => srcs.has(k)).length
        return (
          <div key={label}>
            <button
              type="button"
              className={cn(
                "mb-1 flex w-full items-center justify-between text-[11px] font-medium tracking-wide text-muted-foreground hover:text-foreground",
                picked > 0 && "text-foreground",
              )}
              onClick={() => toggleGroup(ks)}
              title={picked > 0 ? "整组已选，点击整组清除" : "点击整组全选"}
            >
              {label}
              {picked > 0 && <span className="tabular-nums">{picked}/{ks.length}</span>}
            </button>
            <div className="flex flex-wrap gap-1.5">
              {ks.map((k) => (
                <Toggle key={k} variant="outline" size="sm" pressed={srcs.has(k)}
                  onPressedChange={() => store.toggleSrc(k)} className="font-normal">
                  {SRC_LABEL[k] || k}
                </Toggle>
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}

/* ============ AssetTabs（按风险/避险/观察分组，单一选态） ============ */
const BUCKET_LABEL: Record<string, string> = { risk: "风险", haven: "避险", out: "观察" }

export function AssetTabs() {
  const s = useStore((st) => st)
  const [assets, setAssets] = useLocalState<AssetDef[]>([])
  useEffect(() => {
    void (async () => {
      try {
        const d = await fetchJson<{ assets: AssetDef[] }>("/api/assets")
        setAssets(d.assets || [])
      } catch { /* 资产页签缺席不影响主链路 */ }
    })()
  }, [setAssets])

  const pickAsset = (a: string) =>
    a !== s.asset && store.setPartial({ asset: a }, { resetPage: true })

  return (
    <div className="flex flex-col gap-2.5" role="group" aria-label="资产筛选">
      <div className="flex flex-wrap gap-1.5">
        <Toggle variant="outline" size="sm" pressed={s.asset === "all"}
          onPressedChange={(on) => on && pickAsset("all")} className="font-normal">
          全部资产
        </Toggle>
      </div>
      {(["risk", "haven", "out"] as const).map((b) => {
        const list = assets.filter((a) => (a.bucket || "out") === b)
        if (!list.length) return null
        return (
          <div key={b}>
            <div className="mb-1 text-[11px] font-medium tracking-wide text-muted-foreground">
              {BUCKET_LABEL[b]}<span className="ml-1 font-normal">{list.length}</span>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {list.map((a) => (
                <Toggle key={a.a} variant="outline" size="sm" pressed={s.asset === a.a}
                  onPressedChange={(on) => on && pickAsset(a.a)} title={a.name} className="font-normal">
                  {a.icon} {a.name}
                </Toggle>
              ))}
            </div>
          </div>
        )
      })}
      {s.asset !== "all" && (
        <ToggleGroup
          type="single"
          value={["bull", "bear", "rel"].includes(s.dir) ? s.dir : ""}
          onValueChange={(v) => store.setPartial({ dir: v || "all" }, { resetPage: true })}
          variant="outline"
          size="sm"
          spacing={2}
          className="flex gap-1.5"
          aria-label="方向筛选"
        >
          {([["bull", "多"], ["bear", "空"], ["rel", "相关"]] as const).map(([d, label]) => (
            <ToggleGroupItem
              key={d}
              value={d}
              className={cn("font-normal",
                d === "bull" && "data-[state=on]:text-rumor",
                d === "bear" && "data-[state=on]:text-debunk",
                d === "rel" && "data-[state=on]:text-muted-foreground",
              )}
            >{label}</ToggleGroupItem>
          ))}
        </ToggleGroup>
      )}
      <span className="text-xs text-muted-foreground" title="方向为规则先验映射，仅供参考；不构成投资建议">
        先验 · 仅供速筛
      </span>
    </div>
  )
}

/* ============ DayNav ============ */
export function DayNav() {
  const s = useStore((st) => st)
  const [days, setDays] = useLocalState<DayCount[]>([])
  usePolling(() => {
    void (async () => {
      try {
        const d = await fetchJson<{ days: DayCount[] }>("/api/days?days=14")
        setDays(d.days || [])
      } catch { /* 热度条缺席不影响主链路 */ }
    })()
  }, 120000)

  const cur = store.curDay()
  const today = todayStr()
  const max = Math.max(1, ...days.map((x) => x.rumor + x.debunk))

  return (
    <div className="flex flex-wrap items-center gap-3 border-t border-border/60 px-4 py-1.5 md:px-6">
      <div className="flex min-w-0 max-w-full flex-wrap items-center gap-1">
        <Button variant="outline" size="icon-sm" title="前一天" onClick={() => store.setDay(dayOffset(cur, -1))}>
          <ChevronLeft />
        </Button>
        <span className="min-w-[76px] text-center text-sm font-medium tabular-nums">
          {s.day === "today" || s.day === today ? "今天" : s.day.slice(5).replace("-", "/")}
        </span>
        <Button variant="outline" size="icon-sm" title="后一天" disabled={cur >= today}
          onClick={() => cur < today && store.setDay(dayOffset(cur, 1))}>
          <ChevronRight />
        </Button>
        <Input type="date" max={today} value={cur} aria-label="选择日期"
          className="ml-1 h-8 w-[128px] px-2 text-xs [color-scheme:dark]"
          onChange={(e) => e.target.value && store.setDay(e.target.value)} />
        {(s.day !== "today" && s.day !== today) && (
          <Button variant="outline" size="sm" className="ml-1 gap-1" onClick={() => store.setDay("today")}>
            <RotateCcw />
            回到今天
          </Button>
        )}
      </div>
      <div className="mx-auto flex min-w-[280px] max-w-[620px] flex-1 cursor-pointer items-end gap-1" aria-label="近14日传闻热度">
        {days.map((x) => {
          const hot = x.rumor + x.debunk
          const h = hot ? Math.max(10, Math.round((hot / max) * 28)) : 3
          const rw = hot ? Math.round((x.rumor / hot) * 100) : 0
          const sel = cur === x.day
          return (
            <div
              key={x.day}
              className={cn("flex max-w-[24px] flex-1 flex-col items-center gap-1", sel && "[&>.hb]:ring-1 [&>.hb]:ring-gold")}
              title={`${x.day.slice(5)} · 传闻${x.rumor} 辟谣${x.debunk} / 共${x.total}`}
              onClick={() => store.setDay(x.day)}
            >
              <div className="hb relative w-full max-w-[12px] overflow-hidden rounded-full bg-muted" style={{ height: h + "px" }}>
                <i className="absolute inset-x-0 bottom-0 bg-rumor/80" style={{ height: rw + "%" }} />
                <i className="absolute inset-x-0 top-0 bg-debunk" style={{ height: 100 - rw + "%" }} />
              </div>
              <span className={cn("text-[11px] leading-none tabular-nums text-muted-foreground", x.day === today && "font-semibold text-foreground")}>
                {x.day.slice(8)}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
