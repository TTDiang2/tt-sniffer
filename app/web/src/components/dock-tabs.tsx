/* 移动端底部 dock：shadcn 中性浮岛 + lucide 图标 + 金/白 pill 指示。
   仅 <761px 渲染；视图切换专职（桌面切换在 topbar）。滑切手势见 useSwipe。 */
import { useEffect, useRef, useState, useSyncExternalStore } from "react"
import * as store from "@/store"
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Button } from "@/components/ui/button"
import { RightPanel } from "@/components/right-panel"
import { FilterBar, SourcePanel, AssetTabs } from "@/components/shell"
import { Quotes } from "@/components/quotes"
import { cn } from "@/lib/utils"
import { ChartColumn, Newspaper, PanelRight, SlidersHorizontal, X } from "lucide-react"

const VIEW_ORDER = ["feed", "chart"]

export function DockTabs({ onWatchCfg }: { onWatchCfg: () => void }) {
  const view = useSyncExternalStore(store.subscribe, () => store.getState().view)
  const [boardOpen, setBoardOpen] = useState(false)
  const [filterOpen, setFilterOpen] = useState(false)
  const boardRef = useRef<HTMLButtonElement | null>(null)
  const filterRef = useRef<HTMLButtonElement | null>(null)
  const idx = VIEW_ORDER.indexOf(view)

  return (
    <>
      <nav className="tab-bar" aria-label="视图与信息看板">
        <button className="dock-side" ref={filterRef} aria-label="筛选 · 搜索" title="筛选 · 搜索"
          onClick={() => setFilterOpen(true)}>
          <SlidersHorizontal />
        </button>
        <div className="dock-tabs" id="dock-tabs">
          <span className="dock-pill" aria-hidden="true" style={{ transform: `translateX(${Math.max(0, idx)}00%)` }} />
          <button
            data-v="feed"
            aria-label="情报"
            aria-current={view === "feed" ? "page" : undefined}
            className={cn(view === "feed" && "active")}
            onClick={() => store.setView("feed")}
          >
            <Newspaper />
            <span className="lbl">情报</span>
          </button>
          <button
            data-v="chart"
            aria-label="统计"
            aria-current={view === "chart" ? "page" : undefined}
            className={cn(view === "chart" && "active")}
            onClick={() => store.setView("chart")}
          >
            <ChartColumn />
            <span className="lbl">统计</span>
          </button>
        </div>
        <button className="dock-side" ref={boardRef} aria-label="信息看板" title="信息看板"
          onClick={() => setBoardOpen(true)}>
          <PanelRight />
        </button>
      </nav>

      <Sheet open={filterOpen} onOpenChange={setFilterOpen}>
        <SheetContent
          side="left"
          role="dialog"
          aria-modal="true"
          aria-label="筛选 · 搜索 · 设置"
          showCloseButton={false}
          className="w-[min(360px,92vw)] gap-0 overflow-y-auto p-0 sm:max-w-[360px]"
          onCloseAutoFocus={(e) => { e.preventDefault(); filterRef.current?.focus() }}
        >
          <SheetHeader className="sticky top-0 z-10 flex min-h-14 flex-row items-center border-b border-border bg-background/95 px-4 py-2.5 backdrop-blur">
            <SheetTitle className="text-base">筛选 · 搜索 · 设置</SheetTitle>
            <Button
              variant="ghost"
              size="icon-sm"
              className="absolute top-2 right-2 size-9"
              aria-label="关闭筛选"
              onClick={() => setFilterOpen(false)}
            >
              <X />
            </Button>
          </SheetHeader>
          <div className="space-y-4 p-4">
            <FilterBar />
            <SourcePanel />
            <AssetTabs />
          </div>
        </SheetContent>
      </Sheet>

      <Sheet open={boardOpen} onOpenChange={setBoardOpen}>
        <SheetContent
          side="right"
          role="dialog"
          aria-modal="true"
          aria-label="信息看板"
          showCloseButton={false}
          className="w-[min(360px,92vw)] gap-0 overflow-y-auto p-0 pb-20 sm:max-w-[360px]"
          onCloseAutoFocus={(e) => { e.preventDefault(); boardRef.current?.focus() }}
        >
          <SheetHeader className="sticky top-0 z-10 flex min-h-14 flex-row items-center border-b border-border bg-background/95 px-4 py-2.5 backdrop-blur">
            <SheetTitle className="text-base">信息看板</SheetTitle>
            <Button
              variant="ghost"
              size="icon-sm"
              className="absolute top-2 right-2"
              aria-label="关闭信息看板"
              onClick={() => setBoardOpen(false)}
            >
              <X />
            </Button>
          </SheetHeader>
          <Quotes />
          <RightPanel
            onWatchCfg={() => { setBoardOpen(false); onWatchCfg() }}
            watchRef={boardRef}
          />
        </SheetContent>
      </Sheet>
    </>
  )
}

/* 滑切手势：<761px、仅 #main 区域，60px 阈值 + 横向占优 2:1；边缘不回绕 */
export function useSwipe() {
  useEffect(() => {
    let t0: Array<number> | null = null
    let last: Array<number> | null = null
    const isMobile = () => window.matchMedia("(max-width: 760px)").matches
    const onStart = (e: TouchEvent) => {
      if (!isMobile()) { t0 = null; last = null; return }
      if (!(e.target as HTMLElement).closest("#main")) { t0 = null; last = null; return }
      t0 = [e.touches[0].clientX, e.touches[0].clientY]
      last = t0
    }
    const onMove = (e: TouchEvent) => {
      if (!t0 || !isMobile()) return
      const t = e.touches[0]
      if (t) last = [t.clientX, t.clientY]
    }
    const finish = (x?: number, y?: number) => {
      if (t0 && isMobile() && x != null && y != null) {
        const [x0, y0] = t0
        const dx = x - x0, dy = y - y0
        if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 2) {
          const i = VIEW_ORDER.indexOf(store.getState().view)
          const j = i + (dx < 0 ? 1 : -1)
          if (j >= 0 && j < VIEW_ORDER.length) store.setView(VIEW_ORDER[j])
        }
      }
      t0 = null; last = null
    }
    const onEnd = (e: TouchEvent) => {
      const t = e.changedTouches[0]
      finish(t?.clientX ?? last?.[0], t?.clientY ?? last?.[1])
    }
    // 目标节点在滑动中被替换（骨架→图表）会触发 touchcancel：用最后位移结算
    const onCancel = () => finish(last?.[0], last?.[1])
    // capture：图表库可能在目标节点吞掉 touch 事件，捕获阶段先拿到起止点
    document.addEventListener("touchstart", onStart, { passive: true, capture: true })
    document.addEventListener("touchmove", onMove, { passive: true, capture: true })
    document.addEventListener("touchend", onEnd, { passive: true, capture: true })
    document.addEventListener("touchcancel", onCancel, { passive: true, capture: true })
    return () => {
      document.removeEventListener("touchstart", onStart, { capture: true })
      document.removeEventListener("touchmove", onMove, { capture: true })
      document.removeEventListener("touchend", onEnd, { capture: true })
      document.removeEventListener("touchcancel", onCancel, { capture: true })
    }
  }, [])
}
