<!-- PAT 配置弹窗（仓库平台凭据域，Vue 组件）。
     只做 PAT：令牌输入 + 平台令牌页直达 + 帮助；写入 /settings/credentials（空串=清除，
     清除走行级 clear）。账号绑定（身份）不在这里做——那是 OAuth 域。 -->
<template>
  <teleport to="body">
    <div v-if="open" class="pd__mask" @click.self="emit('close')">
      <div class="pd__box">
        <h3 class="pd__title">
          <span v-html="icon"></span>
          <span>{{ t('pat_btn_configure') + ' · ' + name }}</span>
        </h3>
        <div v-if="betaHint" class="pd__beta">
          <span class="pd__betatext">{{ t('beta_hint_text') }}</span>
          <button type="button" class="bt bt-s bt-o pd__betabtn" @click="openBetaApply">
            {{ t('beta_hint_apply') }}
          </button>
        </div>
        <div v-if="!oauthConfigured" class="pd__note">{{ t('pat_no_refresh_hint') }}</div>
        <a v-if="effectivePatUrl" class="bt pd__link" :href="effectivePatUrl" target="_blank" rel="noopener noreferrer">
          {{ t('open_token_page') + (patHost ? ' ' + patHost : '') }}
        </a>
        <div class="srow">
          <label>{{ t('access_token') }}</label>
          <input v-model="token" type="password" class="kb-input" :placeholder="t('paste_token')" />
        </div>
        <div v-if="error" class="pd__err">{{ error }}</div>
        <details class="pd__help">
          <summary>{{ t('how_to_get_token') }}</summary>
          <div>{{ helpSteps }}</div>
        </details>
        <button class="bt bt-p pd__wide" :disabled="busy" @click="save()">
          {{ busy ? t('verifying') : t('save') }}
        </button>
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
  name: { type: String, default: '' },
  patUrl: { type: String, default: '' },
  oauthConfigured: { type: Boolean, default: false },
  betaHint: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'saved'])

function t(key, vars) {
  const full = 'app.account.' + key
  const got = G.I18N && G.I18N.t ? G.I18N.t(full, vars) : full
  return got && got !== full ? got : key
}
const icon = computed(function () {
  return typeof G.window.platformIcon === 'function' ? G.window.platformIcon(props.provider, 24) : ''
})
// 官方实测过的预选参数链接优先（与 BindDialog 同源），其余取平台卡 pat_create_url
const PAT_CREATE_URLS = {
  github: 'https://github.com/settings/tokens/new?scopes=repo&description=Grape%20DevOps',
  gitcode: 'https://gitcode.com/setting/token-classic/create',
}
const effectivePatUrl = computed(function () {
  return PAT_CREATE_URLS[props.provider] || props.patUrl || ''
})
const patHost = computed(function () {
  return effectivePatUrl.value ? effectivePatUrl.value.split('/')[2] || '' : ''
})
const helpKey = computed(function () {
  return { github: 'github_token_help', gitcode: 'gitcode_token_help', gitee: 'gitee_token_help' }[
    props.provider
  ] || 'pat_token_help_generic'
})
const helpSteps = computed(function () {
  return helpKey.value === 'pat_token_help_generic'
    ? t(helpKey.value, { host: patHost.value || props.name })
    : t(helpKey.value)
})

const token = ref('')
const error = ref('')
const busy = ref(false)

function openBetaApply() {
  if (G.app && typeof G.app.openBetaApply === 'function') {
    G.app.openBetaApply()
    return
  }
  G.window.location.href = '/static-vue/beta-apply'
}

watch(
  function () {
    return props.open
  },
  function (v) {
    if (v) {
      token.value = ''
      error.value = ''
      busy.value = false
    }
  }
)

function save() {
  const val = (token.value || '').trim()
  if (!val) {
    error.value = t('please_enter_token')
    return
  }
  busy.value = true
  error.value = ''
  const body = { platform_tokens: {} }
  body.platform_tokens[props.provider] = val
  G.API.put('/settings/credentials', body, 15000)
    .then(function () {
      busy.value = false
      emit('saved', props.provider)
    })
    .catch(function (e) {
      busy.value = false
      const code = (e && e.detail && typeof e.detail === 'object' ? e.detail.code : 0) || (e && e.status) || 0
      if (code === 401) error.value = t('token_invalid_expired', { platform: props.name })
      else if (code === 502) error.value = t('network_error_retry')
      else if (code === 400) error.value = t('token_format_invalid')
      else error.value = (e && e.message) || t('bind_failed')
    })
}
</script>

<style scoped>
/* 自带头部外框（不依赖宿主页面 scoped CSS；与 BindDialog 同尺寸/同观感） */
.pd__mask {
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
.pd__box {
  width: 440px;
  max-width: 92vw;
  background: var(--c);
  border-radius: 16px;
  padding: 26px;
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.12);
}
.pd__box h3 {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 12px;
}
.pd__title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.pd__note {
  margin-bottom: 12px;
  color: var(--t2);
  font-size: 12px;
  line-height: 1.6;
}
/* Issue #4254：自注册非白名单用户内测提示（非阻断） */
.pd__beta {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
  padding: 10px 12px;
  border-radius: 12px;
  background: rgba(255, 159, 10, 0.1);
  box-shadow: inset 0 0 0 1px rgba(255, 159, 10, 0.28);
}
.pd__betatext {
  font-size: 12px;
  color: var(--t2);
  line-height: 1.6;
}
.pd__betabtn {
  align-self: flex-start;
}
.pd__link {
  display: block;
  margin: 0 0 12px;
  text-align: center;
  text-decoration: none;
  color: var(--a);
  border: 1px solid var(--a);
}
.pd__err {
  padding: 4px 0;
  color: var(--r);
  font-size: 12px;
}
.pd__help {
  margin: 8px 0;
  font-size: 12px;
  color: var(--t2);
}
.pd__help summary {
  cursor: pointer;
  color: var(--a);
}
.pd__wide {
  width: 100%;
}
</style>