<template>
  <div class="pl">
    <!-- 流程图卡片 -->
    <div v-if="displayPipe" class="pl__flow-card">
      <div class="pl__flow-hdr">
        <div>
          <div class="pl__flow-title">{{ t('pipeline.page_title') }} · {{ data.repo || '' }}</div>
          <div class="pl__flow-sub">
            <a v-if="issueUrl" :href="issueUrl" target="_blank" rel="noopener"
              >{{ t('pipeline.issue_prefix') }} {{ displayPipe.issue_id }}</a
            >
            <template v-else
              >{{ t('pipeline.issue_prefix') }} {{ displayPipe.issue_id || '#?' }}</template
            >
            <template v-if="displayPipe.title">
              · <span class="sub-title">{{ displayPipe.title }}</span></template
            >
            <template v-if="displayPipe.current_owner">
              ·
              <span
                >{{ t('pipeline.current_owner') }}: <b>{{ displayPipe.current_owner }}</b></span
              ></template
            >
            <template v-if="displayPipe.executing">
              ⚡<span class="ok">{{ t('pipeline.executing') }}</span></template
            >
            <template v-else>
              <span>{{ t('pipeline.idle') }}</span></template
            >
          </div>
          <div v-if="crossSceneLabel" class="pl__cross-scene">⇄ {{ crossSceneLabel }}</div>
        </div>
        <div class="pl__stats">
          <div class="pl__stat">
            <div class="v ok">{{ data.active_count || 0 }}</div>
            <div class="l">{{ t('pipeline.stat_active') }}</div>
          </div>
          <div class="pl__stat">
            <div class="v accent">{{ data.today_count || 0 }}</div>
            <div class="l">{{ t('pipeline.stat_today') }}</div>
          </div>
          <div class="pl__stat">
            <div class="v err">{{ data.failed_count || 0 }}</div>
            <div class="l">{{ t('pipeline.stat_failed') }}</div>
          </div>
        </div>
      </div>
      <div class="pl__flow-grid">
        <div
          v-for="(node, i) in pipeNodes"
          :key="i"
          :class="['pl__nd', { dim: nodeStatus(i) === 0, run: nodeStatus(i) === 1 }]"
          @click="nodeClick(node)"
        >
          <!-- 执行中雷达扫描：与需求中心卡片（tsk-radar-sweep）同一视觉语言。
               此前这里只有 .pl__nd-dot 的 plPulse 缩放脉冲，节点卡片本身没有任何扫描效果，
               用户侧表现为「流水线显示执行中，但卡片没有雷达」。 -->
          <div
            v-if="nodeStatus(i) === 1"
            class="pl__nd-radar"
            :style="nodeRadarStyle(node, i)"
          ></div>
          <div
            class="pl__nd-bar"
            :style="{ background: nodeStatus(i) === 0 ? 'var(--c-text-3)' : nodeColor(node, i) }"
          ></div>
          <div class="pl__nd-name">
            <span
              class="pl__nd-dot"
              :style="{ background: nodeStatus(i) === 0 ? 'var(--c-text-3)' : nodeColor(node, i) }"
            ></span>
            {{ nodeLabel(node) }} <span class="mode-ic">{{ modeIcon(node) }}</span>
          </div>
          <div class="pl__nd-ev">{{ nodeEvent(i) }}</div>
          <div class="pl__nd-actor">{{ nodeActor(i) }}</div>
          <div class="pl__nd-status">
            <span class="d" :style="{ background: dotColor(i) }"></span>{{ statusText(i) }}
          </div>
        </div>
      </div>
    </div>

    <!-- Issue #89: 部分任务尚未生成流水线事件提示 -->
    <div v-if="data.has_tasks_without_events" class="pl__twe">
      <span class="warn-ic">⚠️</span>
      <span class="twe-text">{{ t('pipeline.tasks_without_events_hint') }}</span>
      <div class="twe-items">
        <span v-for="task in tweShown" :key="task.issue_id" class="tg tg-i"
          >{{ task.issue_id }} {{ task.title ? '· ' + task.title.substring(0, 30) : '' }}</span
        >
        <span v-if="tweMore" class="more">+{{ tweMore }}</span>
      </div>
    </div>

    <!-- 搜索框 -->
    <div class="pl__search-bar">
      <div class="pl__search-wrap">
        <span class="pl__search-ic">🔍</span>
        <input
          v-model="query"
          :placeholder="t('pipeline.search_placeholder')"
          @keyup.enter="applySearch"
          @change="applySearch"
        />
      </div>
    </div>

    <!-- 表格 -->
    <div class="pl__table-card">
      <div class="pl__table-wrap">
        <table class="pl__table">
          <thead>
            <tr>
              <th>{{ t('pipeline.th_issue') }}</th>
              <th>{{ t('pipeline.th_title') }}</th>
              <th>{{ t('pipeline.th_parent_issue') }}</th>
              <th>{{ t('pipeline.th_duration') }}</th>
              <th>{{ t('pipeline.th_agents') }}</th>
              <th>{{ t('pipeline.th_stage') }}</th>
              <th>{{ t('pipeline.th_scene') }}</th>
              <th>{{ t('pipeline.th_owner') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="p in pagePipes"
              :key="String(p.issue_id) + '-' + pagePipes.indexOf(p)"
              :class="{ sel: allPipes.indexOf(p) === activeIdx }"
              @click="selPipe(allPipes.indexOf(p))"
              @dblclick="rowDblClick(allPipes.indexOf(p))"
            >
              <td>
                <b>{{ p.issue_id || '?' }}</b
                ><br />
                <span v-if="p.executing" class="tg tg-c exec"
                  >⚡ {{ t('pipeline.executing') }}</span
                >
                <span v-else class="idle">{{ t('pipeline.idle') }}</span>
              </td>
              <td>
                <a v-if="rowIssueUrl(p)" :href="rowIssueUrl(p)" target="_blank" rel="noopener">{{
                  p.title || '—'
                }}</a>
                <span v-else>{{ p.title || '—' }}</span>
              </td>
              <td>
                <a v-if="parentIssueUrl(p)" :href="parentIssueUrl(p)" target="_blank" rel="noopener"
                  >#{{ parentIssueNum(p) }}</a
                >
                <span v-else class="dim">—</span>
              </td>
              <td>{{ fmtDur(p.duration) }}</td>
              <td>
                <span
                  v-for="(agentId, j) in agentIds"
                  :key="agentId"
                  :class="['tg', tagCls[j]]"
                  :style="agentTagStyle(p, j)"
                  @click.stop="agentTagClick(p, j)"
                  @mouseover="hoverScale($event, true)"
                  @mouseout="hoverScale($event, false)"
                >
                  {{ agentLabel(j).substring(0, 1) }}
                </span>
              </td>
              <td>
                <span :class="stageClass(p)">{{ stageName(p) }}</span>
              </td>
              <td>
                <span v-if="p.scene_id" class="tg tg-i">{{ sceneLabel(p) }}</span>
                <span v-else class="dim">—</span>
              </td>
              <td>{{ p.current_owner || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 分页 -->
    <div v-if="totalPages > 1" class="pl__pager">
      <button class="pl__pbtn" :disabled="page <= 1" @click="prevPage()">‹</button>
      <template v-for="n in pageButtons" :key="n">
        <span v-if="n === '...'" class="dots">…</span>
        <button v-else :class="['pl__pbtn', { act: n === page }]" @click="goPage(n)">
          {{ n }}
        </button>
      </template>
      <button class="pl__pbtn" :disabled="page >= totalPages" @click="nextPage()">›</button>
      <span class="pager-info">
        {{
          t('pipeline.pagination_total', {
            total: pagerTotal,
            page,
            pages: totalPages,
          })
        }}
        <template v-if="loadingMore"> · {{ t('common.loading') || '加载中' }}…</template>
      </span>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { stripRepoPrefix } from '../lib/repo-key.js'

const G = globalThis

const props = defineProps({ data: { type: Object, required: true } })

const PER_PAGE = 10
// Issue #542：history 增量分页——后端默认截断最近 50 条，翻页时按 offset 续拉
const HISTORY_PAGE = 100
const pipeData = ref(props.data)
const loadingMore = ref(false)
let pollTimer = null
const agentIds = ['vod', 'issue', 'coding', 'qa', 'sre']
const tagCls = ['tg-v', 'tg-i', 'tg-c', 'tg-q', 'tg-s']
const agentColors = ['#e86aa8', '#ff7a70', '#6fb4ff', '#b39bf2', '#ffc66e']
const TAG_COLORS = {
  vod: '#e86aa8',
  issue: '#ff7a70',
  coding: '#6fb4ff',
  qa: '#b39bf2',
  sre: '#ffc66e',
  chat: '#67cbe8',
}
const MODE_ICONS = { emit: '→', waterfall: '◇', serial: '↻', parallel: '⇄' }

const query = ref('')
const appliedQuery = ref('')
const page = ref(1)
const activeIdx = ref(0)

function t(key, params) {
  return G.I18N.t(key, params)
}
function agentLabel(idx) {
  const keys = [
    'pipeline.agent_vod',
    'pipeline.agent_issue',
    'pipeline.agent_coding',
    'pipeline.agent_qa',
    'pipeline.agent_sre',
  ]
  return t(keys[idx]) || agentIds[idx]
}
function stageLabelFn(stage) {
  const map = {
    vod: 'pipeline.agent_vod',
    issue: 'pipeline.agent_issue',
    coding: 'pipeline.agent_coding',
    qa: 'pipeline.agent_qa',
    sre: 'pipeline.agent_sre',
    done: 'pipeline.stage_done',
  }
  return map[stage] ? t(map[stage]) || stage : stage
}

const allPipes = computed(function () {
  const d = pipeData.value || {}
  const active = d.active
  const history = d.history || []
  return active ? [active, ...history] : history
})
const displayPipe = computed(function () {
  return allPipes.value[activeIdx.value] || (pipeData.value || {}).active
})
const filteredPipes = computed(function () {
  if (!appliedQuery.value) return allPipes.value
  const q = appliedQuery.value.toLowerCase().trim()
  if (!q) return allPipes.value
  return allPipes.value.filter(function (p) {
    const haystack = [
      String(p.issue_id || ''),
      p.title || '',
      String(p.parent_issue_id || ''),
      p.stage || '',
      p.scene_id || '',
      p.scene_name || '',
      p.current_owner || '',
    ]
      .join(' ')
      .toLowerCase()
    return haystack.includes(q)
  })
})
const totalPipes = computed(function () {
  return filteredPipes.value.length
})
// Issue #542：分页信息的总数以服务端全量为准（未加载的历史也算）
const pagerTotal = computed(function () {
  const d = pipeData.value || {}
  const cap = (d.active ? 1 : 0) + (typeof d.history_total === 'number' ? d.history_total : 0)
  return Math.max(totalPipes.value, cap)
})
const totalPages = computed(function () {
  // Issue #542：未加载的历史也算进页数（history_total = 后端全量条数）
  const d = pipeData.value || {}
  const loaded = totalPipes.value
  const hasTotal = typeof d.history_total === 'number' && d.history_total > 0
  const cap = hasTotal ? (d.active ? 1 : 0) + d.history_total : loaded
  return Math.max(1, Math.ceil(Math.max(loaded, cap) / PER_PAGE))
})
const pagePipes = computed(function () {
  const s = (page.value - 1) * PER_PAGE
  return filteredPipes.value.slice(s, s + PER_PAGE)
})
const pageButtons = computed(function () {
  if (totalPages.value <= 1) return []
  let s = Math.max(1, page.value - 3)
  let e = Math.min(totalPages.value, s + 6)
  if (e - s < 6) s = Math.max(1, e - 6)
  const out = []
  if (s > 1) {
    out.push(1)
    if (s > 2) out.push('...')
  }
  for (let i = s; i <= e; i++) out.push(i)
  if (e < totalPages.value) {
    if (e < totalPages.value - 1) out.push('...')
    out.push(totalPages.value)
  }
  return out
})

const pipeNodes = computed(function () {
  return getPipeNodes(pipeData.value, displayPipe.value)
})
function getPipeNodes(data, pipe) {
  let cfg = pipe && pipe.pipeline_config
  if (!cfg || !cfg.nodes || cfg.nodes.length === 0) cfg = data && data.pipeline_config
  if (cfg && cfg.nodes && cfg.nodes.length > 0) return cfg.nodes
  return agentIds.map(function (id, i) {
    return {
      id,
      tag: id,
      skill: '',
      mode: 'emit',
      next: i < agentIds.length - 1 ? agentIds[i + 1] : 'done',
      handoff: i < agentIds.length - 1 ? 'a2a' : 'complete',
    }
  })
}

const crossSceneLabel = computed(function () {
  const pipe = displayPipe.value
  if (!pipe) return ''
  const stages = pipe.stages || []
  for (let i = 0; i < stages.length; i++) {
    const ev = (stages[i] && stages[i].latest_event) || {}
    if (ev.event_type === 'cross_scene_handoff') {
      let label = t('pipeline.cross_scene_handoff') || '跨场景移交'
      if (ev.scene_id) label += ' → ' + ev.scene_id
      return label
    }
  }
  return ''
})

const issueUrl = computed(function () {
  const pipe = displayPipe.value
  if (!pipe || !props.data.repo_key) return ''
  const num = String(pipe.issue_id || '').replace(/^#/, '')
  return G._vcsUrl(props.data.repo_key, '/issues/' + num)
})

const tweShown = computed(function () {
  return (props.data.tasks_without_events || []).slice(0, 5)
})
const tweMore = computed(function () {
  const n = (props.data.tasks_without_events || []).length
  return n > 5 ? n - 5 : 0
})

function nodeStatus(i) {
  return (displayPipe.value.statuses || [])[i] || 0
}
function nodeEvent(i) {
  const ev = ((displayPipe.value.stages || [])[i] || {}).latest_event
  return ev ? ev.event_type || '—' : '—'
}
function nodeActor(i) {
  const ev = ((displayPipe.value.stages || [])[i] || {}).latest_event
  return ev && ev.actor ? '@' + ev.actor : ' '
}
function nodeColor(node, i) {
  const tag = node.tag || node.id
  return TAG_COLORS[tag] || agentColors[i % agentColors.length]
}
// 执行中雷达底色：取节点主题色 + 约 13% alpha 的扇形，旋转后形成扫描带
function nodeRadarStyle(node, i) {
  const c = nodeColor(node, i)
  return { background: 'conic-gradient(from 0deg, transparent 0deg, ' + c + '22 40deg, transparent 90deg)' }
}
function nodeLabel(node) {
  const tag = node.tag || node.id
  return (node.id || tag).charAt(0).toUpperCase() + (node.id || tag).slice(1)
}
function modeIcon(node) {
  return MODE_ICONS[node.mode] || MODE_ICONS.emit
}
function dotColor(i) {
  const sc = nodeStatus(i)
  return sc === 2
    ? '#72d98c'
    : sc === 1
      ? '#6fb4ff'
      : sc === 3
        ? '#ff7a70'
        : sc === 4
          ? '#9a9aa0'
          : sc === 5
            ? '#8e8e93'
            : '#9a9aa0'
}
function statusText(i) {
  const sc = nodeStatus(i)
  if (sc === 2) return t('pipeline.status_done')
  if (sc === 1) return t('pipeline.status_in_progress')
  if (sc === 3) {
    // Issue #2432: name the node that failed (no failure reason shown)
    const st = (displayPipe.value.stages || [])[i] || {}
    const ev = st.latest_event || {}
    const name =
      st.failed_node_name ||
      ev.failed_node_name ||
      st.name ||
      st.tag ||
      st.id ||
      displayPipe.value.failed_node_name ||
      ''
    return name ? t('pipeline.status_failed') + ' · ' + name : t('pipeline.status_failed')
  }
  if (sc === 4) return t('pipeline.status_cancelled') || '已取消'
  if (sc === 5) return t('pipeline.status_skipped') || '已跳过'
  return t('pipeline.status_pending')
}
function nodeClick(node) {
  const num = String(displayPipe.value.issue_id || '').replace(/^#/, '')
  G.app.nav(node.tag || node.id, num)
}

function selPipe(idx) {
  activeIdx.value = idx
}
function applySearch() {
  appliedQuery.value = query.value
  page.value = 1
  clampPage()
}
// ── Issue #542：history 增量分页 ──
function ownerRepo() {
  let rk = (pipeData.value && pipeData.value.repo_key) || (props.data && props.data.repo_key) || ''
  rk = stripRepoPrefix(rk)
  const i = rk.indexOf('/')
  if (i < 0) return ['', '']
  return [rk.slice(0, i), rk.slice(i + 1)]
}
function loadedCount() {
  const d = pipeData.value || {}
  return (d.active ? 1 : 0) + (d.history || []).length
}
// 翻到未加载的页 → 按 offset 续拉一段 history 并追加（按 issue_id 去重）
function ensurePage(target) {
  const d = pipeData.value
  if (!d) return
  if (target * PER_PAGE <= loadedCount()) return
  if (d.history_has_more === false) return
  if (loadingMore.value) return
  const pair = ownerRepo()
  if (!pair[0] || !pair[1]) return
  loadingMore.value = true
  G.API.get(
    '/pipeline/' +
      pair[0] +
      '/' +
      pair[1] +
      '?history_limit=' +
      HISTORY_PAGE +
      '&history_offset=' +
      (d.history || []).length
  )
    .then(function (r) {
      loadingMore.value = false
      if (G.window._currentView !== 'pipe') return
      const merged = (d.history || []).slice()
      const seen = {}
      merged.forEach(function (p) {
        seen[p.issue_id] = 1
      })
      ;((r && r.history) || []).forEach(function (p) {
        if (!p || seen[p.issue_id]) return
        merged.push(p)
        seen[p.issue_id] = 1
      })
      pipeData.value = Object.assign({}, d, {
        history: merged,
        history_has_more: !!(r && r.history_has_more === true),
      })
    })
    .catch(function () {
      loadingMore.value = false
      G.app.showToast(t('pipeline.load_failed') || '加载失败')
    })
}
// ── Issue #600：静默轮询——重派/新终态到达后卡片状态自动刷新 ──
function anyRunning() {
  return allPipes.value.some(function (p) {
    return (p.statuses || []).some(function (s) {
      return s === 1 // 1 = 进行中（后端数字状态码）
    })
  })
}
function refreshFirstScreen() {
  const d = pipeData.value
  if (!d) return
  const pair = ownerRepo()
  if (!pair[0] || !pair[1] || loadingMore.value) return
  G.API.get(
    '/pipeline/' + pair[0] + '/' + pair[1] + '?history_limit=' + HISTORY_PAGE + '&history_offset=0'
  )
    .then(function (r) {
      if (G.window._currentView !== 'pipe' || !r) return
      // active 直接替换；history 按 issue_id 原地更新，已深翻的部分保留
      const merged = (d.history || []).slice()
      const idx = {}
      merged.forEach(function (p, i) {
        idx[p.issue_id] = i
      })
      ;(r.history || []).forEach(function (p) {
        if (!p) return
        if (idx[p.issue_id] === undefined) merged.push(p)
        else merged[idx[p.issue_id]] = p
      })
      pipeData.value = Object.assign({}, d, {
        active: r.active || d.active,
        history: merged,
        history_total: r.history_total === undefined ? d.history_total : r.history_total,
        history_has_more:
          r.history_has_more === undefined ? d.history_has_more : !!r.history_has_more,
      })
    })
    .catch(function () {
      /* 静默：下一轮再试 */
    })
}
function goPage(p) {
  const tgt = Math.min(Math.max(1, p), totalPages.value)
  ensurePage(tgt)
  page.value = tgt
  clampPage()
}
function prevPage() {
  goPage(page.value - 1)
}
function nextPage() {
  goPage(page.value + 1)
}
function clampPage() {
  if (page.value > totalPages.value) page.value = 1
  if (page.value < 1) page.value = 1
}

function fmtDur(d) {
  return d ? (d >= 60 ? Math.floor(d / 60) + 'm ' + Math.round(d % 60) + 's' : d + 's') : '—'
}
function rowIssueUrl(p) {
  const prMatch = String(p.issue_id || '').match(/^PR#(\d+)$/)
  const num = String(p.issue_id || '').replace(/^#/, '')
  if (prMatch) return G._vcsUrl(props.data.repo_key || '', '/pull/' + prMatch[1])
  if (num) return G._vcsUrl(props.data.repo_key || '', '/issues/' + num)
  return ''
}
function parentIssueNum(p) {
  return String(p.parent_issue_id || '').replace(/^#/, '')
}
function parentIssueUrl(p) {
  const n = parentIssueNum(p)
  return n ? G._vcsUrl(props.data.repo_key || '', '/issues/' + n) : ''
}
function agentTagStyle(p, j) {
  const hasAgent = p.statuses && p.statuses[j] > 0
  const evSeq =
    (p.stages && p.stages[j] && p.stages[j].latest_event && p.stages[j].latest_event.seq) || ''
  if (hasAgent && evSeq) return 'cursor:pointer;transition:transform .15s'
  return 'opacity:0.3;cursor:default'
}
function agentTagClick(p, j) {
  const hasAgent = p.statuses && p.statuses[j] > 0
  const evSeq =
    (p.stages && p.stages[j] && p.stages[j].latest_event && p.stages[j].latest_event.seq) || ''
  if (hasAgent && evSeq) G.app.nav('event', String(evSeq))
}
function hoverScale(ev, on) {
  ev.target.style.transform = on ? 'scale(1.25)' : 'scale(1)'
}
function stageName(p) {
  return p.stage ? stageLabelFn(p.stage) || p.stage : '—'
}
function stageClass(p) {
  if (p.stage === 'done') return 'tgx'
  return agentIds.includes(p.stage) ? 'ty' : 't3'
}
function sceneLabel(p) {
  const labels = {
    default: t('pipeline.scene_default'),
    'cloud-native': t('pipeline.scene_cloud_native'),
    iac: t('pipeline.scene_iac'),
    'agent-dev': t('pipeline.scene_agent_dev'),
    'skill-dev': t('pipeline.scene_skill_dev'),
    'api-skill-dev': t('pipeline.scene_api_skill_dev'),
    'blog-publish': t('pipeline.scene_blog_publish'),
  }
  return p.scene_id ? labels[p.scene_id] || p.scene_name || p.scene_id : '—'
}

function rowDblClick(idx) {
  const p = allPipes.value[idx]
  if (!p) return
  const statuses = p.statuses || []
  const stages = p.stages || []
  for (let i = 0; i < statuses.length; i++) {
    if (statuses[i] === 1) {
      const ev = stages[i] && stages[i].latest_event
      if (ev && ev.seq) return G.app.nav('event', ev.seq)
    }
  }
  for (let i = statuses.length - 1; i >= 0; i--) {
    if (statuses[i] > 0) {
      const ev = stages[i] && stages[i].latest_event
      if (ev && ev.seq) return G.app.nav('event', ev.seq)
    }
  }
  const issNum = String(p.issue_id || '').replace(/^#/, '')
  const repo = G.stripRepoPrefix(props.data.repo || '')
  if (issNum && repo) {
    G.API.get('/events/' + encodeURIComponent(repo) + '?page=1&page_size=50').then(function (d) {
      const ev = (d.events || []).find(function (e) {
        return String(e.issue_id || '').replace(/^#/, '') === issNum
      })
      if (ev) G.app.nav('event', ev.seq)
    })
  }
}

// focusIssueNum parity: preselect the issue's pipeline
function focus(num) {
  const target = String(num).replace(/^#/, '')
  const idx = allPipes.value.findIndex(function (p) {
    return String(p.issue_id || '').replace(/^#/, '') === target
  })
  if (idx >= 0) {
    activeIdx.value = idx
    page.value = Math.floor(filteredPipes.value.indexOf(allPipes.value[idx]) / PER_PAGE) + 1
    clampPage()
  }
}
defineExpose({ focus })
// 换数据源（重新 renderPipeline）时重置分页与选中
watch(
  () => props.data,
  function (d) {
    pipeData.value = d
    page.value = 1
    activeIdx.value = 0
  }
)
onMounted(function () {
  // Issue #600：有进行中的流水线时每 30s 静默刷新，重派/终态到达无需手动刷新
  pollTimer = setInterval(function () {
    if (G.window._currentView !== 'pipe') return
    if (anyRunning()) refreshFirstScreen()
  }, 30000)
})
onBeforeUnmount(function () {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
})
</script>

<style scoped>
.pl {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 120px);
  overflow: hidden;
  gap: 12px;
  font-family: var(--f-family);
}
.pl__flow-card {
  background: var(--c-bg-elevated);
  border: 1px solid var(--glass-border);
  border-radius: 18px;
  box-shadow: var(--shadow-1);
  flex-shrink: 0;
  overflow: hidden;
}
.pl__flow-hdr {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 24px 6px;
}
.pl__flow-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--c-text);
}
.pl__flow-title a,
.pl__flow-sub a {
  color: var(--c-accent);
  text-decoration: underline;
}
.pl__flow-sub {
  font-size: 11px;
  color: var(--c-text-3);
  margin-top: 3px;
}
.pl__flow-sub .sub-title {
  color: var(--c-text-2);
  font-weight: 500;
}
.pl__flow-sub b {
  color: var(--c-accent);
}
.pl__flow-sub .ok {
  color: var(--c-success);
}
.pl__cross-scene {
  font-size: 10px;
  color: var(--c-accent);
  background: var(--c-accent-soft);
  border-radius: 6px;
  padding: 3px 8px;
  margin-top: 4px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.pl__stats {
  display: flex;
  gap: 6px;
}
.pl__stat {
  text-align: center;
  padding: 4px 14px;
  background: var(--c-bg);
  border-radius: 10px;
  min-width: 56px;
}
.pl__stat .v {
  font-size: 16px;
  font-weight: 700;
  line-height: 1.1;
}
.pl__stat .v.ok {
  color: var(--c-success);
}
.pl__stat .v.accent {
  color: var(--c-accent);
}
.pl__stat .v.err {
  color: var(--c-danger);
}
.pl__stat .l {
  font-size: 9px;
  color: var(--c-text-3);
  margin-top: 1px;
}
.pl__flow-grid {
  display: flex;
  gap: 8px;
  padding: 6px 24px 12px;
  overflow-x: auto;
}
.pl__nd {
  flex: 0 0 145px;
  min-height: 100px;
  background: var(--c-bg);
  border: 1px solid var(--glass-border);
  border-radius: 14px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  transition: all 0.2s;
  cursor: pointer;
  position: relative;
  overflow: hidden;
}
.pl__nd:hover {
  background: var(--c-bg-elevated);
  box-shadow: var(--shadow-2);
  transform: translateY(-1px);
}
.pl__nd-bar {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 3px;
  border-radius: 14px 14px 0 0;
}
.pl__nd-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--c-text);
  margin-bottom: 6px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.pl__nd-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}
.pl__nd-name .mode-ic {
  font-size: 9px;
  color: var(--c-text-3);
}
.pl__nd-ev {
  font-size: 10px;
  color: var(--c-text-3);
  line-height: 1.4;
  margin-bottom: 4px;
}
.pl__nd-actor {
  font-size: 9px;
  color: var(--c-text-3);
  margin-bottom: 6px;
}
.pl__nd-status {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 10px;
  font-weight: 500;
  margin-top: auto;
}
.pl__nd-status .d {
  width: 5px;
  height: 5px;
  border-radius: 50%;
}
.pl__nd.dim {
  opacity: 0.5;
  filter: grayscale(0.6);
}
.pl__nd.run {
  background: color-mix(in srgb, var(--c-warning) 12%, transparent);
}
.pl__nd.run .pl__nd-dot {
  animation: plPulse 1.5s ease-in-out infinite;
}
/* 执行中雷达扫描层：绝对定位铺满卡片，conic-gradient 旋转形成扫描带。
   放在内容之下（z-index:0），卡片内其余元素统一抬到 z-index:1 以保证文字清晰；
   overflow:hidden 已在 .pl__nd 上，旋转不会溢出卡片。 */
.pl__nd-radar {
  position: absolute;
  inset: 0;
  border-radius: 14px;
  pointer-events: none;
  z-index: 0;
  animation: plRadar 3s linear infinite;
}
/* 雷达层之下的内容统一抬到 z-index:1（保证文字压在扫描带之上）。
   注意必须**同时排除 .pl__nd-bar**：它是 position:absolute 的顶部色条，这条规则
   一旦把它改成 relative，它就从「贴卡片顶边」掉进 flex 流、被 14px 内边距顶下来，
   表现为每个职责卡片里的横线向下漂移（2026-11-16 修，引入于 8a928677f）。 */
.pl__nd > *:not(.pl__nd-radar):not(.pl__nd-bar) {
  position: relative;
  z-index: 1;
}
@keyframes plRadar {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
@keyframes plPulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.5;
    transform: scale(1.3);
  }
}
.pl__twe {
  background: color-mix(in srgb, var(--c-warning) 12%, transparent);
  border: 1px solid var(--c-warning);
  border-radius: 12px;
  padding: 10px 16px;
  margin: 0 0 8px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.pl__twe .warn-ic {
  font-size: 14px;
}
.pl__twe .twe-text {
  font-size: 12px;
  color: var(--c-text-3);
}
.pl__twe .twe-items {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 2px;
  flex-wrap: wrap;
}
.pl__twe .tg {
  font-size: 10px;
  margin: 2px;
}
.pl__twe .more {
  font-size: 10px;
  color: var(--c-text-3);
  margin-left: 4px;
}
.pl__search-bar {
  display: flex;
  align-items: center;
  flex-shrink: 0;
  padding: 6px 24px;
}
.pl__search-wrap {
  position: relative;
  flex: 0 1 300px;
}
.pl__search-ic {
  position: absolute;
  left: 10px;
  top: 50%;
  transform: translateY(-50%);
  font-size: 12px;
  color: var(--c-text-3);
  pointer-events: none;
}
.pl__search-wrap input {
  width: 100%;
  padding: 5px 10px 5px 30px;
  border: 1px solid var(--glass-border);
  border-radius: 8px;
  font-size: 12px;
  background: var(--c-bg-elevated);
  color: var(--c-text);
  outline: none;
  transition:
    border-color 0.2s,
    box-shadow 0.2s;
}
.pl__search-wrap input:focus {
  border-color: var(--c-accent);
}
.pl__table-card {
  background: var(--c-bg-elevated);
  border: 1px solid var(--glass-border);
  border-radius: 18px;
  box-shadow: var(--shadow-1);
  display: flex;
  flex-direction: column;
  flex: 1;
  overflow: hidden;
  min-height: 0;
}
.pl__table-wrap {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}
.pl__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.pl__table th {
  text-align: left;
  padding: 10px 14px;
  font-weight: 500;
  color: var(--c-text-3);
  border-bottom: 1px solid var(--glass-border);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  position: sticky;
  top: 0;
  background: var(--c-bg-elevated);
  z-index: 1;
}
.pl__table td {
  padding: 10px 14px;
  border-bottom: 1px solid var(--glass-border);
}
.pl__table tbody tr {
  cursor: pointer;
  transition: background 0.12s;
}
.pl__table tbody tr:hover td {
  background: var(--c-bg);
}
.pl__table tbody tr.sel td {
  background: var(--c-accent-soft);
  font-weight: 500;
}
.pl__table tbody tr:last-child td {
  border-bottom: none;
}
.pl__table a {
  color: var(--c-accent);
  text-decoration: underline;
  font-size: 11px;
}
.pl__table .dim {
  color: var(--c-text-3);
}
.pl__table .idle {
  color: var(--c-text-2);
  font-size: 11px;
}
.pl__table .exec {
  color: var(--on-accent);
  background: var(--c-success);
}
.pl__table .tg {
  font-size: 10px;
  margin-right: 2px;
}
.pl__pager {
  display: flex;
  align-items: center;
  gap: 3px;
  flex-shrink: 0;
  justify-content: flex-end;
  padding: 0 24px 4px;
}
.pl__pbtn {
  padding: 2px 7px;
  border-radius: 6px;
  border: 1px solid var(--glass-border);
  font-size: 10px;
  background: var(--c-bg-elevated);
  color: var(--c-text-2);
  cursor: pointer;
  transition: all 0.12s;
  line-height: 1.4;
}
.pl__pbtn:hover {
  border-color: var(--c-accent);
  color: var(--c-accent);
}
.pl__pbtn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.pl__pbtn.act {
  border-color: var(--c-accent);
  background: var(--c-accent);
  color: var(--on-accent);
  font-weight: 600;
}
.pl__pager .dots {
  color: var(--c-text-3);
  padding: 0 2px;
}
.pl__pager .pager-info {
  color: var(--c-text-3);
  margin-left: 8px;
  font-size: 10px;
}

/* 暗色档徽章（2026-11-20 主人反馈：issue 号 / 场景 / agent 一栏在暗色下不像一套语言）
 * 旧档来自 legacy app.css：20% 低透底（片底/卡片对比只有 1.08–1.20 = 无形）+ 400 档亮粉字，
 * 深色卡片上只剩「发光小字」，既没有形状也不是这套深色语言。
 * 改成与运行时页同一套口径：同色相 18% 底 + 34% 同色 inset 环 + 55%↔白提亮字；
 * 浅色档一个字节不动（仍由 app.css 的 pastel 档负责）。 */
:root[data-theme-mode='dark'] .pl .tg-v { --pl-tag: #e86aa8; }
:root[data-theme-mode='dark'] .pl .tg-i { --pl-tag: #ff7a70; }
:root[data-theme-mode='dark'] .pl .tg-c { --pl-tag: #6fb4ff; }
:root[data-theme-mode='dark'] .pl .tg-q { --pl-tag: #b39bf2; }
:root[data-theme-mode='dark'] .pl .tg-s { --pl-tag: #ffc66e; }
:root[data-theme-mode='dark'] .pl .tg-ch { --pl-tag: #67cbe8; }
:root[data-theme-mode='dark'] .pl :is(.tg-v, .tg-i, .tg-c, .tg-q, .tg-s, .tg-ch) {
  background: color-mix(in srgb, var(--pl-tag) 18%, transparent) !important;
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--pl-tag) 34%, transparent);
  color: color-mix(in srgb, var(--pl-tag) 55%, #fff) !important;
}
/* 「尚未生成流水线事件」提示条：框线用的是满饱和 --c-warning，深底上太跳，压到 35% */
:root[data-theme-mode='dark'] .pl .pl__twe {
  background: color-mix(in srgb, var(--c-warning) 10%, transparent);
  border-color: color-mix(in srgb, var(--c-warning) 35%, transparent);
}
/* 执行中标签原来是实心 --c-success + 白字（深底上整块发光），压成同族淡底 */
:root[data-theme-mode='dark'] .pl .pl__table .exec {
  background: color-mix(in srgb, var(--c-success) 20%, transparent) !important;
  color: color-mix(in srgb, var(--c-success) 60%, #fff) !important;
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--c-success) 38%, transparent);
}
@media (prefers-color-scheme: dark) {
  :root[data-theme-mode='auto'] .pl .tg-v { --pl-tag: #e86aa8; }
  :root[data-theme-mode='auto'] .pl .tg-i { --pl-tag: #ff7a70; }
  :root[data-theme-mode='auto'] .pl .tg-c { --pl-tag: #6fb4ff; }
  :root[data-theme-mode='auto'] .pl .tg-q { --pl-tag: #b39bf2; }
  :root[data-theme-mode='auto'] .pl .tg-s { --pl-tag: #ffc66e; }
  :root[data-theme-mode='auto'] .pl .tg-ch { --pl-tag: #67cbe8; }
  :root[data-theme-mode='auto'] .pl :is(.tg-v, .tg-i, .tg-c, .tg-q, .tg-s, .tg-ch) {
    background: color-mix(in srgb, var(--pl-tag) 18%, transparent) !important;
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--pl-tag) 34%, transparent);
    color: color-mix(in srgb, var(--pl-tag) 55%, #fff) !important;
  }
  :root[data-theme-mode='auto'] .pl .pl__twe {
    background: color-mix(in srgb, var(--c-warning) 10%, transparent);
    border-color: color-mix(in srgb, var(--c-warning) 35%, transparent);
  }
  :root[data-theme-mode='auto'] .pl .pl__table .exec {
    background: color-mix(in srgb, var(--c-success) 20%, transparent) !important;
    color: color-mix(in srgb, var(--c-success) 60%, #fff) !important;
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--c-success) 38%, transparent);
  }
}
</style>
