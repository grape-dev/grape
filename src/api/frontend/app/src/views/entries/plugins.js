import { createApp } from 'vue'
import PluginMarketView from '../PluginMarketView.vue'
import { pluginsEnabled } from '@/lib/features'

// Issue #3169（batch：plugins）老壳契约 ——
//   renderPlugins() : app.js nav('plugins') — 插件市场页（scope 子页签+分类/搜索/排序/分页+卡片+详情/上传）
// Mounted fresh on every call, module-level state resets like the old modules did.
let _app = null
function mount(props) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) { try { _app.unmount() } catch (e) { /* 未挂载/已卸载则忽略 */ } _app = null }
  el.innerHTML = ''
  _app = createApp(PluginMarketView, props || {})
  _app.mount(el)
}
window.renderPlugins = function () {
  // Issue #3395：#plugins 深链被路由守卫漏放（旧缓存 app.js 等）时兜底回退。
  if (!pluginsEnabled()) {
    if (window.app && typeof window.app.nav === 'function') window.app.nav('dash')
    return
  }
  mount()
}
