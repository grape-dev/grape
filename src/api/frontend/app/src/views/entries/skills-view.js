// inject view css + bridge tokens + shared AG css (legacy shell has no new-engine tokens)
;(function () {
  ;['bridge.css', 'ag-ui.css', 'viewcss/skills-view.css'].forEach(function (f) {
    var link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = '/static/js/assets/' + f + '?v=11'
    document.head.appendChild(link)
  })
})()
import { createApp } from 'vue'
import SkillsView from '../SkillsView.vue'

// Batch 5 (skills): legacy shell contract —
//   renderSkills()      : app.js nav('skills') — 技能 tab 页（一级Tab栏+市场列表+Agent筛选+标签筛选）
//   renderSkillDetail(d): app.js nav('skill'/'skill-sub', param) 加载后注入数据
// Mounted fresh on every call, module-level state resets like the old module did.
let _app = null
function mount(props) {
  const el = document.getElementById('pageContent')
  if (!el) return
  if (_app) _app.unmount()
  _app = createApp(SkillsView, props || {})
  _app.mount(el)
}
window.renderSkills = function () {
  mount()
}
window.renderSkillDetail = function (d) {
  // 组件自身处理 huawei-cloud 分包路由
  mount({ initialDetail: d })
}
