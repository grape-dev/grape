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

      <!-- KPI 卡片行 -->
      <div class="kpi-row">
        <div class="kpi-card kpi-blue" title="在线设备数（status=online）" @click="$router.push('/runtime/devices')">
          <div class="kpi-icon" style="background:#e8f0fe;color:#006eff;">🖥️</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ vc.onlineDevices || 0 }}<span class="kpi-unit">/{{ vc.totalDevices || 0 }}</span></div>
            <div class="kpi-label">在线设备</div>
            <div class="kpi-sub">共 {{ vc.totalDevices || 0 }} 台接入</div>
          </div>
          <div class="kpi-action">设备管理 →</div>
        </div>
        <div class="kpi-card kpi-purple" title="Agent 引擎类型数（agent_type）" @click="$router.push('/runtime/engines')">
          <div class="kpi-icon" style="background:#f0e6ff;color:#7222ed;">⚙️</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ vc.engineTotal || 0 }}</div>
            <div class="kpi-label">引擎</div>
            <div class="kpi-sub">启用 {{ vc.engineEnabled || 0 }} 种</div>
          </div>
          <div class="kpi-action">引擎管理 →</div>
        </div>
        <div class="kpi-card kpi-pink" title="运行时凭据总数（runtime_credentials）" @click="$router.push('/runtime/credentials')">
          <div class="kpi-icon" style="background:#fff0f6;color:#eb2f96;">🔑</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ vc.credentialTotal || 0 }}</div>
            <div class="kpi-label">凭据</div>
            <div class="kpi-sub">有效 {{ vc.credentialActive || 0 }} 条</div>
          </div>
          <div class="kpi-action">凭据管理 →</div>
        </div>
        <div class="kpi-card kpi-orange" title="Agent 任务总数（agent_tasks）">
          <div class="kpi-icon" style="background:#fff7e6;color:#fa8c16;">🤖</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ vc.taskTotal || 0 }}</div>
            <div class="kpi-label">Agent 任务</div>
            <div class="kpi-sub">今日 +{{ vc.taskToday || 0 }} · 成功率 {{ vc.taskSuccessRate || '0.0%' }}</div>
          </div>
        </div>
        <div class="kpi-card kpi-green" title="已托管仓库数（managed_repos）" @click="$router.push('/repos/manage')">
          <div class="kpi-icon" style="background:#f6ffed;color:#52c41a;">📦</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ vc.repoTotal || 0 }}</div>
            <div class="kpi-label">托管仓库</div>
            <div class="kpi-sub">已接入 AI 工作流</div>
          </div>
          <div class="kpi-action">代码仓管理 →</div>
        </div>
        <div class="kpi-card kpi-cyan" title="管理台用户数（admin_users）" @click="$router.push('/sys/users')">
          <div class="kpi-icon" style="background:#e6fffb;color:#13c2c2;">👥</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ vc.adminUserTotal || 0 }}</div>
            <div class="kpi-label">用户</div>
            <div class="kpi-sub">平台用户 {{ vc.platformUserTotal || 0 }}</div>
          </div>
          <div class="kpi-action">用户管理 →</div>
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
              <tr><td>在线设备</td><td>agent_runtimes 表中 status=online 的设备数（在线 / 总数）</td></tr>
              <tr><td>引擎</td><td>agent_type 引擎类型总数（启用 / 全部）</td></tr>
              <tr><td>凭据</td><td>runtime_credentials 凭据总数（有效 status=active / 全部）</td></tr>
              <tr><td>Agent 任务</td><td>agent_tasks 任务总数；今日新增按创建日期（UTC）统计</td></tr>
              <tr><td>成功率</td><td>已完成任务 ÷ 任务总数</td></tr>
              <tr><td>托管仓库</td><td>managed_repos 已托管仓库数</td></tr>
              <tr><td>用户</td><td>管理台用户 admin_users；平台用户 user_accounts</td></tr>
              <tr><td>设备状态图</td><td>agent_runtimes 按 status 分组（在线/离线等）</td></tr>
              <tr><td>引擎启停图</td><td>agent_type 按 enable 分组（启用/停用）</td></tr>
              <tr><td>凭据状态图</td><td>runtime_credentials 按 status 分组（有效 active / 已吊销 revoked）</td></tr>
              <tr><td>任务状态图</td><td>agent_tasks 按 status 分组（completed/queued/dispatched 等）</td></tr>
              <tr><td>仓库事件图</td><td>events 表按 repo_key 聚合的事件量 Top 8（活跃仓库排行）</td></tr>
              <tr><td>用户构成图</td><td>管理台用户（admin_users）与平台用户（user_accounts）数量对比</td></tr>
              <tr><td>事件趋势</td><td>events 表按天聚合的 Webhook 事件量（近 14 天）</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 图表区：6 卡对应 6 图（3×2） -->
      <div class="chart-grid chart-grid-6">
        <div class="chart-card">
          <div class="chart-title">在线设备 · 设备状态分布</div>
          <div class="chart-body" ref="deviceStatusRef"></div>
        </div>
        <div class="chart-card">
          <div class="chart-title">引擎 · 启停分布</div>
          <div class="chart-body" ref="engineStatusRef"></div>
        </div>
        <div class="chart-card">
          <div class="chart-title">凭据 · 状态分布</div>
          <div class="chart-body" ref="credentialStatusRef"></div>
        </div>
        <div class="chart-card">
          <div class="chart-title">Agent 任务 · 状态分布</div>
          <div class="chart-body" ref="taskStatusRef"></div>
        </div>
        <div class="chart-card">
          <div class="chart-title">托管仓库 · 事件 Top8</div>
          <div class="chart-body" ref="repoEventRef"></div>
        </div>
        <div class="chart-card">
          <div class="chart-title">用户 · 构成分布</div>
          <div class="chart-body" ref="userCompositionRef"></div>
        </div>
      </div>

      <!-- 事件监控附加区 -->
      <div class="event-section">
        <div class="section-title">事件监控</div>
        <div class="chart-grid">
          <div class="chart-card">
            <div class="chart-title">事件类型分布</div>
            <div class="chart-body" ref="eventTypeRef"></div>
          </div>
          <div class="chart-card">
            <div class="chart-title">近 14 天事件趋势</div>
            <div class="chart-body" ref="eventTrendRef"></div>
          </div>
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
const eventTypeRef = ref(null)
const eventTrendRef = ref(null)
const deviceStatusRef = ref(null)
const engineStatusRef = ref(null)
const credentialStatusRef = ref(null)
const repoEventRef = ref(null)
const userCompositionRef = ref(null)

const COLOR_POOL = ['#006eff', '#52c41a', '#faad14', '#f5222d', '#722ed1', '#13c2c2', '#eb2f96', '#fa8c16']
const TASK_STATUS_COLORS = { completed: '#52c41a', queued: '#faad14', dispatched: '#006eff' }
const DEVICE_STATUS_COLORS = { online: '#52c41a', offline: '#d9d9d9' }
const CREDENTIAL_STATUS_COLORS = { active: '#52c41a', revoked: '#faad14' }
const USER_COMP_COLORS = { '管理台用户': '#006eff', '平台用户': '#13c2c2' }
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
  await new Promise(r => setTimeout(r, 50))
  renderCharts()
}

function renderCharts() {
  charts.forEach(c => c.dispose())
  charts.length = 0
  renderDeviceStatus()
  renderEngineStatus()
  renderCredentialStatus()
  renderTaskStatus()
  renderRepoEvent()
  renderUserComposition()
  renderEventType()
  renderEventTrend()
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
    itemStyle: { color: DEVICE_STATUS_COLORS[k] || '#d9d9d9' }
  }))
  chart.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, itemWidth: 10, itemHeight: 10, textStyle: { fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['40%', '70%'], center: ['50%', '45%'],
      label: { show: true, formatter: '{b}\n{d}%', fontSize: 11 },
      data: data.length ? data : [{ name: '暂无数据', value: 0, itemStyle: { color: '#f0f0f0' } }],
      emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.2)' } }
    }]
  })
}

// 引擎启停分布 - 环形图（对应"引擎"卡片）
function renderEngineStatus() {
  if (!engineStatusRef.value) return
  const chart = echarts.init(engineStatusRef.value)
  charts.push(chart)
  const dist = d.value.engineEnabledDistribution || {}
  const data = Object.keys(dist).filter(k => dist[k]).map((k, i) => ({
    name: k,
    value: dist[k],
    itemStyle: { color: k === '启用' ? '#7222ed' : (COLOR_POOL[i % COLOR_POOL.length]) }
  }))
  chart.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, itemWidth: 10, itemHeight: 10, textStyle: { fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['40%', '70%'], center: ['50%', '45%'],
      label: { show: true, formatter: '{b}\n{c}', fontSize: 11 },
      data: data.length ? data : [{ name: '暂无数据', value: 0, itemStyle: { color: '#f0f0f0' } }],
      emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.2)' } }
    }]
  })
}

// 凭据状态分布 - 环形图（对应"凭据"卡片）
function renderCredentialStatus() {
  if (!credentialStatusRef.value) return
  const chart = echarts.init(credentialStatusRef.value)
  charts.push(chart)
  const dist = d.value.credentialStatusDistribution || {}
  const data = Object.keys(dist).filter(k => dist[k]).map(k => ({
    name: STATUS_LABELS[k] || k,
    value: dist[k],
    itemStyle: { color: CREDENTIAL_STATUS_COLORS[k] || '#d9d9d9' }
  }))
  chart.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, itemWidth: 10, itemHeight: 10, textStyle: { fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['40%', '70%'], center: ['50%', '45%'],
      label: { show: true, formatter: '{b}\n{c}', fontSize: 11 },
      data: data.length ? data : [{ name: '暂无数据', value: 0, itemStyle: { color: '#f0f0f0' } }],
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
    itemStyle: { color: TASK_STATUS_COLORS[k] || '#d9d9d9' }
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

// 仓库事件 Top8 - 横向条形图（对应"托管仓库"卡片）
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
        return (p.name === '' ? p.name : p.name + (full ? '<br/>' + full : '')) + '<br/>事件数: ' + p.value
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
      itemStyle: { color: '#52c41a', borderRadius: [0, 4, 4, 0] }
    }]
  })
}

// 用户构成分布 - 柱状图（对应"用户"卡片）
function renderUserComposition() {
  if (!userCompositionRef.value) return
  const chart = echarts.init(userCompositionRef.value)
  charts.push(chart)
  const dist = d.value.userCompositionDistribution || {}
  const data = Object.keys(dist).map(k => ({
    name: k,
    value: dist[k],
    itemStyle: { color: USER_COMP_COLORS[k] || '#006eff' }
  }))
  chart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 40, right: 16, top: 24, bottom: 32 },
    xAxis: { type: 'category', data: data.map(x => x.name), axisLabel: { fontSize: 11 } },
    yAxis: { type: 'value', minInterval: 1 },
    series: [{
      type: 'bar', data, barMaxWidth: 60,
      label: { show: true, position: 'top', fontSize: 11 },
      itemStyle: { borderRadius: [4, 4, 0, 0] }
    }]
  })
}

// 事件类型分布 - 环形图（事件监控区）
function renderEventType() {
  if (!eventTypeRef.value) return
  const chart = echarts.init(eventTypeRef.value)
  charts.push(chart)
  const dist = d.value.eventTypeDistribution || {}
  const data = Object.keys(dist).filter(k => dist[k]).map((k, i) => ({
    name: k, value: dist[k], itemStyle: { color: COLOR_POOL[i % COLOR_POOL.length] }
  }))
  chart.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, itemWidth: 10, itemHeight: 10, textStyle: { fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['40%', '70%'], center: ['50%', '45%'],
      label: { show: true, formatter: '{b}\n{c}', fontSize: 11 },
      data: data.length ? data : [{ name: '暂无数据', value: 0, itemStyle: { color: '#f0f0f0' } }],
      emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.2)' } }
    }]
  })
}

// 近 14 天事件趋势 - 折线图（事件监控区）
function renderEventTrend() {
  if (!eventTrendRef.value) return
  const chart = echarts.init(eventTrendRef.value)
  charts.push(chart)
  const dist = d.value.eventTrend || {}
  const entries = Object.entries(dist).sort(([a], [b]) => (a < b ? -1 : 1))
  chart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 40, right: 16, top: 24, bottom: 32 },
    xAxis: { type: 'category', data: entries.map(([k]) => k.substring(5)), axisLabel: { fontSize: 11 } },
    yAxis: { type: 'value', minInterval: 1 },
    series: [{
      type: 'line', smooth: true, symbolSize: 6,
      data: entries.map(([, v]) => v),
      lineStyle: { color: '#006eff', width: 2 },
      itemStyle: { color: '#006eff' },
      areaStyle: { color: 'rgba(0,110,255,0.08)' },
      label: { show: true, position: 'top', fontSize: 10 }
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
.kpi-row { display: flex; gap: 12px; flex-wrap: wrap; }
.kpi-card {
  flex: 1; min-width: 150px;
  background: #fff; border-radius: 10px; border: 1px solid var(--border-color);
  padding: 16px 18px; cursor: pointer; transition: all 0.2s;
  box-shadow: var(--card-shadow);
  display: flex; align-items: center; gap: 12px;
}
.kpi-card:hover { box-shadow: var(--card-shadow-hover); transform: translateY(-1px); }
.kpi-blue { border-left: 3px solid #006eff; }
.kpi-purple { border-left: 3px solid #7222ed; }
.kpi-pink { border-left: 3px solid #eb2f96; }
.kpi-orange { border-left: 3px solid #fa8c16; }
.kpi-green { border-left: 3px solid #52c41a; }
.kpi-cyan { border-left: 3px solid #13c2c2; }
.kpi-icon { width: 36px; height: 36px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 18px; flex-shrink: 0; }
.kpi-body { flex: 1; min-width: 0; }
.kpi-value { font-size: 26px; font-weight: 700; color: var(--text-primary); line-height: 1.2; }
.kpi-unit { font-size: 14px; font-weight: 500; color: var(--text-tertiary); }
.kpi-label { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
.kpi-sub { font-size: 11px; color: var(--text-tertiary); margin-top: 2px; line-height: 1.4; }
.kpi-action { font-size: 11px; color: var(--primary); white-space: nowrap; }

/* ---- 数据口径说明 ---- */
.glossary-wrap {
  background: #fff; border-radius: 10px; border: 1px solid var(--border-color);
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
  padding: 6px 12px; border-bottom: 1px solid #f0f0f0;
}
.glossary-table tr:last-child td { border-bottom: none; }
.glossary-table td:first-child { width: 120px; color: var(--text-primary); font-weight: 500; }

/* ---- 图表区 ---- */
.chart-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
.chart-grid-6 { grid-template-columns: repeat(3, 1fr); }
.chart-card {
  background: #fff; border-radius: 10px; border: 1px solid var(--border-color);
  box-shadow: var(--card-shadow); overflow: hidden;
}
.chart-title { font-size: 14px; font-weight: 600; padding: 14px 18px 0; color: var(--text-primary); }
.chart-body { width: 100%; height: 260px; padding: 8px; }

/* ---- 事件监控附加区 ---- */
.event-section { display: flex; flex-direction: column; gap: 12px; }
.section-title {
  font-size: 15px; font-weight: 600; color: var(--text-primary);
  padding-left: 4px; border-left: 3px solid var(--primary); padding-left: 10px;
}

@media (max-width: 1024px) {
  .chart-grid-6 { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 768px) {
  .dashboard { padding: 12px; gap: 12px; }
  .kpi-card { min-width: 120px; padding: 10px 12px; }
  .kpi-value { font-size: 20px; }
  .chart-body { height: 200px; }
}
@media (max-width: 480px) {
  .chart-grid, .chart-grid-6 { grid-template-columns: 1fr; }
  .kpi-row { gap: 6px; }
  .kpi-card { min-width: calc(50% - 6px); flex: none; gap: 8px; padding: 8px 10px; }
  .kpi-value { font-size: 16px; }
  .kpi-action { display: none; }
  .chart-body { height: 180px; }
}
</style>
