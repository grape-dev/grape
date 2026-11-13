// inject view css + bridge tokens + shared AGButton css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/pipeline.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static-vue/assets/' + f + '?v=47'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import Pipeline from '../Pipeline.vue'

// Batch 3a (pipeline): legacy shell contract — global renderPipeline(data, focusIssueNum).
// data comes from app.js ('pipe' view): API.get('/pipeline/owner/name') result.
// Keeps persistent query/page across re-renders like the legacy module-level vars did.
let _app = null
window.renderPipeline = function (data, focusIssueNum) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) _app.unmount()
  _app = createApp(Pipeline, { data })
  const vm = _app.mount(el)
  // focus issue parity: preselect the target issue's pipeline
  if (focusIssueNum && vm.focus) vm.focus(focusIssueNum)
}
