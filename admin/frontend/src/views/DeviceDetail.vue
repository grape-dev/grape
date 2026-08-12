<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>设备详情</h1>
      <span class="badge" :style="badgeStyle(detail?.health)">{{ healthText(detail?.health) }}</span>
      <span class="t3" style="font-size:12px">{{ detail?.deviceName || detail?.name || '' }}</span>
      <button class="btn btn-sm" style="margin-left:auto" @click="$router.back()">← 返回列表</button>
    </div>

    <div class="dashboard" style="padding:20px 28px" v-if="detail">
      <!-- 设备信息 -->
      <div class="card">
        <div class="detail-grid">
          <div class="detail-item"><span class="t3">状态</span>{{ detail.status }}</div>
          <div class="detail-item"><span class="t3">范围</span>{{ scopeText(detail.scope) }}</div>
          <div class="detail-item"><span class="t3">归属</span>{{ detail.ownerName || '—' }}</div>
          <div class="detail-item"><span class="t3">最后心跳</span>{{ formatTime(detail.lastSeenAt) }}</div>
          <div class="detail-item"><span class="t3">IP</span>{{ detail.lastIp || '—' }}</div>
          <div class="detail-item"><span class="t3">创建时间</span>{{ formatTime(detail.createdAt) }}</div>
          <div class="detail-item"><span class="t3">引擎数</span>{{ detail.engines?.length ?? 0 }}</div>
          <div class="detail-item"><span class="t3">任务数</span>{{ detail.tasks?.totalElements ?? 0 }}</div>
        </div>
      </div>

      <!-- 引擎列表 -->
      <div class="card" style="margin-top:16px">
        <h5 style="margin:0 0 10px">关联引擎（{{ detail.engines?.length ?? 0 }}）</h5>
        <table class="vod-table" v-if="detail.engines?.length">
          <thead>
            <tr><th>实例名</th><th>类型</th><th>版本</th><th>二进制</th><th>健康</th><th>状态</th><th>CLI 路径</th><th>最后心跳</th></tr>
          </thead>
          <tbody>
            <tr v-for="e in detail.engines" :key="e.id">
              <td>{{ e.name }}</td>
              <td>{{ e.name }}</td>
              <td class="t3" style="max-width:200px">{{ e.version || '-' }}</td>
              <td class="t3">{{ e.binary || '-' }}</td>
              <td><span :class="['status-tag', healthClass(e.health)]">{{ healthText(e.health) }}</span></td>
              <td class="t2">{{ e.status }}</td>
              <td class="t3" style="max-width:180px">{{ e.cliPath || '-' }}</td>
              <td class="t3">{{ formatTime(e.lastSeenAt) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="t2" style="padding:12px;text-align:center">该设备暂无关联引擎</p>
      </div>

      <!-- 关联任务 -->
      <div class="card" style="margin-top:16px">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap">
          <h5 style="margin:0">关联任务（{{ detail.tasks?.totalElements ?? 0 }} 条）<span style="font-size:13px;color:#8896a7;font-weight:400" v-if="filterCount">（已筛选 {{ filterCount }} 条）</span></h5>
          <select v-model="selectedEngineId" class="page-size-select" style="max-width:260px;margin-left:auto" @change="onEngineChange">
            <option :value="null">全部引擎</option>
            <option v-for="e in detail.engines || []" :key="e.id" :value="e.id">{{ e.name }}</option>
          </select>
          <button v-if="hasAnyFilter" class="btn btn-sm" style="color:#e5484d;border-color:#f3b4b6" @click="clearFilters">清除筛选</button>
        </div>
        <div style="overflow-x:auto">
          <table class="vod-table">
            <thead><tr>
              <th style="width:70px">ID</th>
              <th class="filterable-th">
                <span class="th-content" @click="toggleFilter('agentType')">
                  类型 <span class="filter-arrow" :class="{ active: taskType }">▼</span>
                </span>
                <div class="col-filter-drop" v-if="filterDropdown === 'agentType'" @click.stop>
                  <div class="col-filter-opt" :class="{ active: !taskType }" @click="setFilter('agentType', '')">全部</div>
                  <div class="col-filter-opt" v-for="(cnt, v) in filterOptions('agentType')" :key="v" :class="{ active: taskType === v }" @click="setFilter('agentType', v)">{{ v || '(空)' }} ({{ cnt }})</div>
                </div>
              </th>
              <th class="filterable-th">
                <span class="th-content" @click="toggleFilter('skill')">
                  技能 <span class="filter-arrow" :class="{ active: taskSkill }">▼</span>
                </span>
                <div class="col-filter-drop" v-if="filterDropdown === 'skill'" @click.stop>
                  <div class="col-filter-opt" :class="{ active: !taskSkill }" @click="setFilter('skill', '')">全部</div>
                  <div class="col-filter-opt" v-for="(cnt, v) in filterOptions('skill')" :key="v" :class="{ active: taskSkill === v }" @click="setFilter('skill', v)">{{ v || '(空)' }} ({{ cnt }})</div>
                </div>
              </th>
              <th class="filterable-th">
                <span class="th-content" @click="toggleFilter('status')">
                  状态 <span class="filter-arrow" :class="{ active: taskStatus }">▼</span>
                </span>
                <div class="col-filter-drop" v-if="filterDropdown === 'status'" @click.stop>
                  <div class="col-filter-opt" :class="{ active: !taskStatus }" @click="setFilter('status', '')">全部</div>
                  <div class="col-filter-opt" v-for="(cnt, v) in filterOptions('status')" :key="v" :class="{ active: taskStatus === v }" @click="setFilter('status', v)">{{ v || '(空)' }} ({{ cnt }})</div>
                </div>
              </th>
              <th class="sortable-th" @click="toggleSort">
                创建时间
                <span class="sort-arrows">
                  <span class="sort-dir" :class="{ active: sortOrder === 'asc' }">▲</span>
                  <span class="sort-dir" :class="{ active: sortOrder === 'desc' }">▼</span>
                </span>
              </th>
            </tr></thead>
            <tbody>
              <tr v-for="t in detail.tasks?.content || []" :key="t.id">
                <td class="t3">{{ t.id }}</td>
                <td>{{ t.agentType }}</td>
                <td>{{ t.skill }}</td>
                <td><span class="status-tag default">{{ t.status }}</span></td>
                <td class="t3">{{ t.createdAt }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="detail.tasks?.totalElements > 0" class="pagination-bar" style="border-top:none">
          <span class="page-info">第 <strong>{{ taskPage + 1 }}</strong> / {{ detail.tasks?.totalPages ?? 1 }} 页（共 {{ detail.tasks?.totalElements ?? 0 }} 条）</span>
          <select v-model="taskSize" class="page-size-select" @change="taskPage = 0; load()">
            <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}条/页</option>
          </select>
          <button class="page-btn" :disabled="taskPage <= 0" @click="taskPage--; load()">上一页</button>
          <button class="page-btn" :disabled="taskPage >= (detail.tasks?.totalPages ?? 1) - 1" @click="taskPage++; load()">下一页</button>
        </div>
      </div>
    </div>

    <p v-else class="t2" style="padding:40px;text-align:center">加载中…</p>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import http from '../api'

const route = useRoute()
const detail = ref(null)
const selectedEngineId = ref(null)
const taskPage = ref(0)
const taskSize = ref(10)
const sizeOptions = [10, 20, 50]
// 列头筛选/排序状态（对齐 VOD 分析结果报表）
const taskStatus = ref('')
const taskSkill = ref('')
const taskType = ref('')
const sortOrder = ref('desc')
const filterDropdown = ref('')
const filterCount = ref(0)
const hasAnyFilter = computed(() => !!(taskStatus.value || taskSkill.value || taskType.value))

const BADGE = {
  online: { color: 'var(--success)', bg: '#dcfce7', label: '在线' },
  recently_lost: { color: '#ea5a0c', bg: '#fef3c7', label: '刚失联' },
  offline: { color: 'var(--text-tertiary)', bg: '#eef0f3', label: '离线' },
  about_to_gc: { color: 'var(--danger)', bg: '#fee2e2', label: '待回收' },
  revoked: { color: 'var(--text-tertiary)', bg: '#eef0f3', label: '已吊销' }
}
const SCOPE_TEXT = { global: '全局', repo: '仓库', user: '个人' }
function scopeText(s) { return SCOPE_TEXT[s] || s || '—' }
function healthText(h) { return (BADGE[h] || { label: h }).label }
function healthClass(h) { return { online: 'success', recently_lost: 'warning', offline: 'default', about_to_gc: 'danger', revoked: 'default' }[h] || 'default' }
function badgeStyle(h) {
  const b = BADGE[h] || { color: 'var(--text-tertiary)', bg: '#eef0f3' }
  return { display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '11px', fontWeight: 500, color: b.color, background: b.bg, padding: '2px 10px', borderRadius: '10px', whiteSpace: 'nowrap' }
}
function formatTime(ts) {
  if (!ts) return '—'
  const time = new Date(ts.replace(' ', 'T')).getTime()
  if (isNaN(time)) return ts
  const diff = Date.now() - time
  if (diff < 60000) return '刚刚'
  if (diff < 3600000) return Math.floor(diff / 60000) + '分钟前'
  if (diff < 86400000) return Math.floor(diff / 3600000) + '小时前'
  return ts.slice(0, 16)
}

async function load() {
  const params = { taskPage: taskPage.value, taskSize: taskSize.value, sortOrder: sortOrder.value }
  if (selectedEngineId.value) params.engineId = selectedEngineId.value
  if (taskStatus.value) params.taskStatus = taskStatus.value
  if (taskSkill.value) params.taskSkill = taskSkill.value
  if (taskType.value) params.taskType = taskType.value
  const { data } = await http.get(`/devices/${route.params.id}`, { params })
  detail.value = data
  // 「已筛选 N 条」：有列头筛选时显示过滤后总数，无筛选不显示（对齐 VOD）
  filterCount.value = (taskStatus.value || taskSkill.value || taskType.value) ? (data.tasks?.totalElements ?? 0) : 0
}
function onEngineChange() {
  taskPage.value = 0
  load()
}
// 列头筛选（对齐 VOD）：展开下拉/切换筛选/清空
function toggleFilter(col) {
  filterDropdown.value = filterDropdown.value === col ? '' : col
}
function setFilter(col, v) {
  if (col === 'status') taskStatus.value = v
  else if (col === 'skill') taskSkill.value = v
  else if (col === 'agentType') taskType.value = v
  filterDropdown.value = ''
  taskPage.value = 0
  load()
}
function clearFilters() {
  taskStatus.value = ''
  taskSkill.value = ''
  taskType.value = ''
  taskPage.value = 0
  load()
}
// 排序切换：desc → asc → desc
function toggleSort() {
  sortOrder.value = sortOrder.value === 'desc' ? 'asc' : 'desc'
  taskPage.value = 0
  load()
}
function filterOptions(col) {
  return detail.value?.tasks?.filterOptions?.[col] || {}
}
load()
</script>

<style scoped>
.detail-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px 24px;
}
.detail-item {
  font-size: 13px;
  color: var(--text-primary);
}
.detail-item .t3 { display: block; font-size: 11px; margin-bottom: 2px; }
@media (max-width: 900px) {
  .detail-grid { grid-template-columns: repeat(2, 1fr); }
}

/* ===== 列头筛选/排序（对齐 VOD 分析结果报表） ===== */
.filterable-th, .sortable-th { position: relative; cursor: pointer; user-select: none; }
.th-content { display: inline-flex; align-items: center; gap: 4px; }
.filter-arrow { font-size: 9px; color: var(--text-tertiary); }
.filter-arrow.active { color: var(--primary); }
.sort-arrows { display: inline-flex; flex-direction: column; line-height: 1; margin-left: 3px; vertical-align: middle; }
.sort-dir { font-size: 8px; color: var(--text-tertiary); }
.sort-dir.active { color: var(--primary); }
.col-filter-drop {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  z-index: 50;
  min-width: 150px;
  max-height: 240px;
  overflow-y: auto;
  background: #fff;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.12);
  padding: 6px;
}
.col-filter-opt {
  padding: 6px 10px;
  font-size: 12px;
  color: var(--text-secondary);
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
}
.col-filter-opt:hover { background: #f5f7fa; }
.col-filter-opt.active { background: var(--primary-bg); color: var(--primary); font-weight: 500; }
</style>
