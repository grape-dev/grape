<template>
  <div class="assistant">
    <div class="chat" ref="chatBox">
      <div v-if="!messages.length" class="empty">
        <p>Grape 运营 AI 助手</p>
        <p class="tip">可以问：多少台设备在线？任务失败率？某个引擎的版本分布？……</p>
      </div>
      <div v-for="(m, i) in messages" :key="i" :class="['msg', m.role]">
        <div class="bubble">
          <template v-if="m.role === 'user'">{{ m.content }}</template>
          <template v-else>
            <div v-for="(part, j) in renderParts(m.content)" :key="j">
              <p v-if="part.type === 'text'" class="text">{{ part.text }}</p>
              <div v-else class="chart" ref="chartEls"></div>
            </div>
            <span v-if="m.streaming" class="cursor">▍</span>
          </template>
        </div>
      </div>
    </div>
    <div class="input-row">
      <input v-model="input" placeholder="输入问题，Enter 发送" @keyup.enter="send" :disabled="busy" />
      <button :disabled="busy || !input.trim()" @click="send">{{ busy ? '思考中...' : '发送' }}</button>
    </div>
  </div>
</template>

<script setup>
import { nextTick, ref } from 'vue'
import * as echarts from 'echarts'
import { getToken } from '../utils/auth'

const messages = ref([])
const input = ref('')
const busy = ref(false)
const chatBox = ref(null)
const chartEls = ref([])
let sessionId = null

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
    const resp = await fetch('/api/ai/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${getToken()}` },
      body: JSON.stringify({ query: q, sessionId })
    })
    if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`)
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
        else if (ev.type === 'done') { /* 结束 */ }
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
.assistant { display: flex; flex-direction: column; height: calc(100vh - 140px); background: #fff; border-radius: 6px; }
.chat { flex: 1; overflow-y: auto; padding: 20px; }
.empty { text-align: center; color: #999; margin-top: 15%; }
.empty .tip { font-size: 13px; margin-top: 6px; color: #c0c4cc; }
.msg { margin-bottom: 14px; display: flex; }
.msg.user { justify-content: flex-end; }
.msg.user .bubble { background: #409eff; color: #fff; }
.msg.assistant .bubble { background: #f4f6f9; }
.bubble { max-width: 78%; padding: 10px 14px; border-radius: 8px; font-size: 14px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
.text { margin: 0; }
.chart { height: 320px; margin: 8px 0; }
.cursor { color: #409eff; animation: blink 1s infinite; }
@keyframes blink { 50% { opacity: 0; } }
.input-row { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid #f0f2f5; }
.input-row input { flex: 1; padding: 10px 12px; border: 1px solid #dcdfe6; border-radius: 4px; }
.input-row button { padding: 10px 20px; background: #409eff; color: #fff; border: none; border-radius: 4px; cursor: pointer; }
</style>
