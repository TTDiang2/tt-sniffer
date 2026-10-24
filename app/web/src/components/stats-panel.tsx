/* 右栏统计面板：资产情绪（双向条）+ 分时/来源占比（shadcn Charts）+
   分类/热词/异动/累计。统一 Card 壳，移动端无 hover 依赖。 */
import { useSyncExternalStore } from "react"
import { fetchJson } from "@/lib/api"
import { todayStr } from "@/lib/format"
import { CLS_LABEL, SRC_LABEL } from "@/lib/types"
import * as store from "@/store"
import { usePolling } from "@/hooks/usePolling"
import { useReducer } from "react"
import { ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart"
import { Bar, BarChart, XAxis, YAxis } from "recharts"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table"
import { cn } from "@/lib/utils"

function useLocal<T>(init: T) {
  const [v, dispatch] = useReducer((_: T, n: T) => n, init)
  return [v, dispatch as (v: T) => void] as const
}

interface SentRow {
  a: string; name: string; icon: string; score: number | null
  bull_n: number; bear_n: number
}
interface Summary {
  hourly: Array<{ h: number; total: number; rumor: number; debunk: number }>
  by_source: Record<string, number>
  by_cls: Record<string, number>
}

export function StatsPanel() {
  const day = useSyncExternalStore(store.subscribe, () => store.getState().day)
  const [senti, setSenti] = useLocal<SentRow[] | null>(null)
  const [sm, setSm] = useLocal<Summary | null>(null)
  const [kw, setKw] = useLocal<Array<[string, number]> | null>(null)
  const [anomaly, setAnomaly] = useLocal<Array<{ name: string; code: string; count: number }> | null>(null)
  const [stats, setStats] = useLocal<{ total: number; by_cls: Record<string, number>; last_ts: string } | null>(null)

  usePolling(() => {
    const cur = store.curDay()
    void fetchJson<{ sentiment: SentRow[] }>(`/api/sentiment?day=${cur}`).then((d) => setSenti(d.sentiment || [])).catch(() => {})
    void fetchJson<Summary>(`/api/summary?day=${cur}`).then(setSm).catch(() => {})
    void fetchJson<{ keywords: Array<[string, number]> }>(`/api/day?day=${cur}`)
      .then((d) => setKw(d.keywords || [])).catch(() => {})
    void fetchJson<{ anomaly: Array<{ name: string; code: string; count: number }> }>("/api/anomaly")
      .then((d) => setAnomaly(d.anomaly || [])).catch(() => {})
    void fetchJson<{ stats: { total: number; by_cls: Record<string, number>; last_ts: string } }>("/api/stats")
      .then((d) => setStats(d.stats)).catch(() => {})
  }, 30000, [day])

  const hours = Array.from({ length: 24 }, (_, h) => {
    const b = sm?.hourly?.find((x) => x.h === h)
    return { h, total: b?.total || 0, rumor: b?.rumor || 0 }
  })
  const src = Object.entries(sm?.by_source || {})
    .map(([k, v]) => { return { k, label: SRC_LABEL[k] || k, v } })
    .sort((a, b) => b.v - a.v)
  const bc = sm?.by_cls || {}
  const isToday = day === "today" || day === todayStr()

  return (
    <>
      <Section title={isToday ? "资产情绪" : `${day.slice(5)} 情绪`} note="当日报道">
        {senti && senti.length ? senti.map((r) => {
          const sv = r.score
          const half = Math.min(50, Math.abs(sv ?? 0) / 2)
          const thin = (r.bull_n + r.bear_n) < 2   // 不足 2 票=薄证据，降透明提示
          return (
            <button key={r.a} className={cn("srow", thin && "opacity-55")} title={`利多论据 ${r.bull_n} · 利空论据 ${r.bear_n}（规则先验聚合，仅供参考）`}
              onClick={() => store.setPartial({ asset: r.a }, { resetPage: true })}>
              <span className="sn whitespace-nowrap text-muted-foreground">{r.icon} {r.name}</span>
              <span className="sbar">
                {sv != null && (sv > 0
                  ? <i className="b" style={{ width: half + "%" }} />
                  : <i className="s" style={{ width: half + "%" }} />)}
              </span>
              <span className={cn("sv num", sv == null ? "zero" : sv > 0 ? "pos" : sv < 0 ? "neg" : "zero")}>
                {sv == null ? "–" : (sv > 0 ? "+" : "") + sv}
              </span>
              <span className="svote" title="利多票/利空票">{r.bull_n}/{r.bear_n}</span>
            </button>
          )
        }) : <Empty text="当日无方向论据" />}
        <p className="mt-2 text-xs text-muted-foreground"
          title="规则先验聚合的当日报道情绪：利多/利空论据加权投票，仅供速筛参考，不构成投资建议。">
          规则票聚合 · 仅供速筛
        </p>
      </Section>

      <Section title={isToday ? "今日分时" : `${day.slice(5)} 分时`}>
        <ChartContainer config={{ total: { color: "var(--chart-1)" } }} className="h-[96px] w-full">
          <BarChart data={hours} margin={{ top: 4, right: 0, bottom: 0, left: 0 }}>
            <XAxis dataKey="h" tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
              ticks={[0, 6, 12, 18, 23]} tickFormatter={(v) => String(v).padStart(2, "0")} axisLine={false} tickLine={false} />
            <ChartTooltip content={<ChartTooltipContent labelFormatter={(v) => `${String(v).padStart(2, "0")}时`} />} />
            <Bar dataKey="total" radius={[3, 3, 0, 0]} fill="var(--color-total)" isAnimationActive={false} />
          </BarChart>
        </ChartContainer>
      </Section>

      <Section title="来源占比">
        <ChartContainer config={{ v: { color: "var(--chart-2)" } }} className="h-[148px] w-full">
          <BarChart data={src} layout="vertical" margin={{ top: 0, right: 6, bottom: 0, left: 0 }}>
            <YAxis dataKey="label" type="category" width={68} interval={0}
              tick={{ fontSize: 11, fill: "var(--muted-foreground)" }} axisLine={false} tickLine={false} />
            <XAxis type="number" hide />
            <ChartTooltip content={<ChartTooltipContent hideLabel />} />
            <Bar dataKey="v" radius={[0, 4, 4, 0]} fill="var(--color-v)" isAnimationActive={false} barSize={12} />
          </BarChart>
        </ChartContainer>
      </Section>

      <Section title="分类构成">
        {["rumor", "debunk", "official", "noise"].filter((k) => bc[k]).length ? (
          ["rumor", "debunk", "official", "noise"].filter((k) => bc[k]).map((k) => (
            <div key={k} className="clsline">
              <span className={"c-" + k + "-t"}>{CLS_LABEL[k]}</span>
              <b className="num">{bc[k]}</b>
            </div>
          ))
        ) : <Empty text="无数据" />}
      </Section>

      <Section title={isToday ? "当日热词" : `${day.slice(5)} 热词`}>
        {kw && kw.length ? (
          <div className="flex flex-wrap gap-1.5">
            {kw.map(([k, n]) => (
              <Badge key={k} variant="secondary" asChild className="font-normal">
                <button onClick={() => store.setPartial({ q: k }, { resetPage: true })}>
                  {k}<b className="ml-1 text-xs font-normal text-muted-foreground">{n}</b>
                </button>
              </Badge>
            ))}
          </div>
        ) : <Empty text="暂无" />}
      </Section>

      <Section title="异动风向" note="近30分钟">
        {anomaly && anomaly.length ? (
          <Table>
            <TableBody>
              {anomaly.map((a, i) => (
                <TableRow key={i}>
                  <TableCell className="px-0 py-1.5 text-sm">{a.name} <code className="text-xs text-muted-foreground">{a.code}</code></TableCell>
                  <TableCell className="px-0 py-1.5 text-right text-sm tabular-nums text-muted-foreground">{a.count}次</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : <Empty text="非交易时段或暂无数据" />}
      </Section>

      <Section title="累计">
        {stats ? (
          <Table>
            <TableBody>
              <TableRow><TableCell className="px-0 py-1.5 text-sm">累计入库</TableCell>
                <TableCell className="px-0 py-1.5 text-right text-sm font-semibold tabular-nums">{stats.total || 0}</TableCell></TableRow>
              {Object.entries(stats.by_cls || {}).map(([k, v]) => (
                <TableRow key={k}><TableCell className={"px-0 py-1.5 text-sm c-" + k + "-t"}>{CLS_LABEL[k] || k}</TableCell>
                  <TableCell className="px-0 py-1.5 text-right text-sm tabular-nums text-muted-foreground">{v}</TableCell></TableRow>
              ))}
              <TableRow><TableCell className="px-0 py-1.5 text-sm">最新入库</TableCell>
                <TableCell className="px-0 py-1.5 text-right text-sm tabular-nums text-muted-foreground">{(stats.last_ts || "").slice(5, 16)}</TableCell></TableRow>
            </TableBody>
          </Table>
        ) : <Empty text="载入中…" />}
      </Section>
    </>
  )
}

function Section({ title, note, children }: { title: React.ReactNode; note?: string; children: React.ReactNode }) {
  return (
    <Card className="gap-0 border-border/70 bg-card/40 py-0 shadow-none">
      <CardHeader className="flex flex-row items-center justify-between px-4 pt-3.5 pb-2">
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
        {note && <span className="text-xs text-muted-foreground">{note}</span>}
      </CardHeader>
      <CardContent className="px-4 pb-3.5">{children}</CardContent>
    </Card>
  )
}
function Empty({ text }: { text: string }) {
  return <div className="py-1 text-sm text-muted-foreground">{text}</div>
}
