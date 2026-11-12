// inject view css + bridge tokens + shared AGButton css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/my-feedback-detail.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static-vue/assets/' + f + '?v=41'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import MyFeedbackDetail from '../MyFeedbackDetail.vue'

// Batch 2 (my_feedback_detail): legacy shell contract — global renderMyFeedbackDetail(id) mounts into #pageContent
window.renderMyFeedbackDetail = function (feedbackId) {
  const el = document.getElementById('pageContent')
  if (!el) return
  createApp(MyFeedbackDetail, { feedbackId: feedbackId }).mount(el)
}
