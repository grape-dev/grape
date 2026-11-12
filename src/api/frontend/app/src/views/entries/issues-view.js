// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/issues-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static/js/assets/' + f + '?v=29'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import IssuesView from '../IssuesView.vue'

// Batch 9 (issues/tasks): legacy shell contract —
//   renderTasks(param): app.js nav('tasks', param) — param 为纯数字时视为深链 issue_number (#tasks/<n>)
// Component exports window contracts: _stopScenePoll (app.js leave-page),
// _grapeConfirm (repos.js confirm dialogs), _backToTaskDetail (batch8 EventView
// back-to-task button — was never defined in legacy, now provided),
// _jumpToNodeEvent/_jumpToPipeline (event-view node jumps), _tasksById (cross-module map).
let _app = null
function mount(props) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) _app.unmount()
  _app = createApp(IssuesView, props || {})
  _app.mount(el)
}
window.renderTasks = function (param) {
  mount({ initialParam: param === undefined ? null : param })
}
