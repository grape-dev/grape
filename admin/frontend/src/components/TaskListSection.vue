<template>
  <div>
      <!-- 关联任务 -->
      <div class="card" style="margin-top:16px;padding:0">
        <div class="section-header">
          <h5 class="sh-title">关联任务 <span class="sh-count">共 {{ tasks?.totalElements ?? 0 }} 条<span v-if="filterCount">（已筛选 {{ filterCount }} 条）</span></span></h5>
          <div class="sh-actions">
            <select v-if="engines" v-model="selectedEngineId" class="page-size-select" style="max-width:260px" @change="onEngineChange">
              <option :value="null">全部引擎</option>
              <option v-for="e in engines || []" :key="e.id" :value="e.id">{{ e.name }}</option>
            </select>
            <button v-if="hasAnyFilter" class="btn btn-sm" style="color:#ff3b30;border-color:rgba(255,59,48,0.2)" @click="clearFilters">清除筛选</button>
          </div>
        </div>
        <div style="padding:14px 20px">

        <!-- 任务状态统计卡（kpi 色带卡片，对齐 AgentManage/Overview 体系） -->
        <div class="task-stat-cards">
          <div class="task-stat-card kpi-primary">
            <div class="task-stat-num primary">{{ taskTotal }}</div>
            <div class="task-stat-label">任务总数</div>
          </div>
          <div v-for="s in taskStatList" :key="s.key" class="task-stat-card" :style="{ borderLeftColor: s.color }">
            <div class="task-stat-num" :style="{ color: s.color }">{{ s.count }}</div>
            <div class="task-stat-label">{{ s.label }}</div>
          </div>
        </div>

        <!-- 任务状态分布图（对齐 VOD chart-row） -->
        <div class="task-chart-row">
          <div class="card chart-card">
            <div class="section-header"><h5 class="sh-title">任务状态分布</h5></div>
            <div ref="taskChartEl" style="height:220px;padding:14px 20px"></div>
          </div>
        </div>

        <div class="table-loading-wrapper">
          <div class="table-loading-overlay" v-if="taskLoading">
            <span class="table-loading-text">加载中...</span>
          </div>
          <div style="overflow-x:auto">
          <table class="vod-table">
            <thead><tr>
              <th style="width:70px">ID</th>
              <th style="width:100px">Issue</th>
              <th style="width:110px" class="filterable-th">
                <span class="th-content" @click="toggleFilter('agentType')">
                  Agent <span class="filter-arrow" :class="{ active: taskType }">▼</span>
                </span>
                <div class="col-filter-drop" v-if="filterDropdown === 'agentType'" @click.stop>
                  <div class="col-filter-opt" :class="{ active: !taskType }" @click="setFilter('agentType', '')">全部</div>
                  <div class="col-filter-opt" v-for="(cnt, v) in filterOptions('agentType')" :key="v" :class="{ active: taskType === v }" @click="setFilter('agentType', v)">{{ v || '(空)' }} ({{ cnt }})</div>
                </div>
              </th>
              <th style="width:180px" class="filterable-th">
                <span class="th-content" @click="toggleFilter('skill')">
                  技能 <span class="filter-arrow" :class="{ active: taskSkill }">▼</span>
                </span>
                <div class="col-filter-drop" v-if="filterDropdown === 'skill'" @click.stop>
                  <div class="col-filter-opt" :class="{ active: !taskSkill }" @click="setFilter('skill', '')">全部</div>
                  <div class="col-filter-opt" v-for="(cnt, v) in filterOptions('skill')" :key="v" :class="{ active: taskSkill === v }" @click="setFilter('skill', v)">{{ v || '(空)' }} ({{ cnt }})</div>
                </div>
              </th>
              <th style="width:90px" class="filterable-th">
                <span class="th-content" @click="toggleFilter('status')">
                  状态 <span class="filter-arrow" :class="{ active: taskStatus }">▼</span>
                </span>
                <div class="col-filter-drop" v-if="filterDropdown === 'status'" @click.stop>
                  <div class="col-filter-opt" :class="{ active: !taskStatus }" @click="setFilter('status', '')">全部</div>
                  <div class="col-filter-opt" v-for="(cnt, v) in filterOptions('status')" :key="v" :class="{ active: taskStatus === v }" @click="setFilter('status', v)">{{ v || '(空)' }} ({{ cnt }})</div>
                </div>
              </th>
              <th style="width:70px">耗时</th>
              <th v-if="cols.engine" style="width:120px">引擎</th>
              <th style="width:150px" class="sortable-th" @click="toggleSort">
                创建时间
                <span class="sort-arrows">
                  <span class="sort-dir" :class="{ active: sortOrder === 'asc' }">▲</span>
                  <span class="sort-dir" :class="{ active: sortOrder === 'desc' }">▼</span>
                </span>
              </th>
            </tr></thead>
            <tbody>
              <tr v-if="!(tasks?.content || []).length" style="text-align:center">
                <td :colspan="visibleColumnCount" class="t2" style="padding:28px 0">
                  <template v-if="(tasks?.totalElements ?? 0) === 0">暂无关联任务</template>
                  <template v-else>
                    无匹配任务
                    <button v-if="hasAnyFilter" class="btn btn-sm" style="color:#ff3b30;border-color:rgba(255,59,48,0.2);margin-left:10px" @click="clearFilters">清除筛选</button>
                  </template>
                </td>
              </tr>
              <tr v-for="t in tasks?.content || []" :key="t.id" class="tl-row-clickable" @click="openLogModal(t.id, t)">
                <td class="t3">{{ (t.id || '').substring(0, 8) }}</td>
                <td style="white-space:nowrap">
                  <a v-if="t.issueId" :href="issueGitCodeUrl(t)" target="_blank" @click.stop style="color:var(--primary);text-decoration:none;font-weight:600">{{ t.issueId }}</a>
                  <span class="t3" v-if="t.eventSeq">#{{ t.eventSeq }}</span>
                  <button v-if="t.issueId" class="btn btn-sm" style="padding:1px 6px;margin-left:6px;font-size:11px" @click.stop="openIssueDetail(t.issueId, t.repoKey)" title="查看执行详情">📊</button>
                  <span v-if="!t.issueId" class="t3">—</span>
                </td>
                <td class="ellipsis-cell" :title="t.agentType">{{ t.agentType }}</td>
                <td class="ellipsis-cell" :title="t.skill">{{ t.skill }}</td>
                <td><span :class="['status-tag', t.status === 'completed' ? 'success' : t.status === 'failed' ? 'danger' : t.status === 'running' ? 'warning' : 'default']">{{ t.status }}</span></td>
                <td class="t3">{{ formatDuration(t.createdAt, t.completedAt) }}</td>
                <td v-if="cols.engine" class="t3" :title="t.engineName">{{ t.engineName || '—' }}</td>
                <td class="t3" :title="t.createdAt">{{ formatTime(t.createdAt) }}</td>
              </tr>
            </tbody>
          </table>
          </div>
        </div>
        <div v-if="tasks?.totalElements > 0" class="pagination-bar" style="border-top:none">
          <span class="page-info">第 <strong>{{ taskPage + 1 }}</strong> / {{ tasks?.totalPages ?? 1 }} 页（共 {{ tasks?.totalElements ?? 0 }} 条）</span>
          <select v-model="taskSize" class="page-size-select" @change="taskPage = 0; emitReload()">
            <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}条/页</option>
          </select>
          <button class="page-btn" :disabled="taskPage <= 0" @click="taskPage--; emitReload()">上一页</button>
          <button class="page-btn" :disabled="taskPage >= (tasks?.totalPages ?? 1) - 1" @click="taskPage++; emitReload()">下一页</button>
        </div>
        </div>
      </div>
    </div>

    <!-- Issue 详情弹窗（时序图 + 事件列表） -->
    <div v-if="issueModal.show" class="log-modal-overlay" @click.self="closeIssueModal">
      <div class="log-modal-dialog" style="width:1000px">
        <div class="log-modal-head">
          <h3 class="log-modal-title">
            <b>Issue {{ issueModal.issueId }}</b>
            <span class="t3" v-if="issueModal.repoName">{{ issueModal.repoName }}</span>
            <span class="t3" v-if="issueModal.eventCount">{{ issueModal.eventCount }} 个事件</span>
          </h3>
          <button class="btn btn-sm" @click="closeIssueModal">✕ 关闭</button>
        </div>
        <div style="padding:8px 20px;border-bottom:1px solid rgba(0,0,0,0.04)">
          <div class="tl-view-tabs">
            <button class="tl-view-btn" :class="{ active: issueModal.view === 'diagram' }" @click="issueModal.view = 'diagram'; renderIssueMermaid()">📊 时序图</button>
            <button class="tl-view-btn" :class="{ active: issueModal.view === 'events' }" @click="issueModal.view = 'events'">📋 事件列表</button>
          </div>
        </div>
        <div style="overflow-y:auto;flex:1;padding:14px 20px">
          <div v-if="issueModal.loading" class="tl-diagram-loading">加载中...</div>
          <div v-else-if="issueModal.view === 'diagram'" class="tl-diagram-container">
            <div v-if="!issueModal.svgRendered" class="tl-diagram-loading">渲染中...</div>
            <div v-else v-html="issueModal.svg" class="tl-diagram-svg"></div>
          </div>
          <div v-else-if="issueModal.view === 'events'">
            <table class="vod-table">
              <thead><tr><th>Seq</th><th>事件</th><th>Agent</th><th>Engine</th><th>Skill</th><th>状态</th><th>耗时</th><th>时间</th></tr></thead>
              <tbody>
                <tr v-for="ev in issueModal.events" :key="ev.seq" :class="{'tl-row-fail': ev.status === 'failed', 'tl-row-ok': ev.status === 'completed', 'tl-row-clickable': ev.taskId}" @click="ev.taskId ? openLogModal(ev.taskId, ev) : null">
                  <td class="t3">#{{ ev.seq }}</td>
                  <td><span :class="['status-tag', eventTypeClass(ev.eventType)]" style="font-size:11px">{{ eventTypeText(ev.eventType) }}</span></td>
                  <td><span class="status-tag info" style="font-size:11px">{{ ev.agentType || '—' }}</span></td>
                  <td class="t3" style="font-size:11px">{{ ev.engine || '—' }}</td>
                  <td class="t3">{{ ev.skill || '—' }}</td>
                  <td><span :class="['status-tag', ev.status === 'completed' ? 'success' : ev.status === 'failed' ? 'danger' : ev.status === 'running' ? 'warning' : 'default']">{{ ev.status }}</span></td>
                  <td class="t3">{{ ev.duration || '—' }}</td>
                  <td class="t3">{{ formatTime(ev.createdAt) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- 日志弹窗（对齐健康中心 log-* 样式） -->
    <div v-if="logModal.show" class="log-modal-overlay log-layer" @click.self="closeLogModal">
      <div class="log-modal-dialog">
        <div class="log-modal-head">
          <h3 class="log-modal-title">
            <span class="t3" v-if="logModal.info.repoKey">{{ logModal.info.repoKey.replace('gitcode:', '') }}</span>
            <b v-if="logModal.info.issueId">{{ logModal.info.issueId }}</b>
            <span class="t3" v-if="logModal.info.eventSeq">#{{ logModal.info.eventSeq }}</span>
            <span class="t3" v-if="logModal.info.createdAt">{{ formatTime(logModal.info.createdAt) }}</span>
          </h3>
          <button class="btn btn-sm" @click="closeLogModal">✕ 关闭</button>
        </div>
        <div class="log-timeline" v-if="logModal.info.status">
          <span class="log-tl-dot" :class="logModal.info.status === 'completed' ? 'ok' : logModal.info.status === 'failed' ? 'fail' : 'run'"></span>
          <span class="t3 log-tl-text">入队 {{ formatTime(logModal.info.createdAt).slice(11, 19) }}</span>
          <span class="log-tl-arrow" v-if="logModal.info.startedAt">→</span>
          <span class="t3 log-tl-text" v-if="logModal.info.startedAt">开始 {{ formatTime(logModal.info.startedAt).slice(11, 19) }}</span>
          <span class="log-tl-arrow" v-if="logModal.info.completedAt">→</span>
          <span class="t3 log-tl-text" v-if="logModal.info.completedAt">完成 {{ formatTime(logModal.info.completedAt).slice(11, 19) }}</span>
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
</template>

<script setup>
import { computed, nextTick, ref, watch } from "vue"
import * as echarts from "echarts"
import http from "../api"

const props = defineProps({
  tasks: { type: Object, default: () => ({}) },
  loading: { type: Boolean, default: false },
  columns: {
    type: Object,
    default: () => ({
      id: true, issue: true, agent: true, skill: true,
      status: true, duration: true, engine: false, createdAt: true
    })
  },
  engines: { type: Array, default: null }
})
const emit = defineEmits(["reload"])

const cols = computed(() => ({
  id: props.columns?.id !== false,
  issue: props.columns?.issue !== false,
  agent: props.columns?.agent !== false,
  skill: props.columns?.skill !== false,
  status: props.columns?.status !== false,
  duration: props.columns?.duration !== false,
  engine: props.columns?.engine === true,
  createdAt: props.columns?.createdAt !== false
}))

const visibleColumnCount = computed(() => {
  let n = 0
  for (const k of ["id","issue","agent","skill","status","duration","engine","createdAt"]) {
    if (cols.value[k]) n++
  }
  return n
})

const taskPage = ref(0)
const taskSize = ref(10)
const sizeOptions = [10, 20, 50]
const taskStatus = ref("")
const taskSkill = ref("")
const taskType = ref("")
const sortOrder = ref("desc")
const filterDropdown = ref("")
const filterCount = ref(0)
const taskLoading = ref(false)
const selectedEngineId = ref(null)
const hasAnyFilter = computed(() => !!(taskStatus.value || taskSkill.value || taskType.value || selectedEngineId.value))

function emitReload() {
  emit("reload", { taskPage: taskPage.value, taskSize: taskSize.value, sortOrder: sortOrder.value, taskStatus: taskStatus.value, taskSkill: taskSkill.value, taskType: taskType.value, engineId: selectedEngineId.value })
}
function toggleFilter(col) { filterDropdown.value = filterDropdown.value === col ? "" : col }
function setFilter(col, v) {
  if (col === "status") taskStatus.value = v
  else if (col === "skill") taskSkill.value = v
  else if (col === "agentType") taskType.value = v
  filterDropdown.value = ""
  taskPage.value = 0
  emitReload()
}
function clearFilters() { taskStatus.value = ""; taskSkill.value = ""; taskType.value = ""; selectedEngineId.value = null; taskPage.value = 0; emitReload() }
function onEngineChange() { taskPage.value = 0; emitReload() }
function toggleSort() { sortOrder.value = sortOrder.value === "desc" ? "asc" : "desc"; emitReload() }
function filterOptions(col) {
  const fo = props.tasks?.filterOptions
  if (!fo) return {}
  return fo[col === "agentType" ? "agentType" : col] || {}
}

const taskTotal = computed(() => props.tasks?.totalElements ?? 0)
const STATUS_MAP = {
  completed: { label: "已完成", color: "#34c759" },
  running: { label: "运行中", color: "#006eff" },
  failed: { label: "失败", color: "#ff2d92" },
  queued: { label: "排队中", color: "#ff9500" }
}
const taskStatList = computed(() => {
  const fo = props.tasks?.filterOptions
  if (!fo || !fo.status) return []
  return Object.entries(fo.status).map(([k, v]) => ({ key: k, label: STATUS_MAP[k]?.label || k, count: v, color: STATUS_MAP[k]?.color || "#999" }))
})

function formatTime(ts) {
  if (!ts) return "—"
  const time = new Date(String(ts).replace(" ", "T")).getTime()
  if (isNaN(time)) return String(ts)
  const diff = Date.now() - time
  if (diff < 60000) return "刚刚"
  if (diff < 3600000) return Math.floor(diff / 60000) + "分钟前"
  if (diff < 86400000) return Math.floor(diff / 3600000) + "小时前"
  return String(ts).slice(0, 16)
}
function formatDuration(start, end) {
  if (!start || !end) return "—"
  const ms = new Date(String(end).replace(" ", "T")) - new Date(String(start).replace(" ", "T"))
  if (isNaN(ms)) return "—"
  const s = Math.round(ms / 1000)
  if (s < 60) return s + "s"
  return Math.floor(s / 60) + "m" + (s % 60) + "s"
}
function issueGitCodeUrl(t) {
  const full = (t.repoKey || "").replace(/^gitcode:/, "")
  if (!full) return "#"
  return "https://gitcode.com/" + full + "/issues/" + (t.issueId || "").replace("#", "")
}

const logModal = ref({ show: false, loading: false, messages: [], info: {} })
async function openLogModal(taskId, task) {
  logModal.value = { show: true, loading: true, messages: [], info: { issueId: task?.issueId, repoKey: task?.repoKey, eventSeq: task?.eventSeq, createdAt: task?.createdAt } }
  try {
    const [msgsResp, infoResp] = await Promise.all([http.get(`/runtime/tasks/${taskId}/messages`), http.get(`/runtime/tasks/${taskId}/info`)])
    const info = infoResp.data || {}
    if (!info.repoKey && task?.repoKey) info.repoKey = task.repoKey
    logModal.value = { show: true, loading: false, messages: msgsResp.data || [], info }
  } catch (e) { logModal.value = { show: true, loading: false, messages: [], info: { error: e.message } } }
}
function closeLogModal() { logModal.value = { show: false, loading: false, messages: [], info: {} } }
function logTypeIcon(t) {
  const map = { step_start: "▶", step_finish: "✓", text: "💬", reasoning: "🤔", tool_use: "🔧", error: "❌", done: "✅" }
  return map[t] || "•"
}
function formatTaskDur(t) {
  if (!t.startedAt || !t.completedAt) return "—"
  try {
    const s = (new Date(t.completedAt) - new Date(t.startedAt)) / 1000
    if (isNaN(s) || s <= 0) return "—"
    if (s < 60) return Math.round(s) + "s"
    if (s < 3600) return Math.floor(s / 60) + "m" + (s % 60 > 0 ? Math.round(s % 60) + "s" : "")
    return Math.floor(s / 3600) + "h" + Math.floor((s % 3600) / 60) + "m"
  } catch { return "—" }
}

const issueModal = ref({ show: false, loading: false, issueId: "", repoName: "", eventCount: 0, events: [], view: "diagram", svg: "", svgRendered: false })
async function openIssueDetail(issueId, repoKey) {
  issueModal.value = { show: true, loading: true, issueId, repoName: "", eventCount: 0, events: [], view: "diagram", svg: "", svgRendered: false }
  try {
    const params = {}
    if (repoKey) params.repoKey = repoKey
    const { data } = await http.get(`/runtime/issues/${encodeURIComponent(issueId)}/events`, { params })
    const events = data.events || []
    const rk = events.length > 0 ? (events[0].repoKey || "") : ""
    issueModal.value = { show: true, loading: false, issueId, repoName: rk.replace(/^gitcode:/, ""), eventCount: events.length, events, view: "diagram", svg: "", svgRendered: false }
    setTimeout(() => renderIssueMermaid(), 50)
  } catch (e) { issueModal.value = { show: true, loading: false, issueId, events: [], view: "events", svg: "", svgRendered: false } }
}
function closeIssueModal() { issueModal.value = { show: false, loading: false, issueId: "", repoName: "", eventCount: 0, events: [], view: "diagram", svg: "", svgRendered: false } }

function eventTypeText(t) {
  const map = { issue_opened: "Issue创建", issue_comment: "评论", issue_updated: "更新", a2a_event: "A2A交办", pull_request_opened: "PR创建", pull_request_merged: "PR合并", push: "推送", vod_signal: "VOD信号", issue_closed: "Issue关闭" }
  return map[t] || t || "事件"
}
function eventTypeClass(t) {
  if (t === "a2a_event") return "info"
  if (t === "pull_request_merged") return "success"
  if (t === "issue_opened") return "info"
  return "default"
}

let mermaidEngine = null
async function getMermaid() {
  if (!mermaidEngine) {
    const mod = await import("mermaid")
    mermaidEngine = mod.default
    mermaidEngine.initialize({ startOnLoad: false, securityLevel: "loose", theme: "base", fontFamily: "inherit", sequence: { mirrorActors: false, actorMargin: 60, messageMargin: 40, noteMargin: 10, boxMargin: 8 } })
  }
  return mermaidEngine
}
function mermaidId(agent) { return (agent || "agent").replace(/[^a-zA-Z0-9_-]/g, "_") }
function buildSequence(events) {
  const agents = []
  for (const ev of events) { const a = ev.agentType || "—"; if (!agents.includes(a)) agents.push(a) }
  const lines = ["sequenceDiagram", "autonumber"]
  for (const a of agents) lines.push("    participant " + mermaidId(a) + " as " + a)
  const statuses = []
  const evTypes = []
  let prev = null
  for (const ev of events) {
    const a = mermaidId(ev.agentType || "—")
    const t = ev.createdAt ? String(ev.createdAt).slice(11, 19) : ""
    const label = (t ? t + " " : "") + eventTypeText(ev.eventType)
    if (prev && a !== prev) { lines.push("    " + prev + "->>" + a + ": " + label); statuses.push(ev.status || ""); evTypes.push("msg") }
    else { lines.push("    Note over " + a + ": " + label); statuses.push(ev.status || ""); evTypes.push("note") }
    prev = a
  }
  return { text: lines.join("\n"), statuses, evTypes }
}
function applySequenceColors(svg, statuses) {
  try {
    const doc = new DOMParser().parseFromString(svg, "image/svg+xml")
    const root = doc.documentElement
    const defs = root.querySelector("defs")
    if (!defs) return svg
    const colorOf = s => (s === "failed" ? "#e5484d" : s === "completed" ? "#2e9e5b" : "#aeaeb2")
    const lines = Array.from(root.querySelectorAll('line[marker-start*="sequencenumber"]'))
    lines.forEach((line, i) => {
      const markerId = root.id + "-seqnum-" + i
      const marker = doc.createElementNS("http://www.w3.org/2000/svg", "marker")
      marker.setAttribute("id", markerId)
      marker.setAttribute("refX", "15"); marker.setAttribute("refY", "15")
      marker.setAttribute("markerWidth", "60"); marker.setAttribute("markerHeight", "40")
      marker.setAttribute("orient", "auto")
      const circle = doc.createElementNS("http://www.w3.org/2000/svg", "circle")
      circle.setAttribute("cx", "15"); circle.setAttribute("cy", "15"); circle.setAttribute("r", "6")
      circle.setAttribute("fill", colorOf(statuses[i] || ""))
      marker.appendChild(circle)
      defs.appendChild(marker)
      line.setAttribute("marker-start", "url(#" + markerId + ")")
    })
    return new XMLSerializer().serializeToString(doc)
  } catch (e) { return svg }
}

function fixLifelineHeight(svgStr, eventCount) {
  try {
    const doc = new DOMParser().parseFromString(svgStr, "image/svg+xml")
    const root = doc.documentElement
    const heightAttr = root.getAttribute("height")
    const viewBox = root.getAttribute("viewBox")
    let svgHeight = 0
    if (viewBox) { const parts = viewBox.split(" "); svgHeight = parseFloat(parts[3]) || 0 }
    if (!svgHeight && heightAttr) { svgHeight = parseFloat(heightAttr) || 0 }
    if (svgHeight <= 0) return svgStr
    const minHeight = eventCount > 8 ? eventCount * 45 : 0
    if (minHeight <= 0 || svgHeight >= minHeight) return svgStr
    const allLines = Array.from(root.querySelectorAll("line"))
    const vLines = allLines.filter(l => {
      const x1 = parseFloat(l.getAttribute("x1") || 0)
      const x2 = parseFloat(l.getAttribute("x2") || 0)
      return Math.abs(x1 - x2) < 2
    })
    vLines.forEach(l => {
      const y2 = parseFloat(l.getAttribute("y2") || 0)
      if (y2 < minHeight - 20) { l.setAttribute("y2", String(minHeight - 10)) }
    })
    root.setAttribute("height", String(minHeight))
    if (viewBox) { const parts = viewBox.split(" "); parts[3] = String(minHeight); root.setAttribute("viewBox", parts.join(" ")) }
    return new XMLSerializer().serializeToString(doc)
  } catch (e) { return svgStr }
}

function bindMermaidClicks(events, evTypes) {
  const el = document.querySelector(".tl-diagram-svg")
  if (!el) return
  const svg = el.querySelector("svg")
  if (!svg) return
  if (!events.length) return
  const msgTexts = Array.from(svg.querySelectorAll("text.messageText"))
  const noteTexts = Array.from(svg.querySelectorAll("text.noteText"))
  const taskEventMap = {}
  let msgIdx = 0, noteIdx = 0
  events.forEach((ev, i) => {
    const type = evTypes[i] || "msg"
    let txt = null
    if (type === "msg") { txt = msgTexts[msgIdx]; msgIdx++ }
    else { txt = noteTexts[noteIdx]; noteIdx++ }
    if (!txt) return
    if (ev.taskId) {
      txt.setAttribute("data-task-id", ev.taskId)
      txt.style.cursor = "pointer"
      txt.style.textDecoration = "underline"
      txt.style.fill = "var(--primary)"
      taskEventMap[ev.taskId] = ev
      const _eng = ev.engine || ""
      if (_eng) { const _t = document.createElementNS("http://www.w3.org/2000/svg", "title"); _t.textContent = _eng; txt.appendChild(_t) }
    }
  })
  svg.onclick = function(e) {
    const target = e.target.closest("[data-task-id]")
    if (!target) return
    const taskId = target.getAttribute("data-task-id")
    if (taskId && taskEventMap[taskId]) {
      e.stopPropagation()
      openLogModal(taskId, taskEventMap[taskId])
    }
  }
}
async function renderIssueMermaid() {
  if (issueModal.value.svgRendered || issueModal.value.loading) return
  const events = issueModal.value.events
  if (!events || events.length === 0) return
  try {
    const mmd = await getMermaid()
    const { text, statuses, evTypes } = buildSequence(events)
    const uid = "mmd-" + Math.random().toString(36).slice(2, 10)
    try { await mmd.parse(text) } catch (e) {}
    const result = await mmd.render(uid, text)
    const svg = result.svg || result
    const coloredSvg = applySequenceColors(svg, statuses)
    const fixedSvg = fixLifelineHeight(coloredSvg, events.length)
    issueModal.value = { ...issueModal.value, svg: fixedSvg, svgRendered: true }
    nextTick(() => bindMermaidClicks(events, evTypes))
  } catch (err) {
    issueModal.value = { ...issueModal.value, svg: '<div style="padding:12px;color:var(--danger)">时序图渲染失败</div>', svgRendered: true }
  }
}

const taskChartEl = ref(null)
let taskChart = null
function renderTaskChart() {
  const el = taskChartEl.value
  if (!el) return
  const fo = props.tasks?.filterOptions
  if (!fo || !fo.status) return
  const data = Object.entries(fo.status).map(([k, v]) => ({ name: STATUS_MAP[k]?.label || k, value: v, itemStyle: { color: STATUS_MAP[k]?.color || "#999" } })).filter(d => d.value > 0)
  if (data.length === 0) { if (taskChart) { taskChart.dispose(); taskChart = null }; el.innerHTML = '<div style="text-align:center;padding:40px;color:#999">暂无数据</div>'; return }
  if (taskChart && taskChart.getDom() !== el) { taskChart.dispose(); taskChart = null }
  if (!taskChart) taskChart = echarts.init(el)
  taskChart.setOption({ tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" }, legend: { bottom: 0, textStyle: { fontSize: 11 } }, series: [{ type: "pie", radius: ["40%", "70%"], label: { show: true, formatter: "{b}\n{c}", fontSize: 11 }, data }] })
}
watch(() => props.tasks, () => {
  filterCount.value = (taskStatus.value || taskSkill.value || taskType.value || selectedEngineId.value) ? (props.tasks?.totalElements ?? 0) : 0
  requestAnimationFrame(() => nextTick(renderTaskChart))
}, { immediate: true, deep: true })

</script>

<style scoped>
.detail-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}
.detail-item {
  background: #fff;
  border: 1px solid var(--border-color);
  border-left: 3px solid var(--border-color);
  border-radius: 14px;
  padding: 10px 14px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  transition: all var(--transition);
  overflow: hidden;
}
.detail-item:hover { box-shadow: var(--card-shadow-hover); transform: translateY(-1px); }
.detail-item .t3 { display: block; font-size: 11px; font-weight: 400; color: var(--text-tertiary); margin-bottom: 3px; }
@media (max-width: 900px) {
  .detail-grid { grid-template-columns: repeat(2, 1fr); }
}

/* ===== 页头返回按钮（对齐 VOD 分析结果报表 back-btn） ===== */
.back-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 14px;
  font-weight: 500;
  color: var(--text-secondary);
  padding: 6px 14px;
  border-radius: 8px;
  background: rgba(0,0,0,0.04);
  border: 1px solid var(--border-color);
  cursor: pointer;
  user-select: none;
  transition: all var(--transition);
  line-height: 1;
}
.back-btn:hover { color: var(--primary); background: #e6f4ff; border-color: #91caff; transform: translateX(-2px); }
.back-btn:active { transform: translateX(0); }
.back-arrow { flex-shrink: 0; }

/* ===== 任务状态统计卡（kpi 色带卡片，对齐 AgentManage/Overview 体系） ===== */
.task-stat-cards {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.task-stat-card {
  flex: 1;
  min-width: 100px;
  max-width: 160px;
  background: #fff;
  border: 1px solid var(--border-color);
  border-radius: 14px;
  border-left: 3px solid #006eff;
  padding: 12px 16px;
  text-align: center;
  transition: all var(--transition);
}
.task-stat-card:hover { box-shadow: var(--card-shadow-hover); transform: translateY(-1px); }
.task-stat-card.kpi-primary { border-left-color: #006eff; }
.task-stat-num { font-size: 20px; font-weight: 700; line-height: 1.2; }
.task-stat-num.primary { color: var(--primary); }
.task-stat-label { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }

/* ===== 任务状态分布图（对齐 VOD chart-row） ===== */
.task-chart-row { margin-bottom: 14px; }
.task-chart-row .chart-card { padding: 14px 16px; }

/* ===== 表格加载遮罩（对齐 VOD table-loading-wrapper） ===== */
.table-loading-wrapper { position: relative; }
.table-loading-overlay {
  position: absolute;
  inset: 0;
  z-index: 30;
  background: rgba(255, 255, 255, 0.72);
  display: flex;
  align-items: center;
  justify-content: center;
}
.table-loading-text {
  font-size: 13px;
  color: var(--text-secondary);
  background: #fff;
  padding: 6px 14px;
  border-radius: 16px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}

/* ===== 列头筛选/排序（对齐 VOD 分析结果报表） ===== */
.filterable-th, .sortable-th { position: relative; cursor: pointer; user-select: none; }
.th-content { display: inline-flex; align-items: center; gap: 4px; }
.filter-arrow { font-size: 9px; color: var(--text-tertiary); }
.filter-arrow.active { color: var(--primary); }
.sort-arrows { display: inline-flex; flex-direction: column; line-height: 1; margin-left: 3px; vertical-align: middle; }
.sort-dir { font-size: 8px; color: var(--text-tertiary); }
.sort-dir.active { color: var(--primary); }
.col-filter-drop {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  z-index: 50;
  min-width: 150px;
  max-height: 240px;
  overflow-y: auto;
  background: #fff;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.12);
  padding: 6px;
}
.col-filter-opt {
  padding: 6px 10px;
  font-size: 12px;
  color: var(--text-secondary);
  border-radius: 8px;
  cursor: pointer;
  white-space: nowrap;
}
.col-filter-opt:hover { background: rgba(0,0,0,0.04); }
.col-filter-opt.active { background: var(--primary-bg); color: var(--primary); font-weight: 500; }
.ellipsis-cell {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
  vertical-align: bottom;
}

/* ===== 关联智能体卡片墙（对齐智能体管理页面 AgentManage） ===== */
.agent-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; margin-bottom: 18px; }
.agent-card {
  background: #fff; border: 1px solid var(--border-color); border-radius: 14px;
  box-shadow: var(--card-shadow); padding: 14px 16px; border-left: 3px solid #006eff;
  display: flex; flex-direction: column; gap: 10px; cursor: pointer;
}
.agent-card:hover { box-shadow: var(--card-shadow-hover); transform: translateY(-1px); }
.agent-card.kpi-blue { border-left-color: #006eff; }
.agent-card.kpi-purple { border-left-color: #5e5ce6; }
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
.as-item span { font-size: 11px; color: var(--text-tertiary); }
.sb-row { display: flex; gap: 6px; flex-wrap: wrap; }
.sb-seg { font-size: 11px; color: #fff; border-radius: 8px; padding: 1px 7px; }
.agent-foot { display: flex; align-items: center; justify-content: space-between; border-top: 1px solid var(--border-soft); padding-top: 8px; }
.cat-tag { font-size: 11px; padding: 1px 8px; border-radius: 14px; }
.cat-official { background: #e6f4ff; color: #006eff; }
.detail-link { font-size: 12px; color: var(--primary); }
/* 刷新按钮加载动画 */
.spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 2px solid rgba(0, 0, 0, 0.15);
  border-top-color: var(--primary, #006eff);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
  vertical-align: -2px;
  margin-right: 4px;
}
@keyframes spin { to { transform: rotate(360deg); } }
/* 日志弹窗（对齐健康中心 log-* 样式） */
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
/* Issue 时序图/事件列表 tab */
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

</style>
