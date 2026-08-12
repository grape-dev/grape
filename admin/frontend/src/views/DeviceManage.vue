<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>设备管理</h1>
      <span class="badge">{{ total }} 台</span>
    </div>
    <div style="padding:20px 28px">
      <!-- 健康分布徽章行（点击联动筛选，对齐 VOD 统计头风格） -->
      <div class="dm-stats">
        <button class="dm-stat" :class="{ on: !health }" @click="setHealth('')">全部 <strong>{{ total }}</strong></button>
        <button v-for="(label, key) in STATS" :key="key" class="dm-stat" :class="{ on: health === key }" @click="setHealth(key)">
          <span class="dm-dot" :style="{ background: (BADGE[key] || {}).color }"></span>{{ label }} <strong>{{ stats[key] ?? 0 }}</strong>
        </button>
      </div>
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
        <select v-model="health">
          <option value="">全部健康</option>
          <option v-for="(label, key) in STATS" :key="key" :value="key">{{ label }}</option>
        </select>
        <input v-model="name" placeholder="设备名搜索" style="width:200px" @keyup.enter="search" />
        <button class="btn" @click="search">查询</button>
        <button class="btn" @click="reset">重置</button>
        <div style="margin-left:auto;display:flex;gap:8px">
          <button class="btn" :style="view === 'list' ? activeTab : {}" @click="view = 'list'">列表</button>
          <button class="btn" :style="view === 'card' ? activeTab : {}" @click="view = 'card'">卡片</button>
        </div>
      </div>

      <!-- 列表模式 -->
      <div class="card" v-if="view === 'list'">
        <table class="vod-table">
          <thead>
            <tr><th>设备名</th><th>范围</th><th>归属人</th><th>状态</th><th>健康</th><th>引擎</th><th>任务</th><th>最后心跳</th><th>IP</th><th>注册时间</th><th style="width:180px">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="d in devices" :key="d.id">
              <td>{{ d.deviceName || d.name }}</td>
              <td><span class="status-tag info">{{ scopeText(d.scope) }}</span></td>
              <td>{{ d.ownerName || '—' }}</td>
              <td><span :class="['status-tag', d.status === 'active' ? 'success' : 'default']">{{ d.status === 'active' ? '有效' : '已吊销' }}</span></td>
              <td><span :class="['status-tag', healthClass(d.health)]">{{ healthText(d.health) }}</span></td>
              <td>{{ d.engineCount }}</td>
              <td>{{ d.taskCount }}</td>
              <td class="t3">{{ formatTime(d.lastSeenAt) }}</td>
              <td class="t3">{{ d.lastIp || '-' }}</td>
              <td class="t3">{{ formatTime(d.createdAt) }}</td>
              <td>
                <button class="btn btn-sm" @click="$router.push(`/runtime/devices/${d.id}`)">详情</button>
                <button v-if="d.status === 'active'" class="btn btn-sm" @click="revoke(d)">吊销</button>
                <button v-if="d.status === 'active'" class="btn btn-sm" @click="recycle(d)">回收</button>
                <button v-if="d.status === 'revoked'" class="btn btn-sm" style="color:#e5484d;border-color:#f3b4b6" @click="openDelete(d)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 卡片模式 -->
      <div v-else class="device-grid">
        <div v-for="d in devices" :key="d.id" class="device-card">
          <div class="dc-head" style="flex-wrap:wrap">
            <span style="display:inline-flex;align-items:center;gap:8px;min-width:0">
              <span :style="badgeStyle(d.health)">
                <span :style="dotStyle(d.health)"></span>{{ healthText(d.health) }}
              </span>
              <span style="max-width:180px;font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" :title="d.deviceName || d.name">{{ d.deviceName || d.name }}</span>
            </span>
            <span class="status-tag info" style="font-size:11px;padding:1px 6px">{{ scopeText(d.scope) }}</span>
          </div>
          <div class="dc-meta" style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px">
            <div class="ellipsis-cell" :title="d.ownerName || '—'"><span class="t3" style="display:inline-block;min-width:46px">归属</span> {{ d.ownerName || '—' }}</div>
            <div><span class="t3" style="display:inline-block;min-width:46px">引擎</span> {{ d.engineCount }} 个</div>
            <div class="ellipsis-cell" :title="formatTime(d.lastSeenAt)"><span class="t3" style="display:inline-block;min-width:46px">心跳</span> {{ formatTime(d.lastSeenAt) }}</div>
            <div class="ellipsis-cell" :title="d.lastIp || '—'"><span class="t3" style="display:inline-block;min-width:46px">IP</span> {{ d.lastIp || '—' }}</div>
            <div style="grid-column:1 / -1"><span class="t3" style="display:inline-block;min-width:46px">任务</span> {{ d.taskCount }} 个</div>
          </div>
          <div class="dc-actions" style="justify-content:flex-end">
            <button class="btn btn-sm" @click="$router.push(`/runtime/devices/${d.id}`)">详情</button>
            <button v-if="d.status === 'active'" class="btn btn-sm" @click="revoke(d)">吊销</button>
            <button v-if="d.status === 'active'" class="btn btn-sm" @click="recycle(d)">回收</button>
            <button v-if="d.status === 'revoked'" class="btn btn-sm" style="color:#e5484d;border-color:#f3b4b6" @click="openDelete(d)">删除</button>
          </div>
        </div>
      </div>

      <p v-if="!devices.length" class="t2" style="padding:16px;text-align:center">无匹配设备</p>
      <div v-if="total > 0" class="pagination-bar">
        <span class="page-info">
          <template v-if="totalPages > 1">第 <strong>{{ page + 1 }}</strong> / {{ totalPages }} 页（共 {{ total }} 台）</template>
          <template v-else>共 {{ total }} 台</template>
        </span>
        <select v-model="size" class="page-size-select" @change="page = 0; load()">
          <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}条/页</option>
        </select>
        <button v-if="totalPages > 1" class="page-btn" :disabled="page <= 0" @click="page--; load()">上一页</button>
        <button v-if="totalPages > 1" class="page-btn" :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
      </div>
    </div>

    <!-- Toast 反馈（对齐 AI 分析页） -->
    <div v-if="toastVisible" class="toast-message" :class="toastType">{{ toastMessage }}</div>

    <!-- 吊销确认弹窗（对齐 AI 分析页 modal） -->
    <div v-if="revokeTarget" class="modal-overlay" @click.self="revokeTarget = null">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">⚠️</span>
          <span class="modal-title">吊销设备凭据</span>
        </div>
        <div class="modal-body">
          确认吊销 <b>{{ revokeTarget.deviceName || revokeTarget.name }}</b> 的凭据？<br />
          其 worker 将立即失效（401），且不可逆。
        </div>
        <div class="modal-footer">
          <button class="btn" @click="revokeTarget = null">取消</button>
          <button class="btn btn-danger" @click="confirmRevoke">确认吊销</button>
        </div>
      </div>
    </div>

    <!-- 回收确认弹窗（对齐 AI 分析页 modal） -->
    <div v-if="recycleTarget" class="modal-overlay" @click.self="recycleTarget = null">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">⚠️</span>
          <span class="modal-title">回收设备</span>
        </div>
        <div class="modal-body">
          <p style="margin:0 0 12px">回收将执行：吊销凭据 → 删除引擎记录 → 清理终态任务。<b style="color:#e5484d">不可逆！</b></p>
          <p style="margin:0 0 6px">请输入设备名 <b>{{ recycleTarget.deviceName || recycleTarget.name }}</b> 确认：</p>
          <input v-model="recycleWord" class="form-input" placeholder="输入设备名确认" style="width:100%;padding:8px 12px;border:1px solid var(--border-color);border-radius:6px;font-size:14px" />
        </div>
        <div class="modal-footer">
          <button class="btn" @click="recycleTarget = null">取消</button>
          <button class="btn btn-danger" :disabled="recycleWord !== (recycleTarget.deviceName || recycleTarget.name)" @click="confirmRecycle">确认回收</button>
        </div>
      </div>
    </div>
    <!-- 删除设备确认弹窗（物理删除不可逆） -->
    <div v-if="deleteTarget" class="modal-overlay" @click.self="deleteTarget = null">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">🗑️</span>
          <span class="modal-title">删除设备</span>
        </div>
        <div class="modal-body">
          <p style="margin:0 0 12px">将<b style="color:#e5484d">物理删除</b>设备 <b>{{ deleteTarget.deviceName || deleteTarget.name }}</b> 的全部记录（凭据/引擎/任务），<b style="color:#e5484d">不可恢复！</b><br />
          设备名将释放，可重新签发同名设备。</p>
          <p style="margin:0 0 6px">请输入设备名 <b>{{ deleteTarget.deviceName || deleteTarget.name }}</b> 确认：</p>
          <input v-model="deleteWord" class="form-input" placeholder="输入设备名确认" style="width:100%;padding:8px 12px;border:1px solid var(--border-color);border-radius:6px;font-size:14px" />
        </div>
        <div class="modal-footer">
          <button class="btn" @click="deleteTarget = null">取消</button>
          <button class="btn btn-danger" :disabled="deleteWord !== (deleteTarget.deviceName || deleteTarget.name)" @click="confirmDeleteDevice">确认删除</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import http from '../api'

const devices = ref([])
const total = ref(0)
const totalPages = ref(0)
const page = ref(0)
const status = ref('')
const scope = ref('')
const health = ref('')
const name = ref('')
const stats = ref({})
const view = ref('list')
const activeTab = { background: 'var(--primary)', color: '#fff', borderColor: 'var(--primary)' }
const sizeOptions = [10, 20, 50]
const size = ref(10)
// toast + 确认弹窗状态（对齐 AI 分析页）
const toastVisible = ref(false)
const toastMessage = ref('')
const toastType = ref('success')
let toastTimer = null
const revokeTarget = ref(null)
const recycleTarget = ref(null)
const recycleWord = ref('')
const deleteTarget = ref(null)
const deleteWord = ref('')

const BADGE = {
  online: { label: '在线', color: 'var(--success)', bg: '#dcfce7' },
  recently_lost: { label: '刚失联', color: '#ea5a0c', bg: '#fef3c7' },
  offline: { label: '离线', color: 'var(--text-tertiary)', bg: '#eef0f3' },
  about_to_gc: { label: '待回收', color: 'var(--danger)', bg: '#fee2e2' },
  revoked: { label: '已吊销', color: 'var(--text-tertiary)', bg: '#eef0f3' }
}
const STATS = {
  online: '在线',
  recently_lost: '刚失联',
  offline: '离线',
  about_to_gc: '待回收',
  revoked: '已吊销'
}
const SCOPE_TEXT = { global: '全局', repo: '仓库', user: '个人' }
function scopeText(s) { return SCOPE_TEXT[s] || s || '—' }
function healthText(h) { return (BADGE[h] || { label: h }).label }
function healthClass(h) { return { online: 'success', recently_lost: 'warning', offline: 'default', about_to_gc: 'danger', revoked: 'default' }[h] || 'default' }
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
function setHealth(h) {
  health.value = h
  page.value = 0
  load()
}
function badgeStyle(h) {
  const b = BADGE[h] || { color: 'var(--text-tertiary)', bg: '#eef0f3' }
  return { display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '12px', fontWeight: 600, color: b.color, background: b.bg, padding: '3px 10px', borderRadius: '999px', whiteSpace: 'nowrap' }
}
function dotStyle(h) {
  const b = BADGE[h] || { color: 'var(--text-tertiary)' }
  return { width: '7px', height: '7px', borderRadius: '50%', background: b.color, display: 'inline-block' }
}

async function load() {
  const { data } = await http.get('/devices', { params: { scope: scope.value, status: status.value, health: health.value, name: name.value, page: page.value, size: size.value } })
  devices.value = data.content
  total.value = data.totalElements
  totalPages.value = data.totalPages
  stats.value = data.stats || {}
}
function search() { page.value = 0; load() }
function reset() { scope.value = ''; status.value = ''; health.value = ''; name.value = ''; page.value = 0; load() }

function showToast(msg, type = 'success') {
  toastMessage.value = msg
  toastType.value = type
  toastVisible.value = true
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toastVisible.value = false }, 2500)
}
function revoke(d) {
  revokeTarget.value = d
}
async function confirmRevoke() {
  const d = revokeTarget.value
  try {
    await http.post(`/devices/${d.id}/revoke`)
    showToast(`设备 ${d.deviceName || d.name} 凭据已吊销`)
    revokeTarget.value = null
    load()
  } catch (e) {
    showToast(e.response?.data?.message || '吊销失败', 'error')
  }
}
function recycle(d) {
  recycleWord.value = ''
  recycleTarget.value = d
}
async function confirmRecycle() {
  const d = recycleTarget.value
  try {
    await http.post(`/devices/${d.id}/cleanup`)
    showToast(`设备 ${d.deviceName || d.name} 已回收`)
    recycleTarget.value = null
    load()
  } catch (e) {
    showToast(e.response?.data?.message || '回收失败', 'error')
  }
}
function openDelete(d) {
  deleteWord.value = ''
  deleteTarget.value = d
}
async function confirmDeleteDevice() {
  const d = deleteTarget.value
  try {
    await http.delete(`/devices/${d.id}`)
    showToast(`设备 ${d.deviceName || d.name} 已删除（不可恢复）`)
    deleteTarget.value = null
    load()
  } catch (e) {
    showToast(e.response?.data?.message || '删除失败', 'error')
  }
}
onMounted(load)
</script>

<style scoped>
/* 健康分布徽章行（对齐 UserManage 角色徽章行风格） */
.dm-stats { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
.dm-stat {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; padding: 3px 10px; border-radius: 12px;
  background: #f5f7fa; color: var(--text-secondary); border: 1px solid transparent;
  cursor: pointer; transition: all 0.15s;
}
.dm-stat strong { font-weight: 600; color: var(--text-primary); }
.dm-stat:hover { border-color: var(--border-color); }
.dm-stat.on { background: var(--primary-bg); color: var(--primary); border-color: var(--primary); }
.dm-stat.on strong { color: var(--primary); }
.dm-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
</style>
