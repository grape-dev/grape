<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>引擎管理</h1>
      <span class="badge">{{ totalElements }} 条</span>
    </div>
    <div style="padding:20px 28px">
      <!-- 引擎类型统计 -->
      <div class="stats">
        <div class="stat-card"><div class="stat-label">引擎总数</div><div class="stat-value">{{ total }}</div></div>
        <div v-for="(cnt, t) in stats" :key="t" class="stat-card">
          <div class="stat-label">{{ t }}</div><div class="stat-value small">{{ cnt }}</div>
        </div>
      </div>

      <!-- 健康分布徽章行（点击联动筛选，对齐设备管理页） -->
      <div class="em-stats">
        <button class="em-stat" :class="{ on: !health }" @click="setHealth('')">全部 <strong>{{ totalElements }}</strong></button>
        <button v-for="(label, key) in HEALTH_STATS" :key="key" class="em-stat" :class="{ on: health === key }" @click="setHealth(key)">
          <span class="em-dot" :style="{ background: (BADGE[key] || {}).color }"></span>{{ label }} <strong>{{ healthStats[key] ?? 0 }}</strong>
        </button>
      </div>

      <div class="toolbar">
        <select v-model="scope">
          <option value="">全部范围</option>
          <option value="global">全局</option>
          <option value="repo">仓库</option>
          <option value="user">个人</option>
        </select>
        <select v-model="type">
          <option value="">全部类型</option>
          <option v-for="t in typeOptions" :key="t" :value="t">{{ t }}</option>
        </select>
        <select v-model="device">
          <option value="">全部设备</option>
          <option v-for="d in deviceOptions" :key="d" :value="d">{{ d }}</option>
        </select>
        <select v-model="health">
          <option value="">全部状态</option>
          <option value="online">在线</option>
          <option value="recently_lost">刚失联</option>
          <option value="offline">离线</option>
          <option value="about_to_gc">待回收</option>
        </select>
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
            <tr><th>实例名</th><th>类型</th><th>版本</th><th>二进制</th><th>提供方</th><th>设备</th><th>范围</th><th>健康</th><th>最后心跳</th><th style="width:90px">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="e in engines" :key="e.id">
              <td>{{ e.displayName || e.name }}</td>
              <td>{{ e.name }}</td>
              <td class="t3" style="max-width:200px">{{ e.version || '-' }}</td>
              <td class="t3">{{ e.binary || '-' }}</td>
              <td class="t3">{{ e.provider || '-' }}</td>
              <td>{{ e.device || '-' }}</td>
              <td><span class="status-tag info">{{ scopeText(e.scope) }}</span></td>
              <td><span :class="['status-tag', healthClass(e.health)]">{{ healthText(e.health) }}</span></td>
              <td class="t3">{{ formatTime(e.lastSeenAt) }}</td>
              <td><button v-if="e.status === 'offline'" class="btn btn-sm" style="color:#e5484d;border-color:#f3b4b6" @click="openDelete(e)">删除</button></td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 卡片模式 -->
      <div v-else class="device-grid">
        <div v-for="e in engines" :key="e.id" class="device-card">
          <div class="dc-head" style="flex-wrap:wrap">
            <span :style="badgeStyle(e.health)">
              <span :style="dotStyle(e.health)"></span>{{ healthText(e.health) }}
            </span>
            <span style="font-weight:600;font-size:14px">
              {{ e.displayName || e.name }}
              <span style="font-weight:400;color:var(--text-tertiary);font-size:12px">{{ e.name }} v{{ e.version || '' }}</span>
            </span>
          </div>
          <div class="dc-meta" style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px">
            <div><span class="t3" style="display:inline-block;min-width:46px">设备</span> {{ e.device || '—' }}</div>
            <div><span class="t3" style="display:inline-block;min-width:46px">健康</span> <span :class="['status-tag', healthClass(e.health)]">{{ healthText(e.health) }}</span></div>
            <div><span class="t3" style="display:inline-block;min-width:46px">心跳</span> {{ formatTime(e.lastSeenAt) }}</div>
            <div><span class="t3" style="display:inline-block;min-width:46px">归属/范围</span> {{ e.ownerId || '—' }} <span class="status-tag info" style="font-size:11px;padding:1px 6px">{{ scopeText(e.scope) }}</span></div>
          </div>
          <div class="dc-actions" style="justify-content:flex-end">
            <button v-if="e.status === 'offline'" class="btn btn-sm" style="color:#e5484d;border-color:#f3b4b6" @click="openDelete(e)">删除</button>
          </div>
        </div>
      </div>

      <p v-if="!engines.length" class="t2" style="padding:16px;text-align:center">无匹配引擎</p>

      <div v-if="totalPages > 1" class="pagination-bar">
        <span class="page-info">第 <strong>{{ page + 1 }}</strong> / {{ totalPages }} 页（共 {{ totalElements }} 条）</span>
        <select v-model="size" class="page-size-select" @change="page = 0; load()">
          <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}条/页</option>
        </select>
        <button class="page-btn" :disabled="page <= 0" @click="page--; load()">上一页</button>
        <button class="page-btn" :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
      </div>
    </div>

    <!-- Toast 反馈 -->
    <div v-if="toastVisible" class="toast-message" :class="toastType">{{ toastMessage }}</div>

    <!-- 删除引擎确认弹窗 -->
    <div v-if="deleteTarget" class="modal-overlay" @click.self="deleteTarget = null">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">🗑️</span>
          <span class="modal-title">删除引擎</span>
        </div>
        <div class="modal-body">
          确认删除离线引擎 <b>{{ deleteTarget.displayName || deleteTarget.name }}</b>（id={{ deleteTarget.id }}）？<br />
          删除后若其 worker 重新上线会重新注册（新 id）。
        </div>
        <div class="modal-footer">
          <button class="btn" @click="deleteTarget = null">取消</button>
          <button class="btn btn-danger" @click="confirmDelete">确认删除</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import http from '../api'

const engines = ref([])
const stats = ref({})
const healthStats = ref({})
const totalElements = ref(0)
const totalPages = ref(0)
const page = ref(0)
const type = ref('')
const scope = ref('')
const device = ref('')
const health = ref('')
const view = ref('list')
const deviceOptions = ref([])
const sizeOptions = [10, 20, 50]
const size = ref(10)
const activeTab = { background: 'var(--primary)', color: '#fff', borderColor: 'var(--primary)' }
// toast + 删除弹窗状态
const toastVisible = ref(false)
const toastMessage = ref('')
const toastType = ref('success')
let toastTimer = null
const deleteTarget = ref(null)

const BADGE = {
  online: { color: 'var(--success)', bg: '#dcfce7', label: '在线' },
  recently_lost: { color: '#ea5a0c', bg: '#fef3c7', label: '刚失联' },
  offline: { color: 'var(--text-tertiary)', bg: '#eef0f3', label: '离线' },
  about_to_gc: { color: 'var(--danger)', bg: '#fee2e2', label: '待回收' }
}
const HEALTH_STATS = {
  online: '在线',
  recently_lost: '刚失联',
  offline: '离线',
  about_to_gc: '待回收'
}
const SCOPE_TEXT = { global: '全局', repo: '仓库', user: '个人' }
function scopeText(s) { return SCOPE_TEXT[s] || s || '—' }
function healthText(h) { return (BADGE[h] || { label: h }).label }
function healthClass(h) { return { online: 'success', recently_lost: 'warning', offline: 'default', about_to_gc: 'danger' }[h] || 'default' }
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

const typeOptions = computed(() => Object.keys(stats.value))
const total = computed(() => Object.values(stats.value).reduce((s, n) => s + n, 0))

async function loadDevices() {
  const { data } = await http.get('/devices', { params: { size: 100 } })
  deviceOptions.value = [...new Set(data.content.map(d => d.deviceName || d.name))]
}
async function load() {
  const { data } = await http.get('/engines', { params: { scope: scope.value, type: type.value, device: device.value, status: health.value, page: page.value, size: size.value } })
  engines.value = data.content
  stats.value = data.stats
  healthStats.value = data.healthStats || {}
  totalElements.value = data.totalElements
  totalPages.value = data.totalPages
}
function search() { page.value = 0; load() }
function reset() { scope.value = ''; type.value = ''; device.value = ''; health.value = ''; page.value = 0; load() }
function showToast(msg, type = 'success') {
  toastMessage.value = msg
  toastType.value = type
  toastVisible.value = true
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toastVisible.value = false }, 2500)
}
function openDelete(e) {
  deleteTarget.value = e
}
async function confirmDelete() {
  const e = deleteTarget.value
  try {
    await http.delete(`/engines/${e.id}`)
    showToast(`引擎 ${e.displayName || e.name} 已删除`)
    deleteTarget.value = null
    load()
  } catch (err) {
    showToast(err.response?.data?.message || '删除失败', 'error')
  }
}
onMounted(() => { load(); loadDevices() })
</script>

<style scoped>
/* 健康分布徽章行（对齐设备管理页） */
.em-stats { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
.em-stat {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; padding: 3px 10px; border-radius: 12px;
  background: #f5f7fa; color: var(--text-secondary); border: 1px solid transparent;
  cursor: pointer; transition: all 0.15s;
}
.em-stat strong { font-weight: 600; color: var(--text-primary); }
.em-stat:hover { border-color: var(--border-color); }
.em-stat.on { background: var(--primary-bg); color: var(--primary); border-color: var(--primary); }
.em-stat.on strong { color: var(--primary); }
.em-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
</style>
