// provider 前缀剥离的统一入口（前端侧唯一实现）。
//
// 约定（与后端 src/kernel/repo_key.py 的 from_repo_key 同口径）：
//   DB/内部：repo_key = "<provider>:<owner>/<repo>"；外部/UI/URL：一律裸的 "<owner>/<repo>"。
//
// 运行时优先委托 shell 注入的 window.stripRepoPrefix（前缀集由平台目录驱动，
// gitee/gitlab 等新平台自动覆盖）；chunk 被单测/独立装载而壳未就绪时，用同口径
// 的通用正则兜底。
//
// 禁止再写死 `^(gitcode|github):` 这类平台枚举——gitee/gitlab 仓的前缀会漏剥，
// 前缀被写进 currentRepo.fullName 后，工作台顶栏「当前项目」会显示
// “gitee:owner/repo”（刷新时才被仓库清单重新解析掉）。
export function stripRepoPrefix(raw) {
  const s = String(raw || '')
  const fn = typeof globalThis !== 'undefined' ? globalThis.stripRepoPrefix : null
  if (typeof fn === 'function') {
    try {
      return fn(s)
    } catch (e) {
      /* 壳实现异常：走下面的通用兜底，不让展示层挂掉 */
    }
  }
  // 与 shell/js/app.js stripRepoPrefix 完全同口径：仓库全称里不会出现 ':'
  const i = s.indexOf(':')
  if (i > 0 && /^[a-z][a-z0-9_-]*$/.test(s.slice(0, i)) && s.slice(i + 1).indexOf('/') > 0) {
    return s.slice(i + 1)
  }
  return s
}
