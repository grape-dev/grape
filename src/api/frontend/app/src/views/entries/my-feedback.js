// inject view css + bridge tokens + shared AGButton css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/my-feedback.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static/js/assets/' + f + '?v=33'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import MyFeedback from '../MyFeedback.vue'

// Batch 2 (my_feedback): legacy shell contract — global renderMyFeedback() mounts into #pageContent
window.renderMyFeedback = function () {
  const el = document.getElementById('pageContent')
  if (!el) return
  createApp(MyFeedback).mount(el)
}
