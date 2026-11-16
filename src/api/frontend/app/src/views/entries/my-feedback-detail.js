import { createApp } from 'vue'
import MyFeedbackDetail from '../MyFeedbackDetail.vue'

// Batch 2 (my_feedback_detail): legacy shell contract — global renderMyFeedbackDetail(id) mounts into #pageContent
window.renderMyFeedbackDetail = function (feedbackId) {
  const el = document.getElementById('pageContent')
  if (!el) return
  createApp(MyFeedbackDetail, { feedbackId: feedbackId }).mount(el)
}
