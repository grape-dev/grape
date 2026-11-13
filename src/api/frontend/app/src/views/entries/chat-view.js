// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/chat-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static-vue/assets/' + f + '?v=56'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import ChatView from '../ChatView.vue'

// Batch 6 (chat): legacy shell contract —
//   renderChat(openConvId): app.js nav('chat', param) — param is conv_id or
//   'issue:N:repoKey' (issues-center deep link); fresh mount per nav.
// Also app.js references: window._chatStreamAbort (leave-page abort),
// window._stopOnboardPoll, window.resumeChatTask — set by the component.
let _app = null
function mount(props) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) _app.unmount()
  _app = createApp(ChatView, props || {})
  _app.mount(el)
}
window.renderChat = function (openConvId) {
  mount(openConvId ? { openConvId: String(openConvId) } : {})
}
