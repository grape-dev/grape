<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <span class="back-btn" @click="goBack">
        <svg class="back-arrow" viewBox="0 0 24 24" width="16" height="16"><path d="M15 18l-6-6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        返回
      </span>
      <h1>引擎详情</h1>
      <span class="badge" :style="badgeStyle(detail?.health)">{{ healthText(detail?.health) }}</span>
      <span class="t3" style="font-size:12px">{{ detail?.displayName || detail?.name || '' }} · {{ detail?.device || '' }}</span>
    </div>

    <div class="dashboard" style="padding:20px 28px" v-if="detail">
      <!-- 引擎信息 -->
      <div class="card" style="padding:16px 20px">
        <div class="detail-grid">
          <div class="detail-item" :style="{ borderLeftColor: detail.health === 'online' ? '#52c41a' : 'var(--text-tertiary)' }">
            <span class="t3">状态</span>
            <span :class="['status-tag', healthClass(detail.health)]">{{ healthText(detail.health) }}</span>
          </div>
          <div class="detail-item"><span class="t3">类型</span>{{ detail.name || '—' }}</div>
          <div class="detail-item"><span class="t3">显示名</span>{{ detail.displayName || '—' }}</div>
          <div class="detail-item"><span class="t3">版本</span>{{ detail.version || '—' }}</div>
          <div class="detail-item"><span class="t3">提供方</span>{{ detail.provider || '—' }}</div>
          <div class="detail-item"><span class="t3">二进制</span><span class="ellipsis-cell" :title="detail.binary || '—'">{{ detail.binary || '—' }}</span></div>
          <div class="detail-item"><span class="t3">CLI 路径</span><span class="ellipsis-cell" :title="detail.cliPath || '—'">{{ detail.cliPath || '—' }}</span></div>
          <div class="detail-item"><span class="t3">Pip 包</span>{{ detail.pipPackage || '—' }}</div>
          <div class="detail-item"><span class="t3">安装状态</span>{{ installedText(detail.installed) }}</div>
          <div class="detail-item"><span class="t3">健康标志</span>{{ healthyText(detail.healthy) }}</div>
          <div class="detail-item" style="border-left-color:#006eff"><span class="t3">最后心跳</span>{{ formatTime(detail.lastSeenAt) }}</div>
          <div class="detail-item"><span class="t3">范围</span>{{ scopeText(detail.scope) }}</div>
          <div class="detail-item"><span class="t3">归属</span>{{ detail.ownerName || '—' }}</div>
          <div class="detail-item"><span class="t3">设备状态</span>{{ deviceStatusText(detail.deviceStatus) }}</div>
          <div class="detail-item"><span class="t3">凭据 ID</span>{{ detail.credentialId ?? '—' }}</div>
          <div class="detail-item"><span class="t3">更新时间</span>{{ formatTime(detail.updatedAt) }}</div>
        </div>
      </div>

      <!-- 关联智能体 -->
      <div class="card" style="margin-top:16px;padding:0">
        <div class="section-header">
          <h5 class="sh-title">关联智能体 <span class="sh-count">共 {{ agents.length }} 个</span></h5>
        </div>
        <div style="padding:14px 20px">
          <div class="agent-grid" v-if="agents.length">
            <div v-for="(a, i) in agents" :key="a.agentType" class="agent-card" :class="accents[i % accents.length]" @click="$router.push(`/agent/manage/${a.agentType}`)">
              <div class="agent-head">
                <div class="agent-icon" :style="iconStyle(i)">{{ a.icon || '🤖' }}</div>
                <div class="agent-meta">
                  <div class="agent-name">{{ a.displayName }}<span class="code-tag">{{ a.agentType }}</span></div>
                  <div class="agent-sub">顺序 #{{ a.orderNum }} · 引擎 {{ a.defaultRuntimeId || '-' }}</div>
                </div>
                <label class="sw" :title="a.enable === 1 ? '点击禁用' : '点击启用'" @click.stop>
                  <input type="checkbox" :checked="a.enable === 1" :disabled="saving === a.agentType" @change="toggle(a, $event.target.checked)" />
                  <span class="sw-slider"></span>
                </label>
              </div>
              <div class="agent-desc">{{ a.description }}</div>
              <div class="agent-stats">
                <div class="as-item"><strong>{{ a.sessionCount || 0 }}</strong><span>会话</span></div>
                <div class="as-item"><strong>{{ a.repoUsageCount || 0 }}</strong><span>使用仓库</span></div>
                <div class="as-item">
                  <strong :style="{ color: loadColor(a) }">{{ fmtLoad(a) }}<span class="load-unit" v-if="a.loadRate != null">%</span></strong>
                  <span>负载率<template v-if="a.loadRate != null">（预警 {{ a.alertThresholdPct }}%）</template></span>
                </div>
                <div class="as-item"><strong>{{ a.taskCount || 0 }}</strong><span>任务</span></div>
              </div>
              <div class="sb-row" v-if="hasBreakdown(a)">
                <span v-for="(n, st) in a.statusBreakdown" :key="st" class="sb-seg" :style="{ background: STATUS_COLOR[st] || '#8c8c8c' }">{{ st }} {{ n }}</span>
              </div>
              <div class="agent-foot">
                <span class="cat-tag cat-official">{{ a.category === 'official' ? '官方' : '自定义' }}</span>
                <span class="detail-link">查看详情 →</span>
              </div>
            </div>
          </div>
          <p v-else class="t2" style="padding:12px;text-align:center">暂无关联智能体</p>
        </div>
      </div>

      <!-- 关联任务 -->
      <div class="card" style="margin-top:16px;padding:0">
        <div class="section-header">
          <h5 class="sh-title">关联任务 <span class="sh-count">共 {{ detail.tasks?.totalElements ?? 0 }} 条<span v-if="filterCount">（已筛选 {{ filterCount }} 条）</span></span></h5>
          <div class="sh-actions">
            <button v-if="hasAnyFilter" class="btn btn-sm" style="color:#e5484d;border-color:#f3b4b6" @click="clearFilters">清除筛选</button>
          </div>
        </div>
        <div style="padding:14px 20px">

        <!-- 任务状态统计卡（kpi 色带卡片，对齐 AgentManage/Overview 体系） -->
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

        <!-- 任务状态分布图（对齐 VOD chart-row） -->
        <div class="task-chart-row">
          <div class="card chart-card">
            <div class="section-header"><h5 class="sh-title">任务状态分布</h5></div>
            <div ref="taskChartEl" style="height:220px;padding:14px 20px"></div>
          </div>
        </div>

        <div class="table-loading-wrapper">
          <div class="table-loading-overlay" v-if="taskLoading">
            <span class="table-loading-text">加载中...</span>
          </div>
          <div style="overflow-x:auto">
          <table class="vod-table">
            <thead><tr>
              <th style="width:70px">ID</th>
              <th style="width:110px" class="filterable-th">
                <span class="th-content" @click="toggleFilter('agentType')">
                  类型 <span class="filter-arrow" :class="{ active: taskType }">▼</span>
                </span>
                <div class="col-filter-drop" v-if="filterDropdown === 'agentType'" @click.stop>
                  <div class="col-filter-opt" :class="{ active: !taskType }" @click="setFilter('agentType', '')">全部</div>
                  <div class="col-filter-opt" v-for="(cnt, v) in filterOptions('agentType')" :key="v" :class="{ active: taskType === v }" @click="setFilter('agentType', v)">{{ v || '(空)' }} ({{ cnt }})</div>
                </div>
              </th>
              <th style="width:180px" class="filterable-th">
                <span class="th-content" @click="toggleFilter('skill')">
                  技能 <span class="filter-arrow" :class="{ active: taskSkill }">▼</span>
                </span>
                <div class="col-filter-drop" v-if="filterDropdown === 'skill'" @click.stop>
                  <div class="col-filter-opt" :class="{ active: !taskSkill }" @click="setFilter('skill', '')">全部</div>
                  <div class="col-filter-opt" v-for="(cnt, v) in filterOptions('skill')" :key="v" :class="{ active: taskSkill === v }" @click="setFilter('skill', v)">{{ v || '(空)' }} ({{ cnt }})</div>
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
              <tr v-if="!(detail.tasks?.content || []).length" style="text-align:center">
                <td :colspan="4" class="t2" style="padding:28px 0">
                  <template v-if="(detail.tasks?.totalElements ?? 0) === 0">暂无关联任务</template>
                  <template v-else>
                    无匹配任务
                    <button v-if="hasAnyFilter" class="btn btn-sm" style="color:#e5484d;border-color:#f3b4b6;margin-left:10px" @click="clearFilters">清除筛选</button>
                  </template>
                </td>
              </tr>
              <tr v-for="t in detail.tasks?.content || []" :key="t.id">
                <td class="t3">{{ t.id }}</td>
                <td class="ellipsis-cell" :title="t.agentType">{{ t.agentType }}</td>
                <td class="ellipsis-cell" :title="t.skill">{{ t.skill }}</td>
                <td class="t3" :title="t.createdAt">{{ formatTime(t.createdAt) }}</td>
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
// 智能返回：历史可回则回退，否则回引擎列表（防直接访问 URL 时 back 跳出系统）
function goBack() {
  if (window.history.length > 1) router.back()
  else router.push('/runtime/engines')
}
const detail = ref(null)
const agents = ref([])
const taskPage = ref(0)
const taskSize = ref(10)
const sizeOptions = [10, 20, 50]
// 列头筛选/排序状态（对齐 VOD 分析结果报表）
const taskSkill = ref('')
const taskType = ref('')
const sortOrder = ref('desc')
const filterDropdown = ref('')
const filterCount = ref(0)
const hasAnyFilter = computed(() => !!(taskSkill.value || taskType.value))
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
function installedText(v) { return v == null ? '—' : v === 1 ? '已安装' : '未安装' }
function healthyText(v) { return v == null ? '—' : v === 1 ? '正常' : '异常' }
function deviceStatusText(s) { return s === 'active' ? '有效' : s === 'revoked' ? '已吊销' : (s || '—') }

const STATUS_COLOR = { queued: '#fa8c16', running: '#006eff', completed: '#52c41a', failed: '#eb2f96' }
const accents = ['kpi-blue', 'kpi-purple', 'kpi-pink', 'kpi-orange', 'kpi-green']
const iconPalette = [
  { background: '#e8f0fe', color: '#006eff' },
  { background: '#f0e6ff', color: '#7222ed' },
  { background: '#fff0f6', color: '#eb2f96' },
  { background: '#fff7e6', color: '#fa8c16' },
  { background: '#f6ffed', color: '#52c41a' }
]
function iconStyle(i) { return iconPalette[i % iconPalette.length] }
function hasBreakdown(a) { return a.statusBreakdown && Object.keys(a.statusBreakdown).length }
function fmtLoad(a) {
  if (a.loadRate == null) return '—'
  const v = Number(a.loadRate)
  return Number.isInteger(v) ? v : v.toFixed(1)
}
function loadColor(a) {
  if (a.loadRate == null) return 'var(--text-primary)'
  return a.loadLevel === 'over' ? '#eb2f96' : '#52c41a'
}
const saving = ref('')
async function toggle(a, checked) {
  saving.value = a.agentType
  try {
    const { data } = await http.post(`/agents/${a.agentType}/enable`, { enable: checked })
    a.enable = data.enable
  } catch (e) {
    console.error('切换失败:', e)
    alert('切换失败，请重试')
  } finally {
    saving.value = ''
  }
}

async function load() {
  taskLoading.value = true
  const params = { taskPage: taskPage.value, taskSize: taskSize.value, sortOrder: sortOrder.value }
  if (taskSkill.value) params.taskSkill = taskSkill.value
  if (taskType.value) params.taskType = taskType.value
  try {
    const { data } = await http.get(`/engines/${route.params.id}`, { params })
    detail.value = data
    agents.value = data.agents || []
    // 「已筛选 N 条」：有列头筛选时显示过滤后总数，无筛选不显示（对齐 VOD）
    filterCount.value = (taskSkill.value || taskType.value) ? (data.tasks?.totalElements ?? 0) : 0
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
// 列头筛选（对齐 VOD）：展开下拉/切换筛选/清空
function toggleFilter(col) {
  filterDropdown.value = filterDropdown.value === col ? '' : col
}
function setFilter(col, v) {
  if (col === 'skill') taskSkill.value = v
  else if (col === 'agentType') taskType.value = v
  filterDropdown.value = ''
  taskPage.value = 0
  load()
}
function clearFilters() {
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

/* ===== 任务状态统计卡（kpi 色带卡片，对齐 AgentManage/Overview 体系） ===== */
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
.ellipsis-cell {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  display: inline-block;
  max-width: 100%;
  vertical-align: bottom;
}

/* ===== 关联智能体卡片墙（对齐智能体管理页面 AgentManage） ===== */
.agent-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; margin-bottom: 18px; }
.agent-card {
  background: #fff; border: 1px solid var(--border-color); border-radius: 10px;
  box-shadow: var(--card-shadow); padding: 14px 16px; border-left: 3px solid #006eff;
  display: flex; flex-direction: column; gap: 10px; cursor: pointer;
}
.agent-card:hover { box-shadow: var(--card-shadow-hover); transform: translateY(-1px); }
.agent-card.kpi-blue { border-left-color: #006eff; }
.agent-card.kpi-purple { border-left-color: #7222ed; }
.agent-card.kpi-pink { border-left-color: #eb2f96; }
.agent-card.kpi-orange { border-left-color: #fa8c16; }
.agent-card.kpi-green { border-left-color: #52c41a; }
.agent-head { display: flex; align-items: center; gap: 10px; }
.agent-icon { width: 38px; height: 38px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 19px; flex-shrink: 0; }
.agent-meta { flex: 1; min-width: 0; }
.agent-name { font-size: 15px; font-weight: 600; color: var(--text-primary); display: flex; align-items: center; gap: 6px; }
.code-tag { font-size: 11px; font-weight: 500; color: var(--text-tertiary); background: #f5f7fa; border-radius: 4px; padding: 1px 6px; }
.agent-sub { font-size: 11px; color: var(--text-tertiary); margin-top: 2px; }
.agent-desc { font-size: 12px; color: var(--text-secondary); line-height: 1.6; min-height: 38px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.agent-stats { display: flex; gap: 18px; border-top: 1px dashed var(--border-color); padding-top: 10px; }
.as-item { display: flex; flex-direction: column; gap: 1px; }
.as-item strong { font-size: 16px; font-weight: 700; color: var(--text-primary); }
.as-item span { font-size: 11px; color: var(--text-tertiary); }
.sb-row { display: flex; gap: 6px; flex-wrap: wrap; }
.sb-seg { font-size: 11px; color: #fff; border-radius: 4px; padding: 1px 7px; }
.agent-foot { display: flex; align-items: center; justify-content: space-between; border-top: 1px solid #f0f0f0; padding-top: 8px; }
.cat-tag { font-size: 11px; padding: 1px 8px; border-radius: 10px; }
.cat-official { background: #e6f4ff; color: #006eff; }
.detail-link { font-size: 12px; color: var(--primary); }
.sw { position: relative; display: inline-block; width: 38px; height: 21px; flex-shrink: 0; cursor: pointer; }
.sw input { opacity: 0; width: 0; height: 0; }
.sw-slider {
  position: absolute; inset: 0; background: #c0c4cc;
  border: 1px solid #a8adb5; border-radius: 20px; transition: all .2s;
}
.sw-slider::before {
  content: ''; position: absolute; width: 15px; height: 15px; left: 2px; top: 2px;
  background: #fff; border-radius: 50%; transition: all .2s;
  box-shadow: 0 1px 2px rgba(0,0,0,.3); border: 1px solid #e2e2e2;
}
.sw input:checked + .sw-slider { background: #52c41a; border-color: #52c41a; }
.sw input:checked + .sw-slider::before { transform: translateX(17px); }
.sw input:disabled + .sw-slider { opacity: .5; cursor: not-allowed; }
</style>
