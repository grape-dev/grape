<template>
  <div class="lbox">
    <span class="logo">G</span>
    <h1>Grape Admin</h1>
    <p class="sub">运营管理台</p>
    <p v-if="error" class="err" style="color:#fca5a5;margin-bottom:8px">{{ error }}</p>
    <div class="inp">
      <input v-model="username" placeholder="用户名" @keyup.enter="login" />
      <input v-model="password" type="password" placeholder="密码" @keyup.enter="login" />
      <button class="obtn" :disabled="busy" @click="login">{{ busy ? '登录中...' : '登 录' }}</button>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import http from '../api'
import { setTokens } from '../utils/auth'

const router = useRouter()
const username = ref('')
const password = ref('')
const error = ref('')
const busy = ref(false)

async function login() {
  if (!username.value || !password.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const { data } = await http.post('/auth/login', { username: username.value, password: password.value })
    setTokens(data.token, data.refreshToken)
    router.push('/runtime/devices')
  } catch (e) {
    error.value = e.response?.data?.message || '登录失败，请检查账号密码'
  } finally {
    busy.value = false
  }
}
</script>
