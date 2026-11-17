<template>
  <div class="auth-card" id="loginCard">
    <img class="logo" :src="logoUrl" alt="Grape" />
    <h1>Grape</h1>
    <p class="subtitle" data-i18n="app.login.register_sub">{{ t('app.login.register_sub', 'AI Native DevOps') }}</p>

    <!-- 登录表单：保持旧壳的 id/name 契约，app.js 的 pwdLogin/校验函数按 id 取元素，行为零改动 -->
    <form id="pwdLogin" @submit.prevent="onSubmit">
      <div class="field-group">
        <label class="field-label" for="loginUser" data-i18n="app.login.account_or_email">{{ t('app.login.account_or_email', '用户名或邮箱') }}</label>
        <div class="field-input-wrap">
          <input
            class="field-input"
            id="loginUser"
            type="text"
            :placeholder="t('app.login.account_or_email', '用户名或邮箱')"
            data-i18n-placeholder="app.login.account_or_email"
            autocomplete="username"
            autocapitalize="none"
            autocorrect="off"
            aria-describedby="loginUserErr"
            @input="G.app._clearLoginError('loginUser')"
            @blur="G.app._blurValidateLogin('loginUser')"
          />
        </div>
        <p class="field-error" id="loginUserErr" role="alert" aria-live="polite"></p>
      </div>
      <div class="field-group">
        <label class="field-label" for="loginPass" data-i18n="app.login.password">{{ t('app.login.password', '密码') }}</label>
        <div class="field-input-wrap">
          <input
            class="field-input"
            id="loginPass"
            type="password"
            :placeholder="t('app.login.password', '密码')"
            data-i18n-placeholder="app.login.password"
            autocomplete="current-password"
            enterkeyhint="go"
            aria-describedby="loginPassErr"
            @input="G.app._clearLoginError('loginPass')"
            @blur="G.app._blurValidateLogin('loginPass')"
          />
          <button
            type="button"
            class="pwd-toggle"
            :aria-label="t('app.login.show_password', '显示密码')"
            data-i18n-aria="app.login.show_password"
            @click="togglePwd($event)"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
              <circle cx="12" cy="12" r="3" />
            </svg>
          </button>
        </div>
        <p class="field-error" id="loginPassErr" role="alert" aria-live="polite"></p>
      </div>
      <button class="auth-btn" id="loginBtn" type="submit">
        <span data-i18n="app.login.login_btn">{{ t('app.login.login_btn', '登录') }}</span>
      </button>
    </form>

    <!-- 主链接（忘记密码按服务端邮箱配置显隐，零闪动：注入值同步读取） -->
    <div class="auth-links">
      <a href="/register" data-i18n="app.login.register">{{ t('app.login.register', '注册账号') }}</a>
      <span class="dot" id="forgotDot" v-show="emailConfigured">·</span>
      <a href="/forgot" id="forgotLink" data-i18n="app.login.forgot_password" v-show="emailConfigured">{{ t('app.login.forgot_password', '忘记密码') }}</a>
    </div>

    <!-- OAuth：口径 v7（2026-11-12 拍板）——纯 logo 圆形图标横排，平台名走 title/aria -->
    <div id="oauthSection" style="margin-top: 20px">
      <div style="margin: 0 0 16px; text-align: center; position: relative">
        <div style="position: absolute; top: 50%; left: 0; right: 0; height: 1px; background: rgba(255, 255, 255, 0.08)"></div>
        <span
          style="position: relative; background: transparent; padding: 0 16px; font-size: 11px; color: #6e6e73; letter-spacing: 0.5px"
          data-i18n="app.login.or_oauth"
          >{{ t('app.login.or_oauth', '或使用第三方登录') }}</span
        >
      </div>
      <div style="display: flex; gap: 10px" id="oauthBtns">
        <button
          v-for="p in providers"
          :key="p.provider"
          class="oauth-btn icon-only"
          :data-provider="p.provider"
          :style="brandStyle(p)"
          :title="loginTitle(p)"
          :aria-label="loginTitle(p)"
          :aria-disabled="p.configured ? null : 'true'"
          @click="G.app.oauthLogin(p.provider)"
          v-html="iconHtml(p)"
        ></button>
      </div>
      <!-- 未配置常显说明（#535）：全部已配置时隐藏；文案按真实名单点名 -->
      <div id="oauthUnconfiguredHint" class="oauth-unconfigured-hint" v-show="unconfigured.length">
        <span :data-i18n="hintKey" :data-i18n-vars="hintVars">{{ hintText }}</span>
        ·
        <a :href="helpUrl" target="_blank" rel="noopener"
          ><span data-i18n="app.login.oauth_view_config_help">{{ t('app.login.oauth_view_config_help', '查看配置帮助') }}</span></a
        >
      </div>
    </div>

    <!-- 次要链接 -->
    <div class="auth-links" style="margin-top: 16px">
      <a @click="G.app.openBetaApply()" style="cursor: pointer; color: #6e6e73" data-i18n="app.login.beta_apply">{{
        t('app.login.beta_apply', '申请内测')
      }}</a>
      <span class="dot">·</span>
      <a href="/admin" style="cursor: pointer; color: #6e6e73; font-size: 12px" data-i18n="app.login.admin">{{ t('app.login.admin', '管理后台') }}</a>
    </div>

    <!-- 语言切换 -->
    <div class="lang-switch" @click="toggleLang">
      <span id="langSwitchLabel">{{ lang === 'zh' ? 'EN' : '中文' }}</span>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'

const G = window
// 静态图走服务端路径（不绑定构建期资源解析：绝对 /static 路径在 vite 里会被当 import 处理）
const logoUrl = '/static/img/grape-logo.jpg?v=1'
const lang = ref((G.I18N && G.I18N.getLang && G.I18N.getLang()) || localStorage.getItem('grapeLang') || 'zh')

// 目录首帧来源 = 服务端 head 注入（__OAUTH_PROVIDERS__，同步），随后用 /auth/providers 对账，
// 并接受 app.js 的 'grape:oauth-providers' 事件（showLogin 会触发一次）。
const providers = ref(
  Array.isArray(G.__OAUTH_PROVIDERS__)
    ? G.__OAUTH_PROVIDERS__.map((p) => ({ ...p }))
    : []
)
const emailConfigured = !!G.__EMAIL_CONFIGURED__

const NAME_MAP = { hwcloud: '华为云', huawei: '华为云', gitcode: 'GitCode', gitee: 'Gitee', github: 'GitHub', gitlab: 'GitLab' }

function t(key, fallback) {
  // lang 依赖：语言切换时整页文案重算（data-i18n 属性同时保留，供 I18N.applyI18nToDOM 兜底）
  void lang.value
  const v = G.I18N && G.I18N.t ? G.I18N.t(key) : ''
  return v && v !== key ? v : fallback !== undefined ? fallback : key
}
function displayName(p) {
  return NAME_MAP[p.provider] || p.name || p.provider
}
function brandStyle(p) {
  // 品牌色只染图标（D105-5 裁定：不再做品牌实底）。无卡色时给中性白。
  const c = (G._provBrandColor && G._provBrandColor(p.provider)) || p.brand_color || ''
  return c ? { '--brand': c } : {}
}
function iconHtml(p) {
  // 单一图标来源：shell/js/oauth-buttons.js 的 window.__oauthIcon（含 gitee/huawei/gitlab）
  return G.__oauthIcon ? G.__oauthIcon(p.provider, p.name) : ''
}
function loginTitle(p) {
  const isHw = p.provider === 'huawei' || p.provider === 'hwcloud'
  if (isHw) return t('app.login.oauth_title_huawei', '使用华为云账号登录')
  const tpl = t('app.login.oauth_title', '使用 {name} 账号登录')
  return String(tpl).replace('{name}', displayName(p))
}

const unconfigured = computed(() => providers.value.filter((p) => !p.configured))
const hintKey = computed(() => (unconfigured.value.length ? 'app.login.oauth_needs_config' : 'app.login.oauth_needs_config_generic'))
const hintVars = computed(() =>
  unconfigured.value.length ? JSON.stringify({ name: unconfigured.value.map(displayName).join(' / ') }) : null
)
const hintText = computed(() => {
  void lang.value
  const names = unconfigured.value.map(displayName).join(' / ')
  if (!names) return t('app.login.oauth_needs_config_generic', '部分第三方登录需要管理员先配置 OAuth 后才能使用')
  const v = G.I18N && G.I18N.t ? G.I18N.t('app.login.oauth_needs_config', { name: names }) : ''
  return v && v !== 'app.login.oauth_needs_config' ? v : `${names} 登录需要管理员先配置 OAuth 后才能使用`
})
const helpUrl = computed(() => docUrl(unconfigured.value[0] && unconfigured.value[0].provider))

function docUrl(provider) {
  return (
    {
      gitcode: '/static/help.html#/developer/01-gitcode-integration',
      github: '/static/help.html#/developer/02-github-integration',
    }[provider] || '/static/help.html'
  )
}

function onSubmit() {
  G.app.pwdLogin()
}
function togglePwd(ev) {
  G.app._toggleLoginPwd(ev.currentTarget)
}
function toggleLang() {
  const next = (G.I18N && G.I18N.getLang && G.I18N.getLang()) === 'zh' ? 'en' : 'zh'
  if (!G.I18N || !G.I18N.setLang) return
  G.I18N.setLang(next).then(() => {
    lang.value = next
  })
}

function applyProviders(list) {
  if (!Array.isArray(list) || !list.length) return
  providers.value = list.map((p) => ({ ...p }))
}
function onProvidersEvent(ev) {
  applyProviders(ev && ev.detail && ev.detail.providers)
}
function refreshProviders() {
  // 对账：服务端注入是首帧快照，运行时状态以 API 为准（配置可能在页面存活期变更）
  try {
    const api = G.API
    if (api && api.get) {
      api.get('/auth/providers').then((d) => applyProviders(d && d.providers)).catch(() => {})
    }
  } catch (e) {
    /* 目录不可用时保留注入快照 */
  }
}

onMounted(() => {
  window.addEventListener('grape:oauth-providers', onProvidersEvent)
  if (!providers.value.length) refreshProviders()
  else refreshProviders()
  // 语言包晚到 / 已就绪两种场景都刷一次 data-i18n 兜底（Vue 自身文案已渲染）
  if (G.I18N && G.I18N.applyI18nToDOM) setTimeout(() => G.I18N.applyI18nToDOM(), 0)
  const el = document.getElementById('loginUser')
  if (el) setTimeout(() => el.focus(), 100)
  showResetNotice()
})
onBeforeUnmount(() => {
  window.removeEventListener('grape:oauth-providers', onProvidersEvent)
})

// 需求3：重置密码成功后跳回登录页显示提示（?reset=1），清理 URL 参数防重复
function showResetNotice() {
  try {
    const p = new URLSearchParams(location.search)
    if (p.get('reset') !== '1') return
    if (window.history.replaceState) window.history.replaceState(null, '', window.location.pathname)
    const msg = t('app.login.toast_password_reset_notice', '密码已重置，请用新密码登录')
    if (G.app && G.app.showToast) G.app.showToast(msg, '', 5000)
  } catch (e) {
    /* 忽略 */
  }
}

defineExpose({ providers })
</script>

<style>
/* 登录页样式沿用 app/src/styles/global/auth.css（由 entry 注入 viewcss/login-view.css?v=）：
   这里只补 icon-only 按 data-provider 的品牌规则——Vue 渲染的按钮没有 inline onclick 属性，
   旧 CSS 的 [onclick*="oauthLogin('gitee')"] 选择器命中不了。 */
.oauth-btn.icon-only[data-provider='gitee'] svg {
  color: transparent !important;
  fill: transparent !important;
  width: 26px !important;
  height: 26px !important;
}
.oauth-btn.icon-only[data-provider='huawei'] svg,
.oauth-btn.icon-only[data-provider='hwcloud'] svg {
  color: transparent !important;
  fill: transparent !important;
  width: 26px !important;
  height: 26px !important;
}
.oauth-btn.icon-only[data-provider='github'] svg {
  color: #fff !important;
}
</style>