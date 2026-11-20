import { createApp } from 'vue'
import ImChannel from '../ImChannel.vue'

// Batch 2b (im_channel): legacy shell contract — global renderImChannel() mounts into #pageContent
window.renderImChannel = function () {
  const el = document.getElementById('pageContent')
  if (!el) return
  createApp(ImChannel).mount(el)
}
