// Login page entry (Vue line): mounts LoginView over #loginVueHost inside the shell document.
// The shell keeps the document skeleton (theme guards, sidebar/main containers, app.js) and
// this chunk only owns the auth card, so the legacy global contract (window.app / I18N / API)
// stays in charge of the login behaviour (pwdLogin, oauthLogin, validation, toasts).
//
// CSS: viewcss/login-view.css = compiled <style> of LoginView.vue + the shared auth.css tokens
// it sits on (auth.css itself is linked by shell/index.html, kept as the single stylesheet).
;(function () {
  ;['viewcss/login-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static-vue/assets/' + f + '?v=1'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import LoginView from '../LoginView.vue'

const el = document.getElementById('loginVueHost')
if (el) {
  createApp(LoginView).mount(el)
}