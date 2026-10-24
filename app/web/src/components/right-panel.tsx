/* 右栏（桌面常驻 316px / 移动端 Sheet）：速览卡 + 辟谣对撞（可点开全文）
   + 统计面板。shadcn Card 基线。 */
import { lazy, Suspense, useRef, useState, useSyncExternalStore } from "react"
import { fetchJson } from "@/lib/api"
import { highlight } from "@/lib/highlight"
import { searchTerms, todayStr } from "@/lib/format"
import type { FeedResp, Item } from "@/lib/types"
import * as store from "@/store"
import { usePolling } from "@/hooks/usePolling"
import { useReducer } from "react"
import { ScoreBadge } from "@/components/badges"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { ReaderDialog } from "@/components/reader-dialog"
import { Settings2 } from "lucide-react"
const LazyStatsPanel = lazy(() => import("@/components/stats-panel").then((m) => ({ default: m.StatsPanel })))

export function RightPanel({ onWatchCfg, watchRef }: {
  onWatchCfg: () => void; watchRef: React.RefObject<HTMLButtonElement | null>
}) {
  const day = useSyncExternalStore(store.subscribe, () => store.getState().day)
  const [top, setTop] = useLocal<FeedResp | null>(null)
  const [watch, setWatch] = useLocal<FeedResp | null>(null)
  const [reader, setReader] = useState<Item | null>(null)
  const anchor = useRef<HTMLElement | null>(null)

  usePolling(() => {
    void (async () => {
      try {
        const [t, w] = await Promise.all([
          fetchJson<FeedResp>(`/api/items?cls=all&sort=score&min_score=1&limit=5&day=${todayStr()}`),
          fetchJson<FeedResp>(`/api/items?cls=watch&sort=score&limit=5&day=${todayStr()}`),
        ])
        setTop(t); setWatch(w)
      } catch { /* 速览缺席不影响主链路 */ }
    })()
  }, 30000, [day])

  const open = (it: Item, el: HTMLElement) => { anchor.current = el; setReader(it) }

  return (
    <div className="right-panel flex flex-col gap-4 p-4" aria-label="信息看板">
      <Card className="gap-0 border-border/70 bg-card/40 py-0 shadow-none">
        <CardHeader className="flex flex-row items-center justify-between px-4 pt-3.5 pb-2">
          <CardTitle className="text-sm font-medium">今日最高分</CardTitle>
          <span className="text-xs text-muted-foreground">{top?.total || 0} 条 ≥1 分</span>
        </CardHeader>
        <CardContent className="px-2 pb-2">
          {(top?.items || []).length
            ? (top!.items.map((it) => <BriefRow key={it.id} it={it} onOpen={open} />))
            : <Empty text="今天还没有得分条目" />}
        </CardContent>
      </Card>

      <Card className="gap-0 border-border/70 bg-card/40 py-0 shadow-none">
        <CardHeader className="flex flex-row items-center justify-between px-4 pt-3.5 pb-2">
          <CardTitle className="text-sm font-medium">关注命中</CardTitle>
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            {watch?.total || 0} 条
            <Button ref={watchRef} variant="ghost" size="icon-sm" title="编辑关注列表" aria-label="编辑关注列表" onClick={onWatchCfg}>
              <Settings2 />
            </Button>
          </span>
        </CardHeader>
        <CardContent className="px-2 pb-2">
          {(watch?.items || []).length
            ? (watch!.items.map((it) => <BriefRow key={it.id} it={it} onOpen={open} />))
            : <Empty text="关注标的今天还没有命中" />}
        </CardContent>
      </Card>

      <Collide onOpen={open} />
      <Suspense fallback={<StatsSkeleton />}>
        <LazyStatsPanel />
      </Suspense>
      <ReaderDialog it={reader} anchor={anchor.current} onClose={() => setReader(null)} />
    </div>
  )
}

function BriefRow({ it, onOpen }: { it: Item; onOpen: (it: Item, el: HTMLElement) => void }) {
  const qt = searchTerms(store.getState().q)
  return (
    <button
      data-id={it.id}
      title={it.title || it.text}
      onClick={(e) => onOpen(it, e.currentTarget)}
      className="flex w-full cursor-pointer items-baseline gap-2 rounded-md px-2 py-2 text-left transition-colors hover:bg-muted/60"
    >
      <span className="text-xs tabular-nums text-muted-foreground">{(it.ts || "").slice(11, 16)}</span>
      <ScoreBadge score={it.score} />
      <span
        className="min-w-0 flex-1 truncate text-sm text-muted-foreground"
        dangerouslySetInnerHTML={{ __html: highlight(it.title || it.text || "", it.keywords, qt) }}
      />
    </button>
  )
}

function Empty({ text }: { text: string }) {
  return <div className="px-2 py-2 text-sm text-muted-foreground">{text}</div>
}

function StatsSkeleton() {
  return (
    <Card className="gap-3 border-border/70 bg-card/40 p-4 shadow-none">
      <Skeleton className="h-4 w-20" />
      <Skeleton className="h-16 w-full" />
      <Skeleton className="h-24 w-full" />
    </Card>
  )
}

interface Pair { rumors: Array<Item & { disp?: string }>; debunk: Item & { disp?: string } }
function Collide({ onOpen }: { onOpen: (it: Item, el: HTMLElement) => void }) {
  const day = useSyncExternalStore(store.subscribe, () => store.curDay())
  const [pairs, setPairs] = useLocal<Pair[] | null>(null)
  usePolling(() => {
    void (async () => {
      try {
        const c = await fetchJson<{ pairs: Pair[] }>(`/api/collide?day=${day}`)
        setPairs(c.pairs || [])
      } catch { /* 对撞缺席不影响主链路 */ }
    })()
  }, 30000, [day])
  return (
    <Card className="gap-0 border-border/70 bg-card/40 py-0 shadow-none">
      <CardHeader className="flex flex-row items-center justify-between px-4 pt-3.5 pb-2">
        <CardTitle className="text-sm font-medium">辟谣对撞</CardTitle>
        <span className="text-xs text-muted-foreground">{(pairs || []).length} 组 · 点按看全文</span>
      </CardHeader>
      <CardContent className="px-3 pb-3">
        {(pairs || []).length ? (
          <div className="flex flex-col gap-2">
            {pairs!.map((p, i) => (
              <div key={i} className="grid grid-cols-[1fr_16px_1fr] items-stretch gap-1">
                <button
                  data-id={p.rumors[0].id}
                  data-collide="rumor"
                  title={`传闻 ${(p.rumors[0].ts || "").slice(11, 16)} · 点按查看全文`}
                  onClick={(e) => onOpen(p.rumors[0], e.currentTarget)}
                  className="line-clamp-2 cursor-pointer rounded-md border border-rumor/20 border-l-2 border-l-rumor bg-rumor/5 px-2.5 py-2 text-left text-xs leading-relaxed text-muted-foreground transition-colors hover:bg-rumor/10"
                  dangerouslySetInnerHTML={{ __html: highlight(p.rumors[0].disp || "", [], searchTerms(store.getState().q)) }}
                />
                <div className="flex items-center justify-center text-xs text-muted-foreground">→</div>
                <button
                  data-id={p.debunk.id}
                  data-collide="debunk"
                  title={`辟谣 ${(p.debunk.ts || "").slice(11, 16)} · 点按查看全文`}
                  onClick={(e) => onOpen(p.debunk, e.currentTarget)}
                  className="line-clamp-2 cursor-pointer rounded-md border border-debunk/20 border-l-2 border-l-debunk bg-debunk/5 px-2.5 py-2 text-left text-xs leading-relaxed text-muted-foreground transition-colors hover:bg-debunk/10"
                  dangerouslySetInnerHTML={{ __html: highlight(p.debunk.disp || "", [], searchTerms(store.getState().q)) }}
                />
              </div>
            ))}
          </div>
        ) : <Empty text="当日暂无传闻-辟谣对撞" />}
      </CardContent>
    </Card>
  )
}

function useLocal<T>(init: T) {
  const [v, dispatch] = useReducer((_: T, n: T) => n, init)
  return [v, dispatch as (v: T) => void] as const
}
