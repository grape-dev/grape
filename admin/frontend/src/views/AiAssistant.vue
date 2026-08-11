<template>
  <div class="chat-wrap">
    <div class="cd chat-main" style="height:calc(100vh - 120px);display:flex;flex-direction:column;margin-bottom:0">
      <div class="cd-h" style="border-bottom:1px solid var(--b);margin:0 -20px 0;padding:0 20px 12px">
        <h2>AI 助手</h2>
      </div>
      <div style="padding:8px 0;border-bottom:1px solid var(--b);display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <select v-model="selectedTemplate" @change="applyTemplate" style="padding:4px 8px;font-size:12px;border:1px solid var(--b);border-radius:6px;background:var(--bg);outline:none;cursor:pointer;max-width:260px">
          <option value="">-- 选择模板 --</option>
          <optgroup v-for="(tpls, g) in builtinGroups" :key="g" :label="g">
            <option v-for="(t, k) in tpls" :key="k" :value="k">{{ t.label }}</option>
          </optgroup>
          <optgroup label="自定义模板 ✨" v-if="customTemplates.length">
            <option v-for="t in customTemplates" :key="t.id" :value="'custom-' + t.id">{{ t.name }}</option>
          </optgroup>
        </select>
        <span class="t3" style="font-size:11px">自定义模板 {{ customTemplates.length }}/{{ templateMax }}</span>
        <button class="bt bt-o bt-s" :disabled="!input.trim()" @click="openSaveTemplate">保存为模板</button>
      </div>
      <div v-if="customTemplates.length" style="padding:6px 0;display:flex;gap:6px;flex-wrap:wrap">
        <span v-for="t in customTemplates" :key="t.id" style="display:inline-flex;align-items:center;gap:5px;font-size:11px;background:var(--bg);border:1px solid var(--b);border-radius:999px;padding:2px 10px;cursor:pointer" @click="fillCustom(t)">
          {{ t.name }}<a style="color:var(--r);font-weight:700" @click.stop="delTemplate(t)">×</a>
        </span>
      </div>
      <div class="chat-body" ref="chatBox">
        <div v-if="!messages.length" class="t3" style="text-align:center;margin-top:15%">
          <p style="font-size:14px;color:var(--t2)">Grape 运营 AI 助手</p>
          <p class="tip mt8">可以问：多少台设备在线？任务失败率？某个引擎的版本分布？……</p>
        </div>
        <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
          <div class="bubble">
            <template v-if="m.role === 'user'">{{ m.content }}</template>
            <template v-else>
              <div v-for="(part, j) in renderParts(m.content)" :key="j">
                <p v-if="part.type === 'text'" class="text">{{ part.text }}</p>
                <div v-else class="chart"></div>
              </div>
              <span v-if="m.streaming" class="cursor">▍</span>
            </template>
          </div>
        </div>
      </div>
      <div class="chat-in">
        <input v-model="input" placeholder="输入问题，Enter 发送" @keyup.enter="send" :disabled="busy" />
        <button class="bt bt-p" :disabled="busy || !input.trim()" @click="send">{{ busy ? '思考中...' : '发送' }}</button>
      </div>
    </div>

    <div v-if="showSaveTpl" class="modal">
      <div class="modal-box">
        <h4>保存为自定义模板</h4>
        <p class="t2" style="font-size:12px;margin:4px 0 10px">将当前输入存为模板，下次可直接选用</p>
        <input v-model="newTplName" placeholder="请输入模板名称（≤50字）" maxlength="50" style="width:100%;padding:8px 10px;border:1px solid var(--b);border-radius:8px;outline:none" />
        <div class="modal-actions">
          <button class="bt bt-o" @click="showSaveTpl = false">取消</button>
          <button class="bt bt-p" :disabled="!newTplName.trim()" @click="saveTemplate">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import * as echarts from 'echarts'
import { getToken } from '../utils/auth'
import http from '../api'

const route = useRoute()
const messages = ref([])
const input = ref('')
const busy = ref(false)
const chatBox = ref(null)
const selectedTemplate = ref('')
const customTemplates = ref([])
const templateMax = ref(50)
const showSaveTpl = ref(false)
const newTplName = ref('')
let sessionId = null

// 内置示例模板（VOD 同款：前端常量，按业务分类）
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

async function loadTemplates() {
  try {
    const { data } = await http.get('/ai/templates')
    customTemplates.value = data.templates || []
    templateMax.value = data.max || 50
  } catch (e) { /* 模板加载失败不阻塞聊天 */ }
}
function applyTemplate() {
  const v = selectedTemplate.value
  if (!v) return
  if (v.startsWith('custom-')) {
    const t = customTemplates.value.find(x => String(x.id) === v.slice(7))
    if (t) input.value = t.content
  } else if (BUILTIN_TEMPLATES[v]) {
    input.value = BUILTIN_TEMPLATES[v].content
  }
  selectedTemplate.value = ''
}
function fillCustom(t) { input.value = t.content }
function openSaveTemplate() { newTplName.value = ''; showSaveTpl.value = true }
async function saveTemplate() {
  try {
    await http.post('/ai/templates', { name: newTplName.value.trim(), content: input.value })
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

// 从历史页"继续对话"进入时带上会话 id
onMounted(() => {
  if (route.query.session) sessionId = String(route.query.session)
  loadTemplates()
})

function renderParts(content) {
  const parts = []
  const re = /```chart\s*([\s\S]*?)```/g
  let last = 0, m
  while ((m = re.exec(content)) !== null) {
    if (m.index > last) parts.push({ type: 'text', text: content.slice(last, m.index) })
    parts.push({ type: 'chart', option: m[1] })
    last = m.index + m[0].length
  }
  if (last < content.length) parts.push({ type: 'text', text: content.slice(last) })
  return parts
}

async function send() {
  const q = input.value.trim()
  if (!q || busy.value) return
  input.value = ''
  busy.value = true
  const userMsg = { role: 'user', content: q }
  const aiMsg = { role: 'assistant', content: '', streaming: true }
  messages.value.push(userMsg, aiMsg)
  await scrollDown()

  try {
    // 异步任务模式（参考 VOD）：submit 立即返回 taskId，再订阅流
    const { data } = await http.post('/ai/chat/submit', { query: q, sessionId })
    const resp = await fetch(`/api/admin/ai/chat/task/${data.taskId}/stream`, {
      headers: { Authorization: `Bearer ${getToken()}` }
    })
    if (!resp.ok || !resp.body) throw new Error(resp.status === 502 ? '服务暂时不可用（可能正在部署/重启），请稍后重试' : `HTTP ${resp.status}`)
    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop()
      for (const line of lines) {
        const data = line.replace(/^data:/, '').trim()
        if (!data) continue
        let ev
        try { ev = JSON.parse(data) } catch { continue }
        if (ev.type === 'token') { aiMsg.content += ev.data; await scrollDown() }
        else if (ev.type === 'session') { sessionId = ev.data }
        else if (ev.type === 'error') { aiMsg.content += `\n[错误] ${ev.data}` }
      }
    }
  } catch (e) {
    aiMsg.content += `\n[请求失败] ${e.message}`
  } finally {
    aiMsg.streaming = false
    busy.value = false
    await scrollDown()
    renderCharts()
  }
}

async function scrollDown() {
  await nextTick()
  if (chatBox.value) chatBox.value.scrollTop = chatBox.value.scrollHeight
}

function renderCharts() {
  const containers = chatBox.value?.querySelectorAll('.chart') || []
  containers.forEach((el, idx) => {
    const m = /```chart\s*([\s\S]*?)```/g
    let match = null, i = 0
    for (const msg of messages.value) {
      if (msg.role !== 'assistant') continue
      while ((match = m.exec(msg.content)) !== null) {
        if (i === idx) break
        i++
      }
    }
    if (!match) return
    try {
      const option = JSON.parse(match[1])
      const chart = echarts.init(el)
      chart.setOption(option)
    } catch (e) { el.textContent = '[图表解析失败]' }
  })
}
</script>

<style scoped>
.chat-wrap { display: flex; }
.chat-body { flex: 1; padding: 14px 4px; overflow-y: auto; font-size: 13px; }
.msg { max-width: 75%; padding: 10px 14px; border-radius: 10px; margin-bottom: 10px; line-height: 1.6; font-size: 12px; }
.msg.user { background: var(--a); color: #fff; border-radius: 10px 10px 10px 2px; }
.msg.assistant { background: var(--bg); border-radius: 10px 10px 2px 10px; margin-left: auto; }
.msg .text { margin: 0; white-space: pre-wrap; word-break: break-word; }
.msg .chart { height: 320px; margin: 8px 0; }
.cursor { color: var(--a); animation: blink 1s infinite; }
@keyframes blink { 50% { opacity: 0; } }
.chat-in { display: flex; gap: 8px; padding: 10px 0 0; border-top: 1px solid var(--b); margin-top: 8px; }
.chat-in input { flex: 1; padding: 8px 12px; border: 1px solid var(--b); border-radius: 7px; font-size: 12px; outline: none; }
.chat-in input:focus { border-color: var(--a); }
</style>
