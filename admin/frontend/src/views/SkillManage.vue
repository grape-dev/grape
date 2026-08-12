<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>Skill 管理</h1>
      <span class="badge">{{ totalElements }} 条</span>
    </div>
    <div style="padding:20px 28px">
      <!-- 技能范围统计 -->
      <div class="stats">
        <div v-for="(cnt, s) in stats" :key="s" class="stat-card">
          <div class="stat-label">{{ SCOPE_TEXT[s] || s || '未知' }}</div>
          <div class="stat-value small">{{ cnt }}</div>
        </div>
      </div>

      <div class="toolbar">
        <input v-model="keyword" class="search-input" placeholder="搜索技能名 / 显示名 / 描述" @keyup.enter="search" />
        <select v-model="agentType" @change="search">
          <option value="">全部 Agent</option>
          <option v-for="t in agentTypeOptions" :key="t" :value="t">{{ t }}</option>
        </select>
        <select v-model="scope" @change="search">
          <option value="">全部范围</option>
          <option value="official">官方</option>
          <option value="repo">仓库</option>
          <option value="user">个人</option>
        </select>
        <button class="btn" @click="search">查询</button>
        <button class="btn" @click="reset">重置</button>
      </div>

      <!-- 卡片网格 -->
      <div class="device-grid">
        <div v-for="s in skills" :key="s.id" class="device-card skill-card" @click="openDetail(s)">
          <div class="dc-head">
            <span class="dc-name">
              <code class="skill-name">{{ s.skillName }}</code>
              <span :class="['status-tag', s.isActive === 1 ? 'success' : 'danger']" style="font-size:11px;padding:1px 8px">
                {{ s.isActive === 1 ? '启用' : '停用' }}
              </span>
            </span>
            <span class="status-tag info">{{ scopeText(s.scope) }}</span>
          </div>
          <div class="skill-title">{{ s.displayName || s.skillName }}</div>
          <p class="skill-desc">{{ s.description || '暂无描述' }}</p>
          <div class="dc-meta">
            <div><span class="t3" style="display:inline-block;min-width:52px">Agent</span> {{ s.agentType || '—' }}</div>
            <div><span class="t3" style="display:inline-block;min-width:52px">更新</span> {{ s.updatedAt || '—' }}</div>
          </div>
          <div class="dc-actions" style="justify-content:flex-end">
            <span class="t3 skill-more">点击查看详情 →</span>
          </div>
        </div>
      </div>

      <p v-if="!skills.length" class="t2" style="padding:16px;text-align:center">无匹配技能</p>

      <div v-if="totalPages > 1" class="pagination-bar">
        <span class="page-info">第 <strong>{{ page + 1 }}</strong> / {{ totalPages }} 页（共 {{ totalElements }} 条）</span>
        <button class="page-btn" :disabled="page <= 0" @click="page--; load()">上一页</button>
        <button class="page-btn" :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
      </div>
    </div>

    <!-- 详情弹窗 -->
    <div v-if="detail" class="modal wide" @click.self="detail = null">
      <div class="modal-box">
        <h4>Skill 详情</h4>
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
          <code class="skill-name skill-name-lg">{{ detail.skillName }}</code>
          <span :class="['status-tag', detail.isActive === 1 ? 'success' : 'danger']">{{ detail.isActive === 1 ? '启用' : '停用' }}</span>
          <span class="status-tag info">{{ scopeText(detail.scope) }}</span>
        </div>
        <h5>{{ detail.displayName || detail.skillName }}</h5>
        <p class="detail-desc">{{ detail.description || '暂无描述' }}</p>
        <div class="detail-grid" style="margin-bottom:16px">
          <div><span class="t3">Agent 类型</span><b>{{ detail.agentType || '—' }}</b></div>
          <div><span class="t3">Skill 路径</span><b>{{ detail.skillPath || '—' }}</b></div>
          <div><span class="t3">归属人</span><b>{{ detail.ownerId || '—' }}</b></div>
          <div><span class="t3">技能 ID</span><b>{{ detail.id }}</b></div>
          <div><span class="t3">创建时间</span><b>{{ detail.createdAt || '—' }}</b></div>
          <div><span class="t3">更新时间</span><b>{{ detail.updatedAt || '—' }}</b></div>
        </div>

        <!-- SKILL.md 完整内容（原文展示，不渲染） -->
        <h5 style="margin-top:0">SKILL.md</h5>
        <div v-if="mdLoading" class="t2" style="padding:20px;text-align:center">加载中...</div>
        <div v-else-if="mdError" class="t2" style="padding:16px;color:var(--danger)">{{ mdError }}</div>
        <pre v-else class="md-raw">{{ mdContent }}</pre>

        <div class="modal-actions">
          <button class="btn" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import http from '../api'

const skills = ref([])
const stats = ref({})
const agentStats = ref({})
const totalElements = ref(0)
const totalPages = ref(0)
const page = ref(0)
const keyword = ref('')
const agentType = ref('')
const scope = ref('')
const detail = ref(null)
const mdLoading = ref(false)
const mdError = ref('')
const mdContent = ref('')

const SCOPE_TEXT = { official: '官方', repo: '仓库', user: '个人' }
function scopeText(s) { return SCOPE_TEXT[s] || s || '—' }

// Agent 类型选项：来自后端全量统计（过滤后）
const agentTypeOptions = computed(() => Object.keys(agentStats.value).filter(t => t))

async function load() {
  const { data } = await http.get('/skills', {
    params: { keyword: keyword.value, agentType: agentType.value, scope: scope.value, page: page.value, size: 12 }
  })
  skills.value = data.content
  stats.value = data.stats || {}
  agentStats.value = data.agentStats || {}
  totalElements.value = data.totalElements
  totalPages.value = data.totalPages
}
function search() { page.value = 0; load() }
function reset() { keyword.value = ''; agentType.value = ''; scope.value = ''; page.value = 0; load() }

async function openDetail(s) {
  detail.value = s
  mdContent.value = ''
  mdError.value = ''
  mdLoading.value = true
  try {
    const { data } = await http.get(`/skills/${s.id}/content`)
    mdContent.value = data.content || ''
  } catch (err) {
    mdError.value = err.response?.data?.message || 'SKILL.md 加载失败'
  } finally {
    mdLoading.value = false
  }
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
.skill-card { cursor: pointer; }
.skill-card:hover { border-color: var(--primary, #4f6ef2); }
.skill-name {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 12px;
  background: #f4f6f8;
  padding: 2px 8px;
  border-radius: 4px;
}
.skill-name-lg { font-size: 14px; }
.skill-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary, #1a1d21);
  margin: 2px 0 6px;
}
.skill-desc {
  font-size: 12px;
  color: var(--text-secondary, #5f6672);
  line-height: 1.6;
  margin: 0 0 10px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 38px;
}
.skill-more { font-size: 11px; }
.detail-desc {
  font-size: 13px;
  color: var(--text-secondary, #5f6672);
  line-height: 1.7;
  margin: 0 0 16px;
  white-space: pre-wrap;
  word-break: break-word;
}
.detail-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px 20px;
  font-size: 13px;
}
.detail-grid .t3 {
  display: inline-block;
  min-width: 70px;
  color: var(--text-tertiary, #8a919c);
}
.detail-grid b { font-weight: 500; color: var(--text-primary, #1a1d21); }
.md-raw {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-primary, #1a1d21);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 460px;
  overflow-y: auto;
  border: 1px solid var(--border, #e2e6ea);
  border-radius: 8px;
  padding: 14px 18px;
  background: #fafbfc;
  margin: 0;
}
</style>
