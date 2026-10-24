// inject view css (built beside the chunk at /static/js/assets/viewcss/my-feedback.css)
;(function () {
  var link = document.createElement('link')
  link.rel = 'stylesheet'
  link.href = '/static/js/assets/viewcss/my-feedback.css'
  document.head.appendChild(link)
})()
import { createApp } from 'vue'
import MyFeedback from '../MyFeedback.vue'

// Batch 2 (my_feedback): legacy shell contract — global renderMyFeedback() mounts into #pageContent
window.renderMyFeedback = function () {
  const el = document.getElementById('pageContent')
  if (!el) return
  createApp(MyFeedback).mount(el)
}
