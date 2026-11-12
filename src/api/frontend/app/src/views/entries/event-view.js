// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/event-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static/js/assets/' + f + '?v=29'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import EventView from '../EventView.vue'

// Batch 8 (events): legacy shell contract —
//   renderEventDetail(data): app.js nav('event', param) loads then injects.
//   navEvent(seq): local re-render without full route (called from within).
// Component also exports: window.resumeConvTask (nav-back recovery),
// window._stopEventPoll (leave-page), window.openTaskLog/closeTaskLog
// (referenced by autopilot.js exec-log buttons).
let _app = null
function mount(props) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) _app.unmount()
  _app = createApp(EventView, props || {})
  _app.mount(el)
}
window.renderEventDetail = function (data) {
  mount({ initialData: data })
}
// navEvent is called from inside the view via API; expose for cross-module use
window.navEvent = function (seq) {
  // fresh mount path: load then mount
  const api = window.API
  if (!api) return
  api
    .get('/events/detail/' + seq)
    .then(function (data) {
      window.renderEventDetail(data)
      var titleEl = document.getElementById('pageTitle')
      if (titleEl) titleEl.textContent = window.I18N.t('events.page_title_prefix', { seq: seq })
    })
    .catch(function (e) {
      var status = (e && e.status) || 0
      if (status === 403) {
        window.app.showToast(window.I18N.t('events.nav_forbidden', { seq: seq }))
        return
      }
      var detail = (e && e.detail) || {}
      if (detail.max_seq && detail.max_seq > 0 && detail.max_seq !== seq)
        window.navEvent(detail.max_seq)
    })
}
