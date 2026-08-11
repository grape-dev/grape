<template>
  <div class="login-box">
    <h2>Grape 运营管理台</h2>
    <p v-if="error" class="error">{{ error }}</p>
    <input v-model="username" placeholder="用户名" />
    <input v-model="password" type="password" placeholder="密码" @keyup.enter="submit" />
    <button :disabled="loading" @click="submit">{{ loading ? '登录中...' : '登录' }}</button>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { login } from '../api'
import { setTokens } from '../utils/auth'

const router = useRouter()
const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

async function submit() {
  error.value = ''
  loading.value = true
  try {
    const { data } = await login(username.value, password.value)
    setTokens(data.token, data.refreshToken)
    localStorage.setItem('admin_username', data.username)
    router.push('/')
  } catch (e) {
    error.value = e.response?.data?.message || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-box { width: 340px; background: #fff; border-radius: 8px; padding: 32px; }
h2 { text-align: center; margin-bottom: 20px; font-size: 18px; }
input { width: 100%; padding: 10px 12px; margin-bottom: 12px; border: 1px solid #dcdfe6; border-radius: 4px; }
button { width: 100%; padding: 10px; background: #409eff; color: #fff; border: none; border-radius: 4px; cursor: pointer; }
.error { color: #f56c6c; font-size: 13px; margin-bottom: 10px; }
</style>
