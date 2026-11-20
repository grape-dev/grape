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
  if (_app) { try { _app.unmount() } catch (e) { /* 未挂载/已卸载则忽略 */ } _app = null }
  el.innerHTML = ''
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
