/* 新消息浮条：top 复用 --hh 机制，点击回今天回顶。 */
import { useSyncExternalStore } from "react"
import * as store from "@/store"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { ArrowUp } from "lucide-react"

export function Newbar() {
  const pending = useSyncExternalStore(store.subscribe, () => store.getState().pending)
  return (
    <Button
      id="newbar"
      size="sm"
      className={cn(
        "fixed left-1/2 z-30 -translate-x-1/2 rounded-full shadow-lg transition-all duration-200",
        "top-[calc(var(--hh,48px)+8px)] max-md:top-auto max-md:bottom-[calc(88px+env(safe-area-inset-bottom,0px))]",
        pending > 0 ? "pointer-events-auto translate-y-0 opacity-100" : "pointer-events-none -translate-y-3 opacity-0",
      )}
      onClick={() => {
        store.clearPending()
        store.setDay("today")
        window.scrollTo({ top: 0, behavior: "smooth" })
      }}
    >
      <ArrowUp className="size-4" />
      {pending} 条新消息
    </Button>
  )
}
