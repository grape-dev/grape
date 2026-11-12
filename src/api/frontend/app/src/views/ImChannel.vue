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
            <div class="imc__pcard-desc">{{ p.desc }}</div>
            <div class="imc__pcard-status">
              <span v-if="p.disabled">—</span>
              <span v-else-if="platformConfig(key).app_id" class="imc__st-ok">
                <span class="dot"></span>{{ imt('connected') }}
              </span>
              <span v-else class="imc__st-no">{{ imt('not_connected') }}</span>
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
                <div class="imc__cfg-desc">{{ cur.desc }}</div>
              </div>
              <div v-if="isConfigured" class="imc__cfg-ok">
                <span class="dot"></span>{{ imt('connected') }}
              </div>
            </div>

            <div class="imc__cfg-row">
              <div class="imc__field">
                <label>{{ ct('chat.app_id') }}</label>
                <input v-model="form.app_id" :placeholder="cur.name + ' App ID'" />
              </div>
              <div class="imc__field">
                <label>{{ ct('chat.app_secret') }}</label>
                <input
                  v-model="form.app_secret"
                  type="password"
                  :placeholder="cur.name + ' App Secret'"
                />
              </div>
            </div>

            <div class="imc__cfg-actions">
              <AGButton variant="plain" @click="scanOnboard">
                <svg
                  width="15"
                  height="15"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                >
                  <rect x="3" y="3" width="18" height="18" rx="2" />
                  <path d="M7 7h10M7 12h6M7 17h4" />
                </svg>
                {{ ct('chat.scan_config') }}
              </AGButton>
              <AGButton @click="save">{{ ct('common.save') }}</AGButton>
              <AGButton v-if="isConfigured" variant="danger" @click="unbind">{{
                imt('unbind')
              }}</AGButton>
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
            <div>
              <div class="imc__help-title">{{ imt('help_title') }}</div>
              <div>{{ imt('help_desc') }}</div>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, reactive, computed } from 'vue'
import AGCard from '@/components/ui/AGCard.vue'
import AGButton from '@/components/ui/AGButton.vue'

const G = globalThis

const loading = ref(true)
const error = ref(false)
const config = ref(null)
const current = ref('feishu')
const form = reactive({ app_id: '', app_secret: '' })

const platforms = {
  feishu: {
    name: 'Feishu',
    nameZh: '飞书',
    color: '#3370FF',
    bg: 'color-mix(in srgb, #3370FF 14%, transparent)',
    bgLight: 'color-mix(in srgb, #3370FF 8%, transparent)',
    gradient: 'linear-gradient(135deg,#3370FF,#5B8EFF)',
    desc: 'Lark / Feishu Open Platform',
  },
  wecom: {
    name: 'WeCom',
    nameZh: '企业微信',
    color: '#07C160',
    bg: 'color-mix(in srgb, #07C160 14%, transparent)',
    bgLight: 'color-mix(in srgb, #07C160 8%, transparent)',
    gradient: 'linear-gradient(135deg,#07C160,#2DD879)',
    desc: 'WeCom / Enterprise WeChat',
    disabled: true,
  },
  dingtalk: {
    name: 'DingTalk',
    nameZh: '钉钉',
    color: '#1677FF',
    bg: 'color-mix(in srgb, #1677FF 14%, transparent)',
    bgLight: 'color-mix(in srgb, #1677FF 8%, transparent)',
    gradient: 'linear-gradient(135deg,#1677FF,#3D8BFF)',
    desc: 'DingTalk / DingTalk Open Platform',
    disabled: true,
  },
}

const cur = computed(() => platforms[current.value])
const curConfig = computed(() => platformConfig(current.value))
const isConfigured = computed(() => !!curConfig.value.app_id)

function ct(key) {
  return G.I18N.t(key)
}
function imt(key) {
  return G.I18N.t('chat.im_channel.' + key)
}
function dispName(p) {
  return G.I18N.getLang() === 'zh' ? p.nameZh : p.name
}
function platformConfig(key) {
  return (config.value && config.value.im && config.value.im[key]) || {}
}

function load() {
  loading.value = true
  error.value = false
  const repo = G.app.currentRepo()
  const qs = repo ? '?repo=' + encodeURIComponent(repo.fullName) : ''
  G.API.get('/chat/config' + qs)
    .then((d) => {
      config.value = d
      const p = d.im || {}
      if (p.dingtalk && p.dingtalk.app_id) current.value = 'dingtalk'
      else if (p.wecom && p.wecom.app_id) current.value = 'wecom'
      else current.value = 'feishu'
      syncForm()
      loading.value = false
    })
    .catch(() => {
      error.value = true
      loading.value = false
    })
}

function syncForm() {
  form.app_id = curConfig.value.app_id || ''
  form.app_secret = curConfig.value.app_secret_exists ? '********' : ''
}

// keep form in sync when switching provider
import { watch } from 'vue'
watch(current, () => syncForm())

function save() {
  const secret = form.app_secret === '********' ? '' : form.app_secret
  const im = {}
  im[current.value] = { app_id: form.app_id, app_secret: secret }
  const repo = G.app.currentRepo()
  const qs = repo ? '?repo=' + encodeURIComponent(repo.fullName) : ''
  G.API.put('/chat/config' + qs, { im })
    .then(() => {
      G.app.showToast(G.I18N.t('chat.config.saved'))
      load()
    })
    .catch((e) => G.app.showToast(G.I18N.t('chat.config.save_failed', { msg: e.message })))
}

/** 解绑当前渠道（Issue #294）：置空 app_id 走既有 PUT 语义，后端同步清理注册表/会话/映射。 */
function unbind() {
  G.app.showConfirm(
    imt('unbind_confirm'),
    function () {
      const im = {}
      im[current.value] = { app_id: '', app_secret: '' }
      const repo = G.app.currentRepo()
      const qs = repo ? '?repo=' + encodeURIComponent(repo.fullName) : ''
      G.API.put('/chat/config' + qs, { im })
        .then(() => {
          G.app.showToast(imt('unbind_success'))
          load()
        })
        .catch((e) => G.app.showToast(imt('unbind_failed', { msg: e.message })))
    },
    { danger: true }
  )
}

function scanOnboard() {
  G.startFeishuOnboard('chat_bot')
}

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
