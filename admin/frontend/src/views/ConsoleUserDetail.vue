<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <button class="btn btn-sm" @click="$router.push('/sys/console')">← 返回</button>
      <h1>{{ info.username || '用户详情' }}</h1>
      <span v-if="info.online" class="status-tag success">在线</span>
      <span v-else class="status-tag default">离线</span>
    </div>

    <div v-if="loading" class="loading-spinner">加载中...</div>

    <div v-else style="padding:20px 40px">
      <!-- 基本信息 -->
      <div class="card" style="margin-bottom:20px">
        <div class="section-header">
          <h3 class="sh-title">基本信息</h3>
        </div>
        <div style="padding:20px 24px">
          <div class="detail-grid">
            <div class="detail-row"><span>用户名</span><b>{{ info.username || '—' }}</b></div>
            <div class="detail-row"><span>昵称</span>{{ info.nickname || '—' }}</div>
            <div class="detail-row"><span>GitCode ID</span>{{ info.gitcodeId || '—' }}</div>
            <div class="detail-row"><span>登录状态</span><span :class="['status-tag', info.online ? 'success' : 'default']">{{ info.online ? '在线' : '离线' }}</span></div>
            <div class="detail-row"><span>最后活跃</span>{{ info.lastActiveAt || '—' }}</div>
            <div class="detail-row"><span>创建时间</span>{{ info.createdAt || '—' }}</div>
            <div class="detail-row"><span>最后场景</span>{{ info.lastScene || '—' }}</div>
            <div class="detail-row"><span>GitCode 发布仓库</span>{{ info.gitcodePublishRepo || '—' }}</div>
            <div class="detail-row"><span>GitHub 发布仓库</span>{{ info.githubPublishRepo || '—' }}</div>
          </div>
        </div>
      </div>

      <!-- 凭证配置 -->
      <div class="card">
        <div class="section-header">
          <h3 class="sh-title">凭证配置</h3>
        </div>
        <div style="padding:20px 24px">
          <div class="detail-grid">
            <div class="detail-row"><span>GitCode Token</span><span :class="['status-tag', info.hasGitcodeToken ? 'success' : 'default']">{{ info.hasGitcodeToken ? '已配置' : '未配置' }}</span></div>
            <div class="detail-row"><span>华为云 AK</span><span :class="['status-tag', info.hasHuaweiAk ? 'success' : 'default']">{{ info.hasHuaweiAk ? '已配置' : '未配置' }}</span></div>
            <div class="detail-row"><span>华为云 SK</span><span :class="['status-tag', info.hasHuaweiSk ? 'success' : 'default']">{{ info.hasHuaweiSk ? '已配置' : '未配置' }}</span></div>
            <div class="detail-row"><span>飞书 App ID</span><span :class="['status-tag', info.hasFeishu ? 'success' : 'default']">{{ info.hasFeishu ? '已配置' : '未配置' }}</span></div>
            <div class="detail-row"><span>飞书 App Secret</span><span :class="['status-tag', info.hasFeishuSecret ? 'success' : 'default']">{{ info.hasFeishuSecret ? '已配置' : '未配置' }}</span></div>
            <div class="detail-row"><span>CodeArts AK</span><span :class="['status-tag', info.hasCodeartsAk ? 'success' : 'default']">{{ info.hasCodeartsAk ? '已配置' : '未配置' }}</span></div>
            <div class="detail-row"><span>CodeArts SK</span><span :class="['status-tag', info.hasCodeartsSk ? 'success' : 'default']">{{ info.hasCodeartsSk ? '已配置' : '未配置' }}</span></div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import http from '../api'

const route = useRoute()
const info = ref({})
const loading = ref(true)

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

onMounted(() => load())
</script>

<style scoped>
.detail-grid { display: flex; flex-direction: column; gap: 4px; }
.detail-row { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--border-soft); font-size: 13px; }
.detail-row span:first-child { color: var(--text-tertiary); }
</style>
