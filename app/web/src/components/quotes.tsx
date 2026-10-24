/* 行情条：数字右对齐+千分位+固定 2 位小数；grid 等宽列（尾行不拉伸）。
   shadcn Card 基线，红涨绿跌仅作用在涨跌幅文字上。 */
import { useSyncExternalStore } from "react"
import { fetchJson } from "@/lib/api"
import { fmtPct, fmtPrice } from "@/lib/format"
import type { Quote } from "@/lib/types"
import { usePolling } from "@/hooks/usePolling"
import * as store from "@/store"
import { Card, CardContent } from "@/components/ui/card"
import { cn } from "@/lib/utils"

export function Quotes() {
  const day = useSyncExternalStore(store.subscribe, () => store.getState().day)
  const [quotes, setQuotes] = useQuotesState()

  usePolling(() => {
    void (async () => {
      try {
        const d = await fetchJson<{ quotes: Quote[] }>("/api/quotes")
        setQuotes(d.quotes || [])
      } catch { setQuotes([]) }
    })()
  }, 8000, [day])

  if (!quotes.length) {
    return (
      <div className="border-b border-border px-6 py-3 text-sm text-muted-foreground">
        行情源暂不可用（不影响主功能）
      </div>
    )
  }
  return (
    <div
      className="quotes-grid grid grid-cols-[repeat(auto-fill,minmax(148px,1fr))] gap-3 border-b border-border px-4 py-4 min-[1600px]:grid-cols-5 md:px-6"
      aria-label="行情快照（8s 刷新；红涨绿跌）"
    >
      {quotes.map((q) => {
        const c = q.chg_pct == null ? 0 : q.chg_pct
        const cls = c > 0 ? "text-rumor" : c < 0 ? "text-debunk" : "text-muted-foreground"
        return (
          <Card key={q.name} className="gap-0 rounded-lg border-border/70 bg-card py-0 shadow-none" title={`源时间 ${q.updated || "—"}（较昨收/昨结）`}>
            <CardContent className="px-4 py-3 text-right">
              <span className="flex items-baseline justify-between gap-2">
                <span className="text-xs whitespace-nowrap text-muted-foreground">{q.name}</span>
                <span className={cn("text-sm font-medium whitespace-nowrap tabular-nums", cls)}>{fmtPct(q.chg_pct)}</span>
              </span>
              <span key={`${q.key}-${q.price}`} className="quote-price mt-1 block text-right text-lg leading-tight font-semibold whitespace-nowrap tabular-nums">
                {fmtPrice(q.price)}
              </span>
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}

/* 局部 state（模块级缓存避免轮询闪烁——60s 刷新间保留旧值） */
import { useReducer } from "react"
let quotesCache: Quote[] = []
function useQuotesState() {
  const [v, dispatch] = useReducer((_: Quote[], n: Quote[]) => { quotesCache = n; return n }, quotesCache)
  return [v, dispatch as (v: Quote[]) => void] as const
}
