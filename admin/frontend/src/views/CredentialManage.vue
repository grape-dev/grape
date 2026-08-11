<template>
  <div class="cd">
    <div class="cd-h">
      <h2>凭据管理 <span class="c">{{ credentials.length }} 条</span></h2>
      <div class="flex gap8">
        <button class="bt bt-p" @click="openIssue">签发凭据</button>
        <button class="bt bt-o" :style="view === 'list' ? activeTab : {}" @click="view = 'list'">列表</button>
        <button class="bt bt-o" :style="view === 'card' ? activeTab : {}" @click="view = 'card'">卡片</button>
      </div>
    </div>
    <div class="toolbar">
      <select v-model="scope">
        <option value="">全部范围</option>
        <option value="global">全局</option>
        <option value="repo">仓库</option>
        <option value="user">用户</option>
      </select>
      <input v-model="owner" placeholder="归属用户（GitCode 用户名）" style="width:200px" @keyup.enter="load" />
      <button class="bt bt-p" @click="load">查询</button>
      <button class="bt bt-o" @click="reset">重置</button>
    </div>

    <!-- 列表模式 -->
    <div v-if="view === 'list'" class="tw">
      <table>
        <thead>
          <tr><th>设备名</th><th>归属用户</th><th>范围</th><th>状态</th><th>健康</th><th>引擎</th><th>最后心跳</th><th>IP</th><th>创建时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="c in credentials" :key="c.id">
            <td>{{ c.name }}</td>
            <td>{{ c.ownerName || '-' }}</td>
            <td><span class="tg tg-c">{{ scopeText(c.scope) }}</span></td>
            <td><span :class="['tg', c.status === 'active' ? 'tg-g' : 'tg-d']">{{ c.status }}</span></td>
            <td><span :class="['tg', healthClass(c.health)]">{{ healthText(c.health) }}</span></td>
            <td>{{ c.engineCount }}</td>
            <td class="t3">{{ c.lastSeenAt || '-' }}</td>
            <td class="t3">{{ c.lastIp || '-' }}</td>
            <td class="t3">{{ c.createdAt || '-' }}</td>
            <td>
              <a v-if="c.status === 'active'" class="bt bt-o bt-s" @click="revoke(c)">吊销</a>
              <span v-else class="t3">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 卡片模式（对齐 Grape 设备卡片） -->
    <div v-else style="display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px">
      <div v-for="c in credentials" :key="c.id"
           style="background:var(--c);border:1px solid var(--b);border-radius:var(--r2);padding:12px 14px;box-shadow:var(--sh)">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
          <span :style="badgeStyle(c.health)">
            <span :style="dotStyle(c.health)"></span>{{ healthText(c.health) }}
          </span>
          <span style="font-weight:600;font-size:14px">
            {{ c.name }}
            <span style="font-weight:400;color:var(--t3);font-size:12px">
              <span class="tg tg-c" style="font-size:11px;padding:1px 6px">{{ scopeText(c.scope) }}</span><span v-if="c.status !== 'active'" style="color:var(--t3)"> · {{ c.status }}</span>
            </span>
          </span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px;color:var(--t2)">
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">归属</span> {{ c.ownerName || '—' }}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">引擎</span> {{ c.engineCount }} 个</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">心跳</span> {{ c.lastSeenAt || '—' }}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">IP</span> {{ c.lastIp || '—' }}</div>
          <div style="grid-column:1 / -1"><span style="color:var(--t3)">创建</span> {{ c.createdAt || '—' }}</div>
        </div>
        <div style="display:flex;justify-content:flex-end;padding-top:10px;border-top:1px solid var(--b);margin-top:10px">
          <a v-if="c.status === 'active'" class="bt bt-o bt-s" @click="revoke(c)">吊销</a>
          <span v-else class="t3" style="font-size:11px">已吊销</span>
        </div>
      </div>
    </div>
    <p v-if="!credentials.length" class="ts t2" style="padding:12px">无匹配凭据</p>
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
</template>

<script setup>
import { onMounted, ref } from 'vue'
import http from '../api'

const credentials = ref([])
const view = ref('list')
const scope = ref('')
const owner = ref('')
const showIssue = ref(false)
const deviceName = ref('')
const issuedToken = ref('')
const activeTab = { background: 'var(--a)', color: '#fff', borderColor: 'var(--a)' }

const SCOPE_NAMES = { global: '全局', repo: '仓库', user: '用户' }
function scopeText(s) { return SCOPE_NAMES[s] || s || '-' }

// 对齐 Grape HEALTH_BADGE 配色
const BADGE = {
  online: { color: 'var(--g)', bg: '#dcfce7', label: '在线' },
  recently_lost: { color: 'var(--or)', bg: '#fef3c7', label: '刚失联' },
  offline: { color: 'var(--t3)', bg: '#eef0f3', label: '离线' },
  about_to_gc: { color: 'var(--r)', bg: '#fee2e2', label: '待回收' },
  revoked: { color: 'var(--t3)', bg: '#eef0f3', label: '已吊销' }
}
function healthText(h) { return (BADGE[h] || { label: h }).label }
function healthClass(h) { const b = BADGE[h] || {}; return { online: 'tg-g', recently_lost: 'tg-s', offline: 'tg-d', about_to_gc: 'tg-i', revoked: 'tg-d' }[h] || 'tg-d' }
function badgeStyle(h) {
  const b = BADGE[h] || { color: 'var(--t3)', bg: '#eef0f3' }
  return { display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '12px', fontWeight: 600, color: b.color, background: b.bg, padding: '3px 10px', borderRadius: '999px', whiteSpace: 'nowrap' }
}
function dotStyle(h) {
  const b = BADGE[h] || { color: 'var(--t3)' }
  return { width: '7px', height: '7px', borderRadius: '50%', background: b.color, display: 'inline-block' }
}

async function load() {
  const { data } = await http.get('/credentials', { params: { scope: scope.value, owner: owner.value } })
  credentials.value = data
}
function reset() {
  scope.value = ''
  owner.value = ''
  load()
}
function openIssue() { showIssue.value = true; issuedToken.value = ''; deviceName.value = '' }
async function issue() {
  try {
    const { data } = await http.post('/credentials', { deviceName: deviceName.value })
    issuedToken.value = data.token
  } catch (e) { alert(e.response?.data?.message || '签发失败') }
}
async function revoke(c) {
  if (!confirm(`确认吊销凭据 ${c.name}（归属 ${c.ownerName || '-'}）？其 worker 将立即失效（401）`)) return
  try {
    await http.post(`/devices/${c.id}/revoke`)
    load()
  } catch (e) {
    alert(e.response?.data?.message || '吊销失败')
  }
}
onMounted(load)
</script>
