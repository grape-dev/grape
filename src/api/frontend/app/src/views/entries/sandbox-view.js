// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static-vue/assets/' + f + '?v=1'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import SandboxView from '../SandboxView.vue'

// B2 (sandbox platform admin): legacy shell contract —
//   renderSandbox()  : app.js nav('sandbox') route branch
//   _sandboxVueApp   : app.js references this BARE identifier when leaving the
//                      view (unmount before innerHTML replace, then sets null).
// The entry owns the app handle so app.js's bare-identifier guard keeps working
// (same pattern as entries/knowledge-view.js).
let _app = null

const sbxHandle = {
  get _isAlive() {
    return !!_app && !!document.querySelector('.apple-card')
  },
  unmount() {
    if (_app) {
      try {
        _app.unmount()
      } catch (e) {
        /* ignore */
      }
    }
    _app = null
    window._sandboxVueApp = null
  },
}

window.renderSandbox = function () {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) {
    try {
      _app.unmount()
    } catch (e) {
      /* ignore */
    }
  }
  el.innerHTML = ''
  _app = createApp(SandboxView)
  _app.mount(el)
  // bare-identifier contract: only set AFTER a real mount
  window._sandboxVueApp = sbxHandle
}
