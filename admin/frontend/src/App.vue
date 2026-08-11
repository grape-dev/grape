<template>
  <div v-if="isLoginPage" class="login-wrap">
    <router-view />
  </div>
  <div v-else class="layout">
    <aside class="sidebar">
      <div class="logo">Grape 运营管理台</div>
      <nav>
        <div v-for="group in menuTree" :key="group.code" class="menu-group">
          <div class="menu-group-title">{{ group.name }}</div>
          <router-link
            v-for="child in group.children"
            :key="child.code"
            :to="child.path"
            class="menu-item"
            active-class="active"
          >{{ child.name }}</router-link>
        </div>
      </nav>
    </aside>
    <main class="content">
      <header class="topbar">
        <span>{{ currentTitle }}</span>
        <span class="user">{{ username }} <a @click="logout" href="javascript:void(0)">退出</a></span>
      </header>
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { fetchMenus } from './api'
import { getToken, clearTokens } from './utils/auth'

const route = useRoute()
const router = useRouter()
const menuTree = ref([])
const username = ref('')

const isLoginPage = computed(() => route.path === '/login')
const currentTitle = computed(() => route.meta.title || '')

onMounted(async () => {
  if (isLoginPage.value) return
  username.value = localStorage.getItem('admin_username') || ''
  try {
    const { data } = await fetchMenus()
    menuTree.value = data
  } catch (e) {
    // 401 已由拦截器处理跳登录
  }
})

function logout() {
  clearTokens()
  router.push('/login')
}
</script>

<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: "PingFang SC", "Microsoft YaHei", sans-serif; background: #f5f6fa; }
.layout { display: flex; min-height: 100vh; }
.sidebar { width: 220px; background: #1f2d3d; color: #fff; padding: 16px 0; }
.logo { padding: 0 20px 16px; font-weight: 600; font-size: 15px; border-bottom: 1px solid rgba(255,255,255,.1); }
.menu-group-title { padding: 14px 20px 6px; font-size: 12px; color: #8a97a8; }
.menu-item { display: block; padding: 10px 20px; color: #c9d4e0; text-decoration: none; font-size: 14px; }
.menu-item:hover { background: rgba(255,255,255,.08); }
.menu-item.active { background: #409eff; color: #fff; }
.content { flex: 1; padding: 20px; }
.topbar { display: flex; justify-content: space-between; align-items: center; background: #fff; padding: 12px 20px; border-radius: 6px; margin-bottom: 16px; }
.user { color: #666; font-size: 13px; }
.user a { margin-left: 8px; color: #409eff; }
.login-wrap { display: flex; justify-content: center; align-items: center; min-height: 100vh; background: #1f2d3d; }
</style>
