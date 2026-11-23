<template>
  <div class="auth-card" id="forgotCard">
    <img class="logo" :src="logoUrl" alt="Grape" />
    <h1 data-i18n="app.login.forgot_title">{{ t('app.login.forgot_title', '重置密码') }}</h1>
    <p class="subtitle" data-i18n="app.login.forgot_sub">{{ t('app.login.forgot_sub', '输入注册邮箱获取验证码') }}</p>

    <div class="step-items" id="stepIndicator" v-show="stepsVisible">
      <div :class="stepClass(1)" id="dot1">
        <div class="step-item-circle">1</div>
        <span data-i18n="app.login.step_label_email">邮箱</span>
      </div>
      <div class="step-line"></div>
      <div :class="stepClass(2)" id="dot2">
        <div class="step-item-circle">2</div>
        <span data-i18n="app.login.step_label_verify">验证码</span>
      </div>
      <div class="step-line"></div>
      <div :class="stepClass(3)" id="dot3">
        <div class="step-item-circle">3</div>
        <span data-i18n="app.login.step_label_done">完成</span>
      </div>
    </div>

    <!-- 无邮箱配置提示（预置 DOM，按注入状态显示，零闪动） -->
    <div id="forgotNotConfigured" class="auth-panel" v-show="!emailConfigured" style="text-align: center; padding: 40px 20px">
      <p style="color: var(--t2); font-size: 14px; line-height: 1.6">
        <span data-i18n="app.login.forgot_email_not_configured">邮箱服务未配置，请联系管理员重置密码</span>
        <a href="/static/help.html#/developer/04-storage-and-email" target="_blank" rel="noopener" style="color: #4c8bf5; text-decoration: underline" data-i18n="app.login.oauth_view_config_help">查看配置帮助</a>
      </p>
    </div>

    <!-- Step 1：输入邮箱 -->
    <div id="forgotStep1" class="auth-panel" v-show="emailConfigured && step === 1">
      <form @submit.prevent="sendForgotCode">
        <div class="field-group">
          <label class="field-label" for="forgotEmail" data-i18n="app.login.email">邮箱</label>
          <div class="field-input-wrap">
            <input
              class="field-input"
              id="forgotEmail"
              type="email"
              placeholder="you@example.com"
              data-i18n-placeholder="app.login.email_placeholder"
              autocomplete="email"
              inputmode="email"
              aria-describedby="forgotEmailErr"
              @input="Auth.clearErrorOnInput('forgotEmail')"
              @blur="Auth.blurValidate('forgotEmail', emailRule)"
            />
          </div>
          <p class="field-error" id="forgotEmailErr" role="alert" aria-live="polite"></p>
        </div>
        <button class="auth-btn" id="forgotSendBtn" type="submit">
          <span data-i18n="app.login.send_code">{{ t('app.login.send_code', '发送验证码') }}</span>
        </button>
      </form>
    </div>

    <!-- Step 2：验证码 + 新密码 -->
    <div id="forgotStep2" class="auth-panel" v-show="emailConfigured && step === 2">
      <form @submit.prevent="resetPassword">
        <div class="field-group">
          <label class="field-label" for="forgotEmailDisplay" data-i18n="app.login.email">邮箱</label>
          <div class="field-input-wrap">
            <input class="field-input" id="forgotEmailDisplay" type="email" disabled />
          </div>
        </div>
        <div class="field-group">
          <label class="field-label" for="forgotCode" data-i18n="app.login.verify_code">验证码</label>
          <div class="code-row">
            <input
              class="field-input"
              id="forgotCode"
              type="text"
              :placeholder="t('app.login.code_placeholder', '6位验证码')"
              maxlength="6"
              data-i18n-placeholder="app.login.code_placeholder"
              inputmode="numeric"
              autocomplete="one-time-code"
              enterkeyhint="next"
              aria-describedby="forgotCodeErr"
            />
            <button type="button" class="send-code-btn" id="forgotResendBtn" @click="resendForgotCode" data-i18n="app.login.resend_code">重新发送</button>
          </div>
          <p class="field-error" id="forgotCodeErr" role="alert" aria-live="polite"></p>
        </div>
        <div class="field-group">
          <label class="field-label" for="forgotNewPass" data-i18n="app.login.new_password">新密码</label>
          <div class="field-input-wrap">
            <input
              class="field-input"
              id="forgotNewPass"
              type="password"
              :placeholder="t('app.login.password_placeholder', '至少8个字符')"
              data-i18n-placeholder="app.login.password_placeholder"
              autocomplete="new-password"
              aria-describedby="forgotNewPassErr"
              @input="onNewPwdInput"
              @blur="Auth.blurValidate('forgotNewPass', newPwdRule)"
            />
            <button type="button" class="pwd-toggle" @click="Auth.togglePwd('forgotNewPass', $event.currentTarget)" :aria-label="t('app.login.show_password', '显示密码')" data-i18n-aria="app.login.show_password">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" /></svg>
            </button>
          </div>
          <div class="pwd-strength pwd-strength-focus" id="forgotNewPassStrength">
            <div class="pwd-strength-bar"><div class="pwd-strength-fill" id="newPwdStrengthFill"></div></div>
            <p class="pwd-strength-label" id="newPwdStrengthLabel" data-i18n="app.login.pwd_strength">密码强度</p>
          </div>
          <ul class="pwd-reqs pwd-reqs-focus" id="newPwdReqs">
            <li id="newreq-len" data-i18n="app.login.pwd_req_length">至少8个字符</li>
            <li id="newreq-complex" data-i18n="app.login.pwd_req_alnum">包含字母和数字</li>
          </ul>
          <p class="field-error" id="forgotNewPassErr" role="alert" aria-live="polite"></p>
        </div>
        <div class="field-group">
          <label class="field-label" for="forgotNewPassConfirm" data-i18n="app.login.confirm_new_password">确认新密码</label>
          <div class="field-input-wrap">
            <input
              class="field-input"
              id="forgotNewPassConfirm"
              type="password"
              :placeholder="t('app.login.confirm_new_password_placeholder', '再次输入新密码')"
              data-i18n-placeholder="app.login.confirm_new_password_placeholder"
              autocomplete="new-password"
              enterkeyhint="done"
              aria-describedby="forgotConfirmErr"
              @input="Auth.clearErrorOnInput('forgotNewPassConfirm')"
              @blur="Auth.blurValidate('forgotNewPassConfirm', confirmRule)"
            />
            <button type="button" class="pwd-toggle" @click="Auth.togglePwd('forgotNewPassConfirm', $event.currentTarget)" :aria-label="t('app.login.show_password', '显示密码')" data-i18n-aria="app.login.show_password">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" /></svg>
            </button>
          </div>
          <p class="field-error" id="forgotConfirmErr" role="alert" aria-live="polite"></p>
        </div>
        <button class="auth-btn" id="forgotResetBtn" type="submit" data-i18n="app.login.reset_password_btn">重置密码</button>
        <button class="auth-btn-secondary" type="button" @click="backToStep1" data-i18n="app.login.back_to_edit_email">← 返回修改邮箱</button>
      </form>
    </div>

    <!-- Step 3：成功 -->
    <div id="forgotStep3" v-show="step === 3">
      <div class="auth-success">
        <div class="success-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" /></svg>
        </div>
        <p class="success-title" data-i18n="app.login.toast_reset_success">密码重置成功</p>
        <p class="success-msg" data-i18n="app.login.toast_use_new_password">请使用新密码登录</p>
      </div>
    </div>

    <div class="auth-links" id="backLinks" v-show="step !== 3">
      <span data-i18n="app.login.remember">想起来了？</span>
      <span class="dot">·</span>
      <a href="/" data-i18n="app.login.back_to_login">返回登录</a>
    </div>

    <div class="lang-switch" @click="Auth.toggleLang()">
      <span id="langSwitchLabel">{{ lang === 'zh' ? 'EN' : '中文' }}</span>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'

const G = window
const Auth = G.Auth
const API = G.API
const logoUrl = '/static/img/grape-logo.jpg?v=1'

const emailConfigured = ref(typeof G.__EMAIL_CONFIGURED__ !== 'undefined' ? !!G.__EMAIL_CONFIGURED__ : false)
const step = ref(emailConfigured.value ? 1 : 0)
const lang = ref((G.I18N && G.I18N.getLang && G.I18N.getLang()) || 'zh')

function t(key, fallback) {
  void lang.value
  const v = G.I18N && G.I18N.t ? G.I18N.t(key) : ''
  return v && v !== key ? v : fallback !== undefined ? fallback : key
}

const stepsVisible = computed(() => emailConfigured.value && step.value !== 3)
function stepClass(n) {
  if (step.value > n) return 'step-item done'
  if (step.value === n) return 'step-item active'
  return 'step-item'
}

function $(id) {
  return document.getElementById(id)
}

const emailRule = (v) => (v && v.indexOf('@') < 0 ? t('app.login.err_email_format', '邮箱格式不正确') : null)
const newPwdRule = (v) => {
  if (v && G.isPwdTooShort && G.isPwdTooShort(v)) return t('app.login.pwd_req_length', '至少8个字符')
  if (v && G.isWeakPassword && G.isWeakPassword(v)) return t('app.login.err_password_blocklist', '密码过于常见，请更换')
  return null
}
const confirmRule = (v) => {
  const np = ($('forgotNewPass') || {}).value || ''
  return v && v !== np.trim() ? t('app.login.err_confirm_mismatch', '两次输入的密码不一致') : null
}

function onNewPwdInput() {
  Auth.clearErrorOnInput('forgotNewPass')
  Auth.updatePwdStrength('forgotNewPass', {
    strengthId: 'forgotNewPassStrength',
    fillId: 'newPwdStrengthFill',
    labelId: 'newPwdStrengthLabel',
    reqsId: 'newPwdReqs',
    reqs: [
      { id: 'newreq-len', test: (p) => !(G.isPwdTooShort && G.isPwdTooShort(p)) },
      { id: 'newreq-complex', test: (p) => /[a-zA-Z]/.test(p) && /\d/.test(p) },
    ],
  })
}

function sendForgotCode() {
  if (!Auth.submitValidate('forgotEmail', (v) => (v ? emailRule(v) : t('app.login.err_email_required', '请输入邮箱')))) return
  const e = ($('forgotEmail') || {}).value.trim()
  const btn = $('forgotSendBtn')
  Auth.setBtnLoading(btn, true)
  API.post('/auth/forgot-password/send-code', { email: e })
    .then(() => {
      Auth.setBtnLoading(btn, false)
      Auth.showToast(t('app.login.toast_code_sent', '验证码已发送') + ' ' + e)
      step.value = 2
      const disp = $('forgotEmailDisplay')
      if (disp) disp.value = e
      setTimeout(() => $('forgotCode') && $('forgotCode').focus(), 100)
      Auth.startCountdown('forgotResendBtn')
    })
    .catch((err) => {
      Auth.setBtnLoading(btn, false)
      const code = (err && err.error_code) || ''
      Auth.showToast(t('app.error.' + code, t('common.server_error', (err && err.message) || t('app.login.toast_send_failed', '发送失败'))))
    })
}

function resendForgotCode() {
  const e = ($('forgotEmail') || {}).value.trim()
  API.post('/auth/forgot-password/send-code', { email: e })
    .then(() => {
      Auth.showToast(t('app.login.toast_code_resent', '验证码已重新发送'))
      Auth.startCountdown('forgotResendBtn')
    })
    .catch((err) => Auth.showToast((err && err.message) || t('app.login.toast_send_failed', '发送失败')))
}

function backToStep1() {
  step.value = 1
  ;['forgotCode', 'forgotNewPass', 'forgotNewPassConfirm'].forEach((id) => {
    const el = $(id)
    if (el) el.value = ''
  })
  const email = $('forgotEmail')
  if (email) email.focus()
}

function resetPassword() {
  const e = ($('forgotEmail') || {}).value.trim()
  const c = ($('forgotCode') || {}).value.trim()
  const np = ($('forgotNewPass') || {}).value.trim()
  let valid = true
  if (!Auth.submitValidate('forgotCode', (v) => (v ? (v.length !== 6 ? t('app.login.err_code_format', '验证码为 6 位数字') : null) : t('app.login.err_code_required', '请输入验证码')))) valid = false
  if (!Auth.submitValidate('forgotNewPass', (v) => (v ? newPwdRule(v) : t('app.login.err_new_password_required', '请输入新密码')))) valid = false
  if (!Auth.submitValidate('forgotNewPassConfirm', (v) => (v ? confirmRule(v) : t('app.login.err_confirm_new_required', '请再次输入新密码')))) valid = false
  if (!valid) return

  const btn = $('forgotResetBtn')
  Auth.setBtnLoading(btn, true)
  API.post('/auth/forgot-password/reset', { email: e, code: c, new_password: np })
    .then(() => {
      Auth.setBtnLoading(btn, false)
      step.value = 3
      setTimeout(() => (window.location.href = '/?reset=1'), 2000)
    })
    .catch((err) => {
      Auth.setBtnLoading(btn, false)
      const code = (err && err.error_code) || ''
      const field = { CODE_INVALID: 'forgotCode', ACCOUNT_NOT_FOUND: 'forgotCode', PASSWORD_WEAK: 'forgotNewPass' }[code]
      const msg = t('app.error.' + code, t('common.server_error', (err && err.message) || t('app.login.toast_reset_failed', '重置失败')))
      if (field) {
        Auth.showFieldError(field, msg)
        if (field === 'forgotCode' && $('forgotCode')) $('forgotCode').focus()
      } else {
        Auth.showToast(msg)
      }
    })
}

onMounted(() => {
  if (G.I18N && G.I18N.applyI18nToDOM) G.I18N.applyI18nToDOM()
  if (emailConfigured.value) {
    const el = $('forgotEmail')
    if (el) setTimeout(() => el.focus(), 50)
  }
})

defineExpose({ step })
</script>