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
  if (_app) { try { _app.unmount() } catch (e) {} _app = null }
  el.innerHTML = ''
  _app = createApp(AccountView, props || {})
  _app.mount(el)
}
window.renderAccount = function (tab) {
  mount(tab ? { initialTab: String(tab) } : {})
}
