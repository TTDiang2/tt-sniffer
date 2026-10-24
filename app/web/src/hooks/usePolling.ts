/* 通用轮询 hook（行情 60s / 采集状态 30s / 14日热度 120s）——卸载必清 timer */
import { useEffect } from "react"

export function usePolling(fn: () => void, ms: number, deps: unknown[] = []): void {
  useEffect(() => {
    fn()
    const t = window.setInterval(fn, ms)
    return () => window.clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
}
