<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <span class="back-btn" @click="goBack">
        <svg class="back-arrow" viewBox="0 0 24 24" width="16" height="16"><path d="M15 18l-6-6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        返回
      </span>
      <h1>智能体详情</h1>
      <span class="badge">{{ a?.agentType }}</span>
      <span class="t3" style="font-size:12px">{{ a?.displayName }}</span>
    </div>

    <div style="padding:20px 28px" v-if="a">
      <!-- 基本信息卡 -->
      <div class="card">
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:14px">
          <div class="agent-icon" :style="iconStyle">{{ a.icon || '🤖' }}</div>
          <div style="flex:1;min-width:0">
            <div style="font-size:17px;font-weight:700;display:flex;align-items:center;gap:8px">
              {{ a.displayName }}<span class="code-tag">{{ a.agentType }}</span>
              <span class="cat-tag cat-official">官方</span>
            </div>
            <div class="t3" style="margin-top:4px">顺序 #{{ a.orderNum }} · 默认引擎 {{ a.defaultRuntimeId || '-' }} · 订阅事件 {{ subscribeCount }}</div>
          </div>
          <label class="sw" :title="a.enable === 1 ? '点击禁用' : '点击启用'">
            <input type="checkbox" :checked="a.enable === 1" :disabled="saving" @change="toggle($event.target.checked)" />
            <span class="sw-slider"></span>
          </label>
        </div>
        <p class="desc">{{ a.description }}</p>
      </div>

      <!-- 指标卡 -->
      <div class="kpi-row" style="margin-top:14px">
        <div class="kpi-card kpi-blue">
          <div class="kpi-icon" style="background:#e8f0fe;color:#006eff;">💬</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ a.sessionCount ?? 0 }}</div>
            <div class="kpi-label">会话数</div>
            <div class="kpi-sub">含 issue 会话 {{ a.issueSessionCount ?? 0 }}</div>
          </div>
        </div>
        <div class="kpi-card kpi-green">
          <div class="kpi-icon" style="background:#f6ffed;color:#52c41a;">📦</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ a.repoUsageCount ?? 0 }}</div>
            <div class="kpi-label">使用仓库</div>
            <div class="kpi-sub">devops 仓库级实例数</div>
          </div>
        </div>
        <div class="kpi-card" :class="a.loadLevel === 'over' ? 'kpi-pink' : 'kpi-cyan'">
          <div class="kpi-icon" :style="a.loadLevel === 'over' ? { background: '#fff0f6', color: '#eb2f96' } : { background: '#e6fffb', color: '#13c2c2' }">{{ a.loadLevel === 'over' ? '⚠️' : '📈' }}</div>
          <div class="kpi-body">
            <div class="kpi-value" :style="{ color: a.loadLevel === 'over' ? '#eb2f96' : '#13c2c2' }">{{ fmtLoad }}<span class="kpi-unit">%</span></div>
            <div class="kpi-label">负载率<template v-if="a.loadLevel === 'over'">（超预警！）</template></div>
            <div class="kpi-sub">进行中 {{ a.inFlightTasks ?? 0 }} ÷ (在线引擎 {{ a.onlineRuntimes ?? 0 }} × {{ a.maxConcurrentPerRuntime ?? 20 }})</div>
          </div>
        </div>
        <div class="kpi-card kpi-orange">
          <div class="kpi-icon" style="background:#fff7e6;color:#fa8c16;">🤖</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ a.taskCount ?? 0 }}</div>
            <div class="kpi-label">任务总数</div>
            <div class="kpi-sub">进行中 {{ a.inFlightTasks ?? 0 }}</div>
          </div>
        </div>
      </div>

      <!-- 预警值配置 -->
      <div class="card" style="margin-top:14px">
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <h5 style="margin:0">负载预警值</h5>
          <span class="t3" style="font-size:12px">超过该百分比阈值，卡片与详情会红色预警（防止过多任务影响 opencode 效果）</span>
          <div style="margin-left:auto;display:flex;align-items:center;gap:8px">
            <input type="number" min="1" max="100" v-model.number="thresholdInput" class="threshold-input" />
            <span class="t3">%</span>
            <button class="btn" :disabled="savingThreshold || thresholdInput === a.alertThresholdPct" @click="saveThreshold">保存</button>
            <span v-if="thresholdSaved" class="t3" style="color:#52c41a">✓ 已保存</span>
          </div>
        </div>
      </div>

      <!-- 任务状态分布 -->
      <div class="card chart-card" style="margin-top:14px">
        <div class="chart-title">任务状态分布</div>
        <div class="chart-body" ref="taskChartRef" style="height:240px"></div>
      </div>

      <!-- 仓库使用明细 -->
      <div class="card" style="margin-top:14px;overflow-x:auto">
        <h5 style="margin:0 0 10px">仓库使用明细（{{ a.repoUsages?.length ?? 0 }}）</h5>
        <table class="vod-table" v-if="a.repoUsages?.length">
          <thead>
            <tr><th>仓库</th><th>状态</th><th>启用</th><th>引擎 ID</th><th>最后活跃</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in a.repoUsages" :key="r.id">
              <td style="max-width:380px">{{ r.repoKey }}</td>
              <td><span :class="['status-tag', r.status === 'idle' ? 'default' : 'success']">{{ r.status }}</span></td>
              <td>{{ r.enable === 1 ? '✅' : '⛔' }}</td>
              <td class="t3">{{ r.runtimeId || '-' }}</td>
              <td class="t3">{{ fmtTime(r.lastActiveAt) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="t2" style="padding:12px;text-align:center">该智能体暂无仓库级实例</p>
      </div>
    </div>

    <div v-else-if="loading" class="loading-spinner">加载中...</div>
    <div v-else class="loading-spinner">智能体不存在或已删除</div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as echarts from 'echarts'
import http from '../api'

const route = useRoute()
const router = useRouter()
const a = ref(null)
const loading = ref(true)
const saving = ref(false)
const savingThreshold = ref(false)
const thresholdSaved = ref(false)
const thresholdInput = ref(80)
const taskChartRef = ref(null)
let chart = null

const agentType = computed(() => route.params.type)
const iconStyle = computed(() => ({ background: '#e8f0fe', color: '#006eff' }))
const subscribeCount = computed(() => {
  try { return (JSON.parse(a.value?.subscribeEvents || '[]') || []).length } catch { return 0 }
})
const fmtLoad = computed(() => {
  if (a.value?.loadRate == null) return '—'
  const v = Number(a.value.loadRate)
  return Number.isInteger(v) ? v : v.toFixed(1)
})

function fmtTime(v) {
  if (!v) return '—'
  return String(v).replace('T', ' ').substring(0, 19)
}

function goBack() { router.push('/agent/manage') }

async function load() {
  loading.value = true
  try {
    const { data } = await http.get(`/agents/${agentType.value}/detail`)
    a.value = data
    thresholdInput.value = data.alertThresholdPct ?? 80
    await nextTick()
    renderChart(data.statusBreakdown || {})
  } catch (e) {
    console.error('智能体详情加载失败:', e)
    a.value = null
  }
  loading.value = false
}

async function toggle(checked) {
  saving.value = true
  try {
    const { data } = await http.post(`/agents/${agentType.value}/enable`, { enable: checked })
    a.value.enable = data.enable
  } catch (e) {
    console.error('切换失败:', e)
    alert('切换失败，请重试')
  } finally {
    saving.value = false
  }
}

async function saveThreshold() {
  const pct = Number(thresholdInput.value)
  if (!Number.isInteger(pct) || pct < 1 || pct > 100) {
    alert('预警值必须是 1-100 的整数')
    return
  }
  savingThreshold.value = true
  thresholdSaved.value = false
  try {
    const { data } = await http.post(`/agents/${agentType.value}/alert`, { thresholdPct: pct })
    a.value.alertThresholdPct = data.alertThresholdPct
    thresholdSaved.value = true
    setTimeout(() => { thresholdSaved.value = false }, 2000)
  } catch (e) {
    console.error('保存预警值失败:', e)
    alert('保存失败，请重试')
  } finally {
    savingThreshold.value = false
  }
}

const STATUS_COLOR = { queued: '#fa8c16', running: '#006eff', completed: '#52c41a', failed: '#eb2f96' }
const STATUS_ORDER = ['queued', 'running', 'completed', 'failed']

function renderChart(breakdown) {
  if (!taskChartRef.value) return
  if (!chart) chart = echarts.init(taskChartRef.value)
  const entries = Object.entries(breakdown || {})
  const data = entries.map(([k, v]) => ({ name: k, value: v, itemStyle: { color: STATUS_COLOR[k] || '#8c8c8c' } }))
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

function onResize() { chart && chart.resize() }

onMounted(() => { load(); window.addEventListener('resize', onResize) })
onBeforeUnmount(() => { window.removeEventListener('resize', onResize); chart && chart.dispose() })
</script>

<style scoped>
.card { background: #fff; border: 1px solid var(--border-color); border-radius: 10px; box-shadow: var(--card-shadow); padding: 16px 20px; }
.agent-icon { width: 44px; height: 44px; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 22px; flex-shrink: 0; }
.code-tag { font-size: 11px; font-weight: 500; color: var(--text-tertiary); background: #f5f7fa; border-radius: 4px; padding: 1px 6px; }
.cat-tag { font-size: 11px; padding: 1px 8px; border-radius: 10px; }
.cat-official { background: #e6f4ff; color: #006eff; }
.desc { font-size: 13px; color: var(--text-secondary); line-height: 1.7; margin: 0; }

.kpi-row { display: flex; gap: 12px; flex-wrap: wrap; }
.kpi-card {
  flex: 1; min-width: 150px;
  background: #fff; border-radius: 10px; border: 1px solid var(--border-color);
  padding: 16px 18px; box-shadow: var(--card-shadow);
  display: flex; align-items: center; gap: 12px;
}
.kpi-blue { border-left: 3px solid #006eff; }
.kpi-green { border-left: 3px solid #52c41a; }
.kpi-cyan { border-left: 3px solid #13c2c2; }
.kpi-orange { border-left: 3px solid #fa8c16; }
.kpi-pink { border-left: 3px solid #eb2f96; }
.kpi-icon { width: 36px; height: 36px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 18px; flex-shrink: 0; }
.kpi-body { flex: 1; min-width: 0; }
.kpi-value { font-size: 24px; font-weight: 700; color: var(--text-primary); line-height: 1.2; }
.kpi-unit { font-size: 13px; font-weight: 500; color: var(--text-tertiary); }
.kpi-label { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
.kpi-sub { font-size: 11px; color: var(--text-tertiary); margin-top: 2px; line-height: 1.4; }

.threshold-input { width: 90px; padding: 6px 10px; border: 1px solid var(--border-color); border-radius: 6px; font-size: 14px; }

.chart-card { padding: 0; }
.chart-title { padding: 14px 18px 0; font-size: 14px; font-weight: 600; color: var(--text-primary); }
.chart-body { padding: 10px 14px 14px; }

.vod-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.vod-table th { text-align: left; padding: 8px 10px; background: #fafafa; font-weight: 600; color: var(--text-secondary); border-bottom: 1px solid var(--border-color); white-space: nowrap; }
.vod-table td { padding: 8px 10px; border-bottom: 1px solid #f0f0f0; }
.status-tag { font-size: 11px; padding: 1px 8px; border-radius: 10px; }
.status-tag.success { background: #f6ffed; color: #52c41a; }
.status-tag.default { background: #f5f7fa; color: var(--text-tertiary); }

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

.loading-spinner { padding: 60px; text-align: center; color: var(--text-secondary); }
</style>
