<!-- 绑定弹窗（共享 Vue 组件）：引导页 allowPat=true（OAuth 授权 + 手动令牌 PAT 双模式），
     账号安全页 allowPat=false（纯 OAuth；未开通渠道只给「去凭据管理」指路）。
     原实现是 app.js / AccountView 里的字符串拼 DOM，2026-11-14 按要求统一组件化。 -->
<template>
  <teleport to="body">
    <!-- 自带头部外框：不依赖宿主页面的 scoped 样式（跨组件 scoped class 不生效，
         否则弹层会掉出页面对齐、裸挂在 body 末尾） -->
    <div v-if="visible" class="bd__mask" @click.self="close()">
      <div class="bd__box">
        <h3 class="bd__title">
          <span v-html="icon"></span>
          <span>{{ titleText }}</span>
        </h3>

        <!-- 非引导页上下文 + 未开通 OAuth → 指路凭据管理（不在账号绑定域给 PAT 表单） -->
        <template v-if="!oauthConfigured && !allowPat">
          <p class="bd__hint">{{ t('bind_unavailable_hint') }}</p>
          <button class="bt bt-o bd__wide" @click="goCredentials()">{{ t('go_credentials_btn') }}</button>
        </template>

        <!-- 已开通 OAuth：OAuth 页签（默认）；引导页再给 PAT 页签 -->
        <template v-else>
          <div v-if="oauthConfigured && allowPat" class="stabs bd__tabs">
            <a :class="tab === 'oauth' ? 'ac' : ''" @click="tab = 'oauth'">{{ t('oauth_auth') }}</a>
            <a :class="tab === 'pat' ? 'ac' : ''" @click="tab = 'pat'">{{ t('manual_token') }}</a>
          </div>

          <div v-if="oauthConfigured && tab === 'oauth'">
            <p class="bd__hint">{{ t(alreadyBound ? 'oauth_rebind_hint' : 'oauth_redirect_hint', { platform: name }) }}</p>
            <button class="bt bt-p bd__wide" :disabled="busy" @click="startOauth()">
              {{ busy ? t('verifying') : t(alreadyBound ? 're_authorize' : 'authorize') }}
            </button>
          </div>

          <!-- PAT 表单：引导页随 OAuth 页签出现，或未开通 OAuth 时直达 -->
          <div v-else>
            <div v-if="!oauthConfigured" class="bd__note">{{ t('pat_no_refresh_hint') }}</div>
            <a
              v-if="patUrl"
              class="bt bd__link"
              :href="patUrl"
              target="_blank"
              rel="noopener noreferrer"
              >{{ t('open_token_page') + (patHost ? ' ' + patHost : '') }}</a
            >
            <div class="srow">
              <label>{{ t('access_token') }}</label>
              <input
                v-model="token"
                type="password"
                class="kb-input"
                :placeholder="t('paste_token')"
              />
            </div>
            <div v-if="patError" class="bd__err">{{ patError }}</div>
            <details class="bd__help">
              <summary>{{ t('how_to_get_token') }}</summary>
              <div>{{ helpSteps }}</div>
            </details>
            <button class="bt bt-p bd__wide" :disabled="busy" @click="savePat()">
              {{ busy ? t('verifying') : t('confirm_bind') }}
            </button>
          </div>
        </template>

        <!-- 转移确认（同平台已绑到另一个 Grape 账号） -->
        <template v-if="transferTicket">
          <h3>{{ t('transfer_confirm_title') }}</h3>
          <p class="tx">{{ t('transfer_confirm_msg', { platform: name }) }}</p>
          <p class="tx t2">{{ t('transfer_confirm_desc') }}</p>
          <div v-if="transferError" class="bd__err">{{ transferError }}</div>
          <div class="bd__row">
            <button class="bt bt-o" @click="transferTicket = ''">{{ t('transfer_cancel_btn') }}</button>
            <button class="bt bt-p" :disabled="busy" @click="confirmTransfer()">{{ t('transfer_confirm_btn') }}</button>
          </div>
        </template>
      </div>
    </div>
  </teleport>
</template>

<script setup>
import { computed, ref, watch } from 'vue'

const G = globalThis

const props = defineProps({
  open: { type: Boolean, default: false },
  provider: { type: String, default: '' },
  allowPat: { type: Boolean, default: true },
  alreadyBound: { type: Boolean, default: false },
  providers: { type: Array, default: () => [] },
  // chrome：历史上下文标记（引导页传 'shell'）；外框两种上下文同为 440px 玻璃卡片，
  // 且样式由本组件自带，不依赖宿主页面 scoped CSS。
  chrome: { type: String, default: 'account' },
})
const emit = defineEmits(['close', 'bound'])

// i18n：账号域键走 app.account.*；个别键只存在于 app.login.*（如 bind_success），
// 原实现只查 account.*，取不到就回吐原始 key —— 绑定成功时界面显示 "bind_success"
// 而不是「GitCode 绑定成功」（2026-11-14 实锤）。两处都查，缺则回退 key。
const langVer = ref(0)
function t(key, vars) {
  void langVer.value // 语言切换后触发重渲染（setLang 在语言包就位后派发事件）
  for (const full of ['app.account.' + key, 'app.login.' + key]) {
    const got = G.I18N && G.I18N.t ? G.I18N.t(full, vars) : full
    if (got && got !== full) return got
  }
  return key
}
try {
  G.window.addEventListener('grape:langchange', () => { langVer.value += 1 })
} catch (e) { /* 非浏览器环境忽略 */ }
const info = computed(function () {
  return props.providers.find(function (p) {
    return p.provider === props.provider
  }) || {}
})
const oauthConfigured = computed(function () {
  return !!info.value.configured
})
const name = computed(function () {
  const label = typeof G.window.platformLabel === 'function' ? G.window.platformLabel(props.provider) : ''
  return label || info.value.name || props.provider
})
const icon = computed(function () {
  return typeof G.window.platformIcon === 'function' ? G.window.platformIcon(props.provider, 24) : ''
})
const titleText = computed(function () {
  return t(props.alreadyBound ? 'update_platform_token' : 'link_platform', { platform: name.value })
})

const visible = ref(false)
const tab = ref('oauth')
const token = ref('')
const patError = ref('')
const busy = ref(false)
const transferTicket = ref('')
const transferError = ref('')

// 官方实测过的预选参数链接优先；其余平台取平台卡 pat_create_url；都没有就不渲染按钮
const PAT_CREATE_URLS = {
  github: 'https://github.com/settings/tokens/new?scopes=repo&description=Grape%20DevOps',
  gitcode: 'https://gitcode.com/setting/token-classic/create',
}
const patUrl = computed(function () {
  return PAT_CREATE_URLS[props.provider] || info.value.pat_create_url || ''
})
const patHost = computed(function () {
  return patUrl.value ? patUrl.value.split('/')[2] || '' : ''
})
const helpKey = computed(function () {
  return { github: 'github_token_help', gitcode: 'gitcode_token_help', gitee: 'gitee_token_help' }[
    props.provider
  ] || 'pat_token_help_generic'
})
const helpSteps = computed(function () {
  return helpKey.value === 'pat_token_help_generic'
    ? t(helpKey.value, { host: patHost.value || name.value })
    : t(helpKey.value)
})

watch(
  function () {
    return props.open
  },
  function (v) {
    visible.value = !!v
    if (v) {
      tab.value = oauthConfigured.value ? 'oauth' : 'pat'
      token.value = ''
      patError.value = ''
      transferTicket.value = ''
      transferError.value = ''
      busy.value = false
    }
  },
  { immediate: true }
)

function close() {
  visible.value = false
  emit('close')
}

function goCredentials() {
  close()
  // 壳内没有 hashchange 监听：必须走 nav()（param = tab）
  if (G.app && typeof G.app.nav === 'function') G.app.nav('account', 'credentials')
}

function bindOk() {
  close()
  const msg = t('bind_success', { label: name.value || props.provider })
  if (G.app && G.app.showToast) G.app.showToast(msg, '', 2500)
  try {
    G.window.localStorage.removeItem('repo_member_cache')
  } catch (e) {
    /* 缓存失效失败不阻断主流程 */
  }
  emit('bound', props.provider)
}

// 关系行是服务端写的（hooks.on_pat_saved / on_oauth_bound），前端登录快照必须重拉：
// 否则「刚配完 PAT / 刚绑定完」仍被引导门按旧快照拦回引导页（实弹）。
function refreshRelations() {
  if (G.app && typeof G.app.refreshRepoLinks === 'function') return G.app.refreshRepoLinks()
  return Promise.resolve(null)
}

function popupOpen(url) {
  const w = 600
  const h = 700
  const left = Math.round(((G.window.screenLeft != null ? G.window.screenLeft : G.window.screenX) || 0) + ((G.window.innerWidth || 1440) - w) / 2)
  const top = Math.round(((G.window.screenTop != null ? G.window.screenTop : G.window.screenY) || 0) + ((G.window.innerHeight || 900) - h) / 2)
  return G.window.open(url, 'oauth_popup', 'width=' + w + ',height=' + h + ',left=' + left + ',top=' + top + ',resizable=yes,scrollbars=yes,status=yes')
}

function startOauth() {
  const prompt = props.alreadyBound ? '&prompt=login' : ''
  const mobile = /Mobi|Android|iPhone|iPad|iPod/i.test(G.window.navigator.userAgent || '')
  if (mobile) {
    G.API.get('/auth/bind/' + props.provider + (prompt ? '?prompt=login' : '')).then(function (d) {
      if (d.redirect_url) G.window.location.href = d.redirect_url
    })
    return
  }
  const popup = popupOpen('')
  if (!popup) {
    G.API.get('/auth/bind/' + props.provider + (prompt ? '?prompt=login' : '')).then(function (d) {
      if (d.redirect_url) G.window.location.href = d.redirect_url
    })
    return
  }
  busy.value = true
  G.API.get('/auth/bind/' + props.provider + '?popup=true' + prompt)
    .then(function (d) {
      if (!d.redirect_url) throw new Error('no_redirect_url')
      popup.location.href = d.redirect_url
      let done = false
      let pollId = null
      let timeoutId = null
      function cleanup() {
        if (pollId) clearInterval(pollId)
        if (timeoutId) clearTimeout(timeoutId)
        G.window.removeEventListener('message', onMsg)
      }
      function finish(ok, errCode) {
        if (done) return
        done = true
        cleanup()
        busy.value = false
        if (ok) refreshRelations().then(function () { bindOk() })
        else if (G.app && G.app.showToast) G.app.showToast(errCode || t('bind_failed'))
      }
      function onMsg(ev) {
        if (ev.origin !== G.window.location.origin) return
        const d2 = ev.data || {}
        if (d2.type !== 'oauth_result' || d2.action !== 'bind') return
        if (d2.success) finish(true)
        else if (d2.need_transfer) {
          done = true
          cleanup()
          busy.value = false
          G.API.get('/auth/me').then(function (me) {
            const id = (me.identities || []).find(function (x) {
              return x.provider === props.provider
            })
            if (id && id.binding_ticket) transferTicket.value = id.binding_ticket
          })
        } else finish(false, d2.error)
      }
      G.window.addEventListener('message', onMsg)
      pollId = setInterval(function () {
        if (done) return
        if (popup.closed) {
          G.API.get('/auth/me').then(function (me) {
            finish((me.bindings || []).indexOf(props.provider) >= 0)
          }).catch(function () {
            finish(false)
          })
        }
      }, 800)
      timeoutId = setTimeout(function () {
        finish(false, t('oauth_timeout_hint'))
      }, 60000)
    })
    .catch(function () {
      try {
        popup.close()
      } catch (e) {
        /* 弹窗已关 */
      }
      busy.value = false
      if (G.app && G.app.showToast) G.app.showToast(t('popup_blocked_hint'))
    })
}

function translateError(e) {
  const code = (e && e.detail && typeof e.detail === 'object' ? e.detail.code : 0) || (e && e.status) || 0
  if (code === 401) return t('token_invalid_expired', { platform: name.value })
  if (code === 409) return t('already_bound_to_another')
  if (code === 502) return t('network_error_retry')
  if (code === 400) return t('token_format_invalid')
  return (e && e.message) || t('bind_failed')
}

function savePat() {
  const val = (token.value || '').trim()
  if (!val) {
    patError.value = t('please_enter_token')
    return
  }
  busy.value = true
  patError.value = ''
  const body = { platform_tokens: {} }
  body.platform_tokens[props.provider] = val
  G.API.put('/settings/credentials', body, 15000)
    .then(function () {
      busy.value = false
      return refreshRelations()
    })
    .then(function () {
      bindOk()
    })
    .catch(function (e) {
      busy.value = false
      const det = (e && e.detail) || {}
      if (det && det.need_transfer && det.binding_ticket) {
        transferTicket.value = det.binding_ticket
        return
      }
      patError.value = translateError(e)
    })
}

function confirmTransfer() {
  busy.value = true
  transferError.value = ''
  G.API.post('/auth/transfer-bind', { binding_ticket: transferTicket.value, confirm: true })
    .then(function () {
      busy.value = false
      bindOk()
    })
    .catch(function (e) {
      busy.value = false
      transferError.value = (e && e.message) || t('bind_failed')
    })
}
</script>

<style scoped>
.bd__mask {
  position: fixed;
  inset: 0;
  z-index: 1001;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(0, 0, 0, 0.35);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
}
.bd__box {
  width: 440px;
  max-width: 92vw;
  background: var(--c);
  border-radius: 16px;
  padding: 26px;
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.12);
}
.bd__box h3 {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 12px;
}
.bd__title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.bd__tabs {
  margin-bottom: 16px;
}
.bd__hint {
  margin-bottom: 16px;
  line-height: 1.6;
}
.bd__wide {
  width: 100%;
}
.bd__note {
  margin-bottom: 12px;
  padding: 12px 14px;
  border: 1px solid var(--b);
  border-radius: 10px;
  background: var(--bg);
  color: var(--t2);
  font-size: 12px;
  line-height: 1.6;
}
.bd__link {
  display: block;
  margin: 0 0 12px;
  text-align: center;
  text-decoration: none;
  color: var(--a);
  border: 1px solid var(--a);
}
.bd__err {
  padding: 4px 0;
  color: var(--r);
  font-size: 12px;
}
.bd__help {
  margin: 8px 0;
  font-size: 12px;
  color: var(--t2);
}
.bd__help summary {
  cursor: pointer;
  color: var(--a);
}
.bd__row {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
</style>