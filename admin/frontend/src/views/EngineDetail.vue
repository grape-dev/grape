<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <span class="back-btn" @click="goBack">
        <svg class="back-arrow" viewBox="0 0 24 24" width="16" height="16"><path d="M15 18l-6-6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        返回
      </span>
      <h1>引擎详情</h1>
      <span class="badge" :style="badgeStyle(detail?.health)">{{ healthText(detail?.health) }}</span>
      <span class="t3" style="font-size:12px">{{ detail?.displayName || detail?.name || '' }} · {{ detail?.device || '' }} · {{ detail?.lastIp || '' }}</span>
    </div>

    <div class="dashboard" style="padding:20px 28px" v-if="detail">
      <!-- 关联智能体 -->
      <div class="card" style="margin-top:16px;padding:0">
        <div class="section-header">
          <h5 class="sh-title">关联智能体 <span class="sh-count">共 {{ agents.length }} 个</span></h5>
          <div class="sh-actions">
            <button class="btn btn-sm" :disabled="refreshing" @click="refresh" title="刷新智能体与任务数据">刷新</button>
          </div>
        </div>
        <div style="padding:14px 20px">
          <div style="position:relative">
            <div class="table-loading-overlay" v-if="refreshing">
              <span class="table-loading-text"><span class="spinner"></span>刷新中...</span>
            </div>
            <div class="agent-grid" v-if="agents.length">
            <div v-for="(a, i) in agents" :key="a.agentType" class="agent-card" :class="accents[i % accents.length]" @click="$router.push(`/agent/manage/${a.agentType}`)">
              <div class="agent-head">
                <div class="agent-icon" :style="iconStyle(i)">{{ a.icon || '🤖' }}</div>
                <div class="agent-meta">
                  <div class="agent-name">{{ a.displayName }}<span class="code-tag">{{ a.agentType }}</span></div>
                  <div class="agent-sub">顺序 #{{ a.orderNum }} · 引擎 {{ a.defaultRuntimeId || '-' }}</div>
                </div>
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
      </div>

      <!-- 关联任务 -->
      <TaskListSection :tasks="detail.tasks" :loading="taskLoading" @reload="onTaskReload" />
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as echarts from 'echarts'
import http from '../api'
import TaskListSection from '../components/TaskListSection.vue'

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
const taskStatus = ref('')
const taskSkill = ref('')
const taskType = ref('')
const sortOrder = ref('desc')
const filterDropdown = ref('')
const filterCount = ref(0)
const hasAnyFilter = computed(() => !!(taskStatus.value || taskSkill.value || taskType.value))
// 加载遮罩 + 统计卡 + 图表
const taskLoading = ref(false)
const refreshing = ref(false)
const taskChartEl = ref(null)
let taskChart = null
// 任务状态展示映射（未知状态原样显示）
const STATUS_META = {
  completed: { label: '完成', color: 'var(--success)' },
  success: { label: '完成', color: 'var(--success)' },
  failed: { label: '失败', color: '#ff3b30' },
  running: { label: '运行中', color: '#006eff' },
  pending: { label: '等待', color: '#ff9500' }
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
  recently_lost: { color: '#ff9500', bg: 'rgba(255,149,0,0.1)', label: '刚失联' },
  offline: { color: 'var(--text-tertiary)', bg: '#eef0f3', label: '离线' },
  about_to_gc: { color: 'var(--danger)', bg: '#fee2e2', label: '待回收' },
  revoked: { color: 'var(--text-tertiary)', bg: '#eef0f3', label: '已吊销' }
}
const SCOPE_TEXT = { global: '全局', repo: '仓库', user: '个人' }
function scopeText(s) { return SCOPE_TEXT[s] || s || '—' }
function healthText(h) { return (BADGE[h] || { label: h }).label }
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

function formatDuration(start, end) {
  if (!start || !end) return '—'
  const ms = new Date(end.replace(' ', 'T')) - new Date(start.replace(' ', 'T'))
  if (isNaN(ms)) return '—'
  const s = Math.round(ms / 1000)
  if (s < 60) return s + 's'
  return Math.floor(s / 60) + 'm' + (s % 60) + 's'
}

function issueGitCodeUrl(t) {
  const full = (t.repoKey || '').replace(/^gitcode:/, '')
  if (!full) return '#'
  return 'https://gitcode.com/' + full + '/issues/' + (t.issueId || '').replace('#', '')
}

// ===== Issue 详情弹窗（时序图 + 事件列表）=====
const issueModal = ref({ show: false, loading: false, issueId: '', repoName: '', eventCount: 0, events: [], view: 'diagram', svg: '', svgRendered: false })

async function openIssueDetail(issueId) {
  issueModal.value = { show: true, loading: true, issueId, repoName: '', eventCount: 0, events: [], view: 'diagram', svg: '', svgRendered: false }
  try {
    const { data } = await http.get(`/runtime/issues/${encodeURIComponent(issueId)}/events`)
    const events = data.events || []
    const repoKey = events.length > 0 ? (events[0].repoKey || '') : ''
    issueModal.value = {
      show: true, loading: false, issueId,
      repoName: repoKey.replace(/^gitcode:/, ''),
      eventCount: events.length, events,
      view: 'diagram', svg: '', svgRendered: false
    }
    setTimeout(() => renderIssueMermaid(), 50)
  } catch (e) {
    issueModal.value = { show: true, loading: false, issueId, events: [], view: 'events', svg: '', svgRendered: false }
  }
}
function closeIssueModal() {
  issueModal.value = { show: false, loading: false, issueId: '', repoName: '', eventCount: 0, events: [], view: 'diagram', svg: '', svgRendered: false }
}

function eventTypeText(t) {
  const map = { issue_opened: 'Issue创建', issue_comment: '评论', issue_updated: '更新', a2a_event: 'A2A交办', pull_request_opened: 'PR创建', pull_request_merged: 'PR合并', push: '推送', vod_signal: 'VOD信号', issue_closed: 'Issue关闭' }
  return map[t] || t || '事件'
}
function eventTypeClass(t) {
  if (t === 'a2a_event') return 'info'
  if (t === 'pull_request_merged') return 'success'
  if (t === 'issue_opened') return 'info'
  return 'default'
}

// ===== Mermaid 时序图渲染 =====
let mermaidEngine = null
async function getMermaid() {
  if (!mermaidEngine) {
    const mod = await import('mermaid')
    mermaidEngine = mod.default
    mermaidEngine.initialize({ startOnLoad: false, securityLevel: 'loose', theme: 'base', fontFamily: 'inherit', sequence: { mirrorActors: false, actorMargin: 60, messageMargin: 40, noteMargin: 10, boxMargin: 8 } })
  }
  return mermaidEngine
}
function mermaidId(agent) { return (agent || 'agent').replace(/[^a-zA-Z0-9_-]/g, '_') }
function actionText(t) {
  const map = { issue_opened: 'Issue创建', issue_comment: '评论', issue_updated: '更新', a2a_event: 'A2A交办', pull_request_opened: 'PR创建', pull_request_merged: 'PR合并', push: '推送', vod_signal: 'VOD信号', issue_closed: 'Issue关闭' }
  return map[t] || t || '事件'
}
function buildSequence(events) {
  const agents = []
  for (const ev of events) {
    const a = ev.agentType || '—'
    if (!agents.includes(a)) agents.push(a)
  }
  const lines = ['sequenceDiagram', 'autonumber']
  for (const a of agents) lines.push(`    participant ${mermaidId(a)} as ${a}`)
  const statuses = []
  let prev = null
  for (const ev of events) {
    const a = mermaidId(ev.agentType || '—')
    const t = ev.createdAt ? String(ev.createdAt).slice(11, 19) : ''
    const label = (t ? t + ' ' : '') + actionText(ev.eventType)
    if (prev && a !== prev) {
      lines.push(`    ${prev}->>${a}: ${label}`)
      statuses.push(ev.status || '')
    } else {
      lines.push(`    Note over ${a}: ${label}`)
      statuses.push(ev.status || '')
    }
    prev = a
  }
  return { text: lines.join('\n'), statuses }
}
function applySequenceColors(svg, statuses) {
  try {
    const doc = new DOMParser().parseFromString(svg, 'image/svg+xml')
    const root = doc.documentElement
    const defs = root.querySelector('defs')
    if (!defs) return svg
    const colorOf = s => (s === 'failed' ? '#ff3b30' : s === 'completed' ? '#34c759' : '#aeaeb2')
    const lines = Array.from(root.querySelectorAll('line[marker-start*="sequencenumber"]'))
    lines.forEach((line, i) => {
      const markerId = root.id + '-seqnum-' + i
      const marker = doc.createElementNS('http://www.w3.org/2000/svg', 'marker')
      marker.setAttribute('id', markerId)
      marker.setAttribute('refX', '15'); marker.setAttribute('refY', '15')
      marker.setAttribute('markerWidth', '60'); marker.setAttribute('markerHeight', '40')
      marker.setAttribute('orient', 'auto')
      const circle = doc.createElementNS('http://www.w3.org/2000/svg', 'circle')
      circle.setAttribute('cx', '15'); circle.setAttribute('cy', '15'); circle.setAttribute('r', '6')
      circle.setAttribute('fill', colorOf(statuses[i] || ''))
      marker.appendChild(circle)
      defs.appendChild(marker)
      line.setAttribute('marker-start', 'url(#' + markerId + ')')
    })
    return new XMLSerializer().serializeToString(doc)
  } catch (e) { return svg }
}
async function renderIssueMermaid() {
  if (issueModal.value.svgRendered || issueModal.value.loading) return
  const events = issueModal.value.events
  if (!events || events.length === 0) return
  try {
    const mmd = await getMermaid()
    const { text, statuses } = buildSequence(events)
    const uid = 'mmd-' + Math.random().toString(36).slice(2, 10)
    try { await mmd.parse(text) } catch (e) { }
    const result = await mmd.render(uid, text)
    const svg = result.svg || result
    issueModal.value = { ...issueModal.value, svg: applySequenceColors(svg, statuses), svgRendered: true }
  } catch (err) {
    issueModal.value = { ...issueModal.value, svg: `<div style="padding:12px;color:var(--danger);font-size:12px">时序图渲染失败: ${err.message || err}</div>`, svgRendered: true }
  }
}

// ===== 日志弹窗 =====
const logModal = ref({ show: false, loading: false, messages: [], info: {} })

async function openLogModal(taskId, task) {
  logModal.value = { show: true, loading: true, messages: [], info: { issueId: task?.issueId, repoKey: task?.repoKey, eventSeq: task?.eventSeq, createdAt: task?.createdAt } }
  try {
    const [msgsResp, infoResp] = await Promise.all([
      http.get(`/health-center/tasks/${taskId}/messages`),
      http.get(`/health-center/tasks/${taskId}/info`),
    ])
    const info = infoResp.data || {}
    if (!info.repoKey && task?.repoKey) info.repoKey = task.repoKey
    logModal.value = { show: true, loading: false, messages: msgsResp.data || [], info }
  } catch (e) {
    logModal.value = { show: true, loading: false, messages: [], info: { error: e.message } }
  }
}
function closeLogModal() { logModal.value = { show: false, loading: false, messages: [], info: {} } }
function logTypeIcon(t) {
  const map = { step_start: '▶', step_finish: '✓', text: '💬', reasoning: '🤔', tool_use: '🔧', error: '❌', done: '✅' }
  return map[t] || '•'
}

const STATUS_COLOR = { queued: '#ff9500', running: '#006eff', completed: '#34c759', failed: '#ff2d92' }
const accents = ['kpi-blue', 'kpi-purple', 'kpi-pink', 'kpi-orange', 'kpi-green']
const iconPalette = [
  { background: 'rgba(0,107,255,0.08)', color: '#006eff' },
  { background: '#f0e6ff', color: '#7222ed' },
  { background: 'rgba(255,45,146,0.08)', color: '#ff2d92' },
  { background: 'rgba(255,149,0,0.1)', color: '#ff9500' },
  { background: 'rgba(52,199,89,0.1)', color: '#34c759' }
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
  return a.loadLevel === 'over' ? '#ff2d92' : '#34c759'
}

async function refresh() {
  refreshing.value = true
  const start = Date.now()
  try { await load() } finally {
    // 保证加载动画至少显示 300ms（本地响应过快时避免一闪而过）
    const wait = Math.max(0, 300 - (Date.now() - start))
    setTimeout(() => { refreshing.value = false }, wait)
  }
}
async function load() {
  taskLoading.value = true
  const params = { taskPage: taskPage.value, taskSize: taskSize.value, sortOrder: sortOrder.value }
  if (taskStatus.value) params.taskStatus = taskStatus.value
  if (taskSkill.value) params.taskSkill = taskSkill.value
  if (taskType.value) params.taskType = taskType.value
  try {
    const { data } = await http.get(`/engines/${route.params.id}`, { params })
    detail.value = data
    agents.value = data.agents || []
    nextTick(renderTaskChart)
  } finally {
    taskLoading.value = false
  }
}

function onTaskReload(params) {
  taskPage.value = params.taskPage ?? 0
  taskSize.value = params.taskSize ?? 10
  sortOrder.value = params.sortOrder ?? 'desc'
  taskStatus.value = params.taskStatus ?? ''
  taskSkill.value = params.taskSkill ?? ''
  taskType.value = params.taskType ?? ''
  load()
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
    color: ['#34c759', '#ff3b30', '#006eff', '#ff9500', '#aeaeb2', '#7222ed']
  })
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
  gap: 12px;
}
.detail-item {
  background: #fff;
  border: 1px solid var(--border-color);
  border-left: 3px solid var(--border-color);
  border-radius: 14px;
  padding: 10px 14px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  transition: all var(--transition);
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
  background: rgba(0,0,0,0.04);
  border: 1px solid var(--border-color);
  cursor: pointer;
  user-select: none;
  transition: all var(--transition);
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
  border-radius: 14px;
  border-left: 3px solid #006eff;
  padding: 12px 16px;
  text-align: center;
  transition: all var(--transition);
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
  border-radius: 8px;
  cursor: pointer;
  white-space: nowrap;
}
.col-filter-opt:hover { background: rgba(0,0,0,0.04); }
.col-filter-opt.active { background: var(--primary-bg); color: var(--primary); font-weight: 500; }
.ellipsis-cell {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
  vertical-align: bottom;
}

/* ===== 关联智能体卡片墙（对齐智能体管理页面 AgentManage） ===== */
.agent-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; margin-bottom: 18px; }
.agent-card {
  background: #fff; border: 1px solid var(--border-color); border-radius: 14px;
  box-shadow: var(--card-shadow); padding: 14px 16px; border-left: 3px solid #006eff;
  display: flex; flex-direction: column; gap: 10px; cursor: pointer;
}
.agent-card:hover { box-shadow: var(--card-shadow-hover); transform: translateY(-1px); }
.agent-card.kpi-blue { border-left-color: #006eff; }
.agent-card.kpi-purple { border-left-color: #7222ed; }
.agent-card.kpi-pink { border-left-color: #ff2d92; }
.agent-card.kpi-orange { border-left-color: #ff9500; }
.agent-card.kpi-green { border-left-color: #34c759; }
.agent-head { display: flex; align-items: center; gap: 10px; }
.agent-icon { width: 38px; height: 38px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 19px; flex-shrink: 0; }
.agent-meta { flex: 1; min-width: 0; }
.agent-name { font-size: 15px; font-weight: 600; color: var(--text-primary); display: flex; align-items: center; gap: 6px; }
.code-tag { font-size: 11px; font-weight: 500; color: var(--text-tertiary); background: rgba(0,0,0,0.04); border-radius: 8px; padding: 1px 6px; }
.agent-sub { font-size: 11px; color: var(--text-tertiary); margin-top: 2px; }
.agent-desc { font-size: 12px; color: var(--text-secondary); line-height: 1.6; min-height: 38px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.agent-stats { display: flex; gap: 18px; border-top: 1px dashed var(--border-color); padding-top: 10px; }
.as-item { display: flex; flex-direction: column; gap: 1px; }
.as-item strong { font-size: 16px; font-weight: 700; color: var(--text-primary); }
.as-item span { font-size: 11px; color: var(--text-tertiary); }
.sb-row { display: flex; gap: 6px; flex-wrap: wrap; }
.sb-seg { font-size: 11px; color: #fff; border-radius: 8px; padding: 1px 7px; }
.agent-foot { display: flex; align-items: center; justify-content: space-between; border-top: 1px solid var(--border-soft); padding-top: 8px; }
.cat-tag { font-size: 11px; padding: 1px 8px; border-radius: 14px; }
.cat-official { background: #e6f4ff; color: #006eff; }
.detail-link { font-size: 12px; color: var(--primary); }
/* 刷新按钮加载动画 */
.spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 2px solid rgba(0, 0, 0, 0.15);
  border-top-color: var(--primary, #006eff);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
  vertical-align: -2px;
  margin-right: 4px;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ===== 日志弹窗 ===== */
.tl-row-clickable { cursor: pointer; }
.tl-row-clickable:hover { background: rgba(0, 107, 255, 0.04); }
.tl-row-fail { background: rgba(229, 72, 77, 0.03); }
.tl-row-ok { background: rgba(46, 158, 91, 0.03); }
.log-tl-dot.ok { background: #34c759; }
.log-tl-dot.fail { background: #ff2d92; }
.log-tl-dot.run { background: #006eff; }
span.ok { color: #34c759; }
span.fail { color: #ff2d92; }
span.run { color: #006eff; }
.tl-diagram-container { overflow-x: auto; }
.tl-diagram-svg svg { max-width: 100%; height: auto; }
.tl-diagram-loading { padding: 20px; text-align: center; color: var(--text-tertiary, #999); }
.btn-primary { background: var(--primary, #006eff); color: #fff; border-color: var(--primary, #006eff); }
</style>
