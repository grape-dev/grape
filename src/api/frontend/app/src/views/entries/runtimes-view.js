// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/runtimes-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static-vue/assets/' + f + '?v=62'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import RuntimesView from '../RuntimesView.vue'

// Batch 11 (runtimes): legacy shell contract —
//   renderRuntimes(data?) : app.js nav('runtimes') 调 _loadRuntimesData()；无参调用=组件自加载
// Batch 11 note: the typeof-guarded bridge for old dashboard.js was retired in
// batch 12 — DashboardView.vue owns its scene-tags cache internally.
let _app = null
function mount(props) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) { try { _app.unmount() } catch (e) {} _app = null }
  el.innerHTML = ''
  _app = createApp(RuntimesView, props || {})
  _app.mount(el)
}
window.renderRuntimes = function () {
  mount()
}
window.renderRuntimesLegacy = window.renderRuntimes
