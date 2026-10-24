/* AI 分析对话（Sheet side=right + 多会话归档 + react-markdown + Sonner）：
   流式 SSE 80ms 节流、thinking 占位、发送钮禁用、HTTP 非 200 立抛。
   会话存 localStorage（最多 30 个），旧单列表自动迁移为一个「历史对话」。 */
import { useEffect, useRef, useState } from "react"
import {
  Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle,
} from "@/components/ui/sheet"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { toast } from "sonner"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import rehypeSanitize from "rehype-sanitize"
import * as store from "@/store"
import { dayOffset, todayStr } from "@/lib/format"
import { fetchJson } from "@/lib/api"
import type { AssetDef } from "@/lib/types"
import { cn } from "@/lib/utils"
import { History, Loader2, MessagesSquare, Plus, Send, Sparkles, Trash2, X } from "lucide-react"

/* 多会话存储。sessions=[{id,created,title,messages}]（保留最近 30 个）。 */
const AI_LS = "tts.ai.history"        // 旧单列表（迁移源，只读）
const AI_LS_SESSIONS = "tts.ai.sessions"
const AI_LS_CUR = "tts.ai.cur"
const MAX_SESSIONS = 30
type Msg = { role: "user" | "assistant"; content: string }
interface Session { id: string; created: number; title: string; messages: Msg[] }

function loadSessions(): Session[] {
  let ss: Session[] = []
  try {
    const v = JSON.parse(localStorage.getItem(AI_LS_SESSIONS) || "[]")
    if (Array.isArray(v)) ss = v
  } catch { /* 损坏则按空处理 */ }

  // 旧单列表迁移：按最后一条消息去重，迁移后清掉旧键
  try {
    const legacy = JSON.parse(localStorage.getItem(AI_LS) || "[]")
    if (Array.isArray(legacy) && legacy.length) {
      const last = legacy[legacy.length - 1] as Msg
      const exists = ss.some((s) => s.messages.some((m) => m.role === last.role && m.content === last.content))
      if (!exists) {
        const firstUser = legacy.find((m: Msg) => m.role === "user")
        const s: Session = {
          id: "legacy-" + Date.now(),
          created: Date.now(),
          title: (firstUser?.content || "历史对话").slice(0, 20),
          messages: legacy.slice(-40),
        }
        ss = [s, ...ss]
        saveSessions(ss)
      }
      localStorage.removeItem(AI_LS)
    }
  } catch { /* 无可迁移 */ }
  return ss
}
function saveSessions(ss: Session[]) {
  localStorage.setItem(AI_LS_SESSIONS, JSON.stringify(ss.slice(0, MAX_SESSIONS)))
}
function curSessionId(): string | null {
  return localStorage.getItem(AI_LS_CUR) || null
}
function setCurSessionId(id: string) {
  localStorage.setItem(AI_LS_CUR, id)
}
function pushMsg(sid: string, role: Msg["role"], content: string) {
  const ss = loadSessions()
  const s = ss.find((x) => x.id === sid)
  if (!s) return
  s.messages.push({ role, content })
  if (role === "user" && s.messages.filter((m) => m.role === "user").length === 1) {
    s.title = content.slice(0, 20)
  }
  s.messages = s.messages.slice(-40)
  saveSessions(ss)
}
function newSession(): Session {
  return { id: "s-" + Date.now() + "-" + Math.random().toString(36).slice(2, 6), created: Date.now(), title: "新对话", messages: [] }
}
function shortDate(ts: number) {
  const d = new Date(ts)
  return `${String(d.getMonth() + 1).padStart(2, "0")}/${String(d.getDate()).padStart(2, "0")}`
}

export function AIDrawer({ open, onOpenChange, triggerRef }: {
  open: boolean; onOpenChange: (o: boolean) => void; triggerRef: React.RefObject<HTMLButtonElement | null>
}) {
  // 触屏设备（无 Shift+Enter 概念）用短 placeholder，防窄屏折行撑高输入框
  const coarse = typeof window !== "undefined" && window.matchMedia("(pointer: coarse)").matches
  const [sessions, setSessions] = useState<Session[]>([])
  const [curId, setCurId] = useState<string | null>(null)
  const msgs = sessions.find((x) => x.id === curId)?.messages ?? []
  const cur = sessions.find((x) => x.id === curId)
  const [streaming, setStreaming] = useState<null | { acc: string; thinking: boolean }>(null)
  const [scope, setScope] = useState("today")
  const [assetScope, setAssetScope] = useState("all")
  const [assets, setAssets] = useState<AssetDef[]>([])
  const [kwd, setKwd] = useState("")
  const [input, setInput] = useState("")
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const boxRef = useRef<HTMLDivElement>(null)
  const scopeRef = useRef(scope)
  const assetRef = useRef(assetScope)
  const kwdRef = useRef(kwd)
  scopeRef.current = scope
  assetRef.current = assetScope
  kwdRef.current = kwd

  useEffect(() => {
    if (!open) return
    const ss = loadSessions()
    let cid = curSessionId()
    if (!ss.find((x) => x.id === cid)) {
      if (ss.length) {
        cid = ss[0].id
      } else {
        const s = newSession()
        ss.unshift(s)
        cid = s.id
        saveSessions(ss)
      }
      setCurSessionId(cid!)
    }
    setSessions(ss)
    setCurId(cid)
    void (async () => {
      try {
        const d = await fetchJson<{ enabled: boolean }>("/api/ai/status")
        setEnabled(d.enabled)
      } catch { setEnabled(false) }
    })()
    void (async () => {
      try {
        const d = await fetchJson<{ assets: AssetDef[] }>("/api/assets")
        setAssets(d.assets || [])
      } catch { /* 资产下拉缺席 */ }
    })()
  }, [open])

  useEffect(() => {
    boxRef.current?.scrollTo({ top: boxRef.current.scrollHeight })
  }, [msgs, streaming])

  function newChat() {
    if (streaming) return
    const s = newSession()
    const ss = [s, ...sessions]
    saveSessions(ss)
    setSessions(ss)
    setCurId(s.id)
    setCurSessionId(s.id)
    setInput("")
    setStreaming(null)
  }

  function switchSession(id: string) {
    if (streaming) return
    setCurId(id)
    setCurSessionId(id)
  }

  function deleteCurrent() {
    if (streaming) return
    let ss = sessions.filter((x) => x.id !== curId)
    if (!ss.length) {
      const s = newSession()
      ss = [s]
      setCurId(s.id)
      setCurSessionId(s.id)
    } else {
      setCurId(ss[0].id)
      setCurSessionId(ss[0].id)
    }
    setSessions(ss)
    saveSessions(ss)
    toast.success("已删除当前对话")
  }

  async function send() {
    const q = input.trim()
    if (!q || streaming || !curId) return
    const sid = curId
    const outgoing: Msg[] = [...msgs.slice(-39), { role: "user", content: q }]
    setInput("")
    setSessions((ss) => ss.map((x) => x.id === sid
      ? { ...x, messages: [...x.messages, { role: "user", content: q }], title: x.messages.some((m) => m.role === "user") ? x.title : q.slice(0, 20) }
      : x))
    pushMsg(sid, "user", q)
    setStreaming({ acc: "", thinking: true })
    let acc = ""
    let last = 0
    try {
      const sel = (() => {
        const v = scopeRef.current
        const st = store.getState()
        if (v === "cur") {
          const p: Record<string, string | undefined> = { scope: "search", q: st.q || undefined }
          if (st.day !== "today") p.day = st.day
          if (!p.q && st.day === "today") p.scope = "today"
          return p
        }
        if (v === "search") return { scope: "search", q: kwdRef.current.trim() || undefined }
        if (v === "yday") return { scope: "day", day: dayOffset(todayStr(), -1) }
        if (v === "d7") return { scope: "days", days: "7" }
        if (v === "asset") return { scope: "asset", asset: assetRef.current }
        if (v === "cls") return { scope: "cls", cls: st.cls === "watch" ? "all" : st.cls }
        return { scope: "today" }
      })()
      const r = await fetch("/api/ai/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selector: sel, messages: outgoing }),
      })
      if (!r.ok) throw new Error("HTTP " + r.status)
      const reader = r.body!.getReader()
      const dec = new TextDecoder()
      let buf = ""
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        let i: number
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, i)
          buf = buf.slice(i + 2)
          const ev = (chunk.match(/^event: (.+)$/m) || [])[1]
          const data = (chunk.match(/^data: (.+)$/m) || [])[1]
          if (!ev || !data) continue
          const j = JSON.parse(data)
          if (ev === "delta") {
            acc += j.text
            const now = Date.now()
            if (now - last > 80 || j.text.endsWith("\n")) {
              setStreaming({ acc, thinking: false })
              last = now
            }
          } else if (ev === "error") {
            toast.error("AI 错误：" + j.error)
          }
        }
      }
      if (acc) {
        pushMsg(sid, "assistant", acc)
        setSessions((ss) => ss.map((x) => x.id === sid
          ? { ...x, messages: [...x.messages, { role: "assistant", content: acc }] }
          : x))
      } else {
        toast.info("（无回复）")
      }
    } catch (e) {
      toast.error("连接失败：" + (e instanceof Error ? e.message : String(e)))
    } finally {
      setStreaming(null)
    }
  }

  const suggestions = ["今天最值得关注的传闻？", "总结当前筛选下的利多/利空", "有哪些传闻已经被辟谣？"]

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        role="dialog"
        aria-modal="true"
        aria-label="AI 分析对话"
        onCloseAutoFocus={(e) => { e.preventDefault(); triggerRef.current?.focus() }}
        showCloseButton={false}
        className="w-[min(520px,100vw)] gap-0 p-0 sm:max-w-[520px]"
      >
        <SheetHeader className="flex-col gap-3 border-b border-border px-5 py-4">
          <div className="flex items-center gap-2">
            <div className="flex size-8 items-center justify-center rounded-md bg-primary text-primary-foreground">
              <Sparkles className="size-4" />
            </div>
            <div className="min-w-0">
              <SheetTitle className="text-base">AI 分析</SheetTitle>
              <SheetDescription className="truncate text-xs">
                {cur?.title && cur.title !== "新对话" ? cur.title : "基于当前情报库对话"}
              </SheetDescription>
            </div>
            <div className="ml-auto flex items-center gap-2">
              <Button variant="outline" size="sm" className="gap-1" disabled={!!streaming} onClick={newChat}>
                <Plus />
                新对话
              </Button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="ghost" size="icon-sm" className="size-9" aria-label="历史对话" disabled={!!streaming}>
                    <History />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-72">
                  <DropdownMenuLabel>历史对话（{sessions.length}）</DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  {sessions.length === 0 && <DropdownMenuItem disabled>暂无对话</DropdownMenuItem>}
                  {sessions.map((s) => (
                    <DropdownMenuItem
                      key={s.id}
                      onSelect={() => switchSession(s.id)}
                      className={cn("flex flex-col items-start gap-0.5", s.id === curId && "bg-accent/60")}
                    >
                      <span className="w-full truncate text-sm">{s.title}</span>
                      <span className="text-xs text-muted-foreground">
                        {shortDate(s.created)} · {s.messages.length} 条消息
                      </span>
                    </DropdownMenuItem>
                  ))}
                  <DropdownMenuSeparator />
                  <DropdownMenuItem variant="destructive" onSelect={deleteCurrent}>
                    <Trash2 />
                    删除当前对话
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
              <Button
                variant="ghost"
                size="icon-sm"
                className="size-9"
                aria-label="关闭 AI 分析"
                onClick={() => onOpenChange(false)}
              >
                <X />
              </Button>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Select value={scope} onValueChange={setScope}>
              <SelectTrigger className="h-8 w-[116px] text-xs" aria-label="分析范围"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="today">今日全部</SelectItem>
                <SelectItem value="yday">昨天</SelectItem>
                <SelectItem value="d7">近7天</SelectItem>
                <SelectItem value="asset">按资产</SelectItem>
                <SelectItem value="cls">按分类</SelectItem>
                <SelectItem value="search">按关键词</SelectItem>
                <SelectItem value="cur">当前筛选</SelectItem>
              </SelectContent>
            </Select>
            {scope === "asset" && (
              <Select value={assetScope} onValueChange={setAssetScope}>
                <SelectTrigger className="h-8 w-[116px] text-xs" aria-label="资产范围"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全部资产</SelectItem>
                  {assets.map((a) => (
                    <SelectItem key={a.a} value={a.a}>{a.icon} {a.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            {scope === "search" && (
              <input
                className="h-8 min-w-0 flex-1 rounded-md border border-input bg-transparent px-2.5 text-xs shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
                placeholder="关键词（逗号分隔多词）"
                value={kwd}
                onChange={(e) => setKwd(e.target.value)}
              />
            )}
          </div>
        </SheetHeader>

        <div ref={boxRef} className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-5 py-4">
          {enabled === false && (
            <div className="rounded-lg border border-border bg-muted/40 px-3.5 py-3 text-xs leading-relaxed text-muted-foreground">
              AI 未启用。启用方法：① 智谱开放平台 open.bigmodel.cn 申请 API Key；
              ② 设环境变量 TTSNIFFER_LLM_API_KEY；③ config.yaml 的 llm.enabled 改 true；
              ④ 重启服务。当前提问只会收到降级提示。
            </div>
          )}

          {!msgs.length && !streaming && (
            <div className="flex flex-1 flex-col items-center justify-center gap-4 py-10 text-center">
              <div className="flex size-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
                <MessagesSquare className="size-6" />
              </div>
              <div>
                <p className="text-sm font-medium">开始一段新对话</p>
                <p className="mt-1 text-xs text-muted-foreground">AI 只读取服务器已采集的情报，不会联网检索。</p>
              </div>
              <div className="flex w-full max-w-sm flex-col gap-2">
                {suggestions.map((s) => (
                  <Button key={s} variant="outline" size="sm" className="justify-start font-normal"
                    onClick={() => setInput(s)}>
                    {s}
                  </Button>
                ))}
              </div>
            </div>
          )}

          {msgs.map((m, i) => (
            <div key={i} className={cn(
              "max-w-[88%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed",
              m.role === "user"
                ? "self-end rounded-br-md bg-primary text-primary-foreground"
                : "self-start rounded-bl-md bg-muted text-foreground",
            )}>
              {m.role === "user" ? m.content : <Markdown src={m.content} />}
            </div>
          ))}

          {streaming && (
            <div className={cn(
              "max-w-[92%] self-start rounded-2xl rounded-bl-md bg-muted px-4 py-2.5 text-sm",
              streaming.thinking && "text-muted-foreground italic",
            )}>
              {streaming.thinking
                ? <span className="ai-thinking">AI 正在思考…</span>
                : <Markdown src={streaming.acc} />}
            </div>
          )}
        </div>

        <div className="border-t border-border bg-background p-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
          <div className="flex items-center gap-2.5">
            <Textarea
              className="max-h-40 min-h-11 flex-1 resize-none"
              placeholder={enabled === false ? "问点什么…（AI 未启用）" : coarse ? "问点什么…" : "问点什么…（Enter 发送，Shift+Enter 换行）"}
              value={input}
              disabled={!!streaming}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault()
                  void send()
                }
              }}
            />
            <Button size="icon" className="size-11 shrink-0 rounded-full disabled:opacity-40 disabled:bg-muted/40" disabled={!input.trim() || !!streaming} onClick={() => void send()} aria-label="发送">
              {streaming ? <Loader2 className="animate-spin" /> : <Send />}
            </Button>
          </div>
          <p className="mt-2 hidden text-xs text-muted-foreground md:block">历史对话仅存本机浏览器（最多 30 个）。</p>
        </div>
      </SheetContent>
    </Sheet>
  )
}

function Markdown({ src }: { src: string }) {
  return (
    <div className="ai-md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>
        {src}
      </ReactMarkdown>
    </div>
  )
}
