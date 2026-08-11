<template>
  <div class="page">
    <div class="toolbar">
      <input v-model="keyword" placeholder="搜索用户名" @keyup.enter="load" />
      <button @click="load">搜索</button>
      <button class="primary" @click="openCreate">新增用户</button>
    </div>
    <table class="tbl">
      <thead>
        <tr><th>ID</th><th>用户名</th><th>昵称</th><th>角色</th><th>状态</th><th>创建人</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="u in users" :key="u.id">
          <td>{{ u.id }}</td>
          <td>{{ u.username }}</td>
          <td>{{ u.nickname }}</td>
          <td>{{ (u.roleNames || []).join('、') }}</td>
          <td><span :class="['badge', u.status === 1 ? 'on' : 'off']">{{ u.status === 1 ? '启用' : '禁用' }}</span></td>
          <td>{{ u.createdBy }}</td>
          <td>
            <a @click="toggleStatus(u)">{{ u.status === 1 ? '禁用' : '启用' }}</a>
            <a @click="openRoles(u)">角色</a>
          </td>
        </tr>
      </tbody>
    </table>
    <div class="pager">
      第 {{ page + 1 }} / {{ totalPages }} 页
      <button :disabled="page <= 0" @click="page--; load()">上一页</button>
      <button :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
    </div>

    <div v-if="showCreate" class="modal">
      <div class="modal-box">
        <h4>新增用户</h4>
        <p v-if="error" class="err">{{ error }}</p>
        <input v-model="form.username" placeholder="用户名（≥3字符）" />
        <input v-model="form.password" type="password" placeholder="密码（≥8字符）" />
        <input v-model="form.nickname" placeholder="昵称" />
        <div class="modal-actions">
          <button @click="showCreate = false">取消</button>
          <button class="primary" @click="createUser">保存</button>
        </div>
      </div>
    </div>

    <div v-if="showRoles" class="modal">
      <div class="modal-box">
        <h4>分配角色：{{ current.username }}</h4>
        <label v-for="r in roles" :key="r.id" class="chk">
          <input type="checkbox" :value="r.id" v-model="form.roleIds" /> {{ r.name }}（{{ r.code }}）
        </label>
        <div class="modal-actions">
          <button @click="showRoles = false">取消</button>
          <button class="primary" @click="saveRoles">保存</button>
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
const showCreate = ref(false)
const showRoles = ref(false)
const current = ref({})
const form = ref({ username: '', password: '', nickname: '', roleIds: [] })
const error = ref('')

async function load() {
  const { data } = await http.get('/users', { params: { keyword: keyword.value, page: page.value, size: 20 } })
  users.value = data.content
  totalPages.value = data.totalPages
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
    const { data } = await http.post('/users', {
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

<style scoped>
.toolbar { margin-bottom: 12px; display: flex; gap: 8px; }
.toolbar input { padding: 6px 10px; border: 1px solid #dcdfe6; border-radius: 4px; width: 200px; }
button { padding: 6px 14px; border: 1px solid #dcdfe6; background: #fff; border-radius: 4px; cursor: pointer; }
button.primary { background: #409eff; color: #fff; border-color: #409eff; }
.tbl { width: 100%; border-collapse: collapse; background: #fff; border-radius: 6px; overflow: hidden; }
.tbl th, .tbl td { padding: 10px 12px; border-bottom: 1px solid #f0f2f5; text-align: left; font-size: 14px; }
.tbl th { background: #fafbfc; color: #666; }
.tbl a { color: #409eff; cursor: pointer; margin-right: 8px; }
.badge { padding: 2px 8px; border-radius: 10px; font-size: 12px; }
.badge.on { background: #e1f3d8; color: #67c23a; }
.badge.off { background: #fde2e2; color: #f56c6c; }
.pager { margin-top: 12px; font-size: 13px; color: #666; display: flex; gap: 8px; align-items: center; }
.modal { position: fixed; inset: 0; background: rgba(0,0,0,.4); display: flex; align-items: center; justify-content: center; }
.modal-box { background: #fff; border-radius: 8px; padding: 20px; width: 380px; }
.modal-box h4 { margin-bottom: 14px; }
.modal-box input { width: 100%; padding: 8px 10px; margin-bottom: 10px; border: 1px solid #dcdfe6; border-radius: 4px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
.chk { display: block; padding: 4px 0; }
.err { color: #f56c6c; font-size: 13px; margin-bottom: 8px; }
</style>
