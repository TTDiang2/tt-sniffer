/* 信息流：账本行（左色轨按分类/金色=关注）+ shadcn Badge 族 + 高亮 +
   折叠组 + 分页 + shadcn Empty/Skeleton 空态。竞态令牌与 SSE 在 store 层。 */
import { lazy, Suspense, useEffect, useRef, useState, useSyncExternalStore } from "react"
import * as store from "@/store"
import { CLS_LABEL, IMP_LABEL, SRC_LABEL, type Item } from "@/lib/types"
import { searchTerms, fmtTs } from "@/lib/format"
import { highlight } from "@/lib/highlight"
import {
  ClsBadge, CopyBadge, FoldBadge, ImpBadge, ScoreBadge, SrcBadge, StreetBadge, TargetBadge, WatchBadge,
} from "@/components/badges"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { ReaderDialog } from "@/components/reader-dialog"
import { cn } from "@/lib/utils"
import { Inbox, TriangleAlert } from "lucide-react"

export function Feed() {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  const view = s.view
  const [reader, setReader] = useState<Item | null>(null)
  const readerAnchor = useRef<HTMLElement | null>(null)

  if (view === "chart") {
    return (
      <Suspense fallback={<ChartsSkeleton />}>
        <LazyChartsView />
      </Suspense>
    )
  }

  let body: React.ReactNode
  if (s.error) {
    body = (
      <Empty className="border border-dashed border-border">
        <EmptyHeader>
          <EmptyMedia variant="icon"><TriangleAlert /></EmptyMedia>
          <EmptyTitle>加载失败</EmptyTitle>
          <EmptyDescription>{s.error}。若持续出现请确认服务已启动后刷新页面。</EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  } else if (s.loading) {
    body = <FeedSkeleton />
  } else if (!s.items.length) {
    const filtered = s.q || s.band !== "all" || s.srcs.size || s.cls !== "all" || s.asset !== "all" || s.tierOnly
    body = (
      <Empty className="border border-dashed border-border">
        <EmptyHeader>
          <EmptyMedia variant="icon"><Inbox /></EmptyMedia>
          <EmptyTitle>这一天暂无条目</EmptyTitle>
          <EmptyDescription>
            {filtered ? "试试放宽筛选，或点「清除筛选」。" : "采集到新内容后会自动出现在这里。"}
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  } else {
    body = s.items.map((it) => (
      <FeedRow key={it.id} it={it} onOpen={(x, el) => { readerAnchor.current = el; setReader(x) }} />
    ))
  }

  const pages = Math.max(1, Math.ceil(s.total / store.PAGE))
  const cur = Math.min(s.page, pages)

  return (
    <div>
      <div id="feed" aria-busy={s.loading} className="mt-4 overflow-hidden rounded-xl border border-border/70 bg-card/30">
        {body}
      </div>
      <ReaderDialog it={reader} anchor={readerAnchor.current} onClose={() => setReader(null)} />
      {pages > 1 && (
        <div className="feedfoot flex items-center justify-center gap-3 py-5">
          <Button variant="outline" size="sm" disabled={cur <= 1} onClick={() => store.goPage(cur - 1)}>‹ 上一页</Button>
          <span className="text-sm text-muted-foreground">第 <b className="tabular-nums text-foreground">{cur}</b> / <b className="tabular-nums text-foreground">{pages}</b> 页</span>
          <Button variant="outline" size="sm" disabled={cur >= pages} onClick={() => store.goPage(cur + 1)}>下一页 ›</Button>
        </div>
      )}
    </div>
  )
}

function FeedSkeleton() {
  return (
    <div className="divide-y divide-border/70">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="flex flex-col gap-2 p-4">
          <div className="flex gap-2">
            <Skeleton className="h-5 w-16 rounded-full" />
            <Skeleton className="h-5 w-20 rounded-full" />
            <Skeleton className="h-5 w-10 rounded-full" />
          </div>
          <Skeleton className="h-4 w-4/5" />
          <Skeleton className="h-4 w-3/5" />
        </div>
      ))}
    </div>
  )
}

function ChartsSkeleton() {
  return (
    <div className="mt-4 flex flex-col gap-4">
      <div className="grid gap-4 md:grid-cols-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="rounded-xl border border-border/70 p-4">
            <Skeleton className="h-5 w-32" />
            <Skeleton className="mt-4 h-[280px] w-full rounded-lg" />
          </div>
        ))}
      </div>
      {Array.from({ length: 3 }).map((_, i) => (
        <div key={i} className="rounded-xl border border-border/70 p-4">
          <Skeleton className="h-5 w-40" />
          <Skeleton className="mt-4 h-[280px] w-full rounded-lg" />
        </div>
      ))}
    </div>
  )
}

export function FeedRow({ it, onOpen }: { it: Item; onOpen: (it: Item, el: HTMLElement) => void }) {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  const [open, setOpen] = useState(false)
  const [flash, setFlash] = useState(false)
  const prevSize = useRef(it.group_size || 0)
  useEffect(() => {
    if ((it.group_size || 0) > prevSize.current) {
      setFlash(true)
      const t = window.setTimeout(() => setFlash(false), 1600)
      prevSize.current = it.group_size || 0
      return () => window.clearTimeout(t)
    }
    prevSize.current = it.group_size || 0
  }, [it.group_size])
  const qts = searchTerms(store.getState().q)
  const { date, time } = fmtTs(it.ts)
  const body = it.text && it.text !== it.title ? it.text : ""
  const long = body.length > 180

  const imps = (it.impacts || [])
    .filter((x) => x.d !== "rel" || (s.asset !== "all" && s.asset === x.a))
    .map((x, i) => (
      <ImpBadge
        key={i}
        d={x.d}
        guess={x.g === "guess"}
        label={`${IMP_LABEL[x.d]}·${x.a}`}
        tip={`规则先验·${x.k || ""}${x.g === "guess" ? "（预期阶段，非落地）" : ""}`}
      />
    ))

  return (
    <article
      data-id={it.id}
      tabIndex={-1}
      onClick={(e) => {
        if ((e.target as HTMLElement).closest("a,button,input,select,textarea,label,.kw")) return
        const sel = window.getSelection()
        if (sel && String(sel)) return
        onOpen(it, e.currentTarget)
      }}
      className={cn(
        "feedrow relative grid cursor-pointer grid-cols-[3px_1fr] border-b border-border/70 py-4 pr-4 pb-4 pl-0 last:border-b-0",
        flash && "row-flash",
        it.tier === "street" && "bg-gold/5",
        it.watch && "[&>.rail]:bg-gold",
      )}
    >
      <div className={cn("rail w-[3px] rounded-r-sm",
        it.cls === "rumor" && "bg-rumor",
        it.cls === "debunk" && "bg-debunk",
        it.cls === "official" && "bg-official",
        it.cls === "noise" && "bg-border-strong",
      )} />
      <div className="min-w-0 pl-4">
        <div className="meta flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span className="tabular-nums">{date} {time}</span>
          <SrcBadge label={SRC_LABEL[it.source] || it.source} />
          <ClsBadge cls={it.cls} label={CLS_LABEL[it.cls] || it.cls} />
          {it.tier === "street" && (
            <Tooltip>
              <TooltipTrigger asChild><span><StreetBadge /></span></TooltipTrigger>
              <TooltipContent>未证实来源（股吧/TG，打分已按可信度降档）</TooltipContent>
            </Tooltip>
          )}
          <ScoreBadge score={it.score} />
          {imps}
          {it.watch && <WatchBadge />}
          {(it.targets || []).map((t) => <TargetBadge key={t} t={t} />)}
          {it.group_size && it.group_size > 1 ? (
            <span onClick={(e) => { e.stopPropagation(); setOpen(!open) }}>
              <FoldBadge
                size={it.group_size}
                open={open}
                tip={`多源同稿/近近稿，内容指纹近似且关键数字一致${it.group_prev ? `；另有 ${it.group_prev} 条在更早日期` : ""}`}
              />
            </span>
          ) : it.cluster_size && it.cluster_size > 1 ? <CopyBadge n={it.cluster_size} /> : null}
          <span className="flex flex-wrap gap-1">
            {(it.keywords || []).filter((k) => k !== "异动关联").slice(0, 4).map((k) => (
              <Badge key={k} variant="secondary" asChild className="font-normal">
                <button className="cursor-pointer" onClick={() => store.setPartial({ q: k }, { resetPage: true })}>{k}</button>
              </Badge>
            ))}
          </span>
          {it.url && (
            <a href={it.url} target="_blank" rel="noopener" className="ml-auto text-primary underline-offset-4 hover:underline">原文 ↗</a>
          )}
        </div>
        {it.title && (
          <div
            className="mt-2 cursor-pointer text-sm leading-relaxed font-medium"
            dangerouslySetInnerHTML={{ __html: highlight(it.title, it.keywords, qts) }}
          />
        )}
        {body && (
          <p
            className={cn("mt-1.5 mb-0 text-sm leading-relaxed break-words text-muted-foreground", long && !open && "clamp3")}
            dangerouslySetInnerHTML={{ __html: highlight(body, it.keywords, qts) }}
          />
        )}
        {it.translation && (
          <div className="mt-2.5 rounded-r-md border-l-2 border-official bg-official/5 px-3 py-2 text-sm">
            <div className="mb-1 text-xs text-muted-foreground">🤖 中文翻译</div>
            {it.translation.title_zh && (
              <div className="font-medium text-foreground">{it.translation.title_zh}</div>
            )}
            {it.translation.summary_zh && (
              <div className="mt-1 leading-relaxed text-muted-foreground">{it.translation.summary_zh}</div>
            )}
          </div>
        )}
        {it.llm?.one_line && (
          <div className="mt-2.5 rounded-r-md border-l-2 border-official bg-official/5 px-3 py-2 text-sm text-muted-foreground">
            🤖 {it.llm.one_line}
          </div>
        )}
        {open && (it.group_members?.length || 0) > 0 && (
          <div className="gmembers mt-2 ml-1 border-l border-dashed border-border pl-3">
            {(it.group_members || []).map((m) => (
              <div key={m.id} data-id={m.id} className="flex items-baseline gap-2 border-b border-dashed border-border/60 py-1.5 pr-1 text-sm last:border-none">
                <span className="tabular-nums text-xs text-muted-foreground">{(m.ts || "").slice(11, 16)}</span>
                <SrcBadge label={SRC_LABEL[m.source] || m.source} />
                <ScoreBadge score={m.score} />
                <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-muted-foreground">{m.title || m.text || ""}</span>
              </div>
            ))}
            {!!it.group_more && (
              <div className="py-1.5 pl-1 text-sm text-muted-foreground">…另有 {it.group_more} 条</div>
            )}
          </div>
        )}
      </div>
    </article>
  )
}

const LazyChartsView = lazy(() => import("@/components/charts-view").then((m) => ({ default: m.ChartsView })))
