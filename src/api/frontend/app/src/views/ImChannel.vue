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

    <!-- 微信扫码绑定弹窗（Issue #1728）：ilink 返回的是扫码页链接，用 QRCode 库画成二维码 -->
    <div v-if="qr.open" class="imc__ob-mask" @click.self="closeQr">
      <div class="imc__ob">
        <div class="imc__ob-hdr">
          <b>{{ imt('wechat_modal_title') }}</b>
          <span class="imc__ob-x" @click="closeQr">✕</span>
        </div>
        <div class="imc__ob-body">
          <div class="imc__ob-qrwrap">
            <div :id="qrEl" class="imc__ob-qr"></div>
            <div v-if="qr.failed" class="imc__qr-fallback">{{ qr.link }}</div>
          </div>
          <div class="imc__ob-pk">{{ qr.text }}</div>
          <div v-if="qr.needPin" class="imc__ob-pin">
            <label>{{ imt('wechat_pin_label') }}</label>
            <input v-model="qr.pin" type="text" placeholder="1234" />
          </div>
          <div class="imc__ob-btns">
            <AGButton v-if="qr.needPin" size="sm" @click="submitWechatPin">{{ imt('wechat_pin_submit') }}</AGButton>
            <AGButton v-if="qr.expired" variant="plain" size="sm" @click="startWechatBind">
              {{ ct('chat.onboard.rescan') }}
            </AGButton>
            <AGButton variant="plain" size="sm" @click="closeQr">{{ imt('wechat_qr_close') }}</AGButton>
          </div>
        </div>
      </div>
    </div>

    <!-- 飞书扫码配置弹窗（Issues #294/#428 语义：409 询问重开、重绑确认、状态轮询） -->
    <div v-if="ob.open" class="imc__ob-mask" @click.self="cancelOnboard">
      <div class="imc__ob">
        <div class="imc__ob-hdr">
          <b>{{ ct('chat.onboard.modal_title') }}</b>
          <span class="imc__ob-x" @click="cancelOnboard">✕</span>
        </div>
        <div class="imc__ob-body">
          <template v-if="ob.status === 'pending'">{{ ct('chat.onboard.generating_qr') }}</template>
          <template v-else-if="ob.status === 'waiting_confirm' && ob.qrUrl">
            <div class="imc__ob-qrwrap">
              <div :id="obEl" class="imc__ob-qr"></div>
              <div v-if="ob.qrFailed" class="imc__qr-fallback">{{ ob.qrUrl }}</div>
            </div>
            <div class="imc__ob-t">{{ ct('chat.onboard.step1_scan') }}</div>
            <div class="imc__ob-t3">{{ ct('chat.onboard.step2_confirm') }}</div>
            <div :id="obCountEl" class="imc__ob-pk">{{ ct('chat.onboard.waiting_confirm') }}</div>
            <div class="imc__ob-warn">{{ ct('chat.onboard.close_warning') }}</div>
          </template>
          <template v-else-if="ob.status === 'connected'">
            <div class="imc__ob-big">✅</div>
            <div class="imc__ob-t">{{ ct('chat.onboard.success_title') }}</div>
            <div class="imc__ob-t2">
              App ID: <span class="imc__mono">{{ ob.appId || '' }}</span>
            </div>
            <div class="imc__ob-t2">{{ ct('chat.onboard.secret_saved') }}</div>
            <div class="imc__ob-pk">{{ ct('chat.onboard.bot_connected') }}</div>
            <AGButton @click="closeOnboard(true)">{{ ct('chat.onboard.close') }}</AGButton>
          </template>
          <template v-else-if="ob.status === 'failed'">
            <div class="imc__ob-big">❌</div>
            <div class="imc__ob-r">{{ ct('chat.onboard.failed_title') }}</div>
            <div class="imc__ob-t2">{{ onboardError }}</div>
            <div class="imc__ob-btns">
              <AGButton variant="plain" size="sm" @click="restartOnboard">{{ ct('chat.onboard.rescan') }}</AGButton>
              <AGButton variant="plain" size="sm" @click="closeOnboard(false)">{{ ct('chat.onboard.cancel') }}</AGButton>
            </div>
          </template>
          <template v-else>{{ ct('chat.onboard.generating_qr') }}</template>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, nextTick, onBeforeUnmount } from 'vue'
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
  // 2026-11-16：微信提到飞书后面（更常用），顺序即左栏卡片顺序。
  wechat: { name: 'WeChat', color: '#1AAD19' },
  wecom: { name: 'WeCom', color: '#07C160' },
  dingtalk: { name: 'DingTalk', color: '#1677FF' },
  slack: { name: 'Slack', color: '#611F69' },
  telegram: { name: 'Telegram', color: '#2AABEE' },
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

function ct(key, vars) {
  return G.I18N.t(key, vars)
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

// ── 二维码渲染（qrcode.min.js 由 shell index.html defer 加载，全局 QRCode）──
const uid = Math.random().toString(36).slice(2, 8)
const qrEl = 'imcQr' + uid
const obEl = 'imcObQr' + uid
const obCountEl = 'imcObCountdown' + uid

function renderQr(url, elId, size) {
  const el = document.getElementById(elId)
  if (!el) return false
  el.innerHTML = ''
  if (!url || typeof G.QRCode !== 'function') return false
  try {
    new G.QRCode(el, { text: url, width: size || 148, height: size || 148 })
    return true
  } catch (e) {
    return false
  }
}

function scanOnboard() {
  startFeishuOnboard(false)
}

// ── 飞书扫码配置（与 Chat 页同一后端契约：start/status/cancel，409 询问重开、重绑确认）──
const ob = reactive({ open: false, status: 'pending', qrUrl: '', qrFailed: false, appId: '', error: '' })
let obTimer = null
let obCountdownTimer = null
let obTaskId = null
let obExpireAt = 0
let obQrRendered = false
const onboardError = computed(function () {
  const s = String(ob.error || '')
  if (s.includes('access_denied')) return ct('chat.onboard.error_access_denied')
  if (s.includes('expired') || s.includes('timeout')) return ct('chat.onboard.qr_expired')
  return s || ct('chat.onboard.error_unknown')
})

function startFeishuOnboard(skipRebind) {
  if (!skipRebind) {
    const existing = (curConfig.value && curConfig.value.app_id) || ''
    if (existing) {
      G.app.showConfirm(ct('chat.onboard.confirm_rebind', { appId: existing }), function () {
        startFeishuOnboard(true)
      }, { danger: true })
      return
    }
  }
  G.API.post('/feishu/onboard/start', { bot_type: 'chat_bot' })
    .then(function (d) {
      obTaskId = (d && d.task_id) || ''
      ob.open = true
      ob.status = 'pending'
      ob.qrUrl = ''
      ob.qrFailed = false
      ob.appId = ''
      ob.error = ''
      obQrRendered = false
      obExpireAt = 0
      pollOnboard()
    })
    .catch(function (e) {
      const detail = (e && e.detail) || {}
      if (e && e.status === 409) {
        const oldTaskId = detail.task_id || ''
        G.app.showConfirm(ct('chat.onboard.confirm_restart'), function () {
          if (oldTaskId) {
            G.API.post('/feishu/onboard/cancel', { task_id: oldTaskId }).finally(function () {
              startFeishuOnboard(true)
            })
          } else startFeishuOnboard(true)
        }, { danger: true })
        return
      }
      G.app.showToast(ct('chat.onboard.start_failed', { msg: (e && e.message) || '' }))
    })
}

function pollOnboard() {
  if (obTimer) clearInterval(obTimer)
  obTimer = setInterval(function () {
    if (!obTaskId) {
      stopOnboardTimers()
      return
    }
    G.API.get('/feishu/onboard/status?task_id=' + encodeURIComponent(obTaskId))
      .then(function (d) {
        if (!d) return
        ob.status = d.status || ob.status
        ob.appId = d.app_id || ob.appId
        ob.error = d.error || ''
        if (d.status === 'waiting_confirm' && d.qr_url && !obQrRendered) {
          obQrRendered = true
          ob.qrUrl = d.qr_url
          obExpireAt = d.expire_in ? Date.now() + Number(d.expire_in) * 1000 : 0
          nextTick(function () {
            ob.qrFailed = !renderQr(ob.qrUrl, obEl, 200)
            startOnboardCountdown()
          })
        }
        if (d.status === 'connected' || d.status === 'failed') {
          stopOnboardTimers()
          if (d.status === 'connected') load()
        }
      })
      .catch(function () { stopOnboardTimers() })
  }, 2000)
}

function startOnboardCountdown() {
  if (obCountdownTimer) clearInterval(obCountdownTimer)
  obCountdownTimer = setInterval(function () {
    const el = document.getElementById(obCountEl)
    if (!el || !obExpireAt) {
      clearInterval(obCountdownTimer)
      obCountdownTimer = null
      return
    }
    const remain = Math.max(0, Math.round((obExpireAt - Date.now()) / 1000))
    if (remain <= 0) {
      el.textContent = ct('chat.onboard.qr_expired')
      clearInterval(obCountdownTimer)
      obCountdownTimer = null
      return
    }
    const mm = String(Math.floor(remain / 60)).padStart(2, '0')
    const ss = String(remain % 60).padStart(2, '0')
    el.textContent = ct('chat.onboard.waiting_countdown', { time: mm + ':' + ss })
  }, 1000)
}

function stopOnboardTimers() {
  if (obTimer) {
    clearInterval(obTimer)
    obTimer = null
  }
  if (obCountdownTimer) {
    clearInterval(obCountdownTimer)
    obCountdownTimer = null
  }
}

function closeOnboard(refresh) {
  stopOnboardTimers()
  obTaskId = null
  ob.open = false
  ob.status = 'pending'
  ob.qrUrl = ''
  ob.qrFailed = false
  obExpireAt = 0
  obQrRendered = false
  if (refresh) load()
}

function cancelOnboard() {
  if (obTaskId) {
    G.app.showConfirm(ct('chat.onboard.confirm_close'), function () {
      const id = obTaskId
      closeOnboard(false)
      if (id) G.API.post('/feishu/onboard/cancel', { task_id: id }).catch(function () {})
    }, { danger: true })
    return
  }
  closeOnboard(false)
}

function restartOnboard() {
  const id = obTaskId
  closeOnboard(false)
  if (id) G.API.post('/feishu/onboard/cancel', { task_id: id }).catch(function () {})
  startFeishuOnboard(true)
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
// 后端 start 返回 {login_id, qrcode_img_content, expires_in}；qrcode_img_content 是扫码页
// 链接（不是图片），必须用 QRCode 库画成二维码——直接塞 <img src> 只会 broken。
const qr = reactive({ open: false, id: '', link: '', failed: false, expired: false, text: '', needPin: false, pin: '' })
let qrTimer = null
let qrSeq = 0

const WECHAT_STATUS_TEXT = {
  wait: 'wechat_wait',
  scaned: 'wechat_scaned',
  need_verifycode: 'wechat_need_pin',
  confirmed: 'wechat_confirmed',
  expired: 'wechat_expired',
  verify_code_blocked: 'wechat_expired',
}

function startWechatBind() {
  closeQr()
  qr.open = true
  qr.text = imt('wechat_wait') || '…'
  G.API.post('/chat/im/wechat/login/start', {})
    .then(function (res) {
      qr.id = (res && (res.login_id || res.id)) || ''
      qr.link = (res && (res.qrcode_img_content || res.qrcode_url)) || ''
      nextTick(function () {
        qr.failed = !renderQr(qr.link, qrEl, 200)
      })
      if (qr.id) pollWechat()
      else qr.text = imt('err_qr_failed')
    })
    .catch(function (e) {
      qr.text = imt('err_qr_failed') + ((e && e.message) || '')
    })
}

function pollWechat() {
  if (qrTimer) clearTimeout(qrTimer)
  const seq = qrSeq
  qrTimer = setTimeout(function () {
    if (!qr.id || seq !== qrSeq) return
    G.API.get('/chat/im/wechat/login/status?id=' + encodeURIComponent(qr.id) + (qr.needPin && qr.pin ? '&verify_code=' + encodeURIComponent(qr.pin) : ''))
      .then(function (res) {
        if (seq !== qrSeq) return
        const st = (res && res.status) || ''
        qr.needPin = st === 'need_verifycode'
        qr.text = (imt(WECHAT_STATUS_TEXT[st] || '') || st) + (res && res.message ? ' · ' + res.message : '')
        if (res && res.refreshed && res.qrcode_img_content) {
          qr.link = res.qrcode_img_content
          nextTick(function () {
            qr.failed = !renderQr(qr.link, qrEl)
          })
        }
        if (st === 'confirmed') {
          qr.text = imt('wechat_confirmed')
          load()
          setTimeout(closeQr, 1200)
          return
        }
        if (st === 'expired' || st === 'verify_code_blocked') {
          qr.expired = true
          return
        }
        pollWechat()
      })
      .catch(function () {
        if (seq === qrSeq) pollWechat()
      })
  }, 2500)
}

function submitWechatPin() {
  pollWechat()
}

function closeQr() {
  qrSeq++
  if (qrTimer) clearTimeout(qrTimer)
  qrTimer = null
  qr.open = false
  qr.id = ''
  qr.link = ''
  qr.failed = false
  qr.expired = false
  qr.needPin = false
  qr.pin = ''
}

onBeforeUnmount(function () {
  closeQr()
  stopOnboardTimers()
})

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
.imc__qr-fallback {
  font-size: 11px;
  line-height: 1.4;
  color: var(--c-text-3);
  word-break: break-all;
  padding: 8px;
  max-width: 210px;
}
/* 微信弹窗内的 PIN 输入（复用飞书弹窗的 imc__ob-* 骨架） */
.imc__ob-pin {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: center;
  margin: 6px 0 10px;
  color: var(--c-text-2);
}
.imc__ob-pin input {
  width: 140px;
  text-align: center;
  padding: 7px 10px;
  border-radius: 10px;
  border: 1px solid var(--glass-border);
  background: var(--c-bg);
  color: var(--c-text);
  font-size: 13px;
  letter-spacing: 2px;
  outline: none;
}
.imc__ob-pin input:focus {
  border-color: var(--c-text-2);
}
/* 飞书扫码弹窗 */
.imc__ob-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  z-index: 1001;
  display: flex;
  align-items: center;
  justify-content: center;
}
.imc__ob {
  width: 340px;
  background: var(--c-bg-elevated);
  border: 1px solid var(--glass-border);
  border-radius: 18px;
  padding: 20px;
  box-shadow: var(--shadow-2);
  font-size: 12px;
  color: var(--c-text);
}
.imc__ob-hdr {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}
.imc__ob-hdr b {
  font-size: 14px;
}
.imc__ob-x {
  cursor: pointer;
  color: var(--c-text-3);
}
.imc__ob-body {
  text-align: center;
  color: var(--c-text-3);
  padding: 10px 0;
}
.imc__ob-qrwrap {
  background: #fff;
  border: 1px solid var(--glass-border);
  border-radius: 10px;
  padding: 12px;
  display: inline-block;
  margin-bottom: 10px;
}
.imc__ob-qr {
  width: 200px;
  min-height: 40px;
}
.imc__ob-qr canvas,
.imc__ob-qr img {
  display: block;
}
.imc__ob-t {
  color: var(--c-text);
  margin-bottom: 4px;
}
.imc__ob-t2 {
  color: var(--c-text-2);
  font-size: 11px;
  margin-bottom: 2px;
}
.imc__ob-t3 {
  color: var(--c-text-3);
  margin-bottom: 10px;
}
.imc__ob-pk {
  color: var(--c-text-2);
  font-size: 11px;
  margin-bottom: 8px;
}
.imc__ob-warn {
  color: var(--c-danger);
  font-size: 11px;
  font-weight: 600;
  border-top: 1px solid var(--glass-border);
  padding-top: 8px;
}
.imc__ob-big {
  font-size: 34px;
  margin-bottom: 8px;
}
.imc__ob-r {
  color: var(--c-danger);
  font-weight: 600;
  margin-bottom: 8px;
}
.imc__ob-btns {
  display: flex;
  gap: 8px;
  justify-content: center;
}
.imc__mono {
  font-family: monospace;
}
</style>
