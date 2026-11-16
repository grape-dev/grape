<!-- 引导页（onboarding）：整页 Vue 化。
     原先由 shell app.js `_renderOnboardingShell` 字符串拼 DOM，2026-11-14 按要求改为 view chunk：
     卡面 = logo + 平台名 + [绑定]；点卡打开共享 BindDialog（allowPat=true，OAuth + PAT 双模式）。
     语言切换、组织下拉、用户下拉沿用 shell 的既有 id 与 app.* 全局方法（同一份菜单逻辑，
     不在这里复制一遍）。 -->
<template>
  <div class="lobby">
    <header class="lobby-head">
      <div class="lobby-brand">
        <img class="lobby-logo" :src="logoUrl" alt="Grape" />
        <span class="lobby-brand-name">Grape</span>
        <span class="lobby-brand-sub">{{ tGlobal('repos.brand_sub') }}</span>
      </div>
      <div class="lv-head-right">
        <div class="lang-toggle lv-lang">
          <button class="lang-btn" :class="lang === 'zh' ? 'active' : ''" @click="setLang('zh')">中</button>
          <button class="lang-btn" :class="lang === 'en' ? 'active' : ''" @click="setLang('en')">EN</button>
        </div>
        <div id="lobbyOrgDropdown" class="lv-drop">
          <button id="lobbyOrgBtn" class="lv-btn" @click="toggleOrg()">
            <span class="lv-emoji">🏢</span>
            <span class="lobby-org-name" id="lobbyOrgName">{{ tGlobal('organizations.my_orgs') }}</span>
          </button>
          <div id="lobbyOrgMenu" class="lv-menu" style="display: none"></div>
        </div>
        <div id="lobbyUserDropdown" class="lv-drop">
          <button class="lv-btn" @click="toggleUser()">
            <span class="lobby-avatar">{{ avatar }}</span>
            <span class="lobby-user-name">{{ name }}</span>
          </button>
          <div id="lobbyUserMenu" class="lv-menu" style="display: none"></div>
        </div>
      </div>
    </header>
    <main class="lobby-main">
      <div class="lobby-content ob-content">
        <section class="ob-hero" :class="heroDone ? 'ob-bound-done' : ''">
          <div class="ob-hero-logo">
            <img :src="logoUrl" alt="Grape" />
          </div>
          <h1 class="ob-hero-title">{{ tGlobal('app.login.ob_cta_title') }}</h1>
          <p class="ob-hero-sub">{{ tGlobal('app.login.ob_cta_sub') }}</p>
          <div class="ob-hero-bind" :data-guide="tGlobal('app.login.ob_bind_guide')">
            <div
              v-for="p in providers"
              :key="p.provider"
              class="ob-bind-card"
              :class="[cardState(p.provider)]"
              :style="'--brand:' + brandColor(p)"
              @click="openBind(p.provider)"
            >
              <div class="ob-bind-icon" v-html="platformIcon(p.provider, 28)"></div>
              <div class="ob-bind-name">{{ pLabel(p) }}</div>
              <button class="bt bt-p ob-bind-btn">{{ tGlobal('app.login.ob_bind_btn') }}</button>
              <div v-if="justBound === p.provider" class="ob-bound-check">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M20 6L9 17l-5-5" />
                </svg>
              </div>
            </div>
          </div>
          <p class="ob-hero-hint">{{ tGlobal('app.login.ob_hero_hint') }}</p>
        </section>
      </div>
    </main>

    <BindDialog
      :open="bindOpen"
      :provider="bindProvider"
      :allow-pat="true"
      :already-bound="bindAlreadyBound"
      :providers="providers"
      chrome="shell"
      @close="bindOpen = false"
      @bound="onBound"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import BindDialog from '../components/BindDialog.vue'

const G = globalThis
// 静态资源走运行时 URL（构建期不解析 /static/...，与 Register/Forgot/Admin 视图同款）
const logoUrl = '/static/img/grape-logo.jpg?v=1'

const props = defineProps({
  providers: { type: Array, default: () => [] },
  name: { type: String, default: '' },
  avatar: { type: String, default: '' },
  lang: { type: String, default: 'zh' },
})
const emit = defineEmits(['bound'])

// 非账号域键（repos.* / organizations.* / app.login.*）直读语言包
const langVer = ref(0)
function tGlobal(key, vars) {
  void langVer.value // 语言切换后重渲染（setLang 在语言包就位后派发 grape:langchange）
  const got = G.I18N && G.I18N.t ? G.I18N.t(key, vars) : key
  return got && got !== key ? got : key
}
try {
  G.window.addEventListener('grape:langchange', () => { langVer.value += 1 })
} catch (e) { /* ignore */ }
function platformIcon(provider, size) {
  return typeof G.window.platformIcon === 'function' ? G.window.platformIcon(provider, size) : ''
}
// 平台名真源同 shell/js/platform-icons.js（华为云在英文界面不再显示中文「华为」）
function pLabel(p) {
  const label = typeof G.window.platformLabel === 'function' ? G.window.platformLabel(p.provider) : ''
  return label || p.name || p.provider
}

const bindOpen = ref(false)
const bindProvider = ref('')
const bindAlreadyBound = ref(false)
const justBound = ref('')
const heroDone = ref(false)
const boundCards = ref([])

const lang = computed(function () {
  return (G.I18N && G.I18N.getLang ? G.I18N.getLang() : props.lang) || 'zh'
})

function brandColor(p) {
  // 品牌色单一真源：shell/js/platform-icons.js 的 BRAND（含 gitee/gitlab/华为）
  const fromCatalog = typeof window.platformColor === 'function' ? window.platformColor(p.provider) : ''
  return fromCatalog || p.brand_color || 'var(--a)'
}

function cardState(provider) {
  const out = []
  if (boundCards.value.indexOf(provider) >= 0 || bindingsOfUser().indexOf(provider) >= 0) out.push('bound')
  return out
}

function bindingsOfUser() {
  // 「已接入」= 身份绑定 ∪ 用户↔平台关系（PAT 接入无身份行）——只认 bindings 会让
  // 纯 PAT 账号在引导页看到"未绑定"（2026-11-14 深度检查）
  const out = []
  try {
    ;(JSON.parse(G.window.localStorage.getItem('user') || '{}').bindings || []).forEach((b) => out.push(b))
  } catch (e) { /* ignore */ }
  ;((window.__repoPlatformLinks && window.__repoPlatformLinks.providers) || []).forEach((b) => {
    if (out.indexOf(b) < 0) out.push(b)
  })
  return out
}

function setLang(l) {
  if (typeof G.window._setAppLang === 'function') G.window._setAppLang(l)
}

function toggleOrg() {
  if (G.app && typeof G.app._toggleLobbyOrgDropdown === 'function') G.app._toggleLobbyOrgDropdown()
}

function toggleUser() {
  if (G.app && typeof G.app._toggleLobbyUserMenu === 'function') G.app._toggleLobbyUserMenu()
}

function openBind(provider) {
  bindProvider.value = provider
  bindAlreadyBound.value = bindingsOfUser().indexOf(provider) >= 0
  bindOpen.value = true
}

// 绑定成功后的唯一出口：goToRepos()（= _obExitMode 摘掉 ob-mode 全屏、_pushHistory('repos')、
// 置 _currentView='repos' 再渲染项目页）。原实现只调 loadRepos()：它不摘 ob-mode、不改路由，
// 于是页面仍是引导页布局——用户看到「绑定成功了却还在引导页」（2026-11-14 实锤）。
function _leaveOnboarding() {
  if (G.app && typeof G.app.goToRepos === 'function') { G.app.goToRepos(); return }
  // 旧壳兜底
  if (G.window._obExitMode) { try { G.window._obExitMode() } catch (e) { /* ignore */ } }
  if (G.app && typeof G.app.loadRepos === 'function') G.app.loadRepos()
}

function onBound(provider) {
  if (boundCards.value.indexOf(provider) < 0) boundCards.value.push(provider)
  emit('bound', provider)
  // 顺序：先刷新关系快照（PAT/OAuth 关系行是服务端写的，它会写 window.__repoPlatformLinks
  // 与 localStorage.user.repo_platform_links），再回读 /auth/me，最后才跳——否则跳过去
  // 时 _hasVcsBinding() 还是旧快照，平台栏/仓库列表为空。
  const links = (G.app && typeof G.app.refreshRepoLinks === 'function')
    ? G.app.refreshRepoLinks()
    : Promise.resolve(null)
  Promise.resolve(links)
    // /auth/profile 同时带 bindings 与 repo_platform_links：前者刷新身份快照，
    // 后者兜底关系快照（refreshRepoLinks 失败时 goToRepos 会因"无关系"又回引导页）
    .then(function () { return G.API.get('/auth/profile').catch(function () { return null }) })
    .then(function (me) {
      try {
        const u = JSON.parse(G.window.localStorage.getItem('user') || '{}')
        if (me && me.bindings) {
          u.bindings = me.bindings
          G.window.localStorage.setItem('user', JSON.stringify(u))
        }
        if (me && me.repo_platform_links && me.repo_platform_links.source) {
          G.window.__repoPlatformLinks = me.repo_platform_links
          u.repo_platform_links = me.repo_platform_links
          G.window.localStorage.setItem('user', JSON.stringify(u))
        }
      } catch (e) {
        /* 快照失败不阻断 */
      }
      setTimeout(_leaveOnboarding, 300)
    })
}

onMounted(function () {
  // 兜底终态：卡面入场动画结束后锁 opacity（等价 shell 版 1.5s 兜底）
  setTimeout(function () {
    document.querySelectorAll('.ob-bind-card').forEach(function (c) {
      c.classList.add('ob-shown')
    })
  }, 1500)
  // OAuth 回跳的迟到成功态：卡片此刻才存在，应用「已绑定 ✓」再进仓库列表
  try {
    const pending = G.window.sessionStorage.getItem('ob_pending_bind_success')
    if (pending) {
      G.window.sessionStorage.removeItem('ob_pending_bind_success')
      justBound.value = pending
      heroDone.value = true
      if (boundCards.value.indexOf(pending) < 0) boundCards.value.push(pending)
      setTimeout(_leaveOnboarding, 1600)
    }
  } catch (e) {
    /* sessionStorage 不可用：忽略 */
  }
  // shell 侧 app.bindProvider 兼容入口（其他遗留调用点仍可打开同一弹层）
  G.window.__grapeOnboardingOpenBind = function (provider) {
    openBind(provider)
  }
})
</script>

<style scoped>
.lv-head-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
  justify-content: flex-end;
}
.lv-lang {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 3px;
  border-radius: 8px;
  border: 1px solid var(--b);
  background: var(--c);
}
.lv-lang .lang-btn {
  padding: 3px 12px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 500;
  border: none;
  cursor: pointer;
  white-space: nowrap;
  background: none;
  color: var(--t3);
}
.lv-lang .lang-btn.active {
  background: var(--a);
  color: #fff;
}
.lv-drop {
  position: relative;
}
.lv-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  border-radius: 8px;
  border: 1px solid var(--b);
  background: var(--c);
  color: var(--t1);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
}
.lv-emoji {
  font-size: 14px;
}
.lv-menu {
  position: absolute;
  top: 100%;
  right: 0;
  margin-top: 4px;
  min-width: 260px;
  max-height: 480px;
  overflow-y: auto;
  background: var(--c);
  border: 1px solid var(--b);
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
  z-index: 9999;
  padding: 4px;
}
.ob-bound-check {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: linear-gradient(135deg, #34c759, #30d158);
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 2px 8px rgba(52, 199, 89, 0.4);
}
</style>