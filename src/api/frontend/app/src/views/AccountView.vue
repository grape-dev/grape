<template>
  <div class="ac">
    <div class="cd">
      <div class="cd-h">
        <h2>{{ t('center_title') }}</h2>
        <button class="bt bt-o ac__back" @click="G.app._accountGoBack()">
          {{ t('back_to_workspace_btn') }}
        </button>
      </div>
      <div class="stabs">
        <a
          v-for="tb in tabs"
          :key="tb.id"
          :class="{ ac: tab === tb.id }"
          @click="switchTab(tb.id)"
          >{{ tb.label }}</a
        >
      </div>
      <div id="accountTabContent">
        <div v-if="tabLoading" class="loading">{{ t('loading') }}</div>
        <div v-else-if="tabError" class="error">{{ tabError }}</div>

        <!-- ═══ profile ═══ -->
        <template v-else-if="tab === 'profile'">
          <div class="cd">
            <div class="cd-h">
              <h2>{{ t('basic_info') }}</h2>
            </div>
            <div class="srow">
              <label>{{ t('username') }}</label
              ><input type="text" :value="profile.username" disabled class="ac__disabled" />
            </div>
            <div class="srow">
              <label>{{ t('email_label') }}</label
              ><input
                type="text"
                :value="profile.email || ''"
                disabled
                class="ac__disabled"
                :placeholder="t('email_not_set')"
              />
            </div>
            <div class="srow">
              <label>{{ t('nickname') }}</label
              ><input v-model="nickname" type="text" />
            </div>
            <button class="bt bt-p mt8" @click="saveProfile">{{ t('save') }}</button>
          </div>
        </template>

        <!-- ═══ security ═══ -->
        <template v-else-if="tab === 'security'">
          <!-- 密码管理 -->
          <div class="cd">
            <div class="cd-h">
              <h2>{{ t('password') }}</h2>
            </div>
            <template v-if="profile.has_password">
              <p class="tx t2 ac__mb">{{ t('password_already_set') }}</p>
              <button class="bt bt-p" @click="showChangePassword">
                {{ t('change_password') }}
              </button>
            </template>
            <template v-else>
              <p class="tx t2 ac__mb">{{ t('password_not_set') }}</p>
              <button class="bt bt-p" @click="showSetPassword">{{ t('set_password') }}</button>
            </template>
          </div>
          <!-- 邮箱管理（无邮箱配置时隐藏，R7） -->
          <div v-if="emailConfigured !== false" class="cd">
            <div class="cd-h">
              <h2>{{ t('email_label') }}</h2>
            </div>
            <p class="tx t2 ac__mb">{{ profile.email || t('email_not_set') }}</p>
            <button class="bt bt-o" @click="showChangeEmail">
              {{ profile.email ? t('change_email_btn') : t('set_email') }}
            </button>
          </div>
          <!-- 第三方账号 -->
          <div class="cd">
            <div class="cd-h">
              <h2>{{ t('third_party_accounts') }}</h2>
            </div>
            <div v-if="!providers.length" class="tx t2 ac__prow">
              {{ t('no_third_party_platforms') }}
            </div>
            <div v-for="p in providers" :key="p.provider" class="ac__prowwrap">
              <!-- token 过期 -->
              <div
                v-if="isBound(p.provider) && isExpired(p.provider)"
                class="ac__prow ac__prow--expired"
              >
                <div class="ac__flex1">
                  <div class="ac__pname">
                    <span v-html="platformIcon(p.provider, 20)"></span>
                    <span class="ac__name">{{ pName(p.provider) }}</span>
                    <span class="tg ac__expired-tag">{{ t('token_expired') }}</span>
                  </div>
                  <div class="ac__expired-hint">{{ t('token_expired_hint') }}</div>
                </div>
                <button class="bt bt-s bt-p" @click="bindProvider(p.provider)">
                  {{ t('re_bind') }}
                </button>
              </div>
              <!-- 已绑定 -->
              <div v-else-if="isBound(p.provider)" class="ac__prow ac__prow--bound">
                <span v-if="identityOf(p.provider)?.external_avatar" class="ac__avatar">
                  <img
                    :src="identityOf(p.provider).external_avatar"
                    @error="$event.target.style.display = 'none'"
                  />
                </span>
                <span v-else class="ac__avatar ac__avatar--text">{{
                  (identityOf(p.provider)?.external_login || pName(p.provider))[0].toUpperCase()
                }}</span>
                <div class="ac__flex1 ac__min0">
                  <div class="ac__pname">
                    <span v-html="platformIcon(p.provider, 20)"></span>
                    <span class="ac__name">{{ pName(p.provider) }}</span>
                    <span class="tg tg-d ac__tag10">{{ t('linked_badge') }}</span>
                  </div>
                  <div class="ac__idline">{{ identityLine(p.provider) }}</div>
                  <div v-if="identityOf(p.provider)?.created_at" class="ac__timeline">
                    {{ t('linked_on') }}{{ identityOf(p.provider).created_at.substring(0, 10) }}
                  </div>
                </div>
                <div class="ac__prow-btns">
                  <button class="bt bt-s bt-o" @click="bindProvider(p.provider)">
                    {{ t('update') }}
                  </button>
                  <button class="bt bt-s ac__unbind-btn" @click="unbindProvider(p.provider)">
                    {{ t('unbind') }}
                  </button>
                </div>
              </div>
              <!-- 未绑定 -->
              <div v-else class="ac__prow">
                <span
                  class="ac__avatar ac__avatar--icon"
                  v-html="platformIcon(p.provider, 20)"
                ></span>
                <div class="ac__flex1">
                  <div class="ac__pname">
                    <span class="ac__name">{{ pName(p.provider) }}</span>
                    <span class="tg tg-d ac__tag10">{{ t('not_linked') }}</span>
                  </div>
                  <div class="ac__linkhint">{{ t('link_to_manage_repos') }}</div>
                </div>
                <button class="bt bt-s bt-p" @click="bindProvider(p.provider)">
                  {{ t('link_btn') }}
                </button>
              </div>
            </div>
          </div>
          <!-- 注销账号 -->
          <div class="ac__danger">
            <button class="bt bt-r" @click="showDeleteAccount">{{ t('delete_account') }}</button>
          </div>
        </template>

        <!-- ═══ credentials ═══ -->
        <template v-else-if="tab === 'credentials'">
          <div class="cd">
            <div class="cd-h"><h2>Huawei Cloud</h2></div>
            <div class="srow ac__crow">
              <label>{{ t('access_key') }}</label>
              <div class="ac__cred-row">
                <input
                  v-model="hwAk"
                  :type="hwAkRevealed ? 'text' : 'password'"
                  autocomplete="new-password"
                  :placeholder="t('access_key')"
                  class="ac__flex1"
                />
                <!-- Issue #474：已配置才给 👁，走 reveal 接口二次确认取明文，不落缓存 -->
                <button
                  v-if="hwAkExists"
                  type="button"
                  class="ac__eye"
                  :title="hwAkRevealed ? t('hide_plaintext') : t('reveal_plaintext')"
                  @click="onRevealClick('ak')"
                >
                  {{ hwAkRevealed ? '🙈' : '👁' }}
                </button>
              </div>
            </div>
            <div class="srow ac__crow">
              <label>{{ t('secret_key') }}</label>
              <div class="ac__cred-row">
                <input
                  v-model="hwSk"
                  :type="hwSkRevealed ? 'text' : 'password'"
                  autocomplete="new-password"
                  :placeholder="t('secret_key')"
                  class="ac__flex1"
                />
                <button
                  v-if="hwSkExists"
                  type="button"
                  class="ac__eye"
                  :title="hwSkRevealed ? t('hide_plaintext') : t('reveal_plaintext')"
                  @click="onRevealClick('sk')"
                >
                  {{ hwSkRevealed ? '🙈' : '👁' }}
                </button>
              </div>
            </div>
            <p class="tx t2 mt8">{{ t('huawei_cloud_desc') }}</p>
            <button class="bt bt-p mt8" @click="saveCloudCreds">{{ t('save') }}</button>
          </div>
        </template>

        <!-- ═══ appearance ═══ -->
        <template v-else-if="tab === 'appearance'">
          <!-- 主题选择 -->
          <div class="cd">
            <h3 class="ac__h3">{{ t('theme') }}</h3>
            <p class="tx t2 ac__desc">{{ t('choose_theme') }}</p>
            <div class="ac__themegrid">
              <!-- Liquid Glass 预览卡 -->
              <div
                :class="['ac__themecard', { 'ac__themecard--on': theme === 'apple' }]"
                @click="selectTheme('apple')"
              >
                <div class="ac__themehead">
                  <span>Liquid Glass</span
                  ><span v-if="theme === 'apple'" class="ac__cur">{{ t('current') }}</span>
                </div>
                <div class="ac__preview ac__preview--apple">
                  <div class="ac__pv-side">
                    <div class="ac__pv-line" style="width: 24px; background: #d2d2d7"></div>
                    <div class="ac__pv-line" style="width: 20px; background: #d2d2d7"></div>
                    <div class="ac__pv-line" style="width: 20px; background: #0071e3"></div>
                    <div class="ac__pv-line" style="width: 20px; background: #d2d2d7"></div>
                  </div>
                  <div class="ac__pv-main">
                    <div
                      class="ac__pv-line"
                      style="width: 60%; background: var(--t); height: 4px"
                    ></div>
                    <div
                      class="ac__pv-line"
                      style="width: 40%; background: var(--t2); height: 3px"
                    ></div>
                    <div
                      class="ac__pv-card"
                      style="
                        background: var(--c);
                        border-color: var(--hairline);
                        border-radius: 12px;
                        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
                      "
                    ></div>
                  </div>
                </div>
                <p class="ac__themedesc">{{ t('theme_apple_desc') }}</p>
              </div>
            </div>
          </div>
          <!-- 显示模式（仅 apple） -->
          <div :class="['cd', { 'ac__disabled-box': theme !== 'apple' }]">
            <h3 class="ac__h3">{{ t('display_mode') }}</h3>
            <p class="tx t2 ac__modehint">{{ t('display_mode_hint') }}</p>
            <div class="ac__modebar">
              <button
                v-for="m in ['light', 'dark', 'auto']"
                :key="m"
                :class="['ac__modebtn', { 'ac__modebtn--on': mode === m }]"
                @click="selectMode(m)"
              >
                {{ modeLabel(m) }}
              </button>
            </div>
          </div>
          <!-- 语言 -->
          <div class="cd">
            <h3 class="ac__h3">{{ t('language') }}</h3>
            <p class="tx t2 ac__modehint">{{ t('choose_language') }}</p>
            <div class="ac__modebar">
              <button
                :class="['ac__modebtn', { 'ac__modebtn--on': lang === 'zh' }]"
                :disabled="langSwitching"
                @click="selectLang('zh')"
              >
                {{ langSwitching && lang === 'zh' ? t('switching') : '中文' }}
              </button>
              <button
                :class="['ac__modebtn', { 'ac__modebtn--on': lang === 'en' }]"
                :disabled="langSwitching"
                @click="selectLang('en')"
              >
                {{ langSwitching && lang === 'en' ? t('switching') : 'English' }}
              </button>
            </div>
          </div>
        </template>

        <!-- ═══ apikeys ═══ -->
        <template v-else-if="tab === 'apikeys'">
          <div class="cd">
            <div class="cd-h">
              <h2>API Keys</h2>
              <button class="bt bt-p ac__keybtn" @click="showCreateApiKey">
                {{ t('generate_key_btn') }}
              </button>
            </div>
            <div v-if="!apiKeys.length" class="tx t2 ac__keyempty">
              {{ t('no_api_keys_short') }}
            </div>
            <div v-for="k in apiKeys" :key="k.id" class="ac__keyrow">
              <div class="ac__flex1 ac__min0">
                <div class="ac__keyname">{{ k.name }}</div>
                <div class="ac__keyprefix">{{ k.prefix }}</div>
                <div class="ac__keymeta">
                  {{ t('created') }}: {{ fmtKeyTime(k.created_at) }}
                  <template v-if="k.last_used_at">
                    · {{ t('last_used') }}: {{ fmtKeyTime(k.last_used_at) }}</template
                  >
                </div>
              </div>
              <button class="bt bt-s ac__revoke-btn" @click="revokeApiKey(k)">
                {{ t('revoke') }}
              </button>
            </div>
          </div>
        </template>
      </div>
    </div>

    <!-- ═══ 通用弹窗（accountModal 等价物） ═══ -->
    <Teleport to="body">
      <div v-if="modal.open" class="ac__modal-mask" @click.self="onModalMask">
        <div class="ac__modal" v-html="modal.html"></div>
      </div>
    </Teleport>

    <!-- API key 明文弹窗 -->
    <Teleport to="body">
      <div v-if="apiKeyModal.open" class="ac__modal-mask" @click.self="apiKeyModal.open = false">
        <div class="ac__modal">
          <h3>{{ t('api_key_created_modal') }}</h3>
          <p class="tx ac__keywarn">{{ t('api_key_copy_warning') }}</p>
          <div class="ac__keybox">
            <code>{{ apiKeyModal.key }}</code>
          </div>
          <div class="ac__modal-btns">
            <button class="bt bt-p" @click="copyApiKey(apiKeyModal.key)">{{ t('copy') }}</button>
            <button class="bt bt-o" @click="apiKeyModal.open = false">{{ t('close') }}</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Issue #474：查看明文凭据（重新输入密码二次确认；明文只留在本次页面，不落缓存） -->
    <Teleport to="body">
      <div v-if="revealModal.open" class="ac__modal-mask" @click.self="revealModal.open = false">
        <div class="ac__modal">
          <h3>{{ t('reveal_title') }}</h3>
          <p class="tx t2 ac__reveal-desc">{{ t('reveal_desc') }}</p>
          <div class="srow">
            <label>{{ t('password') }}</label>
            <input
              v-model="revealModal.password"
              type="password"
              autocomplete="current-password"
              :placeholder="t('enter_password')"
              @keydown.enter.prevent="doReveal"
            />
          </div>
          <div v-if="revealModal.error" class="ac__err">{{ revealModal.error }}</div>
          <div class="ac__modal-btns">
            <button class="bt bt-o" @click="revealModal.open = false">{{ t('cancel') }}</button>
            <button class="bt bt-p" :disabled="revealModal.busy" @click="doReveal">
              {{ revealModal.busy ? t('loading') : t('reveal_confirm') }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
// Issue #1477：API Key 时间统一走本地时区格式化（后端返回 UTC ISO 串）
function fmtKeyTime(v) {
  if (!v) return '—'
  try {
    return (G.fmtLocalTime && G.fmtLocalTime(v)) || String(v)
  } catch (e) {
    return String(v)
  }
}

import { ref, reactive, onMounted } from 'vue'

const G = globalThis

// ── I18N（app.account.* 命名空间）──
function t(key, params) {
  const full = 'app.account.' + key
  const v = G.I18N.t(full, params)
  return v && v !== full ? v : DEFAULTS[key] || key
}
const DEFAULTS = {
  center_title: '个人中心',
  back_to_workspace_btn: '返回工作台',
  loading: '加载中...',
  nav_profile: '个人资料',
  security_tab: '账号安全',
  credentials_tab: '凭据管理',
  appearance_tab: '外观',
  apikeys_tab: 'API Keys',
  basic_info: '基本信息',
  username: '用户名',
  email_label: '邮箱',
  email_not_set: '未设置',
  nickname: '昵称',
  save: '保存',
  password: '密码',
  password_already_set: '已设置密码，可随时修改',
  password_not_set: '尚未设置密码，设置后可用账号密码登录',
  change_password: '修改密码',
  set_password: '设置密码',
  third_party_accounts: '第三方账号',
  no_third_party_platforms: '暂无可用的第三方平台',
  token_expired: 'Token Expired',
  token_expired_hint: '您的令牌已过期或被撤销，请重新绑定。',
  re_bind: '重新绑定',
  linked_badge: '已绑定',
  not_linked: '未绑定',
  link_to_manage_repos: '绑定后可管理该平台的仓库',
  update: '更新',
  unbind: '解绑',
  link_btn: '绑定',
  linked_on: '绑定于 ',
  delete_account: '注销账号',
  access_key: 'Access Key (AK)',
  secret_key: 'Secret Key (SK)',
  huawei_cloud_desc: '用于 SRE Agent 的华为云资源操作（ECS/VPC/OBS 等）',
  theme: '主题',
  choose_theme: '选择界面主题风格',
  current: '✓ 当前',
  theme_apple_desc: '毛玻璃效果 + 圆角设计',
  display_mode: '显示模式',
  display_mode_hint: '浅色/深色固定，自动模式跟随系统切换',
  light_mode: '浅色',
  dark_mode: '深色',
  auto_mode: '自动',
  language: '语言',
  choose_language: '选择界面显示语言',
  switching: '切换中...',
  generate_key_btn: '生成密钥',
  no_api_keys_short: '暂无 API 密钥',
  created: '创建',
  last_used: '最近使用',
  revoke: '吊销',
  api_key_created_modal: 'API 密钥已创建',
  api_key_copy_warning: '请立即复制保存，此密钥仅显示一次！',
  copy: '复制',
  close: '关闭',
  change_email_btn: '更换邮箱',
  set_email: '设置邮箱',
  // Issue #474：明文按需取回
  reveal_plaintext: '查看明文',
  hide_plaintext: '隐藏明文',
  reveal_title: '查看明文凭据',
  reveal_desc:
    '为安全起见，请重新输入当前 Grape 密码确认身份。明文将仅显示本次，不会保存在浏览器中。',
  reveal_confirm: '确认查看',
  reveal_failed: '明文取回失败',
  reveal_not_configured: '该凭据未配置',
  enter_password: '输入密码',
  cancel: '取消',
}
function modeLabel(m) {
  return t(m === 'light' ? 'light_mode' : m === 'dark' ? 'dark_mode' : 'auto_mode')
}

// ── helpers ──
function esc(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}
function platformIcon(provider, size) {
  return typeof G.window.platformIcon === 'function' ? G.window.platformIcon(provider, size) : ''
}
function pName(provider) {
  return provider === 'github' ? 'GitHub' : 'GitCode'
}

// ── tab 状态（hash 路由兼容：#account#security 等）──
const props = defineProps({ initialTab: { type: String, default: '' } })
const tabs = [
  { id: 'profile', label: t('nav_profile') },
  { id: 'security', label: t('security_tab') },
  { id: 'credentials', label: t('credentials_tab') },
  { id: 'appearance', label: t('appearance_tab') },
  { id: 'apikeys', label: 'API Keys' },
]
const tab = ref('profile')
const tabLoading = ref(false)
const tabError = ref('')
function resolveInitialTab() {
  if (
    props.initialTab &&
    tabs.some(function (x) {
      return x.id === props.initialTab
    })
  )
    return props.initialTab
  if (G.window.location.hash && G.window.location.hash !== '#account') {
    const hashTab = G.window.location.hash.replace('#account', '').replace('#', '')
    if (
      tabs.some(function (x) {
        return x.id === hashTab
      })
    )
      return hashTab
  }
  return 'profile'
}
function switchTab(id) {
  tab.value = id
  loadTab()
}

// ── profile ──
const profile = ref({})
const nickname = ref('')
function saveProfile() {
  const nick = nickname.value.trim()
  G.API.put('/auth/profile', { nickname: nick })
    .then(function () {
      profile.value.nickname = nick
      const nameEl = document.getElementById('userName')
      if (nameEl) nameEl.textContent = nick || profile.value.username
      G.app.showToast(t('profile_saved_success'))
    })
    .catch(function (e) {
      G.app.showToast(e.message || '操作失败')
    })
}

// ── security ──
const providers = ref([])
const emailConfigured = ref(null) // null=未知
const expiredMap = ref({})
function isBound(provider) {
  return (profile.value.bindings || []).indexOf(provider) >= 0
}
function isExpired(provider) {
  return !!expiredMap.value[provider]
}
function identityOf(provider) {
  return (profile.value.identities || []).find(function (i) {
    return i.provider === provider
  })
}
function identityLine(provider) {
  const identity = identityOf(provider)
  const login = (identity && identity.external_login) || ''
  const name = (identity && identity.external_name) || ''
  return name && name !== login ? login + ' · ' + name : login
}
function bindProvider(provider) {
  // 绑定方式弹窗（OAuth + 手动令牌）——完整 OAuth popup 流程
  const alreadyBound = isBound(provider)
  const providerInfo = providers.value.find(function (p) {
    return p.provider === provider
  })
  const oauthConfigured = !!(providerInfo && providerInfo.configured)
  let html =
    '<h3 style="display:flex;align-items:center;gap:8px">' +
    platformIcon(provider, 24) +
    ' ' +
    esc(
      alreadyBound
        ? t('update_platform_token', { platform: pName(provider) })
        : t('link_platform', { platform: pName(provider) })
    ) +
    '</h3>'
  if (oauthConfigured) {
    html += '<div class="stabs" style="margin-bottom:16px">'
    html += '<a class="ac" id="bindTabOauth">' + esc(t('oauth_auth')) + '</a>'
    html += '<a id="bindTabToken">' + esc(t('manual_token')) + '</a></div>'
    html +=
      '<div id="bindPanelOauth"><p class="tx t2" style="margin-bottom:16px">' +
      esc(
        alreadyBound
          ? t('oauth_rebind_hint', { platform: pName(provider) })
          : t('oauth_redirect_hint', { platform: pName(provider) })
      ) +
      '</p>'
    html +=
      '<button class="bt bt-p" style="width:100%" id="oauthBindBtn">' +
      esc(alreadyBound ? t('re_authorize') : t('authorize')) +
      '</button></div>'
    html += '<div id="bindPanelToken" style="display:none">' + manualBindForm(provider) + '</div>'
  } else {
    html += '<div id="bindPanelToken">' + manualBindForm(provider) + '</div>'
  }
  showModal(html, function (root) {
    const oauthBtn = root.querySelector('#oauthBindBtn')
    if (oauthBtn)
      oauthBtn.onclick = function () {
        doOauthBind(provider, this)
      }
    const tabOauth = root.querySelector('#bindTabOauth')
    const tabToken = root.querySelector('#bindTabToken')
    if (tabOauth && tabToken) {
      tabOauth.onclick = function () {
        root.querySelector('#bindPanelOauth').style.display = ''
        root.querySelector('#bindPanelToken').style.display = 'none'
        tabOauth.classList.add('ac')
        tabToken.classList.remove('ac')
      }
      tabToken.onclick = function () {
        root.querySelector('#bindPanelOauth').style.display = 'none'
        root.querySelector('#bindPanelToken').style.display = ''
        tabOauth.classList.remove('ac')
        tabToken.classList.add('ac')
      }
    }
    const manualBtn = root.querySelector('#manualBindBtn')
    if (manualBtn)
      manualBtn.onclick = function () {
        doManualBind(provider, this)
      }
  })
}
function manualBindForm(provider) {
  const helpSteps = provider === 'github' ? t('github_token_help') : t('gitcode_token_help')
  const providerInfo = providers.value.find(function (p) {
    return p.provider === provider
  })
  const oauthConfigured = !!(providerInfo && providerInfo.configured)
  let html = ''
  if (!oauthConfigured) {
    html +=
      '<div style="background:var(--bg);border:1px solid var(--b);border-radius:10px;padding:12px 14px;margin-bottom:12px;display:flex;gap:8px;align-items:flex-start"><span style="font-size:14px;flex-shrink:0">ℹ️</span><div style="font-size:12px;color:var(--t2);line-height:1.6">' +
      esc(t('pat_no_refresh_hint')) +
      '</div></div>'
  }
  html +=
    '<div class="srow"><label>' +
    esc(t('access_token')) +
    '</label><input type="password" id="manualTokenInput" class="kb-input" placeholder="' +
    esc(t('paste_token')) +
    '"></div>'
  html +=
    '<div id="manualBindError" style="display:none;color:var(--r);font-size:12px;padding:4px 0"></div>'
  html +=
    '<details style="margin:8px 0"><summary style="cursor:pointer;font-size:12px;color:var(--a)">' +
    esc(t('how_to_get_token')) +
    '</summary><div style="font-size:12px;color:var(--t2);padding:8px 0;line-height:1.6">' +
    helpSteps +
    '</div></details>'
  html +=
    '<button class="bt bt-p" style="width:100%" id="manualBindBtn">' +
    esc(t('confirm_bind')) +
    '</button>'
  return html
}
function translateBindError(e, provider) {
  let code = 0
  if (e && e.detail && typeof e.detail === 'object') code = e.detail.code || 0
  else if (e && e.status) code = e.status
  if (code === 401) return t('token_invalid_expired', { platform: pName(provider) })
  if (code === 409) return t('already_bound_to_another')
  if (code === 502) return t('network_error_retry')
  if (code === 400) return t('token_format_invalid')
  return (e && e.message) || t('bind_failed')
}
function doManualBind(provider, btn) {
  const root = btn.closest('.ac__modal')
  const tokenEl = root.querySelector('#manualTokenInput')
  const errEl = root.querySelector('#manualBindError')
  const token = (tokenEl.value || '').trim()
  if (!token) {
    if (errEl) {
      errEl.textContent = t('please_enter_token')
      errEl.style.display = 'block'
    }
    return
  }
  btn.disabled = true
  const origText = btn.textContent
  btn.innerHTML = '<span class="acct-btn-spinner"></span>' + esc(t('verifying'))
  if (errEl) errEl.style.display = 'none'
  const field = provider === 'github' ? 'github_token' : 'gitcode_token'
  const body = {}
  body[field] = token
  G.API.put('/settings/credentials', body, 15000)
    .then(function () {
      G.window.localStorage.removeItem('repo_member_cache')
      try {
        const _u = JSON.parse(G.window.localStorage.getItem('user') || '{}')
        if (!_u.bindings) _u.bindings = []
        if (_u.bindings.indexOf(provider) < 0) _u.bindings.push(provider)
        G.window.localStorage.setItem('user', JSON.stringify(_u))
      } catch (e) {
        /* ignore：与老代码同款静默降级 */
      }
      closeModal()
      showBindSuccessToast(provider)
      navigateToRepos()
    })
    .catch(function (e) {
      const detail = e && e.detail && typeof e.detail === 'object' ? e.detail : {}
      if (detail.need_transfer && detail.binding_ticket) {
        closeModal()
        showTransferConfirm(provider, detail.binding_ticket, token)
        return
      }
      btn.disabled = false
      btn.textContent = origText
      const msg = translateBindError(e, provider)
      if (errEl) {
        errEl.textContent = msg
        errEl.style.display = 'block'
      }
    })
}
// OAuth popup 绑定（同步上下文开窗防拦截 + postMessage + 轮询降级）
function oauthOpenCenteredPopup(url) {
  const w = 600,
    h = 700
  const screenLeft = G.window.screenLeft != null ? G.window.screenLeft : G.window.screenX
  const screenTop = G.window.screenTop != null ? G.window.screenTop : G.window.screenY
  const parentW = G.window.innerWidth || document.documentElement.clientWidth || 1440
  const parentH = G.window.innerHeight || document.documentElement.clientHeight || 900
  const left = Math.round(screenLeft + (parentW - w) / 2)
  const top = Math.round(screenTop + (parentH - h) / 2)
  return G.window.open(
    url,
    'oauth_popup',
    'width=' +
      w +
      ',height=' +
      h +
      ',left=' +
      left +
      ',top=' +
      top +
      ',resizable=yes,scrollbars=yes,status=yes'
  )
}
function doOauthBind(provider, btn) {
  if (btn) {
    btn.disabled = true
    btn.dataset.origText = btn.textContent
    btn.innerHTML = '<span class="acct-btn-spinner"></span>' + esc(t('verifying'))
  }
  const alreadyBound = isBound(provider)
  const promptParam = alreadyBound ? '&prompt=login' : ''
  // 移动端全页跳转
  if (/Mobi|Android|iPhone|iPad|iPod/i.test(G.window.navigator.userAgent)) {
    G.API.get('/auth/bind/' + provider + promptParam.replace('&', '?'))
      .then(function (d) {
        if (d.redirect_url) G.window.location.href = d.redirect_url
      })
      .catch(function (e) {
        if (btn) {
          btn.disabled = false
          btn.textContent = btn.dataset.origText || ''
        }
        G.app.showToast(e.message || '操作失败')
      })
    return
  }
  const popup = oauthOpenCenteredPopup('')
  if (!popup) {
    G.API.get('/auth/bind/' + provider + (promptParam ? '?prompt=login' : ''))
      .then(function (d2) {
        if (d2.redirect_url) G.window.location.href = d2.redirect_url
      })
      .catch(function () {
        if (btn) {
          btn.disabled = false
          btn.textContent = btn.dataset.origText || ''
        }
        G.app.showToast(t('popup_blocked_hint') || '操作失败')
      })
    return
  }
  if (modal.open) modal.locked = true
  G.API.get('/auth/bind/' + provider + '?popup=true' + promptParam)
    .then(function (d) {
      if (!d.redirect_url) throw new Error('no_redirect_url')
      popup.location.href = d.redirect_url
      let timeoutId = null
      let pollId = null
      let handled = false
      function cleanup() {
        if (timeoutId) {
          clearTimeout(timeoutId)
          timeoutId = null
        }
        if (pollId) {
          clearInterval(pollId)
          pollId = null
        }
        G.window.removeEventListener('message', handler)
      }
      function handler(ev) {
        if (ev.origin !== G.window.location.origin) return
        const data = ev.data
        if (!data || data.type !== 'oauth_result') return
        if (handled) return
        handled = true
        cleanup()
        if (data.action === 'bind' && data.success) confirmBindSuccess(provider, btn, true)
        else if (data.action === 'bind' && data.need_transfer) fetchTransferContext(provider, btn)
        else if (data.action === 'bind' && !data.success) {
          if (btn) {
            btn.disabled = false
            btn.textContent = btn.dataset.origText || ''
          }
          closeModal()
          G.app.showToast(translateOAuthError(data.error))
        } else {
          if (btn) {
            btn.disabled = false
            btn.textContent = btn.dataset.origText || ''
          }
          closeModal()
          G.app.showToast('操作失败')
        }
      }
      G.window.addEventListener('message', handler)
      pollId = setInterval(function () {
        if (handled) {
          cleanup()
          return
        }
        if (popup.closed) {
          handled = true
          cleanup()
          confirmBindSuccess(provider, btn)
        }
      }, 500)
      timeoutId = setTimeout(function () {
        if (!handled) {
          handled = true
          cleanup()
          if (btn) {
            btn.disabled = false
            btn.textContent = btn.dataset.origText || ''
          }
          if (popup.closed) confirmBindSuccess(provider, btn)
          else {
            closeModal()
            G.app.showToast(t('oauth_timeout_hint') || '操作失败')
          }
        }
      }, 60000)
    })
    .catch(function (e) {
      try {
        popup.close()
      } catch (e2) {
        /* ignore */
      }
      if (btn) {
        btn.disabled = false
        btn.textContent = btn.dataset.origText || ''
      }
      closeModal()
      G.app.showToast(e.message || '操作失败')
    })
}
function translateOAuthError(errorCode) {
  const key = 'app.login.oauth_error_' + errorCode
  const v = G.I18N.t(key)
  return v && v !== key ? v : errorCode || '操作失败'
}
function confirmBindSuccess(provider, btn, fromPostMessage) {
  const beforeBindings = profile.value.bindings || []
  const wasBound = beforeBindings.indexOf(provider) >= 0
  G.API.get('/auth/me')
    .then(function (d) {
      const afterBindings = d.bindings || []
      const nowBound = afterBindings.indexOf(provider) >= 0
      if (!nowBound) {
        if (btn) {
          btn.disabled = false
          btn.textContent = btn.dataset.origText || ''
        }
        closeModal()
        if (!wasBound) G.app.showToast(t('bind_cancelled') || '操作失败')
        return
      }
      if (!fromPostMessage && wasBound) {
        if (btn) {
          btn.disabled = false
          btn.textContent = btn.dataset.origText || ''
        }
        closeModal()
        return
      }
      try {
        const _u = JSON.parse(G.window.localStorage.getItem('user') || '{}')
        _u.bindings = afterBindings
        G.window.localStorage.setItem('user', JSON.stringify(_u))
      } catch (e2) {
        /* ignore */
      }
      G.window.localStorage.removeItem('repo_member_cache')
      showBindSuccessToast(provider)
      closeModal()
      navigateToRepos()
    })
    .catch(function () {
      if (btn) {
        btn.disabled = false
        btn.textContent = btn.dataset.origText || ''
      }
      G.app.showToast(t('bind_failed'))
    })
}
function fetchTransferContext(provider, btn) {
  G.API.get('/auth/me')
    .then(function (d) {
      const identity = (d.identities || []).find(function (i) {
        return i.provider === provider
      })
      const ticket = identity && identity.binding_ticket
      if (ticket) showTransferConfirm(provider, ticket)
      else {
        if (btn) btn.disabled = false
        closeModal()
        G.app.showToast('操作失败')
      }
    })
    .catch(function () {
      if (btn) btn.disabled = false
      closeModal()
      G.app.showToast('操作失败')
    })
}
function showTransferConfirm(provider, ticket, token) {
  const html =
    '<h3>' +
    esc(t('transfer_confirm_title')) +
    '</h3>' +
    '<p class="tx" style="margin-bottom:8px;line-height:1.5">' +
    t('transfer_confirm_msg', { platform: pName(provider) }) +
    '</p>' +
    '<p class="tx t2" style="margin-bottom:20px;line-height:1.5">' +
    esc(t('transfer_confirm_desc')) +
    '</p>' +
    '<div id="transferError" style="display:none;margin-bottom:12px;padding:8px 12px;border-radius:8px;background:rgba(220,40,40,0.08);color:var(--r);font-size:12px"></div>' +
    '<div style="display:flex;gap:8px;justify-content:flex-end">' +
    '<button class="bt bt-o" id="transferCancelBtn">' +
    esc(t('transfer_cancel_btn')) +
    '</button>' +
    '<button class="bt bt-p" id="transferConfirmBtn">' +
    esc(t('transfer_confirm_btn')) +
    '</button></div>'
  showModal(html, function (root) {
    root.querySelector('#transferCancelBtn').onclick = closeModal
    root.querySelector('#transferConfirmBtn').onclick = function () {
      const btn = this
      const errEl = root.querySelector('#transferError')
      btn.disabled = true
      btn.innerHTML = '<span class="acct-btn-spinner"></span>' + esc(t('transfer_confirm_btn'))
      if (errEl) errEl.style.display = 'none'
      G.API.post(
        '/auth/transfer-bind',
        { binding_ticket: ticket, confirm: true, access_token: token || '' },
        15000
      )
        .then(function () {
          G.window.localStorage.removeItem('repo_member_cache')
          try {
            const _u = JSON.parse(G.window.localStorage.getItem('user') || '{}')
            if (!_u.bindings) _u.bindings = []
            if (_u.bindings.indexOf(provider) < 0) _u.bindings.push(provider)
            G.window.localStorage.setItem('user', JSON.stringify(_u))
          } catch (e) {
            /* ignore：与老代码同款静默降级 */
          }
          closeModal()
          showBindSuccessToast(provider)
          navigateToRepos()
        })
        .catch(function (e) {
          btn.disabled = false
          btn.textContent = t('transfer_confirm_btn')
          const code =
            e && e.detail && typeof e.detail === 'object'
              ? e.detail.code || 0
              : (e && e.status) || 0
          let msg
          if (code === 409) msg = t('already_bound_to_another')
          else if (code === 401) msg = t('token_invalid_expired', { platform: pName(provider) })
          else msg = (e && e.message) || '操作失败'
          if (errEl) {
            errEl.textContent = msg
            errEl.style.display = 'block'
          }
        })
    }
  })
}
function showBindSuccessToast(provider) {
  const overlay = document.createElement('div')
  overlay.id = 'bindSuccessToast'
  overlay.style.cssText =
    'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:1002;background:var(--c);border-radius:16px;padding:28px 40px;box-shadow:0 16px 48px rgba(0,0,0,0.18);text-align:center;animation:bindToastIn .3s cubic-bezier(.34,1.56,.64,1)'
  overlay.innerHTML =
    '<div style="font-size:38px;margin-bottom:10px">🎉</div><div style="font-size:15px;font-weight:600;color:var(--t);margin-bottom:4px">' +
    esc(pName(provider)) +
    ' ' +
    esc(G.I18N.t('app.account.bind_success_title')) +
    '</div><div style="font-size:12px;color:var(--t3)">' +
    esc(G.I18N.t('app.account.repos_syncing')) +
    '</div>'
  document.body.appendChild(overlay)
  setTimeout(function () {
    overlay.style.transition = 'opacity .3s'
    overlay.style.opacity = '0'
    setTimeout(function () {
      overlay.remove()
    }, 300)
  }, 1800)
}
function navigateToRepos() {
  G.app.goToRepos()
}
// 解绑（含仓库影响警告 + 清理）
function unbindProvider(provider) {
  G.app.showConfirm(
    t('unbind_confirm'),
    function () {
      G.API.del('/auth/bind/' + provider)
        .then(function (d) {
          onUnbindSuccess(provider, d)
        })
        .catch(function (e) {
          if (e && e.detail && e.detail.need_confirm && e.detail.warehouses) {
            const wl = e.detail.warehouses
            const wlist = wl
              .map(function (w) {
                return (
                  '<div style="font-size:12px;color:var(--t2);padding:2px 0">• ' + esc(w) + '</div>'
                )
              })
              .join('')
            const wmsg =
              t('unbind_affected_repos_prefix') +
              '<div style="margin:8px 0">' +
              wlist +
              '</div>' +
              t('unbind_continue')
            G.app.showConfirm(
              wmsg,
              function () {
                G.API.del('/auth/bind/' + provider + '?confirm=true')
                  .then(function (d) {
                    onUnbindSuccess(provider, d)
                  })
                  .catch(onUnbindError)
              },
              { danger: true }
            )
          } else onUnbindError(e)
        })
    },
    { danger: true }
  )
}
function onUnbindSuccess(provider, d) {
  G.app.showToast((d && d.message) || G.I18N.t('app.account.unbind_success') || '已解绑')
  if (d && d.bindings) {
    try {
      const _u = JSON.parse(G.window.localStorage.getItem('user') || '{}')
      _u.bindings = d.bindings
      G.window.localStorage.setItem('user', JSON.stringify(_u))
    } catch (e) {
      /* ignore：与老代码同款静默降级 */
    }
  }
  G.window.localStorage.removeItem('repo_member_cache')
  // 清理被解绑平台的 currentRepo
  try {
    const cr = JSON.parse(G.window.sessionStorage.getItem('currentRepo') || 'null')
    if (cr && cr.repoKey && cr.repoKey.indexOf(provider + ':') === 0) {
      G.window.sessionStorage.removeItem('currentRepo')
    }
  } catch (e) {
    /* ignore：与老代码同款静默降级 */
  }
  setTimeout(function () {
    if (G.app.initSidebarRepos) G.app.initSidebarRepos()
  }, 100)
  loadTab()
}
function onUnbindError(e) {
  if (e && e.detail && e.detail.need_set_password) {
    G.app.showToast(t('keep_one_login_method'), t('set_password_before_unbind'))
  } else {
    G.app.showToast(e.message || '操作失败')
  }
}

// ── 密码/邮箱弹窗组 ──
function showChangePassword() {
  const hasEmail = profile.value && profile.value.email
  const html =
    '<h3>' +
    esc(t('change_password')) +
    '</h3>' +
    '<p class="tx t2" style="margin-bottom:12px">' +
    esc(t('change_password_logout_notice')) +
    '</p>' +
    '<div class="srow"><label>' +
    esc(t('current_password_label')) +
    '</label><input type="password" id="cpCurr" class="kb-input" autocomplete="current-password"></div>' +
    '<div class="srow"><label>' +
    esc(t('new_password_label')) +
    '</label><input type="password" id="cpNew" class="kb-input" autocomplete="new-password"></div>' +
    '<div class="srow"><label>' +
    esc(t('confirm_label')) +
    '</label><input type="password" id="cpConfirm" class="kb-input" autocomplete="new-password"></div>' +
    '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px">' +
    '<button class="bt bt-o" id="cpCancel">' +
    esc(t('cancel')) +
    '</button>' +
    '<button class="bt bt-p" id="cpOk">' +
    esc(t('confirm_change')) +
    '</button></div>' +
    (hasEmail && emailConfigured.value !== false
      ? '<p style="text-align:center;margin-top:12px"><a style="font-size:12px;color:var(--a);cursor:pointer;text-decoration:none" id="forgotLink">' +
        esc(G.I18N.t('app.account.forgot_password_link')) +
        '</a></p>'
      : '')
  showModal(html, function (root) {
    root.querySelector('#cpCancel').onclick = closeModal
    root.querySelector('#cpOk').onclick = function () {
      const curr = root.querySelector('#cpCurr').value.trim()
      const np = root.querySelector('#cpNew').value.trim()
      const cp = root.querySelector('#cpConfirm').value.trim()
      if (!curr || !np || !cp) {
        G.app.showToast(t('please_fill_all'))
        return
      }
      if (np !== cp) {
        G.app.showToast(t('passwords_do_not_match'))
        return
      }
      G.API.post('/auth/change-password', { current_password: curr, new_password: np })
        .then(function (d) {
          if (d.token) {
            G.window.localStorage.setItem('token', d.token)
            if (G.API.setToken) G.API.setToken(d.token)
          }
          closeModal()
          G.app.showToast(t('password_changed'), t('other_devices_logged_out'))
          loadTab()
        })
        .catch(function (e) {
          G.app.showToast(e.message || '操作失败')
        })
    }
    const forgot = root.querySelector('#forgotLink')
    if (forgot) forgot.onclick = showResetPassword
  })
}
function showResetPassword() {
  const email = profile.value.email
  if (!email || emailConfigured.value === false) {
    if (emailConfigured.value === false) G.app.showToast(t('email_not_configured'))
    else G.app.showToast(t('set_password_needs_email'))
    return
  }
  const html =
    '<h3>' +
    esc(t('reset_password')) +
    '</h3>' +
    '<p class="tx t2" style="margin-bottom:12px">' +
    esc(t('reset_password_hint')) +
    '</p>' +
    '<div id="rpStep1"><p class="tx t2" style="margin-bottom:12px">' +
    esc(t('code_sent_to_email_hint')) +
    '：<b>' +
    esc(email) +
    '</b></p>' +
    '<button class="bt bt-p" style="width:100%" id="rpSend">' +
    esc(t('send_code')) +
    '</button></div>' +
    '<div id="rpStep2" style="display:none;flex-direction:column">' +
    '<p class="tx t2" style="margin-bottom:8px">' +
    esc(t('code_sent')) +
    '</p>' +
    '<div class="srow"><label>' +
    esc(t('verification_code')) +
    '</label><input type="text" id="rpCode" class="kb-input" maxlength="6"></div>' +
    '<div class="srow"><label>' +
    esc(t('new_password')) +
    '</label><input type="password" id="rpPwd" class="kb-input" autocomplete="new-password"></div>' +
    '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px">' +
    '<button class="bt bt-o" id="rpCancel">' +
    esc(t('cancel')) +
    '</button>' +
    '<button class="bt bt-p" id="rpOk">' +
    esc(t('confirm_set')) +
    '</button></div></div>'
  showModal(html, function (root) {
    root.querySelector('#rpSend').onclick = function () {
      G.API.post('/auth/forgot-password/send-code', { email: email })
        .then(function () {
          G.app.showToast(t('code_sent'))
          root.querySelector('#rpStep1').style.display = 'none'
          root.querySelector('#rpStep2').style.display = 'flex'
        })
        .catch(function (e) {
          G.app.showToast(e.message || '操作失败')
        })
    }
    root.querySelector('#rpCancel').onclick = closeModal
    root.querySelector('#rpOk').onclick = function () {
      const code = root.querySelector('#rpCode').value.trim()
      const pwd = root.querySelector('#rpPwd').value.trim()
      if (!code || !pwd) {
        G.app.showToast(t('please_fill_all'))
        return
      }
      G.API.post('/auth/forgot-password/reset', { email: email, code: code, new_password: pwd })
        .then(function () {
          closeModal()
          G.window.localStorage.removeItem('token')
          G.window.localStorage.removeItem('user')
          G.app.showToast(t('password_changed'), t('other_devices_logged_out'))
          setTimeout(function () {
            G.window.location.href = '/'
          }, 1500)
        })
        .catch(function (e) {
          G.app.showToast(e.message || '操作失败')
        })
    }
  })
}
function showSetPassword() {
  if (emailConfigured.value === false) {
    G.app.showToast(t('email_not_configured'))
    return
  }
  if (!profile.value.email) {
    G.app.showToast(t('set_password_needs_email'))
    showChangeEmail()
    return
  }
  const email = profile.value.email
  const html =
    '<h3>' +
    esc(t('set_password')) +
    '</h3>' +
    '<p class="tx t2" style="margin-bottom:12px;line-height:1.6">' +
    esc(t('set_password_hint')) +
    '</p>' +
    '<div id="spStep1"><p class="tx t2" style="margin-bottom:12px">' +
    esc(t('code_sent_to_email_hint')) +
    '：<b>' +
    esc(email) +
    '</b></p>' +
    '<button class="bt bt-p" style="width:100%" id="spSend">' +
    esc(t('send_code')) +
    '</button></div>' +
    '<div id="spStep2" style="display:none;flex-direction:column">' +
    '<p class="tx t2" style="margin-bottom:8px">' +
    esc(t('code_sent')) +
    '</p>' +
    '<div class="srow"><label>' +
    esc(t('verification_code')) +
    '</label><input type="text" id="spCode" class="kb-input" maxlength="6"></div>' +
    '<div class="srow"><label>' +
    esc(t('new_password')) +
    '</label><input type="password" id="spPwd" class="kb-input" autocomplete="new-password"></div>' +
    '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px">' +
    '<button class="bt bt-o" id="spCancel">' +
    esc(t('cancel')) +
    '</button>' +
    '<button class="bt bt-p" id="spOk">' +
    esc(t('confirm_set')) +
    '</button></div></div>'
  showModal(html, function (root) {
    root.querySelector('#spSend').onclick = function () {
      G.API.post('/auth/set-password/send-code')
        .then(function () {
          G.app.showToast(t('code_sent'))
          root.querySelector('#spStep1').style.display = 'none'
          root.querySelector('#spStep2').style.display = 'flex'
        })
        .catch(function (e) {
          G.app.showToast(e.message || '操作失败')
        })
    }
    root.querySelector('#spCancel').onclick = closeModal
    root.querySelector('#spOk').onclick = function () {
      const code = root.querySelector('#spCode').value.trim()
      const pwd = root.querySelector('#spPwd').value.trim()
      if (!code || !pwd) {
        G.app.showToast(t('please_fill_all'))
        return
      }
      G.API.post('/auth/set-password', { code: code, new_password: pwd })
        .then(function () {
          closeModal()
          G.app.showToast(t('password_set_success'), t('can_login_with_password'))
          loadTab()
        })
        .catch(function (e) {
          G.app.showToast(e.message || '操作失败')
        })
    }
  })
}
function showChangeEmail() {
  if (emailConfigured.value === false) {
    G.app.showToast(t('email_not_configured'))
    return
  }
  const html =
    '<h3>' +
    esc(t('change_email_btn')) +
    '</h3>' +
    '<div id="ceStep1"><p class="tx t2" style="margin-bottom:12px">' +
    esc(t('code_sent_to_new_email_hint')) +
    '</p>' +
    '<div class="srow"><label>' +
    esc(t('new_email')) +
    '</label><input type="text" id="ceEmail" class="kb-input" placeholder="email@example.com"></div>' +
    '<button class="bt bt-p" style="width:100%" id="ceSend">' +
    esc(t('send_code')) +
    '</button></div>' +
    '<div id="ceStep2" style="display:none;flex-direction:column">' +
    '<p class="tx t2" style="margin-bottom:8px" id="ceStep2Hint">' +
    esc(t('code_sent_to_new_email')) +
    '</p>' +
    '<div class="srow"><label>' +
    esc(t('verification_code')) +
    '</label><input type="text" id="ceCode" class="kb-input" maxlength="6"></div>' +
    '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px">' +
    '<button class="bt bt-o" id="ceCancel">' +
    esc(t('cancel')) +
    '</button>' +
    '<button class="bt bt-p" id="ceOk">' +
    esc(t('confirm_change_email')) +
    '</button></div></div>'
  showModal(html, function (root) {
    root.querySelector('#ceSend').onclick = function () {
      const email = root.querySelector('#ceEmail').value.trim()
      if (!email) {
        G.app.showToast(t('enter_email'))
        return
      }
      G.API.post('/auth/change-email/send-code', { new_email: email })
        .then(function () {
          G.app.showToast(t('code_sent'))
          const hintEl = root.querySelector('#ceStep2Hint')
          if (hintEl)
            hintEl.innerHTML = esc(t('code_sent_to_new_email')) + '：<b>' + esc(email) + '</b>'
          root.querySelector('#ceStep1').style.display = 'none'
          root.querySelector('#ceStep2').style.display = 'flex'
        })
        .catch(function (e) {
          G.app.showToast(e.message || '操作失败')
        })
    }
    root.querySelector('#ceCancel').onclick = closeModal
    root.querySelector('#ceOk').onclick = function () {
      const email = root.querySelector('#ceEmail').value.trim()
      const code = root.querySelector('#ceCode').value.trim()
      if (!email || !code) {
        G.app.showToast(t('please_fill_all'))
        return
      }
      G.API.post('/auth/change-email', { new_email: email, code: code })
        .then(function () {
          closeModal()
          G.app.showToast(t('email_changed'), t('old_email_disabled'))
          loadTab()
        })
        .catch(function (e) {
          G.app.showToast(e.message || '操作失败')
        })
    }
  })
}

// ── 注销账号 ──
function showDeleteAccount() {
  const hasPwd = profile.value ? profile.value.has_password : false
  const hasEmail = profile.value ? !!profile.value.email : false
  const canVerifyByEmail = hasEmail && emailConfigured.value !== false
  const bindings = (profile.value && profile.value.bindings) || []
  const methods = []
  if (hasPwd) methods.push({ id: 'pwd', label: t('verify_with_password') })
  if (canVerifyByEmail) methods.push({ id: 'code', label: t('verify_with_code') })
  if (bindings.length > 0) methods.push({ id: 'oauth', label: t('verify_with_oauth') })
  if (methods.length === 0) {
    G.app.showToast(t('keep_one_login_method'))
    return
  }
  let html = '<h3 style="color:var(--r)">' + esc(t('delete_account')) + '</h3>'
  html +=
    '<p class="tx" style="color:var(--r);margin-bottom:12px">' +
    esc(t('delete_account_warning_short')) +
    '</p>'
  html +=
    '<div id="daStep1"><p class="tx t2" style="margin-bottom:10px">' +
    esc(t('choose_verification_method')) +
    '</p><div style="display:flex;flex-direction:column;gap:8px">'
  methods.forEach(function (m) {
    html +=
      '<button class="bt bt-o" style="width:100%" data-da="' +
      m.id +
      '">' +
      esc(m.label) +
      '</button>'
  })
  html += '</div></div>'
  html += '<div id="daStepPwd" style="display:none;flex-direction:column;margin-top:12px">'
  html +=
    '<div class="srow"><label>' +
    esc(t('password')) +
    '</label><input type="password" id="daPwd" class="kb-input" autocomplete="current-password"></div>'
  html +=
    '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:8px"><button class="bt bt-o" id="daCancel1">' +
    esc(t('cancel')) +
    '</button><button class="bt bt-r" id="daOk1">' +
    esc(t('confirm_delete')) +
    '</button></div></div>'
  html += '<div id="daStepCode" style="display:none;flex-direction:column;margin-top:12px">'
  html +=
    '<button class="bt bt-o" style="margin-bottom:8px" id="daSendCode">' +
    esc(t('send_code')) +
    '</button>'
  html +=
    '<div class="srow"><label>' +
    esc(t('verification_code')) +
    '</label><input type="text" id="daCode" class="kb-input" maxlength="6"></div>'
  html +=
    '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:8px"><button class="bt bt-o" id="daCancel2">' +
    esc(t('cancel')) +
    '</button><button class="bt bt-r" id="daOk2">' +
    esc(t('confirm_delete')) +
    '</button></div></div>'
  if (bindings.length > 0) {
    html += '<div id="daStepOauth" style="display:none;flex-direction:column;margin-top:12px">'
    html +=
      '<p class="tx t2" style="margin-bottom:8px">' + esc(t('oauth_in_progress_hint')) + '</p>'
    if (bindings.length === 1) {
      html +=
        '<button class="bt bt-r" style="width:100%" data-daoauth="' +
        bindings[0] +
        '">' +
        esc(t('confirm_delete')) +
        '</button>'
    } else {
      bindings.forEach(function (p) {
        html +=
          '<button class="bt bt-o" style="width:100%;margin-bottom:6px" data-daoauth="' +
          p +
          '">' +
          esc(pName(p)) +
          '</button>'
      })
    }
    html +=
      '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:8px"><button class="bt bt-o" id="daCancel3">' +
      esc(t('cancel')) +
      '</button></div></div>'
  }
  showModal(html, function (root) {
    root.querySelectorAll('[data-da]').forEach(function (b) {
      b.onclick = function () {
        root.querySelector('#daStep1').style.display = 'none'
        const step = root.querySelector(
          '#daStep' + (b.dataset.da === 'pwd' ? 'Pwd' : b.dataset.da === 'code' ? 'Code' : 'Oauth')
        )
        if (step) step.style.display = 'flex'
      }
    })
    ;['daCancel1', 'daCancel2', 'daCancel3'].forEach(function (id) {
      const el = root.querySelector('#' + id)
      if (el) el.onclick = closeModal
    })
    const ok1 = root.querySelector('#daOk1')
    if (ok1) ok1.onclick = doDeleteAccount
    const ok2 = root.querySelector('#daOk2')
    if (ok2) ok2.onclick = doDeleteAccount
    const sendCode = root.querySelector('#daSendCode')
    if (sendCode)
      sendCode.onclick = function () {
        G.API.post('/auth/delete-account/send-code')
          .then(function () {
            G.app.showToast(t('code_sent'))
          })
          .catch(function (e) {
            G.app.showToast(e.message || '操作失败')
          })
      }
    root.querySelectorAll('[data-daoauth]').forEach(function (b) {
      b.onclick = function () {
        doDeleteOauth(b.dataset.daoauth)
      }
    })
  })
}
function doDeleteAccount() {
  const root = document.querySelector('.ac__modal')
  const pwd = root.querySelector('#daPwd') ? root.querySelector('#daPwd').value.trim() : ''
  const code = root.querySelector('#daCode') ? root.querySelector('#daCode').value.trim() : ''
  if (!pwd && !code) {
    G.app.showToast(t('enter_password_or_code'))
    return
  }
  G.API.post('/auth/delete-account', { password: pwd, code: code })
    .then(function () {
      closeModal()
      G.window.localStorage.removeItem('token')
      G.window.localStorage.removeItem('user')
      G.app.showToast(t('account_deleted'), t('cooldown_started'))
      setTimeout(function () {
        G.window.location.href = '/'
      }, 2000)
    })
    .catch(function (e) {
      G.app.showToast(e.message || '操作失败')
    })
}
function doDeleteOauth(provider) {
  const popup = oauthOpenCenteredPopup('')
  if (!popup) {
    G.app.showToast(t('popup_blocked_hint') || '操作失败')
    return
  }
  if (modal.open) modal.locked = true
  G.API.get('/auth/delete-account/oauth/' + provider + '?popup=true&prompt=login')
    .then(function (d) {
      if (!d.redirect_url) throw new Error('no_redirect_url')
      popup.location.href = d.redirect_url
      let handled = false
      let timeoutId = null
      let pollId = null
      function cleanup() {
        if (timeoutId) {
          clearTimeout(timeoutId)
          timeoutId = null
        }
        if (pollId) {
          clearInterval(pollId)
          pollId = null
        }
        G.window.removeEventListener('message', handler)
      }
      function handler(ev) {
        if (ev.origin !== G.window.location.origin) return
        const data = ev.data
        if (!data || data.type !== 'oauth_result') return
        if (handled) return
        handled = true
        cleanup()
        if (data.action === 'delete' && data.delete_oauth_state) {
          G.API.post('/auth/delete-account', { oauth_state: data.delete_oauth_state })
            .then(function () {
              closeModal()
              G.window.localStorage.removeItem('token')
              G.window.localStorage.removeItem('user')
              G.app.showToast(t('account_deleted'), t('cooldown_started'))
              setTimeout(function () {
                G.window.location.href = '/'
              }, 2000)
            })
            .catch(function (e) {
              modal.locked = false
              G.app.showToast(e.message || '操作失败')
            })
        } else {
          modal.locked = false
          G.app.showToast(data.error || '操作失败')
        }
      }
      G.window.addEventListener('message', handler)
      pollId = setInterval(function () {
        if (handled) {
          cleanup()
          return
        }
        if (popup.closed) {
          handled = true
          cleanup()
          modal.locked = false
          G.app.showToast('操作失败')
        }
      }, 500)
      timeoutId = setTimeout(function () {
        if (!handled) {
          handled = true
          cleanup()
          modal.locked = false
          G.app.showToast(t('oauth_timeout_hint') || '操作失败')
        }
      }, 60000)
    })
    .catch(function (e) {
      try {
        popup.close()
      } catch (e2) {
        /* ignore */
      }
      modal.locked = false
      G.app.showToast(e.message || '操作失败')
    })
}

// ── credentials（Issue #474：接口只回存在性，字段用 ******** 占位，明文按需 reveal）──
const MASK = '********'
const hwAk = ref('')
const hwSk = ref('')
const hwAkExists = ref(false)
const hwSkExists = ref(false)
const hwAkRevealed = ref(false)
const hwSkRevealed = ref(false)
let cloudCredOriginal = { ak: '', sk: '' }
let cloudCredRevealed = { ak: null, sk: null }
const revealModal = reactive({ open: false, field: '', password: '', busy: false, error: '' })
function maskField(field) {
  if (field === 'ak') {
    hwAk.value = hwAkExists.value ? MASK : ''
    hwAkRevealed.value = false
  } else {
    hwSk.value = hwSkExists.value ? MASK : ''
    hwSkRevealed.value = false
  }
}
function onRevealClick(field) {
  const revealed = field === 'ak' ? hwAkRevealed.value : hwSkRevealed.value
  if (revealed) {
    // 再点一次 = 收回明文，回到掩码占位
    cloudCredRevealed = { ...cloudCredRevealed, [field]: null }
    maskField(field)
    return
  }
  revealModal.open = true
  revealModal.field = field
  revealModal.password = ''
  revealModal.error = ''
  revealModal.busy = false
}
function doReveal() {
  const pwd = (revealModal.password || '').trim()
  if (!pwd) {
    revealModal.error = t('enter_password')
    return
  }
  revealModal.busy = true
  revealModal.error = ''
  // provider 固定 huawei_cloud（本页只有华为云 AK/SK 走 reveal）
  G.API.post(
    '/settings/credentials/reveal',
    { provider: 'huawei_cloud', field: revealModal.field, password: pwd },
    15000
  )
    .then(function (d) {
      const value = (d && d.value) || ''
      revealModal.busy = false
      revealModal.open = false
      if (!value) {
        G.app.showToast(t('reveal_not_configured'))
        return
      }
      if (revealModal.field === 'ak') {
        hwAk.value = value
        hwAkRevealed.value = true
      } else {
        hwSk.value = value
        hwSkRevealed.value = true
      }
      cloudCredRevealed = { ...cloudCredRevealed, [revealModal.field]: value }
    })
    .catch(function (e) {
      revealModal.busy = false
      const msg = (e && ((e.detail && e.detail.message) || e.message)) || ''
      // 服务端提示（密码错误/频率限制）已可读，优先原样展示
      revealModal.error = msg || t('reveal_failed')
    })
}
function saveCloudCreds() {
  const body = {}
  const akVal = hwAk.value.trim()
  const skVal = hwSk.value.trim()
  // 占位符/未改动/reveal 回来的原值都不回传（清空 = 显式清除）
  if (akVal !== MASK && akVal !== cloudCredOriginal.ak && akVal !== (cloudCredRevealed.ak || ''))
    body.huawei_cloud_ak = akVal
  if (skVal !== MASK && skVal !== cloudCredOriginal.sk && skVal !== (cloudCredRevealed.sk || ''))
    body.huawei_cloud_sk = skVal
  if (Object.keys(body).length === 0) {
    G.app.showToast(t('no_changes'))
    return
  }
  G.API.put('/settings/credentials', body, 15000)
    .then(function () {
      G.app.showToast(t('credentials_saved'))
      loadTab()
    })
    .catch(function (e) {
      G.app.showToast(t('save_failed') + ': ' + (e.message || ''))
    })
}

// ── appearance ──
const theme = ref('apple')
const mode = ref('light')
const lang = ref('zh')
const langSwitching = ref(false)
// 2026-11-12 默认风格退役（第一步）：外观页只保留 Liquid Glass，setTheme 只接受 apple
function selectTheme(th) {
  if (!G.app.setTheme) {
    G.app.showToast(t('theme_switch_unavailable'))
    return
  }
  G.app.setTheme('apple')
  theme.value = 'apple'
  G.app.showToast(t('switched_to_liquid_glass'))
}
function selectMode(m) {
  if (!G.app.setThemeMode) return
  G.app.setThemeMode(m)
  mode.value = m
}
function selectLang(l) {
  if (l === lang.value && G.I18N.isLangLoaded(l)) return
  if (langSwitching.value) return
  langSwitching.value = true
  G.I18N.setLang(l)
    .then(function (result) {
      lang.value = l
      if (result.failed === 0) {
        // Issue #273：语言切换成功不再弹「Success」（用户已看到界面语言变化，弹窗是噪音）
      } else if (result.failed < result.total) {
        G.app.showToast(
          G.I18N.t('common.success') +
            ' (' +
            G.I18N.t('app.account.partial_load_failed_suffix', {
              failed: result.failed,
              total: result.total,
            }) +
            ')'
        )
      } else {
        G.app.showToast(G.I18N.t('common.error'))
      }
    })
    .catch(function (e) {
      G.app.showToast(G.I18N.t('common.error') + ': ' + (e && e.message ? e.message : ''))
    })
    .finally(function () {
      langSwitching.value = false
    })
}

// ── apikeys ──
const apiKeys = ref([])
const apiKeyModal = reactive({ open: false, key: '' })
function showCreateApiKey() {
  G.app.showPrompt({
    title: t('enter_key_name_prompt'),
    placeholder: t('enter_key_name_prompt'),
    confirmText: t('confirm_create') || '确认',
    validate: function (val) {
      return !!val
    },
    onConfirm: function (name) {
      G.API.post('/settings/api-keys', { name: name })
        .then(function (d) {
          apiKeyModal.key = d.plaintext || d.key || ''
          apiKeyModal.open = true
          loadApiKeys()
        })
        .catch(function (e) {
          G.app.showToast(e.message || '操作失败')
        })
    },
  })
}
function copyApiKey(key) {
  if (G.window.navigator.clipboard) {
    G.window.navigator.clipboard.writeText(key).then(function () {
      G.app.showToast(t('copied'))
    })
  } else {
    const ta = document.createElement('textarea')
    ta.value = key
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy')
      G.app.showToast(t('copied'))
    } catch (e) {
      G.app.showToast('复制失败')
    }
    ta.remove()
  }
}
function revokeApiKey(k) {
  G.app.showConfirm(
    t('revoke_key_confirm', { name: k.name }),
    function () {
      G.API.del('/settings/api-keys/' + k.id)
        .then(function () {
          G.app.showToast(t('revoked'))
          loadApiKeys()
        })
        .catch(function (e) {
          G.app.showToast(e.message || '操作失败')
        })
    },
    { danger: true }
  )
}

// ── 通用 modal（等价老 _accountShowModal：innerHTML + 绑定回调）──
const modal = reactive({ open: false, html: '', locked: false })
function showModal(html, binder) {
  modal.html = html
  modal.open = true
  modal.locked = false
  if (binder) {
    // v-html 渲染是异步的——nextTick 后绑定
    setTimeout(function () {
      const root = document.querySelector('.ac__modal')
      if (root) binder(root)
    }, 0)
  }
}
function closeModal() {
  modal.open = false
  modal.locked = false
}
function onModalMask() {
  if (!modal.locked) closeModal()
}

// ── tab 加载 ──
function loadTab() {
  tabError.value = ''
  if (tab.value === 'profile') {
    tabLoading.value = true
    G.API.get('/auth/me')
      .then(function (d) {
        profile.value = d
        nickname.value = d.nickname || ''
        tabLoading.value = false
      })
      .catch(function (e) {
        tabError.value = e.message || t('load_failed')
        tabLoading.value = false
      })
  } else if (tab.value === 'security') {
    tabLoading.value = true
    G.API.get('/auth/providers')
      .then(function (pd) {
        emailConfigured.value = !!(pd && pd.email_configured)
        providers.value = (pd && pd.providers) || []
        return G.API.get('/auth/me')
      })
      .catch(function () {
        emailConfigured.value = true
        providers.value = []
        return G.API.get('/auth/me')
      })
      .then(function (d) {
        profile.value = d
        const m = {}
        ;(d.token_expired_providers || []).forEach(function (ep) {
          if (typeof ep === 'object') m[ep.provider] = ep
          else m[ep] = { provider: ep, error_code: 'TOKEN_EXPIRED' }
        })
        expiredMap.value = m
        tabLoading.value = false
      })
      .catch(function (e) {
        tabError.value = e.message || t('load_failed')
        tabLoading.value = false
      })
  } else if (tab.value === 'credentials') {
    tabLoading.value = true
    G.API.get('/settings/credentials')
      .then(function (d) {
        const hw = d.huawei_cloud || {}
        // Issue #474：接口只回存在性（ak_exists/sk_exists），不回明文 → 已配置显示 ******** 占位
        hwAkExists.value = !!hw.ak_exists
        hwSkExists.value = !!hw.sk_exists
        hwAk.value = hwAkExists.value ? MASK : ''
        hwSk.value = hwSkExists.value ? MASK : ''
        hwAkRevealed.value = false
        hwSkRevealed.value = false
        cloudCredOriginal = { ak: hwAk.value, sk: hwSk.value }
        cloudCredRevealed = { ak: null, sk: null }
        tabLoading.value = false
      })
      .catch(function (e) {
        tabError.value = e.message || t('load_failed')
        tabLoading.value = false
      })
  } else if (tab.value === 'appearance') {
    theme.value = G.app.getTheme
      ? G.app.getTheme()
      : G.window.localStorage.getItem('grapeTheme') || 'apple'
    mode.value = G.app.getThemeMode
      ? G.app.getThemeMode()
      : G.window.localStorage.getItem('grapeThemeMode') || 'light'
    lang.value = G.I18N ? G.I18N.getLang() : G.window.localStorage.getItem('grapeLang') || 'zh'
  } else if (tab.value === 'apikeys') {
    loadApiKeys()
  }
}
function loadApiKeys() {
  tabLoading.value = true
  G.API.get('/settings/api-keys')
    .then(function (data) {
      let keys = (data && data.keys) || data || []
      if (!Array.isArray(keys)) keys = []
      apiKeys.value = keys
      tabLoading.value = false
    })
    .catch(function (e) {
      tabError.value = e.message || t('load_failed')
      tabLoading.value = false
    })
}

// app.js 契约：OAuth 回调页 (need_transfer) 延迟调 _accountShowTransferConfirm
G.window._accountShowTransferConfirm = showTransferConfirm

onMounted(function () {
  tab.value = resolveInitialTab()
  loadTab()
})
</script>

<style scoped>
.ac {
  min-height: 100%;
}
.ac__back {
  margin-left: auto;
  font-size: 12px;
  padding: 4px 12px;
}
.ac__mb {
  margin-bottom: 12px;
}
.ac__prowwrap {
  margin-bottom: 0;
}
.ac__prow {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px 16px;
  border: 1px solid var(--b);
  border-radius: 12px;
  background: var(--c);
  margin-bottom: 10px;
  box-shadow: var(--sh);
}
.ac__prow--expired {
  border-color: var(--r);
}
.ac__flex1 {
  flex: 1;
  min-width: 0;
}
.ac__min0 {
  min-width: 0;
}
.ac__pname {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
}
.ac__name {
  font-size: 14px;
  font-weight: 600;
  color: var(--t);
}
.ac__tag10 {
  font-size: 10px;
}
.ac__expired-tag {
  font-size: 10px;
  background: var(--r);
  color: var(--on-accent);
  padding: 2px 6px;
  border-radius: 4px;
}
.ac__expired-hint {
  font-size: 12px;
  color: var(--r);
  margin-bottom: 8px;
}
.ac__avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  overflow: hidden;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}
.ac__avatar img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.ac__avatar--text {
  background: var(--bg);
  font-size: 14px;
  font-weight: 600;
  color: var(--t2);
}
.ac__avatar--icon {
  background: var(--bg);
  opacity: 0.5;
}
.ac__idline {
  font-size: 12px;
  color: var(--t2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ac__timeline {
  font-size: 11px;
  color: var(--t3);
  margin-top: 2px;
}
.ac__prow-btns {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}
.ac__unbind-btn {
  color: var(--r);
  border-color: var(--r);
  font-size: 11px;
}
.ac__linkhint {
  font-size: 12px;
  color: var(--t3);
  margin-top: 2px;
}
.ac__danger {
  border-top: 1px solid var(--b);
  margin-top: 24px;
  padding-top: 20px;
}
.ac__crow {
  align-items: center;
}
.ac__cred-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
}
.ac__eye {
  border: 1px solid var(--b);
  background: var(--bg);
  border-radius: 8px;
  width: 30px;
  height: 30px;
  padding: 0;
  font-size: 14px;
  cursor: pointer;
  color: var(--t3);
  flex-shrink: 0;
}
.ac__h3 {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 12px;
}
.ac__desc {
  margin-bottom: 20px;
  font-size: 12px;
  line-height: 1.6;
}
.ac__themegrid {
  /* 默认风格退役后只剩一张卡：单列，最宽 520px，别留半张空位 */
  display: grid;
  grid-template-columns: minmax(0, 520px);
  gap: 16px;
  margin-bottom: 24px;
}
.ac__themecard {
  cursor: pointer;
  border: 2px solid var(--b);
  border-radius: 14px;
  padding: 20px;
  transition: all 0.25s;
  background: var(--c);
}
.ac__themecard--on {
  border-color: var(--a);
}
.ac__themehead {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
  font-size: 14px;
  font-weight: 600;
  color: var(--t);
}
.ac__cur {
  color: var(--a);
  font-size: 12px;
  font-weight: 600;
}
.ac__preview {
  height: 80px;
  border-radius: 10px;
  overflow: hidden;
  border: 1px solid var(--b);
  display: flex;
}
.ac__pv-side {
  width: 60px;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 8px 0;
  gap: 4px;
}
.ac__pv-line {
  height: 3px;
  border-radius: 2px;
}
.ac__pv-main {
  flex: 1;
  padding: 10px;
}
.ac__pv-card {
  height: 30px;
  border-radius: 6px;
  border: 1px solid;
  margin-top: 8px;
}
.ac__themedesc {
  font-size: 11px;
  color: var(--t3);
  margin-top: 10px;
}
.ac__disabled-box {
  opacity: 0.4;
  pointer-events: none;
}
.ac__modehint {
  margin-bottom: 14px;
  font-size: 12px;
}
.ac__modebar {
  display: flex;
  gap: 10px;
}
.ac__modebtn {
  padding: 10px 22px;
  border-radius: 980px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  border: 1px solid var(--b);
  background: var(--c);
  color: var(--t2);
  transition: all 0.2s;
}
.ac__modebtn--on {
  background: var(--a);
  color: var(--on-accent);
  border-color: var(--a);
}
.ac__keybtn {
  font-size: 12px;
  padding: 7px 16px;
}
.ac__keyempty {
  padding: 16px;
  text-align: center;
}
.ac__keyrow {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--b);
}
.ac__keyname {
  font-size: 12px;
  font-weight: 600;
  color: var(--t);
}
.ac__keyprefix {
  font-size: 11px;
  color: var(--t3);
  font-family: monospace;
  margin-top: 2px;
}
.ac__keymeta {
  font-size: 10px;
  color: var(--t3);
  margin-top: 2px;
}
.ac__revoke-btn {
  color: var(--r);
}
.ac__modal-mask {
  position: fixed;
  inset: 0;
  z-index: 1001;
  background: rgba(0, 0, 0, 0.35);
  backdrop-filter: blur(6px);
  display: flex;
  align-items: center;
  justify-content: center;
}
.ac__modal {
  width: 440px;
  max-width: 92vw;
  background: var(--c);
  border-radius: 16px;
  padding: 26px;
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.12);
}
.ac__modal h3 {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 12px;
}
.ac__modal-btns {
  display: flex;
  gap: 10px;
  justify-content: flex-end;
  margin-top: 16px;
}
.ac__keywarn {
  color: var(--r);
  margin-bottom: 12px;
}
.ac__keybox {
  background: var(--bg);
  border: 1px solid var(--b);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.ac__keybox code {
  font-size: 12px;
  word-break: break-all;
}
/* Issue #474：reveal 弹窗说明与错误提示（与弹窗同宽的内联文案） */
.ac__reveal-desc {
  margin-bottom: 12px;
}
.ac__err {
  color: var(--r);
  font-size: 12px;
  margin-top: 8px;
}
.ac__disabled {
  background: var(--chip-bg);
}
</style>
