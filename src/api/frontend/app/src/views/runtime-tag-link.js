/* 引擎「角色标签」联动规则（Issue #3396）
 *
 * 用户 2026-11-20 裁定：
 *   1) 勾「全能」→ 后面列出的角色全部勾上；
 *   2) 后面列出的角色全部勾上 → 自动勾「全能」；
 *   3) 任一把已勾的角色取消 → 「全能」自动取消；
 *   4) 保存时若「全能」成立，只下发 ['all']。
 *
 * 关键点（2026-11-20 追问补充）：判定基准是「当前可勾的角色集合」而不是固定六档。
 * 场景过滤会把某些角色从选项里拿掉（例如博客开发场景没有 coding），此时：
 *   · 「全能」= 当前可见角色全勾 —— 否则永远点不亮；
 *   · 点「全能」只勾当前可见的那些，不会凭空显示不存在的档位。
 *
 * 本模块是纯函数（无 Vue 依赖），供 RuntimesView.vue 引用，
 * 并由 tests/js/test_issue_3396_engine_tag_linkage.js 直接执行做行为回归，
 * 避免「页面逻辑又被谁改回互斥/丢联动」却没人发现。
 */

export const ALL_ROLES = ['vod', 'issue', 'coding', 'qa', 'sre', 'chat']

/** 只保留角色档（滤掉 engine:/scene:/scope: 派生行与 'all'） */
export function normalizeRoles(tags) {
  return (tags || []).filter((x) => ALL_ROLES.indexOf(x) >= 0)
}

/** 后端标签 → 编辑器模型：'all' 展开成完整角色集合（UI 上全勾 + 全能勾） */
export function initialRoles(agentTags) {
  const raw = (agentTags || []).filter((x) => ALL_ROLES.indexOf(x) >= 0 || x === 'all')
  return raw.indexOf('all') >= 0 ? ALL_ROLES.slice() : ALL_ROLES.filter((r) => raw.indexOf(r) >= 0)
}

/** 当前可勾的角色集合（下拉/弹窗选项去掉 'all' 档） */
export function visibleRoles(options) {
  return (options || []).filter((x) => x !== 'all' && ALL_ROLES.indexOf(x) >= 0)
}

/** 「全能」是否成立：可勾集合非空，且每一个都已勾上 */
export function isAllChecked(tags, visible) {
  const vis = visibleRoles(visible)
  if (!vis.length) return false
  return vis.every((r) => (tags || []).indexOf(r) >= 0)
}

/** 单个复选框的勾选态（'all' 用派生的全选态） */
export function isChecked(tags, tag, visible) {
  if (tag === 'all') return isAllChecked(tags, visible)
  return (tags || []).indexOf(tag) >= 0
}

/**
 * 勾选联动：
 *   'all' 勾上 → 当前可见角色全勾；取消 → 全部角色清空（含未在选项里出现的）
 *   角色  勾上 → 加入集合；取消 → 移出集合（「全能」随之由 isAllChecked 派生）
 */
export function toggle(tags, tag, checked, visible) {
  const cur = normalizeRoles(tags)
  if (tag === 'all') {
    if (!checked) return []
    const vis = visibleRoles(visible)
    const out = cur.slice()
    vis.forEach((r) => {
      if (out.indexOf(r) < 0) out.push(r)
    })
    return ALL_ROLES.filter((r) => out.indexOf(r) >= 0)
  }
  const set = cur.slice()
  const i = set.indexOf(tag)
  if (checked && i < 0) set.push(tag)
  if (!checked && i >= 0) set.splice(i, 1)
  return ALL_ROLES.filter((r) => set.indexOf(r) >= 0)
}

/** 只保留「当前场景可见」的已勾角色（2026-11-24 裁定：切场景后保存 = 按当前场景收窄） */
export function selectedVisible(tags, visible) {
  const vis = visibleRoles(visible)
  return normalizeRoles(tags).filter((r) => vis.indexOf(r) >= 0)
}

/**
 * 本次保存会因场景收窄而**被移除**的角色。
 * 用途：保存前明确告知用户「这次保存将移除 qa」，而不是让它静默消失。
 */
export function droppedByScene(tags, visible) {
  // 全勾可见角色 = 全能（下发 ['all']，语义是「不受角色限制」）→ 不发生收窄
  if (isAllChecked(tags, visible)) return []
  const vis = visibleRoles(visible)
  return normalizeRoles(tags).filter((r) => vis.indexOf(r) < 0)
}

/**
 * 提交载荷：当前可见角色全勾 → 只下发 ['all']（裁定 4）；否则**只下发当前场景可见的
 * 已勾角色**。
 *
 * 2026-11-24 用户裁定（一次切场景+保存就该把角色改掉）：编辑器里看不见的角色不能还带
 * 在载荷里 —— 否则界面「5 个角色全空」却保存成功，实际库里的旧角色纹丝不动，用户以为
 * 改了其实没改。场景收窄即数据收窄；真正会移除角色的保存由 droppedByScene() 先提示确认。
 *
 * 注意：['all'] 的语义仍是「不受角色限制」，全勾可见角色即代表全能，此时不视为收窄。
 */
export function toPayload(tags, visible) {
  return isAllChecked(tags, visible) ? ['all'] : selectedVisible(tags, visible)
}