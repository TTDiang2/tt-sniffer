/* 壳：桌面（≥761px：左常驻筛选栏 300px + 主区 + 右常驻看板 316px）
   + 移动单栏；--hh = header 实高（ResizeObserver），newbar/固定栏共用。 */
import { lazy, Suspense, useEffect, useRef, useState, useSyncExternalStore } from "react"
import * as store from "@/store"
import { useSSE } from "@/hooks/useSSE"
import { Header, FilterBar, SourcePanel, AssetTabs } from "@/components/shell"
import { Quotes } from "@/components/quotes"
import { Feed } from "@/components/feed"
import { RightPanel } from "@/components/right-panel"
import { Newbar } from "@/components/newbar"
import { DockTabs, useSwipe } from "@/components/dock-tabs"
import { TooltipProvider } from "@/components/ui/tooltip"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
const LazyAIDrawer = lazy(() => import("@/components/ai-drawer").then((m) => ({ default: m.AIDrawer })))
import { WatchDialog } from "@/components/watch-dialog"
import { Toaster } from "@/components/ui/sonner"

function useMedia(query: string): boolean {
  const [m, setM] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const mq = window.matchMedia(query)
    const on = () => setM(mq.matches)
    mq.addEventListener("change", on)
    return () => mq.removeEventListener("change", on)
  }, [query])
  return m
}

function useChrome() {
  useEffect(() => {
    const h = document.querySelector("#root header") || document.querySelector("header")
    if (!h) return
    const sync = () => document.documentElement.style.setProperty(
      "--hh", h.getBoundingClientRect().height + "px")
    sync()
    window.addEventListener("load", sync)
    window.addEventListener("resize", sync)
    let ro: ResizeObserver | undefined
    if (window.ResizeObserver) {
      ro = new ResizeObserver(sync)
      ro.observe(h)
    }
    return () => {
      window.removeEventListener("load", sync)
      window.removeEventListener("resize", sync)
      ro?.disconnect()
    }
  }, [])
}

function usePopState() {
  useEffect(() => {
    const onPop = () => {
      store.applyFromUrl()
      void store.reload()
    }
    window.addEventListener("popstate", onPop)
    return () => window.removeEventListener("popstate", onPop)
  }, [])
}

export default function App() {
  useSSE()
  useChrome()
  usePopState()
  useSwipe()
  const desktop = useMedia("(min-width: 761px)")
  const view = useSyncExternalStore(store.subscribe, () => store.getState().view)
  const [aiOpen, setAIOpen] = useState(false)
  const [aiMounted, setAIMounted] = useState(false)
  const [watchOpen, setWatchOpen] = useState(false)
  const [watchTick, setWatchTick] = useState(0)
  const [leftCollapsed, setLeftCollapsed] = useState(() => localStorage.getItem("tts.leftCollapsed") === "1")
  const [rightCollapsed, setRightCollapsed] = useState(() => localStorage.getItem("tts.rightCollapsed") === "1")
  const aiBtnRef = useRef<HTMLButtonElement | null>(null)
  const watchBtnRef = useRef<HTMLButtonElement | null>(null)

  const toggleLeft = () => setLeftCollapsed((v) => {
    const next = !v
    localStorage.setItem("tts.leftCollapsed", next ? "1" : "0")
    return next
  })
  const toggleRight = () => setRightCollapsed((v) => {
    const next = !v
    localStorage.setItem("tts.rightCollapsed", next ? "1" : "0")
    return next
  })

  const quotesPad = {
    paddingLeft: desktop && !leftCollapsed ? 314 : 0,
    paddingRight: desktop && !rightCollapsed ? 316 : 0,
  }
  const contentPad = {
    paddingLeft: desktop && !leftCollapsed ? 314 : 0,
    paddingRight: desktop && !rightCollapsed ? 330 : 0,
  }

  useEffect(() => {
    store.applyFromUrl()
    void store.reload()
  }, [])

  return (
    <TooltipProvider delayDuration={250}>
      <div className="flex min-h-screen flex-col">
        <Header
          onAI={() => { setAIMounted(true); setAIOpen(true) }}
          aiRef={aiBtnRef}
          leftCollapsed={leftCollapsed}
          rightCollapsed={rightCollapsed}
          onToggleLeft={toggleLeft}
          onToggleRight={toggleRight}
        />
        {desktop && (
          <div className="transition-[padding] duration-200 ease-out" style={quotesPad}>
            <Quotes />
          </div>
        )}
        <div className="transition-[padding] duration-200 ease-out" style={contentPad}>
          {desktop && !leftCollapsed && (
            <aside
              className="fixed top-[var(--hh,48px)] bottom-0 left-0 z-[5] flex w-[300px] flex-col gap-4 overflow-y-auto border-r border-border bg-background p-4"
              aria-label="筛选 · 搜索 · 设置"
            >
              {/* 卡片头部对齐右栏「今日最高分」样式：gap-0 py-0 + 单行基线头（标题上下空隙收口） */}
              <Card className="gap-0 border-border/70 bg-card/40 py-0 shadow-none">
                <CardHeader className="flex flex-row items-center justify-between px-3.5 pt-3.5 pb-2">
                  <CardTitle className="text-sm font-medium">搜索与筛选</CardTitle>
                </CardHeader>
                <CardContent className="px-3.5 pb-3.5">
                  <FilterBar />
                </CardContent>
              </Card>
              <Card className="gap-0 border-border/70 bg-card/40 py-0 shadow-none">
                <CardHeader className="flex flex-row items-center justify-between px-3.5 pt-3.5 pb-2">
                  <CardTitle className="text-sm font-medium">来源</CardTitle>
                </CardHeader>
                <CardContent className="px-3.5 pb-3.5">
                  <SourcePanel />
                </CardContent>
              </Card>
              <Card className="gap-0 border-border/70 bg-card/40 py-0 shadow-none">
                <CardHeader className="flex flex-row items-center justify-between px-3.5 pt-3.5 pb-2">
                  <CardTitle className="text-sm font-medium">资产</CardTitle>
                </CardHeader>
                <CardContent className="px-3.5 pb-3.5">
                  <AssetTabs />
                </CardContent>
              </Card>
            </aside>
          )}
          <main id="main" className="min-w-0 px-4 pb-20 min-[761px]:px-6">
            <Feed />
          </main>
          {desktop && !rightCollapsed && (
            <aside
              className="right-rail fixed top-[var(--hh,48px)] bottom-0 right-0 z-[5] w-[316px] overflow-y-auto border-l border-border bg-background"
              style={{ right: "var(--removed-body-scroll-bar-size, 0px)" }}
            >
              <RightPanel key={watchTick} onWatchCfg={() => setWatchOpen(true)} watchRef={watchBtnRef} />
            </aside>
          )}
        </div>
        <Newbar />
        {!desktop && <DockTabs onWatchCfg={() => setWatchOpen(true)} />}
        {aiMounted && (
          <Suspense fallback={null}>
            <LazyAIDrawer open={aiOpen} onOpenChange={setAIOpen} triggerRef={aiBtnRef} />
          </Suspense>
        )}
        <WatchDialog open={watchOpen} onClose={() => setWatchOpen(false)} onSaved={() => setWatchTick((v) => v + 1)} triggerRef={watchBtnRef} />
        <Toaster position="top-center" richColors theme="dark" />
        <span className="sr-only" aria-live="polite">{view === "chart" ? "统计视图" : "信息流视图"}</span>
      </div>
    </TooltipProvider>
  )
}
