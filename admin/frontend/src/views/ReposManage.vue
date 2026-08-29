<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>代码仓管理</h1>
      <span class="badge">{{ totalElements }} 个</span>
    </div>
    <div style="padding:20px 28px">
      <!-- 场景分类徽章行（点击联动筛选，对齐设备页 dm-stats 风格） -->
      <div class="dm-stats">
        <button class="dm-stat" :class="{ on: !scene }" @click="setScene('')">全部 <strong>{{ totalElements }}</strong></button>
        <button v-for="(cnt, s) in sceneStats" :key="s" class="dm-stat" :class="{ on: scene === s }" @click="setScene(s)">
          <span class="dm-dot" style="background:var(--text-tertiary)"></span>{{ s || 'default' }} <strong>{{ cnt }}</strong>
        </button>
      </div>

      <div class="toolbar">
        <input v-model="keyword" class="search-input" placeholder="搜索仓库名 / owner / repo_key" @keyup.enter="load" />
        <button class="btn" @click="load">查询</button>
        <button class="btn" @click="reset">重置</button>
        <div style="margin-left:auto;display:flex;gap:8px">
          <button class="btn" :style="view === 'list' ? activeTab : {}" @click="view = 'list'">列表</button>
          <button class="btn" :style="view === 'card' ? activeTab : {}" @click="view = 'card'">卡片</button>
        </div>
      </div>

      <!-- 列表模式 -->
      <div class="card" v-if="view === 'list'">
        <table class="vod-table">
          <thead>
            <tr><th>仓库</th><th>Owner</th><th>Webhook</th><th>场景</th><th>Bot</th><th>注册时间</th><th>活跃度</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in repos" :key="r.repoKey">
              <td>
                <strong>{{ r.repoName || r.fullName }}</strong>
                <div class="t3" style="font-size:11px">{{ r.repoKey }}</div>
              </td>
              <td>{{ r.repoOwner || '—' }}</td>
              <td class="t3">{{ r.webhookId ? '#' + r.webhookId : '—' }}</td>
              <td><span class="status-tag info" :title="sceneHint(r.devScene)">{{ r.devScene || 'default' }}</span></td>
              <td>{{ r.botUsername || '—' }}</td>
              <td class="t3">{{ r.createdAt || '—' }}</td>
              <td>
                <span :class="['status-tag', activityClass(r), 'act-tag']" :title="activityTitle(r)" @click="openTimeline(r)">
                  {{ r.eventCount || 0 }} 个事件<template v-if="r.eventCount"> · 失败率 {{ failRate(r) }}%</template>
                </span>
              </td>
              <td><a class="repo-link" :href="repoUrl(r)" target="_blank" rel="noopener">打开仓库 ↗</a></td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 卡片模式 -->
      <div v-else class="device-grid">
        <div v-for="r in repos" :key="r.repoKey" class="device-card repo-card">
          <div class="dc-head">
            <span class="repo-icon">📦</span>
            <span class="dc-name repo-name">{{ r.repoName || r.fullName }}</span>
            <span v-if="r.isAdmin === 1" class="status-tag success">管理员</span>
          </div>
          <div class="repo-key">{{ r.repoKey }}</div>
          <div class="dc-meta">
            <div><span class="t3" style="display:inline-block;min-width:60px">Owner</span> {{ r.repoOwner || '—' }}</div>
            <div><span class="t3" style="display:inline-block;min-width:60px">Webhook</span> {{ r.webhookId ? '#' + r.webhookId : '—' }}</div>
            <div>
              <span class="t3" style="display:inline-block;min-width:60px">场景</span>
              <span class="status-tag info" :title="sceneHint(r.devScene)">{{ r.devScene || 'default' }}</span>
            </div>
            <div><span class="t3" style="display:inline-block;min-width:60px">Bot</span> {{ r.botUsername || '—' }}</div>
            <div><span class="t3" style="display:inline-block;min-width:60px">注册时间</span> {{ r.createdAt || '—' }}</div>
            <div>
              <span class="t3" style="display:inline-block;min-width:60px">活跃度</span>
              <span :class="['status-tag', activityClass(r), 'act-tag']" :title="activityTitle(r)" @click="openTimeline(r)">
                {{ r.eventCount || 0 }} 个事件<template v-if="r.eventCount"> · 失败率 {{ failRate(r) }}%</template>
              </span>
            </div>
          </div>
          <div class="dc-actions">
            <a class="repo-link" :href="repoUrl(r)" target="_blank" rel="noopener">打开仓库 ↗</a>
          </div>
        </div>
      </div>

      <p v-if="!repos.length" class="t2" style="padding:16px;text-align:center">无匹配代码仓</p>

      <div v-if="totalElements > 0" class="pagination-bar">
        <span class="page-info">第 <strong>{{ page + 1 }}</strong> / {{ totalPages }} 页（共 {{ totalElements }} 个）</span>
        <select v-model="size" class="page-size-select" @change="page = 0; load()">
          <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}条/页</option>
        </select>
        <button class="page-btn" :disabled="page <= 0" @click="page--; load()">上一页</button>
        <button class="page-btn" :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
      </div>
    </div>
  </div>

  <!-- 时序弹窗（对齐全局 modal 样式：不透明白卡片） -->
  <div v-if="timelineRepo" class="modal-overlay" @click.self="closeTimeline">
    <div class="modal-dialog" style="width:960px;max-width:94vw;max-height:86vh;display:flex;flex-direction:column">
      <div class="modal-header">
        <span class="modal-icon">📈</span>
        <span class="modal-title">仓库「{{ timelineRepo.repoName || timelineRepo.repoKey }}」A2A 时序</span>
        <button class="btn" style="margin-left:auto;padding:2px 10px" @click="closeTimeline">✕</button>
      </div>
      <div class="modal-body" style="overflow-y:auto;flex:1;margin-bottom:16px">
        <div v-if="timelineLoading" class="t2" style="padding:30px;text-align:center">加载中...</div>
        <div v-else-if="!timelineIssues.length" class="t2" style="padding:30px;text-align:center">该仓库暂无 issue 事件记录</div>
        <div v-for="iss in timelineIssues" :key="iss.issueId" class="tl-row">
          <div class="tl-head" style="cursor:pointer" @click="toggleIssue(iss)">
            <span class="tl-toggle">{{ expandedIssues.has(iss.issueId) ? '▾' : '▸' }}</span>
            <code class="tl-issue">{{ iss.issueId }}</code>
            <a v-if="timelineRepo.repoKey" :href="issueGitCodeUrl(timelineRepo, iss.issueId)" target="_blank" @click.stop style="color:var(--primary);font-size:12px;text-decoration:none;margin-left:4px" title="在 GitCode 中打开">↗</a>
            <span class="t3 tl-title">{{ iss.title || '（无标题）' }}</span>
            <span class="t3" style="margin-left:auto">{{ iss.events.length }} 个事件</span>
          </div>
          <div v-if="expandedIssues.has(iss.issueId)" class="tl-mmd" :data-iid="iss.issueId"></div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn" @click="closeTimeline">关闭</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref, nextTick } from 'vue'
import http from '../api'

// mermaid 动态懒加载：仅打开时序弹窗渲染时才 import，页面冷启动不加载，避免影响整页

const repos = ref([])
const sceneStats = ref({})
const totalElements = ref(0)
const totalPages = ref(0)
const page = ref(0)
const size = ref(10)
const sizeOptions = [10, 20, 50]
const keyword = ref('')
const scene = ref('')
const view = ref('card')
const activeTab = { background: 'var(--primary)', color: '#fff', borderColor: 'var(--primary)' }

// ===== 时序弹窗 =====
const timelineRepo = ref(null)
const timelineIssues = ref([])
const timelineLoading = ref(false)
const expandedIssues = ref(new Set())

function toggleIssue(iss) {
  const s = new Set(expandedIssues.value)
  if (s.has(iss.issueId)) s.delete(iss.issueId); else s.add(iss.issueId)
  expandedIssues.value = s
  if (s.has(iss.issueId)) renderAllMermaid()
}

async function openTimeline(r) {
  if (!r.eventCount) { alert('该仓库暂无事件记录'); return }
  timelineRepo.value = r
  timelineIssues.value = []
  expandedIssues.value = new Set()
  timelineLoading.value = true
  try {
    const { data } = await http.get('/repos/timeline', { params: { repoKey: r.repoKey } })
    timelineIssues.value = data.issues || []
    renderAllMermaid()
  } catch (err) {
    alert(err.response?.data?.message || '时序加载失败')
  } finally {
    timelineLoading.value = false
  }
}
function closeTimeline() { timelineRepo.value = null; timelineIssues.value = []; expandedIssues.value = new Set() }

// ===== mermaid 时序图（懒加载引擎） =====
let mermaidEngine = null
async function getMermaid() {
  if (!mermaidEngine) {
    const mod = await import('mermaid')
    mermaidEngine = mod.default
    mermaidEngine.initialize({
      startOnLoad: false, securityLevel: 'loose', theme: 'base',
      fontFamily: 'inherit',
      sequence: { mirrorActors: false, actorMargin: 60, messageMargin: 40, noteMargin: 10, boxMargin: 8 }
    })
  }
  return mermaidEngine
}

function mermaidId(a) { return (a || 'agent').replace(/[^a-zA-Z0-9_-]/g, '_') }

function agentsOf(iss) {
  const seen = []
  for (const ev of iss.events) { const a = ev.agentType || '—'; if (!seen.includes(a)) seen.push(a) }
  return seen
}

function actionText(t) {
  if (t === 'a2a_event') return 'A2A 交办'
  return {
    issue_opened: 'issue 打开', issue_comment: '评论', issue_updated: 'issue 更新',
    pull_request_opened: 'PR 打开', note_hook_pr: 'PR 评论', issue_closed: 'issue 关闭', push: '推送'
  }[t] || t
}

// 按事件生成 mermaid 文本 + 每条消息对应的状态数组（与渲染行的顺序一致）
function buildSequence(iss) {
  const agents = agentsOf(iss)
  const lines = ['sequenceDiagram', 'autonumber']
  for (const a of agents) lines.push(`    participant ${mermaidId(a)} as ${a}`)
  const statuses = []
  let prev = null
  for (const ev of iss.events) {
    const a = mermaidId(ev.agentType || '—')
    const t = ev.createdAt ? ev.createdAt.slice(11) : ''
    const label = t ? `${t} ${actionText(ev.eventType)}` : actionText(ev.eventType)
    if (prev && a !== prev) {
      lines.push(`    ${prev}->>${a}: ${label}`)
      statuses.push(ev.status)
    }
    lines.push(`    ${a}->>${a}: ${label}`)
    statuses.push(ev.status)
    prev = a
  }
  return { text: lines.join('\n'), statuses }
}

// 渲染后按状态给序号圆点着色：mermaid 的序号点是共用 marker（黑色），这里为每条消息
// 克隆独立的彩色 marker 并改写引用，实现成功绿 / 失败红 / 进行中灰
function applySequenceColors(svg, statuses) {
  try {
    const doc = new DOMParser().parseFromString(svg, 'image/svg+xml')
    const root = doc.documentElement
    const defs = root.querySelector('defs')
    if (!defs) return svg
    // mermaid 的 actor 生命线固定 y2=2000，图超长时纵线消失——扩展到实际高度
    const vb = (root.getAttribute('viewBox') || '').trim().split(/\s+/)
    const svgH = parseFloat(vb[3]) || parseFloat(root.getAttribute('height')) || 0
    if (svgH > 0) {
      Array.from(root.querySelectorAll('line.actor-line')).forEach(l => l.setAttribute('y2', String(svgH)))
    }
    const colorOf = s => (s === 'failed' ? '#e5484d' : s === 'pending' ? '#94a3b8' : '#2e9e5b')
    const lines = Array.from(root.querySelectorAll('line[marker-start*="sequencenumber"]'))
    lines.forEach((line, i) => {
      const color = colorOf(statuses[i])
      const markerId = `${root.id}-seqnum-${i}`
      const marker = doc.createElementNS('http://www.w3.org/2000/svg', 'marker')
      marker.setAttribute('id', markerId)
      marker.setAttribute('refX', '15'); marker.setAttribute('refY', '15')
      marker.setAttribute('markerWidth', '60'); marker.setAttribute('markerHeight', '40')
      marker.setAttribute('orient', 'auto')
      const circle = doc.createElementNS('http://www.w3.org/2000/svg', 'circle')
      circle.setAttribute('cx', '15'); circle.setAttribute('cy', '15'); circle.setAttribute('r', '6')
      circle.setAttribute('fill', colorOf(statuses[i]))
      marker.appendChild(circle)
      defs.appendChild(marker)
      line.setAttribute('marker-start', `url(#${markerId})`)
    })
    return new XMLSerializer().serializeToString(doc)
  } catch (e) {
    return svg
  }
}

async function renderAllMermaid() {
  await nextTick()
  const boxes = document.querySelectorAll('.tl-mmd')
  if (!boxes.length) return
  let mmd
  try {
    mmd = await getMermaid()
  } catch (err) {
    for (const box of boxes) box.innerHTML = '<span class="t3">时序引擎加载失败：' + (err.message || err) + '</span>'
    return
  }
  for (const box of boxes) {
    const iss = timelineIssues.value.find(i => i.issueId === box.getAttribute('data-iid'))
    if (!iss || box.dataset.rendered) continue
    box.dataset.rendered = '1'
    box.innerHTML = '<span class="t3">渲染中…</span>'
    const uid = 'mmd-' + Math.random().toString(36).slice(2, 10)
    try {
      const { text, statuses } = buildSequence(iss)
      const { svg } = await mmd.render(uid, text)
      box.innerHTML = applySequenceColors(svg, statuses)
    } catch (err) {
      box.innerHTML = '<span class="t3">时序渲染失败：' + (err.message || err) + '</span>'
    }
  }
}

async function load() {
  const { data } = await http.get('/repos', { params: { keyword: keyword.value, scene: scene.value, page: page.value, size: size.value } })
  repos.value = data.content
  sceneStats.value = data.sceneStats || {}
  totalElements.value = data.totalElements
  totalPages.value = data.totalPages
}
function reset() { keyword.value = ''; scene.value = ''; page.value = 0; load() }
function setScene(s) { scene.value = s; page.value = 0; load() }

const SCENE_HINTS = {
  default: '默认开发场景', main: '主场景', quick: '快速迭代场景', stable: '稳定发布场景'
}
function sceneHint(s) { return SCENE_HINTS[s] || '开发场景（agent-skill-mapping 配置）' }
function repoUrl(r) {
  const full = r.fullName || (r.repoKey || '').replace(/^gitcode:/, '')
  return 'https://gitcode.com/' + full
}
function issueGitCodeUrl(repo, issueId) {
  const full = repo.fullName || (repo.repoKey || '').replace(/^gitcode:/, '')
  return 'https://gitcode.com/' + full + '/issues/' + issueId
}
function failRate(r) {
  if (!r.eventCount) return 0
  return Math.round((r.failedCount || 0) / r.eventCount * 100)
}
function activityClass(r) {
  if (!r.eventCount || r.eventCount === 0) return 'danger'
  if (failRate(r) > 10) return 'warning'
  if (!r.lastEventAt) return 'warning'
  const days = (Date.now() - new Date(r.lastEventAt.replace(' ', 'T')).getTime()) / 86400000
  if (days > 7) return 'warning'
  return 'success'
}
function activityTitle(r) {
  const parts = []
  if (r.lastEventAt) parts.push('最近事件: ' + r.lastEventAt)
  else parts.push('托管后暂无事件（webhook 可能异常）')
  if (r.eventCount && r.failedCount) parts.push('失败 ' + r.failedCount + '/' + r.eventCount)
  return parts.join('\n')
}

onMounted(load)
</script>

<style scoped>
.search-input {
  padding: 7px 12px;
  border: 1px solid var(--border, #e2e6ea);
  border-radius: 6px;
  font-size: 13px;
  min-width: 240px;
  background: #fff;
}
.repo-card { cursor: default; }
.repo-icon { font-size: 20px; }
.repo-name {
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.repo-key {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 11.5px;
  color: var(--text-tertiary, #8a919c);
  background: #f4f6f8;
  padding: 2px 8px;
  border-radius: 6px;
  display: inline-block;
  margin-bottom: 8px;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.repo-link {
  margin-left: auto;
  font-size: 12px;
  color: var(--primary, #4f6ef2);
  text-decoration: none;
}
.repo-link:hover { text-decoration: underline; }
/* 时序弹窗 */
.act-tag { cursor: pointer; }
.act-tag:hover { opacity: 0.85; }
.tl-row { border: 1px solid var(--border-color); border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; }
.tl-head { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.tl-toggle { color: #94a3b8; font-size: 12px; flex-shrink: 0; }
.tl-issue { font-weight: 600; color: var(--primary); }
.tl-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 55%; }
/* mermaid 时序图容器 */
.tl-mmd { overflow-x: auto; padding: 4px 0; }
.tl-mmd svg { max-width: 100%; height: auto; }
/* 场景分类徽章行（对齐设备页 dm-stats 风格） */
.dm-stats { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
.dm-stat {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; padding: 3px 10px; border-radius: 12px;
  background: rgba(0,0,0,0.04); color: var(--text-secondary); border: 1px solid transparent;
  cursor: pointer; transition: all 0.15s;
}
.dm-stat strong { font-weight: 600; color: var(--text-primary); }
.dm-stat:hover { border-color: var(--border-color); }
.dm-stat.on { background: var(--primary-bg); color: var(--primary); border-color: var(--primary); }
.dm-stat.on strong { color: var(--primary); }
.dm-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
</style>
