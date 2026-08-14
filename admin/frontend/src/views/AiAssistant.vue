<template>
  <div class="page-container">
    <div v-if="toastVisible" class="toast-message" :class="toastType">{{ toastMessage }}</div>

    <div class="ai-query-header">
      <h2>AI分析</h2>
      <p class="ai-query-desc">基于 Hermes Agent 能力，支持多轮对话，输入自然语言查询，AI 自动分析并以图表展示结果</p>
    </div>

    <div class="ai-query-layout">
      <!-- 左侧：查询输入 + 分析报表 -->
      <div class="ai-query-input-panel">
        <!-- 查询输入 -->
        <div class="query-toolbar">
          <h3>查询输入</h3>
          <div class="query-actions">
            <select v-model="queryTemplate" @change="applyTemplate" class="template-select">
              <option value="">-- 选择模板 --</option>
              <optgroup v-for="(tpls, g) in builtinGroups" :key="g" :label="g">
                <option v-for="(t, k) in tpls" :key="k" :value="k">{{ t.label }}</option>
              </optgroup>
              <optgroup label="自定义模板 ✨" v-if="customTemplates.length">
                <option v-for="t in customTemplates" :key="t.id" :value="'custom-' + t.id">{{ t.name }}</option>
              </optgroup>
            </select>
            <button class="btn btn-primary btn-sm" @click="send" :disabled="!queryText.trim() || isQuerying">
              {{ isQuerying ? '查询中...' : '发送' }}
            </button>
          </div>
        </div>
        <textarea
          v-model="queryText"
          class="query-textarea"
          placeholder="请输入自然语言查询，支持多轮对话&#10;例如：统计各设备在线状态并用图表展示"
          rows="4"
          @keydown.ctrl.enter="send"
        ></textarea>
        <div class="char-counter-row">
          <span class="char-counter" :class="{ 'near-limit': queryText.length >= 540 && queryText.length <= 600, 'at-limit': queryText.length > 600 }">{{ queryText.length }}/600</span>
        </div>
        <div class="template-bar">
          <span class="template-count-hint">自定义模板 {{ customTemplates.length }}/{{ templateMax }}</span>
          <button class="btn-save-template" :disabled="!queryText.trim()" @click="openSaveTpl">
            <span>💾</span><span>保存为模板</span>
          </button>
          <div class="custom-template-wrapper" ref="tplWrapRef">
            <button class="custom-template-btn" :class="{ open: showTplDrop }" @click="showTplDrop = !showTplDrop">
              <span>📋</span><span>自定义模板</span><span class="arrow">▼</span>
            </button>
            <div class="custom-template-dropdown" v-if="showTplDrop">
              <div class="custom-template-dropdown-header">选择模板快速填充查询输入框</div>
              <div class="custom-template-dropdown-body">
                <div v-if="customTemplates.length === 0" class="custom-template-empty">暂无自定义模板</div>
                <div v-for="t in customTemplates" :key="t.id" class="custom-template-item" @click="fillCustom(t)">
                  <span class="template-name">{{ t.name }}</span>
                  <button class="template-delete" @click.stop="delTemplate(t)" title="删除">✕</button>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div class="query-hint">
          <p>💡 Ctrl+Enter 发送 | 支持 ```chart 代码块自动渲染图表 | 多轮对话保持上下文 | 异步模式：查询中可切换会话不中断</p>
        </div>

        <!-- 分析报表 -->
        <div class="session-list-section">
          <div class="session-list-header">
            <h4>分析报表</h4>
            <button class="btn btn-sm btn-primary" @click="newSession" title="新建会话">+ 新建</button>
          </div>
          <div class="session-list">
            <div
              v-for="s in sessions" :key="s.id"
              class="session-item" :class="{ active: s.id === activeSessionId, editing: editingId === s.id, running: isSessionRunning(s) }"
              @click="switchSession(s)">
              <input
                v-if="editingId === s.id"
                v-model="editName"
                class="session-name-input"
                maxlength="50"
                @click.stop
                @keydown.enter="confirmRename"
                @keydown.esc="cancelRename"
                @blur="confirmRename"
              />
              <span v-else class="session-name" @dblclick.stop="startRename(s)">{{ s.name }}</span>
              <span v-if="isSessionRunning(s)" class="session-running-indicator" title="分析中">●</span>
              <span class="session-time">{{ formatSessionTime(s.lastAt || s.updatedAt) }}</span>
              <button v-if="editingId !== s.id" class="session-edit" @click.stop="startRename(s)" title="重命名">✎</button>
              <button class="session-del" @click.stop="delSession(s)" title="删除">✕</button>
            </div>
            <div v-if="sessions.length === 0" class="session-empty">暂无会话</div>
          </div>
        </div>
      </div>

      <!-- 右侧：对话结果 -->
      <div class="ai-query-result-panel" :class="{ 'fullscreen-mode': isFullscreen }">
        <div class="result-toolbar">
          <h3>{{ currentName || '对话记录' }}</h3>
          <div class="result-toolbar-actions">
            <button v-if="activeMsgs.length" class="btn btn-sm" :class="{ 'btn-success': copyOk }" @click="copyAll">{{ copyOk ? '已复制' : '复制全部' }}</button>
            <button v-if="activeMsgs.length" class="btn btn-sm" @click="isFullscreen = !isFullscreen">{{ isFullscreen ? '还原' : '全屏' }}</button>
          </div>
        </div>
        <div class="result-content" ref="resultBox">
          <div class="result-empty" v-if="activeMsgs.length === 0 && !isQuerying">
            <div class="empty-icon">🤖</div>
            <p>AI 查询结果将显示在这里</p>
            <p class="empty-hint">输入查询内容并点击「发送」开始对话</p>
          </div>
          <div v-for="(m, mi) in activeMsgs" :key="mi" class="dialog-message" :class="m.role">
            <div class="msg-avatar">{{ m.role === 'user' ? '👤' : '🤖' }}</div>
            <div class="msg-body">
              <div class="msg-role">{{ m.role === 'user' ? '我' : 'AI' }}</div>
              <div v-for="(seg, si) in renderParts(m.raw)" :key="si" class="result-segment">
                <pre v-if="seg.type === 'text'" class="result-text">{{ seg.text }}</pre>
                <div v-else :data-chart="mi + '-' + si" class="chart-container"></div>
              </div>
              <div v-if="m.streaming" class="loading-dots-container">
                <div class="loading-dots"><span></span><span></span><span></span></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 字数超限弹窗 -->
    <div v-if="showOverLimitModal" class="modal-overlay" @click.self="closeOverLimitModal">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">⚠️</span>
          <span class="modal-title">字数超限</span>
        </div>
        <div class="modal-body">
          自定义模板字数不能超过 {{ TEMPLATE_MAX_LENGTH }} 字，当前已输入 {{ overLimitCount }} 字。
        </div>
        <div class="modal-footer">
          <button class="btn btn-sm" @click="closeOverLimitModal">我知道了</button>
        </div>
      </div>
    </div>

    <!-- 保存模板弹窗 -->
    <div class="dialog-overlay" v-if="showSaveTpl" @click.self="showSaveTpl = false">
      <div class="save-template-dialog">
        <div class="dialog-header">
          <div>
            <h4>💾 保存为自定义模板</h4>
            <p class="dialog-subtitle">将当前提示词存为模板，下次可直接选用</p>
          </div>
          <button class="dialog-close" @click="showSaveTpl = false">✕</button>
        </div>
        <div class="dialog-body">
          <label class="dialog-label">模板名称</label>
          <input v-model="newTplName" class="dialog-input" placeholder="请输入模板名称" maxlength="50" />
          <div class="char-counter" style="text-align:right;margin-top:6px" :class="{ 'near-limit': newTplName.length >= 45 && newTplName.length <= 50, 'at-limit': newTplName.length > 50 }">{{ newTplName.length }}/50</div>
        </div>
        <div class="dialog-footer">
          <button class="btn" @click="showSaveTpl = false">取消</button>
          <button class="btn btn-primary" :disabled="!newTplName.trim()" @click="saveTemplate">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { getToken } from '../utils/auth'
import http from '../api'

// ===== 状态 =====
const queryText = ref('')
const queryTemplate = ref('')
const busy = ref(false)            // 同步轮询模式：查询中标志
const asyncMode = ref(true)        // 默认异步订阅（对齐 VOD；复选框 UI 隐藏）
const sessions = ref([])           // 会话对象：{ id, sessionId, name, lastAt, updatedAt, msgs, taskId, taskCompleted, local }
const activeSessionId = ref(null)
const customTemplates = ref([])
const templateMax = ref(50)
const showSaveTpl = ref(false)
const newTplName = ref('')
const showTplDrop = ref(false)
const isFullscreen = ref(false)
const copyOk = ref(false)
const editingId = ref(null)
const editName = ref('')
const resultBox = ref(null)
const tplWrapRef = ref(null)
const chartInstances = {}
// 同步模式轮询管理：{ [sessionId]: { taskId, aiMsg, pollTimer } }
const sessionTaskMap = {}
// toast 与字数超限（对齐 VOD）
const toastMessage = ref('')
const toastVisible = ref(false)
const toastType = ref('success')
let toastTimer = null
const TEMPLATE_MAX_LENGTH = 600
const TEMPLATE_NAME_MAX_LENGTH = 50
const showOverLimitModal = ref(false)
const overLimitCount = ref(0)

const activeSession = computed(() => sessions.value.find(s => s.id === activeSessionId.value))
const activeMsgs = computed(() => activeSession.value?.msgs || [])
const currentName = computed(() => activeSession.value?.name || '')
const isQuerying = computed(() => {
  const s = activeSession.value
  return s ? (!!s.taskId && !s.taskCompleted) : false
})
function isSessionRunning(s) {
  return s && !!s.taskId && !s.taskCompleted
}
function showToast(msg, type = 'success') {
  toastMessage.value = msg
  toastType.value = type
  toastVisible.value = true
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toastVisible.value = false }, 2500)
}
function closeOverLimitModal() { showOverLimitModal.value = false }

// ===== 内置模板（Grape 业务提示词，含 VOD 式 ```chart 硬性格式指令）=====
const BUILTIN_TEMPLATES = {
  'device-health': { group: '设备巡检', label: '设备健康巡检', content: '请汇总当前所有设备的健康状态，列出离线、刚失联、待回收的设备，说明风险并给出处理建议，统计结果用```chart代码块返回echarts配置JSON' },
  'task-fail': { group: '任务分析', label: '任务失败原因分析', content: '请分析最近的任务执行情况，按失败原因分类统计，给出改进建议，统计结果用```chart代码块返回echarts配置JSON' },
  'engine-overview': { group: '数据概览', label: '引擎分布总览(多图)', content: '请统计各类型引擎的数量、版本和健康状态分布，用```chart代码块返回echarts配置JSON（可多图）' },
  'credential-audit': { group: '安全审计', label: '凭据安全审计', content: '请检查当前所有有效凭据的归属用户、范围和最近心跳，列出可疑设备，统计结果用```chart代码块返回echarts配置JSON' },
  'chart-pie': { group: '图表示例', label: '在线状态占比(饼图)', content: '请统计设备在线/离线状态占比，用```chart代码块返回echarts饼图配置JSON' },
  'chart-bar': { group: '图表示例', label: '引擎数量分布(柱状图)', content: '请统计各引擎类型数量，用```chart代码块返回echarts柱状图配置JSON' }
}
const builtinGroups = computed(() => {
  const groups = {}
  for (const [k, t] of Object.entries(BUILTIN_TEMPLATES)) {
    ;(groups[t.group] ||= {})[k] = { label: t.label, content: t.content }
  }
  return groups
})

// ===== 任务状态持久化（sessionStorage，对齐 VOD）=====
function getStoredTaskId(sessionId) {
  try { return sessionStorage.getItem('aiquery_task_' + sessionId) || null } catch (e) { return null }
}
function setStoredTaskId(sessionId, taskId) {
  try {
    if (taskId) sessionStorage.setItem('aiquery_task_' + sessionId, taskId)
    else sessionStorage.removeItem('aiquery_task_' + sessionId)
  } catch (e) { /* ignore */ }
}
function clearStoredTaskId(sessionId) {
  try { sessionStorage.removeItem('aiquery_task_' + sessionId) } catch (e) { /* ignore */ }
}

// ===== 会话管理 =====
function generateSessionName(query) {
  const t = (query || '').trim()
  return t.length <= 20 ? t : t.substring(0, 20) + '...'
}
function formatSessionTime(ts) {
  if (!ts) return ''
  const time = typeof ts === 'number' ? ts : new Date(ts.replace(' ', 'T')).getTime()
  if (!time || isNaN(time)) return ''
  const diff = Date.now() - time
  if (diff < 60000) return '刚刚'
  if (diff < 3600000) return Math.floor(diff / 60000) + '分钟前'
  if (diff < 86400000) return Math.floor(diff / 3600000) + '小时前'
  const d = new Date(time)
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
function newSession() {
  disposeAllCharts()
  const s = {
    id: 'local_' + Date.now() + '_' + Math.random().toString(36).substr(2, 6),
    sessionId: null,
    name: '新对话',
    msgs: [],
    taskId: null,
    taskCompleted: true,
    updatedAt: Date.now(),
    lastAt: null,
    local: true
  }
  sessions.value.unshift(s)
  activeSessionId.value = s.id
  return s
}
async function loadSessions() {
  try {
    const { data } = await http.get('/ai/history/sessions')
    const serverSessions = (data.sessions || []).map(s => {
      const storedTask = getStoredTaskId(s.sessionId)
      return {
        id: s.sessionId,
        sessionId: s.sessionId,
        name: s.name,
        messageCount: s.messageCount,
        lastAt: s.lastAt,
        updatedAt: null,
        msgs: null,                       // 懒加载
        taskId: storedTask,               // sessionStorage 恢复未完成任务
        taskCompleted: !storedTask,
        local: false
      }
    })
    // 合并：已存在会话（本地新会话/内存态）保留运行时状态（msgs/taskId），仅更新服务端字段
    const merged = serverSessions.map(ss => {
      const existing = sessions.value.find(x => x.id === ss.id || (x.local && x.sessionId === ss.sessionId))
      if (existing) {
        return { ...existing, ...ss, msgs: existing.msgs, taskId: existing.taskId, taskCompleted: existing.taskCompleted, local: false }
      }
      return ss
    })
    // 保留本地未完成任务且后端尚未出现的会话（任务仍在跑）
    const pendingLocal = sessions.value.filter(s => s.local && isSessionRunning(s) && !merged.some(m => m.sessionId === s.sessionId))
    sessions.value = [...pendingLocal, ...merged]
    if (sessions.value.length === 0) newSession()
    if (!sessions.value.some(s => s.id === activeSessionId.value)) {
      activeSessionId.value = sessions.value[0]?.id || null
    }
  } catch (e) {
    sessions.value = []
    newSession()
  }
}
async function loadMsgs(s) {
  if (!s.sessionId) return
  try {
    const { data } = await http.get(`/ai/history/session/${s.sessionId}/messages`)
    s.msgs = []
    for (const m of data) {
      s.msgs.push({ role: 'user', raw: m.query })
      s.msgs.push({ role: 'assistant', raw: m.answer })
    }
  } catch (e) { s.msgs = [] }
}
async function switchSession(s) {
  if (!asyncMode.value && isQuerying.value) return   // 同步模式查询中禁切（VOD 规则）
  disposeAllCharts()                                  // 对齐 VOD：切会话前销毁图表实例
  activeSessionId.value = s.id
  if (s.msgs === null) { s.msgs = []; await loadMsgs(s) }
  await nextTick()
  initCharts()
  scrollToBottom()
  if (asyncMode.value) reconnectActiveTask()          // 异步模式：切回未完成任务自动重连
}
function startRename(s) {
  editingId.value = s.id
  editName.value = s.name
}
async function confirmRename() {
  if (editingId.value && editName.value.trim()) {
    const s = sessions.value.find(x => x.id === editingId.value)
    if (s && editName.value.trim() !== s.name) {
      s.name = editName.value.trim()
      if (s.sessionId) {
        try { await http.put(`/ai/history/session/${s.sessionId}/name`, { name: s.name }) } catch (e) { /* ignore */ }
      }
    }
  }
  editingId.value = null
}
function cancelRename() { editingId.value = null }
async function delSession(s) {
  if (!confirm(`确认删除会话「${s.name}」？`)) return
  disposeAllCharts()
  clearStoredTaskId(s.id)
  if (s.sessionId) {
    try { await http.delete(`/ai/history/session/${s.sessionId}`) } catch (e) { /* ignore */ }
  }
  sessions.value = sessions.value.filter(x => x.id !== s.id)
  if (activeSessionId.value === s.id) activeSessionId.value = sessions.value[0]?.id || null
  await nextTick()
}

// ===== 模板 =====
async function loadTemplates() {
  try {
    const { data } = await http.get('/ai/templates')
    customTemplates.value = data.templates || []
    templateMax.value = data.max || 50
  } catch (e) { /* ignore */ }
}
function applyTemplate() {
  const v = queryTemplate.value
  if (!v) return
  if (v.startsWith('custom-')) {
    const t = customTemplates.value.find(x => String(x.id) === v.slice(7))
    if (t) queryText.value = t.content
  } else if (BUILTIN_TEMPLATES[v]) {
    queryText.value = BUILTIN_TEMPLATES[v].content
  }
  queryTemplate.value = ''
}
function fillCustom(t) { queryText.value = t.content; showTplDrop.value = false }
function openSaveTpl() {
  if (queryText.value.length > TEMPLATE_MAX_LENGTH) {
    overLimitCount.value = queryText.value.length
    showOverLimitModal.value = true
    return
  }
  newTplName.value = ''
  showSaveTpl.value = true
}
async function saveTemplate() {
  try {
    await http.post('/ai/templates', { name: newTplName.value.trim(), content: queryText.value })
    showSaveTpl.value = false
    await loadTemplates()
    showToast('模板保存成功')
  } catch (e) {
    showToast(e.response?.data?.message || '保存失败', 'error')
  }
}
async function delTemplate(t) {
  try {
    await http.delete(`/ai/templates/${t.id}`)
    await loadTemplates()
    showToast('模板已删除')
  } catch (e) {
    showToast(e.response?.data?.message || '删除失败', 'error')
  }
}

// ===== 发送（统一入口：异步订阅 / 同步轮询）=====
function send() {
  if (isQuerying.value || !queryText.value.trim()) return
  if (asyncMode.value) runQueryAsync()
  else runQuerySync()
}

/** 模式 A：异步订阅 —— submit 后 SSE 订阅，可切换会话/窗口不中断，支持断线重连 */
async function runQueryAsync() {
  const text = queryText.value.trim()
  let session = activeSession.value
  if (!session) session = newSession()
  if (session.msgs === null) session.msgs = []
  queryText.value = ''
  // 首次查询用问题命名会话
  if (session.name === '新对话') session.name = generateSessionName(text)
  session.msgs.push({ role: 'user', raw: text })
  const aiMsg = { role: 'assistant', raw: '', streaming: true }
  session.msgs.push(aiMsg)
  scrollToBottom()
  try {
    const { data } = await http.post('/ai/chat/submit', { query: text, sessionId: session.sessionId || null })
    session.taskId = data.taskId
    session.taskCompleted = false
    setStoredTaskId(session.id, data.taskId)
    await streamTask(session, aiMsg)
    await loadSessions()
  } catch (e) {
    aiMsg.raw = `[请求失败] ${e.response?.data?.message || e.message}`
    aiMsg.streaming = false
    session.taskCompleted = true
    clearStoredTaskId(session.id)
  }
}

/** SSE 订阅任务输出：token 追加 → session 事件 → 终态清理（后端重放缓冲，可重连） */
async function streamTask(session, aiMsg) {
  const resp = await fetch(`/api/admin/ai/chat/task/${session.taskId}/stream`, {
    headers: { Authorization: `Bearer ${getToken()}` }
  })
  if (!resp.ok || !resp.body) {
    aiMsg.raw += `\n[连接失败] ${resp.status === 502 ? '服务暂时不可用（可能正在部署/重启），请稍后重试' : 'HTTP ' + resp.status}`
    aiMsg.streaming = false
    session.taskCompleted = true
    clearStoredTaskId(session.id)
    return
  }
  const reader = resp.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    const lines = buf.split('\n')
    buf = lines.pop()
    for (const line of lines) {
      const t = line.trim()
      if (!t.startsWith('data:')) continue
      const payload = t.slice(5).trim()
      if (!payload) continue
      let ev
      try { ev = JSON.parse(payload) } catch (e) { continue }
      if (ev.type === 'token') {
        aiMsg.raw += ev.data
        if (activeSessionId.value === session.id) scrollToBottom()
      } else if (ev.type === 'session') {
        session.sessionId = ev.data
      } else if (ev.type === 'error') {
        aiMsg.raw += `\n[错误] ${ev.data}`
      }
    }
  }
  aiMsg.streaming = false
  session.taskCompleted = true
  clearStoredTaskId(session.id)
  await nextTick()
  initCharts()
  scrollToBottom()
}

/** 断线/切回重连：未完成任务重新订阅（后端重放已缓冲内容，不丢不重） */
async function reconnectActiveTask() {
  const s = activeSession.value
  if (!s || !s.taskId || s.taskCompleted) return
  if (s.msgs === null) s.msgs = []
  let aiMsg = [...s.msgs].reverse().find(m => m.role === 'assistant' && m.streaming)
  if (!aiMsg) {
    aiMsg = { role: 'assistant', raw: '', streaming: true }
    s.msgs.push(aiMsg)
  }
  await streamTask(s, aiMsg)
}

/** 模式 B：同步轮询 —— submit 后每 1.5s 轮询 status，实时显示部分结果 */
async function runQuerySync() {
  const text = queryText.value.trim()
  let session = activeSession.value
  if (!session) session = newSession()
  if (session.msgs === null) session.msgs = []
  queryText.value = ''
  if (session.name === '新对话') session.name = generateSessionName(text)
  session.msgs.push({ role: 'user', raw: text })
  const aiMsg = { role: 'assistant', raw: '' }
  session.msgs.push(aiMsg)
  busy.value = true
  try {
    const { data } = await http.post('/ai/chat/submit', { query: text, sessionId: session.sessionId || null })
    session.taskId = data.taskId
    session.taskCompleted = false
    await pollTaskStatus(session, aiMsg)
  } catch (e) {
    aiMsg.raw = `[请求失败] ${e.response?.data?.message || e.message}`
    session.taskCompleted = true
  } finally {
    busy.value = false
  }
}
async function pollTaskStatus(session, aiMsg) {
  const POLL_INTERVAL = 1500
  const poll = async (retry) => {
    try {
      const { data } = await http.get(`/ai/chat/task/${session.taskId}/status`)
      if (data.status === 'running') {
        if (data.result) {
          aiMsg.raw = data.result
          if (activeSessionId.value === session.id) scrollToBottom()
        }
        setTimeout(() => poll(0), POLL_INTERVAL)
      } else if (data.status === 'done') {
        aiMsg.raw = data.result || '（无返回内容）'
        session.taskCompleted = true
        clearStoredTaskId(session.id)
        if (data.sessionId) session.sessionId = data.sessionId
        await nextTick()
        initCharts()
        scrollToBottom()
        loadSessions()
      } else if (data.status === 'error') {
        aiMsg.raw = '查询失败: ' + (data.error || '未知错误')
        session.taskCompleted = true
        clearStoredTaskId(session.id)
      }
    } catch (e) {
      if (retry < 3) {
        setTimeout(() => poll(retry + 1), POLL_INTERVAL)
      } else {
        aiMsg.raw = '查询失败: 网络错误'
        session.taskCompleted = true
        clearStoredTaskId(session.id)
      }
    }
  }
  poll(0)
}

// ===== 渲染 =====
function renderParts(raw) {
  const parts = []
  // 对齐 VOD：识别 ```chart 与 ```echarts 两种代码块标识
  const re = /```\s*(?:chart|echarts?)\s*?\n?\s*([\s\S]*?)```/g
  let last = 0
  let m
  while ((m = re.exec(raw)) !== null) {
    if (m.index > last) {
      const text = raw.slice(last, m.index).trim()
      if (text) parts.push({ type: 'text', text })
    }
    try {
      const chartJson = tryParseChartJson(m[1].trim())
      parts.push({ type: 'chart', json: chartJson })
    } catch (e) {
      parts.push({ type: 'text', text: '[图表配置解析失败]\n' + m[1] })
    }
    last = re.lastIndex
  }
  if (last < raw.length) {
    const remaining = raw.slice(last).trim()
    if (remaining) parts.push({ type: 'text', text: remaining })
  }
  return parts.length ? parts : [{ type: 'text', text: raw }]
}

// ===== 图表 JSON 容错解析（移植 VOD tryParseChartJson 7 阶段修复）=====
function tryParseChartJson(raw) {
  const funcs = []
  let funcProcessed = raw
  let funcId = 0
  let result = ''
  for (let i = 0; i < raw.length; i++) {
    if (raw.substring(i, i + 8) === 'function') {
      const precededByQuote = (i > 0 && raw[i - 1] === '"')
      let j = i + 8
      while (j < raw.length && raw[j] !== '{') j++
      if (j < raw.length) {
        let depth = 1
        let k = j + 1
        while (k < raw.length && depth > 0) {
          if (raw[k] === '{') depth++
          else if (raw[k] === '}') depth--
          k++
        }
        const funcBody = raw.substring(i, k)
        funcs.push(funcBody)
        if (precededByQuote) {
          result = result.slice(0, -1)
          result += `"__FUNC_${funcId}__"`
          if (k < raw.length && raw[k] === '"') k++
        } else {
          result += `"__FUNC_${funcId}__"`
        }
        funcId++
        i = k - 1
        continue
      }
    }
    result += raw[i]
  }
  funcProcessed = result

  // Stage 0: 直接解析
  try { return restoreFuncs(JSON.parse(funcProcessed), funcs) } catch (e) { /* fallthrough */ }
  // Stage 1: 修复换行符
  const fixed = fixNewlines(funcProcessed)
  try { return restoreFuncs(JSON.parse(fixed), funcs) } catch (e) { /* fallthrough */ }
  // Stage 2: 修复换行 + 插入缺失逗号
  let s = insertMissingCommas(fixed)
  try { return restoreFuncs(JSON.parse(s), funcs) } catch (e) { /* fallthrough */ }
  // Stage 3: 修复换行 + 逗号 + 自动补全
  s = autoCompleteJson(s)
  try { return restoreFuncs(JSON.parse(s), funcs) } catch (e) { /* fallthrough */ }
  // Stage 4: 修复换行 + 截断(字符串内遇{[) + 逗号 + 补全
  s = fixTruncation(fixed)
  s = insertMissingCommas(s)
  s = autoCompleteJson(s)
  try { return restoreFuncs(JSON.parse(s), funcs) } catch (e) { /* fallthrough */ }
  // Stage 5: 压缩 + 全部修复
  const compressed = funcProcessed.replace(/\n\s*/g, ' ')
  s = insertMissingCommas(compressed)
  s = autoCompleteJson(s)
  try { return restoreFuncs(JSON.parse(s), funcs) } catch (e) { /* fallthrough */ }
  // Stage 6: 移除多余闭合括号
  const trimmed = removeExtraBrackets(funcProcessed)
  try { return restoreFuncs(JSON.parse(trimmed), funcs) } catch (e) { /* fallthrough */ }
  let s6 = insertMissingCommas(trimmed)
  s6 = autoCompleteJson(s6)
  try { return restoreFuncs(JSON.parse(s6), funcs) } catch (e) { /* fallthrough */ }

  throw new Error('JSON parse failed after all repair attempts')
}
function restoreFuncs(obj, funcs) {
  if (typeof obj === 'string') {
    const mm = obj.match(/^__FUNC_(\d+)__$/)
    if (mm) {
      try { return new Function('return ' + funcs[parseInt(mm[1])])() } catch (e) { return obj }
    }
    return obj
  }
  if (Array.isArray(obj)) return obj.map(v => restoreFuncs(v, funcs))
  if (obj && typeof obj === 'object') {
    const r = {}
    for (const k in obj) r[k] = restoreFuncs(obj[k], funcs)
    return r
  }
  return obj
}
function fixNewlines(raw) {
  let inStr = false, esc = false, fixed = ''
  for (let i = 0; i < raw.length; i++) {
    const ch = raw[i]
    if (esc) {
      if (ch === '\n') { fixed += 'n' }
      else if (ch === '\r') { /* skip */ }
      else { fixed += ch }
      esc = false; continue
    }
    if (ch === '\\') { fixed += ch; esc = true; continue }
    if (ch === '"') { fixed += ch; inStr = !inStr; continue }
    if (ch === '\n' && inStr) { fixed += '\\n'; continue }
    if (ch === '\r' && inStr) { continue }
    fixed += ch
  }
  return fixed
}
function fixTruncation(s) {
  let inStr = false, esc = false, fixed = ''
  for (let i = 0; i < s.length; i++) {
    const ch = s[i]
    if (esc) { fixed += ch; esc = false; continue }
    if (ch === '\\') { fixed += ch; esc = true; continue }
    if (ch === '"') { fixed += ch; inStr = !inStr; continue }
    if (inStr && (ch === '{' || ch === '[')) {
      fixed += '"}'  // 闭合字符串+对象
      inStr = false
    }
    fixed += ch
  }
  return fixed
}
function insertMissingCommas(s) {
  let inStr = false, esc = false, out = ''
  for (let i = 0; i < s.length; i++) {
    const ch = s[i]
    out += ch
    if (esc) { esc = false; continue }
    if (ch === '\\') { esc = true; continue }
    if (ch === '"') {
      inStr = !inStr
      if (!inStr) {
        let j = i + 1
        while (j < s.length && /\s/.test(s[j])) j++
        if (j < s.length) {
          const next = s[j]
          let k = out.length - 1
          while (k > 0 && /\s/.test(out[k - 1])) k--
          const prev = out[k - 1]
          if (next === '"') {
            if (prev !== ',' && prev !== '{' && prev !== '[') out += ','
          } else if (/[a-zA-Z_]/.test(next)) {
            if (prev !== ',' && prev !== '{' && prev !== '[') out += ',"'
          }
        }
      }
    }
    if (!inStr && ch === '}') {
      let j = i + 1
      while (j < s.length && /\s/.test(s[j])) j++
      if (j < s.length && (s[j] === '{' || s[j] === '"')) out += ','
    }
  }
  return out
}
function autoCompleteJson(s) {
  let inStr = false, esc = false, braces = 0, brackets = 0
  for (let i = 0; i < s.length; i++) {
    const ch = s[i]
    if (esc) { esc = false; continue }
    if (ch === '\\') { esc = true; continue }
    if (ch === '"') { inStr = !inStr; continue }
    if (!inStr) {
      if (ch === '{') braces++
      else if (ch === '}') braces--
      else if (ch === '[') brackets++
      else if (ch === ']') brackets--
    }
  }
  let repaired = s
  if (inStr) repaired += '"'
  repaired = repaired.replace(/,\s*$/, '')
  for (let i = 0; i < brackets; i++) repaired += ']'
  for (let i = 0; i < braces; i++) repaired += '}'
  return repaired
}
function removeExtraBrackets(s) {
  let result = s
  for (let iter = 0; iter < 10; iter++) {
    try {
      JSON.parse(result)
      return result
    } catch (e) {
      if (!e.message.includes('after JSON')) break
      let inStr = false, esc = false, depth = 0, rootClose = -1
      for (let i = 0; i < result.length; i++) {
        const ch = result[i]
        if (esc) { esc = false; continue }
        if (ch === '\\') { esc = true; continue }
        if (ch === '"') { inStr = !inStr; continue }
        if (!inStr) {
          if (ch === '{' || ch === '[') depth++
          else if (ch === '}' || ch === ']') {
            depth--
            if (depth === 0) { rootClose = i; break }
          }
        }
      }
      if (rootClose >= 0) result = result.substring(0, rootClose) + result.substring(rootClose + 1)
      else break
    }
  }
  return result
}
async function initCharts() {
  await nextTick()
  for (const el of document.querySelectorAll('.chart-container')) {
    const key = el.getAttribute('data-chart')
    if (!key) continue
    const [mi, si] = key.split('-').map(Number)
    const seg = renderParts(activeMsgs.value[mi]?.raw || '')[si]
    if (!seg || seg.type !== 'chart') continue
    // 对齐 VOD：已有实例先销毁再重建（切换会话后 DOM 重建，旧实例绑定在已销毁节点上）
    if (chartInstances[key]) {
      chartInstances[key].dispose()
      delete chartInstances[key]
    }
    const inst = echarts.init(el)
    inst.setOption(seg.json)
    chartInstances[key] = inst
  }
}
/** 对齐 VOD：销毁并清空全部图表实例（切换/新建/删除会话时调用，防 key 缓存误命中） */
function disposeAllCharts() {
  Object.values(chartInstances).forEach(c => { try { c.dispose() } catch (e) { /* ignore */ } })
  Object.keys(chartInstances).forEach(k => delete chartInstances[k])
}
function scrollToBottom() {
  nextTick(() => { if (resultBox.value) resultBox.value.scrollTop = resultBox.value.scrollHeight })
}
async function copyAll() {
  const text = activeMsgs.value.map(m => (m.role === 'user' ? '我: ' : 'AI: ') + m.raw).join('\n\n')
  try {
    await navigator.clipboard.writeText(text)
    copyOk.value = true
    setTimeout(() => { copyOk.value = false }, 1500)
  } catch (e) { /* ignore */ }
}
watch(activeMsgs, () => initCharts(), { deep: true })

// ===== 挂载 =====
function onDocClick(e) {
  if (tplWrapRef.value && !tplWrapRef.value.contains(e.target)) showTplDrop.value = false
}
onMounted(async () => {
  loadTemplates()
  await loadSessions()
  if (asyncMode.value) reconnectActiveTask()
  document.addEventListener('click', onDocClick)
})
onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
  disposeAllCharts()
})
</script>

<style scoped>
/* ===== 对齐 VOD AiQuery.vue 样式体系 ===== */
.ai-query-header { margin-bottom: 20px; }
.ai-query-header h2 { font-size: 20px; font-weight: 600; }
.ai-query-desc { font-size: 13px; color: var(--text-secondary); margin-top: 4px; }
.ai-query-layout { display: flex; gap: 20px; height: calc(100vh - 200px); }
.ai-query-input-panel { width: 38%; flex-shrink: 0; background: #fff; border-radius: 12px; border: 1px solid var(--border-color); padding: 20px; display: flex; flex-direction: column; }
.ai-query-result-panel { flex: 1; background: #fff; border-radius: 12px; border: 1px solid var(--border-color); padding: 20px; display: flex; flex-direction: column; overflow: hidden; transition: all 0.3s ease; }
.ai-query-result-panel.fullscreen-mode { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; z-index: 9999; border-radius: 0; border: none; padding: 24px; transition: all 0.3s ease; }

.session-list-section { margin-top: 16px; border-top: 1px solid var(--border-color); padding-top: 12px; }
.session-list-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.session-list-header h4 { font-size: 13px; color: var(--text-secondary); }
.session-list { max-height: 180px; overflow-y: auto; }
.session-item { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 6px; cursor: pointer; font-size: 13px; transition: all 0.15s; }
.session-item:hover { background: #f5f7fa; }
.session-item.active { background: #e8f5ff; color: var(--primary); font-weight: 500; }
.session-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-time { font-size: 11px; color: var(--text-tertiary); flex-shrink: 0; }
.session-del { width: 18px; height: 18px; border: none; background: transparent; cursor: pointer; border-radius: 4px; color: var(--text-tertiary); font-size: 11px; flex-shrink: 0; }
.session-del:hover { background: #ffeee6; color: #f5222d; }
.session-edit { width: 18px; height: 18px; border: none; background: transparent; cursor: pointer; border-radius: 4px; color: var(--text-tertiary); font-size: 11px; flex-shrink: 0; opacity: 0; transition: opacity 0.15s; }
.session-item:hover .session-edit { opacity: 1; }
.session-edit:hover { background: #e8f5ff; color: var(--primary); }
.session-name-input { flex: 1; font-size: 13px; border: 1px solid var(--primary); border-radius: 4px; padding: 2px 6px; outline: none; background: #fff; }
.session-item.editing { background: #f0f7ff; }
.session-item.running .session-name { color: var(--primary); }
.session-running-indicator { color: #52c41a; font-size: 10px; animation: pulse 1.5s infinite; flex-shrink: 0; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
.session-empty { font-size: 12px; color: var(--text-tertiary); text-align: center; padding: 12px; }

.query-toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.query-toolbar h3 { font-size: 16px; font-weight: 600; }
.query-actions { display: flex; gap: 8px; align-items: center; }
.async-toggle { display: flex; align-items: center; gap: 4px; font-size: 12px; color: var(--text-secondary); cursor: pointer; user-select: none; white-space: nowrap; }
.async-toggle input { margin: 0; cursor: pointer; }
.async-toggle:hover { color: var(--primary); }
.template-select { padding: 6px 10px; border: 1px solid var(--border-color); border-radius: 6px; font-size: 13px; outline: none; }
.query-textarea { width: 100%; flex: 1; padding: 12px; border: 1px solid var(--border-color); border-radius: 8px; font-size: 14px; font-family: inherit; resize: none; line-height: 1.6; outline: none; box-sizing: border-box; }
.query-textarea:focus { border-color: var(--primary); }
.query-hint { margin-top: 8px; padding: 8px 12px; background: #f0f7ff; border-radius: 6px; font-size: 12px; color: var(--text-secondary); }
.query-hint p { margin: 0; }

.result-toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.result-toolbar h3 { font-size: 16px; font-weight: 600; }
.result-toolbar-actions { display: flex; gap: 8px; align-items: center; }
.result-content { flex: 1; overflow-y: auto; }
.result-empty { text-align: center; color: var(--text-tertiary); padding: 60px 20px; }
.empty-icon { font-size: 48px; margin-bottom: 12px; }
.empty-hint { font-size: 12px; margin-top: 4px; }

.dialog-message { display: flex; gap: 12px; margin-bottom: 20px; }
.dialog-message.user { flex-direction: row-reverse; }
.msg-avatar { width: 36px; height: 36px; border-radius: 50%; flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-size: 20px; background: #f5f7fa; overflow: hidden; }
.msg-body { flex: 1; min-width: 0; }
.dialog-message.user .msg-body { display: flex; flex-direction: column; align-items: flex-end; }
.msg-role { font-size: 12px; color: var(--text-tertiary); margin-bottom: 4px; }
.result-segment { margin-bottom: 8px; }
.dialog-message.user .result-segment { max-width: 90%; }
.result-text { font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; font-family: 'Courier New', monospace; color: var(--text-primary); padding: 12px; background: #f8f9fb; border-radius: 8px; margin: 0; }
.dialog-message.user .result-text { background: #e8f5ff; }
.chart-container { width: 100%; height: 400px; border-radius: 8px; border: 1px solid #f0f0f0; }
.loading-dots-container { display: inline-flex; align-items: center; gap: 8px; }
.loading-dots { display: inline-flex; gap: 6px; padding: 8px; }
.loading-dots span { width: 8px; height: 8px; border-radius: 50%; background: var(--primary); animation: query-bounce 1.2s infinite; }
.loading-dots span:nth-child(2) { animation-delay: 0.2s; }
.loading-dots span:nth-child(3) { animation-delay: 0.4s; }
@keyframes query-bounce { 0%,60%,100% { transform: translateY(0); } 30% { transform: translateY(-6px); } }
.btn-success { background: #52c41a !important; color: #fff !important; border-color: #52c41a !important; }

/* 自定义模板管理区 */
.char-counter-row { display: flex; justify-content: flex-end; align-items: center; margin-top: 4px; padding: 0 2px; }
.char-counter { font-size: 12px; color: var(--text-tertiary); transition: color 0.2s; }
.char-counter.near-limit { color: #fa8c16; font-weight: 600; }
.char-counter.at-limit { color: #f5222d; font-weight: 600; }
.template-bar { display: flex; justify-content: flex-end; align-items: center; gap: 8px; margin-top: 8px; }
.template-count-hint { font-size: 12px; color: var(--text-tertiary); margin-right: auto; }
.btn-save-template { display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px; border: 1px dashed var(--primary); border-radius: 6px; font-size: 13px; color: var(--primary); background: #f0f7ff; cursor: pointer; transition: all 0.15s; }
.btn-save-template:hover:not(:disabled) { background: #e8f5ff; border-style: solid; }
.btn-save-template:disabled { color: var(--text-tertiary); border-color: var(--border-color); background: #f5f5f5; cursor: not-allowed; }
.custom-template-wrapper { position: relative; }
.custom-template-btn { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border: 1px solid var(--border-color); border-radius: 6px; font-size: 13px; background: #fff; color: var(--text-primary); cursor: pointer; transition: all 0.15s; }
.custom-template-btn:hover { border-color: var(--primary); }
.custom-template-btn .arrow { margin-left: 4px; font-size: 10px; transition: transform 0.2s; }
.custom-template-btn.open .arrow { transform: rotate(180deg); }
.custom-template-dropdown { position: absolute; top: calc(100% + 4px); right: 0; width: 280px; background: #fff; border: 1px solid var(--border-color); border-radius: 8px; box-shadow: 0 4px 16px rgba(0,0,0,0.12); z-index: 100; overflow: hidden; }
.custom-template-dropdown-header { padding: 10px 14px; font-size: 12px; color: var(--text-tertiary); font-weight: 500; border-bottom: 1px solid #f0f0f0; }
.custom-template-dropdown-body { max-height: 280px; overflow-y: auto; }
.custom-template-empty { padding: 20px 14px; text-align: center; color: var(--text-tertiary); font-size: 13px; }
.custom-template-item { display: flex; align-items: center; padding: 9px 14px; cursor: pointer; transition: background 0.1s; font-size: 13px; gap: 8px; }
.custom-template-item:hover { background: #f0f7ff; }
.custom-template-item .template-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.custom-template-item .template-delete { width: 22px; height: 22px; border: none; background: transparent; cursor: pointer; border-radius: 4px; display: flex; align-items: center; justify-content: center; color: var(--text-tertiary); font-size: 14px; flex-shrink: 0; opacity: 0; transition: opacity 0.15s; }
.custom-template-item:hover .template-delete { opacity: 1; }
.custom-template-item .template-delete:hover { background: #ffeee6; color: #f5222d; }

/* 保存模板弹窗 */
.dialog-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.35); z-index: 10000; display: flex; align-items: center; justify-content: center; }
.save-template-dialog { background: #fff; border-radius: 12px; width: 420px; max-width: 90vw; box-shadow: 0 8px 32px rgba(0,0,0,0.2); }
.dialog-header { padding: 20px 20px 0; position: relative; }
.dialog-header h4 { font-size: 16px; font-weight: 600; margin: 0 0 4px; }
.dialog-subtitle { font-size: 12px; color: var(--text-secondary); margin: 0; }
.dialog-close { position: absolute; top: 16px; right: 16px; border: none; background: transparent; cursor: pointer; font-size: 18px; color: var(--text-tertiary); padding: 4px; border-radius: 4px; }
.dialog-close:hover { background: #f5f5f5; color: var(--text-primary); }
.dialog-body { padding: 16px 20px; }
.dialog-label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 6px; }
.dialog-input { width: 100%; padding: 8px 12px; border: 1px solid var(--border-color); border-radius: 6px; font-size: 14px; outline: none; box-sizing: border-box; }
.dialog-input:focus { border-color: var(--primary); }
.dialog-footer { padding: 12px 20px 20px; display: flex; justify-content: flex-end; gap: 8px; }

/* Toast 与弹窗样式已提升至全局 style.css（modal-overlay/modal-dialog/toast-message） */

/* 响应式（对齐 VOD） */
@media (max-width: 768px) {
  .ai-query-layout { flex-direction: column; height: auto; }
  .ai-query-input-panel { width: 100%; max-height: 40vh; }
  .chart-container { height: 280px; }
}
@media (max-width: 480px) {
  .chart-container { height: 220px; }
  .query-toolbar { flex-wrap: wrap; gap: 8px; }
  .result-toolbar { flex-wrap: wrap; gap: 8px; }
}
</style>
