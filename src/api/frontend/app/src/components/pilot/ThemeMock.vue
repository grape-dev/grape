<template>
  <div ref="root" :class="['mock', 'skin-' + skin]">
    <!-- 容器查询只作用于容器的后代，所以栅格放在内层 .mock__grid -->
    <div class="mock__grid">
      <!-- 侧栏：图标栏 + 导航 -->
      <aside class="mock__rail">
        <div class="mock__logo">G</div>
        <div class="mock__rail-items">
          <button
            v-for="ic in rail"
            :key="ic.k"
            class="rail-btn"
            :class="{ on: ic.on }"
            :title="ic.t"
          >
            {{ ic.e }}
          </button>
        </div>
        <div class="mock__avatar">猫</div>
      </aside>

      <nav class="mock__nav">
        <div class="mock__ws">
          <span class="ws-dot"></span>
          <span class="ws-name">grape-dev</span>
          <span class="ws-caret">⌄</span>
        </div>
        <div v-for="g in navGroups" :key="g.title" class="nav-group">
          <div class="nav-group__title">{{ g.title }}</div>
          <a v-for="it in g.items" :key="it.label" class="nav-item" :class="{ on: it.on }">
            <span class="nav-item__e">{{ it.e }}</span>
            <span class="nav-item__t">{{ it.label }}</span>
            <span v-if="it.count" class="nav-item__c">{{ it.count }}</span>
          </a>
        </div>
        <div class="mock__quota">
          <div class="quota-row"><span>本月额度</span><span>62%</span></div>
          <div class="quota-bar"><i style="width: 62%"></i></div>
        </div>
      </nav>

      <main class="mock__main">
        <!-- 页头 -->
        <header class="mock__head">
          <div class="head-left">
            <div class="crumb">平台 <span>/</span> 运行<span>/</span> 总览</div>
            <h1>运行总览</h1>
            <p class="head-sub">近 24 小时 · 18 个引擎在线 · 3 条待处理告警</p>
          </div>
          <div class="head-actions">
            <AGButton size="sm" variant="plain">导出</AGButton>
            <AGButton size="sm">新建任务</AGButton>
          </div>
        </header>

        <!-- KPI -->
        <section class="kpis">
          <AGCard v-for="k in kpis" :key="k.label" class="kpi" :padded="false">
            <div class="kpi__label">{{ k.label }}</div>
            <div class="kpi__row">
              <div class="kpi__value">{{ k.value }}</div>
              <svg class="kpi__spark" viewBox="0 0 64 22" preserveAspectRatio="none">
                <polyline
                  :points="spark(k.trend)"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.6"
                />
              </svg>
            </div>
            <div class="kpi__foot">
              <span class="delta" :class="k.up ? 'up' : 'down'"
                >{{ k.up ? '▲' : '▼' }} {{ k.delta }}</span
              >
              <span class="kpi__hint">{{ k.hint }}</span>
            </div>
          </AGCard>
        </section>

        <!-- 图表 + 告警 -->
        <section class="grid-2">
          <AGCard class="panel" :padded="false">
            <div class="panel__head">
              <div>
                <h2>任务吞吐</h2>
                <p class="panel__sub">每次提交触发的平均排队时长 4.2s</p>
              </div>
              <div class="segmented">
                <button
                  v-for="t in ['24h', '7d', '30d']"
                  :key="t"
                  :class="{ on: range === t }"
                  @click="range = t"
                >
                  {{ t }}
                </button>
              </div>
            </div>
            <div class="chart">
              <div v-for="(b, i) in bars" :key="i" class="chart__col">
                <div class="chart__bar" :style="{ height: b + '%' }" :class="{ hot: b > 78 }"></div>
              </div>
            </div>
            <div class="chart__axis">
              <span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span
              ><span>24:00</span>
            </div>
          </AGCard>

          <AGCard class="panel" :padded="false">
            <div class="panel__head">
              <h2>最近告警</h2>
              <span class="pill-count">3 未处理</span>
            </div>
            <ul class="alerts">
              <li v-for="a in alerts" :key="a.title" class="alert">
                <span class="alert__dot" :class="'tone-' + a.tone"></span>
                <div class="alert__body">
                  <div class="alert__title">{{ a.title }}</div>
                  <div class="alert__meta">{{ a.meta }}</div>
                </div>
                <span class="alert__time">{{ a.time }}</span>
              </li>
            </ul>
            <div class="panel__foot">
              <AGButton size="sm" variant="plain">查看全部</AGButton>
            </div>
          </AGCard>
        </section>

        <!-- 表格 -->
        <AGCard class="panel panel--flush" :padded="false">
          <div class="panel__head panel__head--pad">
            <h2>引擎列表</h2>
            <div class="head-actions">
              <AGButton size="sm" variant="plain">筛选</AGButton>
              <AGButton size="sm" variant="plain">刷新</AGButton>
            </div>
          </div>
          <AGTable :columns="cols" :rows="engines" empty-text="暂无引擎">
            <template #cell-name="{ row }">
              <div class="engine">
                <span class="engine__dot" :style="{ background: sceneColor(row.scene) }"></span>
                <span class="engine__name">{{ row.name }}</span>
                <span class="pill-scene" :style="sceneStyle(row.scene)">{{ row.scene }}</span>
              </div>
            </template>
            <template #cell-status="{ row }">
              <span class="badge" :class="'badge--' + row.tone">{{ row.status }}</span>
            </template>
            <template #cell-latency="{ row }">{{ row.latency }}</template>
            <template #cell-owner="{ row }">
              <span class="muted">{{ row.owner }}</span>
            </template>
          </AGTable>
        </AGCard>

        <!-- 组件区 -->
        <section class="grid-2">
          <AGCard class="panel" :padded="false">
            <h2>控件</h2>
            <div class="row">
              <AGButton size="sm">主要按钮</AGButton>
              <AGButton size="sm" variant="plain">次要按钮</AGButton>
              <AGButton size="sm" variant="danger">删除</AGButton>
              <AGButton size="sm" disabled>禁用</AGButton>
            </div>
            <div class="row row--gap">
              <AGPill>skill-chain</AGPill>
              <AGPill>branch: main</AGPill>
              <span class="badge badge--ok">在线</span>
              <span class="badge badge--warn">降级</span>
              <span class="badge badge--err">离线</span>
            </div>
            <div class="row row--gap">
              <AGInput v-model="q" label="搜索引擎" placeholder="输入关键字" />
              <AGSelect v-model="opt" :options="opts" placeholder="全部分类" allow-placeholder />
            </div>
            <div class="row row--gap row--vert">
              <div class="switch-row">
                <span class="switch-label">自动扩缩容</span>
                <button class="switch" :class="{ on: sw }" @click="sw = !sw"><i></i></button>
                <span class="muted">{{ sw ? '已开启' : '已关闭' }}</span>
              </div>
              <div class="progress-row">
                <div class="progress"><i :style="{ width: prog + '%' }"></i></div>
                <span class="muted">{{ prog }}%</span>
              </div>
            </div>
          </AGCard>

          <AGCard class="panel" :padded="false">
            <h2>对比度体检</h2>
            <p class="panel__sub">
              按 WCAG：正文 4.5:1，注释/控件 3:1。数值由当前皮肤变量实时计算。
            </p>
            <div v-for="c in contrast" :key="c.label" class="cc">
              <span class="cc__chip" :style="{ background: c.bg, color: c.color }">Aa</span>
              <span class="cc__label">{{ c.label }}</span>
              <span class="cc__val">{{ c.ratio }}</span>
              <span class="badge" :class="c.pass ? 'badge--ok' : 'badge--err'">{{
                c.pass ? '达标' : '不足'
              }}</span>
            </div>
          </AGCard>
        </section>

        <!-- 色板 -->
        <AGCard class="panel" :padded="false">
          <h2>色板</h2>
          <div class="swatches">
            <div v-for="s in swatches" :key="s.k" class="swatch">
              <span class="swatch__box" :style="{ background: s.v }"></span>
              <span class="swatch__name">{{ s.k }}</span>
              <span class="swatch__hex">{{ s.hex }}</span>
            </div>
          </div>
        </AGCard>
      </main>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import AGButton from '@/components/ui/AGButton.vue'
import AGCard from '@/components/ui/AGCard.vue'
import AGInput from '@/components/ui/AGInput.vue'
import AGSelect from '@/components/ui/AGSelect.vue'
import AGPill from '@/components/ui/AGPill.vue'
import AGTable from '@/components/ui/AGTable.vue'

defineProps({ skin: { type: String, required: true } })

const root = ref(null)
const range = ref('24h')
const q = ref('')
const opt = ref('')
const sw = ref(true)
const prog = ref(62)

const rail = [
  { k: 'overview', e: '◎', t: '总览', on: true },
  { k: 'pipelines', e: '⌥', t: '流水线' },
  { k: 'agents', e: '✦', t: '智能体' },
  { k: 'issues', e: '⚑', t: '需求' },
  { k: 'settings', e: '⚙', t: '设置' },
]
const navGroups = [
  {
    title: '运行',
    items: [
      { label: '运行总览', e: '◎', on: true },
      { label: '流水线', e: '⌥', count: 12 },
      { label: '智能体', e: '✦', count: 6 },
      { label: '可观测性', e: '◔' },
    ],
  },
  {
    title: '协作',
    items: [
      { label: '需求', e: '⚑', count: 28 },
      { label: '知识库', e: '▤' },
      { label: '场景市场', e: '❖' },
    ],
  },
]
const kpis = [
  {
    label: '在线引擎',
    value: '18',
    delta: '2',
    up: true,
    hint: '较昨日',
    trend: [12, 13, 13, 15, 14, 16, 18],
  },
  {
    label: '今日任务',
    value: '1,284',
    delta: '8.4%',
    up: true,
    hint: '较昨日',
    trend: [8, 9, 11, 10, 12, 13, 14],
  },
  {
    label: '成功率',
    value: '96.4%',
    delta: '0.6%',
    up: true,
    hint: '近 7 天',
    trend: [94, 95, 95, 96, 95, 96, 96],
  },
  {
    label: 'P95 耗时',
    value: '3m 12s',
    delta: '11s',
    up: false,
    hint: '较昨日',
    trend: [16, 15, 15, 14, 14, 13, 13],
  },
]
const bars = [
  22, 18, 15, 12, 10, 14, 26, 41, 58, 66, 72, 61, 54, 63, 74, 81, 76, 68, 55, 43, 34, 28, 24, 20,
]
const alerts = [
  {
    title: 'build-agent-04 心跳丢失',
    meta: 'SRE · 重试 2 次后成功',
    time: '3 分钟前',
    tone: 'err',
  },
  {
    title: 'qa-runner 队列积压 42',
    meta: 'QA · 已自动扩容 1 实例',
    time: '11 分钟前',
    tone: 'warn',
  },
  {
    title: 'vod-pipeline 成功率下降到 88%',
    meta: 'VOD · 待人工确认',
    time: '26 分钟前',
    tone: 'warn',
  },
  { title: 'code-reviewer 版本已更新', meta: '智能体 · 无影响', time: '1 小时前', tone: 'ok' },
  { title: '文档索引重建完成', meta: '知识库 · 2,148 条', time: '2 小时前', tone: 'ok' },
]
const cols = [
  { key: 'name', title: '引擎' },
  { key: 'status', title: '状态', width: '110px' },
  { key: 'latency', title: 'P95', width: '90px' },
  { key: 'owner', title: '负责人', width: '130px' },
]
const engines = [
  {
    name: 'build-agent-04',
    scene: 'Coding',
    status: '在线',
    tone: 'ok',
    latency: '2m 41s',
    owner: 'Jason',
  },
  { name: 'qa-runner', scene: 'QA', status: '降级', tone: 'warn', latency: '5m 08s', owner: 'Mia' },
  {
    name: 'vod-pipeline',
    scene: 'VOD',
    status: '降级',
    tone: 'warn',
    latency: '4m 32s',
    owner: 'Neko',
  },
  {
    name: 'code-reviewer',
    scene: 'Coding',
    status: '在线',
    tone: 'ok',
    latency: '1m 12s',
    owner: 'Jason',
  },
  { name: 'sre-watchdog', scene: 'SRE', status: '离线', tone: 'err', latency: '—', owner: '运维' },
  {
    name: 'doc-indexer',
    scene: 'Issue',
    status: '在线',
    tone: 'ok',
    latency: '48s',
    owner: 'Lily',
  },
]
const opts = [
  { value: 'coding', label: '编码' },
  { value: 'qa', label: '测试' },
  { value: 'vod', label: '视频' },
]
const swatchKeys = [
  '--c-bg',
  '--c-bg-elevated',
  '--c-bg-float',
  '--c-accent',
  '--c-success',
  '--c-warning',
  '--c-danger',
]

const swatches = ref([])
const contrast = ref([])

function spark(arr) {
  const max = Math.max(...arr)
  const min = Math.min(...arr)
  const span = max - min || 1
  return arr
    .map((v, i) => `${(i / (arr.length - 1)) * 64},${20 - ((v - min) / span) * 18}`)
    .join(' ')
}
const SCENE = { VOD: '#b07a8b', Issue: '#b8857a', Coding: '#7a92b0', QA: '#9a85a8', SRE: '#b89b6a' }
function sceneColor(s) {
  return SCENE[s] || 'var(--c-text-3)'
}
function sceneStyle(s) {
  const c = SCENE[s]
  return c ? { background: c + '33', color: mixTone(c) } : {}
}
// 莫兰迪底色在深色上偏暗，文字用同色相提亮版
function mixTone(hex) {
  const n = parseInt(hex.slice(1), 16)
  const f = (v) => Math.min(255, Math.round(v + (255 - v) * 0.32))
  return `rgb(${f((n >> 16) & 255)},${f((n >> 8) & 255)},${f(n & 255)})`
}

function parseColor(str) {
  const s = String(str).trim()
  // 构建产物会把 rgba() 压成 #rrggbbaa，所以 3/4/6/8 位十六进制都要认
  let m = /^#([0-9a-f]{3,8})$/i.exec(s)
  if (m) {
    const h = m[1].length <= 4 ? m[1].replace(/./g, (c) => c + c) : m[1]
    return [
      parseInt(h.slice(0, 2), 16),
      parseInt(h.slice(2, 4), 16),
      parseInt(h.slice(4, 6), 16),
      h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1,
    ]
  }
  m = /rgba?\(([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?\)/i.exec(s)
  return m ? [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]] : null
}
function over(fg, bg) {
  const a = fg[3]
  return [fg[0] * a + bg[0] * (1 - a), fg[1] * a + bg[1] * (1 - a), fg[2] * a + bg[2] * (1 - a), 1]
}
function lum(c) {
  const g = (v) => {
    const x = v / 255
    return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4)
  }
  return 0.2126 * g(c[0]) + 0.7152 * g(c[1]) + 0.0722 * g(c[2])
}
function ratio(fg, bg) {
  const l1 = lum(fg)
  const l2 = lum(bg)
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
}
function hexOf(c) {
  return (
    '#' +
    c
      .slice(0, 3)
      .map((v) => Math.round(v).toString(16).padStart(2, '0'))
      .join('')
  )
}

onMounted(() => {
  const cs = window.getComputedStyle(root.value)
  const get = (k) => cs.getPropertyValue(k).trim()
  const card = parseColor(get('--c-bg-elevated')) || [44, 44, 46, 1]

  swatches.value = swatchKeys.map((k) => {
    const raw = get(k)
    const p = parseColor(raw)
    return { k: k.replace('--c-', ''), v: raw, hex: p ? hexOf(over(p, card)) : raw }
  })

  const pairs = [
    { label: '正文 text-primary', k: '--c-text', need: 4.5 },
    { label: '次要 text-secondary', k: '--c-text-2', need: 4.5 },
    { label: '注释 text-tertiary', k: '--c-text-3', need: 3.0 },
    { label: '强调 accent（控件口径）', k: '--c-accent', need: 3.0 },
  ]
  contrast.value = pairs.map((p) => {
    const fg = parseColor(get(p.k))
    if (!fg) return { label: p.label, ratio: '—', pass: false, bg: '', color: '' }
    const solid = over(fg, card)
    const r = ratio(solid, card)
    return {
      label: p.label,
      ratio: r.toFixed(2) + ':1',
      pass: r >= p.need,
      bg: hexOf(card),
      color: hexOf(solid),
    }
  })
})
</script>

<style scoped>
.mock {
  display: block;
  min-height: 100%;
  background: var(--c-bg);
  color: var(--c-text);
  font-family: var(--f-family);
  font-size: var(--f-body);
  container-type: inline-size;
}
.mock__grid {
  display: grid;
  grid-template-columns: 56px 188px 1fr;
  min-height: 100%;
}

/* 侧栏 */
.mock__rail {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--s-3);
  padding: var(--s-3) 0;
  background: color-mix(in srgb, var(--c-bg-elevated) 55%, transparent);
  border-right: 1px solid var(--glass-border);
}
.mock__logo {
  width: 30px;
  height: 30px;
  border-radius: 9px;
  display: grid;
  place-items: center;
  font-weight: 700;
  color: #fff;
  background: linear-gradient(
    160deg,
    var(--c-accent),
    color-mix(in srgb, var(--c-accent) 45%, #000)
  );
}
.mock__rail-items {
  display: flex;
  flex-direction: column;
  gap: var(--s-1);
}
.rail-btn {
  width: 34px;
  height: 34px;
  border: 0;
  border-radius: var(--r-control);
  background: transparent;
  color: var(--c-text-2);
  font-size: 15px;
  cursor: pointer;
  transition: 0.18s ease;
}
.rail-btn:hover {
  background: var(--row-hover);
  color: var(--c-text);
}
.rail-btn.on {
  background: var(--c-accent-soft);
  color: var(--c-accent-text, var(--c-accent));
}
.mock__avatar {
  margin-top: auto;
  width: 30px;
  height: 30px;
  border-radius: var(--r-pill);
  display: grid;
  place-items: center;
  font-size: 12px;
  background: var(--c-bg-float);
  color: var(--c-text);
}

/* 导航 */
.mock__nav {
  display: flex;
  flex-direction: column;
  gap: var(--s-4);
  padding: var(--s-3) var(--s-2);
  border-right: 1px solid var(--glass-border);
  background: color-mix(in srgb, var(--c-bg-elevated) 35%, transparent);
  min-width: 0;
}
.mock__ws {
  display: flex;
  align-items: center;
  gap: var(--s-2);
  padding: var(--s-2) var(--s-3);
  border-radius: var(--r-control);
  background: var(--glass-bg);
  box-shadow: var(--card-ring);
  font-weight: 600;
  font-size: 13px;
}
.ws-dot {
  width: 8px;
  height: 8px;
  border-radius: var(--r-pill);
  background: var(--c-success);
  box-shadow: 0 0 0 3px var(--c-success-soft);
}
.ws-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ws-caret {
  color: var(--c-text-3);
}
.nav-group__title {
  font-size: 11px;
  letter-spacing: 0.06em;
  color: var(--c-text-3);
  padding: var(--s-1) var(--s-3);
  text-transform: uppercase;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: var(--s-2);
  padding: 6px var(--s-3);
  border-radius: var(--r-control);
  color: var(--c-text-2);
  font-size: 13px;
  cursor: pointer;
  text-decoration: none;
  transition: 0.16s ease;
}
.nav-item:hover {
  background: var(--row-hover);
  color: var(--c-text);
}
.nav-item.on {
  background: var(--c-accent-soft);
  color: var(--c-accent-text, var(--c-accent));
  font-weight: 600;
}
.nav-item__e {
  width: 14px;
  text-align: center;
  opacity: 0.9;
}
.nav-item__t {
  flex: 1;
}
.nav-item__c {
  font-size: 11px;
  color: var(--c-text-3);
}
.nav-item.on .nav-item__c {
  color: inherit;
}
.mock__quota {
  margin-top: auto;
  padding: var(--s-2) var(--s-3);
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.quota-row {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--c-text-3);
}
.quota-bar {
  height: 4px;
  border-radius: var(--r-pill);
  background: var(--row-hover);
  overflow: hidden;
}
.quota-bar i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--c-accent);
}

/* 主区 */
.mock__main {
  padding: var(--s-4) var(--s-5) var(--s-6);
  display: flex;
  flex-direction: column;
  gap: var(--s-4);
  min-width: 0;
  background:
    radial-gradient(
      1200px 320px at 12% -10%,
      color-mix(in srgb, var(--c-accent) 12%, transparent),
      transparent 70%
    ),
    radial-gradient(
      900px 300px at 88% 0%,
      color-mix(in srgb, var(--c-vod) 10%, transparent),
      transparent 70%
    );
}
.mock__head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--s-4);
}
.crumb {
  font-size: 11px;
  color: var(--c-text-3);
  margin-bottom: 6px;
}
.crumb span {
  margin: 0 4px;
  opacity: 0.5;
}
.mock__head h1 {
  font-size: var(--f-head-1);
  line-height: 1.15;
  letter-spacing: -0.02em;
  margin: 0;
}
.head-sub {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--c-text-2);
}
.head-actions {
  display: flex;
  gap: var(--s-2);
  flex-shrink: 0;
}

/* KPI */
.kpis {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--s-3);
}
.kpi {
  padding: var(--s-3) var(--s-4);
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.kpi__label {
  font-size: 11px;
  letter-spacing: 0.04em;
  color: var(--c-text-3);
}
.kpi__row {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--s-2);
}
.kpi__value {
  font-size: 26px;
  font-weight: 650;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}
.kpi__spark {
  width: 64px;
  height: 22px;
  color: var(--c-accent);
  opacity: 0.85;
  flex-shrink: 0;
}
.kpi__foot {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
}
.delta.up {
  color: var(--c-success);
}
.delta.down {
  color: var(--c-danger);
}
.kpi__hint {
  color: var(--c-text-3);
}

/* 面板 */
.grid-2 {
  display: grid;
  grid-template-columns: 1.35fr 1fr;
  gap: var(--s-3);
  align-items: start;
}
.panel {
  padding: var(--s-4);
  display: flex;
  flex-direction: column;
  gap: var(--s-3);
  min-width: 0;
}
.panel--flush {
  padding: 0;
  overflow: hidden;
}
.panel__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--s-3);
}
.panel__head--pad {
  padding: var(--s-4) var(--s-4) 0;
  align-items: center;
}
.panel h2 {
  margin: 0;
  font-size: var(--f-head-3);
  letter-spacing: -0.01em;
}
.panel__sub {
  margin: 4px 0 0;
  font-size: 11px;
  color: var(--c-text-3);
  line-height: 1.5;
}
.pill-count {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: var(--r-pill);
  background: var(--c-warning-soft);
  color: var(--c-warning);
  white-space: nowrap;
}
.segmented {
  display: flex;
  gap: 2px;
  padding: 2px;
  border-radius: var(--r-pill);
  background: var(--glass-bg-strong);
  box-shadow: var(--card-ring);
}
.segmented button {
  border: 0;
  background: transparent;
  color: var(--c-text-2);
  font-size: 11px;
  padding: 4px 10px;
  border-radius: var(--r-pill);
  cursor: pointer;
  font-family: inherit;
  transition: 0.16s ease;
}
.segmented button.on {
  background: var(--c-accent);
  color: #fff;
  font-weight: 600;
}

/* 柱状图 */
.chart {
  height: 132px;
  display: flex;
  align-items: flex-end;
  gap: 3px;
  padding-top: var(--s-2);
  border-radius: var(--r-control);
  background: linear-gradient(to top, var(--row-hover), transparent 65%);
}
.chart__col {
  flex: 1;
  display: flex;
  align-items: flex-end;
  height: 100%;
}
.chart__bar {
  width: 100%;
  border-radius: 3px 3px 2px 2px;
  background: color-mix(in srgb, var(--c-accent) 62%, transparent);
  transition: height 0.3s ease;
}
.chart__bar.hot {
  background: var(--c-accent);
  box-shadow: 0 0 10px color-mix(in srgb, var(--c-accent) 55%, transparent);
}
.chart__axis {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--c-text-3);
}

/* 告警 */
.alerts {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}
.alert {
  display: flex;
  align-items: center;
  gap: var(--s-3);
  padding: var(--s-2) 0;
  border-bottom: 1px solid var(--glass-border);
}
.alert:last-child {
  border-bottom: none;
}
.alert__dot {
  width: 7px;
  height: 7px;
  border-radius: var(--r-pill);
  flex-shrink: 0;
}
.tone-ok {
  background: var(--c-success);
  box-shadow: 0 0 0 3px var(--c-success-soft);
}
.tone-warn {
  background: var(--c-warning);
  box-shadow: 0 0 0 3px var(--c-warning-soft);
}
.tone-err {
  background: var(--c-danger);
  box-shadow: 0 0 0 3px var(--c-danger-soft);
}
.alert__body {
  min-width: 0;
  flex: 1;
}
.alert__title {
  font-size: 12.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.alert__meta {
  font-size: 11px;
  color: var(--c-text-3);
  margin-top: 2px;
}
.alert__time {
  font-size: 10.5px;
  color: var(--c-text-3);
  white-space: nowrap;
}
.panel__foot {
  display: flex;
  justify-content: flex-end;
}

/* 表格内元素 */
.engine {
  display: flex;
  align-items: center;
  gap: var(--s-2);
}
.engine__dot {
  width: 7px;
  height: 7px;
  border-radius: var(--r-pill);
  flex-shrink: 0;
}
.engine__name {
  font-weight: 550;
}
.pill-scene {
  font-size: 10px;
  padding: 1px 7px;
  border-radius: var(--r-pill);
  letter-spacing: 0.02em;
}
.badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: var(--r-pill);
  white-space: nowrap;
}
.badge--ok {
  background: var(--c-success-soft);
  color: var(--c-success);
}
.badge--warn {
  background: var(--c-warning-soft);
  color: var(--c-warning);
}
.badge--err {
  background: var(--c-danger-soft);
  color: var(--c-danger);
}
.muted {
  color: var(--c-text-3);
  font-size: 11.5px;
}

/* 控件区 */
.row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--s-2);
}
.row--gap {
  margin-top: var(--s-1);
  gap: var(--s-3);
}
.row--vert {
  flex-direction: column;
  align-items: stretch;
  gap: var(--s-2);
}
.switch-row,
.progress-row {
  display: flex;
  align-items: center;
  gap: var(--s-2);
  font-size: 12px;
}
.switch-label {
  width: 84px;
  color: var(--c-text-2);
}
.switch {
  width: 38px;
  height: 22px;
  border: 0;
  border-radius: var(--r-pill);
  background: color-mix(in srgb, var(--c-text-3) 45%, transparent);
  cursor: pointer;
  padding: 2px;
  transition: 0.2s ease;
  display: flex;
  align-items: center;
}
.switch i {
  width: 18px;
  height: 18px;
  border-radius: var(--r-pill);
  background: #fff;
  transition: transform 0.2s ease;
  display: block;
}
.switch.on {
  background: var(--c-accent);
}
.switch.on i {
  transform: translateX(16px);
}
.progress {
  flex: 1;
  height: 5px;
  border-radius: var(--r-pill);
  background: var(--row-hover);
  overflow: hidden;
}
.progress i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(
    90deg,
    var(--c-accent),
    color-mix(in srgb, var(--c-accent) 55%, var(--c-success))
  );
}

/* 对比度体检 */
.cc {
  display: flex;
  align-items: center;
  gap: var(--s-2);
  font-size: 12px;
  padding: 4px 0;
}
.cc__chip {
  width: 26px;
  height: 20px;
  border-radius: 5px;
  display: grid;
  place-items: center;
  font-size: 11px;
  font-weight: 600;
  flex-shrink: 0;
}
.cc__label {
  flex: 1;
  color: var(--c-text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cc__val {
  font-variant-numeric: tabular-nums;
  color: var(--c-text);
}

/* 色板 */
.swatches {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s-2);
}
.swatch {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 10px;
  color: var(--c-text-3);
  min-width: 92px;
}
.swatch__box {
  height: 30px;
  border-radius: 6px;
  box-shadow: var(--card-ring);
}
.swatch__name {
  color: var(--c-text-2);
}
.swatch__hex {
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.02em;
}

/* 窄容器（双栏对照时每栏 ≈ 700px）：收紧侧栏与栅格 */
@container (max-width: 980px) {
  .mock__grid {
    grid-template-columns: 48px 1fr;
  }
  .mock__nav {
    display: none;
  }
  .kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .grid-2 {
    grid-template-columns: 1fr;
  }
  .mock__main {
    padding: var(--s-4);
  }
  .mock__head h1 {
    font-size: var(--f-head-2);
  }
}
</style>
