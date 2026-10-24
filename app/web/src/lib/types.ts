/* 后端 /api 契约类型（P1 消费面：items/days/quotes/assets/stats/stream） */

export interface Imp {
  a: string            // 资产 key
  d: "bull" | "bear" | "rel"
  g?: "guess"          // 预期阶段（虚线框）
  k?: string           // 规则词
}

export interface Item {
  id: string
  source: string
  ts: string           // ISO
  title: string
  text: string
  disp?: string
  url: string
  cls: "rumor" | "debunk" | "official" | "noise"
  score: number
  keywords: string[]
  targets: string[]
  watch?: boolean
  tier?: "street" | "authoritative"
  impacts?: Imp[]
  llm?: { one_line: string; direction: string; confidence: number | string; is_rumor: boolean } | null
  translation?: { title_zh: string; summary_zh: string } | null
  group_key?: string
  group_size?: number
  group_sources?: string[]
  group_prev?: number
  group_members?: Item[]
  group_more?: number
  cluster_size?: number
}

export interface FeedResp {
  items: Item[]
  total: number
  total_raw?: number
}

export interface DayCount { day: string; rumor: number; debunk: number; total: number }

export interface Quote {
  key?: string
  name: string
  price: number | string
  chg_pct: number | null
  updated?: string
}

export interface AssetDef {
  a: string
  name: string
  icon: string
  bucket: string
}

export const CLS_LABEL: Record<string, string> = {
  rumor: "传闻", debunk: "辟谣", official: "官方", noise: "快讯",
}

export const SRC_LABEL: Record<string, string> = {
  cls: "财联社", em: "东财", sina: "新浪", ths: "同花顺", wscn: "华尔街见闻",
  anomaly: "盘口异动", ztpool: "涨停池", guba: "股吧", jiuyan: "韭研公社", tg: "TG", crypto_rss: "加密 RSS",
  bbg_markets: "彭博市场", bbg_econ: "彭博经济", bbg_politics: "彭博政治",
  bbg_tech: "彭博科技", bbg_google: "彭博/谷歌",
  wsj_markets: "WSJ 市场", wsj_world: "WSJ 国际", wsj_business: "WSJ 商业",
  wsj_tech: "WSJ 科技", marketwatch: "MarketWatch",
  reuters: "路透", ap: "美联社", ft: "FT", cnbc: "CNBC",
  google_news: "谷歌新闻", polymarket: "Polymarket",
}

export const IMP_LABEL: Record<string, string> = { bull: "利多", bear: "利空", rel: "相关" }
