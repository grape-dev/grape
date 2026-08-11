import { createRouter, createWebHistory } from 'vue-router'
import { getToken } from '../utils/auth'

const routes = [
  { path: '/login', name: 'login', component: () => import('../views/Login.vue'), meta: { public: true } },
  { path: '/', redirect: '/runtime/devices' },
  { path: '/runtime/devices', name: 'runtime.devices', component: () => import('../views/DeviceManage.vue'), meta: { title: '设备管理' } },
  { path: '/runtime/engines', name: 'runtime.engines', component: () => import('../views/EngineManage.vue'), meta: { title: '引擎管理' } },
  { path: '/runtime/credentials', name: 'runtime.credentials', component: () => import('../views/CredentialManage.vue'), meta: { title: '凭据管理' } },
  { path: '/ai/assistant', name: 'ai.assistant', component: () => import('../views/AiAssistant.vue'), meta: { title: 'AI 助手' } },
  { path: '/ai/history', name: 'ai.history', component: () => import('../views/ChatHistory.vue'), meta: { title: 'AI 历史' } },
  { path: '/sys/users', name: 'sys.users', component: () => import('../views/UserManage.vue'), meta: { title: '用户管理' } },
  { path: '/sys/roles', name: 'sys.roles', component: () => import('../views/RolePermissionConfig.vue'), meta: { title: '角色权限配置' } }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

router.beforeEach((to) => {
  if (!to.meta.public && !getToken()) {
    return { path: '/login' }
  }
  if (to.path === '/login' && getToken()) {
    return { path: '/' }
  }
  return true
})

export default router
