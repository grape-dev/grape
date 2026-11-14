// 从 repo_key 前缀取平台——别再写「非 github 即 gitcode」的二分。
//
// 背景（2026-11-14 实锤）：托管的 provider 判定原先是
//   github: → 'github'，gitcode: → 'gitcode'，其它 → ''（空）
// gitee 仓库因此不带 provider 进 /repos/host，后端退到「按 token 可用性兜底」
// 挑到 github，用 GitHub token 去查 gitee 仓库 → 上游 404「明明我是管理员却托管失败」。
export function providerFromRepoKey(repoKey) {
  const rk = String(repoKey || '')
  const i = rk.indexOf(':')
  if (i <= 0) return ''
  const p = rk.slice(0, i)
  const rest = rk.slice(i + 1)
  // provider key 只允许 [a-z0-9_-]；且后半必须是 owner/repo（不能像 URL 那样带 //）
  if (!/^[a-z][a-z0-9_-]*$/.test(p)) return ''
  if (!rest || rest.indexOf('/') <= 0 || rest.indexOf('//') >= 0) return ''
  return p
}

// 由建仓/派生响应拼 repo_key：provider 优先，其次响应自带 repo_key，最后前缀不猜（空串）。
// 原实现 `provider === 'github' ? 'github:' : 'gitcode:'` 把 gitee/gitlab 建出来的仓库
// 都标成 gitcode（2026-11-14 实锤同款二分支），后续托管/探活全打错平台。
export function repoKeyOf(row) {
  const r = row || {}
  if (r.repo_key) return r.repo_key
  const provider = providerFromRepoKey(r.repo_key || '') || String(r.provider || '')
  const full = String(r.full_name || '')
  if (!provider || !full) return ''
  return provider + ':' + full
}

// 可一键创建的平台 = 服务端 /auth/platform-links 的 providers（关系 ∪ PAT 口径）
// ∪ 已加载仓库的 repo_key 前缀；tags 用调用方传入的取名函数（platformLabel）。
export function platformChoices(providers, repos, labelOf) {
  const seen = []
  const push = (k) => { if (k && seen.indexOf(k) < 0) seen.push(k) }
  ;(providers || []).forEach(push)
  ;(repos || []).forEach((r) => push(providerFromRepoKey((r && r.repo_key) || '')))
  return seen.map((k) => ({ key: k, name: (labelOf && labelOf(k)) || k }))
}
