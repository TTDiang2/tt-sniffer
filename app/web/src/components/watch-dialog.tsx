/* 关注列表编辑（Dialog + Input + Button；alert 清退 → Sonner）。
   GET=生效列表（DB 用户层优先，无则 config 种子）；PUT=全量替换用户层，
   保存后热更新打分器（新条目立即生效，历史不重算）。 */
import { useEffect, useState } from "react"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { toast } from "sonner"
import { fetchOk } from "@/lib/api"
import { Plus, Trash2 } from "lucide-react"

interface WatchRow { code: string; name: string }
interface WatchResp { items: WatchRow[]; source: string }

export function WatchDialog({ open, onClose, onSaved, triggerRef }: {
  open: boolean; onClose: () => void; onSaved: () => void
  triggerRef: React.RefObject<HTMLButtonElement | null>
}) {
  const [rows, setRows] = useState<Array<WatchRow & { _key: number }>>([])
  const [source, setSource] = useState("config")

  useEffect(() => {
    if (!open) return
    void (async () => {
      try {
        const d = await fetchOk("/api/watchlist").then((r) => r.json()) as WatchResp
        setSource(d.source || "config")
        setRows((d.items || []).map((it, i) => ({ code: it.code || "", name: it.name || "", _key: i })))
        if (!(d.items || []).length) setRows([{ code: "", name: "", _key: 0 }])
      } catch {
        setRows([{ code: "", name: "", _key: 0 }])
      }
    })()
  }, [open])

  const setRow = (key: number, patch: Partial<WatchRow>) =>
    setRows((rs) => rs.map((r) => (r._key === key ? { ...r, ...patch } : r)))
  const addRow = () =>
    setRows((rs) => [...rs, { code: "", name: "", _key: Math.max(0, ...rs.map((r) => r._key)) + 1 }])

  const save = async () => {
    const items = rows
      .map((r) => ({ code: r.code.trim(), name: r.name.trim() }))
      .filter((x) => x.name)
    try {
      await fetchOk("/api/watchlist", 2, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(items),
      })
      toast.success("关注列表已保存（新采集条目立即按新表打分）")
      onClose()
      onSaved()
    } catch (e) {
      toast.error("保存失败：" + (e instanceof Error ? e.message : String(e)))
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        className="w-[min(520px,92vw)] sm:max-w-[520px]"
        role="dialog"
        aria-modal="true"
        aria-label="关注列表"
        onCloseAutoFocus={(e) => { e.preventDefault(); triggerRef.current?.focus() }}
      >
        <DialogHeader>
          <DialogTitle>关注列表</DialogTitle>
          <DialogDescription className="text-xs">
            {source === "user"
              ? "自定义列表：保存只写入本机数据库，不改 config.yaml。"
              : "当前为 config.yaml 种子，保存后转为自定义列表。"}
          </DialogDescription>
        </DialogHeader>
        <div className="-mx-1 max-h-[46vh] overflow-y-auto px-1">
          <div className="flex flex-col gap-2 py-1">
            {rows.map((r) => (
              <div key={r._key} className="flex gap-2">
                <Input
                  className="min-w-0 flex-1"
                  placeholder="代码（可空）"
                  value={r.code}
                  spellCheck={false}
                  onChange={(e) => setRow(r._key, { code: e.target.value })}
                />
                <Input
                  className="min-w-0 flex-[1.6]"
                  placeholder="名称（必填）"
                  value={r.name}
                  onChange={(e) => setRow(r._key, { name: e.target.value })}
                />
                <Button
                  variant="ghost" size="icon"
                  title="删除此行"
                  aria-label="删除此行"
                  onClick={() => setRows((rs) => rs.filter((x) => x._key !== r._key))}
                >
                  <Trash2 />
                </Button>
              </div>
            ))}
          </div>
        </div>
        <p className="text-xs text-muted-foreground">名称必填、代码可空；保存后新采集条目立即按新表打分（历史条目不重算）。</p>
        <DialogFooter className="gap-2 sm:justify-between">
          <Button variant="outline" size="sm" onClick={addRow}>
            <Plus />
            加一行
          </Button>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>取消</Button>
            <Button size="sm" onClick={() => void save()}>保存</Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
