<template>
  <div class="cd">
    <div class="cd-h">
      <h2>设备管理 <span class="c">{{ total }} 台</span></h2>
      <div class="flex gap8">
        <button class="bt bt-o" :style="view === 'list' ? activeTab : {}" @click="view = 'list'">列表</button>
        <button class="bt bt-o" :style="view === 'card' ? activeTab : {}" @click="view = 'card'">卡片</button>
      </div>
    </div>
    <div class="toolbar">
      <select v-model="status">
        <option value="">全部状态</option>
        <option value="active">active</option>
        <option value="revoked">revoked</option>
      </select>
      <input v-model="name" placeholder="设备名搜索" style="width:200px" @keyup.enter="search" />
      <button class="bt bt-p" @click="search">查询</button>
      <button class="bt bt-o" @click="reset">重置</button>
    </div>

    <!-- 列表模式 -->
    <div v-if="view === 'list'" class="tw">
      <table>
        <thead>
          <tr><th>设备名</th><th>状态</th><th>健康</th><th>引擎</th><th>任务</th><th>最后心跳</th><th>IP</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="d in devices" :key="d.id">
            <td>{{ d.deviceName || d.name }}</td>
            <td><span :class="['tg', d.status === 'active' ? 'tg-g' : 'tg-d']">{{ d.status }}</span></td>
            <td><span :class="['tg', healthClass(d.health)]">{{ healthText(d.health) }}</span></td>
            <td>{{ d.engineCount }}</td>
            <td>{{ d.taskCount }}</td>
            <td class="t3">{{ d.lastSeenAt || '-' }}</td>
            <td class="t3">{{ d.lastIp || '-' }}</td>
            <td>
              <a class="bt bt-o bt-s" @click="openDetail(d)">详情</a>
              <a v-if="d.status === 'active'" class="bt bt-o bt-s" @click="revoke(d)">吊销</a>
              <a class="bt bt-o bt-s" @click="recycle(d)">回收</a>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 卡片模式（对齐 Grape 设备卡片） -->
    <div v-else style="display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px">
      <div v-for="d in devices" :key="d.id"
           style="background:var(--c);border:1px solid var(--b);border-radius:var(--r2);padding:12px 14px;box-shadow:var(--sh)">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
          <span :style="badgeStyle(d.health)">
            <span :style="dotStyle(d.health)"></span>{{ healthText(d.health) }}
          </span>
          <span style="font-weight:600;font-size:14px">
            {{ d.deviceName || d.name }}
            <span style="font-weight:400;color:var(--t3);font-size:12px">{{ d.status }}</span>
          </span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px;color:var(--t2)">
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">引擎</span> {{ d.engineCount }} 个</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">任务</span> {{ d.taskCount }} 个</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">心跳</span> {{ d.lastSeenAt || '—' }}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">IP</span> {{ d.lastIp || '—' }}</div>
        </div>
        <div style="display:flex;justify-content:flex-end;gap:6px;padding-top:10px;border-top:1px solid var(--b);margin-top:10px">
          <a class="bt bt-o bt-s" @click="openDetail(d)">详情</a>
          <a v-if="d.status === 'active'" class="bt bt-o bt-s" @click="revoke(d)">吊销</a>
          <a class="bt bt-o bt-s" @click="recycle(d)">回收</a>
        </div>
      </div>
    </div>

    <p v-if="!devices.length" class="ts t2" style="padding:12px">无匹配设备</p>

    <div v-if="total > 0" class="pager">
      第 {{ page + 1 }} / {{ totalPages }} 页（共 {{ total }} 台）
      <select v-model="size" @change="page = 0; load()">
        <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}</option>
      </select>
      <button class="bt bt-o bt-s" :disabled="page <= 0" @click="page--; load()">上一页</button>
      <button class="bt bt-o bt-s" :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
    </div>
  </div>

  <div v-if="detail" class="modal wide">
    <div class="modal-box">
      <h4>设备详情：{{ detail.deviceName || detail.name }}</h4>
      <p class="t2">健康：{{ detail.health }} ｜ 最后心跳：{{ detail.lastSeenAt }} ｜ IP：{{ detail.lastIp }}</p>
      <div style="margin:14px 0 4px">
        <select v-model="selectedEngineId" @change="onEngineChange" style="padding:3px 8px;font-size:11px;border:1px solid var(--b);border-radius:6px;background:var(--bg);outline:none;cursor:pointer;max-width:260px">
          <option :value="null">全部引擎</option>
          <option v-for="e in detail.engines || []" :key="e.id" :value="e.id">{{ e.name }}</option>
        </select>
      </div>
      <h5>关联任务（{{ detail.tasks?.totalElements ?? 0 }} 条）</h5>
      <div class="tw">
        <table>
          <thead><tr><th>ID</th><th>类型</th><th>技能</th><th>状态</th><th>创建时间</th></tr></thead>
          <tbody>
            <tr v-for="t in detail.tasks?.content || []" :key="t.id">
              <td class="t3">{{ t.id }}</td>
              <td>{{ t.agentType }}</td>
              <td>{{ t.skill }}</td>
              <td><span class="tg tg-d">{{ t.status }}</span></td>
              <td class="t3">{{ t.createdAt }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="detail.tasks?.totalElements > 0" class="pager">
        第 {{ taskPage + 1 }} / {{ detail.tasks?.totalPages ?? 1 }} 页（共 {{ detail.tasks?.totalElements ?? 0 }} 条）
        <select v-model="taskSize" @change="taskPage = 0; openDetail(detail)">
          <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}</option>
        </select>
        <button class="bt bt-o bt-s" :disabled="taskPage <= 0" @click="taskPage--; openDetail(detail)">上一页</button>
        <button class="bt bt-o bt-s" :disabled="taskPage >= (detail.tasks?.totalPages ?? 1) - 1" @click="taskPage++; openDetail(detail)">下一页</button>
      </div>
      <div class="modal-actions"><button class="bt bt-o" @click="detail = null">关闭</button></div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import http from '../api'

const devices = ref([])
const total = ref(0)
const totalPages = ref(0)
const page = ref(0)
const status = ref('')
const name = ref('')
const view = ref('list')
const detail = ref(null)
const selectedEngineId = ref(null)
const activeTab = { background: 'var(--a)', color: '#fff', borderColor: 'var(--a)' }
const sizeOptions = [5, 10, 20, 50]
const size = ref(5)
const taskPage = ref(0)
const taskSize = ref(10)

const BADGE = {
  online: { color: 'var(--g)', bg: '#dcfce7', label: '在线' },
  recently_lost: { color: 'var(--or)', bg: '#fef3c7', label: '刚失联' },
  offline: { color: 'var(--t3)', bg: '#eef0f3', label: '离线' },
  about_to_gc: { color: 'var(--r)', bg: '#fee2e2', label: '待回收' },
  revoked: { color: 'var(--t3)', bg: '#eef0f3', label: '已吊销' }
}
function healthText(h) { return (BADGE[h] || { label: h }).label }
function healthClass(h) { return { online: 'tg-g', recently_lost: 'tg-s', offline: 'tg-d', about_to_gc: 'tg-i', revoked: 'tg-d' }[h] || 'tg-d' }
function badgeStyle(h) {
  const b = BADGE[h] || { color: 'var(--t3)', bg: '#eef0f3' }
  return { display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '12px', fontWeight: 600, color: b.color, background: b.bg, padding: '3px 10px', borderRadius: '999px', whiteSpace: 'nowrap' }
}
function dotStyle(h) {
  const b = BADGE[h] || { color: 'var(--t3)' }
  return { width: '7px', height: '7px', borderRadius: '50%', background: b.color, display: 'inline-block' }
}

async function load() {
  const { data } = await http.get('/devices', { params: { status: status.value, name: name.value, page: page.value, size: size.value } })
  devices.value = data.content
  total.value = data.totalElements
  totalPages.value = data.totalPages
}
function search() { page.value = 0; load() }
function reset() { status.value = ''; name.value = ''; page.value = 0; load() }

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
  const isNewDevice = d !== detail.value
  if (isNewDevice) {
    selectedEngineId.value = null
    taskPage.value = 0
  }
  const params = { taskPage: taskPage.value, taskSize: taskSize.value }
  if (selectedEngineId.value) params.engineId = selectedEngineId.value
  const { data } = await http.get(`/devices/${d.id}`, { params })
  detail.value = data
}
function onEngineChange() {
  taskPage.value = 0
  openDetail(detail.value)
}
onMounted(load)
</script>
