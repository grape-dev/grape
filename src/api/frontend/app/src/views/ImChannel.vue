<template>
  <div class="imc">
    <div v-if="loading" class="imc__hint">{{ ct('common.loading') }}</div>
    <div v-else-if="error" class="imc__hint imc__hint--err">{{ ct('common.load_failed') }}</div>
    <template v-else-if="config">
      <!-- 顶部 Banner -->
      <div
        class="imc__banner"
        :style="{
          background: 'linear-gradient(135deg,' + cur.bg + ',' + cur.bgLight + ')',
          borderColor: cur.color + '22',
        }"
      >
        <div
          class="imc__banner-icon"
          :style="{ background: cur.gradient, boxShadow: '0 6px 16px ' + cur.color + '44' }"
        >
          <svg
            width="24"
            height="24"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path
              d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"
            />
          </svg>
        </div>
        <div class="imc__banner-text">
          <h2>{{ imt('title') }}</h2>
          <p>{{ imt('subtitle') }}</p>
        </div>
        <div v-if="isConfigured" class="imc__active">
          <span class="dot"></span>{{ imt('active_status') }}
        </div>
      </div>

      <div class="imc__main">
        <!-- 左栏：平台选择 -->
        <div class="imc__left">
          <div class="imc__group-label">{{ imt('provider') }}</div>
          <div
            v-for="(p, key) in platforms"
            :key="key"
            :class="['imc__pcard', { active: current === key, off: p.disabled }]"
            :style="{
              borderColor: current === key ? p.color : undefined,
              background: current === key ? p.bg : undefined,
              boxShadow: current === key ? '0 8px 24px ' + p.color + '22' : undefined,
            }"
            @click="p.disabled ? null : (current = key)"
          >
            <div
              v-if="current === key && !p.disabled"
              class="imc__pcard-top"
              :style="{ background: p.gradient }"
            ></div>
            <div v-if="p.disabled" class="imc__pcard-soon">{{ imt('coming_soon') }}</div>
            <div class="imc__pcard-head">
              <div class="imc__pcard-icon" :style="{ background: p.bgLight }">
                <img
                  :src="'/static/img/' + key + '.svg'"
                  width="26"
                  height="26"
                  style="object-fit: contain"
                  :style="{ filter: p.disabled ? 'grayscale(.6)' : undefined }"
                  alt=""
                />
              </div>
              <div class="imc__pcard-name-wrap">
                <div
                  class="imc__pcard-name"
                  :style="{ color: current === key && !p.disabled ? p.color : undefined }"
                >
                  {{ dispName(p) }}
                </div>
                <div class="imc__pcard-en">{{ p.name }}</div>
              </div>
            </div>
            <div class="imc__pcard-desc">{{ descOf(key) }}</div>
            <div class="imc__pcard-status">
              <span v-if="badgeOf(key).tone === 'ok'" class="imc__st-ok">
                <span class="dot"></span>{{ badgeOf(key).text }}
              </span>
              <span v-else class="imc__st-no">{{ badgeOf(key).text }}</span>
            </div>
          </div>
        </div>

        <!-- 右栏：配置表单 -->
        <div class="imc__right">
          <AGCard class="imc__cfg">
            <div class="imc__cfg-head">
              <div
                class="imc__cfg-icon"
                :style="{ background: cur.bgLight, borderColor: cur.color + '22' }"
              >
                <img
                  :src="'/static/img/' + current + '.svg'"
                  width="26"
                  height="26"
                  style="object-fit: contain"
                  alt=""
                />
              </div>
              <div class="imc__cfg-name">
                <div class="imc__cfg-title">{{ dispName(cur) }} {{ imt('config') }}</div>
                <div class="imc__cfg-desc">{{ descOf(current) }}</div>
              </div>
              <div v-if="isConfigured" class="imc__cfg-ok">
                <span class="dot"></span>{{ imt('connected') }}
              </div>
            </div>

            <div class="imc__cfg-row">
              <div
                v-for="f in curFields"
                :key="f.key"
                class="imc__field"
                :class="{ 'imc__field--wide': curFields.length === 1 }"
              >
                <label>{{ fieldLabel(f) }}</label>
                <input
                  v-model="form[f.key]"
                  :type="f.type"
                  :readonly="f.readonly"
                  :placeholder="fieldPlaceholder(f)"
                />
              </div>
            </div>
            <div v-if="current === 'wechat'" class="imc__cfg-tip">{{ imt('guide_wechat_s1') }}</div>

            <div v-if="qr.open" class="imc__qr">
              <img v-if="qr.img" :src="qr.img" alt="wechat qr" class="imc__qr-img" />
              <div class="imc__qr-side">
                <div class="imc__qr-status">{{ qr.text }}</div>
                <div v-if="qr.needPin" class="imc__field">
                  <label>{{ imt('wechat_pin_label') }}</label>
                  <input v-model="qr.pin" type="text" placeholder="1234" />
                </div>
                <div class="imc__qr-actions">
                  <AGButton v-if="qr.needPin" size="sm" @click="submitWechatPin">{{ imt('wechat_pin_submit') }}</AGButton>
                  <AGButton variant="plain" size="sm" @click="closeQr">{{ imt('wechat_qr_close') }}</AGButton>
                </div>
              </div>
            </div>

            <div class="imc__cfg-actions">
              <AGButton v-if="current === 'wechat'" variant="plain" @click="startWechatBind">
                {{ imt('btn_scan_wechat') }}
              </AGButton>
              <AGButton v-else-if="current === 'feishu'" variant="plain" @click="scanOnboard">
                {{ ct('chat.scan_config') }}
              </AGButton>
              <AGButton @click="save">{{ ct('common.save') }}</AGButton>
              <AGButton v-if="isConfigured" variant="danger" @click="unbind">{{ imt('unbind') }}</AGButton>
            </div>
          </AGCard>

          <div
            class="imc__help"
            :style="{ background: cur.bgLight, borderColor: cur.color + '22' }"
          >
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              :stroke="cur.color"
              stroke-width="2"
              style="flex-shrink: 0; margin-top: 1px"
            >
              <circle cx="12" cy="12" r="10" />
              <path d="M12 16v-4M12 8h.01" />
            </svg>
            <div class="imc__help-body">
              <div class="imc__help-title">{{ guideTitle }}</div>
              <ol class="imc__guide-list">
                <li v-for="(st, i) in curGuide.steps" :key="i" v-html="st"></li>
              </ol>
              <div v-if="curGuide.tip" class="imc__guide-tip">{{ curGuide.tip }}</div>
              <div class="imc__guide-actions">
                <a
                  v-if="curGuide.url"
                  :href="curGuide.url"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="imc__guide-link"
                  :style="{ color: cur.color, borderColor: cur.color + '55' }"
                  >{{ imt('guide_url_' + current) }} ↗</a
                >
                <button v-if="current === 'slack'" class="imc__guide-link" type="button" @click="copyManifest">
                  {{ imt('guide_manifest_btn') }}
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onBeforeUnmount } from 'vue'
import AGCard from '@/components/ui/AGCard.vue'
import AGButton from '@/components/ui/AGButton.vue'

const G = globalThis

const loading = ref(true)
const error = ref(false)
const config = ref(null)
const current = ref('feishu')
const live = ref({}) // channel_type -> {connected, status}（来自 /im/installations）
const form = reactive({ app_id: '', app_secret: '' })

// 2026-11-13：与后端 Type 枚举对齐的 6 个渠道（feishu/wecom/dingtalk/slack/telegram/wechat）。
// 品牌色只用于卡片强调色与图标底，不写死凭据字段——字段由 FIELDS 表驱动。
const platforms = {
  feishu: { name: 'Feishu', color: '#3370FF' },
  wecom: { name: 'WeCom', color: '#07C160' },
  dingtalk: { name: 'DingTalk', color: '#1677FF' },
  slack: { name: 'Slack', color: '#611F69' },
  telegram: { name: 'Telegram', color: '#2AABEE' },
  wechat: { name: 'WeChat', color: '#1AAD19' },
}
for (const k of Object.keys(platforms)) {
  platforms[k].bg = 'color-mix(in srgb, ' + platforms[k].color + ' 14%, transparent)'
  platforms[k].bgLight = 'color-mix(in srgb, ' + platforms[k].color + ' 8%, transparent)'
  platforms[k].gradient = 'linear-gradient(135deg,' + platforms[k].color + ',' + platforms[k].color + 'bb)'
}

// 每个渠道的凭据字段：label 为完整 i18n key；placeholder 可选；readonly 用于扫码写入的渠道。
const FIELDS = {
  feishu: [
    { key: 'app_id', type: 'text', label: 'chat.app_id' },
    { key: 'app_secret', type: 'password', label: 'chat.app_secret' },
  ],
  wecom: [
    { key: 'app_id', type: 'text', label: 'chat.im_channel.label_wecom_app_id', ph: 'hint_wecom_app_id' },
    { key: 'app_secret', type: 'password', label: 'chat.im_channel.label_wecom_secret', ph: 'hint_wecom_secret' },
  ],
  dingtalk: [
    { key: 'app_id', type: 'text', label: 'chat.im_channel.label_dingtalk_app_id', ph: 'hint_dingtalk_app_id' },
    { key: 'app_secret', type: 'password', label: 'chat.im_channel.label_dingtalk_secret', ph: 'hint_dingtalk_secret' },
  ],
  slack: [
    { key: 'app_id', type: 'password', label: 'chat.im_channel.label_slack_app_id' },
    { key: 'app_secret', type: 'password', label: 'chat.im_channel.label_slack_secret' },
  ],
  telegram: [
    { key: 'app_id', type: 'password', label: 'chat.im_channel.label_telegram_app_id', ph: 'hint_telegram_app_id' },
  ],
  wechat: [
    { key: 'app_id', type: 'text', label: 'chat.im_channel.label_wechat_app_id', ph: 'hint_wechat_app_id', readonly: true },
  ],
}

// 各平台接入后台地址（按钮文案走 guide_url_<p>，地址集中在 JS，避免 i18n 里出现 URL）
const GUIDE_URLS = {
  feishu: 'https://open.feishu.cn/app',
  wecom: 'https://work.weixin.qq.com/wework_admin/frame#apps',
  dingtalk: 'https://open-dev.dingtalk.com/fe/app#/corp/app',
  slack: 'https://api.slack.com/apps',
  telegram: 'https://t.me/BotFather',
}

// Slack App Manifest（Issue #1309）：Socket Mode + 最小 scope，直接粘贴到 api.slack.com/apps 建应用
const SLACK_MANIFEST = {
  display_information: { name: 'grape-devops', description: 'Grape DevOps assistant', background_color: '#611f69' },
  features: { bot_user: { display_name: 'grape', always_online: true } },
  oauth_config: {
    scopes: {
      bot: ['app_mentions:read', 'channels:history', 'chat:write', 'files:write', 'groups:history', 'im:history', 'im:read', 'im:write', 'reactions:write'],
    },
  },
  settings: {
    event_subscriptions: {
      bot_events: ['app_mention', 'message.channels', 'message.groups', 'message.im'],
    },
    interactivity: { is_enabled: true },
    org_deploy_enabled: false,
    socket_mode_enabled: true,
    token_rotation_enabled: false,
  },
}

const cur = computed(() => platforms[current.value])
const curFields = computed(() => FIELDS[current.value] || FIELDS.feishu)
const curConfig = computed(() => platformConfig(current.value))
const isConfigured = computed(() => !!curConfig.value.app_id)
const curGuide = computed(() => guideOf(current.value))
const guideTitle = computed(() => imt('guide_title', { n: String(curGuide.value.steps.length) }))

function ct(key) {
  return G.I18N.t(key)
}
function imt(key, vars) {
  const full = 'chat.im_channel.' + key
  const v = G.I18N.t(full, vars)
  return v && v !== full ? v : ''
}
function dispName(p) {
  const key = Object.keys(platforms).find(function (k) { return platforms[k] === p })
  return imt('platform_' + key) || p.name
}
function descOf(key) {
  return imt('desc_' + key)
}
function fieldLabel(f) {
  return f.label.indexOf('chat.im_channel.') === 0 ? imt(f.label.slice(16)) : G.I18N.t(f.label)
}
function fieldPlaceholder(f) {
  if (f.ph) return imt(f.ph)
  return (dispName(cur.value) || '') + ' ' + fieldLabel(f)
}
function platformConfig(key) {
  return (config.value && config.value.im && config.value.im[key]) || {}
}
function badgeOf(key) {
  const inst = live.value[key]
  const cfg = platformConfig(key)
  if (inst && inst.connected) return { tone: 'ok', text: imt('badge_connected') }
  if (cfg && cfg.app_id) return { tone: 'no', text: inst ? imt('badge_configured_offline') : imt('badge_configured') }
  return { tone: 'no', text: imt('badge_none') }
}
function guideOf(key) {
  const steps = []
  for (let i = 1; i <= 4; i++) {
    const v = imt('guide_' + key + '_s' + i)
    if (v) steps.push(v)
  }
  return { steps: steps, tip: imt('guide_' + key + '_tip'), url: GUIDE_URLS[key] || '' }
}

function qs() {
  const repo = G.app.currentRepo()
  return repo ? '?repo=' + encodeURIComponent(repo.fullName) : ''
}

function load() {
  loading.value = true
  error.value = false
  G.API.get('/chat/config' + qs())
    .then(function (d) {
      config.value = d
      const im = d.im || {}
      const bound = Object.keys(platforms).find(function (k) {
        return im[k] && im[k].app_id
      })
      current.value = (im.provider && platforms[im.provider]) ? im.provider : bound || 'feishu'
      syncForm()
      loading.value = false
      loadLive()
    })
    .catch(function () {
      error.value = true
      loading.value = false
    })
}

function loadLive() {
  // 只取连接状态给徽标用（正式界面不展示运维面板）
  G.API.get('/im/installations' + qs())
    .then(function (list) {
      const map = {}
      ;(list && list.installations ? list.installations : []).forEach(function (it) {
        if (!it || it.status === 'revoked' || !it.channel_type) return
        map[it.channel_type] = { connected: !!it.connected, status: it.status }
      })
      live.value = map
    })
    .catch(function () {
      live.value = {}
    })
}

function syncForm() {
  const cfg = curConfig.value || {}
  form.app_id = cfg.app_id || ''
  form.app_secret = cfg.app_secret_exists || cfg.app_secret ? '********' : ''
}
watch(current, function () {
  syncForm()
  closeQr()
})

function save() {
  const secret = form.app_secret === '********' ? '' : form.app_secret
  const payload = { im: { provider: current.value, app_id: form.app_id, app_secret: secret } }
  G.API.put('/chat/config' + qs(), payload)
    .then(function () {
      G.app.showToast(G.I18N.t('chat.config.saved'))
      load()
    })
    .catch(function (e) {
      G.app.showToast(G.I18N.t('chat.config.save_failed', { msg: e.message }))
    })
}

function unbind() {
  G.app.showConfirm(
    imt('unbind_confirm'),
    function () {
      G.API.put('/chat/config' + qs(), { im: { provider: current.value, app_id: '', app_secret: '' } })
        .then(function () {
          G.app.showToast(imt('unbind_success'))
          load()
        })
        .catch(function (e) {
          G.app.showToast(imt('unbind_failed', { msg: e.message }))
        })
    },
    { danger: true }
  )
}

function scanOnboard() {
  G.startFeishuOnboard('chat_bot')
}

function copyManifest() {
  const text = JSON.stringify(SLACK_MANIFEST, null, 2)
  const ok = function () { G.app.showToast(imt('copy_ok')) }
  const fail = function () { G.app.showToast(imt('copy_fail')) }
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(ok).catch(fail)
      return
    }
  } catch (e) { /* fallthrough */ }
  fail()
}

// ── 微信 ilink 扫码绑定（Issue #1728）──
const qr = reactive({ open: false, id: '', img: '', text: '', needPin: false, pin: '' })
let qrTimer = null

function startWechatBind() {
  qr.open = true
  qr.img = ''
  qr.needPin = false
  qr.pin = ''
  qr.text = G.I18N.t('chat.im_channel.loading') || '…'
  G.API.post('/chat/im/wechat/login/start', {})
    .then(function (res) {
      qr.id = (res && res.id) || ''
      qr.img = (res && (res.qrcode_img_content || res.qrcode_url)) || ''
      pollWechat()
    })
    .catch(function (e) {
      qr.text = imt('err_qr_failed') + (e && e.message ? e.message : '')
    })
}

function pollWechat() {
  if (qrTimer) clearTimeout(qrTimer)
  qrTimer = setTimeout(function () {
    if (!qr.id) return
    G.API.get('/chat/im/wechat/login/status?id=' + encodeURIComponent(qr.id) + (qr.needPin && qr.pin ? '&verify_code=' + encodeURIComponent(qr.pin) : ''))
      .then(function (res) {
        const st = (res && res.status) || ''
        qr.needPin = st === 'need_verifycode'
        qr.text = st + (res && res.message ? ' · ' + res.message : '')
        if (st === 'confirmed') {
          qr.text = imt('connected')
          load()
          setTimeout(closeQr, 1200)
          return
        }
        if (st === 'expired') return
        pollWechat()
      })
      .catch(function () { pollWechat() })
  }, 2500)
}

function submitWechatPin() {
  pollWechat()
}

function closeQr() {
  if (qrTimer) clearTimeout(qrTimer)
  qrTimer = null
  qr.open = false
  qr.id = ''
  qr.img = ''
  qr.needPin = false
  qr.pin = ''
}

onBeforeUnmount(closeQr)

load()
</script>

<style scoped>
.imc {
  color: var(--c-text);
  font-family: var(--f-family);
}
.imc__hint {
  text-align: center;
  color: var(--c-text-3);
  padding: 80px 0;
}
.imc__hint--err {
  color: var(--c-danger);
}
.imc__banner {
  border-radius: 16px;
  padding: 20px 24px;
  margin-bottom: 20px;
  display: flex;
  align-items: center;
  gap: 16px;
  border: 1px solid transparent;
}
.imc__banner-icon {
  width: 48px;
  height: 48px;
  border-radius: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--on-accent);
  flex-shrink: 0;
}
.imc__banner-text {
  flex: 1;
}
.imc__banner-text h2 {
  font-size: var(--f-head-2);
  font-weight: 800;
  margin: 0;
}
.imc__banner-text p {
  font-size: var(--f-caption);
  color: var(--c-text-3);
  margin: 4px 0 0;
}
.imc__active {
  padding: 7px 16px;
  border-radius: 20px;
  background: var(--c-bg-elevated);
  color: var(--c-success);
  font-size: 11px;
  font-weight: 700;
  display: flex;
  align-items: center;
  gap: 6px;
  box-shadow: var(--shadow-1);
}
.imc__active .dot,
.imc__st-ok .dot,
.imc__cfg-ok .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--c-success);
}
.imc__main {
  display: flex;
  gap: 20px;
  align-items: flex-start;
}
.imc__left {
  flex: 0 0 300px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.imc__group-label {
  font-size: 11px;
  font-weight: 700;
  color: var(--c-text-3);
  margin-bottom: 2px;
  letter-spacing: 0.5px;
  text-transform: uppercase;
}
.imc__pcard {
  padding: 18px;
  border-radius: 16px;
  border: 2px solid var(--glass-border);
  background: var(--c-bg-elevated);
  transition: all 0.25s cubic-bezier(0.25, 0.1, 0.25, 1);
  position: relative;
  overflow: hidden;
  box-shadow: var(--shadow-1);
  cursor: pointer;
}
.imc__pcard.off {
  opacity: 0.55;
  cursor: not-allowed;
}
.imc__pcard-top {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 3px;
}
.imc__pcard-soon {
  position: absolute;
  top: 10px;
  right: 10px;
  padding: 2px 8px;
  border-radius: 10px;
  background: var(--c-bg);
  color: var(--c-text-3);
  font-size: 9px;
  font-weight: 600;
  border: 1px solid var(--glass-border);
}
.imc__pcard-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.imc__pcard-icon {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.imc__pcard-name-wrap {
  min-width: 0;
  flex: 1;
}
.imc__pcard-name {
  font-size: var(--f-body);
  font-weight: 700;
}
.imc__pcard-en {
  font-size: 10px;
  color: var(--c-text-3);
}
.imc__pcard-desc {
  font-size: 10px;
  color: var(--c-text-3);
  margin-bottom: 10px;
  line-height: 1.4;
}
.imc__pcard-status {
  display: flex;
  align-items: center;
  font-size: 10px;
}
.imc__st-ok {
  color: var(--c-success);
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  border-radius: 10px;
  background: rgba(52, 199, 89, 0.12);
}
.imc__st-no {
  color: var(--c-text-3);
  padding: 3px 8px;
  border-radius: 10px;
  background: var(--c-bg);
}
.imc__right {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.imc__cfg {
  padding: 22px 24px;
  border-radius: 16px;
}
.imc__cfg-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 18px;
  padding-bottom: 16px;
  border-bottom: 2px solid var(--c-bg);
}
.imc__cfg-icon {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  border: 1px solid transparent;
}
.imc__cfg-name {
  flex: 1;
}
.imc__cfg-title {
  font-size: var(--f-body);
  font-weight: 700;
}
.imc__cfg-desc {
  font-size: 11px;
  color: var(--c-text-3);
}
.imc__cfg-ok {
  font-size: 10px;
  color: var(--c-success);
  background: rgba(52, 199, 89, 0.12);
  padding: 4px 10px;
  border-radius: 12px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 3px;
}
.imc__cfg-row {
  display: flex;
  gap: 14px;
  margin-bottom: 18px;
}
.imc__field {
  flex: 1;
}
.imc__field label {
  font-size: 11px;
  color: var(--c-text-2);
  display: block;
  margin-bottom: 6px;
  font-weight: 600;
}
.imc__field input {
  width: 100%;
  padding: 11px 14px;
  border: 1.5px solid var(--glass-border);
  border-radius: 10px;
  font-size: 13px;
  background: var(--c-bg-elevated);
  color: var(--c-text);
  outline: none;
  transition: all 0.2s;
}
.imc__field input:focus {
  border-color: var(--c-accent);
}
.imc__cfg-actions {
  display: flex;
  gap: 10px;
}
.imc__help {
  padding: 14px 18px;
  font-size: 11px;
  color: var(--c-text-3);
  line-height: 1.7;
  border-radius: 12px;
  border: 1px solid transparent;
  display: flex;
  gap: 10px;
  align-items: flex-start;
}
.imc__help-title {
  font-weight: 700;
  margin-bottom: 3px;
  color: var(--c-text-2);
}
</style>
/* 2026-11-13：六渠道版新增样式（字段/引导/二维码） */
.imc__field--wide {
  grid-column: 1 / -1;
}
.imc__cfg-tip {
  font-size: 12px;
  color: var(--c-text-3);
  margin: 8px 0 4px;
}
.imc__help-body {
  flex: 1;
  min-width: 0;
}
.imc__guide-list {
  margin: 6px 0 0 16px;
  padding: 0;
  font-size: 12px;
  line-height: 1.75;
  color: var(--c-text-2);
}
.imc__guide-tip {
  margin-top: 8px;
  font-size: 12px;
  color: var(--c-text-3);
  line-height: 1.6;
}
.imc__guide-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 10px;
}
.imc__guide-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 12px;
  border: 1px solid transparent;
  border-radius: 999px;
  background: transparent;
  font-size: 12px;
  cursor: pointer;
  text-decoration: none;
}
.imc__qr {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  margin: 12px 0 4px;
  padding: 14px;
  border: 1px solid var(--glass-border);
  border-radius: 14px;
  background: var(--c-bg);
}
.imc__qr-img {
  width: 148px;
  height: 148px;
  object-fit: contain;
  border-radius: 10px;
  background: #fff;
}
.imc__qr-side {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.imc__qr-status {
  font-size: 12px;
  color: var(--c-text-2);
}
.imc__qr-actions {
  display: flex;
  gap: 8px;
}
