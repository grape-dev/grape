// Issue #3734：任务看板轮询决策纯函数（Vue 侧消费，零 DOM 依赖便于单测）。
// 背景：会话失效（401）后轮询仍在 30s 周期裸发请求（单日 2.8 万条 401），
// 这里把「本轮请求结果 → 轮询下一步动作」的裁决收敛成纯函数：
//   401 / 会话已失效 → stop（立即停机，等重新登录）
//   非 401 连续失败达阈值 → degrade（降频 60s，避免故障时每 30s 刷屏）
//   其余 → continue（保持常规 30s 节奏）

export const POLL_INTERVAL_MS = 30000
export const POLL_DEGRADE_INTERVAL_MS = 60000
export const POLL_FAILURE_LIMIT = 3

/** 新建轮询守卫状态：失败计数 + 会话失效标记。 */
export function createPollGuardState() {
  return { failures: 0, sessionExpired: false }
}

/** 复位守卫状态：任一次成功（或重新登录后重启轮询）即回到常规节奏。 */
export function resetPollGuard() {
  return createPollGuardState()
}

/**
 * 本轮结果 → 下一步动作。
 * state.status：本轮请求的 HTTP 状态（网络错误可为 undefined）。
 * state.failures：连续失败次数（本轮失败后已累计的次数）。
 * state.sessionExpired：是否已处于会话失效态。
 * 返回 'continue' | 'degrade' | 'stop'。
 */
export function nextPollAction(state) {
  if (!state) return 'continue'
  if (state.sessionExpired) return 'stop'
  if (state.status === 401 || state.status === '401') return 'stop'
  const failures = Number(state.failures) || 0
  if (failures >= POLL_FAILURE_LIMIT) return 'degrade'
  return 'continue'
}

/** 动作 → 下一轮定时器间隔（ms）。degrade 退避 60s，其余保持 30s。 */
export function pollIntervalFor(action) {
  return action === 'degrade' ? POLL_DEGRADE_INTERVAL_MS : POLL_INTERVAL_MS
}

// ── Issue #3812：会话感知轮询器（高频轮询视图复用）──────────────────────────
// 背景：#3734 只收口了任务看板（30s）。事件详情页（5s）、任务日志弹窗（5s）、
// Autopilot 执行日志（2s/3s）仍是裸 setInterval——会话失效后它们以更高节奏无限
// 打 401，且 catch 静默吞错，用户只看到「登录页已切换但后台还在请求」。
// 这里把「定时器唯一性 + 401 停机 + 非 401 连续失败降频 + 会话事件订阅」收敛成
// 一个可复用封装，视图只提供 tick 与停机/恢复回调，不再各写一套。
export const SESSION_EXPIRED_EVENT = 'grape:session-expired'
export const SESSION_RESTORED_EVENT = 'grape:session-restored'

/**
 * 缺省降频倍数（PR #2157 复审 C3 数值证据门）：无显式 degradeIntervalMs 时按 base × 3 放大。
 *
 * 依据：非 401 连续失败一次即说明后端/网络异常，3 倍是「还能自愈」与「少打扰后端」的
 * 折中（5s→15s、2s→6s、3s→9s）；再大则恢复感知超过 10s 量级，再小则几乎不减负。
 * 401 速率与轮询命中率未实测 → <undecided: 401 速率 / 轮询命中率实测>。
 */
export const DEGRADE_FACTOR = 3

/** 高频轮询的降频步长：显式值优先（5s→15s / 2s→10s / 3s→10s），缺省按 base × DEGRADE_FACTOR。 */
export function degradeIntervalFor(baseMs, degradeMs) {
  const base = Number(baseMs) || POLL_INTERVAL_MS
  const coded = Number(degradeMs) || 0
  if (coded > base) return coded
  return base * DEGRADE_FACTOR
}

function _noop() {}

/**
 * 建一个会话感知轮询器。
 * options:
 *   intervalMs         常规节奏（缺省 30s）
 *   degradeIntervalMs  降频节奏（缺省 base*3）
 *   tick()             每轮动作（自行发请求并在结果回调里调 noteSuccess/noteError）
 *   onSessionExpired(err) 停机回调（展示横幅、收敛在途请求）
 *   onSessionRestored()   会话恢复回调（视图自行决定是否重启）
 *   window             注入 window（默认全局，便于单测传假对象）
 *   subscribe          是否订阅 grape:session-expired / grape:session-restored（缺省 true）
 * 返回：{ start, stop, noteSuccess, noteError, dispose, isRunning, isExpired,
 *        failureCount, currentInterval }
 */
export function createSessionPoller(options) {
  const o = options || {}
  const baseIntervalMs = Number(o.intervalMs) || POLL_INTERVAL_MS
  const degradeMs = degradeIntervalFor(baseIntervalMs, o.degradeIntervalMs)
  const tick = typeof o.tick === 'function' ? o.tick : _noop
  const onExpired = typeof o.onSessionExpired === 'function' ? o.onSessionExpired : _noop
  const onRestored = typeof o.onSessionRestored === 'function' ? o.onSessionRestored : _noop
  const win =
    o.window && typeof o.window.addEventListener === 'function'
      ? o.window
      : typeof window !== 'undefined'
        ? window
        : null

  let timer = null
  let failures = 0
  let intervalMs = baseIntervalMs
  let expired = false
  let listening = false
  let disposed = false

  function clearTimer() {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }
  function arm() {
    clearTimer() // 先清后挂：任何路径都不会留下两个定时器（双跑 = 双倍 401）
    if (disposed || expired) return
    timer = setInterval(function () {
      if (disposed || expired) return
      tick()
    }, intervalMs)
  }
  function onExpiredEvent() {
    if (disposed || expired) return
    expired = true
    clearTimer()
    onExpired(null)
  }
  function onRestoredEvent() {
    if (disposed || !expired) return
    expired = false
    failures = 0
    intervalMs = baseIntervalMs
    onRestored()
  }
  function listen() {
    if (listening || disposed || o.subscribe === false || !win) return
    listening = true
    win.addEventListener(SESSION_EXPIRED_EVENT, onExpiredEvent)
    win.addEventListener(SESSION_RESTORED_EVENT, onRestoredEvent)
  }
  function unlisten() {
    if (!listening || !win) return
    listening = false
    win.removeEventListener(SESSION_EXPIRED_EVENT, onExpiredEvent)
    win.removeEventListener(SESSION_RESTORED_EVENT, onRestoredEvent)
  }

  function start() {
    if (disposed) return
    failures = 0
    intervalMs = baseIntervalMs
    expired = false
    listen()
    arm()
  }
  function stop() {
    clearTimer()
  }
  function noteSuccess() {
    failures = 0
    if (intervalMs !== baseIntervalMs) {
      intervalMs = baseIntervalMs
      if (timer !== null) arm()
    }
  }
  function noteError(err) {
    if (disposed || expired) return 'stop'
    failures += 1
    const action = nextPollAction({
      status: err && err.status,
      failures: failures,
      sessionExpired: expired,
    })
    if (action === 'stop') {
      expired = true
      clearTimer()
      onExpired(err)
      return 'stop'
    }
    if (action === 'degrade' && intervalMs !== degradeMs) {
      intervalMs = degradeMs
      if (timer !== null) arm()
    }
    return action
  }
  function dispose() {
    clearTimer()
    unlisten()
    disposed = true
    expired = false
    failures = 0
    intervalMs = baseIntervalMs
  }

  return {
    start: start,
    stop: stop,
    noteSuccess: noteSuccess,
    noteError: noteError,
    dispose: dispose,
    isRunning: function () {
      return timer !== null
    },
    isExpired: function () {
      return expired
    },
    failureCount: function () {
      return failures
    },
    currentInterval: function () {
      return intervalMs
    },
  }
}
