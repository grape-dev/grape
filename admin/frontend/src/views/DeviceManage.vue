<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>设备管理</h1>
      <span class="badge">{{ total }} 台</span>
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
          <option value="active">active</option>
          <option value="revoked">revoked</option>
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
            <tr><th>设备名</th><th>范围</th><th>状态</th><th>健康</th><th>引擎</th><th>任务</th><th>最后心跳</th><th>IP</th><th style="width:180px">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="d in devices" :key="d.id">
              <td>{{ d.deviceName || d.name }}</td>
              <td><span class="status-tag info">{{ scopeText(d.scope) }}</span></td>
              <td><span :class="['status-tag', d.status === 'active' ? 'success' : 'default']">{{ d.status }}</span></td>
              <td><span :class="['status-tag', healthClass(d.health)]">{{ healthText(d.health) }}</span></td>
              <td>{{ d.engineCount }}</td>
              <td>{{ d.taskCount }}</td>
              <td class="t3">{{ d.lastSeenAt || '-' }}</td>
              <td class="t3">{{ d.lastIp || '-' }}</td>
              <td>
                <button class="btn btn-sm" @click="openDetail(d)">详情</button>
                <button v-if="d.status === 'active'" class="btn btn-sm" @click="revoke(d)">吊销</button>
                <button class="btn btn-sm" @click="recycle(d)">回收</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 卡片模式 -->
      <div v-else class="device-grid">
        <div v-for="d in devices" :key="d.id" class="device-card">
          <div class="dc-head" style="flex-wrap:wrap">
            <span :style="badgeStyle(d.health)">
              <span :style="dotStyle(d.health)"></span>{{ healthText(d.health) }}
            </span>
            <span style="font-weight:600;font-size:14px">
              {{ d.deviceName || d.name }}
              <span style="font-weight:400;color:var(--text-tertiary);font-size:12px">{{ d.status }} <span class="status-tag info" style="font-size:11px;padding:1px 6px">{{ scopeText(d.scope) }}</span></span>
            </span>
          </div>
          <div class="dc-meta" style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px">
            <div><span class="t3" style="display:inline-block;min-width:46px">引擎</span> {{ d.engineCount }} 个</div>
            <div><span class="t3" style="display:inline-block;min-width:46px">任务</span> {{ d.taskCount }} 个</div>
            <div><span class="t3" style="display:inline-block;min-width:46px">心跳</span> {{ d.lastSeenAt || '—' }}</div>
            <div><span class="t3" style="display:inline-block;min-width:46px">IP</span> {{ d.lastIp || '—' }}</div>
          </div>
          <div class="dc-actions" style="justify-content:flex-end">
            <button class="btn btn-sm" @click="openDetail(d)">详情</button>
            <button v-if="d.status === 'active'" class="btn btn-sm" @click="revoke(d)">吊销</button>
            <button class="btn btn-sm" @click="recycle(d)">回收</button>
          </div>
        </div>
      </div>

      <p v-if="!devices.length" class="t2" style="padding:16px;text-align:center">无匹配设备</p>

      <div v-if="total > 0" class="pagination-bar">
        <span class="page-info">第 <strong>{{ page + 1 }}</strong> / {{ totalPages }} 页（共 {{ total }} 台）</span>
        <select v-model="size" class="page-size-select" @change="page = 0; load()">
          <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}条/页</option>
        </select>
        <button class="page-btn" :disabled="page <= 0" @click="page--; load()">上一页</button>
        <button class="page-btn" :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
      </div>
    </div>

    <div v-if="detail" class="modal wide" @click.self="detail = null">
      <div class="modal-box">
        <h4>设备详情：{{ detail.deviceName || detail.name }}</h4>
        <p class="t2" style="font-size:12px">健康：{{ detail.health }} ｜ 最后心跳：{{ detail.lastSeenAt }} ｜ IP：{{ detail.lastIp }}</p>
        <div style="margin:14px 0 4px">
          <select v-model="selectedEngineId" class="page-size-select" style="max-width:260px" @change="onEngineChange">
            <option :value="null">全部引擎</option>
            <option v-for="e in detail.engines || []" :key="e.id" :value="e.id">{{ e.name }}</option>
          </select>
        </div>
        <h5>关联任务（{{ detail.tasks?.totalElements ?? 0 }} 条）</h5>
        <div style="overflow-x:auto">
          <table class="vod-table">
            <thead><tr><th>ID</th><th>类型</th><th>技能</th><th>状态</th><th>创建时间</th></tr></thead>
            <tbody>
              <tr v-for="t in detail.tasks?.content || []" :key="t.id">
                <td class="t3">{{ t.id }}</td>
                <td>{{ t.agentType }}</td>
                <td>{{ t.skill }}</td>
                <td><span class="status-tag default">{{ t.status }}</span></td>
                <td class="t3">{{ t.createdAt }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="detail.tasks?.totalElements > 0" class="pagination-bar" style="border-top:none">
          <span class="page-info">第 <strong>{{ taskPage + 1 }}</strong> / {{ detail.tasks?.totalPages ?? 1 }} 页（共 {{ detail.tasks?.totalElements ?? 0 }} 条）</span>
          <select v-model="taskSize" class="page-size-select" @change="taskPage = 0; openDetail(detail)">
            <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}条/页</option>
          </select>
          <button class="page-btn" :disabled="taskPage <= 0" @click="taskPage--; openDetail(detail)">上一页</button>
          <button class="page-btn" :disabled="taskPage >= (detail.tasks?.totalPages ?? 1) - 1" @click="taskPage++; openDetail(detail)">下一页</button>
        </div>
        <div class="modal-actions sticky"><button class="btn" @click="detail = null">关闭</button></div>
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
const name = ref('')
const view = ref('list')
const detail = ref(null)
const selectedEngineId = ref(null)
const activeTab = { background: 'var(--primary)', color: '#fff', borderColor: 'var(--primary)' }
const sizeOptions = [5, 10, 20, 50]
const size = ref(5)
const taskPage = ref(0)
const taskSize = ref(10)

const BADGE = {
  online: { label: '在线', color: 'var(--success)', bg: '#dcfce7' },
  recently_lost: { label: '刚失联', color: '#ea5a0c', bg: '#fef3c7' },
  offline: { label: '离线', color: 'var(--text-tertiary)', bg: '#eef0f3' },
  about_to_gc: { label: '待回收', color: 'var(--danger)', bg: '#fee2e2' },
  revoked: { label: '已吊销', color: 'var(--text-tertiary)', bg: '#eef0f3' }
}
const SCOPE_TEXT = { global: '全局', repo: '仓库', user: '个人' }
function scopeText(s) { return SCOPE_TEXT[s] || s || '—' }
function healthText(h) { return (BADGE[h] || { label: h }).label }
function healthClass(h) { return { online: 'success', recently_lost: 'warning', offline: 'default', about_to_gc: 'danger', revoked: 'default' }[h] || 'default' }
function badgeStyle(h) {
  const b = BADGE[h] || { color: 'var(--text-tertiary)', bg: '#eef0f3' }
  return { display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '12px', fontWeight: 600, color: b.color, background: b.bg, padding: '3px 10px', borderRadius: '999px', whiteSpace: 'nowrap' }
}
function dotStyle(h) {
  const b = BADGE[h] || { color: 'var(--text-tertiary)' }
  return { width: '7px', height: '7px', borderRadius: '50%', background: b.color, display: 'inline-block' }
}

async function load() {
  const { data } = await http.get('/devices', { params: { scope: scope.value, status: status.value, name: name.value, page: page.value, size: size.value } })
  devices.value = data.content
  total.value = data.totalElements
  totalPages.value = data.totalPages
}
function search() { page.value = 0; load() }
function reset() { scope.value = ''; status.value = ''; name.value = ''; page.value = 0; load() }

async function revoke(d) {
  if (!confirm(`确认吊销设备 ${d.deviceName || d.name} 的凭据？其 worker 将立即失效（401）`)) return
  await http.post(`/devices/${d.id}/revoke`)
  load()
}
async function recycle(d) {
  const word = prompt(`设备回收将：吊销凭据 → 删除引擎记录 → 清理终态任务。不可逆！\n输入设备名 ${d.deviceName || d.name} 确认：`)
  if (word !== (d.deviceName || d.name)) { alert('确认词不匹配，已取消'); return }
  try {
    await http.post(`/devices/${d.id}/cleanup`)
    load()
  } catch (e) { alert(e.response?.data?.message || '回收失败') }
}
async function openDetail(d) {
  const isNewDevice = d !== detail.value
  if (isNewDevice) {
    selectedEngineId.value = null
    taskPage.value = 0
  }
  const params = { taskPage: taskPage.value, taskSize: taskSize.value }
  if (selectedEngineId.value) params.engineId = selectedEngineId.value
  const { data } = await http.get(`/devices/${d.id}`, { params })
  detail.value = data
}
function onEngineChange() {
  taskPage.value = 0
  openDetail(detail.value)
}
onMounted(load)
</script>
