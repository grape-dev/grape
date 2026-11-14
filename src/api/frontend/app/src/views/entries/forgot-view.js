// Forgot-password page entry (Vue line): mounts ForgotView over #forgotVueHost.
// Same contract as the register/login entries: the shell owns Auth/API/I18N, the
// component owns the markup and keeps the legacy ids for the shared helpers.
// CSS: 本页样式全部来自 shell 页面已引用的 auth.css / app.css（组件无私有样式，故无 viewcss 注入）。
import { createApp } from 'vue'
import ForgotView from '../ForgotView.vue'

const el = document.getElementById('forgotVueHost')
if (el) {
  createApp(ForgotView).mount(el)
}