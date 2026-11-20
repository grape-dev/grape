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
  if (_app) { try { _app.unmount() } catch (e) {} _app = null }
  el.innerHTML = ''
  _app = createApp(IssuesView, props || {})
  _app.mount(el)
}
window.renderTasks = function (param) {
  mount({ initialParam: param === undefined ? null : param })
}
