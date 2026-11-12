// inject view css + bridge tokens + shared AGButton css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/im-channel.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static/js/assets/' + f + '?v=13'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import ImChannel from '../ImChannel.vue'

// Batch 2b (im_channel): legacy shell contract — global renderImChannel() mounts into #pageContent
window.renderImChannel = function () {
  const el = document.getElementById('pageContent')
  if (!el) return
  createApp(ImChannel).mount(el)
}
