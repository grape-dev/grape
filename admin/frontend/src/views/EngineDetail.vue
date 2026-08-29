<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <span class="back-btn" @click="goBack">
        <svg class="back-arrow" viewBox="0 0 24 24" width="16" height="16"><path d="M15 18l-6-6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        返回
      </span>
      <h1>引擎详情</h1>
      <span class="badge" :style="badgeStyle(detail?.health)">{{ healthText(detail?.health) }}</span>
      <span class="t3" style="font-size:12px">{{ detail?.displayName || detail?.name || '' }} · {{ detail?.device || '' }} · {{ detail?.lastIp || '' }}</span>
    </div>

    <div class="dashboard" style="padding:20px 28px" v-if="detail">
      <!-- 关联智能体 -->
      <div class="card" style="margin-top:16px;padding:0">
        <div class="section-header">
          <h5 class="sh-title">关联智能体 <span class="sh-count">共 {{ agents.length }} 个</span></h5>
          <div class="sh-actions">
            <button class="btn btn-sm" :disabled="refreshing" @click="refresh" title="刷新智能体与任务数据">刷新</button>
          </div>
        </div>
        <div style="padding:14px 20px">
          <div style="position:relative">
            <div class="table-loading-overlay" v-if="refreshing">
              <span class="table-loading-text"><span class="spinner"></span>刷新中...</span>
            </div>
            <div class="agent-grid" v-if="agents.length">
            <div v-for="(a, i) in agents" :key="a.agentType" class="agent-card" :class="accents[i % accents.length]" @click="$router.push(`/agent/manage/${a.agentType}`)">
              <div class="agent-head">
                <div class="agent-icon" :style="iconStyle(i)">{{ a.icon || '🤖' }}</div>
                <div class="agent-meta">
                  <div class="agent-name">{{ a.displayName }}<span class="code-tag">{{ a.agentType }}</span></div>
                  <div class="agent-sub">顺序 #{{ a.orderNum }} · 引擎 {{ a.defaultRuntimeId || '-' }}</div>
                </div>
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
                <span class="cat-tag cat-official">{{ a.category === 'official' ? '官方' : '自定义' }}</span>
                <span class="detail-link">查看详情 →</span>
              </div>
            </div>
          </div>
          <p v-else class="t2" style="padding:12px;text-align:center">暂无关联智能体</p>
          </div>
        </div>
      </div>

      <!-- 关联任务 -->
      <div class="card" style="margin-top:16px;padding:0">
        <div class="section-header">
          <h5 class="sh-title">关联任务 <span class="sh-count">共 {{ detail.tasks?.totalElements ?? 0 }} 条<span v-if="filterCount">（已筛选 {{ filterCount }} 条）</span></span></h5>
          <div class="sh-actions">
            <button v-if="hasAnyFilter" class="btn btn-sm" style="color:#e5484d;border-color:#f3b4b6" @click="clearFilters">清除筛选</button>
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

        <!-- 筛选/排序控制栏（保留原有列头筛选） -->
        <div class="task-filter-bar">
          <div class="filter-chip-group">
            <span class="filter-chip" :class="{ active: filterDropdown === 'agentType' }" @click="toggleFilter('agentType')">
              类型 {{ taskType || '全部' }} <span class="filter-arrow" :class="{ active: taskType }">▼</span>
              <div class="col-filter-drop" v-if="filterDropdown === 'agentType'" @click.stop>
                <div class="col-filter-opt" :class="{ active: !taskType }" @click="setFilter('agentType', '')">全部</div>
                <div class="col-filter-opt" v-for="(cnt, v) in filterOptions('agentType')" :key="v" :class="{ active: taskType === v }" @click="setFilter('agentType', v)">{{ v || '(空)' }} ({{ cnt }})</div>
              </div>
            </span>
            <span class="filter-chip" :class="{ active: filterDropdown === 'skill' }" @click="toggleFilter('skill')">
              技能 {{ taskSkill || '全部' }} <span class="filter-arrow" :class="{ active: taskSkill }">▼</span>
              <div class="col-filter-drop" v-if="filterDropdown === 'skill'" @click.stop>
                <div class="col-filter-opt" :class="{ active: !taskSkill }" @click="setFilter('skill', '')">全部</div>
                <div class="col-filter-opt" v-for="(cnt, v) in filterOptions('skill')" :key="v" :class="{ active: taskSkill === v }" @click="setFilter('skill', v)">{{ v || '(空)' }} ({{ cnt }})</div>
              </div>
            </span>
            <span class="filter-chip" :class="{ active: filterDropdown === 'status' }" @click="toggleFilter('status')">
              状态 {{ taskStatus || '全部' }} <span class="filter-arrow" :class="{ active: taskStatus }">▼</span>
              <div class="col-filter-drop" v-if="filterDropdown === 'status'" @click.stop>
                <div class="col-filter-opt" :class="{ active: !taskStatus }" @click="setFilter('status', '')">全部</div>
                <div class="col-filter-opt" v-for="(cnt, v) in filterOptions('status')" :key="v" :class="{ active: taskStatus === v }" @click="setFilter('status', v)">{{ v || '(空)' }} ({{ cnt }})</div>
              </div>
            </span>
            <span class="filter-chip" @click="toggleSort">
              时间 {{ sortOrder === 'asc' ? '↑' : '↓' }}
            </span>
          </div>
          <span class="t3" style="font-size:12px">{{ issueGroups.length }} 个 Issue · {{ standaloneTasks.length }} 个独立任务</span>
        </div>

        <div class="table-loading-wrapper">
          <div class="table-loading-overlay" v-if="taskLoading">
            <span class="table-loading-text">加载中...</span>
          </div>

          <!-- 按 Issue 分组的任务列表 -->
          <div v-if="!(detail.tasks?.content || []).length" class="tl-diagram-loading" style="padding:28px 0">
            <template v-if="(detail.tasks?.totalElements ?? 0) === 0">暂无关联任务</template>
            <template v-else>
              无匹配任务
              <button v-if="hasAnyFilter" class="btn btn-sm" style="color:#e5484d;border-color:#f3b4b6;margin-left:10px" @click="clearFilters">清除筛选</button>
            </template>
          </div>

          <template v-else>
            <!-- Issue 分组 -->
            <div v-for="grp in issueGroups" :key="grp.issueId" class="tl-issue">
              <div class="tl-issue-head" @click="toggleIssue(grp.issueId)">
                <span class="tl-arrow">{{ expandedIssues.has(grp.issueId) ? '▾' : '▸' }}</span>
                <b class="tl-issue-id">{{ grp.issueId }}</b>
                <span class="tl-issue-count t3">{{ grp.tasks.length }} 任务</span>
                <span class="tl-issue-status" :class="grp.hasFailed ? 'fail' : 'ok'">{{ grp.hasFailed ? '✗ 有失败' : '✓ 正常' }}</span>
                <span class="t3 tl-issue-time">{{ (grp.lastTime || '').substring(0, 16) }}</span>
              </div>
              <div v-if="expandedIssues.has(grp.issueId)" class="tl-issue-body">
                <!-- 三个 tab -->
                <div class="tl-view-tabs">
                  <button class="tl-view-btn" :class="{ active: issueViewMode(grp.issueId) === 'diagram' }" @click="setIssueView(grp.issueId, 'diagram')">📊 时序图</button>
                  <button class="tl-view-btn" :class="{ active: issueViewMode(grp.issueId) === 'events' }" @click="setIssueView(grp.issueId, 'events')">📋 事件列表</button>
                  <button class="tl-view-btn" :class="{ active: issueViewMode(grp.issueId) === 'tasks' }" @click="setIssueView(grp.issueId, 'tasks')">📝 任务列表</button>
                </div>
                <!-- 时序图 -->
                <div v-show="issueViewMode(grp.issueId) === 'diagram'" class="tl-diagram-container">
                  <div v-if="!issueRendered(grp.issueId)" class="tl-diagram-loading">渲染中...</div>
                  <div v-else v-html="issueSvg(grp.issueId)" class="tl-diagram-svg"></div>
                </div>
                <!-- 事件列表 -->
                <div v-show="issueViewMode(grp.issueId) === 'events'">
                  <div v-if="issueEventsLoading(grp.issueId)" class="tl-diagram-loading">加载事件...</div>
                  <div v-else-if="!issueEvents(grp.issueId).length" class="tl-diagram-loading">暂无事件</div>
                  <table v-else class="vod-table">
                    <thead><tr><th>Seq</th><th>事件</th><th>Agent</th><th>Skill</th><th>状态</th><th>耗时</th><th>时间</th></tr></thead>
                    <tbody>
                      <tr v-for="ev in issueEvents(grp.issueId)" :key="ev.seq" :class="{ 'tl-row-fail': ev.status === 'failed', 'tl-row-ok': ev.status === 'completed', 'tl-row-clickable': ev.taskId }" @click="ev.taskId ? openLogModal(ev.taskId, { issueId: grp.issueId, repoKey: grp.tasks[0]?.repoKey }) : null">
                        <td class="t3">#{{ ev.seq }}</td>
                        <td><span :class="['status-tag', eventTypeClass(ev.eventType)]" style="font-size:11px">{{ eventTypeText(ev.eventType) }}</span></td>
                        <td><span class="status-tag info" style="font-size:11px">{{ ev.agentType || '—' }}</span></td>
                        <td class="t3">{{ ev.skill || '—' }}</td>
                        <td><span :class="['status-tag', ev.status === 'completed' ? 'success' : ev.status === 'failed' ? 'danger' : ev.status === 'running' ? 'warning' : 'default']">{{ ev.status }}</span></td>
                        <td class="t3">{{ formatDurationNum(ev.duration) }}</td>
                        <td class="t3">{{ formatTime(ev.createdAt) }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <!-- 任务列表 -->
                <div v-show="issueViewMode(grp.issueId) === 'tasks'">
                  <table class="vod-table">
                    <thead><tr><th>ID</th><th>Agent</th><th>Skill</th><th>状态</th><th>耗时</th><th>Exit</th><th>创建时间</th></tr></thead>
                    <tbody>
                      <tr v-for="t in grp.tasks" :key="t.id" class="tl-row-clickable" @click="openLogModal(t.id, t)">
                        <td class="t3">{{ (t.id || '').substring(0, 8) }}</td>
                        <td class="ellipsis-cell" :title="t.agentType">{{ t.agentType }}</td>
                        <td class="ellipsis-cell" :title="t.skill">{{ t.skill }}</td>
                        <td><span :class="['status-tag', t.status === 'completed' ? 'success' : t.status === 'failed' ? 'danger' : t.status === 'running' ? 'warning' : 'default']">{{ t.status }}</span></td>
                        <td class="t3">{{ formatDuration(t.createdAt, t.completedAt) }}</td>
                        <td class="t3">{{ t.exitCode ?? '—' }}</td>
                        <td class="t3" :title="t.createdAt">{{ formatTime(t.createdAt) }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </div>

            <!-- 独立任务组（无 issueId） -->
            <div v-if="standaloneTasks.length" class="tl-issue">
              <div class="tl-issue-head" style="cursor:default">
                <span class="tl-arrow">▪</span>
                <b class="tl-issue-id">独立任务</b>
                <span class="tl-issue-count t3">{{ standaloneTasks.length }} 任务</span>
                <span class="t3 tl-issue-time">无关联 Issue</span>
              </div>
              <div class="tl-issue-body" style="border-top:1px solid var(--border-color)">
                <table class="vod-table">
                  <thead><tr><th>ID</th><th>Agent</th><th>Skill</th><th>状态</th><th>耗时</th><th>Exit</th><th>创建时间</th></tr></thead>
                  <tbody>
                    <tr v-for="t in standaloneTasks" :key="t.id" class="tl-row-clickable" @click="openLogModal(t.id, t)">
                      <td class="t3">{{ (t.id || '').substring(0, 8) }}</td>
                      <td class="ellipsis-cell" :title="t.agentType">{{ t.agentType }}</td>
                      <td class="ellipsis-cell" :title="t.skill">{{ t.skill }}</td>
                      <td><span :class="['status-tag', t.status === 'completed' ? 'success' : t.status === 'failed' ? 'danger' : t.status === 'running' ? 'warning' : 'default']">{{ t.status }}</span></td>
                      <td class="t3">{{ formatDuration(t.createdAt, t.completedAt) }}</td>
                      <td class="t3">{{ t.exitCode ?? '—' }}</td>
                      <td class="t3" :title="t.createdAt">{{ formatTime(t.createdAt) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </template>
        </div>
        <div v-if="detail.tasks?.totalElements > 0" class="pagination-bar" style="border-top:none">
          <span class="page-info">第 <strong>{{ taskPage + 1 }}</strong> / {{ detail.tasks?.totalPages ?? 1 }} 页（共 {{ detail.tasks?.totalElements ?? 0 }} 条）</span>
          <select v-model="taskSize" class="page-size-select" @change="taskPage = 0; load()">
            <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}条/页</option>
          </select>
          <button class="page-btn" :disabled="taskPage <= 0" @click="taskPage--; load()">上一页</button>
          <button class="page-btn" :disabled="taskPage >= (detail.tasks?.totalPages ?? 1) - 1" @click="taskPage++; load()">下一页</button>
        </div>
        </div>
      </div>
    </div>

    <p v-else class="t2" style="padding:40px;text-align:center">加载中…</p>

    <!-- 日志弹窗 -->
    <div v-if="logModal.show" class="log-modal-overlay" @click.self="closeLogModal">
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
          <span class="t3">入队 {{ formatTime(logModal.info.createdAt).slice(11, 19) }}</span>
          <span v-if="logModal.info.startedAt" class="t3">→ 开始 {{ formatTime(logModal.info.startedAt).slice(11, 19) }}</span>
          <span v-if="logModal.info.completedAt" class="t3">→ 完成 {{ formatTime(logModal.info.completedAt).slice(11, 19) }}</span>
          <span :class="logModal.info.status === 'completed' ? 'ok' : logModal.info.status === 'failed' ? 'fail' : 'run'" style="margin-left:auto;font-weight:600">{{ logModal.info.status.toUpperCase() }}</span>
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
  </div>
</template>

<script setup>
import { computed, nextTick, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as echarts from 'echarts'
import http from '../api'

const route = useRoute()
const router = useRouter()
// 智能返回：历史可回则回退，否则回引擎列表（防直接访问 URL 时 back 跳出系统）
function goBack() {
  if (window.history.length > 1) router.back()
  else router.push('/runtime/engines')
}
const detail = ref(null)
const agents = ref([])
const taskPage = ref(0)
const taskSize = ref(10)
const sizeOptions = [10, 20, 50]
// 列头筛选/排序状态（对齐 VOD 分析结果报表）
const taskStatus = ref('')
const taskSkill = ref('')
const taskType = ref('')
const sortOrder = ref('desc')
const filterDropdown = ref('')
const filterCount = ref(0)
const hasAnyFilter = computed(() => !!(taskStatus.value || taskSkill.value || taskType.value))
// 加载遮罩 + 统计卡 + 图表
const taskLoading = ref(false)
const refreshing = ref(false)
const taskChartEl = ref(null)
let taskChart = null
// 任务状态展示映射（未知状态原样显示）
const STATUS_META = {
  completed: { label: '完成', color: 'var(--success)' },
  success: { label: '完成', color: 'var(--success)' },
  failed: { label: '失败', color: '#e5484d' },
  running: { label: '运行中', color: '#1890ff' },
  pending: { label: '等待', color: '#ea5a0c' }
}
const taskTotal = computed(() => {
  const st = detail.value?.tasks?.filterOptions?.status || {}
  return Object.values(st).reduce((s, n) => s + n, 0)
})
const taskStatList = computed(() => {
  const st = detail.value?.tasks?.filterOptions?.status || {}
  return Object.entries(st).map(([key, count]) => {
    const meta = STATUS_META[key] || { label: key, color: 'var(--text-secondary)' }
    return { key, label: meta.label, color: meta.color, count }
  })
})

const BADGE = {
  online: { color: 'var(--success)', bg: '#dcfce7', label: '在线' },
  recently_lost: { color: '#ea5a0c', bg: '#fef3c7', label: '刚失联' },
  offline: { color: 'var(--text-tertiary)', bg: '#eef0f3', label: '离线' },
  about_to_gc: { color: 'var(--danger)', bg: '#fee2e2', label: '待回收' },
  revoked: { color: 'var(--text-tertiary)', bg: '#eef0f3', label: '已吊销' }
}
const SCOPE_TEXT = { global: '全局', repo: '仓库', user: '个人' }
function scopeText(s) { return SCOPE_TEXT[s] || s || '—' }
function healthText(h) { return (BADGE[h] || { label: h }).label }
function badgeStyle(h) {
  const b = BADGE[h] || { color: 'var(--text-tertiary)', bg: '#eef0f3' }
  return { display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '11px', fontWeight: 500, color: b.color, background: b.bg, padding: '2px 10px', borderRadius: '10px', whiteSpace: 'nowrap' }
}
function formatTime(ts) {
  if (!ts) return '—'
  const time = new Date(ts.replace(' ', 'T')).getTime()
  if (isNaN(time)) return ts
  const diff = Date.now() - time
  if (diff < 60000) return '刚刚'
  if (diff < 3600000) return Math.floor(diff / 60000) + '分钟前'
  if (diff < 86400000) return Math.floor(diff / 3600000) + '小时前'
  return ts.slice(0, 16)
}
function installedText(v) { return v == null ? '—' : v === 1 ? '已安装' : '未安装' }
function healthyText(v) { return v == null ? '—' : v === 1 ? '正常' : '异常' }
function deviceStatusText(s) { return s === 'active' ? '有效' : s === 'revoked' ? '已吊销' : (s || '—') }

function formatDuration(start, end) {
  if (!start || !end) return '—'
  const ms = new Date(end.replace(' ', 'T')) - new Date(start.replace(' ', 'T'))
  if (isNaN(ms)) return '—'
  const s = Math.round(ms / 1000)
  if (s < 60) return s + 's'
  return Math.floor(s / 60) + 'm' + (s % 60) + 's'
}

// ===== 日志弹窗 =====
const logModal = ref({ show: false, loading: false, messages: [], info: {} })

async function openLogModal(taskId, task) {
  logModal.value = { show: true, loading: true, messages: [], info: { issueId: task?.issueId, repoKey: task?.repoKey, eventSeq: task?.eventSeq, createdAt: task?.createdAt } }
  try {
    const [msgsResp, infoResp] = await Promise.all([
      http.get(`/runtime/tasks/${taskId}/messages`),
      http.get(`/runtime/tasks/${taskId}/info`),
    ])
    const info = infoResp.data || {}
    if (!info.repoKey && task?.repoKey) info.repoKey = task.repoKey
    if (!info.issueId && task?.issueId) info.issueId = task.issueId
    if (info.eventSeq == null && task?.eventSeq) info.eventSeq = task.eventSeq
    logModal.value = { show: true, loading: false, messages: msgsResp.data || [], info }
  } catch (e) {
    logModal.value = { show: true, loading: false, messages: [], info: { error: e.message } }
  }
}
function closeLogModal() { logModal.value = { show: false, loading: false, messages: [], info: {} } }
function logTypeIcon(t) {
  const map = { step_start: '▶', step_finish: '✓', text: '💬', reasoning: '🤔', tool_use: '🔧', error: '❌', done: '✅' }
  return map[t] || '•'
}

// ===== 按 Issue 分组 =====
const issueGroups = computed(() => {
  const tasks = detail.value?.tasks?.content || []
  const map = {}
  for (const t of tasks) {
    if (!t.issueId) continue
    if (!map[t.issueId]) {
      map[t.issueId] = { issueId: t.issueId, tasks: [], hasFailed: false, lastTime: '' }
    }
    map[t.issueId].tasks.push(t)
    if (t.status === 'failed') map[t.issueId].hasFailed = true
    if (t.createdAt && t.createdAt > map[t.issueId].lastTime) map[t.issueId].lastTime = t.createdAt
  }
  return Object.values(map)
})

const standaloneTasks = computed(() => {
  const tasks = detail.value?.tasks?.content || []
  return tasks.filter(t => !t.issueId)
})

// ===== Issue 展开/折叠 + 三 Tab =====
const expandedIssues = ref(new Set())
const issueViewModes = ref({})
const renderedIssues = ref(new Set())
const renderingIssues = ref(new Set())
const issueSvgs = ref({})
const issueEventsData = ref({})
const issueEventsLoadingSet = ref(new Set())

function issueSvg(issueId) {
  return issueSvgs.value[issueId] || ''
}

function toggleIssue(issueId) {
  const s = new Set(expandedIssues.value)
  if (s.has(issueId)) {
    s.delete(issueId)
  } else {
    s.add(issueId)
    if (!issueViewModes.value[issueId]) {
      issueViewModes.value = { ...issueViewModes.value, [issueId]: 'diagram' }
    }
  }
  expandedIssues.value = s
  if (s.has(issueId) && issueViewModes.value[issueId] !== 'events') {
    setTimeout(() => renderMermaidForIssue(issueId), 50)
    if (!issueEventsData.value[issueId]) loadIssueEvents(issueId)
  }
}

function issueViewMode(issueId) {
  return issueViewModes.value[issueId] || 'diagram'
}

function issueRendered(issueId) {
  return renderedIssues.value.has(issueId)
}

function setIssueView(issueId, mode) {
  issueViewModes.value = { ...issueViewModes.value, [issueId]: mode }
  if (mode === 'diagram' && !renderedIssues.value.has(issueId)) {
    setTimeout(() => renderMermaidForIssue(issueId), 50)
  }
  if (!issueEventsData.value[issueId]) loadIssueEvents(issueId)
}

function issueEvents(issueId) {
  return issueEventsData.value[issueId] || []
}

function issueEventsLoading(issueId) {
  return issueEventsLoadingSet.value.has(issueId)
}

async function loadIssueEvents(issueId) {
  if (issueEventsData.value[issueId] || issueEventsLoadingSet.value.has(issueId)) return
  issueEventsLoadingSet.value = new Set([...issueEventsLoadingSet.value, issueId])
  try {
    const { data } = await http.get(`/runtime/issues/${issueId}/events`)
    issueEventsData.value = { ...issueEventsData.value, [issueId]: data.events || [] }
  } catch (e) {
    issueEventsData.value = { ...issueEventsData.value, [issueId]: [] }
  } finally {
    const s = new Set(issueEventsLoadingSet.value)
    s.delete(issueId)
    issueEventsLoadingSet.value = s
  }
}

// ===== Mermaid 时序图渲染 =====
let mermaidEngine = null

async function getMermaid() {
  if (!mermaidEngine) {
    const mod = await import('mermaid')
    mermaidEngine = mod.default
    mermaidEngine.initialize({
      startOnLoad: false,
      securityLevel: 'loose',
      theme: 'base',
      fontFamily: 'inherit',
      sequence: { mirrorActors: false, actorMargin: 60, messageMargin: 40, noteMargin: 10, boxMargin: 8 }
    })
  }
  return mermaidEngine
}

function mermaidId(agent) {
  return (agent || 'agent').replace(/[^a-zA-Z0-9_-]/g, '_')
}

function actionText(t) {
  const map = { issue_opened: 'Issue创建', issue_comment: '评论', issue_updated: '更新', a2a_event: 'A2A交办', pull_request_opened: 'PR创建', pull_request_merged: 'PR合并', push: '推送', vod_signal: 'VOD信号', issue_closed: 'Issue关闭' }
  return map[t] || t || '事件'
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
  let prev = null
  for (const ev of events) {
    const a = mermaidId(ev.agentType || '—')
    const t = ev.createdAt ? ev.createdAt.slice(11, 19) : ''
    const label = (t ? t + ' ' : '') + actionText(ev.eventType)
    if (prev && a !== prev) {
      lines.push(`    ${prev}->>${a}: ${label}`)
      statuses.push(ev.status || '')
    } else {
      lines.push(`    Note over ${a}: ${label}`)
      statuses.push(ev.status || '')
    }
    prev = a
  }
  return { text: lines.join('\n'), statuses }
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

async function renderMermaidForIssue(issueId) {
  if (renderingIssues.value.has(issueId)) return
  if (renderedIssues.value.has(issueId)) return
  renderingIssues.value = new Set([...renderingIssues.value, issueId])
  try {
    // 确保事件数据已加载
    if (!issueEventsData.value[issueId]) await loadIssueEvents(issueId)
    const events = issueEventsData.value[issueId] || []
    if (!events.length) {
      issueSvgs.value = { ...issueSvgs.value, [issueId]: '<div style="padding:12px;color:var(--text-tertiary);font-size:12px">暂无事件数据</div>' }
      renderedIssues.value = new Set([...renderedIssues.value, issueId])
      return
    }
    const mmd = await getMermaid()
    const { text, statuses } = buildSequence(events)
    const uid = 'mmd-' + Math.random().toString(36).slice(2, 10)
    try { await mmd.parse(text) } catch (e) { /* parse 失败也尝试 render */ }
    const result = await mmd.render(uid, text)
    const svg = result.svg || result
    issueSvgs.value = { ...issueSvgs.value, [issueId]: applySequenceColors(svg, statuses) }
    renderedIssues.value = new Set([...renderedIssues.value, issueId])
  } catch (err) {
    const events = issueEventsData.value[issueId] || []
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
  const map = { issue_opened: 'Issue创建', issue_comment: '评论', issue_updated: '更新', a2a_event: 'A2A交办', pull_request_opened: 'PR创建', pull_request_merged: 'PR合并', push: '推送', vod_signal: 'VOD信号' }
  return map[t] || t || '—'
}

function eventTypeClass(t) {
  if (t === 'a2a_event') return 'info'
  if (t === 'pull_request_merged') return 'success'
  if (t === 'issue_opened') return 'info'
  return 'default'
}

function formatDurationNum(d) {
  if (!d) return '—'
  const s = Number(d)
  if (isNaN(s) || s <= 0) return '—'
  if (s < 60) return s + 's'
  if (s < 3600) return Math.floor(s / 60) + 'm' + (s % 60) + 's'
  return Math.floor(s / 3600) + 'h' + Math.floor((s % 3600) / 60) + 'm'
}

const STATUS_COLOR = { queued: '#fa8c16', running: '#006eff', completed: '#52c41a', failed: '#eb2f96' }
const accents = ['kpi-blue', 'kpi-purple', 'kpi-pink', 'kpi-orange', 'kpi-green']
const iconPalette = [
  { background: '#e8f0fe', color: '#006eff' },
  { background: '#f0e6ff', color: '#7222ed' },
  { background: '#fff0f6', color: '#eb2f96' },
  { background: '#fff7e6', color: '#fa8c16' },
  { background: '#f6ffed', color: '#52c41a' }
]
function iconStyle(i) { return iconPalette[i % iconPalette.length] }
function hasBreakdown(a) { return a.statusBreakdown && Object.keys(a.statusBreakdown).length }
function fmtLoad(a) {
  if (a.loadRate == null) return '—'
  const v = Number(a.loadRate)
  return Number.isInteger(v) ? v : v.toFixed(1)
}
function loadColor(a) {
  if (a.loadRate == null) return 'var(--text-primary)'
  return a.loadLevel === 'over' ? '#eb2f96' : '#52c41a'
}

async function refresh() {
  refreshing.value = true
  const start = Date.now()
  try { await load() } finally {
    // 保证加载动画至少显示 300ms（本地响应过快时避免一闪而过）
    const wait = Math.max(0, 300 - (Date.now() - start))
    setTimeout(() => { refreshing.value = false }, wait)
  }
}
async function load() {
  taskLoading.value = true
  const params = { taskPage: taskPage.value, taskSize: taskSize.value, sortOrder: sortOrder.value }
  if (taskStatus.value) params.taskStatus = taskStatus.value
  if (taskSkill.value) params.taskSkill = taskSkill.value
  if (taskType.value) params.taskType = taskType.value
  try {
    const { data } = await http.get(`/engines/${route.params.id}`, { params })
    detail.value = data
    agents.value = data.agents || []
    // 「已筛选 N 条」：有列头筛选时显示过滤后总数，无筛选不显示（对齐 VOD）
    filterCount.value = (taskStatus.value || taskSkill.value || taskType.value) ? (data.tasks?.totalElements ?? 0) : 0
    nextTick(renderTaskChart)
  } finally {
    taskLoading.value = false
  }
}
// 任务状态分布饼图（对齐 VOD chart-row）
function renderTaskChart() {
  const el = taskChartEl.value
  if (!el) return
  const st = detail.value?.tasks?.filterOptions?.status || {}
  const data = Object.entries(st).map(([key, count]) => {
    const meta = STATUS_META[key] || { label: key }
    return { name: meta.label, value: count }
  })
  if (!data.length) return
  if (!taskChart) taskChart = echarts.init(el)
  taskChart.setOption({
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { fontSize: 12 } },
    series: [{
      type: 'pie',
      radius: ['40%', '68%'],
      center: ['50%', '44%'],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
      label: { show: true, formatter: '{b}: {c}' },
      data
    }],
    color: ['#52c41a', '#e5484d', '#1890ff', '#ea5a0c', '#8896a7', '#7222ed']
  })
}
// 列头筛选（对齐 VOD）：展开下拉/切换筛选/清空
function toggleFilter(col) {
  filterDropdown.value = filterDropdown.value === col ? '' : col
}
function setFilter(col, v) {
  if (col === 'status') taskStatus.value = v
  else if (col === 'skill') taskSkill.value = v
  else if (col === 'agentType') taskType.value = v
  filterDropdown.value = ''
  taskPage.value = 0
  load()
}
function clearFilters() {
  taskStatus.value = ''
  taskSkill.value = ''
  taskType.value = ''
  taskPage.value = 0
  load()
}
// 排序切换：desc → asc → desc
function toggleSort() {
  sortOrder.value = sortOrder.value === 'desc' ? 'asc' : 'desc'
  taskPage.value = 0
  load()
}
function filterOptions(col) {
  return detail.value?.tasks?.filterOptions?.[col] || {}
}
load()
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
  border-radius: 10px;
  padding: 10px 14px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  transition: all 0.2s ease;
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
  background: #f5f5f5;
  border: 1px solid var(--border-color);
  cursor: pointer;
  user-select: none;
  transition: all 0.2s ease;
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
  border-radius: 10px;
  border-left: 3px solid #006eff;
  padding: 12px 16px;
  text-align: center;
  transition: all 0.2s ease;
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
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
}
.col-filter-opt:hover { background: #f5f7fa; }
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
  background: #fff; border: 1px solid var(--border-color); border-radius: 10px;
  box-shadow: var(--card-shadow); padding: 14px 16px; border-left: 3px solid #006eff;
  display: flex; flex-direction: column; gap: 10px; cursor: pointer;
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
.agent-foot { display: flex; align-items: center; justify-content: space-between; border-top: 1px solid #f0f0f0; padding-top: 8px; }
.cat-tag { font-size: 11px; padding: 1px 8px; border-radius: 10px; }
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

/* ===== 日志弹窗 ===== */
.tl-row-clickable { cursor: pointer; }
.tl-row-clickable:hover td { background: #f0f5ff !important; }
.tl-row-clickable:hover { background: rgba(0, 107, 255, 0.04); }
.log-tl-dot.ok { background: #52c41a; }
.log-tl-dot.fail { background: #eb2f96; }
.log-tl-dot.run { background: #006eff; }
span.ok { color: #52c41a; }
span.fail { color: #eb2f96; }
span.run { color: #006eff; }

/* ===== 筛选/排序控制栏 ===== */
.task-filter-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.filter-chip-group { display: flex; gap: 8px; flex-wrap: wrap; }
.filter-chip { position: relative; display: inline-flex; align-items: center; gap: 4px; padding: 5px 12px; font-size: 12px; border: 1px solid var(--border-color); border-radius: 16px; background: #fff; color: var(--text-secondary); cursor: pointer; user-select: none; white-space: nowrap; }
.filter-chip:hover { border-color: var(--primary); color: var(--primary); }
.filter-chip.active { border-color: var(--primary); background: var(--primary-bg, #e8f0fe); color: var(--primary); }

/* ===== Issue 分组列表 ===== */
.tl-issue { border: 1px solid var(--border-color); border-radius: 8px; overflow: hidden; margin-bottom: 8px; }
.tl-issue-head { display: flex; align-items: center; gap: 10px; padding: 10px 14px; cursor: pointer; background: #fafbfc; }
.tl-issue-head:hover { background: #f5f9ff; }
.tl-arrow { font-size: 12px; color: var(--text-tertiary); width: 14px; }
.tl-issue-id { font-size: 13px; min-width: 40px; }
.tl-issue-count { font-size: 11px; white-space: nowrap; }
.tl-issue-status { font-size: 10px; font-weight: 600; white-space: nowrap; }
.tl-issue-status.fail { color: var(--danger); }
.tl-issue-status.ok { color: var(--success); }
.tl-issue-time { font-size: 11px; white-space: nowrap; margin-left: auto; }
.tl-issue-body { border-top: 1px solid var(--border-color); padding: 10px 14px; }
.tl-view-tabs { display: flex; gap: 0; margin-bottom: 10px; }
.tl-view-btn { padding: 5px 12px; font-size: 12px; border: 1px solid var(--border-color); background: #fff; color: var(--text-secondary); cursor: pointer; }
.tl-view-btn:first-child { border-radius: 6px 0 0 6px; }
.tl-view-btn:not(:first-child) { border-left: none; }
.tl-view-btn:last-child { border-radius: 0 6px 6px 0; }
.tl-view-btn.active { background: var(--primary); color: #fff; border-color: var(--primary); }
.tl-diagram-container { overflow-x: auto; padding: 8px 0; min-height: 60px; }
.tl-diagram-svg svg { max-width: 100%; }
.tl-diagram-loading { padding: 20px; text-align: center; color: var(--text-tertiary); font-size: 12px; }
.tl-row-fail td { background: #fff1f0 !important; }
.tl-row-ok td { background: #f6ffed !important; }

/* ===== 日志弹窗（对齐 HealthCenter） ===== */
.log-modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.5); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.log-modal-dialog { width: 860px; max-width: 94vw; max-height: 85vh; background: #fff; border-radius: 10px; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 8px 32px rgba(0,0,0,0.2); }
.log-modal-head { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; border-bottom: 1px solid var(--border-color); }
.log-modal-title { font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.log-timeline { display: flex; align-items: center; gap: 6px; padding: 8px 18px; border-bottom: 1px solid var(--border-soft, #f0f0f0); font-size: 11px; }
.log-tl-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
.log-modal-body { flex: 1; overflow-y: auto; padding: 14px 18px; }
.log-empty { text-align: center; padding: 32px; color: var(--text-tertiary); }
.log-error-info { text-align: left; background: #fff1f0; border: 1px solid #ffa39e; border-radius: 8px; padding: 12px; }
.log-error-row { font-size: 12px; margin-bottom: 4px; font-family: monospace; word-break: break-all; }
.log-terminal { background: #0c1222; border-radius: 8px; padding: 12px; max-height: 60vh; overflow-y: auto; }
.log-line { display: flex; align-items: flex-start; gap: 8px; padding: 3px 0; font-size: 12px; line-height: 1.6; border-bottom: 1px solid rgba(255,255,255,0.04); font-family: Menlo, Monaco, Consolas, monospace; }
.log-time { color: #5b667b; font-size: 10px; min-width: 55px; flex-shrink: 0; }
.log-icon { min-width: 16px; flex-shrink: 0; }
.log-content { color: #e0e6f0; word-break: break-all; white-space: pre-wrap; }
.log-type-step_start .log-content { color: #5b9eff; }
.log-type-step_finish .log-content { color: #1ca44c; }
.log-type-text .log-content { color: #e0e6f0; }
.log-type-reasoning .log-content { color: #b388ff; }
.log-type-tool_use .log-content { color: #ea5a0c; }
.log-type-error .log-content { color: #dc2828; }
.log-type-done .log-content { color: #1ca44c; }

</style>
