<template>
  <section class="ap-page">
    <!-- 列表：Apple 设置式分组列表，一行一个渠道 -->
    <template v-if="!current">
      <p class="ap-sub">
        {{ t('page.channels_list_sub') }}
        <AGHelp
          :title="t('page.help_title')"
          :what="t('page.channels_hint')"
          values="GET /api/admin/oauth-channels → PUT /{channel}/credentials、/{channel}/enabled"
          :note="t('page.channels_note')"
        />
      </p>
      <div class="ap-group">
        <div v-for="c in channels" :key="c.provider" class="ap-row clickable" @click="open(c)">
          <img v-if="logoFor(c.provider)" class="ap-logo" :src="logoFor(c.provider)" :alt="channelName(c)" />
          <div class="ap-main">
            <div class="ap-title">{{ channelName(c) }}</div>
            <div class="ap-desc">{{ c.provider }}</div>
          </div>
          <AGPill :tone="c.enabled ? 'ok' : 'mute'">{{ c.enabled ? t('page.pill_on') : t('page.pill_off') }}</AGPill>
          <span class="ap-chev">›</span>
        </div>
      </div>
      <p v-if="!loading && !channels.length" class="ap-hint">{{ t('page.channels_empty') }}</p>
      <p v-if="loading" class="ap-hint">{{ t('page.loading') }}</p>
      <p v-if="error" class="ap-err">{{ error }}</p>
    </template>

    <!-- 详情 -->
    <template v-else>
      <div class="ap-back" @click="back()"><span class="ap-chev-l">‹</span> {{ t('page.back') }}</div>
      <div class="ap-detail-hd">
        <img v-if="logoFor(current.provider)" class="ap-logo ap-logo-lg" :src="logoFor(current.provider)" :alt="channelName(current)" />
        <b>{{ channelName(current) }}</b><code class="ap-key">{{ current.provider }}</code>
      </div>
      <p class="ap-sub">{{ t('page.domain_note_channel') }}</p>

      <div class="ap-sect">{{ t('page.sec_state') }}</div>
      <div class="ap-group">
        <div class="ap-row">
          <span class="ap-label">{{ t('page.enabled') }}<AGHelp v-bind="docRow('enabled')" /></span>
          <span class="ap-right">
            <label class="ap-switch">
              <input type="checkbox" :checked="current.enabled" @change="toggleEnabled(current, $event)" />
              <span>{{ current.enabled ? t('page.pill_on') : t('page.pill_off') }}</span>
            </label>
          </span>
        </div>
      </div>

      <div class="ap-sect">{{ t('page.sec_credential') }}</div>
      <div class="ap-group">
        <div class="ap-row">
          <span class="ap-label">{{ t('page.client_id') }}<AGHelp v-bind="docRow('client_id')" /></span>
          <input
            v-model="edit[current.provider].client_id"
            class="ap-input"
            :placeholder="current.credential_present.client_id ? t('page.keep') : t('page.unset')"
          />
        </div>
        <div class="ap-row">
          <span class="ap-label">{{ t('page.client_secret') }}<AGHelp v-bind="docRow('client_secret')" /></span>
          <input
            type="password"
            v-model="edit[current.provider].client_secret"
            class="ap-input"
            :placeholder="current.credential_present.client_secret ? t('page.keep') : t('page.unset')"
          />
        </div>
        <div class="ap-add">
          <span class="ap-hint">{{ t('page.keep_hint') }}</span>
          <button class="ap-btn" @click="saveCred(current)">{{ t('page.save_creds') }}</button>
        </div>
      </div>

      <div class="ap-sect">{{ t('page.sec_definition') }}</div>
      <div class="ap-group">
        <div v-for="f in DEF_FIELDS" :key="f" class="ap-row">
          <span class="ap-label">{{ f }}<AGHelp v-bind="docRow(f)" readonly /></span>
          <span class="ap-val mono">{{ fmt(current[f]) }}</span>
        </div>
      </div>

      <p v-if="error" class="ap-err">{{ error }}</p>
    </template>
  </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import AGHelp from '../components/ui/AGHelp.vue'
import AGPill from '../components/ui/AGPill.vue'

const props = defineProps({ api: { type: Function, required: true } })

// i18n：与 AdminView 同源（admin.* 命名空间），缺键回落到接口 docs
const langVer = ref(0)
try { window.addEventListener('grape:langchange', () => { langVer.value += 1 }) } catch (e) { /* 忽略 */ }

function t(key, fallback = '') {
  void langVer.value
  try {
    const v = window.I18N && window.I18N.t('admin.' + key)
    if (v && v !== 'admin.' + key) return v
  } catch (e) { /* 忽略 */ }
  return fallback
}

const DEF_FIELDS = ['type', 'auth_url', 'token_url', 'user_url', 'emails_endpoint', 'scope',
  'pkce_enabled', 'id_field', 'login_field', 'name_field', 'avatar_field', 'redirect_uri', 'accept_header']

const channels = ref([])
const docs = ref({})
const edit = reactive({})
const loading = ref(true)
const error = ref('')
// 列表 → 详情：选中渠道的 provider key（null = 列表视图）
const selected = ref(null)
const current = computed(() => channels.value.find((c) => c.provider === selected.value) || null)

function back() {
  selected.value = null
}

function open(c) {
  error.value = ''
  selected.value = c.provider
}

function docRow(field) {
  // enabled 的语义在两个页面已经分叉（渠道=能否登录 / 平台=用户面是否显示），
  // 不能用同一份说明：field.enabled 是渠道口径，平台页用 field.enabled_platform。
  const key = field === 'enabled' ? 'enabled_channel' : field
  const d = docs.value[field] || {}
  return {
    title: field,
    what: t(`field.${key}.what`, d.what || ''),
    values: t(`field.${key}.values`, d.values || ''),
    note: t(`field.${key}.note`, d.note || ''),
  }
}
function fmt(v) {
  if (v === true) return 'true'
  if (v === false) return 'false'
  return v === undefined || v === null || v === '' ? t('page.empty') : String(v)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const r = await props.api('/oauth-channels')
    channels.value = r.channels || []
    docs.value = r.docs || {}
    for (const c of channels.value) {
      edit[c.provider] = { client_id: '', client_secret: '' }
    }
  } catch (e) {
    error.value = `${t('page.load_failed')}：${e.message || e}`
  } finally {
    loading.value = false
  }
}

async function toggleEnabled(c, ev) {
  const enabled = !!ev.target.checked
  try {
    await props.api(`/oauth-channels/${c.provider}/enabled`, { method: 'PUT', body: { enabled } })
    c.enabled = enabled
  } catch (e) {
    ev.target.checked = !enabled
    error.value = `${t('page.toggle_failed')}：${e.message || e}`
  }
}

async function saveCred(c) {
  const body = {}
  for (const k of ['client_id', 'client_secret']) {
    const v = (edit[c.provider][k] || '').trim()
    if (v) body[k] = v
  }
  if (!Object.keys(body).length) { error.value = t('page.need_one'); return }
  try {
    await props.api(`/oauth-channels/${c.provider}/credentials`, { method: 'PUT', body })
    edit[c.provider] = { client_id: '', client_secret: '' }
    await load()
  } catch (e) {
    error.value = `${t('page.save_failed')}：${e.message || e}`
  }
}

// 品牌图标：/static/img/<provider>.svg 存在就用真图（gitcode/github 已有，gitee/gitlab/huawei 本轮补）；
// 没有的品牌不硬凑——回退到空，让标题文字兜底，避免出现"图标错位"的假 logo。
const LOGO_KNOWN = new Set(['gitcode', 'github', 'gitee', 'gitlab', 'huawei'])
// v=2：图标内容换过（gitee/huawei 改为与登录页同一枚），带上版本查询串避免浏览器用旧缓存
function logoFor(provider) {
  return LOGO_KNOWN.has(provider) ? `/static/img/${provider}.svg?v=3` : ''
}

function channelName(c) {
  return t('provider_name.' + c.provider, '') || c.name || c.provider
}

onMounted(async () => { await load() })
</script>

<style scoped>
/* Apple 设置式分组列表：灰底页面 + 白色内嵌组 + 细分割线 + 右侧 chevron */
.ap-page { max-width: 880px; }
.ap-detail-hd { display: flex; align-items: center; gap: 8px; font-size: 17px; font-weight: 600; color: #1d1d1f; margin: 2px 0 6px; }
.ap-h1 { display: flex; align-items: center; gap: 8px; font-size: 24px; font-weight: 700; letter-spacing: -0.02em; color: #1d1d1f; margin: 4px 0 6px; }
.ap-sub { font-size: 13px; color: #6e6e73; margin: 0 0 18px; line-height: 1.5; }
.ap-sect { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: #86868b; margin: 24px 4px 8px; }
.ap-group { background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05); margin-bottom: 6px; }
.ap-row { display: flex; align-items: center; gap: 10px; padding: 13px 16px; border-bottom: 1px solid #f0f0f2; min-height: 44px; }
.ap-row:last-child { border-bottom: 0; }
.ap-row.clickable { cursor: pointer; transition: background 0.15s; }
.ap-row.clickable:hover { background: #fafafa; }
.ap-main { flex: 1; min-width: 0; }
.ap-title { font-size: 15px; font-weight: 600; color: #1d1d1f; }
.ap-logo { width: 20px; height: 20px; flex: 0 0 20px; object-fit: contain; border-radius: 4px; }
.ap-logo-lg { width: 28px; height: 28px; flex: 0 0 28px; margin-right: 2px; }
.ap-desc { font-size: 12.5px; color: #6e6e73; margin-top: 2px; }
.ap-label { font-size: 13px; color: #3a3a3c; min-width: 168px; display: inline-flex; align-items: center; }
.ap-val { font-size: 13px; color: #1d1d1f; word-break: break-all; flex: 1; text-align: right; }
.ap-val.mono, .ap-title.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.ap-val.muted { color: #8e8e93; text-align: left; }
.ap-key { background: #f2f2f7; border-radius: 6px; padding: 2px 8px; font-size: 12px; color: #3a3a3c; font-weight: 400; }
.ap-right { display: inline-flex; align-items: center; gap: 10px; }
.ap-switch { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: #3a3a3c; }
.ap-chev { color: #c7c7cc; font-size: 20px; line-height: 1; }
.ap-chev-l { font-size: 18px; margin-right: 2px; }
.ap-back { display: inline-flex; align-items: center; gap: 2px; font-size: 14px; color: #0071e3; cursor: pointer; margin-bottom: 10px; }
.ap-back:hover { text-decoration: underline; }
.ap-add { display: flex; align-items: center; gap: 8px; padding: 12px 16px; border-top: 1px solid #f0f0f2; flex-wrap: wrap; background: #fcfcfd; }
.ap-add .ap-hint { flex: 1; }
.ap-input { flex: 1; min-width: 150px; padding: 8px 10px; border: 1px solid #d2d2d7; border-radius: 8px; font-size: 13px; color: #1d1d1f; background: #fff; }
.ap-input:focus { outline: none; border-color: #0071e3; box-shadow: 0 0 0 3px rgba(0, 113, 227, 0.14); }
.ap-btn { padding: 8px 16px; border: 0; border-radius: 980px; background: #0071e3; color: #fff; cursor: pointer; font-size: 13px; font-weight: 500; }
.ap-btn:hover { background: #0077ed; }
.ap-btn:disabled { background: #b8d4f5; cursor: not-allowed; }
.ap-pills { display: flex; flex-wrap: wrap; gap: 6px; }
.ap-src { margin-left: 4px; font-size: 11px; opacity: 0.7; }
.ap-del { cursor: pointer; margin-left: 6px; color: #b3261e; font-size: 11px; }
.ap-hint { color: #6e6e73; font-size: 13px; padding: 8px 4px; }
.ap-err { color: #b3261e; font-size: 13px; padding: 8px 4px; }
</style>
