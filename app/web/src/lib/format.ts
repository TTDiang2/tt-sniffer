/* 数字/日期格式化：P1 规格变更——行情数字右对齐+千分位+固定小数位（评审④） */

/** 价格：千分位 + 固定 2 位小数（4192.8→4,192.80；26140.03→26,140.03） */
export function fmtPrice(v: number | string): string {
  const n = typeof v === "string" ? parseFloat(v) : v
  if (!Number.isFinite(n)) return String(v)
  return n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/** 涨跌幅：符号 + 固定 2 位（+1.23% / -0.50% / 0.00%） */
export function fmtPct(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "–"
  const sign = v > 0 ? "+" : ""
  return sign + v.toFixed(2) + "%"
}

export function todayStr(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`
}

export function dayOffset(day: string, n: number): string {
  const d = new Date(day + "T00:00:00")
  d.setDate(d.getDate() + n)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`
}

/** ts ISO → "MM-DD HH:mm"（账本行 meta） */
export function fmtTs(ts: string): { date: string; time: string } {
  return {
    date: (ts || "").slice(5, 10),
    time: (ts || "").slice(11, 19),
  }
}

/** 搜索词：按中英文逗号切分（与后端 _where AND 切分同口径） */
export function searchTerms(q: string): string[] {
  return (q || "").split(/[,，]/).map((s) => s.trim()).filter(Boolean)
}
