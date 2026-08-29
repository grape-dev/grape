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
  background: linear-gradient(135deg, #f5f5f7 0%, #e8e8ed 100%);
}
.auth-card {
  width: 380px;
  background: rgba(255,255,255,0.9);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border-radius: 20px;
  border: 1px solid rgba(0,0,0,0.04);
  box-shadow: 0 20px 60px rgba(0,0,0,0.1);
  padding: 40px 36px 36px;
}
.auth-brand { text-align: center; margin-bottom: 28px; }
.auth-logo-img {
  width: 96px; height: 96px; border-radius: 24px;
  object-fit: cover; margin-bottom: 24px;
}
.auth-brand h1 { font-size: 22px; font-weight: 700; color: var(--text-primary); }
.auth-sub { font-size: 13px; color: var(--text-tertiary); margin-top: 4px; }
.form-item { margin-bottom: 18px; }
.form-item label { display: block; font-size: 13px; color: var(--text-secondary); margin-bottom: 6px; font-weight: 500; }
.form-item input {
  width: 100%; padding: 11px 14px;
  border: 1px solid var(--border-color); border-radius: 10px;
  background: #fff;
  font-size: 14px; outline: none; color: var(--text-primary);
  transition: all var(--transition);
}
.form-item input:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(0,107,255,0.1); }
.auth-error {
  font-size: 13px; color: var(--danger);
  background: rgba(255,59,48,0.08); border: none;
  border-radius: 10px; padding: 10px 12px; margin-bottom: 14px;
}
.auth-btn {
  width: 100%; padding: 12px;
  border: none; border-radius: 10px;
  background: var(--primary); color: #fff;
  font-size: 15px; font-weight: 600; cursor: pointer;
  transition: all var(--transition);
  box-shadow: 0 2px 8px rgba(0,107,255,0.2);
}
.auth-btn:hover { background: var(--primary-hover); transform: translateY(-1px); box-shadow: 0 4px 16px rgba(0,107,255,0.3); }
.auth-btn:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
</style>
