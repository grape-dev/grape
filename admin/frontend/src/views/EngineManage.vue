<template>
  <div>
    <!-- 页头：不同类型引擎总数 -->
    <div class="stats mb12">
      <div class="st"><span class="v">{{ total }}</span><span class="l">引擎总数</span></div>
      <div v-for="(cnt, t) in stats" :key="t" class="st">
        <span class="v">{{ cnt }}</span><span class="l">{{ t }}</span>
      </div>
    </div>

    <div class="cd">
      <div class="cd-h">
        <h2>引擎管理 <span class="c">{{ totalElements }} 条</span></h2>
        <div class="flex gap8">
          <button class="bt bt-o" :style="view === 'list' ? activeTab : {}" @click="view = 'list'">列表</button>
          <button class="bt bt-o" :style="view === 'card' ? activeTab : {}" @click="view = 'card'">卡片</button>
        </div>
      </div>
      <div class="toolbar">
        <select v-model="scope">
          <option value="">全部范围</option>
          <option value="global">全局</option>
          <option value="repo">仓库</option>
          <option value="user">个人</option>
        </select>
        <select v-model="type">
          <option value="">全部类型</option>
          <option v-for="t in typeOptions" :key="t" :value="t">{{ t }}</option>
        </select>
        <select v-model="device">
          <option value="">全部设备</option>
          <option v-for="d in deviceOptions" :key="d" :value="d">{{ d }}</option>
        </select>
        <select v-model="status">
          <option value="">全部状态</option>
          <option value="online">在线</option>
          <option value="recently_lost">刚失联</option>
          <option value="offline">离线</option>
          <option value="about_to_gc">待回收</option>
        </select>
        <button class="bt bt-p" @click="search">查询</button>
        <button class="bt bt-o" @click="reset">重置</button>
      </div>

      <!-- 列表模式 -->
      <div v-if="view === 'list'" class="tw">
        <table>
          <thead>
            <tr><th>实例名</th><th>类型</th><th>版本</th><th>设备</th><th>范围</th><th>健康</th><th>状态</th><th>最后心跳</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="e in engines" :key="e.id">
              <td>{{ e.displayName || e.name }}</td>
              <td>{{ e.name }}</td>
              <td class="t3">{{ e.version || '-' }}</td>
              <td>{{ e.device || '-' }}</td>
              <td>{{ scopeText(e.scope) }}</td>
              <td><span :class="['tg', healthClass(e.health)]">{{ healthText(e.health) }}</span></td>
              <td class="t2">{{ e.status }}</td>
              <td class="t3">{{ e.lastSeenAt || '-' }}</td>
              <td><a class="bt bt-o bt-s" @click="remove(e)">删除</a></td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 卡片模式（对齐 Grape 引擎卡片） -->
      <div v-else style="display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px">
        <div v-for="e in engines" :key="e.id"
             style="background:var(--c);border:1px solid var(--b);border-radius:var(--r2);padding:12px 14px;box-shadow:var(--sh)">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
            <span :style="badgeStyle(e.health)">
              <span :style="dotStyle(e.health)"></span>{{ healthText(e.health) }}
            </span>
            <span style="font-weight:600;font-size:14px">
              {{ e.displayName || e.name }}
              <span style="font-weight:400;color:var(--t3);font-size:12px">{{ e.name }} v{{ e.version || '' }}</span>
            </span>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px;color:var(--t2)">
            <div><span style="color:var(--t3);display:inline-block;min-width:46px">设备</span> {{ e.device || '—' }}</div>
            <div><span style="color:var(--t3);display:inline-block;min-width:46px">状态</span> {{ e.status }}</div>
            <div><span style="color:var(--t3);display:inline-block;min-width:46px">心跳</span> {{ e.lastSeenAt || '—' }}</div>
            <div><span style="color:var(--t3);display:inline-block;min-width:46px">归属/范围</span> {{ e.ownerId || '—' }} · {{ scopeText(e.scope) }}</div>
          </div>
          <div style="display:flex;justify-content:flex-end;padding-top:10px;border-top:1px solid var(--b);margin-top:10px">
            <a class="bt bt-o bt-s" @click="remove(e)">删除</a>
          </div>
        </div>
      </div>

      <p v-if="!engines.length" class="ts t2" style="padding:12px">无匹配引擎</p>

      <div v-if="totalPages > 1" class="pager">
        第 {{ page + 1 }} / {{ totalPages }} 页（共 {{ totalElements }} 条）
        <button class="bt bt-o bt-s" :disabled="page <= 0" @click="page--; load()">上一页</button>
        <button class="bt bt-o bt-s" :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import http from '../api'

const engines = ref([])
const stats = ref({})
const totalElements = ref(0)
const totalPages = ref(0)
const page = ref(0)
const type = ref('')
const scope = ref('')
const device = ref('')
const status = ref('')
const view = ref('list')
const deviceOptions = ref([])
const activeTab = { background: 'var(--a)', color: '#fff', borderColor: 'var(--a)' }

const BADGE = {
  online: { color: 'var(--g)', bg: '#dcfce7', label: '在线' },
  recently_lost: { color: 'var(--or)', bg: '#fef3c7', label: '刚失联' },
  offline: { color: 'var(--t3)', bg: '#eef0f3', label: '离线' },
  about_to_gc: { color: 'var(--r)', bg: '#fee2e2', label: '待回收' }
}
const SCOPE_TEXT = { global: '全局', repo: '仓库', user: '个人' }
function scopeText(s) { return SCOPE_TEXT[s] || s || '—' }
function healthText(h) { return (BADGE[h] || { label: h }).label }
function healthClass(h) { return { online: 'tg-g', recently_lost: 'tg-s', offline: 'tg-d', about_to_gc: 'tg-i' }[h] || 'tg-d' }
function badgeStyle(h) {
  const b = BADGE[h] || { color: 'var(--t3)', bg: '#eef0f3' }
  return { display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '12px', fontWeight: 600, color: b.color, background: b.bg, padding: '3px 10px', borderRadius: '999px', whiteSpace: 'nowrap' }
}
function dotStyle(h) {
  const b = BADGE[h] || { color: 'var(--t3)' }
  return { width: '7px', height: '7px', borderRadius: '50%', background: b.color, display: 'inline-block' }
}

const typeOptions = computed(() => Object.keys(stats.value))
const total = computed(() => Object.values(stats.value).reduce((s, n) => s + n, 0))

async function loadDevices() {
  const { data } = await http.get('/devices', { params: { size: 100 } })
  deviceOptions.value = [...new Set(data.content.map(d => d.deviceName || d.name))]
}
async function load() {
  const { data } = await http.get('/engines', { params: { scope: scope.value, type: type.value, device: device.value, status: status.value, page: page.value, size: 10 } })
  engines.value = data.content
  stats.value = data.stats
  totalElements.value = data.totalElements
  totalPages.value = data.totalPages
}
function search() { page.value = 0; load() }
function reset() { scope.value = ''; type.value = ''; device.value = ''; status.value = ''; page.value = 0; load() }
async function remove(e) {
  if (!confirm(`确认删除引擎 ${e.displayName || e.name}（id=${e.id}）？`)) return
  try {
    await http.delete(`/engines/${e.id}`)
    load()
  } catch (err) { alert(err.response?.data?.message || '删除失败') }
}
onMounted(() => { load(); loadDevices() })
</script>
