<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <span class="back-btn" @click="$router.push('/sys/console')">
        <svg viewBox="0 0 24 24" width="16" height="16"><path d="M15 18l-6-6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        返回
      </span>
      <h1>{{ info.username || '用户详情' }}</h1>
      <span v-if="info.online" class="status-tag success">在线</span>
      <span v-else class="status-tag default">离线</span>
      <span class="t3" style="font-size:12px;margin-left:4px" v-if="info.lastActiveAt">最后活跃 {{ formatTime(info.lastActiveAt) }}</span>
      <div style="margin-left:auto;display:flex;gap:8px">
        <button class="btn btn-danger btn-sm" @click="resetToken" :disabled="resetting">{{ resetting ? '处理中...' : '强制下线' }}</button>
      </div>
    </div>

    <div v-if="loading" class="loading-spinner">加载中...</div>

    <div v-else style="padding:20px 40px">
      <!-- 基本信息（紧凑横排）+ 凭证配置 -->
      <div class="card" style="padding:0;margin-bottom:20px">
        <div class="section-header">
          <h3 class="sh-title">基本信息</h3>
          <span class="sh-count">凭证 {{ credentialConfigured }} / 4</span>
        </div>
        <div style="padding:20px 24px">
          <!-- 基本信息 -->
          <div class="info-row">
            <div class="info-item"><span class="info-label">用户名</span><b>{{ info.username || '—' }}</b></div>
            <div class="info-item"><span class="info-label">昵称</span>{{ info.nickname || '—' }}</div>
            <div class="info-item"><span class="info-label">来源</span>{{ info.gitcodeId ? 'GitCode 登录' : '其他' }}</div>
            <div class="info-item"><span class="info-label">创建时间</span>{{ formatTime(info.createdAt) }}</div>
            <div class="info-item"><span class="info-label">最后场景</span>{{ info.lastScene || '—' }}</div>
            <div class="info-item" v-if="info.gitcodePublishRepo"><span class="info-label">发布仓库</span><router-link to="/repos/manage" style="color:var(--primary);text-decoration:none">{{ info.gitcodePublishRepo }}</router-link></div>
          </div>

          <!-- 凭证配置（紧挨基本信息） -->
          <div class="cred-row">
            <div class="cred-chip" :class="{ on: info.hasGitcodeToken }">
              <span class="cred-chip-name">GitCode</span>
              <span class="cred-chip-status">{{ info.hasGitcodeToken ? '已配置' : '未配置' }}</span>
            </div>
            <div class="cred-chip" :class="{ on: info.hasHuaweiAk }">
              <span class="cred-chip-name">华为云</span>
              <span class="cred-chip-status">{{ info.hasHuaweiAk ? '已配置' : '未配置' }}</span>
            </div>
            <div class="cred-chip" :class="{ on: info.hasFeishu }">
              <span class="cred-chip-name">飞书</span>
              <span class="cred-chip-status">{{ info.hasFeishu ? '已配置' : '未配置' }}</span>
            </div>
            <div class="cred-chip" :class="{ on: info.hasCodeartsAk }">
              <span class="cred-chip-name">CodeArts</span>
              <span class="cred-chip-status">{{ info.hasCodeartsAk ? '已配置' : '未配置' }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 仓库与 Issue + 最近活动 -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px">
      <!-- 仓库与 Issue -->
      <div class="card" style="padding:0">
          <div class="section-header">
            <h3 class="sh-title">Issue 任务列表</h3>
            <div class="sh-actions">
              <button class="btn btn-sm" @click="loadRepoIssues()">刷新</button>
            </div>
          </div>
          <div style="padding:20px 24px">
              <!-- 仓库选择下拉（可选筛选） -->
              <div style="margin-bottom:16px">
                <select v-model="selectedRepo" class="page-size-select" style="width:100%;max-width:400px;padding:8px 12px" @change="loadRepoIssues(0)">
                  <option value="">全部仓库</option>
                  <option v-for="r in info.repoMatrix" :key="r.repoKey" :value="r.repoKey">{{ r.fullName }}</option>
                </select>
              </div>

              <!-- Issue 列表 -->
              <div style="margin-top:16px">
                <div v-if="repoIssuesLoading" style="text-align:center;padding:32px;color:var(--text-tertiary)">加载中...</div>
                <div v-else-if="repoIssues.length === 0" style="text-align:center;padding:32px;color:var(--text-tertiary)">该仓库暂无 Issue 数据</div>
                <div v-else>
                  <p class="t3" style="font-size:12px;margin-bottom:8px">{{ repoIssuesTotal }} 个 Issue — 点击展开</p>
                  <!-- Issue 卡片列表 -->
                  <div v-for="iss in repoIssues" :key="iss.issueId" class="tl-issue">
                    <div class="tl-issue-head" @click="toggleIssueTimeline(iss)">
                      <span class="tl-arrow">{{ expandedIssues.has(iss.issueId) ? '▾' : '▸' }}</span>
                      <b class="tl-issue-id">
                        <a :href="`https://gitcode.com/${iss.repoName || selectedRepoData?.fullName}/issues/${iss.issueId.replace('#','')}`" target="_blank" @click.stop class="tl-issue-link">
                          <span class="t3" style="font-size:11px">{{ iss.repoName || selectedRepoData?.fullName }}</span> {{ iss.issueId }}
                        </a>
                      </b>
                      <span class="tl-issue-title t3" v-if="iss.issueTitle" :title="iss.issueTitle">{{ iss.issueTitle }}</span>
                      <span class="tl-issue-count t3">{{ iss.eventCount ?? 0 }} 事件</span>
                      <span class="tl-issue-status" :class="iss.failed > 0 ? 'fail' : 'ok'">{{ iss.failed > 0 ? '✗ 有失败' : '✓ 正常' }}</span>
                      <span class="t3 tl-issue-time">{{ (iss.lastTaskAt || '').substring(0, 16).replace('T', ' ') }}</span>
                    </div>
                    <!-- 展开详情 -->
                    <div v-if="expandedIssues.has(iss.issueId)" class="tl-issue-body">
                      <div class="tl-view-tabs">
                        <button class="tl-view-btn" :class="{ active: issueViewModes[iss.issueId] !== 'events' }" @click="switchIssueView(iss, 'diagram')">📊 时序图</button>
                        <button class="tl-view-btn" :class="{ active: issueViewModes[iss.issueId] === 'events' }" @click="switchIssueView(iss, 'events')">📋 事件列表</button>
                      </div>
                      <div v-show="issueEventsLoading[iss.issueId]" class="tl-diagram-loading">加载中...</div>
                      <div v-show="!issueEventsLoading[iss.issueId] && issueViewModes[iss.issueId] !== 'events' && issueEvents[iss.issueId] && issueEvents[iss.issueId].length" class="tl-diagram-container" :ref="el => diagramRefs[iss.issueId] = el">
                        <div v-if="!renderedIssues.has(iss.issueId)" class="tl-diagram-loading">渲染中...</div>
                        <div v-else v-html="issueSvg(iss.issueId)" class="tl-diagram-svg"></div>
                      </div>
                      <div v-show="issueViewModes[iss.issueId] === 'events' && issueEvents[iss.issueId] && issueEvents[iss.issueId].length">
                        <table class="vod-table">
                          <thead>
                            <tr><th>Seq</th><th>事件</th><th>Agent</th><th>Engine</th><th>耗时</th><th>状态</th><th>时间</th></tr>
                          </thead>
                          <tbody>
                            <tr v-for="(ev, eidx) in issueEvents[iss.issueId]" :key="eidx" :class="{ 'tl-row-fail': ev.status === 'failed', 'tl-row-ok': ev.status === 'completed', 'tl-row-clickable': ev.taskId }" @click="ev.taskId ? openLogModal(ev.taskId, ev) : null">
                              <td class="t3">#{{ eidx + 1 }}</td>
                              <td><span :class="['status-tag', eventTypeClass(ev.eventType)]" style="font-size:11px">{{ eventTypeText(ev.eventType) }}</span></td>
                              <td><span class="status-tag info" style="font-size:11px">{{ ev.agentType || '—' }}</span></td>
                              <td class="t3" style="font-size:11px">{{ ev.engine || '—' }}<span v-if="ev.engineIp" style="font-size:10px;color:var(--text-tertiary);display:block">{{ ev.engineIp }}</span></td>
                              <td class="t3">{{ ev.duration ? Math.round(ev.duration) + 's' : '—' }}</td>
                              <td><span :class="['status-tag', ev.status === 'completed' ? 'success' : ev.status === 'failed' ? 'danger' : ev.status === 'running' ? 'warning' : 'default']" style="font-size:10px">{{ ev.status || '—' }}</span></td>
                              <td class="t3">{{ formatTime(ev.createdAt) }}</td>
                            </tr>
                          </tbody>
                        </table>
                      </div>
                      <div v-show="!issueEventsLoading[iss.issueId] && (!issueEvents[iss.issueId] || !issueEvents[iss.issueId].length)" class="tl-diagram-loading">暂无事件记录</div>
                    </div>
                  </div>
                  <!-- 分页 -->
                  <PaginationBar v-if="repoIssuesTotal > 0" v-model:page="repoIssuesPage" v-model:size="repoIssuesSize" :total="repoIssuesTotal" :total-pages="repoIssuesTotalPages" :size-options="[5, 10]" @change="loadRepoIssues()" />
                </div>
              </div>
          </div>
      </div>

      <!-- 最近活动 -->
      <div class="card" style="padding:0">
          <div class="section-header">
            <h3 class="sh-title">最近执行</h3>
            <div class="sh-actions">
              <button class="btn btn-sm" :style="activityFilter === 'recent' ? 'background:var(--primary);color:#fff;border-color:var(--primary)' : ''" @click="activityFilter = 'recent'">最近执行</button>
              <button class="btn btn-sm" :style="activityFilter === 'pending' ? 'background:var(--primary);color:#fff;border-color:var(--primary)' : ''" @click="activityFilter = 'pending'">进行中</button>
              <button class="btn btn-sm" :style="activityFilter === 'failed' ? 'background:var(--primary);color:#fff;border-color:var(--primary)' : ''" @click="activityFilter = 'failed'">最近失败</button>
            </div>
          </div>
          <div style="padding:20px 24px">
            <p class="t3" style="margin-bottom:12px">最多查询 5 条，当前 {{ filteredActivity.length }} 条</p>
            <table class="vod-table" v-if="filteredActivity.length">
              <thead>
                <tr><th style="width:20%;text-align:left">Issue</th><th style="width:15%;text-align:left">事件</th><th style="width:10%;text-align:left">Agent</th><th style="width:15%;text-align:left">Engine</th><th style="width:10%;text-align:left">耗时</th><th style="width:10%;text-align:left">状态</th><th style="width:20%;text-align:left">时间</th></tr>
              </thead>
              <tbody>
                <tr v-for="(t, idx) in filteredActivity" :key="t.id" style="cursor:pointer" @click="openLogModal(t.id, t)">
                  <td>
                    <a v-if="t.issueId" :href="`https://gitcode.com/${t.repoName}/issues/${t.issueId.replace('#','')}`" target="_blank" @click.stop style="color:var(--primary);text-decoration:none">{{ t.issueId }}</a><span v-else>—</span>
                    <div v-if="t.issueTitle" class="t3" :title="t.issueTitle" style="font-size:11px;color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ t.issueTitle }}</div>
                  </td>
                  <td><span :class="['status-tag', eventTypeClass(t.eventType)]" style="font-size:11px">{{ eventTypeText(t.eventType) }}</span></td>
                  <td><span class="status-tag info" style="font-size:11px">{{ t.agentType || '—' }}</span></td>
                  <td class="t3" style="font-size:11px">{{ t.engine || '—' }}<span v-if="t.engineIp" class="t3" style="font-size:10px;color:var(--text-tertiary);display:block">{{ t.engineIp }}</span></td>
                  <td class="t3">{{ formatDuration(t.duration) }}</td>
                  <td><span :class="['status-tag', t.status === 'completed' ? 'success' : t.status === 'failed' ? 'danger' : t.status === 'cancelled' ? 'default' : 'warning']" style="font-size:10px">{{ t.status === 'completed' ? '完成' : t.status === 'failed' ? '失败' : t.status === 'cancelled' ? '取消' : t.status }}</span></td>
                  <td class="t3">{{ formatTime(t.startedAt) }}</td>
                </tr>
              </tbody>
            </table>
            <p v-else class="t2" style="padding:20px;text-align:center">暂无记录</p>
          </div>
      </div>
      </div>
      </div>

    <!-- 任务日志弹窗 -->
    <div v-if="logModal.show" class="log-modal-overlay log-layer" @click.self="closeLogModal">
      <div class="log-modal-dialog">
        <div class="log-modal-head">
          <h3 class="log-modal-title">
            <span class="t3" v-if="logModal.info.repoKey">{{ logModal.info.repoKey.replace('gitcode:', '') }}</span>
            <b v-if="logModal.info.issueId">{{ logModal.info.issueId }}</b>
            <span v-if="logModal.info.agentType" class="status-tag info" style="font-size:11px">{{ logModal.info.agentType }}</span>
            <span class="t3" v-if="logModal.info.engine" style="color:var(--primary)">🔧 {{ logModal.info.engine }}</span>
          </h3>
          <button class="btn btn-sm" @click="closeLogModal">✕ 关闭</button>
        </div>
        <div class="log-timeline" v-if="logModal.info.status">
          <span class="log-tl-dot" :class="logModal.info.status === 'completed' ? 'ok' : logModal.info.status === 'failed' ? 'fail' : 'run'"></span>
          <span class="log-tl-text" v-if="logModal.info.createdAt">入队 {{ formatTime(logModal.info.createdAt).slice(11, 19) }}</span>
          <span class="log-tl-arrow" v-if="logModal.info.startedAt">→</span>
          <span class="log-tl-text" v-if="logModal.info.startedAt">开始 {{ formatTime(logModal.info.startedAt).slice(11, 19) }}</span>
          <span class="log-tl-arrow" v-if="logModal.info.completedAt">→</span>
          <span class="log-tl-text" v-if="logModal.info.completedAt">完成 {{ formatTime(logModal.info.completedAt).slice(11, 19) }}</span>
          <span class="log-tl-dur" v-if="logModal.info.startedAt && logModal.info.completedAt">⏱ {{ formatTaskDur(logModal.info) }}</span>
          <span class="log-tl-status" :class="logModal.info.status === 'completed' ? 'ok' : logModal.info.status === 'failed' ? 'fail' : 'run'">{{ logModal.info.status.toUpperCase() }}</span>
        </div>
        <div class="log-modal-body">
          <div v-if="logModal.loading" class="tl-diagram-loading">加载日志...</div>
          <div v-else-if="logModal.messages.length === 0" class="log-empty">
            <div v-if="logModal.info.resultText || logModal.info.errors" class="log-error-info">
              <div v-if="logModal.info.resultText" class="log-error-row">结果: {{ logModal.info.resultText }}</div>
              <div v-if="logModal.info.exitCode != null" class="log-error-row" :style="{ color: logModal.info.exitCode !== 0 ? 'var(--danger)' : 'var(--success)' }">Exit Code: {{ logModal.info.exitCode }}</div>
              <div v-if="logModal.info.errors" class="log-error-row" style="color:var(--danger)">{{ logModal.info.errors }}</div>
            </div>
            <div v-else>暂无日志记录</div>
          </div>
          <div v-else class="log-terminal">
            <div v-for="msg in logModal.messages" :key="msg.seq" :class="['log-line', 'log-type-' + msg.type]">
              <span class="log-time t3">{{ (msg.createdAt || '').slice(11, 19) }}</span>
              <span class="log-icon">{{ logTypeIcon(msg.type) }}</span>
              <span class="log-content">{{ msg.content || msg.tool || '' }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Toast -->
    <div v-if="toast.show" class="toast-message" :class="toast.type">{{ toast.msg }}</div>
  </div>
</template>

<script setup>
import { onMounted, ref, computed, watch, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import http from '../api'
import PaginationBar from '../components/PaginationBar.vue'

const route = useRoute()
const info = ref({})
const loading = ref(true)
const resetting = ref(false)
const activityFilter = ref('recent')
const runtimeExpanded = ref({})
const selectedRepo = ref('')
const pendingDetailOpen = ref(false)
const logModal = ref({ show: false, loading: false, messages: [], info: {} })
const diagramRefs = {}
const renderedIssues = ref(new Set())
const renderingIssues = ref(new Set())
const expandedIssues = ref(new Set())
const issueViewModes = ref({})
const issueEvents = ref({})  // {issueId: events[]}
const issueEventsLoading = ref({})  // {issueId: true/false}
const issueSvgs = ref({})  // {issueId: svgHtml}

function issueSvg(issueId) {
  return issueSvgs.value[issueId] || ''
}

const pendingTotal = computed(() => {
  if (!selectedRepoData.value) return 0
  return (selectedRepoData.value.queued ?? 0) + (selectedRepoData.value.dispatched ?? 0) + (selectedRepoData.value.stuck ?? 0)
})

function togglePendingDetail() {
  pendingDetailOpen.value = !pendingDetailOpen.value
}
async function openLogModal(taskId, t) {
  logModal.value = { show: true, loading: true, messages: [], info: { issueId: t.issueId, repoKey: t.repoName ? 'gitcode:' + t.repoName : '', agentType: t.agentType, skill: t.skill, engine: t.engine, status: t.status } }
  try {
    const [msgsResp, infoResp] = await Promise.all([
      http.get(`/runtime/tasks/${taskId}/messages`),
      http.get(`/runtime/tasks/${taskId}/info`)
    ])
    const info = infoResp.data || {}
    if (!info.repoKey && t.repoName) info.repoKey = 'gitcode:' + t.repoName
    if (!info.issueId) info.issueId = t.issueId
    if (!info.agentType) info.agentType = t.agentType
    if (!info.skill) info.skill = t.skill
    if (!info.engine) info.engine = t.engine
    if (!info.status) info.status = t.status
    logModal.value = { show: true, loading: false, messages: msgsResp.data || [], info }
  } catch (e) {
    logModal.value = { show: true, loading: false, messages: [], info: { error: e.message, issueId: t.issueId, repoKey: t.repoName ? 'gitcode:' + t.repoName : '' } }
  }
}
function closeLogModal() {
  logModal.value = { show: false, loading: false, messages: [], info: {} }
}
function formatTaskDur(t) {
  if (!t.startedAt || !t.completedAt) return '—'
  const s = (new Date(t.completedAt) - new Date(t.startedAt)) / 1000
  if (isNaN(s) || s <= 0) return '—'
  return formatDuration(Math.round(s) + 's')
}
function logTypeIcon(t) {
  const map = { step_start: '▶', step_finish: '✓', text: '💬', reasoning: '🤔', tool_use: '🔧', error: '❌', done: '✅' }
  return map[t] || '•'
}
async function toggleIssueTimeline(iss) {
  const id = iss.issueId
  const s = new Set(expandedIssues.value)
  if (s.has(id)) {
    s.delete(id)
  } else {
    s.add(id)
    if (!issueViewModes.value[id]) {
      issueViewModes.value = { ...issueViewModes.value, [id]: 'diagram' }
    }
  }
  expandedIssues.value = s
  // 展开时渲染
  if (s.has(id) && issueViewModes.value[id] !== 'events') {
    if (!issueEvents.value[id]) {
      // 首次展开：加载事件数据
      issueEventsLoading.value = { ...issueEventsLoading.value, [id]: true }
      try {
        const { data } = await http.get(`/console-users/${route.params.id}/issue-timeline`, {
          params: { repoKey: iss.repoKey, issueId: id }
        })
        issueEvents.value = { ...issueEvents.value, [id]: data.events || [] }
        issueEventsLoading.value = { ...issueEventsLoading.value, [id]: false }
        // 渲染时序图（setTimeout 等 Vue 渲染完 DOM）
        if (data.events && data.events.length) {
          setTimeout(() => renderMermaidForIssue(id, iss), 50)
        }
      } catch (e) {
        issueEvents.value = { ...issueEvents.value, [id]: [] }
        issueEventsLoading.value = { ...issueEventsLoading.value, [id]: false }
      }
    } else if (issueEvents.value[id] && issueEvents.value[id].length) {
      // 已有数据：重新渲染（v-html 重新渲染会丢失 data-task-id 和 onclick）
      if (renderedIssues.value.has(id)) {
        const { evTypes } = buildSequence(issueEvents.value[id])
        nextTick(() => bindMermaidClicks(id, iss, evTypes))
      } else {
        setTimeout(() => renderMermaidForIssue(id, iss), 50)
      }
    }
  }
}

function switchIssueView(iss, mode) {
  const id = iss.issueId
  issueViewModes.value = { ...issueViewModes.value, [id]: mode }
  if (mode === 'diagram' && issueEvents.value[id] && issueEvents.value[id].length) {
    if (renderedIssues.value.has(id)) {
      const { evTypes } = buildSequence(issueEvents.value[id])
      nextTick(() => bindMermaidClicks(id, iss, evTypes))
    } else {
      setTimeout(() => renderMermaidForIssue(id, iss), 50)
    }
  }
}

// ===== Mermaid 时序图 =====
let mermaidEngine = null

async function getMermaid() {
  if (!mermaidEngine) {
    const mod = await import('mermaid')
    mermaidEngine = mod.default
    mermaidEngine.initialize({
      startOnLoad: false, securityLevel: 'loose', theme: 'base', fontFamily: 'inherit',
      sequence: { mirrorActors: false, actorMargin: 60, messageMargin: 40, noteMargin: 10, boxMargin: 8, wrap: true, width: 160 }
    })
  }
  return mermaidEngine
}

function mermaidId(agent) {
  return (agent || 'agent').replace(/[^a-zA-Z0-9_-]/g, '_')
}

function buildSequence(events) {
  const agents = []
  for (const ev of events) {
    const a = ev.agentType || '—'
    if (!agents.includes(a)) agents.push(a)
  }
  const lines = ['sequenceDiagram', 'autonumber']
  for (const a of agents) lines.push(`    participant ${mermaidId(a)} as ${a}`)
  const statuses = []
  const evTypes = []
  let prev = null
  for (const ev of events) {
    const a = mermaidId(ev.agentType || '—')
    const t = ev.createdAt ? ev.createdAt.slice(11, 19) : ''
    const label = (t ? t + ' ' : '') + eventTypeText(ev.eventType)
    if (prev && a !== prev) {
      lines.push(`    ${prev}->>${a}: ${label}`)
      statuses.push(ev.status || '')
      evTypes.push('msg')
    } else {
      lines.push(`    Note over ${a}: ${label}`)
      statuses.push(ev.status || '')
      evTypes.push('note')
    }
    prev = a
  }
  return { text: lines.join('\n'), statuses, evTypes }
}

function applySequenceColors(svg, statuses) {
  try {
    const doc = new DOMParser().parseFromString(svg, 'image/svg+xml')
    const root = doc.documentElement
    const defs = root.querySelector('defs')
    if (!defs) return svg
    const colorOf = s => (s === 'failed' ? '#e5484d' : s === 'completed' ? '#2e9e5b' : '#94a3b8')
    const lines = Array.from(root.querySelectorAll('line[marker-start*="sequencenumber"]'))
    lines.forEach((line, i) => {
      const markerId = root.id + '-seqnum-' + i
      const marker = doc.createElementNS('http://www.w3.org/2000/svg', 'marker')
      marker.setAttribute('id', markerId)
      marker.setAttribute('refX', '15'); marker.setAttribute('refY', '15')
      marker.setAttribute('markerWidth', '60'); marker.setAttribute('markerHeight', '40')
      marker.setAttribute('orient', 'auto')
      const circle = doc.createElementNS('http://www.w3.org/2000/svg', 'circle')
      circle.setAttribute('cx', '15'); circle.setAttribute('cy', '15'); circle.setAttribute('r', '6')
      circle.setAttribute('fill', colorOf(statuses[i] || ''))
      marker.appendChild(circle)
      defs.appendChild(marker)
      line.setAttribute('marker-start', 'url(#' + markerId + ')')
    })
    return new XMLSerializer().serializeToString(doc)
  } catch (e) {
    return svg
  }
}

function fixLifelineHeight(svgStr, eventCount) {
  try {
    const doc = new DOMParser().parseFromString(svgStr, 'image/svg+xml')
    const root = doc.documentElement
    const heightAttr = root.getAttribute('height')
    const viewBox = root.getAttribute('viewBox')
    let svgHeight = 0
    if (viewBox) {
      const parts = viewBox.split(' ')
      svgHeight = parseFloat(parts[3]) || 0
    }
    if (!svgHeight && heightAttr) {
      svgHeight = parseFloat(heightAttr) || 0
    }
    if (svgHeight <= 0) return svgStr
    const minHeight = eventCount > 8 ? eventCount * 45 + 100 : 0
    if (minHeight <= 0 || svgHeight >= minHeight) return svgStr
    const allLines = Array.from(root.querySelectorAll('line'))
    const vLines = allLines.filter(l => {
      const x1 = parseFloat(l.getAttribute('x1') || 0)
      const x2 = parseFloat(l.getAttribute('x2') || 0)
      return Math.abs(x1 - x2) < 2
    })
    vLines.forEach(l => {
      const y2 = parseFloat(l.getAttribute('y2') || 0)
      if (y2 < minHeight - 20) {
        l.setAttribute('y2', String(minHeight - 10))
      }
    })
    root.setAttribute('height', String(minHeight))
    if (viewBox) {
      const parts = viewBox.split(' ')
      parts[3] = String(minHeight)
      root.setAttribute('viewBox', parts.join(' '))
    }
    return new XMLSerializer().serializeToString(doc)
  } catch (e) {
    return svgStr
  }
}

function bindMermaidClicks(issueId, iss, evTypes) {
  const el = diagramRefs[issueId]
  if (!el) return
  const svg = el.querySelector('svg')
  if (!svg) return
  const events = issueEvents.value[issueId] || []
  if (!events.length) return
  const msgTexts = Array.from(svg.querySelectorAll('text.messageText'))
  const noteTexts = Array.from(svg.querySelectorAll('text.noteText'))
  const taskEventMap = {}
  let msgIdx = 0, noteIdx = 0
  events.forEach((ev, i) => {
    const type = evTypes[i] || 'msg'
    let txt = null
    if (type === 'msg') {
      txt = msgTexts[msgIdx]
      msgIdx++
    } else {
      txt = noteTexts[noteIdx]
      noteIdx++
    }
    if (!txt) return
    if (ev.taskId) {
      txt.setAttribute('data-task-id', ev.taskId)
      txt.setAttribute('data-issue-id', iss.issueId)
      txt.style.cursor = 'pointer'
      txt.style.textDecoration = 'underline'
      txt.style.fill = 'var(--primary)'
      taskEventMap[ev.taskId] = ev
      // tooltip：引擎名
      const _eng = ev.engine || ''
      if (_eng) {
        const _t = document.createElementNS('http://www.w3.org/2000/svg', 'title')
        _t.textContent = _eng
        txt.appendChild(_t)
      }
    }
  })
  // 事件委托：避免 Vue v-html 重新渲染导致事件绑定丢失
  svg.onclick = function(e) {
    const target = e.target.closest('[data-task-id]')
    if (!target) return
    const taskId = target.getAttribute('data-task-id')
    if (taskId && taskEventMap[taskId]) {
      e.stopPropagation()
      openLogModal(taskId, iss)
    }
  }
}

async function renderMermaidForIssue(issueId, iss) {
  if (renderingIssues.value.has(issueId)) return
  const events = issueEvents.value[issueId] || []
  if (!events.length) return
  renderingIssues.value = new Set([...renderingIssues.value, issueId])
  try {
    const mmd = await getMermaid()
    const { text, statuses, evTypes } = buildSequence(events)
    const uid = 'mmd-' + Math.random().toString(36).slice(2, 10)
    try { await mmd.parse(text) } catch (e) { /* parse 失败也尝试 render */ }
    const result = await mmd.render(uid, text)
    const svg = result.svg || result
    const coloredSvg = applySequenceColors(svg, statuses)
    const fixedSvg = fixLifelineHeight(coloredSvg, events.length)
    issueSvgs.value = { ...issueSvgs.value, [issueId]: fixedSvg }
    renderedIssues.value = new Set([...renderedIssues.value, issueId])
    nextTick(() => bindMermaidClicks(issueId, iss, evTypes))
  } catch (err) {
    const { text } = buildSequence(events)
    issueSvgs.value = { ...issueSvgs.value, [issueId]: `<div style="padding:12px;color:var(--danger);font-size:12px">时序渲染失败: ${err.message || err}</div><pre style="padding:8px;background:#0c1222;color:#e0e6f0;font-size:10px;border-radius:6px;overflow-x:auto;white-space:pre-wrap">${text.replace(/</g, '&lt;')}</pre>` }
    renderedIssues.value = new Set([...renderedIssues.value, issueId])
  } finally {
    const s = new Set(renderingIssues.value)
    s.delete(issueId)
    renderingIssues.value = s
  }
}
function eventTypeText(t) {
  const map = { issue_opened: 'Issue创建', issue_comment: '评论', issue_updated: '更新', a2a_event: 'A2A交办', pull_request_opened: 'PR创建', pull_request_merged: 'PR合并', push: '推送', vod_signal: 'VOD信号', issue_closed: 'Issue关闭', onboarding_exp: 'VOD接入' }
  return map[t] || t || '—'
}
function eventTypeClass(t) {
  if (t === 'a2a_event') return 'info'
  if (t === 'pull_request_merged') return 'success'
  if (t === 'issue_opened') return 'info'
  return 'default'
}
function formatDuration(d) {
  if (!d) return '—'
  const m = d.match(/^(\d+)s$/)
  if (!m) return d
  const s = parseInt(m[1])
  if (s < 60) return s + 's'
  if (s < 3600) return Math.floor(s / 60) + 'm' + (s % 60) + 's'
  return Math.floor(s / 3600) + 'h' + Math.floor((s % 3600) / 60) + 'm'
}
const repoIssues = ref([])
const repoIssuesPage = ref(0)
const repoIssuesSize = ref(5)
const repoIssuesTotal = ref(0)
const repoIssuesTotalPages = ref(0)
const repoIssuesLoading = ref(false)
const toast = ref({ show: false, msg: '', type: 'success' })

const selectedRepoData = computed(() => {
  if (!selectedRepo.value) return null
  return (info.value.repoMatrix || []).find(r => r.repoKey === selectedRepo.value)
})

const filteredActivity = computed(() => {
  if (activityFilter.value === 'pending') return info.value.recentPendingTasks || []
  if (activityFilter.value === 'failed') return info.value.recentFailedTasks || []
  return info.value.recentTasks || []
})

const credentialConfigured = computed(() => {
  let count = 0
  if (info.value.hasGitcodeToken) count++
  if (info.value.hasHuaweiAk) count++
  if (info.value.hasFeishu) count++
  if (info.value.hasCodeartsAk) count++
  return count
})

async function loadRepoIssues(p) {
  if (p !== undefined) repoIssuesPage.value = p
  repoIssuesLoading.value = true
  try {
    const params = { page: repoIssuesPage.value, size: repoIssuesSize.value }
    if (selectedRepo.value) params.repoKey = selectedRepo.value
    const { data } = await http.get(`/console-users/${route.params.id}/repo-issues`, { params })
    repoIssues.value = data.content || []
    repoIssuesTotal.value = data.totalElements || 0
    repoIssuesTotalPages.value = data.totalPages || 0
  } catch (e) {
    console.error('[ConsoleUserDetail] loadRepoIssues failed', e)
    repoIssues.value = []
    repoIssuesTotal.value = 0
  } finally {
    repoIssuesLoading.value = false
  }
}

function formatTime(val) {
  if (!val || val === '—') return '—'
  try {
    let ts = val.length > 19 ? val.substring(0, 16) : val
    return ts.replace('T', ' ')
  } catch { return val }
}

async function load() {
  loading.value = true
  try {
    const { data } = await http.get(`/console-users/${route.params.id}`)
    info.value = data
  } catch (e) {
    console.error('[ConsoleUserDetail] load failed', e)
  } finally {
    loading.value = false
  }
}

async function resetToken() {
  if (!confirm(`确认强制 ${info.value.username} 下线？用户需要重新登录。`)) return
  resetting.value = true
  try {
    const { data } = await http.post(`/console-users/${route.params.id}/reset-token`)
    if (data.success) {
      toast.value = { show: true, msg: data.message, type: 'success' }
      setTimeout(() => { toast.value.show = false }, 3000)
      await load()
    } else {
      toast.value = { show: true, msg: data.message, type: 'error' }
      setTimeout(() => { toast.value.show = false }, 3000)
    }
  } catch (e) {
    toast.value = { show: true, msg: '操作失败', type: 'error' }
    setTimeout(() => { toast.value.show = false }, 3000)
  } finally {
    resetting.value = false
  }
}

onMounted(() => load())

// 页面加载后自动加载 Issue 列表
watch(() => info.value.repoMatrix, (val) => {
  if (val && !repoIssues.value.length) {
    loadRepoIssues(0)
  }
})
</script>

<style scoped>
.back-btn { display: inline-flex; align-items: center; gap: 4px; cursor: pointer; color: var(--text-secondary); font-size: 13px; transition: color var(--transition); }
.back-btn:hover { color: var(--primary); }

.info-row { display: flex; flex-wrap: wrap; gap: 20px 32px; margin-bottom: 16px; }
.info-item { display: flex; flex-direction: column; gap: 2px; }
.info-label { font-size: 11px; color: var(--text-tertiary); }

.cred-row { display: flex; gap: 10px; flex-wrap: wrap; padding-top: 16px; border-top: 1px solid var(--border-soft); }
.cred-chip { display: flex; flex-direction: column; align-items: center; gap: 2px; padding: 8px 16px; border-radius: 10px; background: rgba(0,0,0,0.04); min-width: 80px; }
.cred-chip.on { background: rgba(52,199,89,0.08); }
.cred-chip-name { font-size: 12px; font-weight: 600; color: var(--text-primary); }
.cred-chip-status { font-size: 10px; color: var(--text-tertiary); }
.cred-chip.on .cred-chip-status { color: #34c759; }

.activity-grid { display: grid; grid-template-columns: repeat(8, 1fr); gap: 6px; }
.repo-summary-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; }
.activity-card { text-align: center; padding: 14px 6px; background: rgba(0,0,0,0.02); border-radius: 10px; }
.activity-value { font-size: 20px; font-weight: 700; color: var(--text-primary); }
.activity-label { font-size: 11px; color: var(--text-secondary); margin-top: 4px; }
/* Issue 卡片列表（对齐健康中心 tl-* 样式 + 苹果风格优化） */
.tl-issue { border: 1px solid var(--border-color); border-radius: var(--radius); overflow: hidden; margin-bottom: 10px; transition: var(--transition); }
.tl-issue:hover { box-shadow: var(--card-shadow); transform: translateY(-1px); }
.tl-issue-head { display: flex; align-items: center; gap: 10px; padding: 12px 16px; cursor: pointer; background: transparent; transition: var(--transition); }
.tl-issue-head:hover { background: rgba(0,107,255,0.02); }
.tl-arrow { font-size: 12px; color: var(--text-tertiary); width: 14px; }
.tl-issue-id { font-size: 13px; min-width: 40px; }
.tl-issue-link { color: var(--primary); text-decoration: none; }
.tl-issue-link:hover { text-decoration: underline; }
.tl-issue-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.tl-issue-count { font-size: 11px; white-space: nowrap; }
.tl-issue-status { font-size: 10px; font-weight: 600; white-space: nowrap; }
.tl-issue-status.fail { color: var(--danger); }
.tl-issue-status.ok { color: var(--success); }
.tl-issue-time { font-size: 11px; white-space: nowrap; }
.tl-issue-body { border-top: 1px solid var(--border-soft); padding: 12px 16px; }
.tl-view-tabs { display: flex; gap: 0; margin-bottom: 10px; }
.tl-view-btn { padding: 6px 14px; font-size: 12px; border: 1px solid var(--border-color); background: transparent; color: var(--text-secondary); cursor: pointer; transition: var(--transition); }
.tl-view-btn:first-child { border-radius: var(--radius-sm) 0 0 var(--radius-sm); }
.tl-view-btn:last-child { border-radius: 0 var(--radius-sm) var(--radius-sm) 0; border-left: none; }
.tl-view-btn.active { background: var(--primary); color: #fff; border-color: var(--primary); }
.tl-diagram-container { overflow-x: auto; padding: 8px 0; min-height: 60px; }
.tl-diagram-loading { padding: 20px; text-align: center; color: var(--text-tertiary); font-size: 12px; }
.tl-diagram-svg svg { max-width: 100%; }
.tl-row-fail td { background: rgba(255,59,48,0.03) !important; }
.tl-row-ok td { background: rgba(52,199,89,0.03) !important; }
.tl-row-clickable { cursor: pointer; transition: var(--transition); }
.tl-row-clickable:hover td { background: rgba(0,107,255,0.03) !important; }
.act-icon-btn { background: none; border: none; cursor: pointer; padding: 4px; border-radius: 4px; color: var(--text-secondary); display: inline-flex; align-items: center; transition: var(--transition); }
.act-icon-btn:hover { background: rgba(0,107,255,0.08); color: var(--primary); }
/* 任务日志弹窗（对齐健康中心 log-* 样式） */
.log-modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.5); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.log-modal-overlay.log-layer { z-index: 1200; }
.log-modal-dialog { width: 860px; max-width: 94vw; max-height: 85vh; background: #fff; border-radius: var(--radius-lg); display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 8px 32px rgba(0,0,0,0.2); }
.log-modal-head { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; border-bottom: 1px solid var(--border-color); }
.log-modal-title { font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.log-timeline { display: flex; align-items: center; gap: 6px; padding: 8px 18px; border-bottom: 1px solid var(--border-soft); font-size: 11px; }
.log-tl-dot { width: 8px; height: 8px; border-radius: 50%; }
.log-tl-dot.ok { background: var(--success); } .log-tl-dot.fail { background: var(--danger); } .log-tl-dot.run { background: var(--warning); }
.log-tl-arrow { color: var(--text-tertiary); }
.log-tl-status { margin-left: auto; font-weight: 600; }
.log-tl-dur { color: var(--primary); font-weight: 600; }
.log-tl-status.ok { color: var(--success); } .log-tl-status.fail { color: var(--danger); } .log-tl-status.run { color: var(--warning); }
.log-modal-body { flex: 1; overflow-y: auto; padding: 14px 18px; }
.log-empty { text-align: center; padding: 32px; color: var(--text-tertiary); }
.log-error-info { text-align: left; background: #fff1f0; border: 1px solid #ffa39e; border-radius: 8px; padding: 12px; }
.log-error-row { font-size: 12px; margin-bottom: 4px; font-family: monospace; }
.log-terminal { background: #0c1222; border-radius: 8px; padding: 12px; max-height: 60vh; overflow-y: auto; }
.log-line { display: flex; align-items: flex-start; gap: 8px; padding: 3px 0; font-size: 12px; line-height: 1.6; border-bottom: 1px solid rgba(255,255,255,0.04); font-family: Menlo, Monaco, Consolas, monospace; }
.log-time { color: #5b667b; font-size: 10px; min-width: 55px; flex-shrink: 0; }
.log-icon { min-width: 16px; flex-shrink: 0; }
.log-content { color: #e0e6f0; word-break: break-all; white-space: pre-wrap; }
.log-type-step_start .log-content { color: #5b9eff; } .log-type-step_finish .log-content { color: #1ca44c; }
.log-type-text .log-content { color: #e0e6f0; } .log-type-reasoning .log-content { color: #b388ff; }
.log-type-tool_use .log-content { color: #ea5a0c; } .log-type-error .log-content { color: #dc2828; }
.log-type-done .log-content { color: #1ca44c; }

.timeline { display: flex; flex-direction: column; gap: 0; }
.timeline-item { display: flex; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border-soft); }
.timeline-item:last-child { border-bottom: none; }
.timeline-dot { width: 8px; height: 8px; border-radius: 50%; margin-top: 4px; flex-shrink: 0; }
.timeline-dot.completed { background: #34c759; }
.timeline-dot.failed { background: #ff3b30; }
.timeline-dot.pending { background: #ff9500; }
.timeline-content { flex: 1; min-width: 0; }
.timeline-header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.timeline-repo { font-size: 13px; font-weight: 500; color: var(--text-primary); }
.timeline-issue { font-size: 11px; color: var(--primary); background: rgba(0,107,255,0.06); padding: 1px 6px; border-radius: 6px; text-decoration: none; cursor: pointer; }
.timeline-issue:hover { background: rgba(0,107,255,0.12); }
.timeline-meta { display: flex; gap: 12px; margin-top: 2px; font-size: 11px; }
</style>
