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
        <!-- 官方范围下提供新增技能入口（系统总看板维护官方技能库） -->
        <button v-if="scope === 'official'" class="btn btn-primary" @click="openUpload">＋ 新增技能</button>
        <div style="margin-left:auto;display:flex;gap:8px">
          <button class="btn" :style="view === 'list' ? activeTab : {}" @click="view = 'list'">列表</button>
          <button class="btn" :style="view === 'card' ? activeTab : {}" @click="view = 'card'">卡片</button>
        </div>
      </div>

      <!-- 列表模式 -->
      <div class="card" v-if="view === 'list'">
        <table class="vod-table">
          <thead>
            <tr><th>技能名</th><th>显示名</th><th>描述</th><th>Agent</th><th>范围</th><th>路径</th><th>状态</th><th>更新时间</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="s in skills" :key="s.id" style="cursor:pointer" @click="openDetail(s)">
              <td><code class="skill-name list-name" :title="s.skillName">{{ s.skillName }}</code></td>
              <td>{{ s.displayName || '—' }}</td>
              <td class="t3" style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ s.description || '—' }}</td>
              <td>{{ s.agentType || '—' }}</td>
              <td><span class="status-tag info">{{ scopeText(s.scope) }}</span></td>
              <td class="t3">{{ s.skillPath || '—' }}</td>
              <td><span :class="['status-tag', s.isActive === 1 ? 'success' : 'danger']">{{ s.isActive === 1 ? '启用' : '停用' }}</span></td>
              <td class="t3">{{ s.updatedAt || '—' }}</td>
              <td @click.stop>
                <button v-if="s.scope === 'official' && !s.obsMode" class="btn btn-sm icon-btn" @click="toClusterSkill(s)" title="转存至 OBS">🔄</button>
                <button v-else class="btn btn-sm icon-btn" @click="downloadSkill(s)" title="下载技能包">⬇</button>
                <button v-if="canUpdate(s)" class="btn btn-sm icon-btn" @click="askUpdateSkill(s)" title="更新官方技能内容">✏️</button>
                <button v-if="canDelete(s)" class="btn btn-sm icon-btn" style="color:var(--danger);border-color:var(--danger)" @click="askDeleteSkill(s)" title="删除官方技能">🗑</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 卡片模式 -->
      <div v-else class="device-grid">
        <div v-for="s in skills" :key="s.id" class="device-card skill-card" @click="openDetail(s)">
          <div class="dc-head">
            <span class="dc-name">
              <code class="skill-name card-name" :title="s.skillName">{{ s.skillName }}</code>
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
            <span style="display:flex;gap:6px">
              <button v-if="s.scope === 'official' && !s.obsMode" class="btn btn-sm icon-btn" @click.stop="toClusterSkill(s)" title="转存至 OBS">🔄</button>
              <button v-else class="btn btn-sm icon-btn" @click.stop="downloadSkill(s)" title="下载技能包">⬇</button>
              <button v-if="canUpdate(s)" class="btn btn-sm icon-btn" @click.stop="askUpdateSkill(s)" title="更新官方技能内容">✏️</button>
              <button v-if="canDelete(s)" class="btn btn-sm icon-btn" style="color:var(--danger);border-color:var(--danger)" @click.stop="askDeleteSkill(s)" title="删除官方技能">🗑</button>
              <button class="btn btn-sm icon-btn" @click.stop="openDetail(s)" title="查看详情">👁</button>
            </span>
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
          <span>官方技能</span>
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
          <button v-if="detail.scope !== 'official'" class="btn btn-primary" @click="askPromote(detail)">转为官方技能</button>
          <button class="btn" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>

    <!-- 转存至 OBS 确认弹窗（对齐设备管理 modal 样式） -->
    <div v-if="clusterTarget" class="modal-overlay" @click.self="clusterTarget = null">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">⚠️</span>
          <span class="modal-title">转存至 OBS</span>
        </div>
        <div class="modal-body">
          <p style="margin:0 0 12px">确认将技能「<b>{{ clusterTarget.skillName }}</b>」转存至 OBS？</p>
          <p style="margin:0">转存后<b>不可回退</b>。</p>
        </div>
        <div class="modal-footer">
          <button class="btn" @click="clusterTarget = null">取消</button>
          <button class="btn btn-danger" @click="confirmToCluster">确认转存</button>
        </div>
      </div>
    </div>

    <!-- 删除官方技能确认弹窗（对齐设备管理 modal 样式） -->
    <div v-if="deleteTarget" class="modal-overlay" @click.self="deleteTarget = null">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">⚠️</span>
          <span class="modal-title">删除官方技能</span>
        </div>
        <div class="modal-body">
          <p style="margin:0 0 12px">确认删除官方技能「<b>{{ deleteTarget.skillName }}</b>」？</p>
          <p style="margin:0">删除后<b>不可恢复</b>。</p>
        </div>
        <div class="modal-footer">
          <button class="btn" :disabled="deleteBusy" @click="deleteTarget = null">取消</button>
          <button class="btn btn-danger" :disabled="deleteBusy" @click="confirmDeleteSkill">{{ deleteBusy ? '删除中...' : '确认删除' }}</button>
        </div>
      </div>
    </div>

    <!-- 更新官方技能弹窗（对齐设备管理 modal 样式） -->
    <div v-if="updateTarget" class="modal-overlay" @click.self="updateTarget = null">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">✏️</span>
          <span class="modal-title">更新官方技能</span>
        </div>
        <div class="modal-body">
          <p style="margin:0 0 12px">更新技能「<b>{{ updateTarget.skillName }}</b>」（agent: {{ updateTarget.agentType }}）：</p>
          <p style="margin:0 0 6px">请选择技能 ZIP 包（包内目录名须与技能名一致）</p>
          <input type="file" accept=".zip" @change="updateFile = $event.target.files[0] || null" class="form-input" style="width:100%;padding:8px 12px;border:1px solid var(--border-color);border-radius:8px;font-size:14px" />
        </div>
        <div class="modal-footer">
          <button class="btn" @click="updateTarget = null">取消</button>
          <button class="btn btn-primary" :disabled="updating" @click="confirmUpdateSkill">{{ updating ? '更新中...' : '确认更新' }}</button>
        </div>
      </div>
    </div>

    <!-- 转为官方技能确认弹窗（对齐设备管理 modal 样式） -->
    <div v-if="promoteTarget" class="modal-overlay" @click.self="promoteTarget = null">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">⚠️</span>
          <span class="modal-title">转为官方技能</span>
        </div>
        <div class="modal-body">
          <p style="margin:0 0 12px">确认将技能「<b>{{ promoteTarget.skillName }}</b>」转为官方技能？</p>
          <p style="margin:0">转换后所有部署可见。</p>
        </div>
        <div class="modal-footer">
          <button class="btn" :disabled="promoteBusy" @click="promoteTarget = null">取消</button>
          <button class="btn btn-primary" :disabled="promoteBusy" @click="confirmPromote">{{ promoteBusy ? '转换中...' : '确认转换' }}</button>
        </div>
      </div>
    </div>

    <!-- 通用结果弹窗（成功/失败，对齐设备管理 modal 样式） -->
    <div v-if="resultBox" class="modal-overlay" @click.self="resultBox = null">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">{{ resultBox.type === 'success' ? '✅' : '⚠️' }}</span>
          <span class="modal-title">{{ resultBox.title }}</span>
        </div>
        <div class="modal-body"><p style="margin:0">{{ resultBox.msg }}</p></div>
        <div class="modal-footer">
          <button class="btn btn-primary" @click="resultBox = null">确定</button>
        </div>
      </div>
    </div>

    <!-- 上传失败提示弹窗（对齐设备管理 modal 样式） -->
    <div v-if="uploadError" class="modal-overlay" @click.self="uploadError = null">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">⚠️</span>
          <span class="modal-title">上传失败</span>
        </div>
        <div class="modal-body"><p style="margin:0">{{ uploadError }}</p></div>
        <div class="modal-footer">
          <button class="btn btn-danger" @click="uploadError = null">确定</button>
        </div>
      </div>
    </div>

    <!-- 上传成功提示弹窗（对齐设备管理 modal 样式） -->
    <div v-if="uploadSuccess" class="modal-overlay" @click.self="uploadSuccess = null">
      <div class="modal-dialog">
        <div class="modal-header">
          <span class="modal-icon">✅</span>
          <span class="modal-title">上传成功</span>
        </div>
        <div class="modal-body"><p style="margin:0">{{ uploadSuccess }}</p></div>
        <div class="modal-footer">
          <button class="btn btn-primary" @click="uploadSuccess = null">确定</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import http from '../api'
import { getUser } from '../utils/auth'

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
const SCOPE_COLORS = { official: '#4f6ef2', repo: '#ff9500', user: '#13c2c2' }
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

// ===== 转存至 OBS / 下载技能包 =====
const clusterTarget = ref(null)  // 转存确认弹窗目标技能
const uploadSuccess = ref(null)  // 上传成功提示弹窗文案
const uploadError = ref(null)    // 上传失败提示弹窗文案
function toClusterSkill(s) { clusterTarget.value = s }  // 打开确认弹窗
async function confirmToCluster() {
  const s = clusterTarget.value
  if (!s) return
  try {
    const { data } = await http.post(`/skills/${s.id}/to-cluster`)
    resultBox.value = { type: 'success', title: '转存成功', msg: data.message || '已转存至 OBS' }
    clusterTarget.value = null
    load()
  } catch (err) {
    resultBox.value = { type: 'error', title: '转存失败', msg: err.response?.data?.message || '未知错误' }
  }
}

async function downloadSkill(s) {
  try {
    const { data } = await http.get(`/skills/${s.id}/download`)
    if (data && data.url) window.open(data.url, '_blank')
    else resultBox.value = { type: 'error', title: '下载失败', msg: '无下载地址' }
  } catch (err) {
    resultBox.value = { type: 'error', title: '下载失败', msg: err.response?.data?.message || '未知错误' }
  }
}

// ===== 官方技能更新/删除（v2.5：超管 OR 创建者本人） =====
const currentUser = getUser() || {}
const currentUsername = String(currentUser.username || '').trim()
const isSuperAdmin = Array.isArray(currentUser.roles) && currentUser.roles.includes('超级管理员')
const deleteTarget = ref(null)  // 删除确认弹窗目标技能
const deleteBusy = ref(false)   // 删除确认按钮 loading 态
const updateTarget = ref(null)  // 更新弹窗目标技能
const updateFile = ref(null)    // 更新弹窗选择的 zip
const updating = ref(false)
const resultBox = ref(null)     // 通用结果弹窗 {type:'success'|'error', title, msg}

function canUpdate(s) {
  return s.scope === 'official' && (isSuperAdmin || (s.createdBy && String(s.createdBy) === currentUsername))
}
function canDelete(s) {
  // 内置（created_by=''）永不可删；其余：超管 OR 创建者本人
  return s.scope === 'official' && s.createdBy && (isSuperAdmin || String(s.createdBy) === currentUsername)
}
function askDeleteSkill(s) { deleteTarget.value = s }
async function confirmDeleteSkill() {
  const s = deleteTarget.value
  if (!s || deleteBusy.value) return
  deleteBusy.value = true
  try {
    const { data } = await http.delete(`/skills/${s.id}`)
    resultBox.value = { type: 'success', title: '删除成功', msg: data.message || '已删除' }
    deleteTarget.value = null
    load()
  } catch (err) {
    resultBox.value = { type: 'error', title: '删除失败', msg: err.response?.data?.message || '未知错误' }
  } finally {
    deleteBusy.value = false
  }
}
function askUpdateSkill(s) { updateTarget.value = s; updateFile.value = null }
async function confirmUpdateSkill() {
  const s = updateTarget.value
  if (!s) return
  if (!updateFile.value) { resultBox.value = { type: 'error', title: '更新失败', msg: '请选择技能 ZIP 包' }; return }
  const fd = new FormData()
  fd.append('file', updateFile.value)
  fd.append('agentType', s.agentType || 'chat')
  updating.value = true
  try {
    const { data } = await http.post(`/skills/${s.id}/update`, fd)
    resultBox.value = { type: 'success', title: '更新成功', msg: data.message || '更新成功' }
    updateTarget.value = null
    load()
  } catch (err) {
    resultBox.value = { type: 'error', title: '更新失败', msg: err.response?.data?.message || '未知错误' }
  } finally {
    updating.value = false
  }
}

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
  uploadScope.value = 'official'
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
    resultBox.value = { type: 'error', title: '模板下载失败', msg: err.response?.data?.message || '未知错误' }
  }
}

async function submitUpload() {
  if (!uploadFile.value) { uploadError.value = '请选择 ZIP 文件'; return }
  if (!uploadAgentType.value) { uploadError.value = '请选择技能类型'; return }
  const fd = new FormData()
  fd.append('file', uploadFile.value)
  fd.append('scope', uploadScope.value)
  fd.append('agentType', uploadAgentType.value)
  uploading.value = true
  try {
    const { data } = await http.post('/skills/upload', fd)
    uploadSuccess.value = data.message || '上传成功'
    showUpload.value = false
    load()
  } catch (err) {
    uploadError.value = err.response?.data?.message || '上传失败'
  } finally {
    uploading.value = false
  }
}

// ===== 转官方（v2.7：设备管理 modal 弹窗——confirm/alert 已替换） =====
const promoteTarget = ref(null)  // 转为官方确认弹窗目标技能
const promoteBusy = ref(false)
function askPromote(s) { promoteTarget.value = s }
async function confirmPromote() {
  const s = promoteTarget.value
  if (!s || promoteBusy.value) return
  promoteBusy.value = true
  try {
    const { data } = await http.post(`/skills/${s.id}/promote`)
    resultBox.value = { type: 'success', title: '转换成功',
      msg: data.warning ? data.message + '\n\n注意：' + data.warning : data.message }
    promoteTarget.value = null
    detail.value = null
    load()
  } catch (err) {
    resultBox.value = { type: 'error', title: '转换失败', msg: err.response?.data?.message || '未知错误' }
  } finally {
    promoteBusy.value = false
  }
}

onMounted(load)
</script>

<style scoped>
/* 图标按钮（转存/下载/更新/删除）：紧凑、仅图标 + title 提示 */
.icon-btn {
  padding: 2px 8px;
  font-size: 13px;
  line-height: 1.4;
}
.search-input {
  padding: 7px 12px;
  border: 1px solid var(--border, #e2e6ea);
  border-radius: 8px;
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
  border-radius: 8px;
}
/* 名称截断（参考设备管理）：长技能名不挤压 范围/状态 标签，悬停显示全名 */
.list-name {
  display: inline-block;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: middle;
}
.card-name {
  display: inline-block;
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: middle;
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
  background: rgba(0,0,0,0.04); color: var(--text-secondary); border: 1px solid transparent;
  cursor: pointer; transition: all var(--transition);
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
  border-radius: 8px;
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
  background: rgba(0,0,0,0.02);
  margin: 0;
}
</style>
