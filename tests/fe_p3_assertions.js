/* P3 斟断言：统计视图五卡渲染/图例色块/数值刻度/矩阵色 --rumor 同源/
 * 矩阵点按切日切资产/气泡点按开 Reader（移动无 hover 依赖）/右栏统计面板
 * /view=chart 深链/console 零错。 */
const CDP_PORT = process.env.TTS_CDP_PORT || "9334"
const BASE = process.env.TTS_BASE || "http://127.0.0.1:8788"
let failed = 0
const ok = (name, cond, detail = "") => {
  if (cond) console.log("PASS " + name)
  else { failed++; console.log("FAIL " + name + " :: " + detail) }
}
async function main() {
  const targets = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json`)).json()
  const page = targets.find(t => t.type === "page")
  const ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })
  let id = 0; const pending = new Map()
  ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id) } }
  const call = (method, params = {}) => new Promise((res, rej) => {
    const i = ++id; pending.set(i, m => m.error ? rej(new Error(method)) : res(m.result))
    ws.send(JSON.stringify({ id: i, method, params }))
  })
  const ev = async (e) => (await call("Runtime.evaluate", { expression: e, awaitPromise: true, returnByValue: true })).result.value
  await call("Page.enable"); await call("Runtime.enable"); await call("Network.enable")
  await call("Network.setCacheDisabled", { cacheDisabled: true })
  const errs = []
  ws.addEventListener("message", ev2 => {
    const m = JSON.parse(ev2.data)
    if (m.method === "Runtime.exceptionThrown") errs.push(m.params.exceptionDetails.text || "exc")
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") errs.push("console: " + JSON.stringify(m.params.args).slice(0, 100))
  })
  const sleep = ms => new Promise(r => setTimeout(r, ms))
  const settle = async (cond, timeout = 15000) => {
    const t0 = Date.now()
    while (Date.now() - t0 < timeout) { try { if (await ev(cond)) return true } catch { } await sleep(350) }
    return false
  }

  await call("Emulation.setDeviceMetricsOverride", { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false })
  await call("Page.navigate", { url: BASE + "/?view=chart&day=2026-10-16" })
  await settle(`!!document.querySelector('svg[role=img][aria-label*="当日新闻点"]')`, 20000)
  await sleep(1200)

  /* ===== 五卡渲染 ===== */
  const cards = await ev(`[...document.querySelectorAll('[data-slot=card-title]')].map(h => h.textContent.trim())`)
  ok("five chart cards render", cards.length >= 5, JSON.stringify(cards))
  ok("scatter (news) svg render", await ev(`!!document.querySelector('svg[role=img][aria-label*="当日新闻点"]')`))
  ok("scatter (trail) svg render", await ev(`!!document.querySelector('svg[role=img][aria-label*="14日轨迹"]')`))
  ok("sep svg render", await ev(`!!document.querySelector('svg[role=img][aria-label*="SEP"]')`))
  ok("matrix grid render", await ev(`document.querySelectorAll('.mgrid').length >= 2`))
  ok("dotplot svg render (v3 SVG 化，原 .dgrid 表格已移除)", await ev(`!!document.querySelector('svg[role=img][aria-label*="利率观点点阵"]')`))
  // 20261023 紧急修复回归①：只设 minWidth 会被容器拉伸成巨图（缩放比必须≈1）
  ok("dotplot svg natural size (no container stretch)", await ev(`(function(){
    const svg = document.querySelector('svg[role=img][aria-label*="利率观点点阵"]')
    if (!svg) return false
    return svg.clientWidth <= svg.viewBox.baseVal.width * 1.3 + 2
  })()`))
  // 20261023 紧急修复回归②：钉死自然宽会挤在左侧留半边空（宽容器必须铺满）
  ok("dotplot fills card width (no empty right half)", await ev(`(function(){
    const svg = document.querySelector('svg[role=img][aria-label*="利率观点点阵"]')
    if (!svg) return false
    const wrap = svg.parentElement
    if (wrap.clientWidth < 600) return true   // 窄屏允许滚动，不强制铺满
    return svg.clientWidth >= wrap.clientWidth * 0.95
  })()`))
  // SEP 点阵同口径：不得被容器拉伸成巨图
  ok("sep svg natural size (no container stretch)", await ev(`(function(){
    const svg = document.querySelector('svg[role=img][aria-label*="SEP"]')
    if (!svg) return false
    return svg.clientWidth <= svg.viewBox.baseVal.width * 1.3 + 2
  })()`))

  /* ===== 20261023 紧急修复回归③：同页切日必须重新测宽 =====
     P0 场景：首屏空数据日（2026-10-17 点阵 0 cells → 组件早退、wrapper 未渲染）
     → 切到有数据日 → 若测量钩子只在挂载时跑一次，宽度永远停在 fallback，
     图表又挤在左侧留半边空。 */
  await ev(`(function(){
    const hit = [...document.querySelectorAll('[title^="10-17"]')][0]
    if (hit) hit.click()
  })()`)
  await sleep(2500)
  await ev(`(function(){
    const hit = [...document.querySelectorAll('[title^="10-16"]')][0]
    if (hit) hit.click()
  })()`)
  // 等点阵真正按新日期渲染完成（宽度随容器重测后稳定）再判定
  await settle(`(function(){
    const svg = document.querySelector('svg[role=img][aria-label*="利率观点点阵"]')
    if (!svg) return false
    const wrap = svg.parentElement
    if (wrap.clientWidth < 600) return true
    return svg.clientWidth >= wrap.clientWidth * 0.95
  })()`, 25000)
  ok("dotplot fills card width after in-page day switch", await ev(`(function(){
    const svg = document.querySelector('svg[role=img][aria-label*="利率观点点阵"]')
    if (!svg) return false
    const wrap = svg.parentElement
    if (wrap.clientWidth < 600) return true
    return svg.clientWidth >= wrap.clientWidth * 0.95
  })()`))

  /* ===== 图例色块 + 数值刻度（评审缺口补齐验收） ===== */
  ok("legend swatches present", await ev(`document.querySelectorAll('i.h-2.w-2').length >= 6`),
    String(await ev(`document.querySelectorAll('i.h-2.w-2').length`)))
  ok("scatter numeric axis ticks (-100..100)", await ev(`(function(){
    const svg = document.querySelector('svg[role=img][aria-label*="当日新闻点"]')
    const texts = [...svg.querySelectorAll('text')].map(t => t.textContent)
    return ['-100','-50','0','50','100'].every(v => texts.includes(v))
  })()`))

  /* ===== 矩阵色 --rumor 同源（P0 登记清债验收） ===== */
  const mix = await ev(`(function(){
    const cell = document.querySelector('.mgrid button[style*="color-mix"]')
    if(!cell) return {none: true}
    const m = cell.getAttribute('style').match(/color-mix\\([^)]+\\)/)
    return {style: m ? m[0] : 'NO-MATCH', bg: getComputedStyle(cell).backgroundColor}
  })()`)
  ok("matrix uses color-mix var(--rumor)/var(--debunk) (P0 debt cleared)",
     !mix.none && /var\(--(rumor|debunk)\)/.test(mix.style || ""), JSON.stringify(mix))

  /* ===== 矩阵点按切日切资产（移动端无 hover 依赖的核心交互） ===== */
  const before = await ev(`new URLSearchParams(location.search).get('day')`)
  await ev(`(function(){
    // 7 天窗口随日历滚动，「第一格」可能恰是当前日（20261022 实发）：
    // 显式挑一个日期≠当前日的格子（格子 title 以 ISO 日期开头），全命中才退化取首格
    const p = new URLSearchParams(location.search).get('day')
    const cur = p || new Date().toISOString().slice(0, 10)
    const cells = [...document.querySelectorAll('.mgrid button[style*="color-mix"]')]
    const c = cells.find(b => !b.title.startsWith(cur)) || cells[0]
    c && c.click()
  })()`)
  await settle(`document.querySelectorAll('.feedrow').length >= 1`, 15000)
  const after = await ev(`(function(){
    return { day: new URLSearchParams(location.search).get('day'),
             asset: new URLSearchParams(location.search).get('asset'),
             view: new URLSearchParams(location.search).get('view') }
  })()`)
  // view=feed 是 URL 缺省值不显式写入（queryString 只写非默认），判 UI 事实即可
  ok("matrix cell click switches day+asset+view=feed",
     after.view !== "chart" && !!after.asset && after.day !== before, JSON.stringify({ before, after }))

  /* ===== 气泡点按=旧版跳行（无 hover 依赖；行不存在时静默回 feed） ===== */
  await ev(`(function(){ const b = [...document.querySelectorAll('[role=tablist] button')].find(x => x.textContent.includes('统计')); if (b) { b.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, button:0})); b.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, button:0})); b.click(); } })()`)
  await settle(`!!document.querySelector('svg[role=img][aria-label*="当日新闻点"] circle')`, 20000)
  const bubbleJump = await ev(`(function(){
    const c = document.querySelector('svg[role=img][aria-label*="当日新闻点"] circle')
    if(!c) return {clicked: false}
    c.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}))
    return {clicked: true}
  })()`)
  await settle(`document.querySelectorAll('.feedrow').length >= 1`, 15000)
  const jumpState = await ev(`new URLSearchParams(location.search).get('view')`)
  ok("scatter bubble click jumps to feed view (no-hover)", bubbleJump.clicked === true && jumpState !== "chart", JSON.stringify({bubbleJump, jumpState}))

  /* ===== 右栏统计面板（Charts；lazy chunk + API 排队，先 settle） ===== */
  await settle(`(document.querySelector('.right-panel')?.textContent || '').includes('资产情绪')`, 20000)
  ok("right panel stats sections render", await ev(`(function(){
    const t = document.querySelector('.right-panel')?.textContent || ''
    return ['情绪','分时','来源占比','分类构成','热词'].every(k => t.includes(k))
  })()`))
  ok("recharts bars render", await ev(`document.querySelectorAll('.right-panel path.recharts-rectangle, .right-panel .recharts-bar-rectangle').length >= 10`))

  /* ===== 390 移动（chart 视图） ===== */
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 3, mobile: true })
  await call("Page.navigate", { url: BASE + "/?view=chart&day=2026-10-16" })
  await settle(`document.querySelectorAll('svg[role=img]').length >= 2`, 20000)
  await sleep(1500)
  ok("390 chart view: no horizontal page overflow", await ev(`document.documentElement.scrollWidth <= document.documentElement.clientWidth + 40`),
    await ev(`document.documentElement.scrollWidth + ' vs ' + document.documentElement.clientWidth`))

  await sleep(600)
  ok("zero console errors", errs.length === 0, errs.join(" | ").slice(0, 300))
  console.log(failed ? failed + " FAILED" : "ALL P3 CHECKS PASS")
  ws.close(); process.exit(failed ? 1 : 0)
}
main().catch(e => { console.error("SCRIPT ERROR", e); process.exit(2) })
