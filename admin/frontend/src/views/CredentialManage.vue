<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>凭据管理</h1>
      <span class="badge">{{ total }} 条</span>
    </div>
    <div style="padding:20px 28px">
      <div class="toolbar">
        <select v-model="scope">
          <option value="">全部范围</option>
          <option value="global">全局</option>
          <option value="repo">仓库</option>
          <option value="user">个人</option>
        </select>
        <select v-model="status">
          <option value="">全部状态</option>
          <option value="active">有效</option>
          <option value="revoked">已吊销</option>
        </select>
        <input v-model="owner" placeholder="归属用户（GitCode 用户名）" style="width:200px" @keyup.enter="load" />
        <button class="btn" @click="load">查询</button>
        <button class="btn" @click="reset">重置</button>
        <div style="margin-left:auto;display:flex;gap:8px">
          <button class="btn btn-primary" @click="openIssue">签发凭据</button>
          <button class="btn" :style="view === 'list' ? activeTab : {}" @click="view = 'list'">列表</button>
          <button class="btn" :style="view === 'card' ? activeTab : {}" @click="view = 'card'">卡片</button>
        </div>
      </div>

      <!-- 列表模式 -->
      <div class="card" v-if="view === 'list'">
        <table class="vod-table">
          <thead>
            <tr><th>凭据名称</th><th>归属用户</th><th>范围</th><th>状态</th><th>引擎</th><th>最后心跳</th><th>IP</th><th>创建时间</th><th style="width:90px">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="c in credentials" :key="c.id">
              <td>{{ c.name }}</td>
              <td>{{ c.ownerName || '-' }}</td>
              <td><span class="status-tag info">{{ scopeText(c.scope) }}</span></td>
              <td><span :class="['status-tag', c.status === 'active' ? 'success' : 'default']">{{ c.status === 'active' ? '有效' : '已吊销' }}</span></td>
              <td>{{ c.engineCount }}</td>
              <td class="t3">{{ formatTime(c.lastSeenAt) }}</td>
              <td class="t3">{{ c.lastIp || '-' }}</td>
              <td class="t3">{{ formatTime(c.createdAt) }}</td>
              <td>
                <button v-if="c.status === 'active'" class="btn btn-sm" @click="openRevoke(c)">吊销</button>
                <button v-if="c.status === 'revoked'" class="btn btn-sm" style="color:#e5484d;border-color:#f3b4b6" @click="openDelete(c)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 卡片模式 -->
      <div v-else class="device-grid">
        <div v-for="c in credentials" :key="c.id" class="device-card">
          <div class="dc-head" style="flex-wrap:wrap">
            <span style="display:inline-flex;align-items:center;gap:8px;min-width:0">
              <span :class="['status-tag', c.status === 'active' ? 'success' : 'default']">{{ c.status === 'active' ? '有效' : '已吊销' }}</span>
              <span style="max-width:180px;font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" :title="c.name">{{ c.name }}</span>
            </span>
            <span class="status-tag info" style="font-size:11px;padding:1px 6px">{{ scopeText(c.scope) }}</span>
          </div>
          <div class="dc-meta" style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px">
            <div class="ellipsis-cell" :title="c.ownerName || '—'"><span class="t3" style="display:inline-block;min-width:46px">归属</span> {{ c.ownerName || '—' }}</div>
            <div><span class="t3" style="display:inline-block;min-width:46px">引擎</span> {{ c.engineCount }} 个</div>
            <div class="ellipsis-cell" :title="formatTime(c.lastSeenAt)"><span class="t3" style="display:inline-block;min-width:46px">心跳</span> {{ formatTime(c.lastSeenAt) }}</div>
            <div class="ellipsis-cell" :title="c.lastIp || '—'"><span class="t3" style="display:inline-block;min-width:46px">IP</span> {{ c.lastIp || '—' }}</div>
          </div>
          <div class="dc-actions" style="justify-content:flex-end">
            <button v-if="c.status === 'active'" class="btn btn-sm" @click="openRevoke(c)">吊销</button>
            <button v-if="c.status === 'revoked'" class="btn btn-sm" style="color:#e5484d;border-color:#f3b4b6" @click="openDelete(c)">删除</button>
          </div>
        </div>
      </div>
      <p v-if="!credentials.length" class="t2" style="padding:16px;text-align:center">无匹配凭据</p>

      <div v-if="total > 0" class="pagination-bar">
        <span class="page-info">第 <strong>{{ page + 1 }}</strong> / {{ totalPages }} 页（共 {{ total }} 条）</span>
        <select v-model="size" class="page-size-select" @change="page = 0; load()">
          <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}条/页</option>
        </select>
        <button class="page-btn" :disabled="page <= 0" @click="page--; load()">上一页</button>
        <button class="page-btn" :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
      </div>
    </div>

    <div v-if="showIssue" class="modal-overlay" @click.self="showIssue = false">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">🔑</span>
          <span class="modal-title">签发凭据</span>
        </div>
        <div class="modal-body">
          <div style="margin-bottom:8px;font-size:13px;color:var(--text-secondary)">范围：<span class="status-tag info" style="font-size:11px;padding:1px 6px">全局</span>（运营管理台仅签发全局凭据）</div>
          <input v-model="credentialName" placeholder="凭据名称（唯一，≤64字符）" style="width:100%;padding:8px 12px;border:1px solid var(--border-color);border-radius:6px;font-size:14px" @keyup.enter="issue" />
          <div v-if="issuedToken" class="mt12" style="border:1px dashed var(--warning);border-radius:8px;padding:12px">
            <p style="color:var(--warning);font-weight:600">⚠ 明文仅显示一次，请立即保存</p>
            <code class="cred-token">{{ issuedToken }}</code>
            <p class="t3" style="font-size:12px">将该 token 填入目标机器 /etc/grape/runtime.env 的 RUNTIME_TOKEN 并重启 runtime-agent</p>
            <button class="btn btn-primary btn-sm mt8" @click="issuedToken = ''">我已保存</button>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn" @click="showIssue = false">取消</button>
          <button class="btn btn-primary" :disabled="issuing" @click="issue">{{ issuing ? '签发中…' : '签发' }}</button>
        </div>
      </div>
    </div>

    <!-- 吊销确认弹窗（对齐设备页 modal） -->
    <div v-if="revokeTarget" class="modal-overlay" @click.self="revokeTarget = null">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">⛔</span>
          <span class="modal-title">吊销凭据</span>
        </div>
        <div class="modal-body">
          确认吊销凭据 <b>{{ revokeTarget.name }}</b>（归属 {{ revokeTarget.ownerName || '-' }}）？<br />
          其 worker 将立即失效（401）。
        </div>
        <div class="modal-footer">
          <button class="btn" @click="revokeTarget = null">取消</button>
          <button class="btn btn-danger" @click="confirmRevoke">确认吊销</button>
        </div>
      </div>
    </div>

    <!-- Toast 反馈 -->
    <div v-if="toastVisible" class="toast-message" :class="toastType">{{ toastMessage }}</div>

    <!-- 删除凭据确认弹窗（物理删除管理台记录，worker 可重新注册） -->
    <div v-if="deleteTarget" class="modal-overlay" @click.self="deleteTarget = null">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">🗑️</span>
          <span class="modal-title">删除凭据</span>
        </div>
        <div class="modal-body">
          <p style="margin:0 0 12px">将物理删除凭据 <b>{{ deleteTarget.name }}</b> 及其关联的引擎/任务记录（管理台数据，删除后不可恢复）。<br />
          worker 机器不受影响——重新签发凭据并更新 worker 端 token 后，可重新注册上线。凭据名称将释放，可签发同名凭据。</p>
          <p style="margin:0 0 6px">请输入凭据名 <b>{{ deleteTarget.name }}</b> 确认：</p>
          <input v-model="deleteWord" class="form-input" placeholder="输入凭据名确认" style="width:100%;padding:8px 12px;border:1px solid var(--border-color);border-radius:6px;font-size:14px" />
        </div>
        <div class="modal-footer">
          <button class="btn" @click="deleteTarget = null">取消</button>
          <button class="btn btn-danger" :disabled="deleteWord !== deleteTarget.name" @click="confirmDeleteCredential">确认删除</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import http from '../api'

const credentials = ref([])
const view = ref('list')
const scope = ref('')
const owner = ref('')
const status = ref('')
const showIssue = ref(false)
const credentialName = ref('')
const issuedToken = ref('')
const issuing = ref(false)
const revokeTarget = ref(null)
const activeTab = { background: 'var(--primary)', color: '#fff', borderColor: 'var(--primary)' }
const total = ref(0)
const totalPages = ref(0)
const page = ref(0)
const sizeOptions = [10, 20, 50]
const size = ref(10)
// toast + 删除弹窗状态
const toastVisible = ref(false)
const toastMessage = ref('')
const toastType = ref('success')
let toastTimer = null
const deleteTarget = ref(null)
const deleteWord = ref('')

const SCOPE_NAMES = { global: '全局', repo: '仓库', user: '个人' }
function scopeText(s) { return SCOPE_NAMES[s] || s || '-' }

function formatTime(ts) {
  if (!ts) return '—'
  const time = new Date(ts.replace(' ', 'T')).getTime()
  if (isNaN(time)) return ts
  const diff = Date.now() - time
  if (diff < 60000) return '刚刚'
  if (diff < 3600000) return Math.floor(diff / 60000) + '分钟前'
  if (diff < 86400000) return Math.floor(diff / 3600000) + '小时前'
  return ts.slice(0, 16)
}
async function load() {
  const { data } = await http.get('/credentials', { params: { scope: scope.value, owner: owner.value, status: status.value, page: page.value, size: size.value } })
  credentials.value = data.content
  total.value = data.totalElements || data.content.length
  totalPages.value = data.totalPages || 1
}
function reset() {
  scope.value = ''
  owner.value = ''
  status.value = ''
  page.value = 0
  load()
}
function openIssue() { showIssue.value = true; issuedToken.value = ''; credentialName.value = '' }
async function issue() {
  if (issuing.value) return
  if (!credentialName.value.trim()) { showToast('请输入凭据名称', 'error'); return }
  issuing.value = true
  try {
    const { data } = await http.post('/credentials', { name: credentialName.value })
    issuedToken.value = data.token
    showToast(`凭据 ${data.name} 签发成功`)
    load()   // 签发成功自动刷新列表
  } catch (e) {
    showToast(e.response?.data?.message || '签发失败', 'error')
  } finally {
    issuing.value = false
  }
}
function openRevoke(c) {
  revokeTarget.value = c
}
async function confirmRevoke() {
  const c = revokeTarget.value
  try {
    await http.post(`/credentials/${c.id}/revoke`)
    showToast(`凭据 ${c.name} 已吊销，其 worker 已失效`)
    revokeTarget.value = null
    load()
  } catch (e) {
    showToast(e.response?.data?.message || '吊销失败', 'error')
  }
}
function showToast(msg, type = 'success') {
  toastMessage.value = msg
  toastType.value = type
  toastVisible.value = true
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toastVisible.value = false }, 2500)
}
function openDelete(c) {
  deleteWord.value = ''
  deleteTarget.value = c
}
async function confirmDeleteCredential() {
  const c = deleteTarget.value
  try {
    await http.delete(`/credentials/${c.id}`)
    showToast(`凭据 ${c.name} 已删除（管理台记录清除）`)
    deleteTarget.value = null
    load()
  } catch (e) {
    showToast(e.response?.data?.message || '删除失败', 'error')
  }
}
onMounted(load)
</script>

<style scoped>
/* ===== 工具条 ===== */
</style>
