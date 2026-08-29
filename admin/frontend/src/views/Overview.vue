<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>总览</h1>
      <span class="refresh-time" v-if="d.lastTaskAt">最近任务：{{ formatAgo(d.lastTaskAt) }}</span>
      <span class="refresh-time" v-else>暂无任务数据</span>
      <button class="btn" style="margin-left:12px" @click="load">刷新</button>
    </div>

    <div v-if="loading" class="loading-spinner">加载中...</div>

    <div v-else class="dashboard">

      <!-- KPI 卡片行：用户 → 仓库 → 设备 → 任务 -->
      <div class="kpi-row">
        <div class="kpi-card kpi-cyan" title="Grape 工作台用户数（user_accounts，即托管仓库登录用户）" @click="$router.push('/sys/console')">
          <div class="kpi-icon" style="background:rgba(48,176,199,0.1);color:#30b0c7;">👥</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ vc.platformUserTotal || 0 }}</div>
            <div class="kpi-label">工作台用户</div>
            <div class="kpi-sub">已注册工作台账号</div>
          </div>
          <div class="kpi-action">工作台用户 →</div>
        </div>
        <div class="kpi-card kpi-green" title="已托管仓库数（managed_repos）" @click="$router.push('/repos/manage')">
          <div class="kpi-icon" style="background:rgba(52,199,89,0.1);color:#34c759;">📦</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ vc.repoTotal || 0 }}</div>
            <div class="kpi-label">托管仓库</div>
            <div class="kpi-sub">已接入 AI 工作流</div>
          </div>
          <div class="kpi-action">代码仓管理 →</div>
        </div>
        <div class="kpi-card kpi-blue" title="在线设备数（status=online）" @click="$router.push('/runtime/devices')">
          <div class="kpi-icon" style="background:rgba(0,107,255,0.08);color:#006eff;">🖥️</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ vc.onlineDevices || 0 }}<span class="kpi-unit">/{{ vc.totalDevices || 0 }}</span></div>
            <div class="kpi-label">在线设备</div>
            <div class="kpi-sub">共 {{ vc.totalDevices || 0 }} 台接入</div>
          </div>
          <div class="kpi-action">设备管理 →</div>
        </div>
        <div class="kpi-card kpi-orange" title="Agent 任务总数（agent_tasks）">
          <div class="kpi-icon" style="background:rgba(255,149,0,0.1);color:#ff9500;">🤖</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ vc.taskTotal || 0 }}</div>
            <div class="kpi-label">Agent 任务</div>
            <div class="kpi-sub">今日 +{{ vc.taskToday || 0 }} · 成功率 {{ vc.taskSuccessRate || '0.0%' }}</div>
          </div>
        </div>
      </div>

      <!-- 数据口径说明（可折叠） -->
      <div class="glossary-wrap">
        <div class="glossary-toggle" @click="showGlossary = !showGlossary">
          📋 数据口径说明 <span class="glossary-arrow">{{ showGlossary ? '▾' : '▸' }}</span>
        </div>
        <div class="glossary-body" v-show="showGlossary">
          <table class="glossary-table">
            <thead>
              <tr><th>指标</th><th>计算规则</th></tr>
            </thead>
            <tbody>
              <tr><td>用户</td><td>工作台用户 user_accounts（grape 工作台登录用户）</td></tr>
              <tr><td>托管仓库</td><td>managed_repos 已托管仓库数</td></tr>
              <tr><td>在线设备</td><td>agent_runtimes 表中 status=online 的设备数（在线 / 总数）</td></tr>
              <tr><td>Agent 任务</td><td>agent_tasks 任务总数；今日新增按创建日期（UTC）统计</td></tr>
              <tr><td>成功率</td><td>已完成任务 ÷ 任务总数</td></tr>
              <tr><td>设备状态图</td><td>agent_runtimes 按 status 分组（在线/离线等）</td></tr>
              <tr><td>任务状态图</td><td>agent_tasks 按 status 分组（completed/queued/dispatched 等）</td></tr>
              <tr><td>仓库事件图</td><td>events 表按 repo_key 聚合的事件量 Top 8（活跃仓库排行）</td></tr>
              <tr><td>用户构成图</td><td>工作台用户（user_accounts）凭证配置分布</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 图表区：4 卡对应 4 图（2×2，对齐 KPI 卡顺序：用户/仓库/设备/任务） -->
      <div class="chart-grid chart-grid-4">
        <div class="chart-card">
          <div class="chart-title">
            <span>工作台用户 · 注册趋势</span>
            <div class="chart-tabs">
              <button :class="{ on: regGranularity === 'year' }" @click="switchRegGranularity('year')">年</button>
              <button :class="{ on: regGranularity === 'month' }" @click="switchRegGranularity('month')">月</button>
              <button :class="{ on: regGranularity === 'day' }" @click="switchRegGranularity('day')">日</button>
            </div>
          </div>
          <div class="chart-body" ref="userCompositionRef"></div>
        </div>
        <div class="chart-card">
          <div class="chart-title">托管仓库 · 任务 Top10</div>
          <div class="chart-body" ref="repoEventRef"></div>
        </div>
        <div class="chart-card">
          <div class="chart-title">在线设备 · 设备状态分布</div>
          <div class="chart-body" ref="deviceStatusRef"></div>
        </div>
        <div class="chart-card">
          <div class="chart-title">任务状态分布</div>
          <div class="chart-body" ref="taskStatusRef"></div>
        </div>
      </div>

    </div>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import http from '../api'
import * as echarts from 'echarts'

const loading = ref(true)
const d = ref({})
const vc = ref({})
const showGlossary = ref(false)
const charts = []
const taskStatusRef = ref(null)
const deviceStatusRef = ref(null)
const repoEventRef = ref(null)
const userCompositionRef = ref(null)
const regGranularity = ref('month')
const regData = ref([])

const COLOR_POOL = ['#006eff', '#34c759', '#ff9500', '#ff3b30', '#5e5ce6', '#30b0c7', '#ff2d92', '#ff9500']
const TASK_STATUS_COLORS = { completed: '#34c759', queued: '#ff9500', dispatched: '#006eff' }
const DEVICE_STATUS_COLORS = { online: '#34c759', offline: '#aeaeb2' }
const USER_COMP_COLORS = { '工作台用户': '#30b0c7' }
const STATUS_LABELS = { active: '有效', revoked: '已吊销', online: '在线', offline: '离线', completed: '已完成', queued: '排队中', dispatched: '已派发' }

function formatAgo(val) {
  if (!val) return ''
  const t = new Date(String(val).replace(' ', 'T'))
  const diff = Math.floor((Date.now() - t.getTime()) / 1000)
  if (isNaN(diff)) return String(val).substring(0, 19)
  if (diff < 60) return '刚刚'
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前'
  if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前'
  return String(val).substring(0, 10)
}

// repo_key 形如 gitcode.com/owner/repo，展示时只留 owner/repo
function shortRepoKey(k) {
  const parts = String(k).split('/')
  return parts.length > 2 ? parts.slice(-2).join('/') : k
}

async function load() {
  loading.value = true
  try {
    const [dRes, vcRes] = await Promise.all([
      http.get('/stats/dashboard'),
      http.get('/stats/overview')
    ])
    d.value = dRes.data || {}
    vc.value = vcRes.data || {}
  } catch (e) {
    console.error('总览加载失败:', e)
    d.value = {}
    vc.value = {}
  }
  loading.value = false
  await loadRegData()
  await new Promise(r => setTimeout(r, 50))
  renderCharts()
}

function renderCharts() {
  charts.forEach(c => c.dispose())
  charts.length = 0
  renderUserComposition()
  renderRepoEvent()
  renderDeviceStatus()
  renderTaskStatus()
}

function handleResize() {
  charts.forEach(c => c.resize())
}

// 设备状态分布 - 饼图（对应"在线设备"卡片）
function renderDeviceStatus() {
  if (!deviceStatusRef.value) return
  const chart = echarts.init(deviceStatusRef.value)
  charts.push(chart)
  const dist = d.value.deviceStatusDistribution || {}
  const data = Object.keys(dist).filter(k => dist[k]).map(k => ({
    name: STATUS_LABELS[k] || k,
    value: dist[k],
    itemStyle: { color: DEVICE_STATUS_COLORS[k] || '#aeaeb2' }
  }))
  chart.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, itemWidth: 10, itemHeight: 10, textStyle: { fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['40%', '70%'], center: ['50%', '45%'],
      label: { show: true, formatter: '{b}\n{d}%', fontSize: 11 },
      data: data.length ? data : [{ name: '暂无数据', value: 0, itemStyle: { color: 'rgba(0,0,0,0.04)' } }],
      emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.2)' } }
    }]
  })
}

// 任务状态分布 - 柱状图（对应"Agent 任务"卡片）
function renderTaskStatus() {
  if (!taskStatusRef.value) return
  const chart = echarts.init(taskStatusRef.value)
  charts.push(chart)
  const dist = d.value.taskStatusDistribution || {}
  const data = Object.keys(dist).map(k => ({
    name: STATUS_LABELS[k] || k,
    value: dist[k],
    itemStyle: { color: TASK_STATUS_COLORS[k] || '#aeaeb2' }
  }))
  chart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 40, right: 16, top: 24, bottom: 32 },
    xAxis: { type: 'category', data: data.map(x => x.name), axisLabel: { fontSize: 11 } },
    yAxis: { type: 'value', minInterval: 1 },
    series: [{
      type: 'bar', data, barMaxWidth: 42,
      label: { show: true, position: 'top', fontSize: 11 },
      itemStyle: { borderRadius: [4, 4, 0, 0] }
    }]
  })
}

// 仓库任务 Top10 - 横向条形图（对应"托管仓库"卡片）
function renderRepoEvent() {
  if (!repoEventRef.value) return
  const chart = echarts.init(repoEventRef.value)
  charts.push(chart)
  const dist = d.value.repoEventDistribution || {}
  const entries = Object.entries(dist)
    .filter(([, v]) => v)
    .sort(([, a], [, b]) => b - a)
  const names = entries.map(([k]) => shortRepoKey(k))
  chart.setOption({
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: params => {
        const p = params[0]
        const full = entries[p.dataIndex] ? entries[p.dataIndex][0] : ''
        return (p.name === '' ? p.name : p.name + (full ? '<br/>' + full : '')) + '<br/>任务数: ' + p.value
      }
    },
    grid: { left: 8, right: 40, top: 16, bottom: 8, containLabel: true },
    xAxis: { type: 'value', minInterval: 1, axisLabel: { fontSize: 11 } },
    yAxis: {
      type: 'category', data: names, inverse: true,
      axisLabel: { fontSize: 11, width: 110, overflow: 'truncate' }
    },
    series: [{
      type: 'bar', data: entries.map(([, v]) => v), barMaxWidth: 18,
      label: { show: true, position: 'right', fontSize: 11 },
      itemStyle: { color: '#34c759', borderRadius: [0, 4, 4, 0] }
    }]
  })
}

// 用户注册趋势 - 柱状图（按年/月/日维度）
async function loadRegData() {
  try {
    const { data } = await http.get('/stats/user-registration', { params: { granularity: regGranularity.value } })
    regData.value = data.data || []
  } catch (e) {
    console.error('[Overview] loadRegData failed', e)
    regData.value = []
  }
}

async function switchRegGranularity(g) {
  regGranularity.value = g
  await loadRegData()
  renderUserComposition()
}

function renderUserComposition() {
  if (!userCompositionRef.value) return
  const chart = echarts.init(userCompositionRef.value)
  charts.push(chart)
  const data = regData.value.map(x => x.count)
  const labels = regData.value.map(x => x.label)
  chart.setOption({
    tooltip: { trigger: 'axis', formatter: p => `${p[0].name}<br/>注册 ${p[0].value} 人` },
    grid: { left: 40, right: 16, top: 24, bottom: 32 },
    xAxis: { type: 'category', data: labels, axisLabel: { fontSize: 11, rotate: labels.length > 6 ? 30 : 0 } },
    yAxis: { type: 'value', minInterval: 1 },
    series: [{
      type: 'bar', data, barMaxWidth: 50,
      label: { show: true, position: 'top', fontSize: 11 },
      itemStyle: { color: '#30b0c7', borderRadius: [4, 4, 0, 0] }
    }]
  })
}

onMounted(() => {
  load()
  window.addEventListener('resize', handleResize)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  charts.forEach(c => c.dispose())
})
</script>

<style scoped>
.refresh-time { font-size: 12px; color: var(--text-tertiary); margin-left: auto; }
.loading-spinner { padding: 60px; text-align: center; color: var(--text-secondary); }

.dashboard { display: flex; flex-direction: column; gap: 20px; padding: 20px 28px; }

/* ---- KPI 卡片 ---- */
.kpi-row { display: flex; gap: 20px; flex-wrap: wrap; }
.kpi-card {
  flex: 1; min-width: 150px;
  background: #fff; border-radius: 14px; border: 1px solid var(--border-soft);
  padding: 20px; cursor: pointer; transition: all var(--transition);
  box-shadow: var(--card-shadow);
  display: flex; align-items: center; gap: 14px;
}
.kpi-card:hover { box-shadow: var(--card-shadow-hover); transform: translateY(-2px); }
.kpi-blue { border-left: 3px solid #006eff; }
.kpi-purple { border-left: 3px solid #5e5ce6; }
.kpi-pink { border-left: 3px solid #ff2d92; }
.kpi-orange { border-left: 3px solid #ff9500; }
.kpi-green { border-left: 3px solid #34c759; }
.kpi-cyan { border-left: 3px solid #30b0c7; }
.kpi-icon { width: 36px; height: 36px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 18px; flex-shrink: 0; }
.kpi-body { flex: 1; min-width: 0; }
.kpi-value { font-size: 26px; font-weight: 700; color: var(--text-primary); line-height: 1.2; }
.kpi-unit { font-size: 14px; font-weight: 500; color: var(--text-tertiary); }
.kpi-label { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
.kpi-sub { font-size: 11px; color: var(--text-tertiary); margin-top: 2px; line-height: 1.4; }
.kpi-action { font-size: 11px; color: var(--primary); white-space: nowrap; }

/* ---- 数据口径说明 ---- */
.glossary-wrap {
  background: #fff; border-radius: 14px; border: 1px solid var(--border-soft);
  box-shadow: var(--card-shadow);
}
.glossary-toggle {
  padding: 10px 18px; font-size: 13px; color: var(--text-secondary);
  cursor: pointer; user-select: none; display: flex; align-items: center; gap: 6px;
}
.glossary-toggle:hover { color: var(--primary); }
.glossary-arrow { font-size: 11px; color: var(--text-tertiary); }
.glossary-body { padding: 0 18px 16px; }
.glossary-table {
  width: 100%; border-collapse: collapse; font-size: 12px; color: var(--text-secondary);
}
.glossary-table th {
  text-align: left; padding: 6px 12px; background: #fafafa; font-weight: 600;
  border-bottom: 1px solid var(--border-color); color: var(--text-primary);
}
.glossary-table td {
  padding: 6px 12px; border-bottom: 1px solid var(--border-soft);
}
.glossary-table tr:last-child td { border-bottom: none; }
.glossary-table td:first-child { width: 120px; color: var(--text-primary); font-weight: 500; }

/* ---- 图表区 ---- */
.chart-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
.chart-grid-4 { grid-template-columns: repeat(2, 1fr); }
.chart-card {
  background: #fff; border-radius: 14px; border: 1px solid var(--border-soft);
  box-shadow: var(--card-shadow); overflow: hidden;
}
.chart-title { font-size: 14px; font-weight: 600; padding: 14px 18px 0; color: var(--text-primary); display: flex; align-items: center; justify-content: space-between; }
.chart-tabs { display: flex; gap: 2px; }
.chart-tabs button { font-size: 11px; padding: 2px 8px; border: 1px solid var(--border-color); border-radius: 6px; background: transparent; color: var(--text-secondary); cursor: pointer; transition: all var(--transition); }
.chart-tabs button.on { background: var(--primary); color: #fff; border-color: var(--primary); }
.chart-body { width: 100%; height: 260px; padding: 8px; }

@media (max-width: 1024px) {
  .chart-grid-4 { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 768px) {
  .dashboard { padding: 12px; gap: 12px; }
  .kpi-card { min-width: 120px; padding: 10px 12px; }
  .kpi-value { font-size: 20px; }
  .chart-body { height: 200px; }
}
@media (max-width: 480px) {
  .chart-grid, .chart-grid-4 { grid-template-columns: 1fr; }
  .kpi-row { gap: 6px; }
  .kpi-card { min-width: calc(50% - 6px); flex: none; gap: 8px; padding: 8px 10px; }
  .kpi-value { font-size: 16px; }
  .kpi-action { display: none; }
  .chart-body { height: 180px; }
}
</style>
