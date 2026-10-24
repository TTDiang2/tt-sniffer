/* fetch 封装：8s 超时 + 一次重试（V0.2 能力平移），AbortError 转人话 */
export async function fetchOk(url: string, tries = 2, init?: RequestInit): Promise<Response> {
  for (let i = 0; i < tries; i++) {
    const ac = new AbortController()
    const t = setTimeout(() => ac.abort(), 8000)
    try {
      const r = await fetch(url, { ...(init || {}), signal: ac.signal })
      if (!r.ok) throw new Error("HTTP " + r.status)
      return r
    } catch (e) {
      if (i === tries - 1) {
        if (e instanceof DOMException && e.name === "AbortError") {
          throw new Error("请求超时（服务器响应超过 8 秒）")
        }
        throw e
      }
    } finally {
      clearTimeout(t)
    }
  }
  throw new Error("unreachable")
}

export async function fetchJson<T>(url: string, tries = 2): Promise<T> {
  return (await fetchOk(url, tries)).json() as Promise<T>
}
