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
          <div class="grp">{{ group.name }}</div>
          <router-link
            v-for="child in group.children"
            :key="child.code"
            :to="child.path"
            :class="{ act: route.name === child.code }"
          >
            <span class="ic">●</span>{{ child.name }}
          </router-link>
        </template>
      </nav>
      <div class="sb-ft" @click="logout">
        <span class="av">{{ me.charAt(0).toUpperCase() }}</span>
        <span class="by">{{ me }}<span>退出登录</span></span>
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
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import http from './api'
import { getToken, clearTokens } from './utils/auth'

const route = useRoute()
const router = useRouter()
const menuTree = ref([])
const me = ref(getToken() ? 'A' : '')
const isLoginPage = computed(() => route.path === '/login')

async function loadMenus() {
  const { data } = await http.get('/menus')
  menuTree.value = data
}
function logout() {
  clearTokens()
  router.push('/login')
}
onMounted(() => { if (!isLoginPage.value) loadMenus() })
</script>
