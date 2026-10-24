/* 徽章族：全部基于 shadcn Badge，语义色只做淡底/描边，不做大面积实心。 */
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import { TriangleAlert } from "lucide-react"

type Cls = "rumor" | "debunk" | "official" | "noise"

export function ClsBadge({ cls, label }: { cls: Cls | string; label: string }) {
  if (cls === "noise") return null
  return (
    <Badge variant="outline" className={cn(
      "font-medium",
      cls === "rumor" && "border-rumor/30 bg-rumor/10 text-rumor",
      cls === "debunk" && "border-debunk/30 bg-debunk/10 text-debunk",
      cls === "official" && "border-official/30 bg-official/10 text-official",
    )}>{label}</Badge>
  )
}

export function SrcBadge({ label }: { label: string }) {
  return <Badge variant="outline" className="font-normal text-muted-foreground">{label}</Badge>
}

export function ScoreBadge({ score }: { score: number }) {
  const cls =
    score >= 80 ? "border-rumor/30 bg-rumor/10 text-rumor font-semibold"
      : score >= 55 ? "border-gold/30 bg-gold/10 text-gold font-semibold"
        : score > 0 ? "border-border bg-muted text-muted-foreground font-medium"
          : "text-muted-foreground font-normal"
  return <Badge variant="outline" className={cn("tabular-nums", cls)}>{score}</Badge>
}

export function WatchBadge() {
  return <Badge variant="outline" className="border-gold/40 bg-gold/10 font-medium text-gold">关注</Badge>
}

export function StreetBadge() {
  return (
    <Badge variant="outline" className="gap-1 border-gold/40 font-normal text-gold">
      <TriangleAlert className="size-3" />
      小道
    </Badge>
  )
}

export function TargetBadge({ t }: { t: string }) {
  return <Badge variant="outline" className="border-gold/40 font-normal text-gold">{t}</Badge>
}

export function FoldBadge({ size, tip, open }: { size: number; tip: string; open: boolean }) {
  return (
    <Badge
      variant="secondary"
      className="cursor-pointer font-normal transition-colors hover:bg-secondary/70"
      title={tip}
    >
      {open ? "▾" : "▸"} {size} 条同类
    </Badge>
  )
}

export function CopyBadge({ n }: { n: number }) {
  return (
    <Badge variant="secondary" className="font-normal" title="跨源内容指纹相同">
      {n} 条转载
    </Badge>
  )
}

/* 方向徽章：空心描边（实心留给 cls），guess=预期阶段虚线框 */
export function ImpBadge({ d, label, tip, guess }: { d: string; label: string; tip: string; guess?: boolean }) {
  return (
    <Badge
      variant="outline"
      className={cn(
        "cursor-default font-normal",
        d === "bull" && "border-rumor/30 text-rumor",
        d === "bear" && "border-debunk/30 text-debunk",
        d === "rel" && "text-muted-foreground",
        guess && "border-dashed",
      )}
      title={tip}
    >
      {label}
    </Badge>
  )
}
