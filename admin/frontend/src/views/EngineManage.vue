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
        <select v-model="status">
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
            <tr><th>实例名</th><th>类型</th><th>版本</th><th>设备</th><th>范围</th><th>健康</th><th>状态</th><th>最后心跳</th><th style="width:90px">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="e in engines" :key="e.id">
              <td>{{ e.displayName || e.name }}</td>
              <td>{{ e.name }}</td>
              <td class="t3">{{ e.version || '-' }}</td>
              <td>{{ e.device || '-' }}</td>
              <td><span class="status-tag info">{{ scopeText(e.scope) }}</span></td>
              <td><span :class="['status-tag', healthClass(e.health)]">{{ healthText(e.health) }}</span></td>
              <td class="t2">{{ e.status }}</td>
              <td class="t3">{{ e.lastSeenAt || '-' }}</td>
              <td><button class="btn btn-sm" @click="remove(e)">删除</button></td>
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
            <div><span class="t3" style="display:inline-block;min-width:46px">状态</span> {{ e.status }}</div>
            <div><span class="t3" style="display:inline-block;min-width:46px">心跳</span> {{ e.lastSeenAt || '—' }}</div>
            <div><span class="t3" style="display:inline-block;min-width:46px">归属/范围</span> {{ e.ownerId || '—' }} <span class="status-tag info" style="font-size:11px;padding:1px 6px">{{ scopeText(e.scope) }}</span></div>
          </div>
          <div class="dc-actions" style="justify-content:flex-end">
            <button class="btn btn-sm" @click="remove(e)">删除</button>
          </div>
        </div>
      </div>

      <p v-if="!engines.length" class="t2" style="padding:16px;text-align:center">无匹配引擎</p>

      <div v-if="totalPages > 1" class="pagination-bar">
        <span class="page-info">第 <strong>{{ page + 1 }}</strong> / {{ totalPages }} 页（共 {{ totalElements }} 条）</span>
        <button class="page-btn" :disabled="page <= 0" @click="page--; load()">上一页</button>
        <button class="page-btn" :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import http from '../api'

const engines = ref([])
const stats = ref({})
const totalElements = ref(0)
const totalPages = ref(0)
const page = ref(0)
const type = ref('')
const scope = ref('')
const device = ref('')
const status = ref('')
const view = ref('list')
const deviceOptions = ref([])
const activeTab = { background: 'var(--primary)', color: '#fff', borderColor: 'var(--primary)' }

const BADGE = {
  online: { color: 'var(--success)', bg: '#dcfce7', label: '在线' },
  recently_lost: { color: '#ea5a0c', bg: '#fef3c7', label: '刚失联' },
  offline: { color: 'var(--text-tertiary)', bg: '#eef0f3', label: '离线' },
  about_to_gc: { color: 'var(--danger)', bg: '#fee2e2', label: '待回收' }
}
const SCOPE_TEXT = { global: '全局', repo: '仓库', user: '个人' }
function scopeText(s) { return SCOPE_TEXT[s] || s || '—' }
function healthText(h) { return (BADGE[h] || { label: h }).label }
function healthClass(h) { return { online: 'success', recently_lost: 'warning', offline: 'default', about_to_gc: 'danger' }[h] || 'default' }
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
  const { data } = await http.get('/engines', { params: { scope: scope.value, type: type.value, device: device.value, status: status.value, page: page.value, size: 10 } })
  engines.value = data.content
  stats.value = data.stats
  totalElements.value = data.totalElements
  totalPages.value = data.totalPages
}
function search() { page.value = 0; load() }
function reset() { scope.value = ''; type.value = ''; device.value = ''; status.value = ''; page.value = 0; load() }
async function remove(e) {
  if (!confirm(`确认删除引擎 ${e.displayName || e.name}（id=${e.id}）？`)) return
  try {
    await http.delete(`/engines/${e.id}`)
    load()
  } catch (err) { alert(err.response?.data?.message || '删除失败') }
}
onMounted(() => { load(); loadDevices() })
</script>
