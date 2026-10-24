/* 告警声音（S0 口径 + 智者门 P1-R1 补全）：模块加载时按持久化 sound 预建
   AudioContext；toggleSound 开启的黄金手势窗口 ensureAudio + resume + 试响
   ——旧版三行为完整平移，防「开启后仍永久静默直到刷新」。 */
let audio: AudioContext | null = null

export function ensureAudio(): AudioContext | null {
  if (!audio) {
    try {
      audio = new (window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)()
    } catch {
      audio = null
    }
  }
  if (audio && audio.state === "suspended") audio.resume().catch(() => {})
  return audio
}

export function beep(): void {
  const ac = audio || ensureAudio()
  if (!ac) return
  try {
    if (ac.state === "suspended") ac.resume().catch(() => {})
    const o = ac.createOscillator()
    const g = ac.createGain()
    o.connect(g)
    g.connect(ac.destination)
    o.frequency.value = 880
    g.gain.setValueAtTime(0.12, ac.currentTime)
    g.gain.exponentialRampToValueAtTime(0.001, ac.currentTime + 0.35)
    o.start()
    o.stop(ac.currentTime + 0.36)
  } catch {
    /* AudioContext 不可用——静默，不炸主链路 */
  }
}
