<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>智能体管理</h1>
      <span class="badge">{{ total }} 个智能体</span>
      <span class="refresh-time" v-if="loaded">已启用 {{ enabledCount }} · 共 {{ totalTasks }} 个任务</span>
      <button class="btn" style="margin-left:12px" @click="load">刷新</button>
    </div>

    <div style="padding:20px 28px">
      <!-- 徽章筛选行（对齐设备页 dm-stats 风格） -->
      <div class="dm-stats">
        <button class="dm-stat" :class="{ on: filter === '' }" @click="filter = ''">全部 <strong>{{ total }}</strong></button>
        <button class="dm-stat" :class="{ on: filter === 1 }" @click="filter = 1"><span class="dm-dot" style="background:#52c41a"></span>已启用 <strong>{{ enabledCount }}</strong></button>
        <button class="dm-stat" :class="{ on: filter === 0 }" @click="filter = 0"><span class="dm-dot" style="background:#fa8c16"></span>已禁用 <strong>{{ total - enabledCount }}</strong></button>
      </div>

      <!-- 智能体卡片墙 -->
      <div class="agent-grid">
        <div v-for="(a, i) in visibleAgents" :key="a.agentType" class="agent-card" :class="accents[i % accents.length]">
          <div class="agent-head">
            <div class="agent-icon" :style="iconStyle(i)">{{ a.icon || '🤖' }}</div>
            <div class="agent-meta">
              <div class="agent-name">{{ a.displayName }}<span class="code-tag">{{ a.agentType }}</span></div>
              <div class="agent-sub">顺序 #{{ a.orderNum }} · 引擎 {{ a.defaultRuntimeId || '-' }}</div>
            </div>
            <label class="sw" :title="a.enable === 1 ? '点击禁用' : '点击启用'">
              <input type="checkbox" :checked="a.enable === 1" :disabled="saving === a.agentType" @change="toggle(a, $event.target.checked)" />
              <span class="sw-slider"></span>
            </label>
          </div>
          <div class="agent-desc">{{ a.description }}</div>
          <div class="agent-stats">
            <div class="as-item"><strong>{{ a.taskCount || 0 }}</strong><span>任务</span></div>
            <div class="as-item"><strong :style="{ color: a.enable === 1 ? '#52c41a' : '#fa8c16' }">{{ a.enable === 1 ? '启用' : '禁用' }}</strong><span>状态</span></div>
            <div class="as-item"><strong>{{ eventCount(a) }}</strong><span>订阅事件</span></div>
          </div>
          <div class="sb-row" v-if="hasBreakdown(a)">
            <span v-for="(n, st) in a.statusBreakdown" :key="st" class="sb-seg" :style="{ background: STATUS_COLOR[st] || '#8c8c8c' }">{{ st }} {{ n }}</span>
          </div>
        </div>
      </div>

      <!-- 任务分布图（对齐总览 chart-card 风格） -->
      <div class="chart-card">
        <div class="chart-title">智能体任务分布（按状态堆叠）</div>
        <div class="chart-body" ref="distRef" style="height:280px"></div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import * as echarts from 'echarts'
import http from '../api'

const STATUS_COLOR = { queued: '#fa8c16', running: '#006eff', completed: '#52c41a', failed: '#eb2f96' }
const STATUS_ORDER = ['queued', 'running', 'completed', 'failed']
const accents = ['kpi-blue', 'kpi-purple', 'kpi-pink', 'kpi-orange', 'kpi-green']
const iconPalette = [
  { background: '#e8f0fe', color: '#006eff' },
  { background: '#f0e6ff', color: '#7222ed' },
  { background: '#fff0f6', color: '#eb2f96' },
  { background: '#fff7e6', color: '#fa8c16' },
  { background: '#f6ffed', color: '#52c41a' }
]

const agents = ref([])
const loaded = ref(false)
const filter = ref('')
const saving = ref('')
const distRef = ref(null)
let chart = null

const total = computed(() => agents.value.length)
const enabledCount = computed(() => agents.value.filter(a => a.enable === 1).length)
const totalTasks = computed(() => agents.value.reduce((s, a) => s + (a.taskCount || 0), 0))
const visibleAgents = computed(() => filter.value === '' ? agents.value : agents.value.filter(a => a.enable === filter.value))

function iconStyle(i) { return iconPalette[i % iconPalette.length] }
function eventCount(a) {
  try { return (JSON.parse(a.subscribeEvents || '[]') || []).length } catch { return 0 }
}
function hasBreakdown(a) { return a.statusBreakdown && Object.keys(a.statusBreakdown).length }

async function load() {
  try {
    const { data } = await http.get('/agents')
    agents.value = (data.agents || []).map(a => ({ ...a, statusBreakdown: a.statusBreakdown || {} }))
    loaded.value = true
    await nextTick()
    renderChart(data.taskStatusBreakdown || {})
  } catch (e) {
    console.error('智能体管理加载失败:', e)
  }
}

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

function renderChart(breakdown) {
  if (!distRef.value) return
  if (!chart) chart = echarts.init(distRef.value)
  const types = Object.keys(breakdown)
  const statuses = STATUS_ORDER.filter(s => types.some(t => breakdown[t][s]))
  chart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { bottom: 0, data: statuses },
    grid: { left: 40, right: 16, top: 16, bottom: 34 },
    xAxis: { type: 'category', data: types, axisLabel: { fontSize: 12 } },
    yAxis: { type: 'value', minInterval: 1 },
    series: statuses.map(s => ({
      name: s,
      type: 'bar',
      stack: 'total',
      barMaxWidth: 42,
      itemStyle: { color: STATUS_COLOR[s] },
      data: types.map(t => breakdown[t][s] || 0)
    }))
  }, true)
}

function onResize() { chart && chart.resize() }

onMounted(() => { load(); window.addEventListener('resize', onResize) })
onBeforeUnmount(() => { window.removeEventListener('resize', onResize); chart && chart.dispose() })
</script>

<style scoped>
.dm-stats { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
.dm-stat {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; padding: 3px 10px; border-radius: 12px;
  background: #f5f7fa; color: var(--text-secondary); border: 1px solid transparent;
  cursor: pointer; transition: all 0.15s;
}
.dm-stat.on { background: #e6f4ff; color: #006eff; border-color: #91caff; }
.dm-stat strong { font-size: 13px; }
.dm-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }

.agent-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; margin-bottom: 18px; }
.agent-card {
  background: #fff; border: 1px solid var(--border-color); border-radius: 10px;
  box-shadow: var(--card-shadow); padding: 14px 16px; border-left: 3px solid #006eff;
  display: flex; flex-direction: column; gap: 10px;
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

.chart-card { background: #fff; border-radius: 10px; border: 1px solid var(--border-color); box-shadow: var(--card-shadow); overflow: hidden; }
.chart-title { padding: 14px 18px 0; font-size: 14px; font-weight: 600; color: var(--text-primary); }
.chart-body { padding: 10px 14px 14px; }
</style>
