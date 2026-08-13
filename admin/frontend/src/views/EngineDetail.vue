<template>
  <div class="page">
    <div class="page-header">
      <button class="back-btn" @click="goBack"><span class="back-arrow">←</span> 返回引擎列表</button>
      <div style="display:flex;align-items:center;gap:10px">
        <span class="badge" :style="badgeStyle(detail?.health)">{{ healthText(detail?.health) }}</span>
        <span style="font-weight:600;font-size:16px">{{ detail?.displayName || detail?.name || '' }}</span>
        <span class="t3" style="font-size:12px">{{ detail?.device || '—' }}</span>
      </div>
    </div>

    <div class="dashboard" style="padding:20px 28px" v-if="detail">
      <div class="detail-grid">
        <div class="detail-item"><span class="t3">类型</span>{{ detail.name || '—' }}</div>
        <div class="detail-item"><span class="t3">显示名</span>{{ detail.displayName || '—' }}</div>
        <div class="detail-item"><span class="t3">版本</span>{{ detail.version || '—' }}</div>
        <div class="detail-item"><span class="t3">提供方</span>{{ detail.provider || '—' }}</div>
        <div class="detail-item"><span class="t3">二进制</span><span class="ellipsis-cell" :title="detail.binary || '—'">{{ detail.binary || '—' }}</span></div>
        <div class="detail-item"><span class="t3">CLI 路径</span><span class="ellipsis-cell" :title="detail.cliPath || '—'">{{ detail.cliPath || '—' }}</span></div>
        <div class="detail-item"><span class="t3">Pip 包</span>{{ detail.pipPackage || '—' }}</div>
        <div class="detail-item"><span class="t3">安装状态</span>{{ installedText(detail.installed) }}</div>
        <div class="detail-item"><span class="t3">引擎状态</span>{{ detail.status || '—' }}</div>
        <div class="detail-item"><span class="t3">健康标志</span>{{ healthyText(detail.healthy) }}</div>
        <div class="detail-item"><span class="t3">最后心跳</span>{{ formatTime(detail.lastSeenAt) }}</div>
        <div class="detail-item"><span class="t3">范围</span>{{ scopeText(detail.scope) }}</div>
        <div class="detail-item"><span class="t3">归属</span>{{ detail.ownerName || '—' }}</div>
        <div class="detail-item"><span class="t3">设备状态</span>{{ deviceStatusText(detail.deviceStatus) }}</div>
        <div class="detail-item"><span class="t3">凭据 ID</span>{{ detail.credentialId ?? '—' }}</div>
        <div class="detail-item"><span class="t3">更新时间</span>{{ formatTime(detail.updatedAt) }}</div>
      </div>

      <!-- 关联任务 -->
      <div class="card" style="margin-top:16px;padding:0">
        <div class="section-header">
          <h5 class="sh-title">关联任务 <span class="sh-count">共 {{ detail.tasks?.totalElements ?? 0 }} 条<span v-if="hasAnyFilter">（已筛选 {{ filterCount }} 条）</span></span></h5>
          <div class="sh-actions">
            <select v-model="taskStatus" class="page-size-select" style="max-width:130px" @change="applyFilters">
              <option value="">全部状态</option>
              <option v-for="(cnt, v) in filterOptions('status')" :key="v" :value="v">{{ statusText(v) }} ({{ cnt }})</option>
            </select>
            <select v-model="taskType" class="page-size-select" style="max-width:130px" @change="applyFilters">
              <option value="">全部类型</option>
              <option v-for="(cnt, v) in filterOptions('agentType')" :key="v" :value="v">{{ v || '(空)' }} ({{ cnt }})</option>
            </select>
            <button v-if="hasAnyFilter" class="btn btn-sm" style="color:#e5484d;border-color:#f3b4b6" @click="clearFilters">清除筛选</button>
          </div>
        </div>
        <div style="padding:14px 20px">
          <div class="task-stat-cards">
            <div class="task-stat-card kpi-primary">
              <div class="task-stat-num primary">{{ taskTotal }}</div>
              <div class="task-stat-label">任务总数</div>
            </div>
            <div v-for="s in taskStatList" :key="s.key" class="task-stat-card" :style="{ borderLeftColor: s.color }">
              <div class="task-stat-num" :style="{ color: s.color }">{{ s.count }}</div>
              <div class="task-stat-label">{{ s.label }}</div>
            </div>
          </div>
          <div class="table-loading-wrapper">
            <div class="table-loading-overlay" v-if="taskLoading"><span class="table-loading-text">加载中...</span></div>
            <div style="overflow-x:auto">
              <table class="vod-table">
                <thead><tr>
                  <th style="width:80px">ID</th>
                  <th style="width:110px">类型</th>
                  <th>技能</th>
                  <th style="width:90px">状态</th>
                  <th>仓库</th>
                  <th style="width:170px">创建时间</th>
                </tr></thead>
                <tbody>
                  <tr v-for="t in tasks" :key="t.id">
                    <td class="t3">{{ t.id }}</td>
                    <td>{{ t.agentType || '—' }}</td>
                    <td class="t3 ellipsis-cell" :title="t.skill || ''">{{ t.skill || '—' }}</td>
                    <td><span :class="['status-tag', taskStatusClass(t.status)]">{{ statusText(t.status) }}</span></td>
                    <td class="t3 ellipsis-cell" :title="t.repoKey || ''">{{ t.repoKey || '—' }}</td>
                    <td class="t3">{{ formatTime(t.createdAt) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
          <p v-if="!tasks.length" class="t2" style="padding:16px;text-align:center">暂无关联任务</p>
          <PaginationBar v-if="totalElements > 0" v-model:page="taskPage" v-model:size="taskSize" :total="totalElements" :total-pages="totalPages" :size-options="[10, 20, 50]" @change="loadTasks" />
        </div>
      </div>

      <!-- 关联会话 -->
      <div class="card" style="margin-top:16px;padding:0">
        <div class="section-header">
          <h5 class="sh-title">关联会话 <span class="sh-count">共 {{ sessions.length }} 条</span></h5>
        </div>
        <div style="padding:14px 20px">
          <table class="vod-table">
            <thead><tr>
              <th style="width:80px">来源</th>
              <th style="width:150px">ID</th>
              <th>仓库</th>
              <th style="width:110px">类型</th>
              <th style="width:90px">状态</th>
              <th style="width:70px">启用</th>
              <th style="width:170px">最后活跃</th>
            </tr></thead>
            <tbody>
              <tr v-for="s in sessions" :key="s.src + s.id">
                <td><span class="status-tag" :class="s.src === 'devops' ? 'info' : 'warning'">{{ s.src === 'devops' ? '仓库' : 'Issue' }}</span></td>
                <td class="t3">{{ s.id }}</td>
                <td class="t3 ellipsis-cell" :title="s.repoKey || ''">{{ s.repoKey || '—' }}</td>
                <td>{{ s.agentType || '—' }}</td>
                <td>{{ s.status || '—' }}</td>
                <td>{{ s.enable ? '是' : '否' }}</td>
                <td class="t3">{{ formatTime(s.lastActiveAt) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="!sessions.length" class="t2" style="padding:16px;text-align:center">暂无关联会话</p>
        </div>
      </div>
    </div>
    <p v-else class="t2" style="padding:24px;text-align:center">加载中...</p>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import http from '../api'
import PaginationBar from '../components/PaginationBar.vue'

const route = useRoute()
const router = useRouter()
const engineId = route.params.id

const detail = ref(null)
const tasks = ref([])
const sessions = ref([])
const taskPage = ref(0)
const taskSize = ref(10)
const totalElements = ref(0)
const totalPages = ref(0)
const taskStatus = ref('')
const taskType = ref('')
const taskLoading = ref(false)

const SCOPE_TEXT = { global: '全局', repo: '仓库', user: '个人' }
const STATUS_TEXT = {
  queued: '排队中', dispatched: '已派发', running: '执行中',
  completed: '已完成', failed: '失败', cancelled: '已取消', unknown: '未知',
}
const STATUS_COLOR = {
  queued: '#8c8c8c', dispatched: '#1890ff', running: '#006eff',
  completed: '#52c41a', failed: '#e5484d', cancelled: '#fa8c16',
}
const HEALTH_BADGE = {
  online: { label: '在线', color: '#52c41a', bg: 'rgba(82,196,26,0.1)' },
  recently_lost: { label: '刚失联', color: '#fa8c16', bg: 'rgba(250,140,22,0.1)' },
  offline: { label: '离线', color: '#8c8c8c', bg: 'rgba(140,140,140,0.1)' },
  about_to_gc: { label: '待回收', color: '#e5484d', bg: 'rgba(229,72,77,0.1)' },
}

const hasAnyFilter = computed(() => !!(taskStatus.value || taskType.value))
const filterCount = computed(() => {
  let n = 0
  if (taskStatus.value) n++
  if (taskType.value) n++
  return n
})
const taskTotal = computed(() => {
  const st = detail.value?.tasks?.filterOptions?.status || {}
  return Object.values(st).reduce((a, b) => a + b, 0)
})
const taskStatList = computed(() => {
  const st = detail.value?.tasks?.filterOptions?.status || {}
  const keys = ['queued', 'dispatched', 'running', 'completed', 'failed', 'cancelled']
  const list = []
  for (const k of keys) {
    if (st[k]) list.push({ key: k, label: STATUS_TEXT[k] || k, count: st[k], color: STATUS_COLOR[k] || '#8c8c8c' })
  }
  const others = Object.entries(st).filter(([k]) => !keys.includes(k))
  for (const [k, c] of others) list.push({ key: k, label: STATUS_TEXT[k] || k, count: c, color: '#8c8c8c' })
  return list
})

function filterOptions(key) {
  return detail.value?.tasks?.filterOptions?.[key] || {}
}
function applyFilters() { taskPage.value = 0; loadTasks() }
function clearFilters() { taskStatus.value = ''; taskType.value = ''; taskPage.value = 0; loadTasks() }

async function loadDetail() {
  const { data } = await http.get(`/engines/${engineId}`)
  detail.value = data
  sessions.value = data.sessions || []
  tasks.value = data.tasks?.content || []
  totalElements.value = data.tasks?.totalElements || 0
  totalPages.value = data.tasks?.totalPages || 0
}
async function loadTasks() {
  taskLoading.value = true
  try {
    const { data } = await http.get(`/engines/${engineId}`, {
      params: { taskPage: taskPage.value, taskSize: taskSize.value, taskStatus: taskStatus.value, taskType: taskType.value },
    })
    detail.value = data
    tasks.value = data.tasks?.content || []
    totalElements.value = data.tasks?.totalElements || 0
    totalPages.value = data.tasks?.totalPages || 0
  } finally {
    taskLoading.value = false
  }
}
function goBack() { router.back() }

function statusText(s) { return STATUS_TEXT[s] || s || '—' }
function taskStatusClass(s) {
  return { completed: 'success', failed: 'danger', running: 'info', queued: 'default', dispatched: 'info', cancelled: 'warning' }[s] || 'default'
}
function scopeText(s) { return SCOPE_TEXT[s] || s || '—' }
function healthText(h) { return (HEALTH_BADGE[h] || { label: h }).label }
function badgeStyle(h) {
  const b = HEALTH_BADGE[h] || { color: '#8c8c8c', bg: 'rgba(140,140,140,0.1)' }
  return { color: b.color, background: b.bg, border: `1px solid ${b.color}33` }
}
function installedText(v) { return v == null ? '—' : v === 1 ? '已安装' : '未安装' }
function healthyText(v) { return v == null ? '—' : v === 1 ? '正常' : '异常' }
function deviceStatusText(s) { return s === 'active' ? '有效' : s === 'revoked' ? '已吊销' : (s || '—') }
function formatTime(v) { return v ? String(v).replace('T', ' ').slice(0, 19) : '—' }

onMounted(loadDetail)
</script>

<style scoped>
.detail-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}
.detail-item {
  background: #fff;
  border: 1px solid var(--border-color);
  border-left: 3px solid var(--border-color);
  border-radius: 10px;
  padding: 10px 14px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  transition: all 0.2s ease;
  overflow: hidden;
}
.detail-item:hover { box-shadow: var(--card-shadow-hover); transform: translateY(-1px); }
.detail-item .t3 { display: block; font-size: 11px; font-weight: 400; color: var(--text-tertiary); margin-bottom: 3px; }
@media (max-width: 900px) {
  .detail-grid { grid-template-columns: repeat(2, 1fr); }
}
.back-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 14px;
  font-weight: 500;
  color: var(--text-secondary);
  padding: 6px 14px;
  border-radius: 8px;
  background: #f5f5f5;
  border: 1px solid var(--border-color);
  cursor: pointer;
  user-select: none;
  transition: all 0.2s ease;
  line-height: 1;
}
.back-btn:hover { color: var(--primary); background: #e6f4ff; border-color: #91caff; transform: translateX(-2px); }
.back-arrow { flex-shrink: 0; }
.task-stat-cards {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.task-stat-card {
  flex: 1;
  min-width: 100px;
  max-width: 160px;
  background: #fff;
  border: 1px solid var(--border-color);
  border-radius: 10px;
  border-left: 3px solid #006eff;
  padding: 12px 16px;
  text-align: center;
  transition: all 0.2s ease;
}
.task-stat-card:hover { box-shadow: var(--card-shadow-hover); transform: translateY(-1px); }
.task-stat-card.kpi-primary { border-left-color: #006eff; }
.task-stat-num { font-size: 20px; font-weight: 700; line-height: 1.2; }
.task-stat-num.primary { color: var(--primary); }
.task-stat-label { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
.table-loading-wrapper { position: relative; }
.table-loading-overlay {
  position: absolute;
  inset: 0;
  z-index: 30;
  background: rgba(255, 255, 255, 0.72);
  display: flex;
  align-items: center;
  justify-content: center;
}
.table-loading-text {
  font-size: 13px;
  color: var(--text-secondary);
  background: #fff;
  padding: 6px 14px;
  border-radius: 16px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}
.ellipsis-cell {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  display: inline-block;
  max-width: 100%;
  vertical-align: bottom;
}
</style>
