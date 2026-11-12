// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/account-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static/js/assets/' + f + '?v=25'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import AccountView from '../AccountView.vue'

// Batch 7 (account): legacy shell contract —
//   renderAccount(tab): app.js nav('account', param) — five tabs
//   (profile/security/credentials/appearance/apikeys), hash-route compatible.
// Also app.js OAuth-callback page calls window._accountShowTransferConfirm
// (set by the component). platformIcon lives in repos.js (shell coupling,
// kept global) — the component reads it via window if available.
let _app = null
function mount(props) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) _app.unmount()
  _app = createApp(AccountView, props || {})
  _app.mount(el)
}
window.renderAccount = function (tab) {
  mount(tab ? { initialTab: String(tab) } : {})
}
