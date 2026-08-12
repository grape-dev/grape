<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>Skill 管理</h1>
      <span class="badge">{{ totalElements }} 条</span>
    </div>
    <div style="padding:20px 28px">
      <!-- 范围分类徽章行（点击联动筛选，对齐设备页 dm-stats 风格） -->
      <div class="dm-stats">
        <button class="dm-stat" :class="{ on: !scope }" @click="setScope('')">全部 <strong>{{ scopeStatsTotal }}</strong></button>
        <button v-for="(label, key) in SCOPE_STATS" :key="key" class="dm-stat" :class="{ on: scope === key }" @click="setScope(key)">
          <span class="dm-dot" :style="{ background: SCOPE_COLORS[key] }"></span>{{ label }} <strong>{{ scopeStats[key] ?? 0 }}</strong>
        </button>
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
        <!-- 仓库/个人范围下提供新增技能入口（与主项目看板一致） -->
        <button v-if="scope === 'repo' || scope === 'user'" class="btn btn-primary" @click="openUpload">＋ 新增技能</button>
        <div style="margin-left:auto;display:flex;gap:8px">
          <button class="btn" :style="view === 'list' ? activeTab : {}" @click="view = 'list'">列表</button>
          <button class="btn" :style="view === 'card' ? activeTab : {}" @click="view = 'card'">卡片</button>
        </div>
      </div>

      <!-- 列表模式 -->
      <div class="card" v-if="view === 'list'">
        <table class="vod-table">
          <thead>
            <tr><th>技能名</th><th>显示名</th><th>描述</th><th>Agent</th><th>范围</th><th>路径</th><th>状态</th><th>更新时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="s in skills" :key="s.id" style="cursor:pointer" @click="openDetail(s)">
              <td><code class="skill-name">{{ s.skillName }}</code></td>
              <td>{{ s.displayName || '—' }}</td>
              <td class="t3" style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ s.description || '—' }}</td>
              <td>{{ s.agentType || '—' }}</td>
              <td><span class="status-tag info">{{ scopeText(s.scope) }}</span></td>
              <td class="t3">{{ s.skillPath || '—' }}</td>
              <td><span :class="['status-tag', s.isActive === 1 ? 'success' : 'danger']">{{ s.isActive === 1 ? '启用' : '停用' }}</span></td>
              <td class="t3">{{ s.updatedAt || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 卡片模式 -->
      <div v-else class="device-grid">
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

      <div v-if="totalElements > 0" class="pagination-bar">
        <span class="page-info">第 <strong>{{ page + 1 }}</strong> / {{ totalPages }} 页（共 {{ totalElements }} 条）</span>
        <select v-model="size" class="page-size-select" @change="page = 0; load()">
          <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}条/页</option>
        </select>
        <button class="page-btn" :disabled="page <= 0" @click="page--; load()">上一页</button>
        <button class="page-btn" :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
      </div>
    </div>

    <!-- 上传弹窗 -->
    <div v-if="showUpload" class="modal" @click.self="showUpload = false">
      <div class="modal-box" style="width:520px">
        <h4>新增技能</h4>
        <div class="upload-row">
          <label>类型</label>
          <span>{{ uploadScope === 'repo' ? '仓库技能' : '个人技能' }}</span>
        </div>
        <div v-if="uploadScope === 'repo'" class="upload-row">
          <label>仓库</label>
          <input v-model="uploadRepo" class="upload-input" placeholder="如 owner/repo（repo_key）" />
        </div>
        <div class="upload-row">
          <label>技能类型</label>
          <select v-model="uploadAgentType" class="upload-input">
            <option value="">请选择</option>
            <option v-for="t in AGENT_TYPE_OPTIONS" :key="t" :value="t">{{ t }}</option>
          </select>
        </div>
        <div class="upload-row">
          <label>ZIP 文件</label>
          <input type="file" accept=".zip" class="upload-input" @change="onFileChange" />
        </div>
        <p class="t3" style="margin:6px 0 14px">
          ZIP 内需包含技能目录（含 SKILL.md，Front Matter 必填 name/version/description），
          不支持可执行文件。
          <a href="#" @click.prevent="downloadTemplate">下载模板</a>
        </p>
        <div class="modal-actions">
          <button class="btn" @click="showUpload = false">取消</button>
          <button class="btn btn-primary" :disabled="uploading" @click="submitUpload">{{ uploading ? '上传中...' : '上传' }}</button>
        </div>
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
          <div><span class="t3">归属人</span><b>{{ detail.ownerName || detail.ownerId || '—' }}</b></div>
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
const scopeStats = ref({})
const totalElements = ref(0)
const totalPages = ref(0)
const page = ref(0)
const size = ref(10)
const sizeOptions = [10, 20, 50]
const keyword = ref('')
const agentType = ref('')
const scope = ref('')
const view = ref('card')
const activeTab = { background: 'var(--primary)', color: '#fff', borderColor: 'var(--primary)' }
const detail = ref(null)
const mdLoading = ref(false)
const mdError = ref('')
const mdContent = ref('')
const showUpload = ref(false)
const uploadScope = ref('user')
const uploadRepo = ref('')
const uploadAgentType = ref('')
const uploadFile = ref(null)
const uploading = ref(false)

const AGENT_TYPE_OPTIONS = ['vod', 'issue', 'coding', 'qa', 'sre', 'chat']

const SCOPE_TEXT = { official: '官方', repo: '仓库', user: '个人' }
function scopeText(s) { return SCOPE_TEXT[s] || s || '—' }
const SCOPE_STATS = { official: '官方', repo: '仓库', user: '个人' }
const SCOPE_COLORS = { official: '#4f6ef2', repo: '#fa8c16', user: '#13c2c2' }
const scopeStatsTotal = computed(() => Object.values(scopeStats.value).reduce((a, b) => a + (Number(b) || 0), 0))

function setScope(s) {
  scope.value = s
  page.value = 0
  load()
}

// Agent 类型选项：来自后端全量统计（过滤后）
const agentTypeOptions = computed(() => Object.keys(agentStats.value).filter(t => t))

async function load() {
  const { data } = await http.get('/skills', {
    params: { keyword: keyword.value, agentType: agentType.value, scope: scope.value, page: page.value, size: size.value }
  })
  skills.value = data.content
  stats.value = data.stats || {}
  agentStats.value = data.agentStats || {}
  scopeStats.value = data.scopeStats || {}
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

// ===== 新增技能（上传，逻辑与主项目看板一致）=====
function openUpload() {
  uploadScope.value = scope.value === 'repo' ? 'repo' : 'user'
  uploadRepo.value = ''
  uploadAgentType.value = ''
  uploadFile.value = null
  showUpload.value = true
}

function onFileChange(e) {
  uploadFile.value = e.target.files[0] || null
}

async function downloadTemplate() {
  try {
    const resp = await http.get('/skills/template', { responseType: 'blob' })
    const url = URL.createObjectURL(resp.data)
    const a = document.createElement('a')
    a.href = url
    a.download = 'skill-template.zip'
    a.click()
    URL.revokeObjectURL(url)
  } catch (err) {
    alert(err.response?.data?.message || '模板下载失败')
  }
}

async function submitUpload() {
  if (!uploadFile.value) { alert('请选择 ZIP 文件'); return }
  if (!uploadAgentType.value) { alert('请选择技能类型'); return }
  if (uploadScope.value === 'repo' && !uploadRepo.value.trim()) { alert('仓库技能必须填写仓库'); return }
  const fd = new FormData()
  fd.append('file', uploadFile.value)
  fd.append('scope', uploadScope.value)
  fd.append('agentType', uploadAgentType.value)
  if (uploadScope.value === 'repo') fd.append('repo', uploadRepo.value.trim())
  uploading.value = true
  try {
    const { data } = await http.post('/skills/upload', fd)
    alert(data.message || '上传成功')
    showUpload.value = false
    load()
  } catch (err) {
    alert(err.response?.data?.message || '上传失败')
  } finally {
    uploading.value = false
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
/* 范围分类徽章行（对齐设备页 dm-stats 风格） */
.dm-stats { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
.dm-stat {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; padding: 3px 10px; border-radius: 12px;
  background: #f5f7fa; color: var(--text-secondary); border: 1px solid transparent;
  cursor: pointer; transition: all 0.15s;
}
.dm-stat strong { font-weight: 600; color: var(--text-primary); }
.dm-stat:hover { border-color: var(--border-color); }
.dm-stat.on { background: var(--primary-bg); color: var(--primary); border-color: var(--primary); }
.dm-stat.on strong { color: var(--primary); }
.dm-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.upload-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  font-size: 13px;
}
.upload-row label {
  min-width: 72px;
  color: var(--text-secondary, #5f6672);
}
.upload-input {
  flex: 1;
  padding: 7px 12px;
  border: 1px solid var(--border, #e2e6ea);
  border-radius: 6px;
  font-size: 13px;
  background: #fff;
}
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
