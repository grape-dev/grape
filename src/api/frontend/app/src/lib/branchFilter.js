// Issue #4372：分支选择框的前缀过滤（忽略大小写）。
// 分支列表由 loadBranches 一次性加载，这里只做本地纯函数过滤，不触达后端。

/** 归一化输入：去除首尾空白并转小写，空值归一为空串。 */
function normalize(value) {
  return String(value === null || value === undefined ? '' : value)
    .trim()
    .toLowerCase()
}

/**
 * 按前缀过滤分支列表（忽略大小写）。
 * - keyword 为空 → 返回全部（保持原顺序）；
 * - 分支名以 keyword 开头即命中（忽略大小写）；
 * - 坏数据（非数组/缺 name）跳过，不抛错。
 */
export function filterBranchesByPrefix(list, keyword) {
  const items = Array.isArray(list) ? list : []
  const prefix = normalize(keyword)
  if (!prefix) return items.slice()
  return items.filter((br) => {
    const name = br && br.name !== undefined && br.name !== null ? String(br.name) : ''
    return normalize(name).startsWith(prefix)
  })
}