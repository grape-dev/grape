// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/observability-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static/js/assets/' + f + '?v=5'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import ObservabilityView from '../ObservabilityView.vue'

// Batch 13 (observability): legacy shell contract —
//   renderObservability(section): app.js nav('observability', param) — param = 分区
//     (overview / devices / inspect)，深链 #observability/<section>
// Component exposes window.stopObsAuto (app.js leave-page typeof guard).
// Uses vendored global echarts (static/vendor) + CDN mermaid (lazy-loaded).
let _app = null
function mount(props) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) _app.unmount()
  _app = createApp(ObservabilityView, props || {})
  _app.mount(el)
}
window.renderObservability = function (section) {
  mount({ initialSection: section || 'overview' })
}
