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
  if (_app) { try { _app.unmount() } catch (e) {} _app = null }
  el.innerHTML = ''
  _app = createApp(SceneMarket, props)
  _app.mount(el)
}
window.renderSceneMarket = function () {
  mount()
}
window.renderSceneDetail = function (marketId) {
  mount({ initialDetailId: String(marketId) })
}
