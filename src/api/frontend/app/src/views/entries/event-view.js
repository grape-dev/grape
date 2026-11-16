// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/event-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static-vue/assets/' + f + '?v=58'
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
// 入口契约：每次 nav('event', seq) 都必须「重新挂载」，不能只更新内部状态。
// （组件内部曾占用 window.renderEventDetail 做就地更新，导致第二次进入停在占位。）
function entryRender(data) {
  mount({ initialData: data })
}

function mount(props) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) { try { _app.unmount() } catch (e) {} _app = null }
  el.innerHTML = ''
  _app = createApp(EventView, props || {})
  _app.mount(el)
  // 挂载完成后再声明一次契约：组件 setup 里若覆盖同名函数，这里重新指回入口版
  window.renderEventDetail = entryRender
}
window.renderEventDetail = entryRender
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
