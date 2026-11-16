// Issue #2649：看板整刷回合的纯函数（Vue 侧共用）。
// 背景：整刷若「先清空再异步重拉」，确认完成→返回看板会出现中间态空板；
// 这里只负责回合状态与提交数据，DOM/响应式交给调用方，便于单测。

/** 帧身份：任一维度变化即视为换了数据子集，旧帧不得复用。 */
export function boardFrameKey(parts) {
  return (parts || []).map((p) => (p === null || p === undefined ? '' : String(p))).join('|')
}

/** 新建一个整刷回合：pending = 待收敛的列数。 */
export function createBoardRound(cursorKeys) {
  return {
    pending: (cursorKeys || []).length,
    failed: false,
    error: null,
    aborted: false,
    items: {},
  }
}

/** 回合收敛：pending 归零即返回 true（调用方据此提交）。 */
export function settleBoardRound(round) {
  if (!round) return false
  round.pending -= 1
  return round.pending <= 0
}

/**
 * 按 id 跨列聚合（先到先得），供整刷一次性原子替换使用。
 * 返回 {list, byId}；byId 供选中项/详情就地重绑定。
 */
export function aggregateBoardTasks(lists) {
  // Issue #2649（QA 第 2 轮）：无原型索引——任务 id 若为 "__proto__"，普通对象会
  // 把 Object.assign 写到 Object.prototype 上；null 原型对象让任意 id 都是普通键。
  const byId = Object.create(null)
  const list = []
  ;(lists || []).forEach((items) => {
    ;(items || []).forEach((t) => {
      if (!t || t.id === undefined || t.id === null) return
      if (byId[t.id]) Object.assign(byId[t.id], t)
      else {
        byId[t.id] = t
        list.push(t)
      }
    })
  })
  return { list, byId }
}

/** 回合提交裁决：ok=替换数据 / keep=保留旧帧并提示 / drop=直接丢弃（已作废或不在看板）。 */
export function decideBoardRound(round, opts) {
  const o = opts || {}
  if (!round || round.aborted) return { action: 'drop', reason: 'aborted' }
  if (!o.inBoardView) return { action: 'drop', reason: 'left_view' }
  if (round.failed) {
    return o.hasOldFrame
      ? { action: 'keep', reason: 'failed' }
      : { action: 'error', reason: 'failed_no_frame' }
  }
  return { action: 'commit', reason: '' }
}
