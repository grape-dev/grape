<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>健康中心</h1>
      <span class="badge">{{ taskKpi.total + deviceKpi.total }} 项监控</span>
      <span style="margin-left:auto;display:flex;align-items:center;gap:10px">
        <span class="t3" style="font-size:12px">{{ lastUpdate || '—' }}</span>
        <button class="btn btn-sm" @click="load">🔄 刷新</button>
        <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:var(--text-secondary)">
          <input type="checkbox" v-model="autoRefresh" @change="toggleAuto" style="margin:0" />自动(15s)
        </label>
      </span>
    </div>

    <div style="padding:20px 28px">
      <!-- 告警中心横幅 -->
      <div class="alert-banner" v-if="alerts.length > 0" :class="{ expanded: alertExpanded }">
        <div class="alert-banner-head" @click="alertExpanded = !alertExpanded">
          <span class="alert-icon">🚨</span>
          <span class="alert-title">告警中心</span>
          <span class="alert-count">{{ alerts.length }} 条异常</span>
          <div class="alert-summary">
            <span v-for="(s, k) in alertSummary" :key="k" class="alert-chip" :style="{ color: s.color, background: s.bg }">
              {{ s.label }} {{ s.count }}
            </span>
          </div>
          <span class="alert-expand-icon">{{ alertExpanded ? '▲' : '▼' }}</span>
        </div>
        <div class="alert-banner-body" v-if="alertExpanded">
          <div class="alert-item" v-for="(a, i) in alerts" :key="i">
            <span class="alert-item-severity" :class="a.severity">{{ severityText(a.severity) }}</span>
            <span class="alert-item-source" :class="'src-' + a.dimension">{{ dimensionText(a.dimension) }}</span>
            <span class="alert-item-title">{{ a.title }}</span>
            <span class="alert-item-detail">{{ a.detail }}</span>
            <span class="alert-item-time t3">{{ a.time || '—' }}</span>
          </div>
        </div>
      </div>

      <!-- 三维度 KPI 卡片行 -->
      <div class="hc-dimensions">
        <!-- 任务维度 -->
        <div class="hc-dim-card" :class="{ active: activeTab === 'tasks' }" @click="switchTab('tasks')">
          <div class="hc-dim-head">
            <span class="hc-dim-icon">📋</span>
            <span class="hc-dim-name">任务</span>
            <span class="hc-dim-health" :style="{ color: taskHealth.color }">● {{ taskHealth.text }}</span>
          </div>
          <div class="hc-kpi-row">
            <div class="hc-kpi"><span class="hc-kpi-val">{{ taskKpi.total }}</span><span class="hc-kpi-label">总任务</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" style="color:var(--success)">{{ taskKpi.completed }}</span><span class="hc-kpi-label">已完成</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" style="color:var(--warning)">{{ taskKpi.running }}</span><span class="hc-kpi-label">运行中</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" style="color:var(--danger)">{{ taskKpi.failed }}</span><span class="hc-kpi-label">失败</span></div>
          </div>
          <div class="hc-dim-bar" v-if="taskKpi.total > 0">
            <div class="hc-dim-bar-seg success" :style="{ width: barWidth(taskKpi.completed, taskKpi.total) }"></div>
            <div class="hc-dim-bar-seg warning" :style="{ width: barWidth(taskKpi.running, taskKpi.total) }"></div>
            <div class="hc-dim-bar-seg danger" :style="{ width: barWidth(taskKpi.failed, taskKpi.total) }"></div>
          </div>
        </div>

        <!-- 设备维度 -->
        <div class="hc-dim-card" :class="{ active: activeTab === 'devices' }" @click="switchTab('devices')">
          <div class="hc-dim-head">
            <span class="hc-dim-icon">🖥️</span>
            <span class="hc-dim-name">设备</span>
            <span class="hc-dim-health" :style="{ color: deviceHealth.color }">● {{ deviceHealth.text }}</span>
          </div>
          <div class="hc-kpi-row">
            <div class="hc-kpi"><span class="hc-kpi-val">{{ deviceKpi.total }}</span><span class="hc-kpi-label">总设备</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" style="color:var(--success)">{{ deviceKpi.online }}</span><span class="hc-kpi-label">在线</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" style="color:var(--warning)">{{ deviceKpi.offline }}</span><span class="hc-kpi-label">离线</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" style="color:var(--danger)">{{ deviceKpi.unhealthy }}</span><span class="hc-kpi-label">异常</span></div>
          </div>
          <div class="hc-dim-bar" v-if="deviceKpi.total > 0">
            <div class="hc-dim-bar-seg success" :style="{ width: barWidth(deviceKpi.online, deviceKpi.total) }"></div>
            <div class="hc-dim-bar-seg warning" :style="{ width: barWidth(deviceKpi.offline, deviceKpi.total) }"></div>
            <div class="hc-dim-bar-seg danger" :style="{ width: barWidth(deviceKpi.unhealthy, deviceKpi.total) }"></div>
          </div>
        </div>

        <!-- 基础设施维度 -->
        <div class="hc-dim-card" :class="{ active: activeTab === 'infra' }" @click="switchTab('infra')">
          <div class="hc-dim-head">
            <span class="hc-dim-icon">🏗️</span>
            <span class="hc-dim-name">基础设施</span>
            <span class="hc-dim-health" :style="{ color: infraHealth.color }">● {{ infraHealth.text }}</span>
          </div>
          <div class="hc-kpi-row">
            <div class="hc-kpi"><span class="hc-kpi-val">{{ infraKpi.dbCount }}</span><span class="hc-kpi-label">数据库</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" :style="{ color: infraKpi.dbHealthy ? 'var(--success)' : 'var(--danger)' }">{{ infraKpi.dbHealthy ? '正常' : '异常' }}</span><span class="hc-kpi-label">DB 状态</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val">{{ infraKpi.elbCount }}</span><span class="hc-kpi-label">ELB</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" style="color:var(--text-tertiary)">—</span><span class="hc-kpi-label">预留</span></div>
          </div>
          <div class="hc-dim-bar" v-if="infraKpi.dbCount > 0">
            <div class="hc-dim-bar-seg" :class="infraKpi.dbHealthy ? 'success' : 'danger'" :style="{ width: '100%' }"></div>
          </div>
        </div>
      </div>

      <!-- Tab 内容区 -->
      <div class="card hc-detail-card" style="margin-top:16px">
        <!-- 任务列表（按 Issue 分组，点击展开事件） -->
        <div v-if="activeTab === 'tasks'">
          <div class="hc-detail-head">
            <h3>任务列表</h3>
            <span class="t3" style="font-size:12px">{{ timelineIssues.length }} 个 Issue — 点击展开</span>
            <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
              <input v-model="timelineRepo" placeholder="仓库名筛选" style="width:160px;padding:5px 8px;border:1px solid var(--border-color);border-radius:6px;font-size:12px" @keyup.enter="loadTimeline" />
              <button class="btn btn-sm" @click="loadTimeline">刷新</button>
            </div>
          </div>
          <div style="padding:12px 20px">
            <div v-if="timelineLoading" style="text-align:center;padding:32px;color:var(--text-tertiary)">加载中...</div>
            <div v-else-if="timelineIssues.length === 0" style="text-align:center;padding:32px;color:var(--text-tertiary)">暂无任务事件</div>
            <!-- Issue 行 -->
            <div v-for="iss in timelineIssues" :key="iss.issueId" class="tl-issue">
              <div class="tl-issue-head" @click="toggleIssue(iss.issueId)">
                <span class="tl-arrow">{{ expandedIssues.has(iss.issueId) ? '▾' : '▸' }}</span>
                <b class="tl-issue-id">{{ iss.repoName || '' }} {{ iss.issueId }}</b>
                <span class="tl-issue-title t3" v-if="iss.title">{{ iss.title }}</span>
                <span class="tl-issue-count t3">{{ iss.eventCount }} 事件</span>
                <span class="tl-issue-status" :class="issueHasFailed(iss) ? 'fail' : 'ok'">{{ issueHasFailed(iss) ? '✗ 有失败' : '✓ 正常' }}</span>
                <span class="t3 tl-issue-time">{{ (iss.lastAt || '').substring(0, 16) }}</span>
              </div>
              <!-- 展开的事件列表 -->
              <div v-if="expandedIssues.has(iss.issueId)" class="tl-issue-body">
                <table class="vod-table">
                  <thead>
                    <tr><th>Seq</th><th>事件</th><th>Agent</th><th>Skill</th><th>状态</th><th>耗时</th><th>时间</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="ev in iss.events" :key="ev.seq" :class="{ 'tl-row-fail': ev.status === 'failed', 'tl-row-ok': ev.status === 'completed' }">
                      <td class="t3">#{{ ev.seq }}</td>
                      <td><span :class="['status-tag', eventTypeClass(ev.eventType)]" style="font-size:11px">{{ eventTypeText(ev.eventType) }}</span></td>
                      <td><span class="status-tag info" style="font-size:11px">{{ ev.agentType || '—' }}</span></td>
                      <td class="t3">{{ ev.skill || '—' }}</td>
                      <td><span :class="['status-tag', ev.status === 'completed' ? 'success' : ev.status === 'failed' ? 'danger' : ev.status === 'running' ? 'warning' : 'default']">{{ ev.status }}</span></td>
                      <td class="t3">{{ formatDuration2(ev.duration) }}</td>
                      <td class="t3">{{ formatTime(ev.createdAt) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>

        <!-- 设备概览（Worker 卡片 + Runtime 引擎表格，对齐主服务概览 tab） -->
        <div v-if="activeTab === 'devices'">
          <div class="hc-detail-head">
            <h3>设备监控</h3>
            <span class="t3" style="font-size:12px">Worker {{ workers.online }}/{{ workers.total }} 在线 · 引擎 {{ runtimes.online }}/{{ runtimes.total }} 在线</span>
            <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
              <button class="btn btn-sm" @click="loadOverview">🔄 刷新</button>
            </div>
          </div>

          <!-- Worker KPI 行 -->
          <div class="hc-overview-kpi">
            <div class="hc-kpi-card"><span class="hc-kpi-val">{{ workers.total }}</span><span class="hc-kpi-label">Worker 总数</span></div>
            <div class="hc-kpi-card"><span class="hc-kpi-val" style="color:var(--success)">{{ workers.online }}</span><span class="hc-kpi-label">在线</span></div>
            <div class="hc-kpi-card"><span class="hc-kpi-val" style="color:var(--text-tertiary)">{{ workers.offline }}</span><span class="hc-kpi-label">离线</span></div>
            <div class="hc-kpi-card"><span class="hc-kpi-val" style="color:var(--primary)">{{ runtimes.total }}</span><span class="hc-kpi-label">引擎总数</span></div>
          </div>

          <!-- Worker 卡片网格 -->
          <div class="hc-section-title">Worker 监控</div>
          <div class="hc-worker-grid" v-if="workersList.length > 0">
            <div class="hc-worker-card" v-for="wk in workersList" :key="wk.id">
              <div class="hc-worker-head">
                <span class="hc-health-dot" :style="{ background: healthColor(wk.health) }"></span>
                <b class="hc-worker-name">{{ wk.deviceName || wk.name || 'Worker #' + wk.id }}</b>
                <span class="hc-worker-health" :style="{ color: healthColor(wk.health) }">{{ wk.health }}</span>
              </div>
              <div class="hc-worker-meta">
                <span>IP: {{ wk.lastIp || '—' }}</span>
                <span>心跳: {{ formatTime(wk.lastSeenAt).slice(11, 19) || '—' }}</span>
              </div>
              <div class="hc-resource-bar">
                <span class="hc-resource-label">CPU</span>
                <div class="hc-resource-track"><div class="hc-resource-fill" :style="{ width: pct(wk.cpuPercent), background: resourceColor(wk.cpuPercent) }"></div></div>
                <span class="hc-resource-val" :style="{ color: resourceColor(wk.cpuPercent) }">{{ Math.round(wk.cpuPercent || 0) }}%</span>
              </div>
              <div class="hc-resource-bar">
                <span class="hc-resource-label">MEM</span>
                <div class="hc-resource-track"><div class="hc-resource-fill" :style="{ width: pct(wk.memoryPercent), background: resourceColor(wk.memoryPercent) }"></div></div>
                <span class="hc-resource-val" :style="{ color: resourceColor(wk.memoryPercent) }">{{ Math.round(wk.memoryPercent || 0) }}%</span>
              </div>
              <div class="hc-resource-bar">
                <span class="hc-resource-label">DISK</span>
                <div class="hc-resource-track"><div class="hc-resource-fill" :style="{ width: pct(wk.diskPercent), background: resourceColor(wk.diskPercent) }"></div></div>
                <span class="hc-resource-val" :style="{ color: resourceColor(wk.diskPercent) }">{{ Math.round(wk.diskPercent || 0) }}%</span>
              </div>
            </div>
          </div>
          <div v-else class="hc-empty">无 Worker 数据</div>

          <!-- Runtime 引擎表格 -->
          <div class="hc-section-title">Runtime 引擎</div>
          <table class="vod-table">
            <thead>
              <tr><th>引擎</th><th>版本</th><th>健康度</th><th>繁忙</th><th>模式</th><th>范围</th><th>最后心跳</th></tr>
            </thead>
            <tbody>
              <tr v-for="rt in runtimesList" :key="rt.id">
                <td><span class="hc-health-dot" :style="{ background: healthColor(rt.health) }"></span> <b>{{ rt.name }}</b></td>
                <td class="t3">{{ rt.version || '—' }}</td>
                <td><span :style="{ color: healthColor(rt.health), fontWeight: 600 }">{{ rt.health }}</span></td>
                <td><span :style="{ color: loadColor(rt.running, rt.maxConcurrent) }">{{ rt.running }}/{{ rt.maxConcurrent }}</span></td>
                <td><span :class="['status-tag', rt.mode === 'local' ? 'info' : 'default']" style="font-size:11px">{{ rt.mode === 'local' ? '本机' : '远端' }}</span></td>
                <td><span class="status-tag default" style="font-size:11px">{{ rt.scope || 'global' }}</span></td>
                <td class="t3">{{ formatTime(rt.lastSeenAt) }}</td>
              </tr>
              <tr v-if="runtimesList.length === 0">
                <td colspan="7" style="text-align:center;padding:32px;color:var(--text-tertiary)">暂无引擎数据</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 基础设施详情 -->
        <div v-if="activeTab === 'infra'">
          <div class="hc-detail-head">
            <h3>基础设施监控</h3>
            <span class="t3" style="font-size:12px">数据库 / ELB / 缓存</span>
          </div>
          <div class="hc-infra-grid">
            <div class="hc-infra-card" v-for="db in infraDetails" :key="db.name">
              <div class="hc-infra-head">
                <span class="hc-infra-icon">{{ db.type === 'mysql' ? '🗄️' : '⚖️' }}</span>
                <span class="hc-infra-name">{{ db.name }}</span>
                <span class="status-tag" :class="db.healthy ? 'success' : 'danger'">{{ db.healthy ? '正常' : '异常' }}</span>
              </div>
              <div class="hc-infra-body">
                <div class="hc-infra-row"><span>类型</span><span>{{ db.type }}</span></div>
                <div class="hc-infra-row"><span>地址</span><span class="t3">{{ db.host }}:{{ db.port }}</span></div>
                <div class="hc-infra-row"><span>延迟</span><span :style="{ color: db.latency > 100 ? 'var(--warning)' : 'var(--success)' }">{{ db.latency }}ms</span></div>
                <div class="hc-infra-row"><span>连接数</span><span>{{ db.connections || '—' }}</span></div>
                <div class="hc-infra-row" v-if="db.error"><span>错误</span><span style="color:var(--danger);font-size:12px">{{ db.error }}</span></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref, computed } from 'vue'
import http from '../api'

const activeTab = ref('tasks')
const autoRefresh = ref(false)
let timer = null

const lastUpdate = ref('')
const alerts = ref([])
const alertExpanded = ref(false)

// KPI
const taskKpi = ref({ total: 0, completed: 0, running: 0, failed: 0, queued: 0, stuck: 0 })
const deviceKpi = ref({ total: 0, online: 0, offline: 0, unhealthy: 0 })
const infraKpi = ref({ dbCount: 0, dbHealthy: true, elbCount: 0 })
const infraDetails = ref([])

// 任务 timeline（按 Issue 分组）
const timelineIssues = ref([])
const timelineLoading = ref(false)
const timelineRepo = ref('')
const expandedIssues = ref(new Set())

// 设备概览（Worker + Runtime）
const workers = ref({ total: 0, online: 0, offline: 0 })
const workersList = ref([])
const runtimes = ref({ total: 0, online: 0, offline: 0 })
const runtimesList = ref([])

const taskHealth = computed(() => {
  const f = taskKpi.value.failed
  if (f > 5) return { text: '异常', color: 'var(--danger)' }
  if (f > 0 || taskKpi.value.stuck > 0) return { text: '警告', color: 'var(--warning)' }
  return { text: '正常', color: 'var(--success)' }
})
const deviceHealth = computed(() => {
  const u = deviceKpi.value.unhealthy + deviceKpi.value.offline
  if (deviceKpi.value.total > 0 && u === deviceKpi.value.total) return { text: '异常', color: 'var(--danger)' }
  if (u > 0) return { text: '警告', color: 'var(--warning)' }
  return { text: '正常', color: 'var(--success)' }
})
const infraHealth = computed(() => {
  if (!infraKpi.value.dbHealthy) return { text: '异常', color: 'var(--danger)' }
  return { text: '正常', color: 'var(--success)' }
})

const alertSummary = computed(() => {
  const map = { critical: { label: '严重', color: '#f5222d', bg: '#fff1f0', count: 0 }, warning: { label: '警告', color: '#faad14', bg: '#fffbe6', count: 0 }, info: { label: '提示', color: '#006eff', bg: '#e8f0fe', count: 0 } }
  alerts.value.forEach(a => { if (map[a.severity]) map[a.severity].count++ })
  return Object.fromEntries(Object.entries(map).filter(([, v]) => v.count > 0))
})

function barWidth(val, total) { return total > 0 ? ((val / total) * 100).toFixed(1) + '%' : '0%' }
function taskStatusClass(s) {
  if (s === 'completed') return 'success'
  if (s === 'running' || s === 'dispatched' || s === 'queued') return 'warning'
  if (s === 'failed') return 'danger'
  return 'default'
}
function healthClass(h) {
  if (h === 'online') return 'success'
  if (h === 'offline') return 'default'
  if (h === 'recently_lost' || h === 'about_to_gc') return 'warning'
  return 'danger'
}
function severityText(s) { return { critical: '严重', warning: '警告', info: '提示' }[s] || s }
function dimensionText(d) { return { tasks: '任务', devices: '设备', infra: '基础设施' }[d] || d }
function formatTime(ts) {
  if (!ts) return '—'
  const s = String(ts)
  if (s.startsWith('20') && s.length >= 16) return s.slice(0, 19).replace('T', ' ')
  return s
}

function switchTab(tab) {
  activeTab.value = tab
  if (tab === 'tasks' && timelineIssues.value.length === 0) loadTimeline()
  if (tab === 'devices' && workersList.value.length === 0) loadOverview()
}

async function load() {
  try {
    const { data } = await http.get('/health-center')
    taskKpi.value = data.taskKpi || taskKpi.value
    deviceKpi.value = data.deviceKpi || deviceKpi.value
    infraKpi.value = data.infraKpi || infraKpi.value
    infraDetails.value = data.infraDetails || []
    alerts.value = data.alerts || []
    lastUpdate.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
    if (timelineIssues.value.length === 0) loadTimeline()
    if (workersList.value.length === 0) loadOverview()
  } catch (e) {
    console.error('[HealthCenter] load failed', e)
    lastUpdate.value = '加载失败'
  }
}

async function loadOverview() {
  try {
    const { data } = await http.get('/health-center/overview')
    const w = data.workers || {}
    workers.value = { total: w.total || 0, online: w.online || 0, offline: w.offline || 0 }
    workersList.value = w.list || []
    const r = data.runtimes || {}
    runtimes.value = { total: r.total || 0, online: r.online || 0, offline: r.offline || 0 }
    runtimesList.value = r.list || []
  } catch (e) {
    console.error('[HealthCenter] loadOverview failed', e)
  }
}

async function loadTimeline() {
  timelineLoading.value = true
  try {
    const { data } = await http.get('/health-center/timeline', { params: { repo: timelineRepo.value } })
    timelineIssues.value = data.issues || []
  } catch (e) {
    console.error('[HealthCenter] loadTimeline failed', e)
    timelineIssues.value = []
  } finally {
    timelineLoading.value = false
  }
}

function toggleIssue(issueId) {
  const s = new Set(expandedIssues.value)
  if (s.has(issueId)) s.delete(issueId)
  else s.add(issueId)
  expandedIssues.value = s
}
function issueHasFailed(iss) { return iss.events.some(e => e.status === 'failed') }
function eventTypeText(t) {
  const map = { issue_opened: 'Issue创建', issue_comment: '评论', issue_updated: '更新', a2a_event: 'A2A交办', pull_request_opened: 'PR创建', pull_request_merged: 'PR合并', push: '推送', vod_signal: 'VOD信号' }
  return map[t] || t || '—'
}
function eventTypeClass(t) {
  if (t === 'a2a_event') return 'info'
  if (t === 'pull_request_merged') return 'success'
  if (t === 'issue_opened') return 'info'
  return 'default'
}
function formatDuration2(d) {
  if (!d) return '—'
  const s = Number(d)
  if (isNaN(s) || s <= 0) return '—'
  if (s < 60) return s + 's'
  if (s < 3600) return Math.floor(s / 60) + 'm' + (s % 60) + 's'
  return Math.floor(s / 3600) + 'h' + Math.floor((s % 3600) / 60) + 'm'
}

function pct(v) { return Math.min(v || 0, 100) + '%' }
function resourceColor(v) {
  v = v || 0
  if (v > 80) return 'var(--danger)'
  if (v > 60) return 'var(--warning)'
  return 'var(--success)'
}
function healthColor(h) {
  if (h === 'online') return 'var(--success)'
  if (h === 'recently_lost') return 'var(--warning)'
  if (h === 'about_to_gc') return 'var(--danger)'
  return 'var(--text-tertiary)'
}
function loadColor(running, max) {
  if (running >= max * 2) return 'var(--danger)'
  if (running >= max) return 'var(--warning)'
  if (running > 0) return 'var(--success)'
  return 'var(--text-tertiary)'
}

function toggleAuto() {
  if (autoRefresh.value) { timer = setInterval(load, 15000) }
  else { clearInterval(timer); timer = null }
}
onMounted(load)
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
/* 告警中心横幅 */
.alert-banner { border: 1px solid #ffa39e; border-radius: var(--radius); background: #fff1f0; margin-bottom: 16px; overflow: hidden; }
.alert-banner-head { display: flex; align-items: center; gap: 10px; padding: 10px 16px; cursor: pointer; user-select: none; }
.alert-icon { font-size: 16px; }
.alert-title { font-weight: 600; font-size: 14px; color: var(--danger); }
.alert-count { font-size: 12px; font-weight: 600; color: #fff; background: var(--danger); padding: 2px 8px; border-radius: 10px; }
.alert-summary { display: flex; gap: 6px; margin-left: 8px; }
.alert-chip { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 8px; }
.alert-expand-icon { margin-left: auto; font-size: 10px; color: var(--text-secondary); }
.alert-banner-body { border-top: 1px solid #ffccc7; background: #fff; max-height: 320px; overflow-y: auto; }
.alert-item { display: flex; align-items: center; gap: 10px; padding: 8px 16px; border-bottom: 1px solid var(--border-soft); font-size: 13px; }
.alert-item:last-child { border-bottom: none; }
.alert-item-severity { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 4px; white-space: nowrap; }
.alert-item-severity.critical { background: #fff1f0; color: #f5222d; }
.alert-item-severity.warning { background: #fffbe6; color: #faad14; }
.alert-item-severity.info { background: #e8f0fe; color: #006eff; }
.alert-item-source { font-size: 11px; padding: 2px 6px; border-radius: 4px; white-space: nowrap; }
.alert-item-source.src-tasks { background: #e8f0fe; color: #006eff; }
.alert-item-source.src-devices { background: #f6ffed; color: #52c41a; }
.alert-item-source.src-infra { background: #fff7e6; color: #d48806; }
.alert-item-title { font-weight: 500; min-width: 80px; }
.alert-item-detail { color: var(--text-secondary); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.alert-item-time { font-size: 11px; white-space: nowrap; }

/* 三维度卡片 */
.hc-dimensions { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
.hc-dim-card { background: var(--bg-white); border: 2px solid transparent; border-radius: var(--radius-lg); padding: 16px; cursor: pointer; transition: all 0.2s; box-shadow: var(--card-shadow); }
.hc-dim-card:hover { box-shadow: var(--card-shadow-hover); }
.hc-dim-card.active { border-color: var(--primary); background: var(--primary-bg); }
.hc-dim-head { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
.hc-dim-icon { font-size: 20px; }
.hc-dim-name { font-size: 15px; font-weight: 600; }
.hc-dim-health { margin-left: auto; font-size: 12px; font-weight: 600; }
.hc-kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 10px; }
.hc-kpi { display: flex; flex-direction: column; align-items: center; gap: 2px; }
.hc-kpi-val { font-size: 22px; font-weight: 700; line-height: 1.2; }
.hc-kpi-label { font-size: 11px; color: var(--text-tertiary); }
.hc-dim-bar { display: flex; height: 4px; border-radius: 2px; overflow: hidden; background: #f0f0f0; }
.hc-dim-bar-seg { height: 100%; transition: width 0.3s; }
.hc-dim-bar-seg.success { background: var(--success); }
.hc-dim-bar-seg.warning { background: var(--warning); }
.hc-dim-bar-seg.danger { background: var(--danger); }

/* 详情卡片 */
.hc-detail-card { padding: 0; overflow: hidden; }
.hc-detail-head { display: flex; align-items: center; gap: 10px; padding: 14px 20px; border-bottom: 1px solid var(--border-soft); }
.hc-detail-head h3 { font-size: 14px; font-weight: 600; }

/* 基础设施网格 */
.hc-infra-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; padding: 20px; }
.hc-infra-card { border: 1px solid var(--border-color); border-radius: var(--radius); overflow: hidden; }
.hc-infra-head { display: flex; align-items: center; gap: 8px; padding: 10px 14px; background: #fafafa; border-bottom: 1px solid var(--border-soft); }
.hc-infra-icon { font-size: 18px; }
.hc-infra-name { font-weight: 600; font-size: 14px; flex: 1; }
.hc-infra-body { padding: 10px 14px; }
.hc-infra-row { display: flex; justify-content: space-between; font-size: 13px; padding: 4px 0; }
.hc-infra-row span:first-child { color: var(--text-tertiary); }
.hc-infra-row span:last-child { font-weight: 500; }

@media (max-width: 900px) {
  .hc-dimensions { grid-template-columns: 1fr; }
}

/* Issue 分组列表 */
.tl-issue { border: 1px solid var(--border-color); border-radius: 8px; overflow: hidden; margin-bottom: 8px; }
.tl-issue-head { display: flex; align-items: center; gap: 10px; padding: 10px 14px; cursor: pointer; background: #fafbfc; }
.tl-issue-head:hover { background: #f5f9ff; }
.tl-arrow { font-size: 12px; color: var(--text-tertiary); width: 14px; }
.tl-issue-id { font-size: 13px; min-width: 40px; }
.tl-issue-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.tl-issue-count { font-size: 11px; white-space: nowrap; }
.tl-issue-status { font-size: 10px; font-weight: 600; white-space: nowrap; }
.tl-issue-status.fail { color: var(--danger); }
.tl-issue-status.ok { color: var(--success); }
.tl-issue-time { font-size: 11px; white-space: nowrap; }
.tl-issue-body { border-top: 1px solid var(--border-color); padding: 10px 14px; }
.tl-row-fail td { background: #fff1f0 !important; }
.tl-row-ok td { background: #f6ffed !important; }

/* 设备概览 */
.hc-overview-kpi { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0; border: 1px solid var(--border-color); border-radius: var(--radius); overflow: hidden; margin: 12px 20px; }
.hc-kpi-card { display: flex; flex-direction: column; align-items: center; gap: 2px; padding: 14px; border-right: 1px solid var(--border-soft); }
.hc-kpi-card:last-child { border-right: none; }
.hc-section-title { font-size: 13px; font-weight: 600; padding: 12px 20px 6px; }
.hc-empty { text-align: center; padding: 24px; color: var(--text-tertiary); font-size: 13px; }

.hc-worker-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; padding: 0 20px 12px; }
.hc-worker-card { background: #fff; border: 1px solid var(--border-color); border-radius: var(--radius); padding: 12px 14px; box-shadow: var(--card-shadow); }
.hc-worker-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.hc-health-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
.hc-worker-name { font-size: 13px; }
.hc-worker-health { margin-left: auto; font-size: 10px; font-weight: 600; }
.hc-worker-meta { display: grid; grid-template-columns: 1fr 1fr; gap: 2px 12px; font-size: 10px; color: var(--text-tertiary); margin-bottom: 8px; }
.hc-resource-bar { display: flex; align-items: center; gap: 6px; font-size: 10px; margin-bottom: 4px; }
.hc-resource-label { color: var(--text-tertiary); min-width: 30px; }
.hc-resource-track { flex: 1; height: 8px; background: #f0f0f0; border-radius: 4px; overflow: hidden; min-width: 60px; }
.hc-resource-fill { height: 100%; border-radius: 4px; transition: width 0.3s; }
.hc-resource-val { font-weight: 600; min-width: 30px; text-align: right; }
</style>
