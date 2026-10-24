/* SSE 实时：连接态/断线补偿/插顶/新消息浮条计数/告警声音（AudioContext
   预建=S0 口径）。useEffect 卸载必须关 EventSource——方案 §8.5 风险项。
   浏览器自动重连只覆盖网络层断开；重连拿到非 200/非 event-stream 响应
   （如隧道 502）时 EventSource 直接 CLOSED 且永不自愈，强刷才能恢复——
   故 CLOSED 时按指数退避重建连接（0.5s 起、封顶 30s，连上即复位）。 */
import { useEffect } from "react"
import * as store from "@/store"
import { beep, ensureAudio } from "@/lib/audio"

if (store.getState().sound) ensureAudio()   // 预建（suspended 态，S0 口径）

export function useSSE(): void {
  useEffect(() => {
    let es: EventSource | undefined
    let retryTimer: number | undefined
    let recreateTimer: number | undefined
    let backoff = 500
    let disposed = false

    const listen = (src: EventSource) => {
      src.addEventListener("item", (e) => {
        store.handleLiveItem(JSON.parse((e as MessageEvent).data))
      })
      src.addEventListener("alert", () => {
        if (store.getState().sound) beep()
      })
      src.addEventListener("llm", (e) => {
        store.handleLiveLlm(JSON.parse((e as MessageEvent).data))
      })
      src.addEventListener("translation", (e) => {
        store.handleLiveTranslation(JSON.parse((e as MessageEvent).data))
      })
    }

    const connect = () => {
      if (disposed) return
      es = new EventSource("/api/stream")
      es.onopen = () => { store.setConn(true); backoff = 500 }
      es.onerror = () => {
        store.setConn(false)
        window.clearTimeout(retryTimer)
        retryTimer = window.setTimeout(() => {
          if (!store.getState().connOK) void store.reload()
        }, 3000)
        if (es && es.readyState === EventSource.CLOSED && !disposed) {
          recreateTimer = window.setTimeout(connect, backoff)
          backoff = Math.min(backoff * 2, 30000)
        }
      }
      listen(es)
    }
    connect()

    return () => {
      disposed = true
      window.clearTimeout(retryTimer)
      window.clearTimeout(recreateTimer)
      es?.close()
      store.setConn(false)
    }
  }, [])
}
