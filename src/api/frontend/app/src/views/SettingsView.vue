<template>
  <div class="sv">
    <div class="cd">
      <div class="cd-h">
        <h2>{{ t('settings.host.title') }}</h2>
      </div>
      <template v-if="!repo">
        <div class="ts t2">{{ t('settings.host.select_repo_first') }}</div>
      </template>
      <template v-else-if="!loaded">
        <div class="loading">{{ t('settings.common.loading') }}</div>
      </template>
      <template v-else-if="error">
        <div class="error">{{ t('settings.common.load_failed', { msg: error }) }}</div>
      </template>
      <template v-else>
        <div class="cd">
          <h3 class="sv__bot-title">{{ t('settings.credentials.devops_bot_title') }}</h3>
          <div v-if="data.bot_token_status === 'expired'" class="sv__expired">
            ⚠️ {{ t('app.error.REPO_TOKEN_EXPIRED') }}
          </div>
          <div class="srow">
            <label>{{ t('settings.credentials.bot_token') }}</label>
            <input v-model="botToken" type="password" :placeholder="botPlaceholder" />
            <span v-if="tokenBadge" class="ts sv__badge" :style="{ color: tokenBadge.color }">{{
              tokenBadge.text
            }}</span>
          </div>
          <div class="srow">
            <label>{{ t('settings.credentials.scope') }}</label>
            <input
              type="text"
              value="read_repo, write_repo, manage_webhook"
              disabled
              class="sv__scope"
            />
          </div>
          <button class="bt bt-p mt8" @click="save">{{ t('settings.common.save') }}</button>
        </div>
        <div class="cd mt16 sv__unhost">
          <div class="cd-h">
            <h2 class="sv__unhost-title">{{ t('settings.host.unhost_title') }}</h2>
          </div>
          <p class="tx t2 sv__unhost-warn">{{ t('settings.host.unhost_warning') }}</p>
          <button class="bt sv__unhost-btn" @click="unhost">
            {{ t('settings.host.unhost_title') }}
          </button>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup>
// Batch 14 (settings): equivalent of legacy settings.js renderSettings('gitcode') — the only live path.
// Theme/lang/pipeline/api-keys code in settings.js was dead (UI moved to AccountView in batch 7);
// i18n lang-switch rerender for 'settings' view calls renderSettings via the entry global.
// initialTab prop is accepted for contract parity (entry passes it) but the only live tab is 'gitcode'.
import { computed, onMounted, ref } from 'vue'

const G = globalThis

const repo = ref(null)
const loaded = ref(false)
const error = ref('')
const data = ref({})
const botToken = ref('')

function t(key, params) {
  return G.I18N.t(key, params)
}
// tOr pattern (batch 13): t() returns the key itself on miss — never use || fallback
function tOr(key, fallback) {
  const v = G.I18N.t(key)
  return v === key ? fallback : v
}

const botPlaceholder = computed(() => {
  const d = data.value || {}
  const lv = d.resolved_token_level || ''
  if (d.has_repo_bot_token || lv === 'repo_bot')
    return t('settings.credentials.bot_token_placeholder_configured')
  if (lv === 'user')
    return tOr(
      'settings.credentials.user_token_placeholder',
      t('settings.credentials.bot_token_placeholder_configured')
    )
  if (lv === 'global_bot')
    return tOr(
      'settings.credentials.global_bot_placeholder',
      t('settings.credentials.bot_token_placeholder_configured')
    )
  return t('settings.credentials.bot_token_placeholder_empty')
})

const tokenBadge = computed(() => {
  const d = data.value || {}
  const lv = d.resolved_token_level || ''
  if (d.has_repo_bot_token)
    return { text: t('settings.credentials.bot_configured'), color: 'var(--g)' }
  if (lv === 'user')
    return {
      text: tOr('settings.credentials.user_token_active', 'User token active'),
      color: 'var(--a)',
    }
  if (lv === 'global_bot')
    return {
      text: tOr('settings.credentials.global_bot_active', 'Global bot token active'),
      color: 'var(--a)',
    }
  if (lv === 'session')
    return {
      text: tOr('settings.credentials.session_token_active', 'Session token active'),
      color: 'var(--a)',
    }
  return null
})

// placeholder constants kept in sync with backend _is_placeholder (double guard, as in legacy)
const CRED_PLACEHOLDERS = [
  'AK-demo-access-key-xxxx',
  'SK-demo-secret-key-xxxx',
  'ghp_devopsbot_xxxxxxxxxxxxxxxxxxxx',
]
function isCredPlaceholder(v) {
  v = (v || '').trim()
  if (!v) return true
  if (CRED_PLACEHOLDERS.indexOf(v) >= 0) return true
  return /^(AK-demo-|SK-demo-|ghp_devopsbot_)/.test(v)
}

function save() {
  const r = repo.value
  if (!r) return
  const body = {}
  const val = botToken.value ? botToken.value.trim() : ''
  if (val && !isCredPlaceholder(val)) body.bot_token = val
  G.API.put('/settings/gitcode-repo/' + r.owner + '/' + r.name, body)
    .then((r2) => {
      G.app.showToast(t('settings.credentials.repo_config_updated'))
      if (r2.warning) G.app.showToast(r2.warning, 'warning')
      if (r2.scope_warnings && r2.scope_warnings.length)
        r2.scope_warnings.forEach((w) => G.app.showToast(w, 'warning'))
    })
    .catch(() => {
      G.app.showToast(t('settings.common.update_failed'))
    })
}

function unhost() {
  const fullName = repo.value.fullName
  G.app.showConfirm(
    t('settings.host.confirm_remove', { name: fullName }),
    function () {
      G.API.post('/repos/remove', { full_name: fullName })
        .then(() => {
          G.app.showToast(t('settings.host.unhost_done'))
          // R4: clear repo caches (same as legacy)
          try {
            localStorage.removeItem('repo_member_cache')
          } catch (e) {
            /* ignore */
          }
          const cur = G.app.currentRepo()
          const wasCurrent = cur && cur.fullName === fullName
          if (wasCurrent) sessionStorage.removeItem('currentRepo')
          G.app.initSidebarRepos()
          G.API.get('/repos/managed').then((md) => {
            const remaining = md.repos || []
            if (wasCurrent || remaining.length === 0) {
              G.API.get('/repos/member').then((d) => {
                const sidebar = document.getElementById('sidebar')
                const tb = document.getElementById('tb')
                const mv = document.getElementById('mainView')
                if (sidebar) sidebar.style.display = 'none'
                if (tb) tb.style.display = 'none'
                if (mv) {
                  mv.style.display = 'flex'
                  mv.style.position = 'fixed'
                  mv.style.inset = '0'
                  mv.style.zIndex = '100'
                }
                const title = document.getElementById('pageTitle')
                if (title) title.textContent = t('settings.host.select_repo')
                G.window.renderRepoSelection((d && d.repos) || [])
              })
            } else {
              G.app.nav('dash')
            }
          })
        })
        .catch((e) => G.app.showToast(t('settings.host.unhost_failed', { msg: e.message })))
    },
    { danger: true }
  )
}

onMounted(() => {
  try {
    repo.value = G.app.currentRepo()
  } catch (e) {
    repo.value = null
  }
  const r = repo.value
  if (!r) return // no current repo → select_repo_first hint (legacy behavior)
  G.API.get('/settings/gitcode-repo/' + r.owner + '/' + r.name)
    .then((d) => {
      data.value = d
      loaded.value = true
    })
    .catch((e) => {
      error.value = e.message
      loaded.value = true
    })
})
</script>

<style scoped>
.sv__bot-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 12px;
}
.sv__expired {
  padding: 8px 12px;
  background: rgba(255, 59, 48, 0.1);
  border-radius: 6px;
  margin-bottom: 8px;
  color: #ff3b30;
  font-size: 12px;
}
.sv__badge {
  font-size: 10px;
}
.sv__scope {
  background: #f1f5f9;
  font-size: 10px;
}
.sv__unhost {
  border-color: var(--r);
}
.sv__unhost-title {
  color: var(--r);
}
.sv__unhost-warn {
  margin-bottom: 8px;
  color: var(--r);
}
.sv__unhost-btn {
  background: var(--r);
  color: #fff;
  border: none;
  padding: 8px 16px;
}
</style>
