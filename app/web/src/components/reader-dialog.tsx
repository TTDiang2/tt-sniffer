/* 全文阅读弹层（v24 语义 → shadcn Dialog）：整卡点击弹出；焦点圈闭/Esc/
   滚动锁由 Radix 提供。焦点还原经 anchor 于 onCloseAutoFocus（受控 open 无
   Trigger 时 Radix triggerRef 为空）。
   v27-5：去掉 min-height，头/正文/脚紧贴内容，长文只让正文滚动。 */
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { CLS_LABEL, IMP_LABEL, SRC_LABEL, type Item } from "@/lib/types"
import { fmtTs } from "@/lib/format"
import {
  ClsBadge, ScoreBadge, SrcBadge, StreetBadge, TargetBadge, WatchBadge, ImpBadge,
} from "@/components/badges"
import { highlight } from "@/lib/highlight"
import { searchTerms } from "@/lib/format"
import * as store from "@/store"

export function ReaderDialog({ it, anchor, onClose }: {
  it: Item | null; anchor: HTMLElement | null; onClose: () => void
}) {
  return (
    <Dialog open={!!it} onOpenChange={(o) => !o && onClose()}>
      {it && (
        <DialogContent
          onCloseAutoFocus={(e) => { e.preventDefault(); anchor?.focus() }}
          className="flex max-h-[85vh] w-[min(760px,calc(100vw-2rem))] max-w-none flex-col gap-0 overflow-hidden p-0 sm:max-w-none"
          role="dialog"
          aria-modal="true"
          aria-label="条目全文"
        >
          <DialogHeader className="flex-row items-start gap-2 border-b border-border px-5 py-3.5 pr-12">
            <DialogTitle className="sr-only">条目全文</DialogTitle>
            <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
              <span className="tabular-nums">{fmtTs(it.ts).date} {fmtTs(it.ts).time.slice(0, 5)}</span>
              <SrcBadge label={SRC_LABEL[it.source] || it.source} />
              <ClsBadge cls={it.cls} label={CLS_LABEL[it.cls] || it.cls} />
              {it.tier === "street" && (
                <span title="未证实来源（股吧/TG，打分已按可信度降档）"><StreetBadge /></span>
              )}
              <ScoreBadge score={it.score} />
              {it.watch && <WatchBadge />}
              {(it.targets || []).map((t) => <TargetBadge key={t} t={t} />)}
              {(it.impacts || []).map((x, i) => (
                <ImpBadge key={i} d={x.d} guess={x.g === "guess"}
                  label={`${IMP_LABEL[x.d]}`}
                  tip={`规则先验·${x.k || ""}${x.g === "guess" ? "（预期阶段，非落地）" : ""}`} />
              ))}
            </div>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
            {it.title && (
              <h3
                className="mt-0 mb-3 text-base leading-relaxed font-semibold"
                dangerouslySetInnerHTML={{ __html: highlight(it.title, it.keywords, searchTerms(store.getState().q)) }}
              />
            )}
            <div
              className="text-sm leading-7 break-words whitespace-pre-wrap text-muted-foreground"
              dangerouslySetInnerHTML={{ __html: highlight(it.text || (it.title ? "" : (it.disp || "")), it.keywords, searchTerms(store.getState().q)) }}
            />
            {!it.text && it.title && (
              <p className="mt-3 text-xs text-muted-foreground">该来源未提供正文，可点下方原文查看。</p>
            )}
            {it.translation && (
              <div className="mt-4 rounded-r-md border-l-2 border-official bg-official/5 px-3 py-2 text-sm">
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
              <div className="mt-4 rounded-r-md border-l-2 border-official bg-official/5 px-3 py-2 text-sm text-muted-foreground">
                🤖 {it.llm.one_line}
              </div>
            )}
          </div>
          {it.url && (
            <div className="border-t border-border px-5 py-3">
              <a href={it.url} target="_blank" rel="noopener" className="text-sm text-primary underline-offset-4 hover:underline">原文 ↗</a>
            </div>
          )}
        </DialogContent>
      )}
    </Dialog>
  )
}
