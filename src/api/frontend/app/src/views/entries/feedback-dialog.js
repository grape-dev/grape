// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/feedback-dialog.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static-vue/assets/' + f + '?v=45'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import FeedbackDialog from '../FeedbackDialog.vue'

// Batch 16 (feedback): legacy feedback.js is a GLOBAL BODY-LEVEL DIALOG (not a page view).
// Contract: window.openFeedbackDialog(opts) — app.js topbar calls it bare with no args;
// EventView (batch 8) calls it with { event_seq } for failed-event task context.
// A single hidden app instance is mounted once on document.body and the dialog toggles
// inside it — legacy behavior (re-click closes the open dialog) is kept in component.show().
let _app = null
let _root = null

function ensureMounted() {
  if (_app) return
  const host = document.createElement('div')
  host.id = 'feedbackVueHost'
  document.body.appendChild(host)
  _app = createApp(FeedbackDialog)
  _root = _app.mount(host)
}

window.openFeedbackDialog = function (opts) {
  ensureMounted()
  if (_root && typeof _root.show === 'function') _root.show(opts || {})
}
