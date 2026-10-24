/* P4 断言：dock 显隐/切换/dock-pill 滑移、滑切手势（真触摸事件）、
 * 移动看板 Sheet、/classic+/static 退役 404、/assets 仍 200、console 零错。 */
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
    while (Date.now() - t0 < timeout) { try { if (await ev(cond)) return true } catch { } await sleep(300) }
    return false
  }
  const touch = async (type, points) => call("Input.dispatchTouchEvent", {
    type, touchPoints: points,
  })

  /* ===== 路由退役（curl 层） ===== */
  const routes = await (async () => {
    const get = async (p) => (await fetch(BASE + p, { redirect: "manual" })).status
    return { root: await get("/"), classic: await get("/classic"), staticM: await get("/static/marked.js"), assets: await get("/assets/nonexistent.js"), api: await get("/api/assets") }
  })()
  ok("/ serves SPA (200)", routes.root === 200, JSON.stringify(routes))
  ok("/classic retired (404)", routes.classic === 404, JSON.stringify(routes))
  ok("/static retired (404)", routes.staticM === 404, JSON.stringify(routes))
  ok("/assets 404 for unknown (not 500)", routes.assets === 404, JSON.stringify(routes))
  ok("/api intact (200)", routes.api === 200, JSON.stringify(routes))

  /* ===== 桌面：dock 隐藏 ===== */
  await call("Emulation.setDeviceMetricsOverride", { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false })
  await call("Page.navigate", { url: BASE + "/?day=2026-10-16" })
  await settle(`document.querySelectorAll('.feedrow').length >= 1`)
  ok("1920: dock hidden", await ev(`!document.querySelector('.tab-bar')`))
  // P4 vision P0-1 防回归：桌面左栏视图切换必须可见且可用（曾被误 hidden 全尺寸）
  ok("1920: view switch visible in left rail", await ev(`(function(){
    const sw = document.querySelector('[role=tablist][aria-label="视图切换"]')
    return !!sw && sw.offsetParent !== null && sw.querySelectorAll('button').length >= 2
  })()`))
  // 社群组（小作文扩充 20261023）：股吧/韭研公社/TG 三 chip 渲染 + 点击筛选生效。
  // 筛选行为用 guba 断言（历史日/任意环境都有数据；jiuyan 为新源，数据依赖当日采集）
  ok("1920: social group renders 3 chips", await ev(`(function(){
    const chips = Array.from(document.querySelectorAll('button')).filter(b => ['股吧','韭研公社','TG'].includes(b.textContent.trim())).map(b => b.textContent.trim())
    return JSON.stringify(chips.sort())
  })()`) === JSON.stringify(["TG", "股吧", "韭研公社"]))
  // 连跑残留治理：清筛选/视图/折叠类 localStorage 后重载，保证本轮从干净态开始
  // （残留的 srcs 选中态会让 toggle 语义翻转、feed 首行命中别的来源）
  await ev(`Object.keys(localStorage).filter(k => /src|view|panel|collapse|filter/i.test(k)).forEach(k => localStorage.removeItem(k))`)
  await call("Page.navigate", { url: BASE + "/?day=2026-10-23" })
  await settle(`document.querySelectorAll('.feedrow').length >= 1`)
  const gubaChip = `Array.from(document.querySelectorAll('button')).find(x => x.textContent.trim() === '股吧')`
  await ev(`${gubaChip}.click()`)
  const gubaOK = await settle(`(document.querySelector('.feedrow')?.textContent || '').includes('股吧')`, 8000)
  ok("social chip click filters feed (guba)", gubaOK === true)
  await ev(`${gubaChip}.click()`)
  await sleep(600)
  await ev(`(function(){ const b = [...document.querySelectorAll('[role=tablist][aria-label="视图切换"] button')].find(x => x.textContent.includes('统计')); if (b) { b.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, button:0})); b.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, button:0})); b.click(); } })()`)
  await settle(`!!document.querySelector('svg[role=img]')`, 20000)
  ok("1920: view switch click enters chart", await ev(`!!document.querySelector('svg[role=img]')`))
  await call("Page.navigate", { url: BASE + "/?day=2026-10-16" })
  await settle(`document.querySelectorAll('.feedrow').length >= 1`)

  /* ===== 390：dock 渲染 + Tab 切换 + dock-pill 滑移 ===== */
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 3, mobile: true })
  await call("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 })
  await call("Page.navigate", { url: BASE + "/?day=2026-10-16" })
  await settle(`document.querySelectorAll('.feedrow').length >= 1 && !!document.querySelector('.tab-bar')`)
  const dock = await ev(`(function(){
    const bar = document.querySelector('.tab-bar')
    const r = bar.getBoundingClientRect()
    const pill = bar.querySelector('.dock-pill')
    const cs = getComputedStyle(pill)
    return { visible: r.width > 300 && r.bottom <= window.innerHeight + 1,
             h: Math.round(r.height), pillX: cs.transform }
  })()`)
  ok("390: dock rendered at bottom", dock.visible === true && dock.h >= 55, JSON.stringify(dock))
  ok("390: dock-pill at 0% (feed)", /none|matrix\(1, 0, 0, 1, 0, 0\)/.test(dock.pillX || "none"), dock.pillX)

  // 点统计 tab → view 切换 + pill 平移 100%
  await ev(`document.querySelector('.tab-bar [data-v=chart]').click()`)
  await settle(`!!document.querySelector('svg[role=img]')`, 20000)
  const afterTab = await ev(`(function(){
    const pill = document.querySelector('.tab-bar .dock-pill')
    const active = document.querySelector('.tab-bar .dock-tabs button.active')
    return { pillX: getComputedStyle(pill).transform, activeV: active?.dataset.v,
             url: new URLSearchParams(location.search).get('view') }
  })()`)
  ok("390: dock tab switches to chart (URL + active)", afterTab.activeV === "chart" && afterTab.url === "chart", JSON.stringify(afterTab))
  ok("390: dock-pill translated 100%", /matrix\(1, 0, 0, 1, -?\d+(\.\d+)?, 0\)/.test(afterTab.pillX) && !/matrix\(1, 0, 0, 1, 0, 0\)/.test(afterTab.pillX), afterTab.pillX)

  /* ===== 滑切手势（真触摸）。手势仅 #main 区域生效——动态滚到 #main 中心 ===== */
  // 每次滑动前重算 #main 中心（视图切换后 #main 位置会变，坐标过期会导致空洞/失效）
  const swipeBy = async (dx) => {
    const pt = await ev(`(function(){
      const m = document.getElementById('main').getBoundingClientRect()
      const y = Math.min(Math.max(m.top + m.height / 2, 300), window.innerHeight - 300)
      window.scrollTo(0, window.scrollY + m.top + m.height / 2 - window.innerHeight / 2)
      return { x: Math.round(m.left + m.width / 2), y: Math.round(y) }
    })()`)
    await sleep(400)
    const startX = dx < 0 ? pt.x + 120 : pt.x - 60
    const endX = startX + dx
    // 真实滑动序列（带中间 touchMove）：touchStart→End 直连会被浏览器判为 tap
    // 合成 click（实测误开 Reader 弹层且手势不触发）
    const steps = 3
    await touch("touchStart", [{ x: startX, y: pt.y, id: 1 }])
    for (let k = 1; k < steps; k++) {
      await sleep(60)
      await touch("touchMove", [{ x: Math.round(startX + (endX - startX) * k / steps), y: pt.y, id: 1 }])
    }
    await sleep(60)
    await touch("touchEnd", [])
  }
  const activeV = () => ev(`document.querySelector('.tab-bar button.active')?.dataset.v`)

  const v1 = await activeV()   // chart（tab 点过）
  await swipeBy(+130)
  await settle(`document.querySelector('.tab-bar button.active')?.dataset.v === 'feed' && document.querySelectorAll('.feedrow').length >= 1`, 15000)
  ok("390: swipe right chart->feed", (await activeV()) === "feed", "before=" + v1 + " after=" + (await activeV()))

  const v2 = await activeV()   // feed
  await swipeBy(-180)
  await settle(`document.querySelector('.tab-bar button.active')?.dataset.v === 'chart'`, 20000)
  ok("390: swipe left feed->chart (from=" + v2 + ")", v2 === "feed" && (await activeV()) === "chart", "after=" + (await activeV()))
  // 再左滑（chart 已是末项，边缘不回绕）
  await sleep(300)
  await swipeBy(-180)
  await sleep(800)
  ok("390: no wrap-around past edge (stays chart)", (await activeV()) === "chart")

  /* ===== 移动看板 Sheet ===== */
  await ev(`document.querySelector('.tab-bar .dock-side[aria-label="信息看板"]').click()`)
  // StatsPanel 是 lazy chunk：等速览三卡与统计节都到齐
  await settle(`!!document.querySelector('[role=dialog]') && document.body.textContent.includes('今日最高分')`, 10000)
  await settle(`(document.querySelector('[role=dialog]')?.textContent || '').includes('情绪')`, 15000)
  const board = await ev(`(function(){
    const d = document.querySelector('[role=dialog]')
    const r = d.getBoundingClientRect()
    const q = d.querySelector('.quotes-grid')
    return { w: Math.round(r.width), hasBoard: d.textContent.includes('信息看板'),
             hasSections: d.textContent.includes('情绪'),
             hasQuotes: !!q, quoteCards: q ? q.children.length : 0,
             quotesFirst: !!(q && d.querySelector('.right-panel') && (q.compareDocumentPosition(d.querySelector('.right-panel')) & Node.DOCUMENT_POSITION_FOLLOWING)) }
  })()`)
  ok("390: board sheet opens with right-panel content", board.hasBoard && board.hasSections && board.w >= 300, JSON.stringify(board))
  ok("390: board sheet carries quotes grid above right-panel", board.hasQuotes && board.quoteCards > 0 && board.quotesFirst, JSON.stringify(board))
  await ev(`document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))`)
  await settle(`!document.querySelector('[role=dialog]')`, 3000)
  ok("390: board sheet Esc closes", await ev(`!document.querySelector('[role=dialog]')`))

  /* ===== body 底部让位（dock 不压内容） ===== */
  ok("390: body bottom padding for dock", await ev(`parseFloat(getComputedStyle(document.body).paddingBottom) >= 78`))

  await sleep(600)
  ok("zero console errors", errs.length === 0, errs.join(" | ").slice(0, 300))
  console.log(failed ? failed + " FAILED" : "ALL P4 CHECKS PASS")
  ws.close(); process.exit(failed ? 1 : 0)
}
main().catch(e => { console.error("SCRIPT ERROR", e); process.exit(2) })
