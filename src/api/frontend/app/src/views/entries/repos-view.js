import { createApp } from 'vue'
import ReposView from '../ReposView.vue'
import SettingsView from '../SettingsView.vue'


// Batch 14 (repos+templates+settings): legacy shell contract —
//   renderRepoSelection(repos): app.js goToRepos/loadRepos/renderReposFromData/switchOrg path
//   renderRepos():              repo management view (app.js _rerenderReposView cached path)
//   renderSettings(tab):        app.js nav('settings') + i18n lang-switch rerender
//   switchSidebarTab(tab):      tab switch inside lobby (legacy global onclick)
//   selectRepo(fn/owner/name/k), platformIcon(provider,size), openCreateRepoModal():
//       cross-module globals called from app.js / scene-market / templates create flows
// platformIcon is referenced as a BARE identifier by app.js (_renderOnboardingShell) —
// a module-scope `window.x = y` binding makes the bare global visible (batch 6 lesson).
let _app = null
let _root = null // mount() return value = root component public instance (has defineExpose'd methods)
function mount(props) {
  const el = document.getElementById('pageContent')
  if (!el) return null
  if (_app) { try { _app.unmount() } catch (e) { /* 未挂载/已卸载则忽略 */ } _app = null }
  el.innerHTML = ''
  _app = createApp(ReposView, props || {})
  _root = _app.mount(el)
  return _app
}
let _lastVm = null

function mountAndGetVm(props) {
  mount(props)
  // createApp().mount() returns the root component public instance; defineExpose'd
  // methods are readable on it directly (app._instance is undefined in prod builds).
  _lastVm = _root || null
  return _app
}

window.renderRepoSelection = function (repos) {
  mountAndGetVm({ initialRepos: repos || [] })
}

window.renderRepos = function () {
  // management view: mounted without initialRepos → component loads /repos/member itself
  mountAndGetVm({})
}

window.renderSettings = function (tab) {
  // settings view is a separate small view; import lazily to keep this chunk lean
  // (it shares the chunk via static import to avoid a second entry)
  mountSettings(tab)
}

window.switchSidebarTab = function (tab) {
  if (tab === 'templates' && _lastVm && typeof _lastVm.showTemplatesTab === 'function') {
    _lastVm.showTemplatesTab()
    return
  }
  // workspace/projects (or no live vm): fresh mount on the requested tab
  mountAndGetVm({ initialTabName: tab })
}

window.selectRepo = function (fullName, owner, name, repoKey) {
  sessionStorage.setItem(
    'currentRepo',
    JSON.stringify({ fullName, owner, name, repoKey: repoKey || '' })
  )
  // 路径跟随仓库：进入项目后地址栏必须是 /app/<owner>/<repo>/，否则深链分享出去指错仓
  if (window.app && typeof window.app.syncAppPath === 'function') {
    window.app.syncAppPath(owner, name)
  }
  const sidebar = document.getElementById('sidebar')
  const mainView = document.getElementById('mainView')
  const tb = document.getElementById('tb')
  const title = document.getElementById('pageTitle')
  if (mainView) {
    mainView.style.position = ''
    mainView.style.inset = ''
    mainView.style.zIndex = ''
    mainView.style.display = 'flex'
  }
  if (sidebar) sidebar.style.display = 'flex'
  if (tb) tb.style.display = 'flex'
  if (title) title.textContent = globalThis.I18N.t('app.nav.dashboard')
  globalThis.app.initSidebarRepos()
  globalThis.app.nav('dash')
}

window.platformIcon = function (provider, size) {
  // delegate to the mounted component's identical SVG generator; fall back to a
  // minimal inline copy for calls before mount (app.js onboarding shell)
  if (_lastVm && typeof _lastVm.platformIcon === 'function')
    return _lastVm.platformIcon(provider, size)
  return window.platformIconFallback(provider, size)
}

// 图标单一真源：shell/js/platform-icons.js（classic script，先于 app.js 执行）
// 早先这里复制过一份"只认 github/gitcode"的生成器，导致 gitee/gitlab/华为云 全回落成
// gitcode 的图形——2026-11-14 收口到 window.platformIcon，不再本地复制。
window.platformIconFallback = function (provider, size) {
  return window.platformIcon(provider, size)
}

window.openCreateRepoModal = function () {
  if (_lastVm && typeof _lastVm.openCreateModal === 'function') _lastVm.openCreateModal()
}

// hostAndSelect / unhostRepo are called via @click from inside the component itself,
// but app.js/i18n may re-mount; expose them as globals too for parity with legacy.
// (renderSettings mounts the SettingsView into the same #pageContent.)

// ---- settings view (same chunk; simple mount) ----
let _sapp = null
function mountSettings(tab) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_sapp) _sapp.unmount()
  if (_app) {
    _app.unmount()
    _app = null
    _lastVm = null
  }
  _sapp = createApp(SettingsView, { initialTab: tab })
  _sapp.mount(el)
}
