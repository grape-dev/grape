<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>工作台用户</h1>
      <span class="badge">{{ total }} 条</span>
    </div>

    <div style="padding:20px 28px">
      <!-- 统计卡 -->
      <div class="stats">
        <div class="stat-card"><div class="stat-label">用户总数</div><div class="stat-value">{{ stats.total ?? 0 }}</div></div>
        <div class="stat-card"><div class="stat-label">在线</div><div class="stat-value small" style="color:#34c759">{{ stats.online ?? 0 }}</div></div>
        <div class="stat-card"><div class="stat-label">离线</div><div class="stat-value small" style="color:#aeaeb2">{{ stats.offline ?? 0 }}</div></div>
        <div class="stat-card"><div class="stat-label">GitCode 登录</div><div class="stat-value small">{{ stats.gitcodeLogin ?? 0 }}</div></div>
        <div class="stat-card"><div class="stat-label">其他方式</div><div class="stat-value small">{{ stats.other ?? 0 }}</div></div>
      </div>

      <!-- 状态徽章行（点击联动筛选） -->
      <div class="em-stats">
        <button class="em-stat" :class="{ on: !source }" @click="setSource('')">全部 <strong>{{ stats.total ?? 0 }}</strong></button>
        <button class="em-stat" :class="{ on: source === 'online' }" @click="setSource('online')"><span class="em-dot" style="background:#34c759"></span>在线 <strong>{{ stats.online ?? 0 }}</strong></button>
        <button class="em-stat" :class="{ on: source === 'offline' }" @click="setSource('offline')"><span class="em-dot" style="background:#aeaeb2"></span>离线 <strong>{{ stats.offline ?? 0 }}</strong></button>
        <button class="em-stat" :class="{ on: source === 'gitcode' }" @click="setSource('gitcode')"><span class="em-dot" style="background:#006eff"></span>GitCode 登录 <strong>{{ stats.gitcodeLogin ?? 0 }}</strong></button>
        <button class="em-stat" :class="{ on: source === 'other' }" @click="setSource('other')"><span class="em-dot" style="background:#ff9500"></span>其他 <strong>{{ stats.other ?? 0 }}</strong></button>
      </div>

      <!-- 工具栏 -->
      <div class="toolbar">
        <input v-model="keyword" placeholder="用户名/昵称搜索" @keyup.enter="search" />
        <button class="btn" @click="search">查询</button>
        <button class="btn" @click="reset">重置</button>
      </div>

      <!-- 列表 -->
      <div class="card" v-if="!loading">
        <table class="vod-table">
          <thead>
            <tr><th style="width:60px">ID</th><th>用户名</th><th>昵称</th><th>GitCode ID</th><th>登录状态</th><th>凭证配置</th><th>最后活跃</th><th style="width:60px">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="u in list" :key="u.id" class="tl-row-clickable" @click="showDetail(u.id)">
              <td class="t3">{{ u.id }}</td>
              <td><b>{{ u.username }}</b></td>
              <td class="t3">{{ u.nickname || '—' }}</td>
              <td class="t3">{{ u.gitcodeId ? u.gitcodeId.substring(0, 12) + '…' : '—' }}</td>
              <td><span :class="['status-tag', u.online ? 'success' : 'default']" style="font-size:10px">{{ u.online ? '在线' : '离线' }}</span></td>
              <td>
                <span v-if="u.hasGitcodeToken" class="status-tag info" style="font-size:10px">GitCode</span>
                <span v-if="u.hasHuaweiAk" class="status-tag warning" style="font-size:10px">华为AK</span>
                <span v-if="u.hasFeishu" class="status-tag success" style="font-size:10px">飞书</span>
                <span v-if="!u.hasGitcodeToken && !u.hasHuaweiAk && !u.hasFeishu" class="t3" style="font-size:11px">未配置</span>
              </td>
              <td class="t3">{{ u.lastActiveAt || '—' }}</td>
              <td @click.stop>
                <button class="act-icon-btn" @click="showDetail(u.id)" title="详情"><svg viewBox="0 0 24 24" width="16" height="16"><path d="M12 5C7 5 3 12 3 12s4 7 9 7 9-7 9-7-4-7-9-7zm0 11a4 4 0 110-8 4 4 0 010 8z" fill="none" stroke="currentColor" stroke-width="2"/></svg></button>
              </td>
            </tr>
            <tr v-if="list.length === 0">
              <td colspan="8" style="text-align:center;padding:40px;color:var(--text-tertiary)">
                <div style="font-size:32px;margin-bottom:8px">👤</div>
                <div>暂无用户数据</div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="loading" class="loading-spinner">加载中...</div>

      <PaginationBar v-if="total > 0" v-model:page="page" v-model:size="size" :total="total" :total-pages="totalPages" :size-options="sizeOptions" @change="load" />
    </div>

    <!-- 详情弹窗 -->
    <div v-if="detail.show" class="modal-overlay" @click.self="detail.show = false">
      <div class="modal-dialog" style="max-width:560px">
        <div class="modal-header">
          <span class="modal-icon">👤</span>
          <span class="modal-title">{{ detail.info.username }} · 工作台用户详情</span>
        </div>
        <div class="modal-body">
          <div v-if="detail.loading" class="loading-spinner">加载中...</div>
          <div v-else class="cu-detail-grid">
            <div class="cu-detail-row"><span>用户名</span><b>{{ detail.info.username || '—' }}</b></div>
            <div class="cu-detail-row"><span>昵称</span>{{ detail.info.nickname || '—' }}</div>
            <div class="cu-detail-row"><span>GitCode ID</span>{{ detail.info.gitcodeId || '—' }}</div>
            <div class="cu-detail-row"><span>创建时间</span>{{ detail.info.createdAt || '—' }}</div>
            <div class="cu-detail-row"><span>GitCode 发布仓库</span>{{ detail.info.gitcodePublishRepo || '—' }}</div>
            <div class="cu-detail-row"><span>GitHub 发布仓库</span>{{ detail.info.githubPublishRepo || '—' }}</div>
            <div class="cu-detail-row"><span>最后场景</span>{{ detail.info.lastScene || '—' }}</div>
            <div class="cu-detail-section">凭证配置状态</div>
            <div class="cu-detail-row"><span>GitCode Token</span><span :class="['status-tag', detail.info.hasGitcodeToken ? 'success' : 'default']" style="font-size:11px">{{ detail.info.hasGitcodeToken ? '已配置' : '未配置' }}</span></div>
            <div class="cu-detail-row"><span>华为云 AK</span><span :class="['status-tag', detail.info.hasHuaweiAk ? 'success' : 'default']" style="font-size:11px">{{ detail.info.hasHuaweiAk ? '已配置' : '未配置' }}</span></div>
            <div class="cu-detail-row"><span>华为云 SK</span><span :class="['status-tag', detail.info.hasHuaweiSk ? 'success' : 'default']" style="font-size:11px">{{ detail.info.hasHuaweiSk ? '已配置' : '未配置' }}</span></div>
            <div class="cu-detail-row"><span>飞书 App ID</span><span :class="['status-tag', detail.info.hasFeishu ? 'success' : 'default']" style="font-size:11px">{{ detail.info.hasFeishu ? '已配置' : '未配置' }}</span></div>
            <div class="cu-detail-row"><span>飞书 App Secret</span><span :class="['status-tag', detail.info.hasFeishuSecret ? 'success' : 'default']" style="font-size:11px">{{ detail.info.hasFeishuSecret ? '已配置' : '未配置' }}</span></div>
            <div class="cu-detail-row"><span>CodeArts AK</span><span :class="['status-tag', detail.info.hasCodeartsAk ? 'success' : 'default']" style="font-size:11px">{{ detail.info.hasCodeartsAk ? '已配置' : '未配置' }}</span></div>
            <div class="cu-detail-row"><span>CodeArts SK</span><span :class="['status-tag', detail.info.hasCodeartsSk ? 'success' : 'default']" style="font-size:11px">{{ detail.info.hasCodeartsSk ? '已配置' : '未配置' }}</span></div>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn" @click="detail.show = false">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import http from '../api'
import PaginationBar from '../components/PaginationBar.vue'

const list = ref([])
const total = ref(0)
const page = ref(0)
const size = ref(10)
const totalPages = ref(0)
const keyword = ref('')
const source = ref('')
const loading = ref(false)
const detail = ref({ show: false, loading: false, info: {} })
const sizeOptions = [10, 20, 50]
const stats = ref({})

async function load() {
  loading.value = true
  try {
    const { data } = await http.get('/console-users', {
      params: {
        page: page.value,
        size: size.value,
        keyword: keyword.value,
        source: source.value
      }
    })
    list.value = data.content || []
    total.value = data.totalElements || 0
    totalPages.value = data.totalPages || 0
    stats.value = data.stats || {}
  } catch (e) {
    console.error('[ConsoleUsers] load failed', e)
  } finally {
    loading.value = false
  }
}

function search() { page.value = 0; load() }
function reset() { keyword.value = ''; source.value = ''; page.value = 0; load() }
function setSource(s) { source.value = s; page.value = 0; load() }

async function showDetail(id) {
  detail.value = { show: true, loading: true, info: {} }
  try {
    const { data } = await http.get(`/console-users/${id}`)
    detail.value = { show: true, loading: false, info: data }
  } catch (e) {
    detail.value = { show: true, loading: false, info: { error: e.message } }
  }
}

onMounted(() => load())
</script>

<style scoped>
.em-stats { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
.em-stat {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; padding: 4px 12px; border-radius: 12px;
  background: rgba(0,0,0,0.04); color: var(--text-secondary); border: 1px solid transparent;
  cursor: pointer; transition: all var(--transition);
}
.em-stat strong { font-weight: 600; color: var(--text-primary); }
.em-stat.on { background: rgba(0,107,255,0.08); color: var(--primary); border-color: rgba(0,107,255,0.2); }
.em-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }

.cu-detail-grid { display: flex; flex-direction: column; gap: 6px; }
.cu-detail-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--border-soft); font-size: 13px; }
.cu-detail-row span:first-child { color: var(--text-tertiary); }
.cu-detail-section { font-size: 13px; font-weight: 600; padding: 12px 0 4px; color: var(--primary); }
.act-icon-btn { background: none; border: none; cursor: pointer; padding: 4px; border-radius: 6px; color: var(--text-secondary); display: inline-flex; align-items: center; transition: all var(--transition); }
.act-icon-btn:hover { background: rgba(0,107,255,0.08); color: var(--primary); }
.tl-row-clickable { cursor: pointer; transition: background var(--transition); }
</style>
