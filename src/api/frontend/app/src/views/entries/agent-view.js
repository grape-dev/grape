// inject view css + bridge tokens + shared AGButton css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/agent-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static-vue/assets/' + f + '?v=56'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import AgentView from '../AgentView.vue'

// Batch 3b (agents): legacy shell contract — global renderAgent(name, data), views vod/issue/coding/qa/sre.
// refreshVodOnboard kept as a no-op-safe global (chat.js cross-module call): VOD IM fields no longer
// exist on this page (config panel removed in scene-center refactor), the re-fetch refreshes nothing visible.
window.renderAgent = function (name, data) {
  const el = document.getElementById('pageContent')
  if (!el) return
  createApp(AgentView, { name, data }).mount(el)
}
window.refreshVodOnboard = function () {
  const repo = globalThis.app && globalThis.app.currentRepo ? globalThis.app.currentRepo() : null
  if (!repo) return
  globalThis.API.get('/agents/vod/' + repo.owner + '/' + repo.name).catch(function () {})
}
