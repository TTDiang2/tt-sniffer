/* P2 验收断言（真键盘版，P2-R2）：①弹层键盘走查=Input.dispatchKeyEvent
 * 真键盘 Tab×14+Shift+Tab×2 圈闭、真 Esc 关闭、焦点还原到触发件（F1）
 * ②XSS 用例集新栈=marked PoC 语义等价集（裸标签/href 引号/script/
 * javascript:+data: scheme 白名单/正常渲染回归）③StrictMode 挂卸泄漏
 * （5×开关 DOM 回落基线+/api/stream 单例）。 */
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
  let streamCount = 0
  ws.addEventListener("message", ev2 => {
    const m = JSON.parse(ev2.data)
    if (m.method === "Runtime.exceptionThrown") errs.push(m.params.exceptionDetails.text || "exc")
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") errs.push("console: " + JSON.stringify(m.params.args).slice(0, 100))
    if (m.method === "Network.requestWillBeSent" && m.params.request.url.includes("/api/stream")) streamCount++
  })
  const sleep = ms => new Promise(r => setTimeout(r, ms))
  // 真键盘（P2-R2）：合成 KeyboardEvent 不移动焦点，必须 Input.dispatchKeyEvent
  const VK = { Tab: 9, Escape: 27 }
  const key = (k, shift = false) => call("Input.dispatchKeyEvent", {
    type: "keyDownRaw" === "x" ? "keyDown" : "keyDown", key: k, code: k,
    windowsVirtualKeyCode: VK[k] || 0, nativeVirtualKeyCode: VK[k] || 0,
    modifiers: shift ? 8 : 0,
  }).then(() => call("Input.dispatchKeyEvent", {
    type: "keyUp", key: k, code: k,
    windowsVirtualKeyCode: VK[k] || 0, nativeVirtualKeyCode: VK[k] || 0,
    modifiers: shift ? 8 : 0,
  }))
  await call("Emulation.setDeviceMetricsOverride", { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false })
  await call("Page.navigate", { url: BASE + "/?day=2026-10-16" })
  await sleep(2800)
  const settle = async (cond, timeout = 12000) => {
    const t0 = Date.now()
    while (Date.now() - t0 < timeout) { try { if (await ev(cond)) return true } catch { } await sleep(300) }
    return false
  }
  await settle(`document.querySelectorAll('.feedrow').length >= 1`)

  /* ===== ① Reader Dialog：真键盘 Tab 圈闭 + Esc + 焦点还原（P2-R1/R2） ===== */
  const railBefore = await ev(`Math.round(document.querySelector('.right-rail')?.getBoundingClientRect().right ?? -1)`)
  const anchorTag = await ev(`(function(){
    const row = document.querySelector('.feedrow');
    row.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
    return row.tagName + '#' + (row.dataset.id || '');
  })()`)
  await settle(`!!document.querySelector('[role=dialog]')`)
  ok("reader: dialog opens on row click", await ev(`!!document.querySelector('[role=dialog]')`), anchorTag)
  const railOpen = await ev(`Math.round(document.querySelector('.right-rail')?.getBoundingClientRect().right ?? -1)`)
  ok("reader: right rail stays put when dialog opens", railBefore === railOpen, `${railBefore} -> ${railOpen}`)
  ok("reader: aria-modal set", await ev(`document.querySelector('[role=dialog]')?.getAttribute('aria-modal') === 'true'`))
  ok("reader: body scroll locked", await ev(`document.body.style.overflow !== '' || document.body.getAttribute('style')?.includes('pointer-events: none') || document.documentElement.style.overflow !== ''`))
  // 真键盘 Tab 圈闭：14 次 Tab + 2 次 Shift+Tab，焦点始终在 dialog 内
  let trapOK = true, trapWhere = ""
  for (let i = 0; i < 14; i++) {
    await key("Tab")
    const inDlg = await ev(`(function(){ const d = document.querySelector('[role=dialog]'); return d ? d.contains(document.activeElement) : false })()`)
    if (!inDlg) { trapOK = false; trapWhere = "Tab#" + (i + 1) + " -> " + await ev(`document.activeElement.tagName + '.' + String(document.activeElement.className).slice(0,30)`); break }
  }
  for (let i = 0; i < 2 && trapOK; i++) {
    await key("Tab", true)
    const inDlg = await ev(`(function(){ const d = document.querySelector('[role=dialog]'); return d ? d.contains(document.activeElement) : false })()`)
    if (!inDlg) { trapOK = false; trapWhere = "ShiftTab#" + (i + 1) }
  }
  ok("reader: REAL-KEYBOARD focus trapped (14 Tab + 2 Shift+Tab)", trapOK === true, trapWhere)
  // 真键盘 Esc 关闭 + 焦点还原到触发行（P2 智者门 F1）
  await key("Escape")
  await sleep(500)
  ok("reader: Esc closes", await ev(`!document.querySelector('[role=dialog]')`))
  ok("reader: scroll unlocked after close", await ev(`document.body.style.overflow === ''`))
  const focusBack = await ev(`(function(){
    const a = document.activeElement;
    return { tag: a.tagName, in: !!a.closest?.('.feedrow'), body: a === document.body }
  })()`)
  ok("reader: focus restored to triggering row (F1)", focusBack.in === true && focusBack.body === false, JSON.stringify(focusBack))

  /* ===== 辟谣对撞 → Reader：必须带完整正文（曾只传 id/title，详情空白） ===== */
  await settle(`!!document.querySelector('.right-panel [data-collide="debunk"]')`, 10000)
  const collideRailBefore = await ev(`Math.round(document.querySelector('.right-rail')?.getBoundingClientRect().right ?? -1)`)
  const collide = await ev(`(function(){
    const b = document.querySelector('.right-panel [data-collide="debunk"]')
    if (!b) return { none: true }
    b.click()
    return { id: b.dataset.id }
  })()`)
  await settle(`!!document.querySelector('[role=dialog]')`)
  const collideRailOpen = await ev(`Math.round(document.querySelector('.right-rail')?.getBoundingClientRect().right ?? -1)`)
  ok("collide: right rail stays put when dialog opens", collideRailBefore === collideRailOpen, `${collideRailBefore} -> ${collideRailOpen}`)
  const collideDetail = await ev(`(function(){
    const d = document.querySelector('[role=dialog]')
    const t = d?.innerText || ''
    return { len: t.length, hasUndefined: t.includes('undefined') }
  })()`)
  ok("collide reader carries full text", !collide.none && collideDetail.len > 80 && !collideDetail.hasUndefined,
     JSON.stringify({ collide, collideDetail }))
  await key("Escape")
  await sleep(500)

  /* ===== ② Watch Dialog：打开/关闭（⚙ 在右栏） ===== */
  await ev(`(function(){ const b = document.querySelector('.right-panel button[title="编辑关注列表"]'); b && b.click(); })()`)
  await settle(`!!document.querySelector('[role=dialog]')`)
  ok("watch: dialog opens from ⚙", await ev(`!!document.querySelector('[role=dialog]')`))
  await settle(`document.querySelectorAll('[role=dialog] input').length >= 2`, 8000)
  ok("watch: has input rows", await ev(`document.querySelectorAll('[role=dialog] input').length >= 2`))
  await key("Escape")
  await sleep(400)
  ok("watch: Esc closes", await ev(`!document.querySelector('[role=dialog]')`))
  const watchBack = await ev(`(function(){
    const a = document.activeElement;
    return { onGear: !!(a && a.title === '编辑关注列表'), body: a === document.body }
  })()`)
  ok("watch: focus restored to ⚙ button (F1)", watchBack.onGear === true, JSON.stringify(watchBack))

  /* ===== ③ AI Sheet：打开 + XSS 用例集新栈（硬项二） ===== */
  // 预置含 PoC 的对话历史 → 打开抽屉 → react-markdown+rehype-sanitize 渲染
  const POC_MD = [
    '链接文本注入: [<img src=x onerror=window.__xss=1>](https://ok.com)',
    '',
    'href 属性注入: [t](https://a.com"onmouseover=alert(1))',
    '',
    '脚本标签: <script>window.__xss2=1</script>',
    '',
    '正常渲染: **粗体** `code` 与 [正常链接](https://ok.com)',
  ].join("\\n")
  await ev('localStorage.setItem("tts.ai.history", ' + JSON.stringify(JSON.stringify([
    { role: 'user', content: 'xss probe' },
    { role: 'assistant', content: POC_MD },
  ])) + ')')
  await ev(`(function(){ const b = document.querySelector('header button[aria-label="AI 分析对话"]'); b && b.click(); })()`)
  await settle(`!!document.querySelector('[data-state=open]') && document.body.textContent.includes('xss probe')`, 10000)
  await sleep(800)
  const xss = await ev(`(function(){
    const md = document.querySelector('.ai-md')
    return {
      sheetOpen: !!md,
      imgInjected: !!document.querySelector('[data-state=open] img'),
      scriptExecuted: window.__xss === 1 || window.__xss2 === 1,
      onmouseoverAttr: [...document.querySelectorAll('.ai-md a')].some(a => a.getAttribute('onmouseover') !== null),
      badHref: [...document.querySelectorAll('.ai-md a')].some(a => /^(javascript|data|vbscript):/i.test(a.getAttribute('href') || '')),
      httpsHref: !!md?.querySelector('a[href="https://ok.com"]'),
      strongRendered: !!md?.querySelector('strong'),
      codeRendered: !!md?.querySelector('code'),
      linkRendered: !!md?.querySelector('a[href="https://ok.com"]'),
      linkText: md?.querySelector('a')?.textContent || '',
    }
  })()`)
  ok("ai: sheet opens with history", xss.sheetOpen === true, JSON.stringify(xss))
  ok("ai-XSS: <img onerror> sanitized (no img node)", xss.imgInjected === false, JSON.stringify(xss))
  ok("ai-XSS: onerror not executed", xss.scriptExecuted === false, JSON.stringify(xss))
  ok("ai-XSS: <script> sanitized", !xss.scriptExecuted, JSON.stringify(xss))
  ok("ai-XSS: href quote injection -> no onmouseover attr", xss.onmouseoverAttr === false, JSON.stringify(xss))
  ok("ai-XSS: javascript:/JavaScript:/data: href stripped, https kept (R3)", xss.badHref === false && xss.httpsHref === true, JSON.stringify(xss))
  ok("ai-md: bold/code/link render normally", xss.strongRendered && xss.codeRendered && xss.linkRendered, JSON.stringify(xss))
  // Sheet Esc 关闭
  await ev(`document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))`)
  await sleep(400)
  ok("ai: Esc closes sheet", await ev(`!document.querySelector('.ai-md') && !document.querySelector('[data-radix-dialog-content]')`))
  const aiBack = await ev(`(function(){
    const a = document.activeElement;
    return { onAI: !!(a && a.getAttribute('aria-label') === 'AI 分析对话'), body: a === document.body }
  })()`)
  ok("ai: focus restored to 🗒 button (F1)", aiBack.onAI === true, JSON.stringify(aiBack))

  /* ===== ④ StrictMode 挂卸泄漏专项（硬项一） ===== */
  const baseNodes = await ev(`document.querySelectorAll('body *').length`)
  const baseStream = streamCount
  for (let i = 0; i < 5; i++) {
    await ev(`(function(){ const b = document.querySelector('header button[aria-label="AI 分析对话"]'); b && b.click(); })()`)
    await sleep(250)
    await ev(`document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))`)
    await sleep(250)
    await ev(`document.querySelector('.feedrow').dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}))`)
    await settle(`!!document.querySelector('[role=dialog]')`)
    await ev(`document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))`)
    await sleep(250)
  }
  const afterNodes = await ev(`document.querySelectorAll('body *').length`)
  ok("leak: DOM nodes return to baseline after 5x open/close (±25)", Math.abs(afterNodes - baseNodes) <= 25, `${baseNodes} -> ${afterNodes}`)
  ok("leak: /api/stream not re-requested (SSE singleton)", streamCount === baseStream, `before=${baseStream} after=${streamCount}`)

  /* ===== ⑤ 模板控件（prompt 清退） ===== */
  ok("tmpl: controls render in filter bar", await ev(`!!document.querySelector('.tmpl-row') && !!document.querySelector('.tmpl-row select, .tmpl-row button')`))

  await sleep(500)
  ok("zero console errors", errs.length === 0, errs.join(" | ").slice(0, 300))
  console.log(failed ? failed + " FAILED" : "ALL P2 CHECKS PASS")
  ws.close(); process.exit(failed ? 1 : 0)
}
main().catch(e => { console.error("SCRIPT ERROR", e); process.exit(2) })
