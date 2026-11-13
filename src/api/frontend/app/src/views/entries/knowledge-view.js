// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/knowledge-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static-vue/assets/' + f + '?v=54'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import KnowledgeView from '../KnowledgeView.vue'

// Batch 15 (knowledge): 收编 legacy knowledge.js (Issue #67 standalone Vue app, options API
// + window.Vue global) into the unified chunk pipeline. Contract kept identical:
//   renderKnowledge()        : app.js nav('knowledge') route branch
//   _knowledgeVueApp         : app.js references this BARE identifier in 3 places:
//     - nav() early-return when already on knowledge view (prevent double mount)
//     - leaving knowledge view → unmount before innerHTML replace
//     - route branch: only call renderKnowledge() when app is absent
//   _showAppleConfirm        : app.js global (delete confirmations) — used via G.window
// The entry owns the app handle so app.js's bare-identifier guards keep working.
let _app = null

// bare-identifier contract: window assignment makes `_knowledgeVueApp` visible to app.js.
// IMPORTANT: only set AFTER a real mount — app.js nav() early-returns when
// (_currentView === 'knowledge' && _knowledgeVueApp) is truthy, so a placeholder
// set at module load would block the FIRST navigation render.
const kbHandle = {
  get _isAlive() {
    return !!_app && !!document.querySelector('.knowledge-page')
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
    window._knowledgeVueApp = null
  },
}

window.renderKnowledge = function () {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) {
    try {
      _app.unmount()
    } catch (e) {
      /* ignore */
    }
  }
  el.innerHTML = '' // legacy cleared the container before mount (Issue #67)
  _app = createApp(KnowledgeView)
  _app.mount(el)
  window._knowledgeVueApp = kbHandle
}
