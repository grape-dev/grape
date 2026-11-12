// inject view css + bridge tokens + shared AGButton css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/organizations.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static/js/assets/' + f + '?v=29'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import Organizations from '../Organizations.vue'

// Batch 2c (organizations): legacy shell contract — global renderOrganizations() mounts into #pageContent.
// app.js also calls app.nav('organizations') which invokes this render; detail view is internal (no separate route).
window.renderOrganizations = function () {
  const el = document.getElementById('pageContent')
  if (!el) return
  createApp(Organizations).mount(el)
}
