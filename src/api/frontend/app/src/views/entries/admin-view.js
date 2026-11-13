// inject view css (standalone admin page — bridge/ag-ui css not needed, the page
// keeps its own legacy tokens.css + inline styles from admin.html)
;(function () {
  ;['viewcss/admin-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static-vue/assets/' + f + '?v=55'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import AdminView from '../AdminView.vue'

// Batch 17 (admin): the standalone admin site. Not part of the app.js shell —
// admin.html is its own document; this entry mounts the whole app (login +
// config center + users + whitelists) over #adminVueHost.
const el = document.getElementById('adminVueHost')
if (el) {
  createApp(AdminView).mount(el)
}
