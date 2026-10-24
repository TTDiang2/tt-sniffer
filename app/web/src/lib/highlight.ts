/* 高亮（S1 门4 D1 口径平移）：先整串转义，再在转义文本上匹配「转义后的词」，
 * 单趟 alternation 长词优先——替换词含转义后的自身形态，不会命中已插入
 * 的 <mark> 内部字符。搜索词=蓝 mark.qterm，打分词=金 mark。 */
const ESC: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }
export function escHtml(s: unknown): string {
  return (s ?? "").toString().replace(/[&<>"]/g, (c) => ESC[c])
}
const escRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")

export function highlight(text: string, kws: string[] | null | undefined, qterms: string[] = []): string {
  const qt = qterms || []
  const terms = [...new Set([...qt, ...(kws || [])].filter(Boolean))]
  if (!terms.length) return escHtml(text)
  const map = new Map<string, boolean>()
  for (const k of terms) map.set(escHtml(k), qt.includes(k))
  const alts = [...map.keys()].sort((a, b) => b.length - a.length).map(escRe).join("|")
  return escHtml(text).replace(
    new RegExp(alts, "g"),
    (m) => (map.get(m) ? '<mark class="qterm">' : "<mark>") + m + "</mark>",
  )
}
