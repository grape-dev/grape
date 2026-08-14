<template>
  <div class="auth-page">
    <div class="auth-card">
      <div class="auth-brand">
        <img class="auth-logo-img" :src="logo128" alt="Grape" />
        <h1>Grape运营管理系统</h1>
        <p class="auth-sub">运营管理台</p>
      </div>
      <div class="auth-form">
        <div class="form-item">
          <label>用户名</label>
          <input v-model="username" placeholder="请输入用户名" autocomplete="off" @keydown.enter="login" />
        </div>
        <div class="form-item">
          <label>密码</label>
          <input v-model="password" type="password" placeholder="请输入密码" autocomplete="new-password" @keydown.enter="login" />
        </div>
        <div class="auth-error" v-if="error">{{ error }}</div>
        <button class="auth-btn" @click="login" :disabled="busy">{{ busy ? '登录中...' : '登 录' }}</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import http from '../api'
import { setTokens, setUser } from '../utils/auth'
import logo128 from '../assets/logo-128.png'

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
    setUser({ username: data.username, nickname: data.nickname, roles: data.roles || [] })
    router.push('/')
  } catch (e) {
    error.value = e.response?.data?.message || '登录失败，请检查账号密码'
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.auth-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f1f3f5;
}
.auth-card {
  width: 380px;
  background: var(--bg-white);
  border-radius: var(--radius-lg);
  border: 1px solid var(--border-color);
  box-shadow: 0 8px 32px rgba(30,60,140,0.10);
  padding: 36px 32px 32px;
}
.auth-brand { text-align: center; margin-bottom: 28px; }
.auth-logo-img {
  width: 96px; height: 96px; border-radius: 24px;
  object-fit: cover; margin-bottom: 24px;
}
.auth-brand h1 { font-size: 20px; font-weight: 700; color: var(--text-primary); }
.auth-sub { font-size: 13px; color: var(--text-tertiary); margin-top: 4px; }
.form-item { margin-bottom: 16px; }
.form-item label { display: block; font-size: 13px; color: var(--text-secondary); margin-bottom: 6px; }
.form-item input {
  width: 100%; padding: 10px 12px;
  border: 1px solid var(--border-color); border-radius: var(--radius);
  background: #fff;
  font-size: 14px; outline: none; color: var(--text-primary);
  transition: border-color 0.2s, box-shadow 0.2s;
}
.form-item input:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(0,110,255,0.12); }
.auth-error {
  font-size: 13px; color: var(--danger);
  background: #fff1f0; border: 1px solid #ffa39e;
  border-radius: 6px; padding: 8px 10px; margin-bottom: 12px;
}
.auth-btn {
  width: 100%; padding: 11px;
  border: none; border-radius: var(--radius);
  background: var(--primary); color: #fff;
  font-size: 14px; font-weight: 600; cursor: pointer;
  transition: all 0.2s;
}
.auth-btn:hover { background: var(--primary-hover); box-shadow: 0 4px 16px rgba(0,110,255,0.25); }
.auth-btn:disabled { opacity: 0.6; cursor: not-allowed; }
</style>
