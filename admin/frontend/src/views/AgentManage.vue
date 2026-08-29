<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>智能体管理</h1>
      <span class="badge">{{ total }} 个智能体</span>
      <span class="refresh-time" v-if="loaded">已启用 {{ enabledCount }} · 共 {{ totalTasks }} 个任务</span>
      <button class="btn" style="margin-left:12px" @click="load">刷新</button>
    </div>

    <div style="padding:20px 28px">
      <!-- 分类筛选徽章：全部 / 官方 / 仓库 / 个人 -->
      <div class="dm-stats">
        <button class="dm-stat" :class="{ on: filter === '' }" @click="filter = ''">全部</button>
        <button class="dm-stat" :class="{ on: filter === 'official' }" @click="filter = 'official'"><span class="dm-dot" style="background:#006eff"></span>官方 <strong>{{ officialCount }}</strong></button>
        <button class="dm-stat" :class="{ on: filter === 'repo' }" @click="filter = 'repo'"><span class="dm-dot" style="background:#34c759"></span>仓库 <strong>{{ repoAgentCount }}</strong></button>
        <button class="dm-stat" :class="{ on: filter === 'personal' }" @click="filter = 'personal'"><span class="dm-dot" style="background:#ff9500"></span>个人 <strong>0</strong></button>
        <span class="dm-hint" v-if="onlineRuntimes">负载口径：进行中任务 ÷ (在线引擎 {{ onlineRuntimes }} × {{ maxConcurrent }} 并发)</span>
      </div>

      <!-- 官方：智能体卡片墙（点击卡片进入详情） -->
      <template v-if="showOfficial">
        <div class="section-label"><span class="section-dot" style="background:#006eff"></span>官方智能体 <span class="t3">（平台内置，点击卡片查看详情）</span></div>
        <div class="agent-grid">
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
              <span class="cat-tag cat-official">官方</span>
              <span class="detail-link">查看详情 →</span>
            </div>
          </div>
        </div>
      </template>

      <!-- 仓库：仓库级智能体实例表格 -->
      <template v-if="showRepo">
        <div class="section-label"><span class="section-dot" style="background:#34c759"></span>仓库智能体 <span class="t3">（devops_agent_session 按仓库 × 智能体的实例）</span></div>
        <div class="card" style="overflow-x:auto">
          <table class="vod-table" v-if="repoAgents.length">
            <thead>
              <tr><th>智能体</th><th>仓库</th><th>状态</th><th>启用</th><th>引擎 ID</th><th>最后活跃</th></tr>
            </thead>
            <tbody>
              <tr v-for="r in repoAgents" :key="r.id">
                <td><span class="code-tag">{{ r.agentType }}</span></td>
                <td style="max-width:320px">{{ r.repoKey }}</td>
                <td><span :class="['status-tag', r.status === 'idle' ? 'default' : 'success']">{{ r.status }}</span></td>
                <td>{{ r.enable === 1 ? '✅' : '⛔' }}</td>
                <td class="t3">{{ r.runtimeId || '-' }}</td>
                <td class="t3">{{ fmtTime(r.lastActiveAt) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="t2" style="padding:20px;text-align:center">暂无仓库级智能体实例</p>
        </div>
      </template>

      <!-- 个人：预留空态 -->
      <template v-if="showPersonal">
        <div class="section-label"><span class="section-dot" style="background:#ff9500"></span>个人智能体 <span class="t3">（预留分类，暂未开放）</span></div>
        <div class="empty-state">
          <div class="empty-icon">👤</div>
          <p>个人智能体功能尚未开放，此分类为预留占位</p>
        </div>
      </template>

      <!-- 任务分布图（对齐总览 chart-card 风格） -->
      <div class="chart-card" style="margin-top:18px">
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

const STATUS_COLOR = { queued: '#ff9500', running: '#006eff', completed: '#34c759', failed: '#ff2d92' }
const STATUS_ORDER = ['queued', 'running', 'completed', 'failed']
const accents = ['kpi-blue', 'kpi-purple', 'kpi-pink', 'kpi-orange', 'kpi-green']
const iconPalette = [
  { background: 'rgba(0,107,255,0.08)', color: '#006eff' },
  { background: '#f0e6ff', color: '#7222ed' },
  { background: 'rgba(255,45,146,0.08)', color: '#ff2d92' },
  { background: 'rgba(255,149,0,0.1)', color: '#ff9500' },
  { background: 'rgba(52,199,89,0.1)', color: '#34c759' }
]

const agents = ref([])
const repoAgents = ref([])
const loaded = ref(false)
const filter = ref('')
const saving = ref('')
const onlineRuntimes = ref(0)
const maxConcurrent = ref(20)
const distRef = ref(null)
let chart = null

const total = computed(() => agents.value.length)
const enabledCount = computed(() => agents.value.filter(a => a.enable === 1).length)
const totalTasks = computed(() => agents.value.reduce((s, a) => s + (a.taskCount || 0), 0))
const officialCount = computed(() => agents.value.length)
const repoAgentCount = computed(() => repoAgents.value.length)
const showOfficial = computed(() => filter.value === '' || filter.value === 'official')
const showRepo = computed(() => filter.value === '' || filter.value === 'repo')
const showPersonal = computed(() => filter.value === '' || filter.value === 'personal')

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
function fmtTime(v) {
  if (!v) return '—'
  return String(v).replace('T', ' ').substring(0, 19)
}

async function load() {
  try {
    const { data } = await http.get('/agents')
    agents.value = (data.agents || []).map(a => ({ ...a, statusBreakdown: a.statusBreakdown || {} }))
    repoAgents.value = data.repoAgents || []
    onlineRuntimes.value = data.onlineRuntimes || 0
    maxConcurrent.value = data.maxConcurrentPerRuntime || 20
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
.dm-stats { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; align-items: center; }
.dm-stat {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; padding: 3px 10px; border-radius: 12px;
  background: rgba(0,0,0,0.04); color: var(--text-secondary); border: 1px solid transparent;
  cursor: pointer; transition: all var(--transition);
}
.dm-stat.on { background: #e6f4ff; color: #006eff; border-color: #91caff; }
.dm-stat strong { font-size: 13px; }
.dm-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.dm-hint { font-size: 11px; color: var(--text-tertiary); margin-left: auto; }

.section-label {
  display: flex; align-items: center; gap: 8px;
  font-size: 14px; font-weight: 600; color: var(--text-primary); margin: 6px 0 12px;
}
.section-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }

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
.load-unit { font-size: 11px; font-weight: 500; color: var(--text-tertiary); margin-left: 1px; }
.as-item span { font-size: 11px; color: var(--text-tertiary); }

.sb-row { display: flex; gap: 6px; flex-wrap: wrap; }
.sb-seg { font-size: 11px; color: #fff; border-radius: 8px; padding: 1px 7px; }

.agent-foot { display: flex; align-items: center; justify-content: space-between; border-top: 1px solid var(--border-soft); padding-top: 8px; }
.cat-tag { font-size: 11px; padding: 1px 8px; border-radius: 14px; }
.cat-official { background: #e6f4ff; color: #006eff; }
.detail-link { font-size: 12px; color: var(--primary); }

.sw { position: relative; display: inline-block; width: 38px; height: 21px; flex-shrink: 0; cursor: pointer; }
.sw input { opacity: 0; width: 0; height: 0; }
.sw-slider {
  position: absolute; inset: 0; background: #c0c4cc;
  border: 1px solid rgba(0,0,0,0.15); border-radius: 20px; transition: all var(--transition);
}
.sw-slider::before {
  content: ''; position: absolute; width: 15px; height: 15px; left: 2px; top: 2px;
  background: #fff; border-radius: 50%; transition: all var(--transition);
  box-shadow: var(--card-shadow); border: 1px solid var(--border-color);
}
.sw input:checked + .sw-slider { background: #34c759; border-color: #34c759; }
.sw input:checked + .sw-slider::before { transform: translateX(17px); }
.sw input:disabled + .sw-slider { opacity: .5; cursor: not-allowed; }

.card { background: #fff; border: 1px solid var(--border-color); border-radius: 14px; box-shadow: var(--card-shadow); padding: 14px 18px; margin-bottom: 18px; }
.vod-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.vod-table th { text-align: left; padding: 8px 10px; background: #fafafa; font-weight: 600; color: var(--text-secondary); border-bottom: 1px solid var(--border-color); white-space: nowrap; }
.vod-table td { padding: 8px 10px; border-bottom: 1px solid var(--border-soft); }
.status-tag { font-size: 11px; padding: 1px 8px; border-radius: 14px; }
.status-tag.success { background: rgba(52,199,89,0.1); color: #34c759; }
.status-tag.default { background: rgba(0,0,0,0.04); color: var(--text-tertiary); }

.empty-state { border: 1px dashed var(--border-color); border-radius: 14px; padding: 36px; text-align: center; background: #fff; margin-bottom: 18px; }
.empty-icon { font-size: 32px; margin-bottom: 8px; }
.empty-state p { font-size: 13px; color: var(--text-tertiary); }

.chart-card { background: #fff; border-radius: 14px; border: 1px solid var(--border-color); box-shadow: var(--card-shadow); overflow: hidden; }
.chart-title { padding: 14px 18px 0; font-size: 14px; font-weight: 600; color: var(--text-primary); }
.chart-body { padding: 10px 14px 14px; }
</style>
