<template>
  <div class="auth-card" id="registerCard">
    <img class="logo" :src="logoUrl" alt="Grape" />
    <h1 id="pageTitle">{{ pageTitle }}</h1>
    <p class="subtitle" id="pageSub">{{ pageSub }}</p>

    <!-- 步骤指示器（有邮箱配置时显示） -->
    <div class="step-items" id="stepIndicator" v-show="showSteps">
      <div :class="stepClass(1)" id="dot1">
        <div class="step-item-circle">1</div>
        <span data-i18n="app.login.step_label_info">填写信息</span>
      </div>
      <div :class="stepLineClass" id="stepLine"></div>
      <div :class="stepClass(2)" id="dot2">
        <div class="step-item-circle">2</div>
        <span data-i18n="app.login.step_label_verify">验证码</span>
      </div>
    </div>

    <!-- OAuth 关联模式 -->
    <div id="oauthTabs" v-show="oauthMode">
      <div class="oauth-tabs">
        <div :class="tab === 'register' ? 'oauth-tab active' : 'oauth-tab'" id="tabRegister" @click="switchTab('register')" data-i18n="app.login.register_new">注册新账号</div>
        <div :class="tab === 'associate' ? 'oauth-tab active' : 'oauth-tab'" id="tabAssociate" @click="switchTab('associate')" data-i18n="app.login.associate_existing">关联已有账号</div>
      </div>
      <div class="oauth-info" id="oauthInfo" v-html="oauthInfoHtml"></div>
    </div>

    <!-- ============ 注册表单 ============ -->
    <form id="registerPanel" class="auth-panel" v-show="tab === 'register'" @submit.prevent="onRegisterSubmit">
      <div class="field-group">
        <label class="field-label" for="regUsername" data-i18n="app.login.username">用户名</label>
        <div class="field-input-wrap">
          <input
            class="field-input"
            id="regUsername"
            type="text"
            :placeholder="t('app.login.username_placeholder', '3-32字符，字母、数字、下划线')"
            data-i18n-placeholder="app.login.username_placeholder"
            autocomplete="username"
            autocapitalize="none"
            autocorrect="off"
            aria-describedby="regUsernameErr"
            @input="Auth.clearErrorOnInput('regUsername')"
            @blur="onBlurValidate('regUsername')"
          />
        </div>
        <p class="field-error" id="regUsernameErr" role="alert" aria-live="polite"></p>
      </div>

      <div class="field-group" id="emailField" v-show="emailConfigured && !oauthMode">
        <label class="field-label" for="regEmail" data-i18n="app.login.email">邮箱</label>
        <div class="field-input-wrap">
          <input
            class="field-input"
            id="regEmail"
            type="email"
            placeholder="you@example.com"
            data-i18n-placeholder="app.login.email_placeholder"
            autocomplete="email"
            inputmode="email"
            aria-describedby="regEmailErr"
            @input="Auth.clearErrorOnInput('regEmail')"
            @blur="onBlurValidate('regEmail')"
          />
        </div>
        <p class="field-error" id="regEmailErr" role="alert" aria-live="polite"></p>
      </div>

      <div class="field-group" id="passwordField">
        <label class="field-label" for="regPassword" data-i18n="app.login.password">密码</label>
        <div class="field-input-wrap">
          <input
            class="field-input"
            id="regPassword"
            type="password"
            :placeholder="t('app.login.password_placeholder', '至少8个字符')"
            data-i18n-placeholder="app.login.password_placeholder"
            autocomplete="new-password"
            aria-describedby="regPasswordErr"
            @input="onPwdInput"
            @blur="onBlurValidate('regPassword')"
          />
          <button type="button" class="pwd-toggle" @click="Auth.togglePwd('regPassword', $event.currentTarget)" :aria-label="t('app.login.show_password', '显示密码')" data-i18n-aria="app.login.show_password">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" /></svg>
          </button>
        </div>
        <div class="pwd-strength pwd-strength-focus" id="regPasswordStrength">
          <div class="pwd-strength-bar"><div class="pwd-strength-fill" id="pwdStrengthFill"></div></div>
          <p class="pwd-strength-label" id="pwdStrengthLabel" data-i18n="app.login.pwd_strength">密码强度</p>
        </div>
        <ul class="pwd-reqs pwd-reqs-focus" id="pwdReqs">
          <li id="req-len" data-i18n="app.login.pwd_req_length">至少8个字符</li>
          <li id="req-complex" data-i18n="app.login.pwd_req_alnum">包含字母和数字</li>
          <li id="req-notmatch" data-i18n="app.login.pwd_req_not_match">不与用户名/邮箱相同</li>
          <li id="req-blocklist" data-i18n="app.login.pwd_req_blocklist">非常见弱密码</li>
        </ul>
        <p class="field-error" id="regPasswordErr" role="alert" aria-live="polite"></p>
      </div>

      <div class="field-group" id="passwordConfirmField">
        <label class="field-label" for="regPasswordConfirm" data-i18n="app.login.confirm_password">确认密码</label>
        <div class="field-input-wrap">
          <input
            class="field-input"
            id="regPasswordConfirm"
            type="password"
            :placeholder="t('app.login.confirm_password_placeholder', '再次输入密码')"
            data-i18n-placeholder="app.login.confirm_password_placeholder"
            autocomplete="new-password"
            aria-describedby="regPasswordConfirmErr"
            @input="Auth.clearErrorOnInput('regPasswordConfirm')"
            @blur="onBlurValidate('regPasswordConfirm')"
          />
          <button type="button" class="pwd-toggle" @click="Auth.togglePwd('regPasswordConfirm', $event.currentTarget)" :aria-label="t('app.login.show_password', '显示密码')" data-i18n-aria="app.login.show_password">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" /></svg>
          </button>
        </div>
        <p class="field-error" id="regPasswordConfirmErr" role="alert" aria-live="polite"></p>
      </div>

      <!-- 验证码区域（第二步） -->
      <div id="regCodeArea" v-show="codeStep">
        <div class="field-group">
          <label class="field-label" for="regCode" data-i18n="app.login.verify_code">验证码</label>
          <div class="code-row">
            <input
              class="field-input"
              id="regCode"
              type="text"
              :placeholder="t('app.login.code_placeholder', '6位验证码')"
              data-i18n-placeholder="app.login.code_placeholder"
              maxlength="6"
              inputmode="numeric"
              autocomplete="one-time-code"
              enterkeyhint="done"
              aria-describedby="regCodeErr"
            />
            <button class="send-code-btn" id="regSendBtn" type="button" @click="resendCode" data-i18n="app.login.resend_code">重新发送</button>
          </div>
          <p class="field-error" id="regCodeErr" role="alert" aria-live="polite"></p>
          <p style="margin-top: 8px; font-size: 12px; color: var(--auth-text-tertiary); line-height: 1.5">
            <span data-i18n="app.login.code_not_received_hint">没收到验证码？如果你已注册，请</span>
            <a href="/" style="color: var(--auth-accent)" data-i18n="app.login.back_to_login">返回登录</a>
            <span data-i18n="app.login.or">或</span>
            <a href="/forgot" style="color: var(--auth-accent)" data-i18n="app.login.forgot_password">忘记密码</a>
          </p>
        </div>
        <button class="auth-btn" id="regVerifyBtn" type="button" @click="registerVerify">
          <span data-i18n="app.login.register_btn">注册</span>
        </button>
        <button class="auth-btn-secondary" type="button" @click="backToStep1" data-i18n="app.login.back_to_edit_info">← 返回修改信息</button>
      </div>

      <div id="regStep1Btn" v-show="!codeStep">
        <button class="auth-btn" type="submit" id="regSubmitBtn">
          <span :data-i18n="submitLabelKey">{{ submitLabelText }}</span>
        </button>
      </div>
    </form>

    <!-- ============ 关联已有账号表单 ============ -->
    <form id="associatePanel" class="auth-panel" v-show="oauthMode && tab === 'associate'" @submit.prevent="associateAccount">
      <div class="field-group">
        <label class="field-label" for="assocIdentifier" data-i18n="app.login.account_or_email">用户名或邮箱</label>
        <div class="field-input-wrap">
          <input
            class="field-input"
            id="assocIdentifier"
            type="text"
            :placeholder="t('app.login.empty_input', '输入 Grape 账号用户名或邮箱')"
            data-i18n-placeholder="app.login.empty_input"
            autocomplete="username"
            autocapitalize="none"
            aria-describedby="assocIdentifierErr"
            @input="Auth.clearErrorOnInput('assocIdentifier')"
          />
        </div>
        <p class="field-error" id="assocIdentifierErr" role="alert" aria-live="polite"></p>
      </div>
      <div class="field-group">
        <label class="field-label" for="assocPassword" data-i18n="app.login.password">密码</label>
        <div class="field-input-wrap">
          <input
            class="field-input"
            id="assocPassword"
            type="password"
            :placeholder="t('app.login.password_placeholder', '输入密码')"
            data-i18n-placeholder="app.login.password_placeholder"
            autocomplete="current-password"
            aria-describedby="assocPasswordErr"
            @input="Auth.clearErrorOnInput('assocPassword')"
          />
          <button type="button" class="pwd-toggle" @click="Auth.togglePwd('assocPassword', $event.currentTarget)" :aria-label="t('app.login.show_password', '显示密码')" data-i18n-aria="app.login.show_password">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" /></svg>
          </button>
        </div>
        <p class="field-error" id="assocPasswordErr" role="alert" aria-live="polite"></p>
      </div>
      <button class="auth-btn" id="assocBtn" type="submit" data-i18n="app.login.associate_btn">关联并登录</button>
    </form>

    <!-- ============ 成功状态 ============ -->
    <div id="successPanel" v-show="success">
      <div class="auth-success">
        <div class="success-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" /></svg>
        </div>
        <p class="success-title" id="successTitle">{{ successTitle }}</p>
        <p class="success-msg" id="successMsg">{{ successMsg }}</p>
      </div>
    </div>

    <div class="auth-links">
      <span data-i18n="app.login.have_account">已有账号？</span>
      <span class="dot">·</span>
      <a href="/" data-i18n="app.login.back_to_login">返回登录</a>
    </div>

    <div class="lang-switch" @click="Auth.toggleLang()"><span id="langSwitchLabel">{{ lang === 'zh' ? 'EN' : '中文' }}</span></div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'

const G = window
const Auth = G.Auth
const API = G.API
const logoUrl = '/static/img/grape-logo.jpg?v=1'

const params = new URLSearchParams(location.search)
const oauthMode = ref(params.get('oauth_associate') === '1')
const oauthState = params.get('state') || ''
const oauthProvider = params.get('provider') || ''
const oauthExternalLogin = params.get('external_login') || ''
const emailConfigured = ref(typeof G.__EMAIL_CONFIGURED__ !== 'undefined' ? !!G.__EMAIL_CONFIGURED__ : false)

const tab = ref('register')
const codeStep = ref(false)
const success = ref(false)
const successTitle = ref('')
const successMsg = ref('')
const lang = ref((G.I18N && G.I18N.getLang && G.I18N.getLang()) || 'zh')

function t(key, fallback) {
  void lang.value
  const v = G.I18N && G.I18N.t ? G.I18N.t(key) : ''
  return v && v !== key ? v : fallback !== undefined ? fallback : key
}

const providerLabel = computed(() => (typeof window.platformLabel === 'function' && window.platformLabel(oauthProvider)) || oauthProvider)
const externalName = params.get('external_name') || oauthExternalLogin
const pageTitle = computed(() =>
  oauthMode.value
    ? String(t('app.login.associate_title', '关联 {provider} 账号')).replace('{provider}', providerLabel.value)
    : t('app.login.register_title', '注册 Grape 账号')
)
const pageSub = computed(() => (oauthMode.value ? t('app.login.select_tab_hint', '选择注册新账号或关联已有账号') : t('app.login.register_sub', 'AI Native DevOps')))
const oauthInfoHtml = computed(() =>
  oauthMode.value
    ? String(t('app.login.oauth_not_associated', '你正在使用 {name}（{login}）授权，请选择注册新账号或关联已有账号'))
        .replace('{name}', '<b>' + Auth.escHtml(externalName) + '</b>')
        .replace('{login}', Auth.escHtml(oauthExternalLogin))
    : ''
)
const showSteps = computed(() => !oauthMode.value && emailConfigured.value)
const submitLabelKey = computed(() => (emailConfigured.value && !oauthMode.value ? 'app.login.send_code' : 'app.login.register_btn'))
const submitLabelText = computed(() => t(submitLabelKey.value, emailConfigured.value ? '发送验证码' : '注册'))

function stepClass(n) {
  // 步骤指示器状态与旧页一致：1=填写信息 2=验证码
  void codeStep.value
  if (n === 1) return codeStep.value ? 'step-item done' : 'step-item active'
  return codeStep.value ? 'step-item active' : 'step-item'
}
const stepLineClass = computed(() => (codeStep.value ? 'step-line done' : 'step-line'))

function $(id) {
  return document.getElementById(id)
}
function switchTab(next) {
  tab.value = next
}
function onRegisterSubmit() {
  // 无邮箱模式：注册即提交（旧页把 onsubmit 换成 registerVerify）
  if (!emailConfigured.value || oauthMode.value) return registerVerify()
  return goToStep2()
}

// ============ 验证规则 ============
const RULES = {
  regUsername: (v) => {
    if (!v) return t('app.login.err_username_required', '请输入用户名')
    if (v.length < 3 || v.length > 32) return t('app.login.err_username_length', '用户名长度需为 3-32 个字符')
    if (!/^[a-zA-Z0-9_]+$/.test(v)) return t('app.login.err_username_format', '用户名只能包含字母、数字、下划线')
    return null
  },
  regEmail: (v) => {
    if (!v) return t('app.login.err_email_required', '请输入邮箱')
    if (v.indexOf('@') < 0 || v.indexOf('.') < 0) return t('app.login.err_email_format', '邮箱格式不正确')
    return null
  },
  regPassword: (v) => {
    if (!v) return t('app.login.err_password_required', '请输入密码')
    if (G.isPwdTooShort && G.isPwdTooShort(v)) return t('app.login.err_password_length', '密码至少 8 个字符')
    if (G.isWeakPassword && G.isWeakPassword(v)) return t('app.login.err_password_blocklist', '密码过于常见，请更换')
    return null
  },
  regPasswordConfirm: (v) => {
    if (!v) return t('app.login.err_confirm_required', '请再次输入密码')
    const pwd = ($('regPassword') || {}).value || ''
    if (v !== pwd.trim()) return t('app.login.err_confirm_mismatch', '两次输入的密码不一致')
    return null
  },
}

function onBlurValidate(fieldId) {
  const rule = RULES[fieldId]
  if (!rule) return
  Auth.blurValidate(fieldId, rule)
}

// ============ 密码强度 ============
function onPwdInput() {
  Auth.clearErrorOnInput('regPassword')
  Auth.updatePwdStrength('regPassword', {
    strengthId: 'regPasswordStrength',
    fillId: 'pwdStrengthFill',
    labelId: 'pwdStrengthLabel',
    reqsId: 'pwdReqs',
    reqs: [
      { id: 'req-len', test: (p) => !(G.isPwdTooShort && G.isPwdTooShort(p)) },
      { id: 'req-complex', test: (p) => /[a-zA-Z]/.test(p) && /\d/.test(p) },
      {
        id: 'req-notmatch',
        test: (p) => {
          const u = (($('regUsername') || {}).value || '').trim().toLowerCase()
          const e = (($('regEmail') || {}).value || '').trim().toLowerCase()
          return p.length > 0 && p.toLowerCase() !== u && p.toLowerCase() !== e
        },
      },
      { id: 'req-blocklist', test: (p) => p.length > 0 && !(G.isWeakPassword && G.isWeakPassword(p)) },
    ],
  })
}

// ============ 步骤管理 ============
function goToStep2() {
  let valid = true
  if (!Auth.submitValidate('regUsername', RULES.regUsername)) valid = false
  if (!oauthMode.value && emailConfigured.value) {
    if (!Auth.submitValidate('regEmail', RULES.regEmail)) valid = false
  }
  if (!Auth.submitValidate('regPassword', RULES.regPassword)) valid = false
  if (!Auth.submitValidate('regPasswordConfirm', RULES.regPasswordConfirm)) valid = false
  if (!valid) return

  if (oauthMode.value) return doOAuthRegister()

  const u = ($('regUsername') || {}).value.trim()
  const e = ($('regEmail') || {}).value.trim()
  const p = ($('regPassword') || {}).value.trim()
  const btn = $('regSubmitBtn')
  Auth.setBtnLoading(btn, true)

  API.post('/auth/register/send-code', { username: u, email: e, password: p })
    .then(() => {
      Auth.setBtnLoading(btn, false)
      Auth.showToast(t('app.login.toast_code_sent_hint', '验证码已发送'))
      codeStep.value = true
      $('regEmail') && ($('regEmail').disabled = true)
      $('regPassword') && ($('regPassword').disabled = true)
      $('regPasswordConfirm') && ($('regPasswordConfirm').disabled = true)
      setTimeout(() => $('regCode') && $('regCode').focus(), 100)
      Auth.startCountdown('regSendBtn')
    })
    .catch((err) => {
      Auth.setBtnLoading(btn, false)
      const code = (err && err.error_code) || ''
      const field = { USERNAME_INVALID: 'regUsername', USERNAME_TAKEN: 'regUsername', EMAIL_INVALID: 'regEmail', EMAIL_NOT_ALLOWED: 'regEmail', PASSWORD_WEAK: 'regPassword' }[code]
      const msg = t('app.error.' + code, t('common.server_error', (err && err.message) || t('app.login.toast_send_failed', '发送失败')))
      if (field) Auth.showFieldError(field, msg)
      else Auth.showToast(msg)
    })
}

function backToStep1() {
  codeStep.value = false
  $('regEmail') && ($('regEmail').disabled = false)
  $('regPassword') && ($('regPassword').disabled = false)
  $('regPasswordConfirm') && ($('regPasswordConfirm').disabled = false)
  if ($('regCode')) $('regCode').value = ''
  if ($('regUsername')) $('regUsername').focus()
}

function resendCode() {
  const u = ($('regUsername') || {}).value.trim()
  const e = ($('regEmail') || {}).value.trim()
  const p = ($('regPassword') || {}).value.trim()
  API.post('/auth/register/send-code', { username: u, email: e, password: p })
    .then(() => {
      Auth.showToast(t('app.login.toast_code_resent', '验证码已重新发送'))
      Auth.startCountdown('regSendBtn')
    })
    .catch((err) => Auth.showToast((err && err.message) || t('app.login.toast_send_failed', '发送失败')))
}

function registerVerify() {
  // 无邮箱模式（含 OAuth 关联）跳过验证码校验；有邮箱时第二步校验
  let valid = true
  if (!Auth.submitValidate('regUsername', RULES.regUsername)) valid = false
  if (!Auth.submitValidate('regPassword', RULES.regPassword)) valid = false
  if (!Auth.submitValidate('regPasswordConfirm', RULES.regPasswordConfirm)) valid = false
  if (!valid) return

  const u = ($('regUsername') || {}).value.trim()
  const e = ($('regEmail') || {}).value.trim()
  const p = ($('regPassword') || {}).value.trim()
  const pc = ($('regPasswordConfirm') || {}).value.trim()
  const c = ($('regCode') || { value: '' }).value.trim()
  Auth.clearFieldError('regCode')
  if (emailConfigured.value && !oauthMode.value) {
    if (!c) {
      Auth.showFieldError('regCode', t('app.login.err_code_required', '请输入验证码'))
      if ($('regCode')) $('regCode').focus()
      return
    }
    if (c.length !== 6) {
      Auth.showFieldError('regCode', t('app.login.err_code_format', '验证码为 6 位数字'))
      return
    }
  }
  if (p !== pc) {
    Auth.showToast(t('app.login.err_confirm_mismatch', '两次输入的密码不一致'))
    return
  }
  const btn = codeStep.value ? $('regVerifyBtn') : $('regSubmitBtn')
  Auth.setBtnLoading(btn, true)
  API.post('/auth/register/verify', { username: u, email: e, password: p, code: c, lang: G.I18N.getLang() })
    .then((d) => {
      Auth.setBtnLoading(btn, false)
      showSuccess('app.login.toast_register_success')
      localStorage.setItem('token', d.token)
      localStorage.setItem('user', JSON.stringify(d.user))
      setTimeout(() => (window.location.href = '/'), 2000)
    })
    .catch((err) => {
      Auth.setBtnLoading(btn, false)
      const code = (err && err.error_code) || ''
      const field = { CODE_INVALID: 'regCode', USERNAME_INVALID: 'regUsername', USERNAME_TAKEN: 'regUsername', EMAIL_ALREADY_REGISTERED: 'regEmail', PASSWORD_WEAK: 'regPassword' }[code]
      const msg = t('app.error.' + code, t('common.server_error', (err && err.message) || t('app.login.toast_register_failed', '注册失败')))
      if (field) {
        Auth.showFieldError(field, msg)
        if (field === 'regCode' && $('regCode')) $('regCode').focus()
      } else {
        Auth.showToast(msg)
      }
    })
}

function showSuccess(titleKey) {
  success.value = true
  successTitle.value = t(titleKey, '注册成功')
  successMsg.value = t('app.login.toast_logging_in', '正在登录...')
  tab.value = 'register'
  // 成功态覆盖表单：与旧页一致，隐藏表单/步骤/OAuth 标签
  if ($('registerPanel')) $('registerPanel').style.display = 'none'
  if ($('associatePanel')) $('associatePanel').style.display = 'none'
  if ($('stepIndicator')) $('stepIndicator').style.display = 'none'
  if ($('oauthTabs')) $('oauthTabs').style.display = 'none'
}

function doOAuthRegister() {
  const u = ($('regUsername') || {}).value.trim()
  const p = ($('regPassword') || {}).value.trim()
  const btn = $('regSubmitBtn')
  Auth.setBtnLoading(btn, true)
  API.post('/auth/login/' + oauthProvider + '/register', { state: oauthState, username: u, password: p, lang: G.I18N.getLang() })
    .then((d) => {
      Auth.setBtnLoading(btn, false)
      localStorage.setItem('token', d.token)
      localStorage.setItem('user', JSON.stringify(d.user))
      showSuccess('app.login.toast_associate_success')
      setTimeout(() => (window.location.href = '/'), 2000)
    })
    .catch((err) => {
      Auth.setBtnLoading(btn, false)
      const code = (err && err.error_code) || ''
      const field = { USERNAME_INVALID: 'regUsername', USERNAME_TAKEN: 'regUsername' }[code]
      const msg = t('app.error.' + code, t('common.server_error', (err && err.message) || t('app.login.toast_register_failed', '注册失败')))
      if (field) Auth.showFieldError(field, msg)
      else Auth.showToast(msg)
    })
}

function associateAccount() {
  let valid = true
  if (!Auth.submitValidate('assocIdentifier', (v) => (v ? null : t('app.login.err_identifier_required', '请输入用户名或邮箱')))) valid = false
  if (!Auth.submitValidate('assocPassword', (v) => (v ? null : t('app.login.err_password_required', '请输入密码')))) valid = false
  if (!valid) return
  const idf = ($('assocIdentifier') || {}).value.trim()
  const pwd = ($('assocPassword') || {}).value.trim()
  const btn = $('assocBtn')
  Auth.setBtnLoading(btn, true)
  API.post('/auth/login/' + oauthProvider + '/associate', { state: oauthState, identifier: idf, password: pwd })
    .then((d) => {
      Auth.setBtnLoading(btn, false)
      localStorage.setItem('token', d.token)
      localStorage.setItem('user', JSON.stringify(d.user))
      showSuccess('app.login.toast_associate_success')
      setTimeout(() => (window.location.href = '/'), 2000)
    })
    .catch((err) => {
      Auth.setBtnLoading(btn, false)
      const code = (err && err.error_code) || ''
      const i18nKey = { CREDENTIAL_INCORRECT: 'app.login.credential_incorrect', ACCOUNT_DEACTIVATED: 'app.login.account_deactivated', ACCOUNT_DEACTIVATING: 'app.login.account_deactivating' }[code]
      if (i18nKey) {
        const localized = t(i18nKey, '')
        Auth.showFieldError('assocIdentifier', localized && localized !== i18nKey ? localized : (err && err.message))
      } else {
        Auth.showToast((err && err.message) || t('app.login.toast_associate_failed', '关联失败'))
      }
    })
}

onMounted(() => {
  // 语言包晚到/已就绪两种场景都刷一次 data-i18n（组件文案已渲染）
  if (G.I18N && G.I18N.applyI18nToDOM) G.I18N.applyI18nToDOM()
  // 语言切换后动态文案（密码强度标签等）需要重算
  G.addEventListener && G.addEventListener('grape:langchange', () => {
    lang.value = (G.I18N && G.I18N.getLang && G.I18N.getLang()) || lang.value
    const pwdEl = $('regPassword')
    if (pwdEl && pwdEl.value) onPwdInput()
  })
  if (!oauthMode.value) {
    const el = $('regUsername')
    if (el) setTimeout(() => el.focus(), 50)
  }
})

defineExpose({ tab, codeStep, success })
</script>