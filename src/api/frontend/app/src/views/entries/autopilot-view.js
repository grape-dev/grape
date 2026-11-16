import { createApp } from 'vue'
import AutopilotView from '../AutopilotView.vue'

// Batch 10 (autopilot): legacy shell contract —
//   renderAutopilot()          : app.js nav('autopilot') — list view
//   renderAutopilotDetail(id)  : app.js nav('autopilot-detail', id) — detail view
// Component reuses window.openTaskLog (batch 8 event-view) for exec-log buttons
// and window._cfNameError/_cfComputeIsSecret (app.js) for custom-field validation.
let _app = null
function mount(props) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) { try { _app.unmount() } catch (e) {} _app = null }
  el.innerHTML = ''
  _app = createApp(AutopilotView, props || {})
  _app.mount(el)
}
window.renderAutopilot = function () {
  mount()
}
window.renderAutopilotDetail = function (id) {
  mount({ initialDetailId: id })
}
