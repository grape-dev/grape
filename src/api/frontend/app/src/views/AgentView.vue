<template>
  <div class="ag" :style="{ '--agent-hue': hueOf(name) }">
    <!-- Agent 头卡（Liquid Glass + agent-hue 染色） -->
    <div class="ag__head">
      <div class="ag__icon">{{ meta.icon || '?' }}</div>
      <div class="ag__info">
        <h2>{{ typeDisplay }}</h2>
        <div class="ag__desc">{{ typeDesc }}</div>
      </div>
      <div class="ag__engine">
        <template v-if="engineText">
          <span v-if="engineDot" class="ag__engine-dot" :style="{ background: engineDot }"></span>
          {{ t('agents.engine.label', { text: engineText }) }}
        </template>
        <span v-else class="ag__engine-none">{{ t('agents.engine.no_engine') }}</span>
      </div>
    </div>

    <!-- VOD IM 已绑定卡片（Issue #294） -->
    <div v-if="name === 'vod' && im.im_app_id" class="ag__imcard">
      <div class="ag__imhead">
        <h3>{{ t('agents.config.im_title') }}</h3>
        <span class="ag__immeta">{{ im.connected ? t('agents.config.im_configured') : '' }}</span>
      </div>
      <div class="ag__imrow">
        <label>{{ t('agents.config.bot_app_id') }}</label>
        <span class="ag__imappid">{{ im.im_app_id }}</span>
        <AGButton variant="danger" size="sm" @click="unbindVodIm">{{
          t('agents.config.unbind')
        }}</AGButton>
      </div>
      <div v-if="imConvs" class="ag__imconvs">
        {{ t('agents.config.bound_chats', { n: imConvs }) }}
      </div>
    </div>

    <!-- 统计 -->
    <div class="ag__stats">
      <div class="ag__st">
        <div class="v">{{ totalEvents }}</div>
        <div class="l">{{ t('agents.event.total') }}</div>
      </div>
      <div class="ag__st">
        <div class="v">{{ completed }}</div>
        <div class="l">{{ t('agents.event.completed') }}</div>
      </div>
      <div class="ag__st">
        <div class="v">{{ running }}</div>
        <div class="l">{{ t('agents.event.running') }}</div>
      </div>
    </div>

    <!-- 事件历史（Issue #127 搜索+分页） -->
    <AGCard>
      <div class="ag__evt-head">
        <h2>{{ t('agents.event.history_title') }}</h2>
        <span class="ag__evt-meta">{{
          t('agents.event.page_info', { total: evtTotal, page, pages: totalPages })
        }}</span>
      </div>
      <div class="ag__evt-search">
        <input
          v-model="keyword"
          :placeholder="t('agents.event.search_placeholder')"
          @input="searchDebounced"
          @keydown.enter="searchNow"
          @change="searchNow"
        />
      </div>
      <div class="ag__evt-body">
        <table v-if="events.length" class="ag__evt-table">
          <thead>
            <tr>
              <th class="c-seq">{{ t('agents.event_table_seq') }}</th>
              <th class="c-time">{{ t('agents.event.table_time') }}</th>
              <th class="c-content">{{ t('agents.event.table_content') }}</th>
              <th class="c-issue">Issue</th>
              <th class="c-actor">{{ t('agents.event.table_actor') }}</th>
              <th class="c-dur">{{ t('agents.event.table_duration') }}</th>
              <th class="c-engine">{{ t('agents.event.table_engine') }}</th>
              <th class="c-status">{{ t('agents.event.table_status') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="ev in events"
              :key="ev.seq"
              :class="{ 'hl-flash': isHighlighted(ev) }"
              @click="rowClick(ev)"
            >
              <td class="seq">#{{ ev.seq || '?' }}</td>
              <td class="time">{{ fmtTime(ev.created_at) }}</td>
              <td class="content">{{ ev.summary || ev.title || ev.event_type }}</td>
              <td>
                <a
                  v-if="issueLink(ev)"
                  :href="issueLink(ev)"
                  target="_blank"
                  rel="noopener"
                  @click.stop=""
                  >{{ issueLabel(ev) }}</a
                >
                <span v-else>{{ issueLabel(ev) || '—' }}</span>
              </td>
              <td class="actor">{{ ev.actor ? '@' + ev.actor : '—' }}</td>
              <td class="dur">{{ ev.duration ? ev.duration + 's' : '—' }}</td>
              <td class="engine">{{ ev.backend || '—' }}</td>
              <td class="status">
                <span :class="statusCls(ev)">{{ statusText(ev) }}</span>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="ag__evt-empty">
          {{ keyword ? t('agents.event.search_no_result') : t('agents.event.none') }}
        </div>
      </div>
      <div class="ag__evt-pager">
        <span class="pager-meta">{{
          t('agents.event.page_info', { total: evtTotal, page, pages: totalPages })
        }}</span>
        <div class="pager-ctrl">
          <select v-model.number="pageSize" @change="firstPage()">
            <option :value="10">10</option>
            <option :value="20">20</option>
            <option :value="50">50</option>
          </select>
          <span class="rows-label">{{ t('agents.event.rows_per_page') }}</span>
          <AGButton variant="plain" size="sm" :disabled="page <= 1" @click="prevPage()">{{
            t('agents.event.prev_page')
          }}</AGButton>
          <span class="page-idx">{{ page }} / {{ totalPages }}</span>
          <AGButton variant="plain" size="sm" :disabled="page >= totalPages" @click="nextPage()">{{
            t('agents.event.next_page')
          }}</AGButton>
        </div>
      </div>
    </AGCard>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import AGCard from '@/components/ui/AGCard.vue'
import AGButton from '@/components/ui/AGButton.vue'

const G = globalThis

const props = defineProps({
  name: { type: String, required: true },
  data: { type: Object, required: true },
})

const events = ref(props.data.recent_events || [])
const evtTotal = ref(props.data.events_total || 0)
const page = ref(props.data.events_page || 1)
const pageSize = ref(props.data.events_page_size || 10)
const keyword = ref('')
let searchTimer = null

function t(key, params) {
  return G.I18N.t(key, params)
}
const HUES = {
  vod: 'var(--pk)',
  issue: 'var(--r)',
  coding: 'var(--a)',
  qa: 'var(--pu)',
  sre: 'var(--or)',
}
function hueOf(n) {
  return HUES[n] || 'var(--a)'
}

const meta = computed(function () {
  return (
    G._AGENT_TYPES[props.name] || {
      display_name: props.name,
      icon: '?',
      color: '#8e8e93',
      bg: '',
      description: '',
    }
  )
})
const typeDisplay = computed(function () {
  const key = 'agents.type.' + props.name + '.name'
  const val = t(key)
  return val !== key ? val : meta.value.display_name || props.name
})
const typeDesc = computed(function () {
  const key = 'agents.type.' + props.name + '.desc'
  const val = t(key)
  return val !== key ? val : meta.value.description || ''
})
const im = computed(function () {
  return (props.data && props.data.im) || {}
})
const imConvs = computed(function () {
  return (im.value.conversations || []).length
})
const m = computed(function () {
  return props.data.metrics || {}
})
const es = computed(function () {
  return m.value.event_stats || {}
})
const completed = computed(function () {
  return es.value.completed || 0
})
const running = computed(function () {
  return es.value.running || 0
})
const totalEvents = computed(function () {
  return m.value.total_events || 0
})
const engineText = computed(function () {
  return (
    props.data.backend ||
    (props.data.backends && props.data.backends.length ? props.data.backends.join(', ') : '')
  )
})
const engineDot = computed(function () {
  const dots = { opencode: '#6ea8fe', hermes: '#a78bfa', codearts: '#fbbf24', codex: '#34d399' }
  const l = (engineText.value || '').toLowerCase()
  if (l.includes('hermes')) return dots.hermes
  if (l.includes('opencode')) return dots.opencode
  if (l.includes('codearts')) return dots.codearts
  if (l.includes('codex')) return dots.codex
  return ''
})
const highlightId = computed(function () {
  return props.data.highlight_issue_id || ''
})
const totalPages = computed(function () {
  return Math.max(1, Math.ceil(evtTotal.value / pageSize.value))
})

function fmtTime(s) {
  return (G.fmtLocalTime(s) || '').substring(5, 16) || '--:--'
}
function normIss(s) {
  return String(s || '').replace(/^#/, '')
}
function isHighlighted(ev) {
  return highlightId.value && normIss(ev.issue_id) === normIss(highlightId.value)
}
function issueLabel(ev) {
  const raw = String(ev.issue_id || '')
  const isPR = raw.startsWith('#pr_')
  const num = isPR ? raw.replace(/^#pr_/, '').split('_')[0] : raw.replace(/^#/, '')
  return isPR && num ? 'PR #' + num : raw
}
function issueLink(ev) {
  const raw = String(ev.issue_id || '')
  const isPR = raw.startsWith('#pr_')
  const num = isPR ? raw.replace(/^#pr_/, '').split('_')[0] : raw.replace(/^#/, '')
  const path = isPR ? '/pull/' + num : '/issues/' + num
  return num && ev.repo_key ? G._vcsUrl(ev.repo_key, path) : ''
}
function statusCls(ev) {
  return ev.status === 'failed' ? 'tr' : ev.status === 'completed' ? 'tgx' : 'ty'
}
function statusText(ev) {
  const key = 'common.status.' + ev.status
  const val = t(key)
  return val !== key ? val : ev.status
}

function firstPage() {
  page.value = 1
  reload()
}
function prevPage() {
  page.value--
  reload()
}
function nextPage() {
  page.value++
  reload()
}

// Issue #127: debounced search
function searchDebounced() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(searchNow, 350)
}
function searchNow() {
  if (searchTimer) clearTimeout(searchTimer)
  page.value = 1
  reload()
}

// 事件重载（保留 URL state 的 issue_number 高亮参数）
function reload() {
  const repo = G.app.currentRepo()
  if (!repo || !props.name) return
  let qs = '?page=' + page.value + '&page_size=' + pageSize.value
  if (keyword.value.trim()) qs += '&keyword=' + encodeURIComponent(keyword.value.trim())
  try {
    const st = window.history.state
    if (st && st.param) qs += '&issue_number=' + encodeURIComponent(st.param)
  } catch (e) {
    /* ignore */
  }
  G.API.get('/agents/' + props.name + '/' + repo.owner + '/' + repo.name + qs)
    .then(function (d) {
      if (G._currentView !== props.name) return
      events.value = d.recent_events || []
      evtTotal.value = d.events_total || 0
    })
    .catch(function () {})
}

// Issue #108: 点击事件行清除 history state 中的 issue_number，返回时不再高亮
function rowClick(ev) {
  const cur = window.history.state
  if (cur && cur.param) {
    window.history.replaceState({ view: cur.view, param: null }, '', '#' + (cur.view || ''))
  }
  G.app.nav('event', ev.seq)
}

// Issue #294: VOD IM 解绑（只带 im_unbind 标记，保留引擎/技能/订阅配置）
function unbindVodIm() {
  const repo = G.app.currentRepo()
  if (!repo) return G.app.showToast(t('agents.toast.select_repo_first'))
  G.app.showConfirm(
    t('agents.config.unbind_confirm'),
    function () {
      G.API.put('/agents/vod/' + repo.owner + '/' + repo.name, { im_unbind: true })
        .then(function () {
          G.app.showToast(t('agents.config.unbind_success'))
          G.API.get('/agents/vod/' + repo.owner + '/' + repo.name)
            .then(function (d) {
              if (G._currentView === 'vod') G.renderAgent('vod', d)
            })
            .catch(function () {})
        })
        .catch(function (e) {
          G.app.showToast(t('agents.config.unbind_failed', { msg: e.message }))
        })
    },
    { danger: true }
  )
}

// Issue #108: 滚动定位到第一个高亮事件行
import { onMounted } from 'vue'
onMounted(function () {
  if (highlightId.value) {
    setTimeout(function () {
      const el = document.querySelector('.hl-flash')
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 120)
  }
})
</script>

<style scoped>
.ag {
  font-family: var(--f-family);
}
/* Liquid Glass head card: glass surface tinted with the agent hue
 * (color-mix resolves var() colors — the old meta.color+'33' concat produced
 * invalid CSS that got dropped, leaving a flat text dump) */
.ag__head {
  background: color-mix(in srgb, var(--glass-bg) 88%, var(--agent-hue) 12%);
  -webkit-backdrop-filter: blur(var(--glass-blur));
  backdrop-filter: blur(var(--glass-blur));
  border: 1px solid var(--glass-border);
  box-shadow: var(--shadow-1);
  border-radius: var(--r-card);
  padding: var(--s-4);
  display: flex;
  align-items: center;
  gap: var(--s-3);
  margin-bottom: var(--s-4);
}
.ag__icon {
  width: 44px;
  height: 44px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--agent-hue) 18%, transparent);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  flex-shrink: 0;
}
.ag__info {
  flex: 1;
  min-width: 0;
}
.ag__info h2 {
  font-size: var(--f-head-3);
  font-weight: 600;
  margin: 0 0 2px;
}
.ag__desc {
  font-size: var(--f-caption);
  color: var(--c-text-2);
}
.ag__engine {
  font-size: var(--f-caption);
  color: var(--c-text-3);
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
}
.ag__engine-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}
.ag__engine-none {
  color: var(--c-danger);
  font-weight: 700;
}
.ag__imcard {
  background: var(--c-bg-elevated);
  border: 1px solid var(--glass-border);
  border-radius: 18px;
  padding: var(--s-4) var(--s-5);
  margin-bottom: var(--s-4);
}
.ag__imhead {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--s-2);
}
.ag__imhead h3 {
  font-size: var(--f-body);
  font-weight: 600;
  margin: 0;
}
.ag__immeta {
  font-size: var(--f-caption);
  color: var(--c-text-3);
}
.ag__imrow {
  display: flex;
  align-items: center;
  gap: var(--s-3);
}
.ag__imrow label {
  font-size: var(--f-caption);
  color: var(--c-text-2);
  flex-shrink: 0;
}
.ag__imappid {
  flex: 1;
  word-break: break-all;
  font-size: var(--f-caption);
  color: var(--c-text-2);
}
.ag__imconvs {
  font-size: var(--f-caption);
  color: var(--c-text-3);
  margin-top: var(--s-1);
}
.ag__stats {
  display: flex;
  gap: var(--s-2);
  margin: var(--s-4) 0;
}
.ag__st {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: var(--s-3) var(--s-4);
  background: var(--c-bg);
  border-radius: var(--r-control);
  min-width: 70px;
}
.ag__st .v {
  font-size: var(--f-head-3);
  font-weight: 700;
  color: color-mix(in srgb, var(--agent-hue) 75%, var(--c-text));
}
.ag__st .l {
  font-size: 10px;
  color: var(--c-text-2);
}
.ag__evt-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--s-2);
}
.ag__evt-head h2 {
  font-size: var(--f-body);
  font-weight: 600;
  margin: 0;
}
.ag__evt-meta {
  font-size: var(--f-caption);
  color: var(--c-text-3);
}
.ag__evt-search {
  margin-bottom: var(--s-2);
}
.ag__evt-search input {
  width: 100%;
  padding: 7px 12px;
  border: 1px solid var(--glass-border);
  border-radius: var(--r-control);
  font-size: var(--f-caption);
  background: var(--c-bg);
  color: var(--c-text);
  outline: none;
  transition: border-color 0.2s;
}
.ag__evt-search input:focus {
  border-color: var(--c-accent);
}
.ag__evt-table {
  width: 100%;
  font-size: var(--f-caption);
  table-layout: fixed;
}
.ag__evt-table th {
  text-align: left;
  padding: 6px 10px;
  font-size: 10px;
  color: var(--c-text-3);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.ag__evt-table .c-seq {
  width: 60px;
}
.ag__evt-table .c-time {
  width: 130px;
}
.ag__evt-table .c-issue {
  width: 75px;
}
.ag__evt-table .c-actor {
  width: 85px;
}
.ag__evt-table .c-dur {
  width: 60px;
  text-align: center;
}
.ag__evt-table .c-engine {
  width: 160px;
}
.ag__evt-table .c-status {
  width: 70px;
  text-align: right;
}
.ag__evt-table td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--glass-border);
}
.ag__evt-table tbody tr {
  cursor: pointer;
}
.ag__evt-table tbody tr:hover td {
  background: var(--c-bg);
}
.ag__evt-table .seq {
  font-weight: 600;
  color: var(--c-text-2);
}
.ag__evt-table .time {
  white-space: nowrap;
  font-size: 10px;
  color: var(--c-text-3);
}
.ag__evt-table .content {
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ag__evt-table a {
  color: var(--c-accent);
  text-decoration: underline;
}
.ag__evt-table .actor {
  font-size: 10px;
  color: var(--c-text-2);
}
.ag__evt-table .dur {
  text-align: center;
  font-size: 10px;
  color: var(--c-text-3);
}
.ag__evt-table .engine {
  font-size: 10px;
  color: var(--c-text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ag__evt-table .status span {
  font-size: 10px;
  font-weight: 600;
}
.ag__evt-table .status .tr {
  color: var(--c-danger);
}
.ag__evt-table .status .tgx {
  color: var(--c-success);
}
.ag__evt-table .status .ty {
  color: var(--c-warning);
}
.ag__evt-empty {
  padding: var(--s-3) 0;
  font-size: var(--f-caption);
  color: var(--c-text-2);
}
.ag__evt-pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--s-2);
  padding: var(--s-3) 0 0;
  flex-wrap: wrap;
}
.ag__evt-pager .pager-meta {
  font-size: var(--f-caption);
  color: var(--c-text-3);
}
.ag__evt-pager .pager-ctrl {
  display: flex;
  align-items: center;
  gap: var(--s-2);
}
.ag__evt-pager select {
  padding: 4px 8px;
  border: 1px solid var(--glass-border);
  border-radius: 6px;
  font-size: 11px;
  background: var(--c-bg);
  color: var(--c-text);
  outline: none;
}
.ag__evt-pager .rows-label {
  font-size: 11px;
  color: var(--c-text-3);
}
.ag__evt-pager .page-idx {
  font-size: 11px;
  color: var(--c-text-3);
}
</style>
