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
  if (_app) { try { _app.unmount() } catch (e) {} _app = null }
  el.innerHTML = ''
  _app = createApp(DashboardView, props || {})
  _app.mount(el)
}
window.renderDashboard = function (data) {
  mount({ initialData: data })
}
window.renderSceneDetailLocal = function (sceneId) {
  mount({ initialDetailId: sceneId })
}
