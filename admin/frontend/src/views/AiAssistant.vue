<template>
  <div class="aiq">
    <!-- 左栏：查询输入 + 分析报表（会话列表） -->
    <div class="aiq-left">
      <div class="aiq-q">
        <h3>查询输入</h3>
        <div class="aiq-actions">
          <select v-model="queryTemplate" @change="applyTemplate" class="aiq-tplselect">
            <option value="">-- 选择模板 --</option>
            <optgroup v-for="(tpls, g) in builtinGroups" :key="g" :label="g">
              <option v-for="(t, k) in tpls" :key="k" :value="k">{{ t.label }}</option>
            </optgroup>
            <optgroup label="自定义模板 ✨" v-if="customTemplates.length">
              <option v-for="t in customTemplates" :key="t.id" :value="'custom-' + t.id">{{ t.name }}</option>
            </optgroup>
          </select>
          <button class="btn btn-primary btn-sm" :disabled="!queryText.trim() || isQuerying" @click="send">{{ isQuerying ? '查询中...' : '发送' }}</button>
        </div>
        <textarea
          v-model="queryText"
          class="aiq-ta"
          rows="4"
          placeholder="请输入自然语言查询，支持多轮对话&#10;例如：统计各设备在线状态并用图表展示"
          @keydown.ctrl.enter="send"
        ></textarea>
        <div class="aiq-counter">{{ queryText.length }}/600</div>
        <div class="aiq-tplbar">
          <span class="aiq-tplcount">自定义模板 {{ customTemplates.length }}/{{ templateMax }}</span>
          <button class="btn btn-sm" :disabled="!queryText.trim()" @click="openSaveTpl">💾 保存为模板</button>
          <div class="aiq-tplwrap">
            <button class="btn btn-sm" @click="showTplDrop = !showTplDrop">📋 自定义模板 <span class="aiq-arrow">▼</span></button>
            <div v-if="showTplDrop" class="aiq-tpldrop">
              <div class="aiq-tpldrop-h">选择模板快速填充查询输入框</div>
              <div v-if="!customTemplates.length" class="aiq-tpldrop-empty">暂无自定义模板</div>
              <div v-for="t in customTemplates" :key="t.id" class="aiq-tplitem" @click="fillCustom(t)">
                <span>{{ t.name }}</span>
                <a class="aiq-tpldel" @click.stop="delTemplate(t)" title="删除">✕</a>
              </div>
            </div>
          </div>
        </div>
        <p class="aiq-hint">💡 Ctrl+Enter 发送 | 支持 ```chart 代码块自动渲染图表 | 多轮对话保持上下文 | 异步模式：查询中可切换会话不中断</p>
      </div>

      <div class="aiq-sessions">
        <div class="aiq-sh">
          <h4>分析报表</h4>
          <button class="btn btn-primary btn-sm" @click="newSession">+ 新建</button>
        </div>
        <div class="aiq-slist">
          <div
            v-for="s in sessions" :key="s.id"
            class="aiq-sitem" :class="{ act: s.id === activeSessionId, running: isSessionRunning(s) }"
            @click="switchSession(s)">
            <span v-if="editingId === s.id">
              <input v-model="editName" class="aiq-sinput" maxlength="50" @click.stop @keydown.enter="confirmRename" @keydown.esc="cancelRename" @blur="confirmRename" />
            </span>
            <span v-else class="aiq-sname" @dblclick.stop="startRename(s)">{{ s.name }}</span>
            <span v-if="isSessionRunning(s)" class="aiq-srun" title="分析中">●</span>
            <span class="aiq-stime">{{ formatSessionTime(s.lastAt || s.updatedAt) }}</span>
            <a v-if="editingId !== s.id" class="aiq-sedit" @click.stop="startRename(s)" title="重命名">✎</a>
            <a class="aiq-sdel" @click.stop="delSession(s)" title="删除">✕</a>
          </div>
          <div v-if="!sessions.length" class="aiq-sempty">暂无会话</div>
        </div>
      </div>
    </div>

    <!-- 右栏：对话结果 -->
    <div class="aiq-right" :class="{ full: isFullscreen }">
      <div class="aiq-rt">
        <h3>{{ currentName || '对话记录' }}</h3>
        <div class="aiq-rta">
          <button v-if="activeMsgs.length" class="btn btn-sm" @click="copyAll">{{ copyOk ? '已复制' : '复制全部' }}</button>
          <button v-if="activeMsgs.length" class="btn btn-sm" @click="isFullscreen = !isFullscreen">{{ isFullscreen ? '还原' : '全屏' }}</button>
        </div>
      </div>
      <div class="aiq-result" ref="resultBox">
        <div v-if="!activeMsgs.length && !isQuerying" class="aiq-empty">
          <div class="aiq-empty-icon">🤖</div>
          <p>AI 查询结果将显示在这里</p>
          <p class="aiq-empty-hint">输入查询内容并点击「发送」开始对话</p>
        </div>
        <div v-for="(m, mi) in activeMsgs" :key="mi" class="aiq-msg" :class="m.role">
          <div class="aiq-av">{{ m.role === 'user' ? '👤' : '🤖' }}</div>
          <div class="aiq-mb">
            <div class="aiq-role">{{ m.role === 'user' ? '我' : 'AI' }}</div>
            <div v-for="(seg, si) in renderParts(m.raw)" :key="si" class="aiq-seg">
              <pre v-if="seg.type === 'text'" class="aiq-text">{{ seg.text }}</pre>
              <div v-else :data-chart="mi + '-' + si" class="aiq-chart"></div>
            </div>
            <div v-if="m.streaming" class="aiq-loading"><span></span><span></span><span></span></div>
          </div>
        </div>
      </div>
    </div>

    <!-- 保存模板弹窗 -->
    <div v-if="showSaveTpl" class="modal" @click.self="showSaveTpl = false">
      <div class="modal-box">
        <h4>💾 保存为自定义模板</h4>
        <p class="t2" style="font-size:12px;margin:4px 0 10px">将当前提示词存为模板，下次可直接选用</p>
        <input v-model="newTplName" placeholder="请输入模板名称（≤50字）" maxlength="50" />
        <div class="modal-actions">
          <button class="btn" @click="showSaveTpl = false">取消</button>
          <button class="btn btn-primary" :disabled="!newTplName.trim()" @click="saveTemplate">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
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
const chartInstances = {}
// 同步模式轮询管理：{ [sessionId]: { taskId, aiMsg, pollTimer } }
const sessionTaskMap = {}

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

// ===== 内置模板（Grape 业务提示词，保留）=====
const BUILTIN_TEMPLATES = {
  'device-health': { group: '设备巡检', label: '设备健康巡检', content: '请汇总当前所有设备的健康状态，列出离线、刚失联、待回收的设备，说明风险并给出处理建议' },
  'task-fail': { group: '任务分析', label: '任务失败原因分析', content: '请分析最近的任务执行情况，按失败原因分类统计，给出改进建议' },
  'engine-overview': { group: '数据概览', label: '引擎分布总览(多图)', content: '请统计各类型引擎的数量、版本和健康状态分布，输出图表' },
  'credential-audit': { group: '安全审计', label: '凭据安全审计', content: '请检查当前所有有效凭据的归属用户、范围和最近心跳，列出可疑设备' },
  'chart-pie': { group: '图表示例', label: '在线状态占比(饼图)', content: '请统计设备在线/离线状态占比，输出饼图' },
  'chart-bar': { group: '图表示例', label: '引擎数量分布(柱状图)', content: '请统计各引擎类型数量，输出柱状图' }
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
function openSaveTpl() { newTplName.value = ''; showSaveTpl.value = true }
async function saveTemplate() {
  try {
    await http.post('/ai/templates', { name: newTplName.value.trim(), content: queryText.value })
    showSaveTpl.value = false
    await loadTemplates()
  } catch (e) { alert(e.response?.data?.message || '保存失败') }
}
async function delTemplate(t) {
  if (!confirm(`确认删除模板「${t.name}」？`)) return
  try {
    await http.delete(`/ai/templates/${t.id}`)
    await loadTemplates()
  } catch (e) { alert(e.response?.data?.message || '删除失败') }
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
  const re = /```chart\s*\n?([\s\S]*?)```/g
  let last = 0
  let m
  while ((m = re.exec(raw)) !== null) {
    if (m.index > last) parts.push({ type: 'text', text: raw.slice(last, m.index) })
    try {
      parts.push({ type: 'chart', json: JSON.parse(m[1]) })
    } catch (e) {
      parts.push({ type: 'text', text: m[0] })
    }
    last = re.lastIndex
  }
  if (last < raw.length) parts.push({ type: 'text', text: raw.slice(last) })
  return parts.length ? parts : [{ type: 'text', text: raw }]
}
async function initCharts() {
  await nextTick()
  for (const el of document.querySelectorAll('.aiq-chart')) {
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
onMounted(async () => {
  loadTemplates()
  await loadSessions()
  if (asyncMode.value) reconnectActiveTask()
})
</script>

<style scoped>
.aiq { display: flex; gap: 12px; height: calc(100vh - 100px); align-items: flex-start; }
.aiq-left { width: 360px; flex-shrink: 0; display: flex; flex-direction: column; gap: 12px; }
.aiq-q { background: #fff; border: 1px solid var(--border-color); border-radius: var(--radius-lg); padding: 14px 16px; box-shadow: var(--card-shadow); }
.aiq-q h3 { font-size: 13px; margin-bottom: 10px; }
.aiq-actions { display: flex; gap: 8px; margin-bottom: 8px; }
.aiq-tplselect { flex: 1; padding: 4px 8px; font-size: 12px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-body); outline: none; cursor: pointer; }
.aiq-ta { width: 100%; padding: 8px 10px; font-size: 13px; border: 1px solid var(--border-color); border-radius: 8px; outline: none; resize: vertical; font-family: inherit; box-sizing: border-box; }
.aiq-ta:focus { border-color: var(--primary); }
.aiq-counter { text-align: right; font-size: 11px; color: var(--text-tertiary); margin: 4px 0 8px; }
.aiq-tplbar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; position: relative; }
.aiq-tplcount { font-size: 11px; color: var(--text-tertiary); margin-right: auto; }
.aiq-arrow { font-size: 9px; }
.aiq-tplwrap { position: relative; }
.aiq-tpldrop { position: absolute; left: 0; top: 32px; width: 240px; background: #fff; border: 1px solid var(--border-color); border-radius: 8px; box-shadow: var(--card-shadow); z-index: 20; max-height: 220px; overflow-y: auto; }
.aiq-tpldrop-h { font-size: 11px; color: var(--text-tertiary); padding: 8px 10px; border-bottom: 1px solid var(--border-color); }
.aiq-tpldrop-empty { font-size: 12px; color: var(--text-tertiary); padding: 12px; text-align: center; }
.aiq-tplitem { display: flex; justify-content: space-between; align-items: center; padding: 7px 10px; font-size: 12px; cursor: pointer; }
.aiq-tplitem:hover { background: var(--bg-body); }
.aiq-tpldel { color: var(--danger); font-weight: 700; }
.aiq-hint { font-size: 11px; color: var(--text-tertiary); margin-top: 10px; }
.aiq-sessions { background: #fff; border: 1px solid var(--border-color); border-radius: var(--radius-lg); padding: 14px 16px; box-shadow: var(--card-shadow); flex: 1; overflow: hidden; display: flex; flex-direction: column; }
.aiq-sh { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.aiq-sh h4 { font-size: 13px; }
.aiq-slist { overflow-y: auto; flex: 1; }
.aiq-sitem { display: flex; align-items: center; gap: 6px; padding: 8px 10px; border-radius: 8px; cursor: pointer; font-size: 12px; }
.aiq-sitem:hover { background: var(--bg-body); }
.aiq-sitem.act { background: var(--primary-bg); color: var(--primary); }
.aiq-sitem.running .aiq-sname { color: var(--primary); }
.aiq-sname { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.aiq-sinput { width: 100%; padding: 2px 6px; font-size: 12px; border: 1px solid var(--primary); border-radius: 4px; outline: none; }
.aiq-srun { color: var(--success); font-size: 9px; flex-shrink: 0; animation: aiqblink 1.2s infinite; }
.aiq-stime { font-size: 10px; color: var(--text-tertiary); flex-shrink: 0; }
.aiq-sedit { color: var(--text-tertiary); font-size: 12px; flex-shrink: 0; }
.aiq-sedit:hover { color: var(--primary); }
.aiq-sdel { color: var(--danger); font-weight: 700; flex-shrink: 0; }
.aiq-sempty { font-size: 12px; color: var(--text-tertiary); text-align: center; padding: 16px 0; }
.aiq-right { flex: 1; background: #fff; border: 1px solid var(--border-color); border-radius: var(--radius-lg); box-shadow: var(--card-shadow); display: flex; flex-direction: column; min-width: 0; height: 100%; }
.aiq-right.full { position: fixed; inset: 0; z-index: 200; border-radius: 0; }
.aiq-rt { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--border-color); flex-shrink: 0; }
.aiq-rt h3 { font-size: 13px; }
.aiq-rta { display: flex; gap: 6px; }
.aiq-result { flex: 1; overflow-y: auto; padding: 16px; }
.aiq-empty { text-align: center; margin-top: 18%; color: var(--text-tertiary); }
.aiq-empty-icon { font-size: 32px; margin-bottom: 8px; }
.aiq-empty-hint { font-size: 12px; margin-top: 4px; }
.aiq-msg { display: flex; gap: 10px; margin-bottom: 16px; }
.aiq-av { width: 30px; height: 30px; border-radius: 50%; background: var(--bg-body); border: 1px solid var(--border-color); display: flex; align-items: center; justify-content: center; font-size: 15px; flex-shrink: 0; }
.aiq-msg.user .aiq-av { background: var(--primary-bg); }
.aiq-mb { flex: 1; min-width: 0; }
.aiq-role { font-size: 11px; color: var(--text-tertiary); margin-bottom: 4px; }
.aiq-seg { margin-bottom: 8px; }
.aiq-text { font-family: inherit; font-size: 13px; line-height: 1.7; white-space: pre-wrap; word-break: break-word; color: var(--text-primary); margin: 0; }
.aiq-chart { width: 100%; height: 320px; }
.aiq-loading { display: inline-flex; gap: 4px; padding: 4px 0; }
.aiq-loading span { width: 6px; height: 6px; border-radius: 50%; background: var(--primary); animation: aiqblink 1.2s infinite; }
.aiq-loading span:nth-child(2) { animation-delay: .2s; }
.aiq-loading span:nth-child(3) { animation-delay: .4s; }
@keyframes aiqblink { 0%,80%,100% { opacity: .2 } 40% { opacity: 1 } }
</style>
