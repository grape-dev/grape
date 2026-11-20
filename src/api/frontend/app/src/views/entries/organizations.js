import { createApp } from 'vue'
import Organizations from '../Organizations.vue'

// Batch 2c (organizations): legacy shell contract — global renderOrganizations() mounts into #pageContent.
// app.js also calls app.nav('organizations') which invokes this render; detail view is internal (no separate route).
window.renderOrganizations = function (orgId, createOnly) {
  const el = document.getElementById('pageContent')
  if (!el) return
  createApp(Organizations, {
    initialDetailId: orgId ? String(orgId) : null,
    initialCreate: !!createOnly,
  }).mount(el)
}

// 全局「创建组织」悬浮弹窗：任意页面点击「＋ 创建组织」直接在当前页面弹出，
// 不切到 #organizations/__create__，关闭后原地留在当前页面。
let _createApp = null
let _createHost = null

function _closeCreateModal() {
  const app = _createApp
  const host = _createHost
  _createApp = null
  _createHost = null
  if (app) {
    try {
      app.unmount()
    } catch (e) {
      /* ignore */
    }
  }
  if (host && host.parentNode) host.parentNode.removeChild(host)
}

window.openCreateOrgModal = function () {
  if (_createApp) return
  const host = document.createElement('div')
  host.id = 'globalCreateOrgHost'
  document.body.appendChild(host)
  _createHost = host
  _createApp = createApp(Organizations, {
    modalOnly: true,
    initialCreate: true,
    onClose: _closeCreateModal,
    onCreated: function (org) {
      const newId = org && (org.id || org.org_id)
      _closeCreateModal()
      if (newId && window.app && typeof window.app.switchOrg === 'function') {
        window.app.switchOrg(String(newId))
      }
    },
  })
  _createApp.mount(host)
}
