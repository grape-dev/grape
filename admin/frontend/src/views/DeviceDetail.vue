<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>设备详情</h1>
      <span class="badge" :style="badgeStyle(detail?.health)">{{ healthText(detail?.health) }}</span>
      <span class="t3" style="font-size:12px">{{ detail?.deviceName || detail?.name || '' }}</span>
      <button class="btn btn-sm" style="margin-left:auto" @click="$router.back()">← 返回列表</button>
    </div>

    <div class="dashboard" style="padding:20px 28px" v-if="detail">
      <!-- 设备信息 -->
      <div class="card">
        <div class="detail-grid">
          <div class="detail-item"><span class="t3">状态</span>{{ detail.status }}</div>
          <div class="detail-item"><span class="t3">范围</span>{{ scopeText(detail.scope) }}</div>
          <div class="detail-item"><span class="t3">归属</span>{{ detail.ownerName || '—' }}</div>
          <div class="detail-item"><span class="t3">最后心跳</span>{{ formatTime(detail.lastSeenAt) }}</div>
          <div class="detail-item"><span class="t3">IP</span>{{ detail.lastIp || '—' }}</div>
          <div class="detail-item"><span class="t3">创建时间</span>{{ formatTime(detail.createdAt) }}</div>
          <div class="detail-item"><span class="t3">引擎数</span>{{ detail.engines?.length ?? 0 }}</div>
          <div class="detail-item"><span class="t3">任务数</span>{{ detail.tasks?.totalElements ?? 0 }}</div>
        </div>
      </div>

      <!-- 引擎列表 -->
      <div class="card" style="margin-top:16px">
        <h5 style="margin:0 0 10px">关联引擎（{{ detail.engines?.length ?? 0 }}）</h5>
        <table class="vod-table" v-if="detail.engines?.length">
          <thead>
            <tr><th>实例名</th><th>类型</th><th>版本</th><th>二进制</th><th>健康</th><th>状态</th><th>CLI 路径</th><th>最后心跳</th></tr>
          </thead>
          <tbody>
            <tr v-for="e in detail.engines" :key="e.id">
              <td>{{ e.name }}</td>
              <td>{{ e.name }}</td>
              <td class="t3" style="max-width:200px">{{ e.version || '-' }}</td>
              <td class="t3">{{ e.binary || '-' }}</td>
              <td><span :class="['status-tag', healthClass(e.health)]">{{ healthText(e.health) }}</span></td>
              <td class="t2">{{ e.status }}</td>
              <td class="t3" style="max-width:180px">{{ e.cliPath || '-' }}</td>
              <td class="t3">{{ formatTime(e.lastSeenAt) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="t2" style="padding:12px;text-align:center">该设备暂无关联引擎</p>
      </div>

      <!-- 关联任务 -->
      <div class="card" style="margin-top:16px">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
          <h5 style="margin:0">关联任务（{{ detail.tasks?.totalElements ?? 0 }} 条）</h5>
          <select v-model="selectedEngineId" class="page-size-select" style="max-width:260px;margin-left:auto" @change="onEngineChange">
            <option :value="null">全部引擎</option>
            <option v-for="e in detail.engines || []" :key="e.id" :value="e.id">{{ e.name }}</option>
          </select>
        </div>
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
          <select v-model="taskSize" class="page-size-select" @change="taskPage = 0; load()">
            <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}条/页</option>
          </select>
          <button class="page-btn" :disabled="taskPage <= 0" @click="taskPage--; load()">上一页</button>
          <button class="page-btn" :disabled="taskPage >= (detail.tasks?.totalPages ?? 1) - 1" @click="taskPage++; load()">下一页</button>
        </div>
      </div>
    </div>

    <p v-else class="t2" style="padding:40px;text-align:center">加载中…</p>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import http from '../api'

const route = useRoute()
const detail = ref(null)
const selectedEngineId = ref(null)
const taskPage = ref(0)
const taskSize = ref(10)
const sizeOptions = [10, 20, 50]

const BADGE = {
  online: { color: 'var(--success)', bg: '#dcfce7', label: '在线' },
  recently_lost: { color: '#ea5a0c', bg: '#fef3c7', label: '刚失联' },
  offline: { color: 'var(--text-tertiary)', bg: '#eef0f3', label: '离线' },
  about_to_gc: { color: 'var(--danger)', bg: '#fee2e2', label: '待回收' },
  revoked: { color: 'var(--text-tertiary)', bg: '#eef0f3', label: '已吊销' }
}
const SCOPE_TEXT = { global: '全局', repo: '仓库', user: '个人' }
function scopeText(s) { return SCOPE_TEXT[s] || s || '—' }
function healthText(h) { return (BADGE[h] || { label: h }).label }
function healthClass(h) { return { online: 'success', recently_lost: 'warning', offline: 'default', about_to_gc: 'danger', revoked: 'default' }[h] || 'default' }
function badgeStyle(h) {
  const b = BADGE[h] || { color: 'var(--text-tertiary)', bg: '#eef0f3' }
  return { display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '11px', fontWeight: 500, color: b.color, background: b.bg, padding: '2px 10px', borderRadius: '10px', whiteSpace: 'nowrap' }
}
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
  const params = { taskPage: taskPage.value, taskSize: taskSize.value }
  if (selectedEngineId.value) params.engineId = selectedEngineId.value
  const { data } = await http.get(`/devices/${route.params.id}`, { params })
  detail.value = data
}
function onEngineChange() {
  taskPage.value = 0
  load()
}
load()
</script>

<style scoped>
.detail-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px 24px;
}
.detail-item {
  font-size: 13px;
  color: var(--text-primary);
}
.detail-item .t3 { display: block; font-size: 11px; margin-bottom: 2px; }
@media (max-width: 900px) {
  .detail-grid { grid-template-columns: repeat(2, 1fr); }
}
</style>
