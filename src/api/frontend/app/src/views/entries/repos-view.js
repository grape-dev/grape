import { createApp } from 'vue'
import ReposView from '../ReposView.vue'
import SettingsView from '../SettingsView.vue'

// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/repos-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static-vue/assets/' + f + '?v=57'
    document.head.appendChild(link)
  })
})()

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
  if (_app) _app.unmount()
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

// standalone copy of the SVG generator (kept in sync with ReposView) — app.js onboarding
// shell calls platformIcon before any lobby mount, so the fallback must exist.
window.platformIconFallback = function (provider, size) {
  size = size || 14
  const color = provider === 'github' ? '#1F2328' : '#F54A36'
  const s = 'vertical-align:-2px;flex-shrink:0'
  if (provider === 'github') {
    return (
      '<svg width="' +
      size +
      '" height="' +
      size +
      '" viewBox="0 0 1024 1024" fill="' +
      color +
      '" class="platform-icon" style="' +
      s +
      '"><path d="M512 42.666667A464.64 464.64 0 0 0 42.666667 502.186667 460.373333 460.373333 0 0 0 363.52 938.666667c23.466667 4.266667 32-9.813333 32-22.186667v-78.08c-130.56 27.733333-158.293333-61.44-158.293333-61.44a122.026667 122.026667 0 0 0-52.053334-67.413333c-42.666667-28.16 3.413333-27.733333 3.413334-27.733334a98.56 98.56 0 0 1 71.68 47.36 101.12 101.12 0 0 0 136.533333 37.973334 99.413333 99.413333 0 0 1 29.866667-61.44c-104.106667-11.52-213.333333-50.773333-213.333334-226.986667a177.066667 177.066667 0 0 1 47.36-124.16 161.28 161.28 0 0 1 4.693334-121.173333s39.68-12.373333 128 46.933333a455.68 455.68 0 0 1 234.666666 0c89.6-59.306667 128-46.933333 128-46.933333a161.28 161.28 0 0 1 4.693334 121.173333A177.066667 177.066667 0 0 1 810.666667 477.866667c0 176.64-110.08 215.466667-213.333334 226.986666a106.666667 106.666667 0 0 1 32 85.333334v125.866666c0 14.933333 8.533333 26.88 32 22.186667A460.8 460.8 0 0 0 981.333333 502.186667 464.64 464.64 0 0 0 512 42.666667"/></svg>'
    )
  }
  return (
    '<svg width="' +
    size +
    '" height="' +
    size +
    '" viewBox="0 0 1024 1024" fill="' +
    color +
    '" class="platform-icon" style="' +
    s +
    '"><path d="M472.746667 896.853333c-72.533333-8.106667-151.466667-39.253333-200.106667-78.08-27.306667-21.76-72.533333-71.253333-89.6-97.706666-51.626667-80.213333-72.533333-179.626667-57.173333-269.653334 15.36-86.613333 50.773333-154.453333 113.92-216.32 109.653333-107.52 285.44-141.653333 425.813333-82.346666 46.08 19.626667 90.453333 46.933333 108.8 66.986666 35.84 39.68 22.613333 104.106667-25.173333 128-20.48 9.813333-58.453333 10.24-72.96 0.853334-14.933333-9.386667-28.16-35.84-32-62.293334-1.706667-12.8-3.84-23.04-4.266667-23.04-0.853333 0-11.946667 5.973333-23.893333 13.226667-40.106667 23.466667-51.626667 26.88-113.066667 30.72-32.426667 1.706667-67.84 4.693333-78.933333 6.4-20.053333 2.56-44.373333-0.853333-83.2-13.226667l-20.48-6.4 0.853333 42.24c0.426667 40.96 0 43.093333-17.493333 79.36-22.613333 46.933333-30.72 75.52-34.56 117.333334-8.106667 100.693333 46.506667 175.36 150.613333 205.226666 67.413333 19.2 179.2 15.36 239.36-8.533333 58.026667-23.04 108.8-76.8 108.8-114.773333 0-15.786667-20.053333-35.84-41.813333-40.96-8.96-2.56-43.946667-5.546667-77.226667-6.826667-69.973333-2.986667-119.04-9.386667-132.266667-18.346667-13.653333-8.533333-20.48-33.28-14.506666-51.2 5.973333-19.2 15.786667-27.306667 43.946666-37.973333 19.2-7.253333 33.28-8.533333 88.746667-8.533333 118.613333 0 174.08 14.933333 217.6 58.453333 49.92 50.346667 55.466667 127.146667 13.226667 194.986667-62.293333 99.84-135.68 156.586667-235.52 182.613333-33.706667 8.96-122.453333 14.933333-158.72 10.666667z"/></svg>'
  )
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
