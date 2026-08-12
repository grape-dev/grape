<template>
  <div v-if="isLoginPage" class="ln">
    <router-view />
  </div>
  <div v-else class="layout">
    <aside class="sb">
      <div class="sb-h">
        <div class="logo-s">
          <span class="sq">G</span>
          <h1>Grape Admin</h1>
        </div>
        <div class="tagline">Ops Console</div>
      </div>
      <nav class="sb-nav">
        <template v-for="group in menuTree" :key="group.code">
          <div class="grp" @click="toggleGroup(group.code)">
            <span class="chev">{{ expanded[group.code] ? '▾' : '▸' }}</span>{{ group.name }}
          </div>
          <router-link
            v-for="child in group.children"
            :key="child.code"
            :to="child.path"
            :class="{ act: route.name === child.code }"
            v-show="expanded[group.code]"
          >
            <span class="ic">●</span>{{ child.name }}
          </router-link>
        </template>
      </nav>
      <div class="sb-ft" @click="logout">
        <span class="av">{{ me.charAt(0).toUpperCase() }}</span>
        <span class="by">{{ me }}<span>退出登录</span></span>
        <span class="t3" style="font-size:10px;margin-left:6px" @click.stop>v{{ BUILD_TIME }}</span>
      </div>
    </aside>
    <main class="mn">
      <div class="tb">
        <span class="bc">运营管理台 <b>/ {{ route.meta.title || '' }}</b></span>
        <span class="sp"></span>
      </div>
      <div class="ct">
        <router-view />
      </div>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import http from './api'
import { getToken, clearTokens } from './utils/auth'

const BUILD_TIME = __BUILD_TIME__

const route = useRoute()
const router = useRouter()
const menuTree = ref([])
const expanded = ref({})
const me = ref(getToken() ? 'A' : '')
const isLoginPage = computed(() => route.path === '/login')

function toggleGroup(code) {
  expanded.value = { ...expanded.value, [code]: !expanded.value[code] }
}
// 默认展开当前路由所属分组
function expandActiveGroup() {
  const active = menuTree.value.find(g => g.children.some(c => route.name === c.code))
  if (active) expanded.value = { ...expanded.value, [active.code]: true }
}
watch(() => route.name, () => expandActiveGroup())

let menuLoading = false
async function loadMenus() {
  if (menuLoading) return
  menuLoading = true
  try {
    const { data } = await http.get('/menus')
    menuTree.value = data
    menuTree.value.forEach(g => { if (!(g.code in expanded.value)) expanded.value = { ...expanded.value, [g.code]: true } })
    expandActiveGroup()
  } catch (e) {
    // 失败不裸抛：侧边栏留空，下次路由变化/刷新自动重试
    console.error('[menu] 加载菜单失败', e)
  } finally {
    menuLoading = false
  }
}
// 登录页与主页面共用本组件：从 /login 跳入时 onMounted 不会再次触发，
// 必须在路由切换时兜底补拉菜单（菜单为空才加载，避免重复请求）
watch(() => route.path, (p) => {
  if (p !== '/login' && menuTree.value.length === 0) loadMenus()
})
function logout() {
  clearTokens()
  router.push('/login')
}
onMounted(() => { if (!isLoginPage.value) loadMenus() })
</script>
