// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/scene-market.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static-vue/assets/' + f + '?v=45'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import SceneMarket from '../SceneMarket.vue'

// Batch 4 (scene-market): legacy shell contract —
//   renderSceneMarket()        : app.js nav('scene-market')
//   renderSceneDetail(marketId): app.js nav('scene-detail', param) AND skills.js
//                                back-navigation (called without nav, must mount fresh)
// Mounted fresh on every call, so list state resets like the old module did.
let _app = null
function mount(props) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) _app.unmount()
  _app = createApp(SceneMarket, props)
  _app.mount(el)
}
window.renderSceneMarket = function () {
  mount()
}
window.renderSceneDetail = function (marketId) {
  mount({ initialDetailId: String(marketId) })
}
