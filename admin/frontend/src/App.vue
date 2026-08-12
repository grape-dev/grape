<template>
  <div v-if="isLoginPage" class="auth-wrapper">
    <router-view />
  </div>
  <div v-else class="app-layout">
    <!-- Top Navigation（上下结构，对齐 VOD） -->
    <header class="topnav">
      <button class="topnav-hamburger" :class="{ open: mobileNavOpen }" @click="mobileNavOpen = !mobileNavOpen">
        <span></span><span></span><span></span>
      </button>
      <div class="topnav-brand" @click="$router.push('/runtime/devices')">
        <span class="brand-icon">🍇</span>
        <span class="brand-text">Grape Admin</span>
        <span class="brand-ver">v{{ BUILD_TIME }}</span>
      </div>
      <nav class="topnav-links">
        <template v-for="item in navItems" :key="item.code">
          <!-- 顶级带 path（AI分析/总览）→ 直接跳转；无 path 有子菜单 → 下拉 -->
          <router-link v-if="item.route" class="topnav-link" :to="item.route">{{ item.icon }} {{ item.name }}</router-link>
          <div v-else-if="item.children && item.children.length" class="topnav-dropdown" :class="{ active: isDropdownActive(item) }">
            <span class="topnav-link topnav-dropdown-trigger">{{ item.icon }} {{ item.name }}<span class="dd-arrow"></span></span>
            <div class="topnav-dropdown-menu">
              <router-link
                v-for="child in item.children"
                :key="child.code"
                class="topnav-dropdown-item"
                :to="child.route"
              >{{ child.name }}</router-link>
            </div>
          </div>
          <router-link v-else class="topnav-link topnav-direct" :class="{ active: route.path === item.path }" :to="item.path">{{ item.icon }} {{ item.name }}</router-link>
        </template>
      </nav>
      <div class="topnav-user" @click="logout">
        <span class="user-icon">👤</span>
        <span class="user-name">{{ me }}</span>
        <span class="user-logout">退出</span>
      </div>
    </header>

    <!-- Mobile drawer -->
    <div class="drawer-overlay" :class="{ open: mobileNavOpen }" @click="mobileNavOpen = false"></div>
    <nav class="drawer" :class="{ open: mobileNavOpen }">
      <div class="drawer-header">
        <span class="drawer-title">导航菜单</span>
        <button class="drawer-close" @click="mobileNavOpen = false">✕</button>
      </div>
      <div class="drawer-body">
        <template v-for="item in navItems" :key="item.code">
          <router-link v-if="item.route" class="drawer-group-title" :to="item.route" @click="mobileNavOpen = false">
            <span class="drawer-item-icon">{{ item.icon }}</span>{{ item.name }}
          </router-link>
          <div v-else-if="item.children && item.children.length" class="drawer-group">
            <div class="drawer-group-title" @click.stop="toggleDrawerGroup(item.code)">
              <span><span class="drawer-item-icon">{{ item.icon }}</span>{{ item.name }}</span>
              <span class="drawer-group-arrow" :class="{ open: openDrawerGroups[item.code] }"></span>
            </div>
            <div class="drawer-sub" :class="{ open: openDrawerGroups[item.code] }">
              <router-link v-for="child in item.children" :key="child.code" class="drawer-sub-item" :to="child.route" @click="mobileNavOpen = false; openDrawerGroups[item.code] = false">{{ child.name }}</router-link>
            </div>
          </div>
          <router-link v-else class="drawer-sub-item" :to="item.path" @click="mobileNavOpen = false">{{ item.name }}</router-link>
        </template>
      </div>
    </nav>

    <!-- Page Content -->
    <main class="topnav-content">
      <router-view :key="$route.path" />
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import http from './api'
import { getToken, clearTokens } from './utils/auth'

const BUILD_TIME = __BUILD_TIME__

// 分组图标（菜单 API 无 icon 字段，静态映射）
const GROUP_ICONS = {
  overview: '📊',
  runtime: '🖥️',
  ai: '🤖',
  sys: '⚙️'
}

const route = useRoute()
const router = useRouter()
const menuTree = ref([])
const mobileNavOpen = ref(false)
const openDrawerGroups = ref({})
const me = ref(getToken() ? 'A' : '')
const isLoginPage = computed(() => route.path === '/login')

// 后端菜单树 → VOD 式导航结构（顶级菜单带 path 且无 children 时直接跳转）
const navItems = computed(() => menuTree.value.map(g => ({
  code: g.code,
  name: g.name,
  icon: GROUP_ICONS[g.code] || '📁',
  route: g.path || '',
  children: (g.children || []).map(c => ({ code: c.code, name: c.name, route: c.path }))
})))

function isDropdownActive(item) {
  if (!item.children) return false
  return item.children.some(c => route.path === c.route)
}

function toggleDrawerGroup(code) {
  openDrawerGroups.value = { ...openDrawerGroups.value, [code]: !openDrawerGroups.value[code] }
}

let menuLoading = false
async function loadMenus() {
  if (menuLoading) return
  menuLoading = true
  try {
    const { data } = await http.get('/menus')
    menuTree.value = data
    // 移动端默认展开当前分组
    const active = data.find(g => (g.children || []).some(c => route.path === c.path))
    if (active && !openDrawerGroups.value[active.code]) {
      openDrawerGroups.value = { ...openDrawerGroups.value, [active.code]: true }
    }
  } catch (e) {
    // 失败不裸抛：顶部导航留空，下次路由变化/刷新自动重试
    console.error('[menu] 加载菜单失败', e)
  } finally {
    menuLoading = false
  }
}
// 登录页与主页面共用本组件：从 /login 跳入时 onMounted 不会再次触发，
// 必须在路由切换时兜底补拉菜单（菜单为空才加载，避免重复请求；未登录不请求）
watch(() => route.path, (p) => {
  if (p !== '/login' && getToken() && menuTree.value.length === 0) loadMenus()
})

function logout() {
  clearTokens()
  router.push('/login')
}

onMounted(() => { if (!isLoginPage.value && getToken()) loadMenus() })
</script>
