<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>工作台用户</h1>
      <span class="badge">{{ total }} 个用户</span>
      <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
        <input v-model="keyword" placeholder="用户名搜索" class="toolbar-input" @keyup.enter="load(0)" />
        <button class="btn btn-sm" @click="load(0)">查询</button>
        <button class="btn btn-sm" @click="keyword=''; load(0)">重置</button>
      </div>
    </div>

    <div v-if="loading" class="loading-spinner">加载中...</div>

    <div v-else style="padding:0 40px">
      <!-- KPI 统计卡 -->
      <div class="kpi-row">
        <div class="kpi-card kpi-cyan">
          <div class="kpi-icon" style="background:rgba(48,176,199,0.1);color:#30b0c7;">👥</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ total }}</div>
            <div class="kpi-label">总用户数</div>
          </div>
        </div>
        <div class="kpi-card kpi-blue">
          <div class="kpi-icon" style="background:rgba(0,107,255,0.08);color:#006eff;">🔑</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ gitcodeCount }}</div>
            <div class="kpi-label">GitCode 凭证</div>
          </div>
        </div>
        <div class="kpi-card kpi-orange">
          <div class="kpi-icon" style="background:rgba(255,149,0,0.1);color:#ff9500;">☁️</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ huaweiCount }}</div>
            <div class="kpi-label">华为云凭证</div>
          </div>
        </div>
        <div class="kpi-card kpi-green">
          <div class="kpi-icon" style="background:rgba(52,199,89,0.1);color:#34c759;">💬</div>
          <div class="kpi-body">
            <div class="kpi-value">{{ feishuCount }}</div>
            <div class="kpi-label">飞书凭证</div>
          </div>
        </div>
      </div>

      <!-- 用户表格 -->
      <div class="card" style="margin-top:20px;padding:0;overflow:hidden">
        <div class="section-header">
          <h3 class="sh-title">用户列表</h3>
          <span class="sh-count">共 {{ total }} 条</span>
        </div>
        <table class="vod-table">
          <thead>
            <tr><th style="width:60px">ID</th><th>用户名</th><th>昵称</th><th>GitCode ID</th><th>凭证配置</th><th>创建时间</th><th style="width:60px">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="u in list" :key="u.id">
              <td class="t3">{{ u.id }}</td>
              <td><b>{{ u.username }}</b></td>
              <td class="t3">{{ u.nickname || '—' }}</td>
              <td class="t3">{{ u.gitcodeId ? u.gitcodeId.substring(0, 12) + '…' : '—' }}</td>
              <td>
                <span v-if="u.hasGitcodeToken" class="status-tag info" style="font-size:10px">GitCode</span>
                <span v-if="u.hasHuaweiAk" class="status-tag warning" style="font-size:10px">华为AK</span>
                <span v-if="u.hasFeishu" class="status-tag success" style="font-size:10px">飞书</span>
                <span v-if="!u.hasGitcodeToken && !u.hasHuaweiAk && !u.hasFeishu" class="t3" style="font-size:11px">未配置</span>
              </td>
              <td class="t3">{{ u.createdAt || '—' }}</td>
              <td>
                <button class="act-icon-btn" @click="showDetail(u.id)" title="详情"><svg viewBox="0 0 24 24" width="16" height="16"><path d="M12 5C7 5 3 12 3 12s4 7 9 7 9-7 9-7-4-7-9-7zm0 11a4 4 0 110-8 4 4 0 010 8z" fill="none" stroke="currentColor" stroke-width="2"/></svg></button>
              </td>
            </tr>
            <tr v-if="list.length === 0">
              <td colspan="7" style="text-align:center;padding:32px;color:var(--text-tertiary)">暂无用户数据</td>
            </tr>
          </tbody>
        </table>
        <div class="pagination-bar" v-if="total > 0">
          <span class="page-info">共 <strong>{{ total }}</strong> 条</span>
          <span class="page-info">{{ page + 1 }} / {{ totalPages }}</span>
          <div style="display:flex;gap:8px;align-items:center">
            <select v-model="pageSize" class="page-size-select" @change="load(0)">
              <option v-for="n in [20, 50, 100]" :key="n" :value="n">{{ n }}条/页</option>
            </select>
            <button class="page-btn" :disabled="page <= 0" @click="load(page - 1)">上一页</button>
            <button class="page-btn" :disabled="page >= totalPages - 1" @click="load(page + 1)">下一页</button>
          </div>
        </div>
      </div>
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
import { onMounted, ref, computed } from 'vue'
import http from '../api'

const list = ref([])
const total = ref(0)
const page = ref(0)
const totalPages = ref(0)
const keyword = ref('')
const pageSize = ref(20)
const loading = ref(false)
const detail = ref({ show: false, loading: false, info: {} })

const gitcodeCount = computed(() => list.value.filter(u => u.hasGitcodeToken).length)
const huaweiCount = computed(() => list.value.filter(u => u.hasHuaweiAk).length)
const feishuCount = computed(() => list.value.filter(u => u.hasFeishu).length)

async function load(p) {
  page.value = p
  loading.value = true
  try {
    const { data } = await http.get('/console-users', { params: { page: page.value, size: pageSize.value, keyword: keyword.value } })
    list.value = data.content || []
    total.value = data.totalElements || 0
    totalPages.value = data.totalPages || 0
  } catch (e) {
    console.error('[ConsoleUsers] load failed', e)
  } finally {
    loading.value = false
  }
}

async function showDetail(id) {
  detail.value = { show: true, loading: true, info: {} }
  try {
    const { data } = await http.get(`/console-users/${id}`)
    detail.value = { show: true, loading: false, info: data }
  } catch (e) {
    detail.value = { show: true, loading: false, info: { error: e.message } }
  }
}

onMounted(() => load(0))
</script>

<style scoped>
.toolbar-input {
  width: 200px; padding: 8px 12px;
  border: 1px solid var(--border-color); border-radius: 10px;
  font-size: 13px; outline: none; background: #fff;
  transition: all var(--transition);
}
.toolbar-input:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(0,107,255,0.1); }

.kpi-row { display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 20px; }
.kpi-card {
  flex: 1; min-width: 150px;
  background: #fff; border-radius: 14px; border: 1px solid var(--border-soft);
  padding: 20px; transition: all var(--transition);
  box-shadow: var(--card-shadow);
  display: flex; align-items: center; gap: 14px;
}
.kpi-card:hover { box-shadow: var(--card-shadow-hover); transform: translateY(-2px); }
.kpi-cyan { border-left: 4px solid #30b0c7; }
.kpi-blue { border-left: 4px solid #006eff; }
.kpi-orange { border-left: 4px solid #ff9500; }
.kpi-green { border-left: 4px solid #34c759; }
.kpi-icon { width: 36px; height: 36px; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 18px; flex-shrink: 0; }
.kpi-body { flex: 1; min-width: 0; }
.kpi-value { font-size: 26px; font-weight: 700; color: var(--text-primary); line-height: 1.2; }
.kpi-label { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }

.cu-detail-grid { display: flex; flex-direction: column; gap: 6px; }
.cu-detail-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--border-soft); font-size: 13px; }
.cu-detail-row span:first-child { color: var(--text-tertiary); }
.cu-detail-section { font-size: 13px; font-weight: 600; padding: 12px 0 4px; color: var(--primary); }
.act-icon-btn { background: none; border: none; cursor: pointer; padding: 4px; border-radius: 6px; color: var(--text-secondary); display: inline-flex; align-items: center; transition: all var(--transition); }
.act-icon-btn:hover { background: rgba(0,107,255,0.08); color: var(--primary); }
</style>
