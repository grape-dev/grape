<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>用户管理</h1>
      <span class="badge">{{ total }} 人</span>
    </div>
    <div style="padding:20px 28px">
      <div class="toolbar">
        <input v-model="keyword" placeholder="搜索用户名" @keyup.enter="load" />
        <button class="btn" @click="load">搜索</button>
        <button class="btn btn-primary" style="margin-left:auto" @click="openCreate">+ 新增用户</button>
      </div>
      <div class="card">
        <table class="vod-table">
          <thead>
            <tr><th style="width:60px">ID</th><th>用户名</th><th>昵称</th><th>角色</th><th style="width:90px">状态</th><th>创建人</th><th style="width:150px">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="u in users" :key="u.id">
              <td class="t3">{{ u.id }}</td>
              <td>{{ u.username }}</td>
              <td>{{ u.nickname }}</td>
              <td>{{ (u.roleNames || []).join('、') }}</td>
              <td><span :class="['status-tag', u.status === 1 ? 'success' : 'default']">{{ u.status === 1 ? '启用' : '禁用' }}</span></td>
              <td class="t3">{{ u.createdBy }}</td>
              <td>
                <button class="btn btn-sm" @click="toggleStatus(u)">{{ u.status === 1 ? '禁用' : '启用' }}</button>
                <button class="btn btn-sm" @click="openRoles(u)">角色</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="pagination-bar">
          <span class="page-info">第 <strong>{{ page + 1 }}</strong> / {{ totalPages }} 页</span>
          <button class="page-btn" :disabled="page <= 0" @click="page--; load()">上一页</button>
          <button class="page-btn" :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
        </div>
      </div>
    </div>

    <div v-if="showCreate" class="modal" @click.self="showCreate = false">
      <div class="modal-box">
        <h4>新增用户</h4>
        <p v-if="error" class="err">{{ error }}</p>
        <input v-model="form.username" placeholder="用户名（≥3字符）" />
        <input v-model="form.password" type="password" placeholder="密码（≥8字符）" />
        <input v-model="form.nickname" placeholder="昵称" />
        <div class="modal-actions">
          <button class="btn" @click="showCreate = false">取消</button>
          <button class="btn btn-primary" @click="createUser">保存</button>
        </div>
      </div>
    </div>

    <div v-if="showRoles" class="modal" @click.self="showRoles = false">
      <div class="modal-box">
        <h4>分配角色：{{ current.username }}</h4>
        <label v-for="r in roles" :key="r.id" class="chk">
          <input type="checkbox" :value="r.id" v-model="form.roleIds" /> {{ r.name }}（{{ r.code }}）
        </label>
        <div class="modal-actions">
          <button class="btn" @click="showRoles = false">取消</button>
          <button class="btn btn-primary" @click="saveRoles">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import http from '../api'

const users = ref([])
const roles = ref([])
const keyword = ref('')
const page = ref(0)
const totalPages = ref(0)
const total = ref(0)
const showCreate = ref(false)
const showRoles = ref(false)
const current = ref({})
const form = ref({ username: '', password: '', nickname: '', roleIds: [] })
const error = ref('')

async function load() {
  const { data } = await http.get('/users', { params: { keyword: keyword.value, page: page.value, size: 20 } })
  users.value = data.content
  totalPages.value = data.totalPages
  total.value = data.totalElements
}
async function loadRoles() {
  const { data } = await http.get('/roles')
  roles.value = data
}
function openCreate() {
  form.value = { username: '', password: '', nickname: '', roleIds: [] }
  error.value = ''
  showCreate.value = true
}
async function createUser() {
  try {
    await http.post('/users', {
      username: form.value.username, password: form.value.password, nickname: form.value.nickname
    })
    showCreate.value = false
    load()
  } catch (e) {
    error.value = e.response?.data?.message || '创建失败'
  }
}
async function toggleStatus(u) {
  const next = u.status === 1 ? 0 : 1
  try {
    await http.put(`/users/${u.id}/status`, { status: next })
    load()
  } catch (e) {
    alert(e.response?.data?.message || '操作失败')
  }
}
function openRoles(u) {
  current.value = u
  form.value.roleIds = [...(u.roleIds || [])]
  showRoles.value = true
}
async function saveRoles() {
  try {
    await http.put(`/users/${current.value.id}/roles`, { roleIds: form.value.roleIds })
    showRoles.value = false
    load()
  } catch (e) {
    alert(e.response?.data?.message || '保存失败')
  }
}
onMounted(() => { load(); loadRoles() })
</script>
