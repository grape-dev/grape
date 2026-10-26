// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/runtimes-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static/js/assets/' + f + '?v=1'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import RuntimesView from '../RuntimesView.vue'

// Batch 11 (runtimes): legacy shell contract —
//   renderRuntimes(data?) : app.js nav('runtimes') 调 _loadRuntimesData()；无参调用=组件自加载
// Also bridges for dashboard.js (typeof-guarded globals it reads from old runtimes.js):
//   __sceneTagsCache / _sceneDisplayName / _loadSceneTagsCache — kept on window for
//   dashboard.js engine-modal scene display names until its own migration batch.
let _app = null
function mount(props) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) _app.unmount()
  _app = createApp(RuntimesView, props || {})
  _app.mount(el)
}
window.renderRuntimes = function () {
  mount()
}
window.renderRuntimesLegacy = window.renderRuntimes
// dashboard.js 桥接（老 runtimes.js 的全局，dashboard 用 typeof 守卫读取）
window.__sceneTagsCache = null
window._loadSceneTagsCache = function (cb) {
  if (window.__sceneTagsCache) {
    cb(window.__sceneTagsCache)
    return
  }
  window.API.get('/runtime/scene-tags')
    .then(function (d) {
      window.__sceneTagsCache = d.scenes || []
      cb(window.__sceneTagsCache)
    })
    .catch(function () {
      window.__sceneTagsCache = []
      cb(window.__sceneTagsCache)
    })
}
window._sceneDisplayName = function (name) {
  if (!window.__sceneTagsCache) return name
  var s = window.__sceneTagsCache.find(function (x) {
    return x.name === name
  })
  return s ? s.display_name : name
}
