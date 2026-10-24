/* P1 几何+功能断言（CDP，Chrome 155）：三档视口无溢出、行情数字规格、
 * 账本行渲染、SSE 连接态、筛选交互。此文件同步入库（tests/）由 pytest 调用。 */
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
  const sleep = ms => new Promise(r => setTimeout(r, ms))
  const errs = []
  ws.addEventListener("message", ev2 => {
    const m = JSON.parse(ev2.data)
    if (m.method === "Runtime.exceptionThrown") errs.push(m.params.exceptionDetails.text || "exc")
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") errs.push("console: " + JSON.stringify(m.params.args).slice(0, 120))
  })
  async function setViewport(w, h, mobile) {
    await call("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: mobile ? 3 : 1, mobile: !!mobile })
    if (mobile) await call("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 })
  }
  async function nav(path) { await call("Page.navigate", { url: BASE + path }); await sleep(2600) }
  const settle = async (cond, timeout = 15000) => {
    const t0 = Date.now()
    while (Date.now() - t0 < timeout) { try { if (await ev(cond)) return true } catch { } await sleep(350) }
    return false
  }

  /* ===== 1920 桌面 ===== */
  await setViewport(1920, 1080, false)
  await nav("/?day=2026-10-16")
  await settle(`document.querySelectorAll('.feedrow').length >= 1`)
  ok("1920: feed rows render", await ev(`document.querySelectorAll('.feedrow').length`) >= 1)
  ok("1920: no horizontal overflow", await ev(`document.documentElement.scrollWidth <= document.documentElement.clientWidth`), await ev(`document.documentElement.scrollWidth + " vs " + document.documentElement.clientWidth`))
  ok("1920: left rail 300px present", await ev(`Math.abs(document.querySelector('aside[aria-label="筛选 · 搜索 · 设置"]').getBoundingClientRect().width - 300) < 2`))
  ok("1920: right panel 316px present", await ev(`Math.abs(document.querySelector('.right-rail').getBoundingClientRect().width - 316) < 2`))
  // --hh 防回归（P3 智者门 C：body>header 选择器 bug 存活三期的教训落成断言）
  const hh = await ev(`(function(){
    const h = document.querySelector('#root header') || document.querySelector('header')
    const rail = document.querySelector('aside[aria-label="筛选 · 搜索 · 设置"]')
    const v = getComputedStyle(document.documentElement).getPropertyValue('--hh').trim()
    return { set: v !== '' && Math.abs(parseFloat(v) - 48) > 1,
             val: v, hdrBottom: Math.round(h.getBoundingClientRect().bottom),
             railTop: rail ? Math.round(rail.getBoundingClientRect().top) : null }
  })()`)
  ok("1920: --hh set (not 48px fallback)", hh.set === true, JSON.stringify(hh))
  ok("1920: rail top == header bottom (+/-1)", hh.railTop !== null && Math.abs(hh.railTop - hh.hdrBottom) <= 1, JSON.stringify(hh))
  // 行情卡：等宽（尾行不拉伸）、数字右对齐、千分位
  const quotes = await ev(`(function(){
    try{
      const qs = [...document.querySelectorAll('.quotes-grid > div')]
      if(!qs.length) return {none: true}
      const ws = qs.map(q => Math.round(q.getBoundingClientRect().width))
      const eq = ws.every(w => Math.abs(w - ws[0]) < 2)
      const qp = qs[0].children[qs[0].children.length - 1]
      const ta = getComputedStyle(qp).textAlign
      const txt = qp.textContent.trim()
      const thousand = txt.indexOf(',') >= 0 && /\\.\\d\\d$/.test(txt)
      return {n: qs.length, eq, ta, txt, thousand}
    }catch(e){ return {err: String(e)} }
  })()`)
  if (quotes && quotes.err) ok("quotes: assertion probe", false, quotes.err)
  else if (!quotes || quotes.none) ok("quotes: grid cards present", false, "no quotes (源不可达?)")
  else {
    ok("quotes: cards equal width (no tail stretch)", quotes.eq, JSON.stringify(quotes))
    ok("quotes: price right-aligned", quotes.ta === "right" || quotes.ta === "end", quotes.ta)
    ok("quotes: thousands + fixed 2 decimals", quotes.thousand, quotes.txt)
  }
  // 账本行：色轨 + 徽章
  const row = await ev(`(function(){
    const r = document.querySelector('.feedrow')
    return {rail: !!r.querySelector('.rail'), badges: r.querySelectorAll('.bg-rumor,.bg-debunk,.bg-official,[data-radix-badge],span[class*=bg-]').length,
            score: !!r.textContent}
  })()`)
  ok("feed row: rail + badges render", row.rail && row.badges >= 1, JSON.stringify(row))
  // SSE 实时态
  ok("SSE: connected (实时)", await settle(`document.querySelector('header')?.textContent.includes('实时')`, 12000))
  // 筛选交互：方向=利多 → URL d=bull & 行数变化
  await ev(`(function(){
    const lb = [...document.querySelectorAll('label')].find(l => l.textContent.trim().startsWith('方向'));
    lb && lb.querySelector('button') && lb.querySelector('button').click();
  })()`)
  await sleep(700)
  await ev(`(function(){
    const items = [...document.querySelectorAll('[role=option]')].filter(x => x.textContent.includes('利多'));
    items[0] && items[0].click();
  })()`)
  await settle(`new URLSearchParams(location.search).get('d') === 'bull' && document.querySelectorAll('.feedrow').length >= 1`, 10000)
  ok("filter: direction=bull syncs URL", await ev(`new URLSearchParams(location.search).get('d')`) === "bull")
  // 资产页签
  ok("assets: toggle group renders", await ev(`document.querySelectorAll('[aria-label="资产筛选"] button, [aria-label="资产筛选"] [role=radio]').length`) >= 1)

  /* ===== 1280 ===== */
  await setViewport(1280, 800, false)
  await nav("/?day=2026-10-16")
  await settle(`document.querySelectorAll('.feedrow').length >= 1`)
  ok("1280: no horizontal overflow", await ev(`document.documentElement.scrollWidth <= document.documentElement.clientWidth`))
  ok("1280: three columns alive", await ev(`!!document.querySelector('aside[aria-label="筛选 · 搜索 · 设置"]') && !!document.querySelector('.right-panel')`))
  const m1280 = await ev(`document.getElementById('main').getBoundingClientRect().width`)
  ok("1280: main >= 600px (fixed rails, not in-flow)", m1280 >= 600, String(m1280))
  const rp1280 = await ev(`(function(){ const r = document.querySelector('.right-rail').getBoundingClientRect(); return {right: Math.round(r.right), w: Math.round(r.width)} })()`)
  ok("1280: right panel flush to viewport right (<=1284)", rp1280.right <= 1284 && rp1280.w >= 310, JSON.stringify(rp1280))

  /* ===== 390 移动（设备仿真） ===== */
  await setViewport(390, 844, true)
  await nav("/?day=2026-10-16")
  await settle(`document.querySelectorAll('.feedrow').length >= 1`)
  ok("390: no horizontal overflow", await ev(`document.documentElement.scrollWidth <= document.documentElement.clientWidth`), await ev(`document.documentElement.scrollWidth + " vs " + clientWidth`))
  const openedFilter = await ev(`(function(){ const b = document.querySelector('.dock-side[aria-label="筛选 · 搜索"]'); if (!b) return false; b.click(); return true })()`)
  await sleep(450)
  ok("390: dock opens filter drawer", openedFilter && await ev(`!!document.querySelector('[role=dialog][aria-label="筛选 · 搜索 · 设置"]')`))
  ok("390: filter drawer has search + assets", await ev(`(function(){ const d = document.querySelector('[role=dialog][aria-label="筛选 · 搜索 · 设置"]'); return !!d && !!d.querySelector('input') && !!d.querySelector('[aria-label="资产筛选"]') })()`))
  await ev(`document.querySelector('[aria-label="关闭筛选"]')?.click()`)
  await sleep(350)
  ok("390: no desktop rail at mobile", await ev(`!document.querySelector('aside[aria-label="筛选 · 搜索 · 设置"]')`))
  ok("390: main flow has no quotes grid (cards live in board sheet)", await ev(`!document.querySelector('.quotes-grid')`))
  ok("390: right panel hidden", await ev(`!document.querySelector('.right-panel')`))
  ok("390: touch targets >=34px", await ev(`(function(){
    const btns = [...document.querySelectorAll('button')].filter(b => b.offsetParent !== null)
    return btns.length === 0 || btns.every(b => b.getBoundingClientRect().height >= 34 - 2)
  })()`))

  await sleep(600)
  ok("zero console errors", errs.length === 0, errs.join(" | ").slice(0, 400))
  console.log(failed ? failed + " FAILED" : "ALL P1 CHECKS PASS")
  ws.close(); process.exit(failed ? 1 : 0)
}
main().catch(e => { console.error("SCRIPT ERROR", e); process.exit(2) })
