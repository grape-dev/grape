// Register page entry (Vue line): mounts RegisterView over #registerVueHost.
// The shell keeps the document + globals (Auth / API / I18N / pwd-blocklist) and the
// component keeps the legacy element ids those helpers read, so the two-step
// registration flow, password strength meter and OAuth associate mode behave as before.
// CSS: 本页样式全部来自 shell 页面已引用的 auth.css / app.css（组件无私有样式，故无 viewcss 注入）。
import { createApp } from 'vue'
import RegisterView from '../RegisterView.vue'

const el = document.getElementById('registerVueHost')
if (el) {
  createApp(RegisterView).mount(el)
}