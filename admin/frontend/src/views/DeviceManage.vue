<template>
  <div class="cd">
    <div class="cd-h">
      <h2>设备列表 <span class="c">{{ devices.length }} 台</span></h2>
      <button class="bt bt-p" @click="openIssue">签发凭据</button>
    </div>
    <div class="tw">
      <table>
        <thead>
          <tr><th>设备</th><th>健康</th><th>状态</th><th>最后心跳</th><th>IP</th><th>引擎</th><th>任务</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="d in devices" :key="d.id">
            <td>{{ d.deviceName || d.name }}</td>
            <td><span :class="['tg', healthClass(d.health)]">{{ healthText(d.health) }}</span></td>
            <td class="t2">{{ d.status }}</td>
            <td class="t3">{{ d.lastSeenAt || '-' }}</td>
            <td class="t3">{{ d.lastIp || '-' }}</td>
            <td>{{ d.engineCount }}</td>
            <td>{{ d.taskCount }}</td>
            <td>
              <a class="bt bt-o bt-s" @click="openDetail(d)">详情</a>
              <a v-if="d.status === 'active'" class="bt bt-o bt-s" @click="revoke(d)">吊销</a>
              <a class="bt bt-o bt-s" @click="recycle(d)">回收</a>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-if="!devices.length" class="t3 mt12">暂无设备</p>
  </div>

  <div v-if="showIssue" class="modal">
    <div class="modal-box">
      <h4>签发设备凭据</h4>
      <input v-model="deviceName" placeholder="设备名（唯一，≤64字符）" />
      <div class="modal-actions">
        <button class="bt bt-o" @click="showIssue = false">取消</button>
        <button class="bt bt-p" @click="issue">签发</button>
      </div>
      <div v-if="issuedToken" class="cd mt12" style="border:1px dashed var(--y)">
        <p class="ty" style="font-weight:600">⚠ 明文仅显示一次，请立即保存</p>
        <code style="word-break:break-all;font-size:12px;display:block;margin:8px 0">{{ issuedToken }}</code>
        <p class="t3">将该 token 填入目标机器 /etc/grape/runtime.env 的 RUNTIME_TOKEN 并重启 runtime-agent</p>
        <button class="bt bt-p mt8" @click="issuedToken = ''">我已保存</button>
      </div>
    </div>
  </div>

  <div v-if="detail" class="modal wide">
    <div class="modal-box">
      <h4>设备详情：{{ detail.deviceName || detail.name }}</h4>
      <p class="t2">健康：{{ detail.health }} ｜ 最后心跳：{{ detail.lastSeenAt }} ｜ IP：{{ detail.lastIp }}</p>
      <h5>引擎（{{ detail.engines.length }}）</h5>
      <div class="tw">
        <table>
          <thead><tr><th>引擎</th><th>版本</th><th>健康</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="e in detail.engines" :key="e.id">
              <td>{{ e.name }}</td><td class="t3">{{ e.version || '-' }}</td>
              <td><span :class="['tg', healthClass(e.health)]">{{ e.health }}</span></td>
              <td class="t2">{{ e.status }}</td>
              <td><a class="bt bt-o bt-s" @click="deleteEngine(e)">删除</a></td>
            </tr>
          </tbody>
        </table>
      </div>
      <h5>最近任务（{{ detail.tasks.length }}）</h5>
      <div class="tw">
        <table>
          <thead><tr><th>ID</th><th>类型</th><th>技能</th><th>状态</th><th>创建时间</th></tr></thead>
          <tbody>
            <tr v-for="t in detail.tasks" :key="t.id">
              <td class="t3">{{ t.id }}</td><td>{{ t.agentType }}</td><td>{{ t.skill }}</td>
              <td><span class="tg tg-d">{{ t.status }}</span></td><td class="t3">{{ t.createdAt }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="modal-actions"><button class="bt bt-o" @click="detail = null">关闭</button></div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import http from '../api'

const devices = ref([])
const showIssue = ref(false)
const deviceName = ref('')
const issuedToken = ref('')
const detail = ref(null)

const HEALTH = { online: ['tg-g', '在线'], recently_lost: ['tg-s', '失联中'], offline: ['tg-d', '离线'], about_to_gc: ['tg-q', '待回收'], revoked: ['tg-i', '已吊销'] }
function healthText(h) { return (HEALTH[h] || ['tg-d', h])[1] }
function healthClass(h) { return (HEALTH[h] || ['tg-d', ''])[0] }

async function load() {
  const { data } = await http.get('/devices')
  devices.value = data
}
function openIssue() { showIssue.value = true; issuedToken.value = ''; deviceName.value = '' }
async function issue() {
  try {
    const { data } = await http.post('/credentials', { deviceName: deviceName.value })
    issuedToken.value = data.token
  } catch (e) { alert(e.response?.data?.message || '签发失败') }
}
async function revoke(d) {
  if (!confirm(`确认吊销设备 ${d.deviceName || d.name} 的凭据？其 worker 将立即失效（401）`)) return
  await http.post(`/devices/${d.id}/revoke`)
  load()
}
async function recycle(d) {
  const word = prompt(`设备回收将：吊销凭据 → 删除引擎记录 → 清理终态任务。不可逆！\n输入设备名 ${d.deviceName || d.name} 确认：`)
  if (word !== (d.deviceName || d.name)) { alert('确认词不匹配，已取消'); return }
  try {
    await http.post(`/devices/${d.id}/cleanup`)
    load()
  } catch (e) { alert(e.response?.data?.message || '回收失败') }
}
async function openDetail(d) {
  const { data } = await http.get(`/devices/${d.id}`)
  detail.value = data
}
async function deleteEngine(e) {
  if (!confirm(`确认删除引擎 ${e.name}（id=${e.id}）？`)) return
  try {
    await http.delete(`/engines/${e.id}`)
    openDetail(detail.value)
  } catch (err) { alert(err.response?.data?.message || '删除失败') }
}
onMounted(load)
</script>
