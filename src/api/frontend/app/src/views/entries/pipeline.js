import { createApp } from 'vue'
import Pipeline from '../Pipeline.vue'

// Batch 3a (pipeline): legacy shell contract — global renderPipeline(data, focusIssueNum).
// data comes from app.js ('pipe' view): API.get('/pipeline/owner/name') result.
// Keeps persistent query/page across re-renders like the legacy module-level vars did.
let _app = null
window.renderPipeline = function (data, focusIssueNum) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) { try { _app.unmount() } catch (e) { /* 未挂载/已卸载则忽略 */ } _app = null }
  el.innerHTML = ''
  _app = createApp(Pipeline, { data })
  const vm = _app.mount(el)
  // focus issue parity: preselect the target issue's pipeline
  if (focusIssueNum && vm.focus) vm.focus(focusIssueNum)
}
