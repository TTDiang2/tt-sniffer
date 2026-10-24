/* 全局筛选状态 store：URL 即状态（深链全字段白名单校验）+ 竞态令牌 reload
   + SSE 插顶判定——旧版 index.html 状态纪律的 TS 平移（含 S0 修复口径）。
   不引状态管理库（方案 §8.5），useSyncExternalStore 消费。 */
import type { FeedResp, Item } from "@/lib/types"
import { fetchOk } from "@/lib/api"
import { todayStr } from "@/lib/format"
import { beep, ensureAudio } from "@/lib/audio"

export const PAGE = 20
export const BAND: Record<string, { min: number; max: number }> = {
  hi: { min: 80, max: -1 }, mid: { min: 55, max: 79 }, low: { min: 1, max: 54 },
}

export interface FeedState {
  day: string                   // 'today' | 'YYYY-MM-DD'
  cls: string                   // all|rumor|debunk|official|watch
  q: string
  band: string                  // all|hi|mid|low
  sort: string                  // time|score
  asset: string
  dir: string                   // all|any|bull|bear|rel（URL 可逗号组合）
  page: number
  fold: boolean
  view: string                  // feed|chart
  sound: boolean
  srcs: Set<string>
  tierOnly: boolean
  // 运行态
  items: Item[]
  total: number
  totalRaw: number
  loading: boolean
  error: string
  connOK: boolean
  pending: number
}

function loadSources(): Set<string> {
  try {
    const parsed = JSON.parse(localStorage.getItem("tts.srcs") || "[]")
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((x): x is string => typeof x === "string"))
  } catch {
    return new Set()
  }
}

let state: FeedState = {
  day: "today", cls: "all", q: "", band: "all", sort: "time",
  asset: "all", dir: "all", page: 1,
  fold: localStorage.getItem("tts.fold") !== "0",
  view: "feed",
  sound: localStorage.getItem("tts.sound") === "1",
  srcs: loadSources(),
  tierOnly: false,
  items: [], total: 0, totalRaw: 0, loading: false, error: "",
  connOK: false, pending: 0,
}

const seen = new Set<string>()
let reloadSeq = 0
const listeners = new Set<() => void>()

export function getState(): FeedState {
  return state
}
function emit() {
  for (const l of listeners) l()
}
export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export const isToday = () => state.day === "today" || state.day === todayStr()
export const curDay = () => (state.day === "today" ? todayStr() : state.day)
const totalPages = () => Math.max(1, Math.ceil(state.total / PAGE))

/* ---------- URL 同步（白名单校验照搬 S0 口径） ---------- */
export function queryString(): string {
  const p = new URLSearchParams()
  if (state.q) p.set("q", state.q)
  if (state.cls !== "all") p.set("cls", state.cls)
  if (state.srcs.size) p.set("src", [...state.srcs].join(","))
  if (state.tierOnly) p.set("tier", "street")
  if (state.band !== "all") p.set("band", state.band)
  if (state.sort !== "time") p.set("sort", state.sort)
  if (state.asset !== "all") p.set("asset", state.asset)
  if (state.dir !== "all") p.set("d", state.dir)
  if (state.page > 1) p.set("page", String(state.page))
  if (!state.fold) p.set("fold", "0")
  if (state.view !== "feed") p.set("view", state.view)
  p.set("day", state.day === "today" ? todayStr() : state.day)
  return p.toString()
}
export function syncUrl() {
  history.replaceState(null, "", location.pathname + "?" + queryString())
}
export function applyFromUrl() {
  const p = new URLSearchParams(location.search)
  const cls = p.get("cls")
  state.cls = ["all", "rumor", "debunk", "official", "watch"].includes(cls ?? "") ? cls as string : "all"
  state.q = p.get("q") || ""
  state.srcs = new Set((p.get("src") || "").split(",").filter(Boolean))
  state.tierOnly = p.get("tier") === "street"
  state.band = ["hi", "mid", "low"].includes(p.get("band") || "") ? p.get("band") as string : "all"
  state.sort = p.get("sort") === "score" ? "score" : "time"
  state.asset = /^[a-z_]+$/.test(p.get("asset") || "") ? p.get("asset") as string : "all"
  const d = p.get("d")
  state.dir = d === "any" ? "any"
    : d && d.split(",").every((x) => ["bull", "bear", "rel"].includes(x)) ? d : "all"
  const pg = parseInt(p.get("page") || "1", 10)
  state.page = Number.isFinite(pg) && pg > 0 ? pg : 1
  state.fold = p.get("fold") === "1" ? true : p.get("fold") === "0" ? false
    : localStorage.getItem("tts.fold") !== "0"
  state.view = p.get("view") === "chart" ? "chart" : "feed"
  const day = p.get("day")
  state.day = day && /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : "today"
  seen.clear()
}

/* ---------- 查询（竞态令牌 + 越界钳页） ---------- */
export function apiUrl(): string {
  const p = new URLSearchParams()
  p.set("cls", state.cls)
  if (state.cls === "watch") p.set("watch", "1")
  if (state.q) p.set("q", state.q)
  if (state.srcs.size) p.set("sources", [...state.srcs].join(","))
  if (state.tierOnly) p.set("tier", "street")
  const band = BAND[state.band]
  if (band) {
    p.set("min_score", String(band.min))
    if (band.max > 0) p.set("max_score", String(band.max))
  }
  if (state.sort !== "time") p.set("sort", state.sort)
  if (state.asset !== "all") p.set("asset", state.asset)
  if (state.dir === "any") p.set("d", "bull,bear")
  else if (state.dir !== "all") p.set("d", state.dir)
  p.set("day", curDay())
  p.set("limit", String(PAGE))
  p.set("offset", String((state.page - 1) * PAGE))
  if (state.fold) p.set("group", "1")
  return "/api/items?" + p.toString()
}

export async function reload(): Promise<void> {
  const seq = ++reloadSeq
  // 不可变更新纪律：useSyncExternalStore 以 Object.is 比较 snapshot，
  // mutate 同一对象不会触发渲染
  state = { ...state, loading: true, error: "" }
  emit()
  try {
    const d = (await (await fetchOk(apiUrl())).json()) as FeedResp
    if (seq !== reloadSeq) return
    const total = d.total || 0
    const totalRaw = d.total_raw || d.total || 0
    if (!(d.items || []).length && state.page > 1 && total > 0) {
      state = { ...state, total, totalRaw, page: Math.max(1, Math.ceil(total / PAGE)) }
      syncUrl()
      return reload()
    }
    state = {
      ...state, total, totalRaw,
      page: Math.min(state.page, Math.max(1, Math.ceil(total / PAGE))),
      items: d.items || [],
    }
    seen.clear()
    for (const it of state.items) seen.add(it.id)
    syncUrl()
  } catch (e) {
    if (seq !== reloadSeq) return
    state = { ...state, error: e instanceof Error ? e.message : String(e), items: [] }
  } finally {
    if (seq === reloadSeq) state = { ...state, loading: false }
    emit()
  }
}

/* ---------- 交互入口 ---------- */
export function setPartial(patch: Partial<FeedState>, opts: { resetPage?: boolean } = {}) {
  state = { ...state, ...patch, ...(opts.resetPage ? { page: 1 } : {}) }
  emit()
  reload()
}
export function setDay(day: string, keepPage = false) {
  state = { ...state, day, ...(keepPage ? {} : { page: 1 }) }
  emit()
  syncUrl()
  reload()
}
export function setView(v: string) {
  state = { ...state, view: v }
  emit()
  syncUrl()
}
export function clearFilters() {
  state = {
    ...state, q: "", cls: "all", band: "all", sort: "time",
    asset: "all", dir: "all", page: 1, tierOnly: false,
    srcs: new Set<string>(),
  }
  localStorage.setItem("tts.srcs", "[]")
  emit()
  reload()
}

/* ---------- SSE（App 层调用；插顶判定照搬旧版） ---------- */
export function canLiveInsert(it: Item): boolean {
  const atTop = window.scrollY < 60
  return isToday() && state.sort === "time" && state.page === 1 && atTop &&
    !state.q && !state.srcs.size && state.band === "all" && state.asset === "all" &&
    (!state.tierOnly || it.tier === "street") &&
    (state.cls === "all" || state.cls === it.cls || (state.cls === "watch" && !!it.watch)) &&
    !seen.has(it.id)
}

export function handleLiveItem(it: Item) {
  if (!canLiveInsert(it)) {
    state = { ...state, pending: state.pending + 1 }
    emit()
    return
  }
  seen.add(it.id)
  // P1-R3：fold 模式命中已折叠组——徽章 +1、成员追加、行闪（旧版行为），
  // 不插新行；跨页/未命中的旧组徽章不追更（下次 reload 自愈，同旧版）
  if (state.fold && it.group_key) {
    const idx = state.items.findIndex((r) => r.group_key === it.group_key)
    if (idx >= 0) {
      const row = state.items[idx]
      const items = state.items.slice()
      items[idx] = {
        ...row,
        group_size: (row.group_size || 1) + 1,
        group_members: [...(row.group_members || []), it],
      }
      state = { ...state, items, totalRaw: state.totalRaw + 1 }
      emit()
      return
    }
  }
  const items = [it, ...state.items]
  while (items.length > PAGE) items.pop()
  state = {
    ...state, items, total: state.total + 1,
    totalRaw: state.fold ? state.totalRaw + 1 : state.totalRaw,
  }
  emit()
}

function updateItem(id: string, patch: Partial<Item>) {
  let changed = false
  const items = state.items.map((row) => {
    if (row.id === id) {
      changed = true
      return { ...row, ...patch }
    }
    if (row.group_members?.some((m) => m.id === id)) {
      changed = true
      return { ...row, group_members: row.group_members.map((m) => (m.id === id ? { ...m, ...patch } : m)) }
    }
    return row
  })
  if (!changed) return
  state = { ...state, items }
  emit()
}

export function handleLiveLlm(data: { id: string } & NonNullable<Item["llm"]>) {
  const { id, ...llm } = data
  updateItem(id, { llm })
}

export function handleLiveTranslation(data: { id: string } & NonNullable<Item["translation"]>) {
  const { id, ...translation } = data
  updateItem(id, { translation })
}

export function setConn(ok: boolean) {
  if (state.connOK !== ok) {
    state = { ...state, connOK: ok }
    emit()
  }
}
export function clearPending() {
  state = { ...state, pending: 0 }
  emit()
}
export function toggleSound() {
  state = { ...state, sound: !state.sound }
  localStorage.setItem("tts.sound", state.sound ? "1" : "0")
  if (state.sound) { ensureAudio(); beep() }   // 点击手势=创建/解锁 AudioContext 窗口，试响一声（P1-R1）
  emit()
}

export function setFold(fold: boolean) {
  state = { ...state, fold, page: 1 }
  localStorage.setItem("tts.fold", fold ? "1" : "0")
  emit()
  reload()
}

export function toggleSrc(src: string) {
  const srcs = new Set(state.srcs)
  if (srcs.has(src)) srcs.delete(src)
  else srcs.add(src)
  localStorage.setItem("tts.srcs", JSON.stringify([...srcs]))
  state = { ...state, srcs, page: 1 }
  emit()
  reload()
}

/* 整组切换（侧栏来源分组头）：一次写入选集，只触发一次 reload */
export function setSources(next: Set<string>) {
  localStorage.setItem("tts.srcs", JSON.stringify([...next]))
  state = { ...state, srcs: next, page: 1 }
  emit()
  reload()
}

export function goPage(n: number) {
  const next = Math.max(1, Math.min(n, totalPages()))
  if (next === state.page || state.loading) return
  state = { ...state, page: next }
  emit()
  reload()
  document.getElementById("main")?.scrollIntoView({ block: "start" })
}
