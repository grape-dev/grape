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
      <span class="t3" style="font-size:12px;margin-left:8px" v-if="info.lastActiveAt">最后活跃 {{ formatTime(info.lastActiveAt) }}</span>
    </div>

    <div v-if="loading" class="loading-spinner">加载中...</div>

    <div v-else style="padding:20px 40px">
      <!-- 基本信息 + 活动统计 -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px">
        <!-- 基本信息 -->
        <div class="card" style="padding:0">
          <div class="section-header">
            <h3 class="sh-title">基本信息</h3>
          </div>
          <div style="padding:20px 24px">
            <div class="detail-grid">
              <div class="detail-row"><span>用户名</span><b>{{ info.username || '—' }}</b></div>
              <div class="detail-row"><span>昵称</span>{{ info.nickname || '—' }}</div>
              <div class="detail-row"><span>GitCode ID</span>{{ info.gitcodeId ? info.gitcodeId.substring(0, 16) + '…' : '—' }}</div>
              <div class="detail-row"><span>创建时间</span>{{ formatTime(info.createdAt) }}</div>
              <div class="detail-row"><span>最后场景</span>{{ info.lastScene || '—' }}</div>
              <div class="detail-row" v-if="info.gitcodePublishRepo">
                <span>GitCode 发布仓库</span>
                <router-link to="/repos/manage" style="color:var(--primary);text-decoration:none">{{ info.gitcodePublishRepo }}</router-link>
              </div>
              <div class="detail-row" v-if="info.githubPublishRepo">
                <span>GitHub 发布仓库</span>
                <router-link to="/repos/manage" style="color:var(--primary);text-decoration:none">{{ info.githubPublishRepo }}</router-link>
              </div>
            </div>
          </div>
        </div>

        <!-- 活动统计 -->
        <div class="card" style="padding:0">
          <div class="section-header">
            <h3 class="sh-title">活动统计</h3>
          </div>
          <div style="padding:20px 24px">
            <div class="activity-grid">
              <div class="activity-card">
                <div class="activity-value">{{ info.taskCount ?? 0 }}</div>
                <div class="activity-label">任务总数</div>
              </div>
              <div class="activity-card">
                <div class="activity-value" style="color:#34c759">{{ info.completedCount ?? 0 }}</div>
                <div class="activity-label">已完成</div>
              </div>
              <div class="activity-card">
                <div class="activity-value" style="color:#ff9500">{{ info.pendingCount ?? 0 }}</div>
                <div class="activity-label">进行中</div>
              </div>
              <div class="activity-card">
                <div class="activity-value" style="color:#ff3b30">{{ info.failedCount ?? 0 }}</div>
                <div class="activity-label">失败</div>
              </div>
              <div class="activity-card">
                <div class="activity-value" style="color:#006eff">{{ info.successRate || '—' }}</div>
                <div class="activity-label">成功率</div>
              </div>
              <div class="activity-card" style="grid-column:span 2;cursor:pointer" @click="showRepos = !showRepos">
                <div class="activity-value">{{ info.repoCount ?? 0 }}</div>
                <div class="activity-label">关联仓库 {{ showRepos ? '▲' : '▼' }}</div>
              </div>
            </div>
            <!-- 仓库列表展开 -->
            <div v-if="showRepos && info.repos && info.repos.length" style="margin-top:12px;display:flex;flex-wrap:wrap;gap:8px">
              <router-link
                v-for="r in info.repos" :key="r.repoKey"
                :to="`/repos/manage`"
                class="repo-chip"
              >📦 {{ r.fullName }}</router-link>
            </div>
          </div>
        </div>
      </div>

      <!-- 凭证配置 -->
      <div class="card" style="padding:0">
        <div class="section-header">
          <h3 class="sh-title">凭证配置</h3>
          <span class="sh-count">已配置 {{ credentialConfigured }} / 4</span>
        </div>
        <div style="padding:20px 24px">
          <div class="cred-group-grid">
            <!-- GitCode -->
            <div class="cred-group">
              <div class="cred-group-title">GitCode</div>
              <div class="cred-group-body">
                <span :class="['status-tag', info.hasGitcodeToken ? 'success' : 'default']">Token {{ info.hasGitcodeToken ? '已配置' : '未配置' }}</span>
              </div>
            </div>
            <!-- 华为云 -->
            <div class="cred-group">
              <div class="cred-group-title">华为云</div>
              <div class="cred-group-body">
                <span :class="['status-tag', info.hasHuaweiAk ? 'success' : 'default']">AK {{ info.hasHuaweiAk ? '已配置' : '未配置' }}</span>
                <span :class="['status-tag', info.hasHuaweiSk ? 'success' : 'default']">SK {{ info.hasHuaweiSk ? '已配置' : '未配置' }}</span>
              </div>
            </div>
            <!-- 飞书 -->
            <div class="cred-group">
              <div class="cred-group-title">飞书</div>
              <div class="cred-group-body">
                <span :class="['status-tag', info.hasFeishu ? 'success' : 'default']">App ID {{ info.hasFeishu ? '已配置' : '未配置' }}</span>
                <span :class="['status-tag', info.hasFeishuSecret ? 'success' : 'default']">Secret {{ info.hasFeishuSecret ? '已配置' : '未配置' }}</span>
              </div>
            </div>
            <!-- CodeArts -->
            <div class="cred-group">
              <div class="cred-group-title">CodeArts</div>
              <div class="cred-group-body">
                <span :class="['status-tag', info.hasCodeartsAk ? 'success' : 'default']">AK {{ info.hasCodeartsAk ? '已配置' : '未配置' }}</span>
                <span :class="['status-tag', info.hasCodeartsSk ? 'success' : 'default']">SK {{ info.hasCodeartsSk ? '已配置' : '未配置' }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 操作栏 -->
      <div style="margin-top:20px;display:flex;gap:12px;justify-content:flex-end">
        <button class="btn btn-danger" @click="resetToken" :disabled="resetting">{{ resetting ? '处理中...' : '强制下线' }}</button>
      </div>
    </div>

    <!-- Toast -->
    <div v-if="toast.show" class="toast-message" :class="toast.type">{{ toast.msg }}</div>
  </div>
</template>

<script setup>
import { onMounted, ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import http from '../api'

const route = useRoute()
const info = ref({})
const loading = ref(true)
const resetting = ref(false)
const showRepos = ref(false)
const toast = ref({ show: false, msg: '', type: 'success' })

const credentialConfigured = computed(() => {
  let count = 0
  if (info.value.hasGitcodeToken) count++
  if (info.value.hasHuaweiAk) count++
  if (info.value.hasFeishu) count++
  if (info.value.hasCodeartsAk) count++
  return count
})

function formatTime(val) {
  if (!val || val === '—') return '—'
  try {
    let ts = val.length > 19 ? val.substring(0, 19) : val
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
</script>

<style scoped>
.detail-grid { display: flex; flex-direction: column; gap: 4px; }
.detail-row { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--border-soft); font-size: 13px; }
.detail-row span:first-child { color: var(--text-tertiary); }

.activity-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }
.activity-card { text-align: center; padding: 16px 8px; background: rgba(0,0,0,0.02); border-radius: 10px; }
.activity-value { font-size: 22px; font-weight: 700; color: var(--text-primary); }
.activity-label { font-size: 11px; color: var(--text-secondary); margin-top: 4px; }

.cred-group-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
.cred-group { background: rgba(0,0,0,0.02); border-radius: 10px; padding: 14px 16px; }
.cred-group-title { font-size: 13px; font-weight: 600; color: var(--text-primary); margin-bottom: 10px; }
.cred-group-body { display: flex; flex-wrap: wrap; gap: 6px; }

.back-btn { display: inline-flex; align-items: center; gap: 4px; cursor: pointer; color: var(--text-secondary); font-size: 13px; transition: color var(--transition); }
.back-btn:hover { color: var(--primary); }

.repo-chip { font-size: 12px; color: var(--primary); background: rgba(0,107,255,0.06); border: none; border-radius: 12px; padding: 4px 12px; text-decoration: none; transition: all var(--transition); }
.repo-chip:hover { background: rgba(0,107,255,0.12); }
</style>
