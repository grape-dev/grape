<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>模板代码仓</h1>
      <span class="badge">{{ total }} 个模板 · 累计 {{ totalUsage }} 次创建</span>
    </div>
    <div style="padding:20px 28px">
      <!-- 分类徽章行（scene_id 可能重复，用下标做唯一标识） -->
      <div class="dm-stats">
        <button class="dm-stat" :class="{ on: activeCategory < 0 }" @click="activeCategory = -1">全部 <strong>{{ total }}</strong></button>
        <button v-for="(c, idx) in categories" :key="idx" class="dm-stat" :class="{ on: activeCategory === idx }" @click="activeCategory = idx">
          {{ c.name }} <strong>{{ c.templates.length }}</strong>
        </button>
      </div>

      <!-- 工具栏：排序 + 视图切换 -->
      <div class="toolbar">
        <select v-model="sortBy" style="width:auto">
          <option value="default">默认顺序</option>
          <option value="usage-desc">创建次数从高到低</option>
          <option value="usage-asc">创建次数从低到高</option>
        </select>
        <div style="margin-left:auto;display:flex;gap:8px">
          <button class="btn" :style="view === 'list' ? activeTab : {}" @click="view = 'list'">列表</button>
          <button class="btn" :style="view === 'card' ? activeTab : {}" @click="view = 'card'">卡片</button>
        </div>
      </div>

      <!-- 列表模式 -->
      <div class="card" v-if="view === 'list'">
        <table class="vod-table">
          <thead>
            <tr><th>模板 ID</th><th>名称</th><th>描述</th><th>类别</th><th>创建次数</th><th>使用人数</th><th>默认仓库名</th></tr>
          </thead>
          <tbody>
            <tr v-for="t in sortedTemplates" :key="t.id">
              <td><code class="skill-name">{{ t.id }}</code></td>
              <td>{{ t.name || '—' }}</td>
              <td class="t3" style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ t.description || '—' }}</td>
              <td class="t3">{{ categoryName(t.id) }}</td>
              <td><span :class="['status-tag', t.usageCount > 0 ? 'success' : 'default']">{{ t.usageCount }}</span></td>
              <td>{{ t.usageUsers || 0 }}</td>
              <td class="t3">{{ t.defaultRepoName || t.id }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 卡片模式 -->
      <div v-else class="device-grid">
        <div v-for="t in sortedTemplates" :key="t.id" class="device-card tpl-card">
          <div class="dc-head">
            <span class="dc-name">
              <code class="skill-name">{{ t.id }}</code>
            </span>
            <span :class="['status-tag', t.usageCount > 0 ? 'success' : 'default']">{{ t.usageCount }} 次创建</span>
          </div>
          <div class="tpl-title">{{ t.name }}</div>
          <p class="tpl-desc">{{ t.description || '暂无描述' }}</p>
          <div class="dc-meta">
            <div><span class="t3">使用人数</span> <b>{{ t.usageUsers || 0 }}</b> 人</div>
            <div><span class="t3">默认仓库名</span> <code class="t3">{{ t.defaultRepoName || t.id }}</code></div>
          </div>
          <div v-if="t.usageRepos && t.usageRepos.length" class="tpl-users">
            <span class="t3">使用仓库</span>
            <a v-for="(u, i) in t.usageRepos.slice(0, 2)" :key="i" class="tpl-user-tag" :href="repoUrl(u)" target="_blank" rel="noopener" :title="`打开 ${u.owner}/${u.repoName}`">{{ u.owner }}/{{ u.repoName }}</a>
            <a v-if="t.usageRepos.length > 2" class="tpl-more" @click="openUsage(t)">查看全部 ({{ t.usageRepos.length }})</a>
          </div>
          <div v-else class="t3 tpl-users">暂无使用记录</div>
        </div>
      </div>

      <p v-if="!filteredTemplates.length" class="t2" style="padding:16px;text-align:center">无匹配模板</p>
    </div>

    <!-- 使用仓库详情弹窗（分页展示全部） -->
    <div v-if="usageModal" class="modal" @click.self="usageModal = null">
      <div class="modal-box" style="width:560px">
        <h4>模板「{{ usageModal.id }}」使用仓库</h4>
        <p class="t3" style="margin:0 0 12px">共 {{ usageModal.usageRepos.length }} 个仓库 · {{ usageModal.usageUsers }} 人使用</p>
        <div class="card" style="max-height:380px;overflow-y:auto">
          <table class="vod-table">
            <thead>
              <tr><th style="width:60px">#</th><th>仓库</th><th>Owner</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-for="(u, i) in usagePageRepos" :key="i">
                <td class="t3">{{ usagePage * usagePageSize + i + 1 }}</td>
                <td><code class="skill-name">{{ u.repoName }}</code></td>
                <td>{{ u.owner }}</td>
                <td><a class="tpl-more" :href="repoUrl(u)" target="_blank" rel="noopener">打开仓库 ↗</a></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div style="margin-top:12px;display:flex;gap:10px;align-items:center;justify-content:flex-end;flex-wrap:nowrap;font-size:13px;color:#64748b">
          <span class="page-info" style="flex-shrink:0;white-space:nowrap">第 <strong>{{ usagePage + 1 }}</strong> / {{ usageTotalPages }} 页</span>
          <select v-model="usagePageSize" class="page-size-select" style="width:auto;flex-shrink:0;margin-bottom:0" @change="usagePage = 0">
            <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}条/页</option>
          </select>
          <button class="page-btn" style="flex-shrink:0;white-space:nowrap" :disabled="usagePage <= 0" @click="usagePage--">上一页</button>
          <button class="page-btn" style="flex-shrink:0;white-space:nowrap" :disabled="usagePage >= usageTotalPages - 1" @click="usagePage++">下一页</button>
        </div>
        <div class="modal-actions">
          <button class="btn" @click="usageModal = null">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import http from '../api'

const categories = ref([])
const total = ref(0)
const totalUsage = ref(0)
const activeCategory = ref(-1)
const view = ref('card')
const activeTab = { background: 'var(--primary)', color: '#fff', borderColor: 'var(--primary)' }
const sortBy = ref('default')

const filteredTemplates = computed(() => {
  if (activeCategory.value < 0) return categories.value.flatMap(c => c.templates)
  return categories.value[activeCategory.value]?.templates || []
})

const sortedTemplates = computed(() => {
  const list = [...filteredTemplates.value]
  if (sortBy.value === 'usage-desc') list.sort((a, b) => (b.usageCount || 0) - (a.usageCount || 0))
  if (sortBy.value === 'usage-asc') list.sort((a, b) => (a.usageCount || 0) - (b.usageCount || 0))
  return list
})

function categoryName(tplId) {
  for (const c of categories.value) {
    if (c.templates.some(t => t.id === tplId)) return c.name
  }
  return '—'
}

function repoUrl(u) {
  return `https://gitcode.com/${u.owner}/${u.repoName}`
}

// 使用仓库详情弹窗：分页展示全部（仓库多时卡片保持紧凑）
const usageModal = ref(null)
const usagePage = ref(0)
const usagePageSize = ref(20)
const sizeOptions = [10, 20, 50]
const usageTotalPages = computed(() => Math.max(1, Math.ceil((usageModal.value?.usageRepos.length || 0) / usagePageSize.value)))
const usagePageRepos = computed(() => {
  if (!usageModal.value) return []
  const from = usagePage.value * usagePageSize.value
  return usageModal.value.usageRepos.slice(from, from + usagePageSize.value)
})
function openUsage(t) {
  usageModal.value = t
  usagePage.value = 0
}

async function load() {
  const { data } = await http.get('/templates')
  categories.value = data.categories || []
  total.value = data.total || 0
  totalUsage.value = data.totalUsage || 0
}

onMounted(load)
</script>

<style scoped>
.dm-stats { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
.dm-stat {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; padding: 3px 10px; border-radius: 12px;
  background: rgba(0,0,0,0.04); color: var(--text-secondary); border: 1px solid transparent;
  cursor: pointer; transition: all 0.15s;
}
.dm-stat strong { font-weight: 600; color: var(--text-primary); }
.dm-stat:hover { border-color: var(--border-color); }
.dm-stat.on { background: var(--primary-bg); color: var(--primary); border-color: var(--primary); }
.dm-stat.on strong { color: var(--primary); }
.tpl-title { font-size: 14px; font-weight: 600; margin: 4px 0 6px; }
.tpl-desc {
  font-size: 12px; color: var(--text-secondary); line-height: 1.6;
  min-height: 38px; max-height: 58px; overflow: hidden;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
}
.tpl-users { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.tpl-user-tag {
  font-size: 11px; padding: 1px 8px; border-radius: 999px;
  background: #eef3ff; color: #4f6ef2; white-space: nowrap;
  text-decoration: none; cursor: pointer;
}
.tpl-user-tag:hover { background: #dbe6ff; }
.tpl-more {
  font-size: 11px; color: var(--primary, #4f6ef2); cursor: pointer; text-decoration: none;
}
.tpl-more:hover { text-decoration: underline; }
</style>
