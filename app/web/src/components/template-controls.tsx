/* 搜索模板（Select + 小 Dialog 消灭 prompt；模板=整套筛选状态存
 * localStorage，不含 page——模板是筛选集而非浏览位置）。两行式（S0 口径）。 */
import { useEffect, useState } from "react"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import * as store from "@/store"
import { BookmarkPlus, Trash2 } from "lucide-react"

const TMPL_KEY = "tts.qtmpl"
interface Tmpl { name: string; params: Record<string, unknown> }

function list(): Tmpl[] {
  try {
    const v = JSON.parse(localStorage.getItem(TMPL_KEY) || "[]")
    return Array.isArray(v) ? v : []
  } catch {
    return []
  }
}
function write(t: Tmpl[]) {
  localStorage.setItem(TMPL_KEY, JSON.stringify(t))
  window.dispatchEvent(new Event("tts-tmpl"))
}

export function TemplateControls() {
  const [tpls, setTpls] = useState<Tmpl[]>(list())
  const [sel, setSel] = useState("")
  const [saveOpen, setSaveOpen] = useState(false)
  const [name, setName] = useState("")
  useEffect(() => {
    const on = () => setTpls(list())
    window.addEventListener("tts-tmpl", on)
    return () => window.removeEventListener("tts-tmpl", on)
  }, [])

  const apply = (idx: string) => {
    const i = parseInt(idx, 10)
    const t = list()[i]
    if (!Number.isFinite(i) || !t) return
    const p = t.params || {}
    store.setPartial({
      q: String(p.q ?? ""),
      cls: ["all", "rumor", "debunk", "official", "watch"].includes(p.cls as string) ? p.cls as string : "all",
      srcs: new Set(Array.isArray(p.srcs) ? (p.srcs as string[]).filter((x) => typeof x === "string") : []),
      tierOnly: !!p.tier,
      band: ["hi", "mid", "low"].includes(p.band as string) ? p.band as string : "all",
      sort: p.sort === "score" ? "score" : "time",
      asset: /^[a-z_]+$/.test((p.asset as string) || "") ? p.asset as string : "all",
      dir: ["bull", "bear", "rel", "any"].includes(p.d as string) ? p.d as string : "all",
    }, { resetPage: true })
    store.setDay(p.day && /^\d{4}-\d{2}-\d{2}$/.test(p.day as string) ? p.day as string : "today")
    setSel("")
  }

  const current = () => {
    const st = store.getState()
    return {
      q: st.q, cls: st.cls, srcs: [...st.srcs], band: st.band,
      sort: st.sort, asset: st.asset, d: st.dir, day: st.day, tier: st.tierOnly,
    }
  }
  const save = () => {
    const n = name.trim()
    if (!n) return
    write([...list().filter((t) => t.name !== n), { name: n, params: current() }])
    setName("")
    setSaveOpen(false)
  }

  return (
    <div className="tmpl-row flex w-full flex-wrap items-center gap-2">
      <span className="text-xs text-muted-foreground">模板</span>
      <Select value={sel} onValueChange={apply}>
        <SelectTrigger className="min-w-0 flex-1" size="sm"><SelectValue placeholder="选择…" /></SelectTrigger>
        <SelectContent>
          {tpls.map((t, i) => (
            <SelectItem key={i} value={String(i)}>{t.name}</SelectItem>
          ))}
        </SelectContent>
      </Select>
      <span className="tbtns flex gap-1">
        <Button variant="ghost" size="sm"
          title="把当前筛选状态存为命名模板" onClick={() => setSaveOpen(true)}>
          <BookmarkPlus />
          存当前
        </Button>
        <Button variant="ghost" size="icon-sm"
          title="删除选中的模板"
          aria-label="删除选中的模板"
          disabled={!sel}
          onClick={() => {
            const t = list().filter((_, i) => String(i) !== sel)
            write(t)
            setSel("")
          }}>
          <Trash2 />
        </Button>
      </span>

      <Dialog open={saveOpen} onOpenChange={setSaveOpen}>
        <DialogContent className="w-[min(360px,92vw)] sm:max-w-[360px]">
          <DialogHeader>
            <DialogTitle>保存筛选模板</DialogTitle>
            <DialogDescription className="text-xs">保存当前的关键词、分类、来源、资产等筛选状态。</DialogDescription>
          </DialogHeader>
          <Input
            autoFocus
            placeholder="模板名称"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && save()}
          />
          <DialogFooter className="gap-2">
            <Button variant="outline" size="sm" onClick={() => setSaveOpen(false)}>取消</Button>
            <Button size="sm" disabled={!name.trim()} onClick={save}>保存</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
