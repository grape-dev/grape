// 引导页 view chunk（onboarding）：整页 Vue 化入口。
// shell app.js 的 _renderOnboardingShell 只负责隐藏侧栏/顶栏并调用 window.renderOnboarding，
// 卡片渲染、绑定弹层（OAuth + PAT 双模式）全部在组件内完成。
;(function () {
  ;['bridge.css', 'viewcss/onboarding-view.css', 'viewcss/bind-dialog.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static-vue/assets/' + f + '?v=8'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import OnboardingView from '../OnboardingView.vue'

let _app = null
function mount(props) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) _app.unmount()
  _app = createApp(OnboardingView, props || {})
  _app.mount(el)
}
window.renderOnboarding = function (props) {
  mount(props || {})
}