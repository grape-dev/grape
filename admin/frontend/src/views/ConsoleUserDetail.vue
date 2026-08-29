<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <span class="back-btn" @click="$router.push('/sys/console')">
        <svg viewBox="0 0 24 24" width="16" height="16"><path d="M15 18l-6-6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        返回
      </span>
      <h1>{{ info.username || '用户详情' }}</h1>
      <span v-if="info.online" class="status-tag success">在线</span>
      <span v-else class="status-tag default">离线</span>
      <span class="t3" style="font-size:12px;margin-left:4px" v-if="info.lastActiveAt">最后活跃 {{ formatTime(info.lastActiveAt) }}</span>
      <div style="margin-left:auto;display:flex;gap:8px">
        <button class="btn btn-danger btn-sm" @click="resetToken" :disabled="resetting">{{ resetting ? '处理中...' : '强制下线' }}</button>
      </div>
    </div>

    <div v-if="loading" class="loading-spinner">加载中...</div>

    <div v-else style="padding:20px 40px">
      <!-- 基本信息（紧凑横排）+ 凭证配置 -->
      <div class="card" style="padding:0;margin-bottom:20px">
        <div class="section-header">
          <h3 class="sh-title">基本信息</h3>
          <span class="sh-count">凭证 {{ credentialConfigured }} / 4</span>
        </div>
        <div style="padding:20px 24px">
          <!-- 基本信息 -->
          <div class="info-row">
            <div class="info-item"><span class="info-label">用户名</span><b>{{ info.username || '—' }}</b></div>
            <div class="info-item"><span class="info-label">昵称</span>{{ info.nickname || '—' }}</div>
            <div class="info-item"><span class="info-label">来源</span>{{ info.gitcodeId ? 'GitCode 登录' : '其他' }}</div>
            <div class="info-item"><span class="info-label">创建时间</span>{{ formatTime(info.createdAt) }}</div>
            <div class="info-item"><span class="info-label">最后场景</span>{{ info.lastScene || '—' }}</div>
            <div class="info-item" v-if="info.gitcodePublishRepo"><span class="info-label">发布仓库</span><router-link to="/repos/manage" style="color:var(--primary);text-decoration:none">{{ info.gitcodePublishRepo }}</router-link></div>
          </div>

          <!-- 凭证配置（紧挨基本信息） -->
          <div class="cred-row">
            <div class="cred-chip" :class="{ on: info.hasGitcodeToken }">
              <span class="cred-chip-name">GitCode</span>
              <span class="cred-chip-status">{{ info.hasGitcodeToken ? '已配置' : '未配置' }}</span>
            </div>
            <div class="cred-chip" :class="{ on: info.hasHuaweiAk }">
              <span class="cred-chip-name">华为云</span>
              <span class="cred-chip-status">{{ info.hasHuaweiAk ? '已配置' : '未配置' }}</span>
            </div>
            <div class="cred-chip" :class="{ on: info.hasFeishu }">
              <span class="cred-chip-name">飞书</span>
              <span class="cred-chip-status">{{ info.hasFeishu ? '已配置' : '未配置' }}</span>
            </div>
            <div class="cred-chip" :class="{ on: info.hasCodeartsAk }">
              <span class="cred-chip-name">CodeArts</span>
              <span class="cred-chip-status">{{ info.hasCodeartsAk ? '已配置' : '未配置' }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 活动统计 + 最近活动 -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px">
        <!-- 活动统计 -->
        <div class="card" style="padding:0">
          <div class="section-header">
            <h3 class="sh-title">活动统计</h3>
            <div class="sh-actions">
              <button class="btn btn-sm" :style="activityTab === 'task' ? 'background:var(--primary);color:#fff;border-color:var(--primary)' : ''" @click="activityTab = 'task'">任务统计</button>
              <button class="btn btn-sm" :style="activityTab === 'issue' ? 'background:var(--primary);color:#fff;border-color:var(--primary)' : ''" @click="activityTab = 'issue'">仓库与 Issue</button>
            </div>
          </div>
          <div style="padding:20px 24px">
            <!-- 任务统计 tab -->
            <div v-if="activityTab === 'task'" class="activity-grid">
              <div class="activity-card">
                <div class="activity-value">{{ info.queuedCount ?? 0 }}</div>
                <div class="activity-label">排队中</div>
              </div>
              <div class="activity-card">
                <div class="activity-value" style="color:#006eff">{{ info.dispatchedCount ?? 0 }}</div>
                <div class="activity-label">已派发</div>
              </div>
              <div class="activity-card">
                <div class="activity-value" style="color:#006eff">{{ info.runningCount ?? 0 }}</div>
                <div class="activity-label">运行中</div>
              </div>
              <div class="activity-card">
                <div class="activity-value" style="color:#34c759">{{ info.completedCount ?? 0 }}</div>
                <div class="activity-label">已完成</div>
              </div>
              <div class="activity-card">
                <div class="activity-value" style="color:#ff3b30">{{ info.failedCount ?? 0 }}</div>
                <div class="activity-label">失败</div>
              </div>
              <div class="activity-card">
                <div class="activity-value" style="color:#aeaeb2">{{ info.cancelledCount ?? 0 }}</div>
                <div class="activity-label">已取消</div>
              </div>
              <div class="activity-card">
                <div class="activity-value" style="color:#ff9500">{{ info.inProgressCount ?? 0 }}</div>
                <div class="activity-label">进行中</div>
              </div>
              <div class="activity-card">
                <div class="activity-value" style="color:#ff3b30">{{ info.stuckCount ?? 0 }}</div>
                <div class="activity-label">卡住</div>
              </div>
            </div>
            <!-- 仓库与 Issue tab -->
            <div v-else>
              <!-- 仓库选择下拉 -->
              <div style="margin-bottom:16px">
                <select v-model="selectedRepo" class="page-size-select" style="width:100%;max-width:400px;padding:8px 12px" @change="loadRepoIssues(0)">
                  <option value="">请选择仓库</option>
                  <option v-for="r in info.repoMatrix" :key="r.repoKey" :value="r.repoKey">{{ r.fullName }}</option>
                </select>
              </div>

              <!-- 仓库总体统计 -->
              <div v-if="selectedRepoData">
                <!-- Issue总数 + 任务总数 单独一行 -->
                <div style="display:flex;gap:24px;margin-bottom:12px;padding:12px 16px;background:rgba(0,0,0,0.02);border-radius:10px">
                  <div style="display:flex;align-items:baseline;gap:6px">
                    <span class="t3" style="font-size:13px">Issue 总数</span>
                    <span style="font-size:22px;font-weight:700;color:#006eff">{{ selectedRepoData.issueCount }}</span>
                  </div>
                  <div style="display:flex;align-items:baseline;gap:6px">
                    <span class="t3" style="font-size:13px">任务总数</span>
                    <span style="font-size:22px;font-weight:700;color:var(--text-primary)">{{ selectedRepoData.taskCount }}</span>
                  </div>
                </div>
                <!-- 状态枚举统计卡 -->
                <div class="repo-summary-grid">
                  <div class="activity-card" style="cursor:pointer" @click="togglePendingDetail">
                    <div class="activity-value" style="color:#ff9500">{{ pendingTotal }}</div>
                    <div class="activity-label">待处理 {{ pendingDetailOpen ? '▲' : '▼' }}</div>
                  </div>
                  <div class="activity-card">
                    <div class="activity-value" style="color:#006eff">{{ selectedRepoData.running ?? 0 }}</div>
                    <div class="activity-label">运行中</div>
                  </div>
                  <div class="activity-card">
                    <div class="activity-value" style="color:#34c759">{{ selectedRepoData.completed }}</div>
                    <div class="activity-label">已完成</div>
                  </div>
                  <div class="activity-card">
                    <div class="activity-value" style="color:#ff3b30">{{ selectedRepoData.failed }}</div>
                    <div class="activity-label">失败</div>
                  </div>
                  <div class="activity-card">
                    <div class="activity-value" style="color:#aeaeb2">{{ selectedRepoData.cancelled ?? 0 }}</div>
                    <div class="activity-label">已取消</div>
                  </div>
                  <div class="activity-card">
                    <div class="activity-value" style="color:#5e5ce6">{{ selectedRepoData.taskCount > 0 ? Math.round(selectedRepoData.completed * 1000 / selectedRepoData.taskCount) / 10 + '%' : '—' }}</div>
                    <div class="activity-label">成功率</div>
                  </div>
                </div>
                <!-- 待处理明细 -->
                <div v-if="pendingDetailOpen" style="margin-top:8px;padding:12px 16px;background:rgba(255,149,0,0.04);border-radius:10px;display:flex;gap:24px">
                  <div style="display:flex;align-items:baseline;gap:4px">
                    <span class="t3">排队中</span>
                    <span style="font-size:16px;font-weight:600">{{ selectedRepoData.queued ?? 0 }}</span>
                  </div>
                  <div style="display:flex;align-items:baseline;gap:4px">
                    <span class="t3">已派发</span>
                    <span style="font-size:16px;font-weight:600;color:#006eff">{{ selectedRepoData.dispatched ?? 0 }}</span>
                  </div>
                  <div style="display:flex;align-items:baseline;gap:4px">
                    <span class="t3">卡住</span>
                    <span style="font-size:16px;font-weight:600;color:#ff3b30">{{ selectedRepoData.stuck ?? 0 }}</span>
                  </div>
                </div>
              </div>

              <!-- Issue 分页列表 -->
              <div v-if="selectedRepo" style="margin-top:16px">
                <table class="vod-table" v-if="repoIssues.length">
                  <thead>
                    <tr><th style="width:25%">Issue</th><th style="width:15%">任务</th><th style="width:15%">成功率</th><th style="width:45%">最近活动</th></tr>
                  </thead>
                  <tbody>
                    <template v-for="iss in repoIssues" :key="iss.issueId">
                      <tr>
                        <td>
                          <a :href="`https://gitcode.com/${selectedRepoData?.fullName}/issues/${iss.issueId.replace('#','')}`" target="_blank" style="color:var(--primary);text-decoration:none">{{ iss.issueId }}</a>
                        </td>
                        <td class="t3" style="cursor:pointer;color:var(--primary)" @click="iss._open = !iss._open">
                          {{ iss.taskCount }} <span style="font-size:10px">{{ iss._open ? '▲' : '▼' }}</span>
                        </td>
                        <td class="t3">{{ iss.taskCount > 0 ? Math.round(iss.completed * 1000 / iss.taskCount) / 10 + '%' : '—' }}</td>
                        <td class="t3">{{ formatTime(iss.lastTaskAt) }}</td>
                      </tr>
                      <tr v-if="iss._open" style="background:rgba(0,0,0,0.02)">
                        <td colspan="4" style="padding:10px 16px">
                          <div style="display:flex;gap:20px;flex-wrap:wrap">
                            <span class="t3">已完成 <b style="color:#34c759">{{ iss.completed }}</b></span>
                            <span class="t3">待处理 <b style="color:#ff9500">{{ (iss.queued ?? 0) + (iss.dispatched ?? 0) + (iss.stuck ?? 0) }}</b></span>
                            <span class="t3" style="padding-left:12px;border-left:1px solid rgba(0,0,0,0.06)">
                              <span style="color:var(--text-tertiary)">排队 {{ iss.queued ?? 0 }}</span>
                            </span>
                            <span class="t3">
                              <span style="color:var(--text-tertiary)">派发 {{ iss.dispatched ?? 0 }}</span>
                            </span>
                            <span class="t3">
                              <span style="color:var(--text-tertiary)">卡住 {{ iss.stuck ?? 0 }}</span>
                            </span>
                            <span class="t3" style="padding-left:12px;border-left:1px solid rgba(0,0,0,0.06)">运行中 <b style="color:#006eff">{{ iss.running ?? 0 }}</b></span>
                            <span class="t3">失败 <b style="color:#ff3b30">{{ iss.failed }}</b></span>
                            <span class="t3">已取消 <b style="color:#aeaeb2">{{ iss.cancelled ?? 0 }}</b></span>
                          </div>
                        </td>
                      </tr>
                    </template>
                    <tr v-if="!repoIssues.length">
                      <td colspan="4" style="text-align:center;padding:20px;color:var(--text-tertiary)">该仓库暂无 Issue 数据</td>
                    </tr>
                  </tbody>
                </table>
                <PaginationBar v-if="repoIssuesTotal > 0" v-model:page="repoIssuesPage" v-model:size="repoIssuesSize" :total="repoIssuesTotal" :total-pages="repoIssuesTotalPages" :size-options="[5, 10]" @change="loadRepoIssues()" />
                <p v-else-if="!repoIssuesLoading" class="t2" style="padding:20px;text-align:center">该仓库暂无 Issue 数据</p>
              </div>
              <p v-else-if="!selectedRepo" class="t2" style="padding:20px;text-align:center">请选择仓库查看 Issue 列表</p>
            </div>
          </div>
        </div>

        <!-- 最近活动 -->
        <div class="card" style="padding:0">
          <div class="section-header">
            <h3 class="sh-title">最近活动</h3>
            <div class="sh-actions">
              <button class="btn btn-sm" :style="activityFilter === 'recent' ? 'background:var(--primary);color:#fff;border-color:var(--primary)' : ''" @click="activityFilter = 'recent'">最近执行</button>
              <button class="btn btn-sm" :style="activityFilter === 'pending' ? 'background:var(--primary);color:#fff;border-color:var(--primary)' : ''" @click="activityFilter = 'pending'">进行中</button>
              <button class="btn btn-sm" :style="activityFilter === 'failed' ? 'background:var(--primary);color:#fff;border-color:var(--primary)' : ''" @click="activityFilter = 'failed'">最近失败</button>
            </div>
          </div>
          <div style="padding:20px 24px">
            <p class="t3" style="margin-bottom:12px">最多查询 5 条，当前 {{ filteredActivity.length }} 条</p>
            <table class="vod-table" v-if="filteredActivity.length">
              <thead>
                <tr><th style="width:8%">Seq</th><th style="width:18%">事件</th><th style="width:12%">Agent</th><th style="width:15%">Engine</th><th style="width:10%">耗时</th><th style="width:10%">状态</th><th style="width:12%">Issue</th><th style="width:15%">时间</th></tr>
              </thead>
              <tbody>
                <tr v-for="(t, idx) in filteredActivity" :key="t.id" style="cursor:pointer" @click="openLogModal(t.id, t)">
                  <td class="t3">#{{ filteredActivity.length - idx }}</td>
                  <td><span :class="['status-tag', eventTypeClass(t.eventType)]" style="font-size:11px">{{ eventTypeText(t.eventType) }}</span></td>
                  <td><span class="status-tag info" style="font-size:11px">{{ t.agentType || '—' }}</span></td>
                  <td class="t3" style="font-size:11px">{{ t.engine || '—' }}</td>
                  <td class="t3">{{ formatDuration(t.duration) }}</td>
                  <td><span :class="['status-tag', t.status === 'completed' ? 'success' : t.status === 'failed' ? 'danger' : t.status === 'cancelled' ? 'default' : 'warning']" style="font-size:10px">{{ t.status === 'completed' ? '完成' : t.status === 'failed' ? '失败' : t.status === 'cancelled' ? '取消' : t.status }}</span></td>
                  <td class="t3"><a v-if="t.issueId" :href="`https://gitcode.com/${t.repoName}/issues/${t.issueId.replace('#','')}`" target="_blank" @click.stop style="color:var(--primary);text-decoration:none">{{ t.issueId }}</a><span v-else>—</span></td>
                  <td class="t3">{{ formatTime(t.startedAt) }}</td>
                </tr>
              </tbody>
            </table>
            <p v-else class="t2" style="padding:20px;text-align:center">暂无记录</p>
          </div>
        </div>
      </div>
    </div>

    <!-- 任务日志弹窗 -->
    <div v-if="logModal.show" class="log-modal-overlay" @click.self="closeLogModal" style="position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.4);z-index:9999;display:flex;align-items:center;justify-content:center">
      <div style="background:#fff;border-radius:14px;width:90%;max-width:800px;max-height:80vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,0.15)">
        <div style="display:flex;align-items:center;gap:12px;padding:16px 20px;border-bottom:1px solid var(--border-color)">
          <h3 style="font-size:15px;font-weight:600;margin:0;flex:1;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <span v-if="logModal.info.repoKey" class="t3" style="font-size:13px">{{ logModal.info.repoKey.replace('gitcode:','') }}</span>
            <b v-if="logModal.info.issueId">{{ logModal.info.issueId }}</b>
            <span v-if="logModal.info.agentType" class="status-tag info" style="font-size:11px">{{ logModal.info.agentType }}</span>
            <span v-if="logModal.info.skill" class="t3" style="font-size:12px">{{ logModal.info.skill }}</span>
            <span v-if="logModal.info.engine" class="t3" style="font-size:12px;color:var(--primary)">{{ logModal.info.engine }}</span>
          </h3>
          <button class="btn btn-sm" @click="closeLogModal">✕ 关闭</button>
        </div>
        <div style="padding:12px 20px;border-bottom:1px solid var(--border-color);display:flex;align-items:center;gap:12px;flex-wrap:wrap" v-if="logModal.info.status">
          <span :class="['status-tag', logModal.info.status === 'completed' ? 'success' : logModal.info.status === 'failed' ? 'danger' : 'warning']" style="font-size:11px">{{ logModal.info.status }}</span>
          <span v-if="logModal.info.startedAt" class="t3" style="font-size:12px">开始 {{ formatTime(logModal.info.startedAt).slice(11,19) }}</span>
          <span v-if="logModal.info.completedAt" class="t3" style="font-size:12px">→ 完成 {{ formatTime(logModal.info.completedAt).slice(11,19) }}</span>
          <span v-if="logModal.info.exitCode != null" class="t3" style="font-size:12px" :style="{color: logModal.info.exitCode !== 0 ? 'var(--danger)' : 'var(--success)'}">Exit: {{ logModal.info.exitCode }}</span>
        </div>
        <div style="flex:1;overflow:auto;padding:16px 20px">
          <div v-if="logModal.loading" style="text-align:center;padding:40px;color:var(--text-tertiary)">加载中...</div>
          <div v-else-if="logModal.messages.length === 0" style="text-align:center;padding:40px;color:var(--text-tertiary)">
            <div v-if="logModal.info.resultText" style="text-align:left;background:#f5f5f7;padding:12px 16px;border-radius:10px;margin-bottom:12px;font-size:13px;color:var(--text-secondary);max-height:200px;overflow:auto;white-space:pre-wrap">{{ logModal.info.resultText }}</div>
            <div v-if="logModal.info.errors" style="text-align:left;color:var(--danger);font-size:13px">{{ logModal.info.errors }}</div>
            <span v-if="!logModal.info.resultText && !logModal.info.errors">暂无日志记录</span>
          </div>
          <div v-else style="background:#1d1d1f;border-radius:10px;padding:12px;font-family:monospace;font-size:12px;line-height:1.6;overflow:auto">
            <div v-for="msg in logModal.messages" :key="msg.seq" style="display:flex;gap:8px;color:#aeaeb2">
              <span style="color:#6e6e73;flex-shrink:0">{{ (msg.createdAt || '').slice(11,19) }}</span>
              <span :style="{color: msg.type === 'error' ? '#ff3b30' : msg.type === 'result' ? '#34c759' : msg.type === 'tool' ? '#5e5ce6' : '#aeaeb2'}">{{ msg.content }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Toast -->
    <div v-if="toast.show" class="toast-message" :class="toast.type">{{ toast.msg }}</div>
  </div>
</template>

<script setup>
import { onMounted, ref, computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import http from '../api'
import PaginationBar from '../components/PaginationBar.vue'

const route = useRoute()
const info = ref({})
const loading = ref(true)
const resetting = ref(false)
const activityTab = ref('task')
const activityFilter = ref('recent')
const selectedRepo = ref('')
const pendingDetailOpen = ref(false)
const logModal = ref({ show: false, loading: false, messages: [], info: {} })

const pendingTotal = computed(() => {
  if (!selectedRepoData.value) return 0
  return (selectedRepoData.value.queued ?? 0) + (selectedRepoData.value.dispatched ?? 0) + (selectedRepoData.value.stuck ?? 0)
})

function togglePendingDetail() {
  pendingDetailOpen.value = !pendingDetailOpen.value
}
async function openLogModal(taskId, t) {
  logModal.value = { show: true, loading: true, messages: [], info: { issueId: t.issueId, repoKey: t.repoName ? 'gitcode:' + t.repoName : '', agentType: t.agentType, skill: t.skill, engine: t.engine, status: t.status } }
  try {
    const [msgsResp, infoResp] = await Promise.all([
      http.get(`/runtime/tasks/${taskId}/messages`),
      http.get(`/runtime/tasks/${taskId}/info`)
    ])
    const info = infoResp.data || {}
    if (!info.repoKey && t.repoName) info.repoKey = 'gitcode:' + t.repoName
    if (!info.issueId) info.issueId = t.issueId
    if (!info.agentType) info.agentType = t.agentType
    if (!info.skill) info.skill = t.skill
    if (!info.engine) info.engine = t.engine
    if (!info.status) info.status = t.status
    logModal.value = { show: true, loading: false, messages: msgsResp.data || [], info }
  } catch (e) {
    logModal.value = { show: true, loading: false, messages: [], info: { error: e.message, issueId: t.issueId, repoKey: t.repoName ? 'gitcode:' + t.repoName : '' } }
  }
}
function closeLogModal() {
  logModal.value = { show: false, loading: false, messages: [], info: {} }
}
function eventTypeText(t) {
  const map = { issue_opened: 'Issue创建', issue_comment: '评论', issue_updated: '更新', a2a_event: 'A2A交办', pull_request_opened: 'PR创建', pull_request_merged: 'PR合并', push: '推送', vod_signal: 'VOD信号' }
  return map[t] || t || '—'
}
function eventTypeClass(t) {
  if (t === 'a2a_event') return 'info'
  if (t === 'pull_request_merged') return 'success'
  if (t === 'issue_opened') return 'info'
  return 'default'
}
function formatDuration(d) {
  if (!d) return '—'
  const m = d.match(/^(\d+)s$/)
  if (!m) return d
  const s = parseInt(m[1])
  if (s < 60) return s + 's'
  if (s < 3600) return Math.floor(s / 60) + 'm' + (s % 60) + 's'
  return Math.floor(s / 3600) + 'h' + Math.floor((s % 3600) / 60) + 'm'
}
const repoIssues = ref([])
const repoIssuesPage = ref(0)
const repoIssuesSize = ref(5)
const repoIssuesTotal = ref(0)
const repoIssuesTotalPages = ref(0)
const repoIssuesLoading = ref(false)
const toast = ref({ show: false, msg: '', type: 'success' })

const selectedRepoData = computed(() => {
  if (!selectedRepo.value) return null
  return (info.value.repoMatrix || []).find(r => r.repoKey === selectedRepo.value)
})

const filteredActivity = computed(() => {
  if (activityFilter.value === 'pending') return info.value.recentPendingTasks || []
  if (activityFilter.value === 'failed') return info.value.recentFailedTasks || []
  return info.value.recentTasks || []
})

const credentialConfigured = computed(() => {
  let count = 0
  if (info.value.hasGitcodeToken) count++
  if (info.value.hasHuaweiAk) count++
  if (info.value.hasFeishu) count++
  if (info.value.hasCodeartsAk) count++
  return count
})

async function loadRepoIssues(p) {
  if (p !== undefined) repoIssuesPage.value = p
  if (!selectedRepo.value) return
  repoIssuesLoading.value = true
  try {
    const { data } = await http.get(`/console-users/${route.params.id}/repo-issues`, {
      params: { repoKey: selectedRepo.value, page: repoIssuesPage.value, size: repoIssuesSize.value }
    })
    repoIssues.value = data.content || []
    repoIssuesTotal.value = data.totalElements || 0
    repoIssuesTotalPages.value = data.totalPages || 0
  } catch (e) {
    console.error('[ConsoleUserDetail] loadRepoIssues failed', e)
    repoIssues.value = []
    repoIssuesTotal.value = 0
  } finally {
    repoIssuesLoading.value = false
  }
}

function formatTime(val) {
  if (!val || val === '—') return '—'
  try {
    let ts = val.length > 19 ? val.substring(0, 16) : val
    return ts.replace('T', ' ')
  } catch { return val }
}

async function load() {
  loading.value = true
  try {
    const { data } = await http.get(`/console-users/${route.params.id}`)
    info.value = data
  } catch (e) {
    console.error('[ConsoleUserDetail] load failed', e)
  } finally {
    loading.value = false
  }
}

async function resetToken() {
  if (!confirm(`确认强制 ${info.value.username} 下线？用户需要重新登录。`)) return
  resetting.value = true
  try {
    const { data } = await http.post(`/console-users/${route.params.id}/reset-token`)
    if (data.success) {
      toast.value = { show: true, msg: data.message, type: 'success' }
      setTimeout(() => { toast.value.show = false }, 3000)
      await load()
    } else {
      toast.value = { show: true, msg: data.message, type: 'error' }
      setTimeout(() => { toast.value.show = false }, 3000)
    }
  } catch (e) {
    toast.value = { show: true, msg: '操作失败', type: 'error' }
    setTimeout(() => { toast.value.show = false }, 3000)
  } finally {
    resetting.value = false
  }
}

onMounted(() => load())

watch(activityTab, (val) => {
  if (val === 'issue' && !selectedRepo.value && info.value.repoMatrix && info.value.repoMatrix.length) {
    selectedRepo.value = info.value.repoMatrix[0].repoKey
    loadRepoIssues(0)
  }
})
</script>

<style scoped>
.back-btn { display: inline-flex; align-items: center; gap: 4px; cursor: pointer; color: var(--text-secondary); font-size: 13px; transition: color var(--transition); }
.back-btn:hover { color: var(--primary); }

.info-row { display: flex; flex-wrap: wrap; gap: 20px 32px; margin-bottom: 16px; }
.info-item { display: flex; flex-direction: column; gap: 2px; }
.info-label { font-size: 11px; color: var(--text-tertiary); }

.cred-row { display: flex; gap: 10px; flex-wrap: wrap; padding-top: 16px; border-top: 1px solid var(--border-soft); }
.cred-chip { display: flex; flex-direction: column; align-items: center; gap: 2px; padding: 8px 16px; border-radius: 10px; background: rgba(0,0,0,0.04); min-width: 80px; }
.cred-chip.on { background: rgba(52,199,89,0.08); }
.cred-chip-name { font-size: 12px; font-weight: 600; color: var(--text-primary); }
.cred-chip-status { font-size: 10px; color: var(--text-tertiary); }
.cred-chip.on .cred-chip-status { color: #34c759; }

.activity-grid { display: grid; grid-template-columns: repeat(8, 1fr); gap: 6px; }
.repo-summary-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; }
.activity-card { text-align: center; padding: 14px 6px; background: rgba(0,0,0,0.02); border-radius: 10px; }
.activity-value { font-size: 20px; font-weight: 700; color: var(--text-primary); }
.activity-label { font-size: 11px; color: var(--text-secondary); margin-top: 4px; }

.timeline { display: flex; flex-direction: column; gap: 0; }
.timeline-item { display: flex; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border-soft); }
.timeline-item:last-child { border-bottom: none; }
.timeline-dot { width: 8px; height: 8px; border-radius: 50%; margin-top: 4px; flex-shrink: 0; }
.timeline-dot.completed { background: #34c759; }
.timeline-dot.failed { background: #ff3b30; }
.timeline-dot.pending { background: #ff9500; }
.timeline-content { flex: 1; min-width: 0; }
.timeline-header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.timeline-repo { font-size: 13px; font-weight: 500; color: var(--text-primary); }
.timeline-issue { font-size: 11px; color: var(--primary); background: rgba(0,107,255,0.06); padding: 1px 6px; border-radius: 6px; text-decoration: none; cursor: pointer; }
.timeline-issue:hover { background: rgba(0,107,255,0.12); }
.timeline-meta { display: flex; gap: 12px; margin-top: 2px; font-size: 11px; }
</style>
