<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>凭据管理</h1>
      <span class="badge">{{ credentials.length }} 条</span>
    </div>
    <div style="padding:20px 28px">
      <!-- 健康分布徽章行（点击联动筛选，对齐设备/引擎页） -->
      <div class="cm-stats">
        <button class="cm-stat" :class="{ on: !health && !status }" @click="setFilter('', '')">全部 <strong>{{ total }}</strong></button>
        <button v-for="(label, key) in HEALTH_STATS" :key="key" class="cm-stat" :class="{ on: health === key }" @click="setFilter(key, '')">
          <span class="cm-dot" :style="{ background: (BADGE[key] || {}).color }"></span>{{ label }} <strong>{{ stats[key] ?? 0 }}</strong>
        </button>
      </div>
      <div class="toolbar">
        <select v-model="scope">
          <option value="">全部范围</option>
          <option value="global">全局</option>
          <option value="repo">仓库</option>
          <option value="user">用户</option>
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
            <tr><th>设备名</th><th>归属用户</th><th>范围</th><th>状态</th><th>健康</th><th>引擎</th><th>最后心跳</th><th>IP</th><th>创建时间</th><th style="width:90px">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="c in credentials" :key="c.id">
              <td>{{ c.name }}</td>
              <td>{{ c.ownerName || '-' }}</td>
              <td><span class="status-tag info">{{ scopeText(c.scope) }}</span></td>
              <td><span :class="['status-tag', c.status === 'active' ? 'success' : 'default']">{{ c.status }}</span></td>
              <td><span :class="['status-tag', healthClass(c.health)]">{{ healthText(c.health) }}</span></td>
              <td>{{ c.engineCount }}</td>
              <td class="t3">{{ formatTime(c.lastSeenAt) }}</td>
              <td class="t3">{{ c.lastIp || '-' }}</td>
              <td class="t3">{{ formatTime(c.createdAt) }}</td>
              <td>
                <button v-if="c.status === 'active'" class="btn btn-sm" @click="revoke(c)">吊销</button>
                <span v-else class="t3">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 卡片模式 -->
      <div v-else class="device-grid">
        <div v-for="c in credentials" :key="c.id" class="device-card">
          <div class="dc-head" style="flex-wrap:wrap">
            <span :style="badgeStyle(c.health)">
              <span :style="dotStyle(c.health)"></span>{{ healthText(c.health) }}
            </span>
            <span style="font-weight:600;font-size:14px">
              {{ c.name }}
              <span style="font-weight:400;color:var(--text-tertiary);font-size:12px">
                <span class="status-tag info" style="font-size:11px;padding:1px 6px">{{ scopeText(c.scope) }}</span><span v-if="c.status !== 'active'" class="t3"> · {{ c.status }}</span>
              </span>
            </span>
          </div>
          <div class="dc-meta" style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px">
            <div><span class="t3" style="display:inline-block;min-width:46px">归属</span> {{ c.ownerName || '—' }}</div>
            <div><span class="t3" style="display:inline-block;min-width:46px">引擎</span> {{ c.engineCount }} 个</div>
            <div><span class="t3" style="display:inline-block;min-width:46px">心跳</span> {{ c.lastSeenAt || '—' }}</div>
            <div><span class="t3" style="display:inline-block;min-width:46px">IP</span> {{ c.lastIp || '—' }}</div>
            <div style="grid-column:1 / -1"><span class="t3">创建</span> {{ c.createdAt || '—' }}</div>
          </div>
          <div class="dc-actions" style="justify-content:flex-end">
            <button v-if="c.status === 'active'" class="btn btn-sm" @click="revoke(c)">吊销</button>
            <span v-else class="t3" style="font-size:11px">已吊销</span>
          </div>
        </div>
      </div>
      <p v-if="!credentials.length" class="t2" style="padding:16px;text-align:center">无匹配凭据</p>
    </div>

    <div v-if="showIssue" class="modal" @click.self="showIssue = false">
      <div class="modal-box">
        <h4>签发设备凭据</h4>
        <input v-model="deviceName" placeholder="设备名（唯一，≤64字符）" />
        <div class="modal-actions">
          <button class="btn" @click="showIssue = false">取消</button>
          <button class="btn btn-primary" @click="issue">签发</button>
        </div>
        <div v-if="issuedToken" class="mt12" style="border:1px dashed var(--warning);border-radius:8px;padding:12px">
          <p style="color:var(--warning);font-weight:600">⚠ 明文仅显示一次，请立即保存</p>
          <code class="cred-token">{{ issuedToken }}</code>
          <p class="t3" style="font-size:12px">将该 token 填入目标机器 /etc/grape/runtime.env 的 RUNTIME_TOKEN 并重启 runtime-agent</p>
          <button class="btn btn-primary btn-sm mt8" @click="issuedToken = ''">我已保存</button>
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
const health = ref('')
const stats = ref({})
const showIssue = ref(false)
const deviceName = ref('')
const issuedToken = ref('')
const activeTab = { background: 'var(--primary)', color: '#fff', borderColor: 'var(--primary)' }
const total = ref(0)

const SCOPE_NAMES = { global: '全局', repo: '仓库', user: '用户' }
function scopeText(s) { return SCOPE_NAMES[s] || s || '-' }

// 对齐 Grape HEALTH_BADGE 配色
const BADGE = {
  online: { color: 'var(--success)', bg: '#dcfce7', label: '在线' },
  recently_lost: { color: '#ea5a0c', bg: '#fef3c7', label: '刚失联' },
  offline: { color: 'var(--text-tertiary)', bg: '#eef0f3', label: '离线' },
  about_to_gc: { color: 'var(--danger)', bg: '#fee2e2', label: '待回收' },
  revoked: { color: 'var(--text-tertiary)', bg: '#eef0f3', label: '已吊销' }
}
const HEALTH_STATS = {
  online: '在线',
  recently_lost: '刚失联',
  offline: '离线',
  about_to_gc: '待回收',
  revoked: '已吊销'
}
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
function setFilter(h, s) {
  health.value = h
  status.value = s
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
  const { data } = await http.get('/credentials', { params: { scope: scope.value, owner: owner.value, status: status.value, health: health.value } })
  credentials.value = data.content
  total.value = data.totalElements || data.content.length
  stats.value = data.stats || {}
}
function reset() {
  scope.value = ''
  owner.value = ''
  status.value = ''
  health.value = ''
  load()
}
function openIssue() { showIssue.value = true; issuedToken.value = ''; deviceName.value = '' }
async function issue() {
  try {
    const { data } = await http.post('/credentials', { deviceName: deviceName.value })
    issuedToken.value = data.token
    load()   // 签发成功自动刷新列表
  } catch (e) { alert(e.response?.data?.message || '签发失败') }
}
async function revoke(c) {
  if (!confirm(`确认吊销凭据 ${c.name}（归属 ${c.ownerName || '-'}）？其 worker 将立即失效（401）`)) return
  try {
    await http.post(`/devices/${c.id}/revoke`)
    load()
  } catch (e) {
    alert(e.response?.data?.message || '吊销失败')
  }
}
onMounted(load)
</script>

<style scoped>
/* 健康分布徽章行（对齐设备/引擎管理页） */
.cm-stats { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
.cm-stat {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; padding: 3px 10px; border-radius: 12px;
  background: #f5f7fa; color: var(--text-secondary); border: 1px solid transparent;
  cursor: pointer; transition: all 0.15s;
}
.cm-stat strong { font-weight: 600; color: var(--text-primary); }
.cm-stat:hover { border-color: var(--border-color); }
.cm-stat.on { background: var(--primary-bg); color: var(--primary); border-color: var(--primary); }
.cm-stat.on strong { color: var(--primary); }
.cm-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
</style>
