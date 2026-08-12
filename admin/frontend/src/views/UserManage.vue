<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>用户管理</h1>
      <span class="badge">{{ total }} 人</span>
    </div>
    <div style="padding:20px 28px">
      <!-- 角色分布徽章（对齐 VOD um-role-summary） -->
      <div class="um-role-summary" v-if="roleStats.length">
        <span class="um-role-badge" v-for="rs in roleStats" :key="rs.roleId">
          {{ rs.name }} <strong>{{ rs.count }}</strong>
        </span>
      </div>

      <div class="toolbar">
        <input v-model="keyword" placeholder="搜索用户名" @keyup.enter="load" />
        <button class="btn" @click="load">搜索</button>
        <button class="btn" @click="reset">重置</button>
        <button class="btn btn-primary" style="margin-left:auto" @click="openCreate">+ 新增用户</button>
      </div>

      <div class="card">
        <table class="vod-table">
          <thead>
            <tr><th style="width:60px">ID</th><th>用户名</th><th>昵称</th><th>角色</th><th style="width:90px">状态</th><th style="width:220px">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="u in users" :key="u.id">
              <td class="t3">{{ u.id }}</td>
              <td>{{ u.username }}</td>
              <td>{{ u.nickname }}</td>
              <td>{{ (u.roleNames || []).join('、') }}</td>
              <td><span :class="['status-tag', u.status === 1 ? 'success' : 'default']">{{ u.status === 1 ? '启用' : '禁用' }}</span></td>
              <td class="um-ops">
                <button class="btn btn-sm" @click="openEdit(u)">编辑</button>
                <button class="btn btn-sm" @click="resetPwd(u)">重置密码</button>
                <button class="btn btn-sm" @click="openRoles(u)">角色</button>
                <button class="btn btn-sm" @click="toggleStatus(u)">{{ u.status === 1 ? '禁用' : '启用' }}</button>
              </td>
            </tr>
            <tr v-if="!users.length">
              <td colspan="6" class="t2" style="padding:20px;text-align:center">暂无用户</td>
            </tr>
          </tbody>
        </table>
        <!-- 分页（对齐 VOD um-pagination） -->
        <div class="um-pagination" v-if="total > 0">
          <div class="um-pagination-info">
            共 {{ total }} 条
            <select v-model="size" class="page-size-select" @change="page = 0; load()">
              <option :value="10">10条/页</option>
              <option :value="20">20条/页</option>
              <option :value="50">50条/页</option>
            </select>
          </div>
          <div class="um-pagination-controls">
            <button :disabled="page <= 0" @click="page--; load()">上一页</button>
            <span class="um-page-current">{{ page + 1 }} / {{ totalPages }}</span>
            <button :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 新增 / 编辑单弹窗（对齐 VOD） -->
    <div v-if="showModal" class="modal" @click.self="showModal = false">
      <div class="modal-box" style="max-width:440px">
        <h4>{{ isEditing ? '编辑用户' : '新增用户' }}</h4>
        <div class="form-row">
          <label>用户名</label>
          <input v-model="form.username" :disabled="isEditing" placeholder="请输入用户名" />
        </div>
        <div class="form-row" v-if="!isEditing">
          <label>密码</label>
          <input v-model="form.password" type="password" placeholder="请输入密码（至少8位）" />
        </div>
        <div class="form-row">
          <label>昵称</label>
          <input v-model="form.nickname" placeholder="昵称（可选）" />
        </div>
        <div class="form-row" style="align-items:flex-start">
          <label>角色</label>
          <div style="flex:1">
            <label v-for="r in roles" :key="r.id" class="chk">
              <input type="checkbox" :value="r.id" v-model="form.roleIds" /> {{ r.name }}（{{ r.code }}）
            </label>
          </div>
        </div>
        <p v-if="modalError" class="err">{{ modalError }}</p>
        <div class="modal-actions">
          <button class="btn" @click="showModal = false">取消</button>
          <button class="btn btn-primary" @click="submitForm" :disabled="submitting">{{ submitting ? '提交中...' : '确定' }}</button>
        </div>
      </div>
    </div>

    <!-- 分配角色弹窗（保留，操作列「角色」入口） -->
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
const roleStats = ref([])
const keyword = ref('')
const page = ref(0)
const size = ref(10)
const totalPages = ref(0)
const total = ref(0)
const showModal = ref(false)
const isEditing = ref(false)
const editingId = ref(null)
const form = ref({ username: '', password: '', nickname: '', roleIds: [] })
const modalError = ref('')
const submitting = ref(false)
const showRoles = ref(false)
const current = ref({})

async function load() {
  const { data } = await http.get('/users', { params: { keyword: keyword.value, page: page.value, size: size.value } })
  users.value = data.content
  totalPages.value = data.totalPages
  total.value = data.totalElements
}
async function loadRoles() {
  const { data } = await http.get('/roles')
  roles.value = data
}
async function loadRoleStats() {
  try {
    const { data } = await http.get('/users/role-stats')
    roleStats.value = data
  } catch (e) { /* 忽略 */ }
}
function reset() { keyword.value = ''; page.value = 0; load() }

function openCreate() {
  isEditing.value = false
  editingId.value = null
  form.value = { username: '', password: '', nickname: '', roleIds: [] }
  modalError.value = ''
  showModal.value = true
}
function openEdit(u) {
  isEditing.value = true
  editingId.value = u.id
  form.value = {
    username: u.username,
    password: '',
    nickname: u.nickname || '',
    roleIds: [...(u.roleIds || [])]
  }
  modalError.value = ''
  showModal.value = true
}
async function submitForm() {
  modalError.value = ''
  if (!form.value.username) { modalError.value = '请输入用户名'; return }
  if (!isEditing.value && (!form.value.password || form.value.password.length < 8)) {
    modalError.value = '密码至少 8 位'; return
  }
  if (!form.value.roleIds.length) { modalError.value = '请至少选择一个角色'; return }
  submitting.value = true
  try {
    let userId = editingId.value
    if (isEditing.value) {
      await http.put(`/users/${userId}`, { nickname: form.value.nickname })
    } else {
      const { data } = await http.post('/users', {
        username: form.value.username, password: form.value.password, nickname: form.value.nickname
      })
      userId = data.id
    }
    await http.put(`/users/${userId}/roles`, { roleIds: form.value.roleIds })
    showModal.value = false
    load(); loadRoleStats()
  } catch (e) {
    modalError.value = e.response?.data?.message || '操作失败'
  } finally {
    submitting.value = false
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
async function resetPwd(u) {
  if (!confirm(`确定要重置 ${u.username} 的密码吗？`)) return
  try {
    const { data } = await http.put(`/users/${u.id}/reset-password`)
    alert(`密码已重置，新密码：${data.password}\n请立即告知用户并妥善保存`)
  } catch (e) {
    alert(e.response?.data?.message || '重置失败')
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
    load(); loadRoleStats()
  } catch (e) {
    alert(e.response?.data?.message || '保存失败')
  }
}
onMounted(() => { load(); loadRoles(); loadRoleStats() })
</script>

<style scoped>
.um-role-summary { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
.um-role-badge {
  font-size: 12px; padding: 3px 10px; border-radius: 12px;
  background: #f0f5ff; color: #1d39c4; border: 1px solid #d6e4ff;
}
.um-role-badge strong { margin-left: 2px; }
.um-ops { display: flex; gap: 4px; flex-wrap: wrap; }
.um-pagination {
  display: flex; align-items: center; justify-content: space-between;
  margin-top: 16px; padding: 16px 24px; border-top: 1px solid var(--border-soft, #f0f0f0);
}
.um-pagination-info { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-secondary); }
.um-pagination-controls { display: flex; align-items: center; gap: 8px; }
.um-pagination-controls button {
  padding: 5px 12px; border: 1px solid var(--border-color); border-radius: 6px;
  background: #fff; font-size: 13px; color: var(--text-primary); cursor: pointer; transition: all 0.2s;
}
.um-pagination-controls button:hover:not(:disabled) { border-color: var(--primary); color: var(--primary); }
.um-pagination-controls button:disabled { opacity: 0.4; cursor: not-allowed; }
.um-page-current { font-size: 13px; color: var(--text-secondary); min-width: 60px; text-align: center; }
</style>
