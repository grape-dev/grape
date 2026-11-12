// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/dashboard-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static/js/assets/' + f + '?v=25'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import DashboardView from '../DashboardView.vue'

// Batch 12 (dashboard): legacy shell contract —
//   renderDashboard(data)          : app.js nav('dash') loads data then injects
//   renderSceneDetailLocal(sceneId): app.js nav('scene-detail-local', id) — self-loading
// Component keeps window._dashboardEditing in sync (app.js visibility guard reads it).
// Batch 11's runtimes bridge for dashboard (_sceneDisplayName etc.) is now retired:
// this view owns its scene-tags cache internally.
let _app = null
function mount(props) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) _app.unmount()
  _app = createApp(DashboardView, props || {})
  _app.mount(el)
}
window.renderDashboard = function (data) {
  mount({ initialData: data })
}
window.renderSceneDetailLocal = function (sceneId) {
  mount({ initialDetailId: sceneId })
}
