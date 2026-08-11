<template>
  <div class="page">
    <div class="toolbar">
      <span class="stat">共 {{ devices.length }} 台设备</span>
      <button class="primary" @click="openIssue">签发凭据</button>
    </div>
    <table class="tbl">
      <thead>
        <tr><th>设备</th><th>健康</th><th>状态</th><th>最后心跳</th><th>IP</th><th>引擎</th><th>任务</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="d in devices" :key="d.id">
          <td>{{ d.deviceName || d.name }}</td>
          <td><span :class="['badge', healthClass(d.health)]">{{ healthText(d.health) }}</span></td>
          <td>{{ d.status }}</td>
          <td>{{ d.lastSeenAt || '-' }}</td>
          <td>{{ d.lastIp || '-' }}</td>
          <td>{{ d.engineCount }}</td>
          <td>{{ d.taskCount }}</td>
          <td>
            <a @click="openDetail(d)">详情</a>
            <a v-if="d.status === 'active'" @click="revoke(d)">吊销</a>
            <a @click="recycle(d)">回收</a>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showIssue" class="modal">
      <div class="modal-box">
        <h4>签发设备凭据</h4>
        <input v-model="deviceName" placeholder="设备名（唯一，≤64字符）" />
        <div class="modal-actions">
          <button @click="showIssue = false">取消</button>
          <button class="primary" @click="issue">签发</button>
        </div>
        <div v-if="issuedToken" class="token-box">
          <p class="warn">⚠ 明文仅显示一次，请立即保存</p>
          <code>{{ issuedToken }}</code>
          <p class="tip">将该 token 填入目标机器 /etc/grape/runtime.env 的 RUNTIME_TOKEN 并重启 runtime-agent</p>
          <button class="primary" @click="issuedToken = ''">我已保存</button>
        </div>
      </div>
    </div>

    <div v-if="detail" class="modal wide">
      <div class="modal-box">
        <h4>设备详情：{{ detail.deviceName || detail.name }}</h4>
        <p>健康：{{ detail.health }} ｜ 最后心跳：{{ detail.lastSeenAt }} ｜ IP：{{ detail.lastIp }}</p>
        <h5>引擎（{{ detail.engines.length }}）</h5>
        <table class="tbl small">
          <thead><tr><th>引擎</th><th>版本</th><th>健康</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="e in detail.engines" :key="e.id">
              <td>{{ e.name }}</td><td>{{ e.version || '-' }}</td>
              <td>{{ e.health }}</td><td>{{ e.status }}</td>
              <td><a @click="deleteEngine(e)">删除</a></td>
            </tr>
          </tbody>
        </table>
        <h5>最近任务（{{ detail.tasks.length }}）</h5>
        <table class="tbl small">
          <thead><tr><th>ID</th><th>类型</th><th>技能</th><th>状态</th><th>创建时间</th></tr></thead>
          <tbody>
            <tr v-for="t in detail.tasks" :key="t.id">
              <td>{{ t.id }}</td><td>{{ t.agentType }}</td><td>{{ t.skill }}</td>
              <td>{{ t.status }}</td><td>{{ t.createdAt }}</td>
            </tr>
          </tbody>
        </table>
        <div class="modal-actions"><button @click="detail = null">关闭</button></div>
      </div>
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

const HEALTH = { online: ['on', '在线'], recently_lost: ['warn', '失联中'], offline: ['off', '离线'], about_to_gc: ['gc', '待回收'], revoked: ['off', '已吊销'] }
function healthText(h) { return (HEALTH[h] || ['', h])[1] }
function healthClass(h) { return (HEALTH[h] || ['', ''])[0] }

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

<style scoped>
.toolbar { margin-bottom: 12px; display: flex; gap: 12px; align-items: center; }
.stat { color: #666; font-size: 14px; }
button { padding: 6px 14px; border: 1px solid #dcdfe6; background: #fff; border-radius: 4px; cursor: pointer; }
button.primary { background: #409eff; color: #fff; border-color: #409eff; }
.tbl { width: 100%; border-collapse: collapse; background: #fff; border-radius: 6px; overflow: hidden; }
.tbl th, .tbl td { padding: 10px 12px; border-bottom: 1px solid #f0f2f5; text-align: left; font-size: 14px; }
.tbl.small { margin: 8px 0; }
.tbl.small th, .tbl.small td { font-size: 13px; padding: 6px 10px; }
.tbl a { color: #409eff; cursor: pointer; margin-right: 8px; }
.badge { padding: 2px 8px; border-radius: 10px; font-size: 12px; }
.badge.on { background: #e1f3d8; color: #67c23a; }
.badge.warn { background: #fdf6ec; color: #e6a23c; }
.badge.off { background: #fde2e2; color: #f56c6c; }
.badge.gc { background: #f0f2f5; color: #909399; }
.modal { position: fixed; inset: 0; background: rgba(0,0,0,.4); display: flex; align-items: center; justify-content: center; }
.modal.wide .modal-box { width: 760px; max-height: 80vh; overflow: auto; }
.modal-box { background: #fff; border-radius: 8px; padding: 20px; width: 420px; }
.modal-box h4 { margin-bottom: 12px; }
.modal-box h5 { margin: 14px 0 6px; }
.modal-box input { width: 100%; padding: 8px 10px; margin-bottom: 10px; border: 1px solid #dcdfe6; border-radius: 4px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
.token-box { margin-top: 14px; border: 1px dashed #e6a23c; background: #fdf6ec; border-radius: 6px; padding: 12px; }
.token-box code { word-break: break-all; font-size: 13px; display: block; margin: 8px 0; }
.warn { color: #e6a23c; font-size: 13px; font-weight: 600; }
.tip { color: #999; font-size: 12px; margin: 6px 0; }
</style>
