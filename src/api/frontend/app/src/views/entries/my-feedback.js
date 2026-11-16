import { createApp } from 'vue'
import MyFeedback from '../MyFeedback.vue'

// Batch 2 (my_feedback): legacy shell contract — global renderMyFeedback() mounts into #pageContent
window.renderMyFeedback = function () {
  const el = document.getElementById('pageContent')
  if (!el) return
  createApp(MyFeedback).mount(el)
}
