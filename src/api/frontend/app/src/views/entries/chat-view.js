// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/chat-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static-vue/assets/' + f + '?v=57'
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
  if (_app) { try { _app.unmount() } catch (e) {} _app = null }
  el.innerHTML = ''
  _app = createApp(ChatView, props || {})
  _app.mount(el)
}
function renderChatEntry(openConvId) {
  mount(openConvId ? { openConvId: String(openConvId) } : {})
  window.renderChat = renderChatEntry   // 挂载后回收契约（组件 setup 可能覆盖）
}
// 契约要在「挂载完成后」再指回入口版：组件 setup 里若有同名赋值（曾出现
// `window.renderChat = null`），会把入口版覆盖成不可调用值，之后每次 nav('chat')
// 都拿到 null → "加载失败（renderChat）"。挂载后回收一次，兜住这类覆盖。
window.renderChat = renderChatEntry
