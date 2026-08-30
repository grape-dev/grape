<template>
  <div class="page-container" style="padding:0; display:flex; flex-direction:column; height:100vh; overflow-y:auto">
    <div v-if="toastVisible" class="toast-message" :class="toastType">{{ toastMessage }}</div>

    <div class="ai-query-header">
      <h2>AI分析 <span class="desc-icon" @click.stop="toggleDesc" title="功能介绍">?</span></h2>
      <div v-if="descVisible" class="desc-bubble" @click.stop>基于 Hermes Agent 能力，支持多轮对话，输入自然语言查询，AI 自动分析并以图表展示结果</div>
    </div>

    <div class="ai-query-layout">
      <!-- 左侧：查询输入 + 分析报表 -->
      <div class="ai-query-input-panel">
        <!-- 查询输入 -->
        <div class="query-toolbar">
          <h3 style="position:relative">查询输入
            <button class="hint-btn" @click.stop="toggleHint" title="使用帮助">💡</button>
            <div v-if="hintVisible" class="hint-bubble" @click.stop>
              <p>· Ctrl+Enter 发送</p>
              <p>· 支持 ```chart 代码块自动渲染图表</p>
              <p>· 多轮对话保持上下文</p>
              <p>· 异步模式：查询中可切换会话不中断</p>
            </div>
          </h3>
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
            <button class="btn btn-primary btn-sm" @click="send" :disabled="!queryText.trim() || isQuerying || !canSend">
              {{ isQuerying ? '查询中...' : (!canSend ? '处理中...' : '发送') }}
            </button>
          </div>
        </div>
        <div class="query-input-wrap">
          <div class="query-textarea-box">
            <textarea
              v-model="queryText"
              ref="queryInputRef"
              class="query-textarea"
              :disabled="!canSend"
              :placeholder="canSend ? '请输入自然语言查询，支持多轮对话\n例如：统计各设备在线状态并用图表展示' : '当前会话正在处理中，请等待完成...'"
              rows="4"
              @keydown.ctrl.enter="send"
            ></textarea>
            <span class="char-counter" :class="{ 'near-limit': queryText.length >= 540 && queryText.length <= 600, 'at-limit': queryText.length > 600 }">{{ queryText.length }}/600</span>
          </div>
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
        <!-- 分析报表 -->
        <div class="session-list-section">
          <div class="session-list-header">
            <h4>分析报表</h4>
            <button class="btn btn-sm btn-primary" @click="newSession" title="新建会话">+ 新建</button>
          </div>
          <div class="session-list" @scroll.passive="onSessionScroll">
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
              <span v-else class="session-name" @dblclick.stop="startRename(s)">{{ formatSessionName(s.name) }}</span>
              <span v-if="isSessionRunning(s)" class="session-running-indicator" title="分析中">●</span>
              <span class="session-time">{{ formatSessionTime(s.lastAt || s.updatedAt) }}</span>
              <button v-if="editingId !== s.id && !s.id.startsWith('new_')" class="session-edit" @click.stop="startRename(s)" title="重命名">✎</button>
              <button v-if="!s.id.startsWith('new_')" class="session-del" @click.stop="askDelSession(s)" title="删除">✕</button>
            </div>
            <div v-if="sessions.length === 0" class="session-empty">暂无会话</div>
          </div>
        </div>
      </div>

      <!-- 右侧：对话结果 -->
      <div class="ai-query-result-panel" :class="{ 'fullscreen-mode': isFullscreen }">
        <div class="result-toolbar">
          <h3>{{ formatSessionName(currentName) || '对话记录' }}</h3>
          <div class="result-toolbar-actions">
            <button v-if="activeMsgs.length" class="btn btn-sm" :class="{ 'btn-success': copyOk }" @click="copyAll">{{ copyOk ? '已复制' : '复制全部' }}</button>
            <button v-if="activeMsgs.length" class="btn btn-sm" @click="toggleFullscreen">{{ isFullscreen ? '还原' : '全屏' }}</button>
          </div>
        </div>
        <div class="result-content" ref="resultBox">
          <div v-if="activeMsgs.length && activeSession && activeSession.hasMore" class="load-more-wrap">
            <button class="btn btn-sm" @click="loadOlder">{{ activeSession.loadingOlder ? '加载中...' : '加载更早历史' }}</button>
          </div>
          <div class="result-empty" v-if="activeMsgs.length === 0 && !isQuerying">
            <div class="empty-icon">🤖</div>
            <p>AI 查询结果将显示在这里</p>
            <p class="empty-hint">输入查询内容并点击「发送」开始对话</p>
          </div>
          <div v-for="(m, mi) in activeMsgs" :key="mi" class="dialog-message" :class="m.role">
            <div class="msg-avatar">{{ m.role === 'user' ? '👤' : '🤖' }}</div>
            <div class="msg-body">
              <div class="msg-role">{{ m.role === 'user' ? '我' : 'AI' }}</div>
              <div v-for="(seg, si) in segFor(m)" :key="si" class="result-segment">
                <pre v-if="seg.type === 'text'" class="result-text">{{ seg.text }}</pre>
                <div v-else :data-chart="mi + '-' + si" class="chart-container"></div>
              </div>
              <div v-if="m.streaming" class="loading-dots-container">
                <div class="loading-dots"><span></span><span></span><span></span></div>
              </div>
              <div v-if="m.role === 'assistant' && m.raw === '（查询失败，可重试）'" class="retry-wrap">
                <button class="btn btn-sm" @click="retryQuery(m)">重试</button>
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
          <button class="btn btn-primary btn-sm" @click="goRefineQuery">去精简</button>
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
          <input v-model="newTplName" ref="newTplNameInput" class="dialog-input" placeholder="请输入模板名称" maxlength="50" />
          <div class="char-counter" style="text-align:right;margin-top:6px" :class="{ 'near-limit': newTplName.length >= 45 && newTplName.length <= 50, 'at-limit': newTplName.length > 50 }">{{ newTplName.length }}/50</div>
        </div>
        <div class="dialog-footer">
          <button class="btn" @click="showSaveTpl = false">取消</button>
          <button class="btn btn-primary" :disabled="!newTplName.trim()" @click="saveTemplate">保存</button>
        </div>
      </div>
    </div>
    <!-- 删除会话确认弹窗（对齐设备管理 modal 样式） -->
    <div v-if="delTarget" class="modal-overlay" @click.self="delTarget = null">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">⚠️</span>
          <span class="modal-title">删除会话</span>
        </div>
        <div class="modal-body">
          <p style="margin:0 0 12px">确认删除会话「<b>{{ delTarget.name }}</b>」？</p>
          <p style="margin:0">删除后<b>不可恢复</b>。</p>
        </div>
        <div class="modal-footer">
          <button class="btn" :disabled="delBusy" @click="delTarget = null">取消</button>
          <button class="btn btn-danger" :disabled="delBusy" @click="confirmDelSession">{{ delBusy ? '删除中...' : '确认删除' }}</button>
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
// 会话列表游标分页（滚动加载——首屏 20——触底加载更多）
const sessionCursorTime = ref('')
const sessionCursorId = ref('')
const sessionHasMore = ref(false)
const sessionsLoadingMore = ref(false)
// 头部功能介绍气泡（？图标点击展示——点外部关闭）
const descVisible = ref(false)
function toggleDesc() { descVisible.value = !descVisible.value }
// 使用帮助气泡（💡 按钮点击展示——点外部关闭）
const hintVisible = ref(false)
function toggleHint() { hintVisible.value = !hintVisible.value }
const pollTimers = new Set()        // N5b：轮询定时器集合（卸载时统一清理）
const customTemplates = ref([])
const templateMax = ref(50)
const showSaveTpl = ref(false)
const newTplNameInput = ref(null)   // 保存模板弹窗输入框（#5 聚焦）
const queryInputRef = ref(null)     // 查询输入框（#4 去精简聚焦）
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

const activeSession = computed(() => sessions.value.find(s => s.id === activeSessionId.value) || null)

// 会话级禁发：当前会话处理中（taskId 有且未完成）→ 禁发
// （避免同会话并发提交——hermes --continue 同名并发——卡/乱）
const canSend = computed(() => {
  const s = activeSession.value
  return !(s && s.taskId && !s.taskCompleted)
})
const activeMsgs = computed(() => activeSession.value?.msgs || [])
const currentName = computed(() => activeSession.value?.name || '')
const isQuerying = computed(() => {
  const s = activeSession.value
  return s ? (!!s.taskId && !s.taskCompleted) : false
})
function isSessionRunning(s) {
  return s && !!s.taskId && !s.taskCompleted
}

/** 加载更早历史（游标分页——每次 20——拼头部） */
async function loadOlder() {
  const s = activeSession.value
  if (!s || s.msgs === null || !s.hasMore || s.loadingOlder) return
  s.loadingOlder = true
  await loadMsgs(s, true)
  s.loadingOlder = false
  await nextTick()
  initCharts()
}

/** 重试失败查询（复用失败消息的 task_id——后端 UPDATE 覆盖——一轮一条 assistant） */
async function retryQuery(failedMsg) {
  const s = activeSession.value
  if (!s) return
  // 用 taskId 精确匹配对应 user（不依赖相邻位置——乱序/多轮
  // 时前一条可能是 assistant——导致静默不响应）
  const userMsg = (s.msgs || []).find(m => m.role === 'user' && m.taskId === failedMsg.taskId) || null
  if (!userMsg) return
  if (isQuerying.value) return
  const text = userMsg.raw
  // 失败消息就地转为 streaming 占位（重试结果覆盖）
  failedMsg.raw = ''
  failedMsg.streaming = true
  failedMsg.segments = null
  scrollToBottom()
  try {
    const { data } = await http.post('/ai/chat/submit', { query: text, sessionId: s.id, taskId: failedMsg.taskId })
    s.taskId = data.taskId
    s.taskCompleted = false
    setStoredTaskId(s.id, data.taskId)
    await streamTask(s, failedMsg)
    await loadSessions()
    // 重试后重新拉消息（服务端已覆盖失败消息——显示与服务端一致——
    // 避免旧 msgs 残留/顺序错乱）
    if (s.msgs !== null) {
      await loadMsgs(s)
      await nextTick()
      initCharts()
    }
  } catch (e) {
    failedMsg.raw = `[请求失败] ${e.response?.data?.message || e.message}`
    failedMsg.streaming = false
    s.taskCompleted = true
    clearTaskKeys(s)
  }
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
// H1：双键清理助手（local id + 已换绑的 hermesSessionId——防 stale 残留/幻影会话）
function clearTaskKeys(session) {
  clearStoredTaskId(session.id)
  if (session.sessionId) clearStoredTaskId(session.sessionId)
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
  // 已有未发送的空会话（new_）→ 切换回去（不重复创建——列表不污染——
  // 同一时间最多一个"新建空会话"）
  const existingNew = sessions.value.find(s => s.id.startsWith('new_'))
  if (existingNew) {
    activeSessionId.value = existingNew.id
    return existingNew
  }
  disposeAllCharts()
  const s = {
    id: 'new_' + Date.now() + '_' + Math.random().toString(36).substr(2, 6),   // 临时 id——提交后换后端会话 UUID
    name: '新对话',
    msgs: [],
    minSeq: 0,
    hasMore: false,
    taskId: null,
    taskCompleted: true,
    local: true
  }
  sessions.value.unshift(s)
  activeSessionId.value = s.id
  return s
}
async function loadSessions() {
  try {
    // 新表会话列表（游标分页——首屏 20 条——滚动触底加载更多——不截断）
    const { data } = await http.get('/ai/chat/sessions?limit=20')
    sessions.value = (data.sessions || []).map(s => {
      const existing = sessions.value.find(x => x.id === s.sessionId)
      const storedTask = getStoredTaskId(s.sessionId)   // sessionStorage 恢复未完成任务
      return {
        id: s.sessionId,
        name: s.name,
        updatedAt: s.updatedAt || '',
        msgs: existing ? existing.msgs : null,   // 已加载的保留（当前会话不空——防页面空白）——未加载的懒加载
        minSeq: existing?.minSeq || 0,
        hasMore: existing?.hasMore ?? true,
        taskId: existing?.taskId || storedTask,
        taskCompleted: existing?.taskCompleted ?? !storedTask,
        local: false
      }
    })
    // 游标（下一页用——最后一条的 updated_at + sessionId）
    const last = sessions.value[sessions.value.length - 1]
    sessionCursorTime.value = last ? last.updatedAt || '' : ''
    sessionCursorId.value = last ? last.id || '' : ''
    sessionHasMore.value = !!data.hasMore
    sessionsLoadingMore.value = false
    if (sessions.value.length === 0) newSession()
    if (!sessions.value.some(s => s.id === activeSessionId.value)) {
      activeSessionId.value = sessions.value[0]?.id || null
    }
    // 进入页面自动加载第一个会话（对齐 VOD）
    const first = sessions.value[0]
    if (first && first.msgs === null && activeSessionId.value === first.id) {
      first.msgs = []
      await loadMsgs(first)
      await nextTick()
      initCharts()
      scrollToBottom()
    }
  } catch (e) {
    // 加载失败不清空已有会话（弱网保留内存态）
    if (sessions.value.length === 0) newSession()
  }
}

// 会话列表滚动触底——加载更多
function onSessionScroll(e) {
  const el = e.target
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 24) {
    loadMoreSessions()
  }
}

// 会话列表滚动触底——加载更多（游标分页——追加——不截断）
async function loadMoreSessions() {
  if (!sessionHasMore.value || sessionsLoadingMore.value) return
  sessionsLoadingMore.value = true
  try {
    const params = new URLSearchParams({ limit: '20' })
    if (sessionCursorTime.value) params.set('cursorTime', sessionCursorTime.value)
    if (sessionCursorId.value) params.set('cursorId', sessionCursorId.value)
    const { data } = await http.get(`/ai/chat/sessions?${params.toString()}`)
    const more = (data.sessions || []).map(s => {
      const existing = sessions.value.find(x => x.id === s.sessionId)
      const storedTask = getStoredTaskId(s.sessionId)
      return {
        id: s.sessionId,
        name: s.name,
        updatedAt: s.updatedAt || '',
        msgs: existing ? existing.msgs : null,
        minSeq: existing?.minSeq || 0,
        hasMore: existing?.hasMore ?? true,
        taskId: existing?.taskId || storedTask,
        taskCompleted: existing?.taskCompleted ?? !storedTask,
        local: false
      }
    })
    // 追加（去重——滚动触底可能重复触发）
    const known = new Set(sessions.value.map(x => x.id))
    sessions.value.push(...more.filter(x => !known.has(x.id)))
    const last = more[more.length - 1]
    sessionCursorTime.value = last ? last.updatedAt || '' : ''
    sessionCursorId.value = last ? last.id || '' : ''
    sessionHasMore.value = !!data.hasMore
  } catch (e) {
    console.warn('加载更多会话失败', e)
  } finally {
    sessionsLoadingMore.value = false
  }
}
async function loadMsgs(s, older = false) {
  try {
    // 新表消息（游标分页——首次最新 40——滚动更早 20——seq 升序返回）
    const { data } = await http.get(`/ai/chat/session/${s.id}/messages?cursorSeq=${older ? s.minSeq : ''}&limit=${older ? 20 : 40}`)
    const msgs = (data.messages || []).map(m => ({
      msgId: m.msgId,
      role: m.role,
      raw: m.content,
      seq: m.seq,
      taskId: m.taskId,
      segments: m.role === 'assistant' ? renderParts(m.content) : null   // 对齐 VOD：加载即结构化
    }))
    s.msgs = older ? [...msgs, ...(s.msgs || [])] : msgs
    s.minSeq = data.minSeq || 0
    s.hasMore = !!data.hasMore
  } catch (e) { /* ignore */ }
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
  // A：重命名聚焦 + 全选（对齐 VOD——双击后直接可输入）
  nextTick(() => {
    const inp = document.querySelector('.session-name-input')
    if (inp) { inp.focus(); inp.select() }
  })
}
async function confirmRename() {
  if (editingId.value && editName.value.trim()) {
    const s = sessions.value.find(x => x.id === editingId.value)
    if (s && editName.value.trim() !== s.name) {
      s.name = editName.value.trim()
      if (!s.id.startsWith('new_')) {
        try { await http.put(`/ai/chat/session/${s.id}/name`, { name: s.name }) } catch (e) { /* ignore */ }
      }
    }
  }
  editingId.value = null
}
function cancelRename() { editingId.value = null }
// ===== 删除会话（设备管理 modal 弹窗——对齐项目统一弹窗样式）=====
const delTarget = ref(null)   // 删除确认目标会话
const delBusy = ref(false)
function askDelSession(s) { delTarget.value = s }
async function confirmDelSession() {
  const s = delTarget.value
  if (!s || delBusy.value) return
  delBusy.value = true
  try {
    disposeAllCharts()
    // #3：同时清两个 key（local id + 已换绑的 hermesSessionId——防 stale 残留）
    clearStoredTaskId(s.id)
    if (!s.id.startsWith('new_')) {
      try { await http.delete(`/ai/chat/session/${s.id}`) } catch (e) { /* ignore */ }
    }
    sessions.value = sessions.value.filter(x => x.id !== s.id)
    delTarget.value = null
    // N5d：删光所有会话 → 自动新建（对齐 VOD——列表不空态无引导）
    if (sessions.value.length === 0) { newSession(); return }
    // 删除活跃会话 → 自动加载下一个（对齐 VOD：删除后显示下一个会话信息）
    if (activeSessionId.value === s.id) {
      const next = sessions.value[0]
      activeSessionId.value = next?.id || null
      if (next && next.msgs === null) { next.msgs = []; await loadMsgs(next) }
      await nextTick()
      initCharts()
      scrollToBottom()
    } else {
      await nextTick()
    }
  } finally {
    delBusy.value = false
  }
}

// ===== 模板 =====
async function loadTemplates() {
  try {
    const { data } = await http.get('/ai/templates')
    customTemplates.value = data.templates || []
    templateMax.value = data.max || 50
  } catch (e) {
    // ④：加载失败提示（对齐 VOD——toast error）
    showToast('模板加载失败，请稍后重试', 'error')
  }
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
  if (templateMax.value <= customTemplates.value.length) { showToast(`模板数量已达上限 ${templateMax.value}`, 'error'); return }
  // #6：内容查重（对齐 VOD——开弹窗前拦截——内容重复拒绝）
  const q = queryText.value.trim()
  if (q && customTemplates.value.some(t => t.content === q)) { showToast('模板已存在', 'error'); return }
  newTplName.value = ''
  showSaveTpl.value = true
  // #5：打开弹窗聚焦输入框（对齐 VOD）
  nextTick(() => { newTplNameInput.value?.focus() })
}
// #4：超限弹窗「去精简」——聚焦 textarea 尾部（对齐 VOD goRefineQuery）
function goRefineQuery() {
  showOverLimitModal.value = false
  nextTick(() => {
    const ta = queryInputRef.value
    if (ta) {
      ta.focus()
      ta.setSelectionRange(ta.value.length, ta.value.length)
      ta.scrollIntoView({ block: 'center' })
    }
  })
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
  if (!canSend.value) return   // 会话级禁发防御（处理中会话不可发——模板 disabled 之外的双保险）
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
    const isNew = session.id.startsWith('new_')
    const { data } = await http.post('/ai/chat/submit', { query: text, sessionId: isNew ? null : session.id })
    if (isNew) {
      session.id = data.sessionId    // 新会话：换后端会话 UUID（新表——绑定 hermes）
      activeSessionId.value = data.sessionId   // 换绑立即同步（否则 activeSession=null——右侧空白/标题"对话记录"）
    }
    session.taskId = data.taskId
    session.taskCompleted = false
    setStoredTaskId(session.id, data.taskId)
    await streamTask(session, aiMsg)
    await loadSessions()
    if (isNew) activeSessionId.value = session.id    // 完成后保持当前会话
  } catch (e) {
    aiMsg.raw = `[请求失败] ${e.response?.data?.message || e.message}`
    aiMsg.streaming = false
    session.taskCompleted = true
    clearTaskKeys(session)
  }
}

/** SSE 订阅任务输出：token 追加 → session 事件 → 终态清理（后端重放缓冲，可重连） */
let currentStreamAbort = null   // 当前 SSE 连接的 AbortController（组件卸载/重连时中断旧连接）
async function streamTask(session, aiMsg) {
  // #2：新订阅先中断旧 SSE 连接（对齐 VOD——防双 reader 同时 append 文本重复）
  if (currentStreamAbort) { currentStreamAbort.abort(); currentStreamAbort = null }
  const ac = new AbortController()
  currentStreamAbort = ac
  // #3：fetch + 自动重试（502/网络瞬断——重试一次——2s 后——
  //   nginx keepalive 之外的前端兜底——用户无感）
  let resp = null
  let lastErr = ''
  for (let attempt = 1; attempt <= 2; attempt++) {
    try {
      resp = await fetch(`/api/admin/ai/chat/task/${session.taskId}/stream`, {
        headers: { Authorization: `Bearer ${getToken()}` },
        signal: ac.signal
      })
      if (resp.ok && resp.body) break
      lastErr = resp.status === 502 ? '服务暂时不可用（可能正在部署/重启），请稍后重试' : 'HTTP ' + resp.status
      if (resp.status !== 502 && resp.status !== 499) break   // 502/499（nginx 瞬断/客户端断）重试一次——其他不重试
    } catch (e) {
      if (e.name === 'AbortError') return
      lastErr = e.message
    }
    if (attempt === 1) await new Promise(r => setTimeout(r, 2000))
  }
  if (!resp || !resp.ok || !resp.body) {
    aiMsg.raw += `\n[连接失败] ${lastErr}`
    aiMsg.streaming = false
    session.taskCompleted = true
    clearTaskKeys(session)
    return
  }
  const reader = resp.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  while (true) {
    let chunk
    try {
      chunk = await reader.read()
    } catch (e) {
      if (e.name === 'AbortError') return   // 中断——静默退出
      throw e
    }
    const { done, value } = chunk
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
      } else if (ev.type === 'error') {
        aiMsg.raw += `\n[错误] ${ev.data}`
      }
    }
  }
  aiMsg.streaming = false
  session.taskCompleted = true
  clearTaskKeys(session)
  // #2b：异步空结果兜底（对齐 VOD——AI 返回空时提示而非空白）
  if (!aiMsg.raw.trim()) aiMsg.raw = '（无返回内容）'
  aiMsg.segments = renderParts(aiMsg.raw)   // 对齐 VOD：完成后一次性结构化缓存
  await nextTick()
  initCharts()
  scrollToBottom()
}

/** 断线/切回重连：未完成任务重新订阅（后端重放已缓冲内容，不丢不重） */
async function reconnectActiveTask() {
  const s = activeSession.value
  if (!s) return
  // taskId 恢复：优先内存/sessionStorage——兜底最后一条 user 消息的 taskId
  // （DB 权威——不依赖浏览器 sessionStorage——新标签页/时序都可靠）
  let taskId = s.taskId || getStoredTaskId(s.id)
  if (!taskId && s.msgs && s.msgs.length) {
    // 兜底候选：优先【失败消息】的 taskId（重试中的任务——
    // 重试不写新 user——失败消息仍在——taskId 指向重试任务）；
    // 其次最后一条 user（正常任务）
    const lastFailed = [...s.msgs].reverse().find(m => m.role === 'assistant' && m.raw === '（查询失败，可重试）' && m.taskId)
    const lastUser = [...s.msgs].reverse().find(m => m.role === 'user' && m.taskId)
    taskId = (lastFailed ? lastFailed.taskId : null) || (lastUser ? lastUser.taskId : null)
  }
  if (!taskId) { s.taskCompleted = true; return }
  s.taskId = taskId
  s.taskCompleted = false   // 发现任务——视为运行中（禁发）——预检终态再解锁
  // #3：重连前预检任务状态（对齐 VOD——任务已不存在/已完成 → 静默收尾）
  try {
    const { data } = await http.get(`/ai/chat/task/${taskId}/status`)
    if (!data || data.status === 'done' || data.status === 'error' || data.status === 'not_found') {
      s.taskCompleted = true
      // N1：终态收尾——streaming 占位消息置 false（防 loading dots 永远转圈）
      const pending = [...(s.msgs || [])].reverse().find(m => m.role === 'assistant' && m.streaming)
      if (pending) {
        pending.streaming = false
        if (!pending.raw?.trim()) pending.raw = '（无返回内容）'
      }
      clearStoredTaskId(s.id)
      // 运行中刷新修复：终态时重新拉取完整消息（assistant 可能已写入 DB——
      // 旧逻辑不拉→只有 user→看不到响应；重拉后立即显示）
      if (s.msgs !== null) {
        await loadMsgs(s)
        await nextTick()
        initCharts()
        scrollToBottom()
      }
      return
    }
  } catch (e) {
    if (e.response?.status === 404) {
      s.taskCompleted = true
      clearStoredTaskId(s.id)
      // 404：任务不存在——消息可能已写入（任务清理后）——兜底拉取防缺失
      if (s.msgs !== null) {
        await loadMsgs(s)
        await nextTick()
        initCharts()
      }
      return
    }
    return   // 网络异常——静默（保持状态——下次再试）
  }
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
    const isNew = session.id.startsWith('new_')
    const { data } = await http.post('/ai/chat/submit', { query: text, sessionId: isNew ? null : session.id })
    if (isNew) {
      session.id = data.sessionId    // 新会话：换后端会话 UUID
      activeSessionId.value = data.sessionId   // 换绑立即同步
    }
    session.taskId = data.taskId
    session.taskCompleted = false
    setStoredTaskId(session.id, data.taskId)
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
    // N5a：会话已删除 → 停止轮询（对齐 VOD：会话不存在即停止）
    if (!sessions.value.some(x => x.id === session.id)) return
    try {
      const { data } = await http.get(`/ai/chat/task/${session.taskId}/status`)
      if (data.status === 'running') {
        if (data.result) {
          aiMsg.raw = data.result
          if (activeSessionId.value === session.id) scrollToBottom()
        }
        const t = setTimeout(() => poll(0), POLL_INTERVAL)
        pollTimers.add(t)
      } else if (data.status === 'done') {
        aiMsg.raw = data.result || '（无返回内容）'
        session.taskCompleted = true
        clearTaskKeys(session)
        if (data.sessionId) session.sessionId = data.sessionId
        await nextTick()
        initCharts()
        scrollToBottom()
        loadSessions()
      } else if (data.status === 'error') {
        aiMsg.raw = '查询失败: ' + (data.error || '未知错误')
        session.taskCompleted = true
        clearTaskKeys(session)
      }
    } catch (e) {
      // N5c：任务不存在（Map 已清理/后端清理）——明确提示（对齐 VOD not_found）
      if (e.response?.status === 404) {
        aiMsg.raw = '任务不存在或已过期'
        session.taskCompleted = true
        clearTaskKeys(session)
        return
      }
      if (retry < 3) {
        const t = setTimeout(() => poll(retry + 1), POLL_INTERVAL)
        pollTimers.add(t)
      } else {
        aiMsg.raw = '查询失败: 网络错误'
        session.taskCompleted = true
        clearTaskKeys(session)
      }
    }
  }
  poll(0)
}

// ===== 渲染 =====
/** 规范化 echarts 配置（hermes 输出宽容——title 可能是字符串——
 *  echarts 期望 {text}——字符串会抛 "Cannot use 'in' operator"） */
function normalizeChartOption(opt) {
  if (opt && typeof opt === 'object') {
    if (typeof opt.title === 'string') opt.title = { text: opt.title }
    else if (Array.isArray(opt.title)) {
      opt.title = opt.title.map(t => (typeof t === 'string' ? { text: t } : t))
    }
  }
  return opt
}
// 对齐 VOD：消息带 segments（结构化——text/chart 分离）——一次性解析缓存——直接渲染
function segFor(m) {
  if (!m) return []
  if (m.segments) return m.segments
  if (m.raw) return renderParts(m.raw)
  return []
}
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
      if (Array.isArray(chartJson)) {
        // hermes 多图表包装（[{title,type,option}]）→ 拆多个 chart 段
        // （直接 setOption 数组会抛 "r.split is not a function"）
        for (const item of chartJson) {
          const opt = item && typeof item === 'object' && item.option ? item.option : item
          if (opt && typeof opt === 'object') parts.push({ type: 'chart', json: opt })
        }
      } else {
        parts.push({ type: 'chart', json: chartJson })
      }
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
  // hermes 可能在 JSON 前输出 "echarts" 前缀（```chart echarts{...}）——容错去掉
  raw = (raw || '').trim()
  if (raw.startsWith('echarts')) raw = raw.slice(7).trim()
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
    const seg = segFor(activeMsgs.value[mi])[si]
    if (!seg || seg.type !== 'chart') continue
    // 对齐 VOD：已有实例先销毁再重建（切换会话后 DOM 重建，旧实例绑定在已销毁节点上）
    if (chartInstances[key]) {
      chartInstances[key].dispose()
      delete chartInstances[key]
    }
    try {
      const inst = echarts.init(el)
      // 规范化配置（hermes 可能输出 title 字符串——echarts 期望 {text}——
      // 否则 "Cannot use 'in' operator" 抛错）
      inst.setOption(normalizeChartOption(seg.json))
      chartInstances[key] = inst
    } catch (e) {
      // 单图表失败不崩整体——记录具体错误 + 延迟重试一次（容器就绪）
      console.error('图表渲染失败 key=' + key, e)
      try {
        await new Promise(r => setTimeout(r, 100))
        const retry = echarts.init(el)
        retry.setOption(seg.json)
        chartInstances[key] = retry
      } catch (e2) {
        console.error('图表渲染重试仍失败 key=' + key, e2)
        el.innerHTML = '<div class="chart-error">图表渲染失败</div>'
      }
    }
  }
}
/** 对齐 VOD：销毁并清空全部图表实例（切换/新建/删除会话时调用，防 key 缓存误命中） */
function disposeAllCharts() {
  Object.values(chartInstances).forEach(c => { try { c.dispose() } catch (e) { /* ignore */ } })
  Object.keys(chartInstances).forEach(k => delete chartInstances[k])
}
// N2：全屏切换后图表 resize（对齐 VOD——立即 + 350ms 二次适配过渡动画）
function resizeAllCharts() {
  Object.values(chartInstances).forEach(c => { try { c.resize() } catch (e) { /* ignore */ } })
}
function toggleFullscreen() {
  isFullscreen.value = !isFullscreen.value
  nextTick(() => {
    resizeAllCharts()
    setTimeout(resizeAllCharts, 350)
  })
}
function formatSessionName(name) {
  // 显示层统一截断（对齐 VOD 20 字显示——后端可存更长——刷新前后一致）
  const t = (name || '').trim()
  return t.length <= 20 ? t : t.substring(0, 20) + '...'
}
function scrollToBottom() {
  nextTick(() => { if (resultBox.value) resultBox.value.scrollTop = resultBox.value.scrollHeight })
}
// N4：复制结果净化（图表段替换 [图表]——对齐 VOD）+ 剪贴板降级（http 环境 execCommand）
async function copyAll() {
  const text = activeMsgs.value.map(m => {
    const prefix = m.role === 'user' ? '我: ' : 'AI: '
    const parts = renderParts(m.raw || '')
    const content = parts.map(p => (p.type === 'chart' ? '[图表]' : p.text)).join('\n')
    return prefix + content
  }).join('\n\n')
  try {
    await navigator.clipboard.writeText(text)
  } catch (e) {
    // 降级：非安全上下文（内网 http）——execCommand
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    try { document.execCommand('copy') } catch (e2) { /* ignore */ }
    document.body.removeChild(ta)
  }
  copyOk.value = true
  setTimeout(() => { copyOk.value = false }, 1500)
}
// N3：流式图表防抖（对齐 VOD——流式期间不每 token 重建图表——停顿后渲染）
let chartsTimer = null
watch(activeMsgs, () => {
  if (chartsTimer) clearTimeout(chartsTimer)
  chartsTimer = setTimeout(() => initCharts(), 500)
}, { deep: true })

// ===== 挂载 =====
function onDocClick(e) {
  if (tplWrapRef.value && !tplWrapRef.value.contains(e.target)) showTplDrop.value = false
  if (descVisible.value && !e.target.closest('.desc-icon') && !e.target.closest('.desc-bubble')) descVisible.value = false
  if (hintVisible.value && !e.target.closest('.hint-btn') && !e.target.closest('.hint-bubble')) hintVisible.value = false
}
// N1：Escape 快捷键（对齐 VOD——优先级：超限弹窗 > 全屏 > 保存模板 > 模板下拉）
function onKeydown(e) {
  if (e.key !== 'Escape') return
  if (showOverLimitModal.value) closeOverLimitModal()
  else if (isFullscreen.value) toggleFullscreen()   // #4：退全屏走 toggle（触发图表 resize）
  else if (showSaveTpl.value) showSaveTpl.value = false
  else if (showTplDrop.value) showTplDrop.value = false
}
onMounted(async () => {
  loadTemplates()
  await loadSessions()
  if (asyncMode.value) reconnectActiveTask()
  document.addEventListener('click', onDocClick)
  window.addEventListener('keydown', onKeydown)
})
onUnmounted(() => {
  // 中断当前 SSE 订阅（后端任务继续运行——切页面不中断分析）
  if (currentStreamAbort) { currentStreamAbort.abort(); currentStreamAbort = null }
  // N5b：清理全部轮询定时器（对齐 VOD——卸载后无残留 1.5s 轮询）
  pollTimers.forEach(t => clearTimeout(t))
  pollTimers.clear()
  if (chartsTimer) clearTimeout(chartsTimer)
  document.removeEventListener('click', onDocClick)
  window.removeEventListener('keydown', onKeydown)
  disposeAllCharts()
})
</script>

<style scoped>
/* ===== 对齐 VOD AiQuery.vue 样式体系 ===== */
.ai-query-header { background: #fff; border-bottom: 1px solid var(--border-color); padding: 16px 32px; flex-shrink: 0; position: relative; }
.ai-query-header h2 { font-size: 18px; font-weight: 600; }
.desc-icon { display: inline-block; width: 16px; height: 16px; line-height: 14px; text-align: center; border: 1px solid var(--text-secondary); border-radius: 50%; font-size: 11px; color: var(--text-secondary); cursor: pointer; vertical-align: 2px; margin-left: 6px; user-select: none; }
.desc-bubble { position: absolute; top: 50px; left: 32px; background: #fff; border: 1px solid var(--border-color); border-radius: 8px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08); padding: 10px 14px; font-size: 12px; color: var(--text-secondary); max-width: 400px; line-height: 1.6; z-index: 20; }
.ai-query-layout { display: flex; gap: 20px; height: calc(100vh - 140px); padding: 20px 28px; }
.ai-query-input-panel { width: 38%; flex-shrink: 0; background: #fff; border-radius: 12px; border: 1px solid var(--border-color); padding: 20px; display: flex; flex-direction: column; overflow-y: auto; }
.ai-query-result-panel { flex: 1; background: #fff; border-radius: 12px; border: 1px solid var(--border-color); padding: 20px; display: flex; flex-direction: column; overflow: hidden; transition: all 0.3s ease; }
.ai-query-result-panel.fullscreen-mode { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; z-index: 9999; border-radius: 0; border: none; padding: 24px; transition: all 0.3s ease; }

.session-list-section { margin-top: 16px; border-top: 1px solid var(--border-color); padding-top: 12px; display: flex; flex-direction: column; flex: 1; min-height: 0; }
.session-list-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.session-list-header h4 { font-size: 13px; color: var(--text-secondary); }
.session-list { flex: 1; overflow-y: auto; min-height: 80px; }
.session-item { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 8px; cursor: pointer; font-size: 13px; transition: all var(--transition); }
.session-item:hover { background: rgba(0,0,0,0.04); }
.session-item.active { background: #e8f5ff; color: var(--primary); font-weight: 500; }
.session-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-time { font-size: 11px; color: var(--text-tertiary); flex-shrink: 0; }
.session-del { width: 18px; height: 18px; border: none; background: transparent; cursor: pointer; border-radius: 8px; color: var(--text-tertiary); font-size: 11px; flex-shrink: 0; }
.session-del:hover { background: rgba(255,59,48,0.08); color: #ff3b30; }
.session-edit { width: 18px; height: 18px; border: none; background: transparent; cursor: pointer; border-radius: 8px; color: var(--text-tertiary); font-size: 11px; flex-shrink: 0; opacity: 0; transition: opacity 0.15s; }
.session-item:hover .session-edit { opacity: 1; }
.session-edit:hover { background: #e8f5ff; color: var(--primary); }
.session-name-input { flex: 1; font-size: 13px; border: 1px solid var(--primary); border-radius: 8px; padding: 2px 6px; outline: none; background: #fff; }
.session-item.editing { background: rgba(0,107,255,0.06); }
.session-item.running .session-name { color: var(--primary); }
.session-running-indicator { color: #34c759; font-size: 10px; animation: pulse 1.5s infinite; flex-shrink: 0; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
.session-empty { font-size: 12px; color: var(--text-tertiary); text-align: center; padding: 12px; }

.query-toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.query-toolbar h3 { font-size: 16px; font-weight: 600; }
.query-actions { display: flex; gap: 8px; align-items: center; }
.async-toggle { display: flex; align-items: center; gap: 4px; font-size: 12px; color: var(--text-secondary); cursor: pointer; user-select: none; white-space: nowrap; }
.async-toggle input { margin: 0; cursor: pointer; }
.async-toggle:hover { color: var(--primary); }
.template-select { padding: 6px 10px; border: 1px solid var(--border-color); border-radius: 8px; font-size: 13px; outline: none; }
.query-textarea { width: 100%; flex: 1; flex-shrink: 0; min-height: 120px; max-height: 140px; padding: 12px; border: 1px solid var(--border-color); border-radius: 8px; font-size: 14px; font-family: inherit; resize: none; outline: none; box-sizing: border-box; }[truncated]
.query-textarea:focus { border-color: var(--primary); }
.query-input-wrap { display: flex; flex-grow: 0; }
.query-textarea-box { position: relative; display: flex; align-self: flex-start; width: 100%; }
.query-textarea-box .char-counter { position: absolute; bottom: 6px; right: 10px; font-size: 11px; color: var(--text-tertiary); pointer-events: none; }
.hint-btn { display: inline-block; width: 20px; height: 20px; line-height: 18px; text-align: center; border: 1px solid var(--border-color); border-radius: 50%; background: #fff; font-size: 12px; cursor: pointer; margin-left: 6px; vertical-align: 2px; color: var(--text-secondary); }
.hint-btn:hover { border-color: var(--primary); color: var(--primary); }
.hint-bubble { position: absolute; top: 28px; left: 0; background: #fff; border: 1px solid var(--border-color); border-radius: 8px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08); padding: 10px 14px; font-size: 12px; color: var(--text-secondary); line-height: 1.8; z-index: 20; white-space: nowrap; }
.hint-bubble p { margin: 0; }

.result-toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.result-toolbar h3 { font-size: 16px; font-weight: 600; }
.result-toolbar-actions { display: flex; gap: 8px; align-items: center; }
.result-content { flex: 1; overflow-y: auto; }
.result-empty { text-align: center; color: var(--text-tertiary); padding: 60px 20px; }
.empty-icon { font-size: 48px; margin-bottom: 12px; }
.empty-hint { font-size: 12px; margin-top: 4px; }

.dialog-message { display: flex; gap: 12px; margin-bottom: 20px; }
.dialog-message.user { flex-direction: row-reverse; }
.msg-avatar { width: 36px; height: 36px; border-radius: 50%; flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-size: 20px; background: rgba(0,0,0,0.04); overflow: hidden; }
.msg-body { flex: 1; min-width: 0; }
.dialog-message.user .msg-body { display: flex; flex-direction: column; align-items: flex-end; }
.msg-role { font-size: 12px; color: var(--text-tertiary); margin-bottom: 4px; }
.result-segment { margin-bottom: 8px; }
.dialog-message.user .result-segment { max-width: 90%; }
.result-text { font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; font-family: 'Courier New', monospace; color: var(--text-primary); padding: 12px; background: rgba(0,0,0,0.02); border-radius: 8px; margin: 0; }
.dialog-message.user .result-text { background: #e8f5ff; }
.chart-container { width: 100%; height: 400px; border-radius: 8px; border: 1px solid var(--border-soft); }
.loading-dots-container { display: inline-flex; align-items: center; gap: 8px; }
.loading-dots { display: inline-flex; gap: 6px; padding: 8px; }
.loading-dots span { width: 8px; height: 8px; border-radius: 50%; background: var(--primary); animation: query-bounce 1.2s infinite; }
.loading-dots span:nth-child(2) { animation-delay: 0.2s; }
.loading-dots span:nth-child(3) { animation-delay: 0.4s; }
@keyframes query-bounce { 0%,60%,100% { transform: translateY(0); } 30% { transform: translateY(-6px); } }
.btn-success { background: #34c759 !important; color: #fff !important; border-color: #34c759 !important; }

/* 自定义模板管理区 */
.char-counter-row { display: flex; justify-content: flex-end; align-items: center; margin-top: 4px; padding: 0 2px; }
.char-counter { font-size: 12px; color: var(--text-tertiary); transition: color 0.2s; }
.char-counter.near-limit { color: #ff9500; font-weight: 600; }
.char-counter.at-limit { color: #ff3b30; font-weight: 600; }
.template-bar { display: flex; justify-content: flex-end; align-items: center; gap: 8px; margin-top: 8px; }
.template-count-hint { font-size: 12px; color: var(--text-tertiary); margin-right: auto; }
.btn-save-template { display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px; border: 1px dashed var(--primary); border-radius: 8px; font-size: 13px; color: var(--primary); background: rgba(0,107,255,0.06); cursor: pointer; transition: all var(--transition); }
.btn-save-template:hover:not(:disabled) { background: #e8f5ff; border-style: solid; }
.btn-save-template:disabled { color: var(--text-tertiary); border-color: var(--border-color); background: rgba(0,0,0,0.04); cursor: not-allowed; }
.custom-template-wrapper { position: relative; }
.custom-template-btn { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border: 1px solid var(--border-color); border-radius: 8px; font-size: 13px; background: #fff; color: var(--text-primary); cursor: pointer; transition: all var(--transition); }
.custom-template-btn:hover { border-color: var(--primary); }
.custom-template-btn .arrow { margin-left: 4px; font-size: 10px; transition: transform 0.2s; }
.custom-template-btn.open .arrow { transform: rotate(180deg); }
.custom-template-dropdown { position: absolute; top: calc(100% + 4px); right: 0; width: 280px; background: #fff; border: 1px solid var(--border-color); border-radius: 8px; box-shadow: 0 4px 16px rgba(0,0,0,0.12); z-index: 100; overflow: hidden; }
.custom-template-dropdown-header { padding: 10px 14px; font-size: 12px; color: var(--text-tertiary); font-weight: 500; border-bottom: 1px solid var(--border-soft); }
.custom-template-dropdown-body { max-height: 280px; overflow-y: auto; }
.custom-template-empty { padding: 20px 14px; text-align: center; color: var(--text-tertiary); font-size: 13px; }
.custom-template-item { display: flex; align-items: center; padding: 9px 14px; cursor: pointer; transition: background 0.1s; font-size: 13px; gap: 8px; }
.custom-template-item:hover { background: rgba(0,107,255,0.06); }
.custom-template-item .template-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.custom-template-item .template-delete { width: 22px; height: 22px; border: none; background: transparent; cursor: pointer; border-radius: 8px; display: flex; align-items: center; justify-content: center; color: var(--text-tertiary); font-size: 14px; flex-shrink: 0; opacity: 0; transition: opacity 0.15s; }
.custom-template-item:hover .template-delete { opacity: 1; }
.custom-template-item .template-delete:hover { background: rgba(255,59,48,0.08); color: #ff3b30; }

/* 保存模板弹窗 */
.dialog-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.35); z-index: 10000; display: flex; align-items: center; justify-content: center; }
.save-template-dialog { background: #fff; border-radius: 12px; width: 420px; max-width: 90vw; box-shadow: 0 8px 32px rgba(0,0,0,0.2); }
.dialog-header { padding: 20px 20px 0; position: relative; }
.dialog-header h4 { font-size: 16px; font-weight: 600; margin: 0 0 4px; }
.dialog-subtitle { font-size: 12px; color: var(--text-secondary); margin: 0; }
.dialog-close { position: absolute; top: 16px; right: 16px; border: none; background: transparent; cursor: pointer; font-size: 18px; color: var(--text-tertiary); padding: 4px; border-radius: 8px; }
.dialog-close:hover { background: rgba(0,0,0,0.04); color: var(--text-primary); }
.dialog-body { padding: 16px 20px; }
.dialog-label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 6px; }
.dialog-input { width: 100%; padding: 8px 12px; border: 1px solid var(--border-color); border-radius: 8px; font-size: 14px; outline: none; box-sizing: border-box; }
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
