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
