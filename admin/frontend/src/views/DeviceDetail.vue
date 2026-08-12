<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <span class="back-btn" @click="goBack">
        <svg class="back-arrow" viewBox="0 0 24 24" width="16" height="16"><path d="M15 18l-6-6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        返回
      </span>
      <h1>设备详情</h1>
      <span class="badge" :style="badgeStyle(detail?.health)">{{ healthText(detail?.health) }}</span>
      <span class="t3" style="font-size:12px">{{ detail?.deviceName || detail?.name || '' }}</span>
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
      <div class="card" style="margin-top:16px;padding:16px 20px">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap">
          <h5 style="margin:0">关联任务（{{ detail.tasks?.totalElements ?? 0 }} 条）<span style="font-size:13px;color:#8896a7;font-weight:400" v-if="filterCount">（已筛选 {{ filterCount }} 条）</span></h5>
          <select v-model="selectedEngineId" class="page-size-select" style="max-width:260px;margin-left:auto" @change="onEngineChange">
            <option :value="null">全部引擎</option>
            <option v-for="e in detail.engines || []" :key="e.id" :value="e.id">{{ e.name }}</option>
          </select>
          <button v-if="hasAnyFilter" class="btn btn-sm" style="color:#e5484d;border-color:#f3b4b6" @click="clearFilters">清除筛选</button>
        </div>

        <!-- 任务状态统计卡（对齐 VOD stat-cards） -->
        <div class="task-stat-cards">
          <div class="task-stat-card">
            <div class="task-stat-num primary">{{ taskTotal }}</div>
            <div class="task-stat-label">任务总数</div>
          </div>
          <div v-for="s in taskStatList" :key="s.key" class="task-stat-card">
            <div class="task-stat-num" :style="{ color: s.color }">{{ s.count }}</div>
            <div class="task-stat-label">{{ s.label }}</div>
          </div>
        </div>

        <!-- 任务状态分布图（对齐 VOD chart-row） -->
        <div class="task-chart-row">
          <div class="card chart-card"><h5 style="margin-bottom:8px">任务状态分布</h5><div ref="taskChartEl" style="height:220px"></div></div>
        </div>

        <div class="table-loading-wrapper">
          <div class="table-loading-overlay" v-if="taskLoading">
            <span class="table-loading-text">加载中...</span>
          </div>
          <div style="overflow-x:auto">
          <table class="vod-table">
            <thead><tr>
              <th style="width:70px">ID</th>
              <th class="filterable-th">
                <span class="th-content" @click="toggleFilter('agentType')">
                  类型 <span class="filter-arrow" :class="{ active: taskType }">▼</span>
                </span>
                <div class="col-filter-drop" v-if="filterDropdown === 'agentType'" @click.stop>
                  <div class="col-filter-opt" :class="{ active: !taskType }" @click="setFilter('agentType', '')">全部</div>
                  <div class="col-filter-opt" v-for="(cnt, v) in filterOptions('agentType')" :key="v" :class="{ active: taskType === v }" @click="setFilter('agentType', v)">{{ v || '(空)' }} ({{ cnt }})</div>
                </div>
              </th>
              <th class="filterable-th">
                <span class="th-content" @click="toggleFilter('skill')">
                  技能 <span class="filter-arrow" :class="{ active: taskSkill }">▼</span>
                </span>
                <div class="col-filter-drop" v-if="filterDropdown === 'skill'" @click.stop>
                  <div class="col-filter-opt" :class="{ active: !taskSkill }" @click="setFilter('skill', '')">全部</div>
                  <div class="col-filter-opt" v-for="(cnt, v) in filterOptions('skill')" :key="v" :class="{ active: taskSkill === v }" @click="setFilter('skill', v)">{{ v || '(空)' }} ({{ cnt }})</div>
                </div>
              </th>
              <th style="width:90px" class="filterable-th">
                <span class="th-content" @click="toggleFilter('status')">
                  状态 <span class="filter-arrow" :class="{ active: taskStatus }">▼</span>
                </span>
                <div class="col-filter-drop" v-if="filterDropdown === 'status'" @click.stop>
                  <div class="col-filter-opt" :class="{ active: !taskStatus }" @click="setFilter('status', '')">全部</div>
                  <div class="col-filter-opt" v-for="(cnt, v) in filterOptions('status')" :key="v" :class="{ active: taskStatus === v }" @click="setFilter('status', v)">{{ v || '(空)' }} ({{ cnt }})</div>
                </div>
              </th>
              <th style="width:150px" class="sortable-th" @click="toggleSort">
                创建时间
                <span class="sort-arrows">
                  <span class="sort-dir" :class="{ active: sortOrder === 'asc' }">▲</span>
                  <span class="sort-dir" :class="{ active: sortOrder === 'desc' }">▼</span>
                </span>
              </th>
            </tr></thead>
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
import { computed, nextTick, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as echarts from 'echarts'
import http from '../api'

const route = useRoute()
const router = useRouter()
// 智能返回：历史可回则回退，否则回设备列表（防直接访问 URL 时 back 跳出系统）
function goBack() {
  if (window.history.length > 1) router.back()
  else router.push('/runtime/devices')
}
const detail = ref(null)
const selectedEngineId = ref(null)
const taskPage = ref(0)
const taskSize = ref(10)
const sizeOptions = [10, 20, 50]
// 列头筛选/排序状态（对齐 VOD 分析结果报表）
const taskStatus = ref('')
const taskSkill = ref('')
const taskType = ref('')
const sortOrder = ref('desc')
const filterDropdown = ref('')
const filterCount = ref(0)
const hasAnyFilter = computed(() => !!(taskStatus.value || taskSkill.value || taskType.value))
// 加载遮罩 + 统计卡 + 图表
const taskLoading = ref(false)
const taskChartEl = ref(null)
let taskChart = null
// 任务状态展示映射（未知状态原样显示）
const STATUS_META = {
  completed: { label: '完成', color: 'var(--success)' },
  success: { label: '完成', color: 'var(--success)' },
  failed: { label: '失败', color: '#e5484d' },
  running: { label: '运行中', color: '#1890ff' },
  pending: { label: '等待', color: '#ea5a0c' }
}
const taskTotal = computed(() => {
  const st = detail.value?.tasks?.filterOptions?.status || {}
  return Object.values(st).reduce((s, n) => s + n, 0)
})
const taskStatList = computed(() => {
  const st = detail.value?.tasks?.filterOptions?.status || {}
  return Object.entries(st).map(([key, count]) => {
    const meta = STATUS_META[key] || { label: key, color: 'var(--text-secondary)' }
    return { key, label: meta.label, color: meta.color, count }
  })
})

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
  taskLoading.value = true
  const params = { taskPage: taskPage.value, taskSize: taskSize.value, sortOrder: sortOrder.value }
  if (selectedEngineId.value) params.engineId = selectedEngineId.value
  if (taskStatus.value) params.taskStatus = taskStatus.value
  if (taskSkill.value) params.taskSkill = taskSkill.value
  if (taskType.value) params.taskType = taskType.value
  try {
    const { data } = await http.get(`/devices/${route.params.id}`, { params })
    detail.value = data
    // 「已筛选 N 条」：有列头筛选时显示过滤后总数，无筛选不显示（对齐 VOD）
    filterCount.value = (taskStatus.value || taskSkill.value || taskType.value) ? (data.tasks?.totalElements ?? 0) : 0
    nextTick(renderTaskChart)
  } finally {
    taskLoading.value = false
  }
}
// 任务状态分布饼图（对齐 VOD chart-row）
function renderTaskChart() {
  const el = taskChartEl.value
  if (!el) return
  const st = detail.value?.tasks?.filterOptions?.status || {}
  const data = Object.entries(st).map(([key, count]) => {
    const meta = STATUS_META[key] || { label: key }
    return { name: meta.label, value: count }
  })
  if (!data.length) return
  if (!taskChart) taskChart = echarts.init(el)
  taskChart.setOption({
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { fontSize: 12 } },
    series: [{
      type: 'pie',
      radius: ['40%', '68%'],
      center: ['50%', '44%'],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
      label: { show: true, formatter: '{b}: {c}' },
      data
    }],
    color: ['#52c41a', '#e5484d', '#1890ff', '#ea5a0c', '#8896a7', '#7222ed']
  })
}
function onEngineChange() {
  taskPage.value = 0
  load()
}
// 列头筛选（对齐 VOD）：展开下拉/切换筛选/清空
function toggleFilter(col) {
  filterDropdown.value = filterDropdown.value === col ? '' : col
}
function setFilter(col, v) {
  if (col === 'status') taskStatus.value = v
  else if (col === 'skill') taskSkill.value = v
  else if (col === 'agentType') taskType.value = v
  filterDropdown.value = ''
  taskPage.value = 0
  load()
}
function clearFilters() {
  taskStatus.value = ''
  taskSkill.value = ''
  taskType.value = ''
  taskPage.value = 0
  load()
}
// 排序切换：desc → asc → desc
function toggleSort() {
  sortOrder.value = sortOrder.value === 'desc' ? 'asc' : 'desc'
  taskPage.value = 0
  load()
}
function filterOptions(col) {
  return detail.value?.tasks?.filterOptions?.[col] || {}
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

/* ===== 页头返回按钮（对齐 VOD 分析结果报表 back-btn） ===== */
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
.back-btn:active { transform: translateX(0); }
.back-arrow { flex-shrink: 0; }

/* ===== 任务状态统计卡（对齐 VOD stat-cards） ===== */
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
  background: #f8f9fb;
  border-radius: 10px;
  padding: 12px 16px;
  text-align: center;
}
.task-stat-num { font-size: 22px; font-weight: 700; line-height: 1.2; }
.task-stat-num.primary { color: var(--primary); }
.task-stat-label { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }

/* ===== 任务状态分布图（对齐 VOD chart-row） ===== */
.task-chart-row { margin-bottom: 14px; }
.task-chart-row .chart-card { padding: 14px 16px; }

/* ===== 表格加载遮罩（对齐 VOD table-loading-wrapper） ===== */
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

/* ===== 列头筛选/排序（对齐 VOD 分析结果报表） ===== */
.filterable-th, .sortable-th { position: relative; cursor: pointer; user-select: none; }
.th-content { display: inline-flex; align-items: center; gap: 4px; }
.filter-arrow { font-size: 9px; color: var(--text-tertiary); }
.filter-arrow.active { color: var(--primary); }
.sort-arrows { display: inline-flex; flex-direction: column; line-height: 1; margin-left: 3px; vertical-align: middle; }
.sort-dir { font-size: 8px; color: var(--text-tertiary); }
.sort-dir.active { color: var(--primary); }
.col-filter-drop {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  z-index: 50;
  min-width: 150px;
  max-height: 240px;
  overflow-y: auto;
  background: #fff;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.12);
  padding: 6px;
}
.col-filter-opt {
  padding: 6px 10px;
  font-size: 12px;
  color: var(--text-secondary);
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
}
.col-filter-opt:hover { background: #f5f7fa; }
.col-filter-opt.active { background: var(--primary-bg); color: var(--primary); font-weight: 500; }
</style>
