// Issue #3395：前端功能开关统一读取入口。
// 唯一数据源 = 服务端 shell HTML 注入的 window.__FEATURES__
// （见 src/api/frontend_flags.py）。各视图禁止自行解析 env / cookie。
export function featureFlags() {
  const raw = typeof globalThis !== 'undefined' ? globalThis.__FEATURES__ : null
  return raw && typeof raw === 'object' ? raw : {}
}

export function isFeatureEnabled(name, fallback = true) {
  const value = featureFlags()[name]
  return typeof value === 'boolean' ? value : fallback
}

export function pluginsEnabled() {
  return isFeatureEnabled('plugins', true)
}