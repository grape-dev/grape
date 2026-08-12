<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>代码仓管理</h1>
      <span class="badge">{{ totalElements }} 个</span>
    </div>
    <div style="padding:20px 28px">
      <!-- 开发场景统计 -->
      <div class="stats">
        <div v-for="(cnt, s) in sceneStats" :key="s" class="stat-card">
          <div class="stat-label">{{ s }}</div>
          <div class="stat-value small">{{ cnt }}</div>
        </div>
      </div>

      <div class="toolbar">
        <input v-model="keyword" class="search-input" placeholder="搜索仓库名 / owner / repo_key" @keyup.enter="load" />
        <button class="btn" @click="load">查询</button>
        <button class="btn" @click="reset">重置</button>
      </div>

      <!-- 卡片网格 -->
      <div class="device-grid">
        <div v-for="r in repos" :key="r.repoKey" class="device-card repo-card">
          <div class="dc-head">
            <span class="repo-icon">📦</span>
            <span class="dc-name repo-name">{{ r.repoName || r.fullName }}</span>
            <span v-if="r.isAdmin === 1" class="status-tag success">管理员</span>
          </div>
          <div class="repo-key">{{ r.repoKey }}</div>
          <div class="dc-meta">
            <div><span class="t3" style="display:inline-block;min-width:60px">Owner</span> {{ r.repoOwner || '—' }}</div>
            <div><span class="t3" style="display:inline-block;min-width:60px">Webhook</span> {{ r.webhookId ? '#' + r.webhookId : '—' }}</div>
            <div>
              <span class="t3" style="display:inline-block;min-width:60px">场景</span>
              <span class="status-tag info" :title="sceneHint(r.devScene)">{{ r.devScene || 'default' }}</span>
            </div>
            <div><span class="t3" style="display:inline-block;min-width:60px">Bot</span> {{ r.botUsername || '—' }}</div>
            <div><span class="t3" style="display:inline-block;min-width:60px">注册时间</span> {{ r.createdAt || '—' }}</div>
            <div>
              <span class="t3" style="display:inline-block;min-width:60px">活跃度</span>
              <span :class="['status-tag', activityClass(r)]" :title="activityTitle(r)">
                {{ r.eventCount || 0 }} 个事件<template v-if="r.eventCount"> · 失败率 {{ failRate(r) }}%</template>
              </span>
            </div>
          </div>
          <div class="dc-actions">
            <a class="repo-link" :href="repoUrl(r)" target="_blank" rel="noopener">打开仓库 ↗</a>
          </div>
        </div>
      </div>

      <p v-if="!repos.length" class="t2" style="padding:16px;text-align:center">无匹配代码仓</p>

      <div v-if="totalElements > 0" class="pagination-bar">
        <span class="page-info">第 <strong>{{ page + 1 }}</strong> / {{ totalPages }} 页（共 {{ totalElements }} 个）</span>
        <select v-model="size" class="page-size-select" @change="page = 0; load()">
          <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}条/页</option>
        </select>
        <button class="page-btn" :disabled="page <= 0" @click="page--; load()">上一页</button>
        <button class="page-btn" :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import http from '../api'

const repos = ref([])
const sceneStats = ref({})
const totalElements = ref(0)
const totalPages = ref(0)
const page = ref(0)
const size = ref(10)
const sizeOptions = [10, 20, 50]
const keyword = ref('')

async function load() {
  const { data } = await http.get('/repos', { params: { keyword: keyword.value, page: page.value, size: size.value } })
  repos.value = data.content
  sceneStats.value = data.sceneStats || {}
  totalElements.value = data.totalElements
  totalPages.value = data.totalPages
}
function reset() { keyword.value = ''; load() }

const SCENE_HINTS = {
  default: '默认开发场景',
  main: '主场景',
  quick: '快速迭代场景',
  stable: '稳定发布场景'
}

function sceneHint(scene) { return SCENE_HINTS[scene] || '开发场景（agent-skill-mapping 配置）' }
function repoUrl(r) {
  const full = r.fullName || (r.repoKey || '').replace(/^gitcode:/, '')
  return 'https://gitcode.com/' + full
}
function failRate(r) {
  if (!r.eventCount) return 0
  return Math.round((r.failedCount || 0) / r.eventCount * 100)
}
function activityClass(r) {
  if (!r.eventCount || r.eventCount === 0) return 'danger'
  if (failRate(r) > 10) return 'warning'
  if (!r.lastEventAt) return 'warning'
  const days = (Date.now() - new Date(r.lastEventAt.replace(' ', 'T')).getTime()) / 86400000
  if (days > 7) return 'warning'
  return 'success'
}
function activityTitle(r) {
  const parts = []
  if (r.lastEventAt) parts.push('最近事件: ' + r.lastEventAt)
  else parts.push('托管后暂无事件（webhook 可能异常）')
  if (r.eventCount && r.failedCount) parts.push('失败 ' + r.failedCount + '/' + r.eventCount)
  return parts.join('\n')
}

onMounted(load)
</script>

<style scoped>
.search-input {
  padding: 7px 12px;
  border: 1px solid var(--border, #e2e6ea);
  border-radius: 6px;
  font-size: 13px;
  min-width: 240px;
  background: #fff;
}
.repo-card { cursor: default; }
.repo-icon { font-size: 20px; }
.repo-name {
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.repo-key {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 11.5px;
  color: var(--text-tertiary, #8a919c);
  background: #f4f6f8;
  padding: 2px 8px;
  border-radius: 4px;
  display: inline-block;
  margin-bottom: 8px;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.repo-link {
  margin-left: auto;
  font-size: 12px;
  color: var(--primary, #4f6ef2);
  text-decoration: none;
}
.repo-link:hover { text-decoration: underline; }
</style>
