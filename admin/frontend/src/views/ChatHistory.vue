<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>AI 历史</h1>
      <span class="badge">{{ total }} 条</span>
      <div style="margin-left:auto;display:flex;gap:8px">
        <input v-model="keyword" placeholder="按问题搜索" style="width:180px" @keyup.enter="search" />
        <button class="btn btn-sm" @click="search">搜索</button>
        <button class="btn btn-sm" @click="reset">重置</button>
      </div>
    </div>
    <div style="padding:20px 28px">
      <div class="card">
        <table class="vod-table">
          <thead>
            <tr><th style="width:70px">ID</th><th>问题</th><th>会话 ID</th><th>时间</th><th style="width:200px">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="h in history" :key="h.id">
              <td>{{ h.id }}</td>
              <td class="ellipsis-cell" style="max-width:340px">{{ h.query }}</td>
              <td class="t3" style="font-size:11px">{{ h.sessionId }}</td>
              <td class="t3">{{ h.createdAt }}</td>
              <td>
                <a class="lnk" @click="openDetail(h)">查看</a>
                <a class="lnk" @click="continueChat(h)">继续对话</a>
                <a class="lnk danger" @click="remove(h)">删除</a>
              </td>
            </tr>
            <tr v-if="!history.length">
              <td colspan="5" class="t2" style="padding:20px;text-align:center">暂无历史记录</td>
            </tr>
          </tbody>
        </table>
        <div v-if="total > 0" class="pagination-bar">
          <span class="page-info">第 <strong>{{ page + 1 }}</strong> / {{ totalPages }} 页（共 {{ total }} 条）</span>
          <button class="page-btn" :disabled="page <= 0" @click="page--; load()">上一页</button>
          <button class="page-btn" :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
        </div>
      </div>
    </div>

    <div v-if="detail" class="modal" @click.self="detail = null">
      <div class="modal-box" style="width:640px">
        <h4>问答详情</h4>
        <div class="qa">
          <div class="qa-q"><b>问：</b>{{ detail.query }}</div>
          <div class="qa-a"><b>答：</b><span style="white-space:pre-wrap">{{ detail.answer }}</span></div>
          <p class="t3" style="font-size:11px">会话 {{ detail.sessionId }} ｜ {{ detail.createdAt }}</p>
        </div>
        <div class="modal-actions">
          <button class="btn btn-primary" @click="continueChat(detail)">继续对话</button>
          <button class="btn" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import http from '../api'

const router = useRouter()
const history = ref([])
const keyword = ref('')
const page = ref(0)
const total = ref(0)
const totalPages = ref(0)
const detail = ref(null)

async function load() {
  const { data } = await http.get('/ai/history', { params: { keyword: keyword.value, page: page.value, size: 10 } })
  history.value = data.content
  total.value = data.totalElements
  totalPages.value = data.totalPages
}
function search() { page.value = 0; load() }
function reset() { keyword.value = ''; page.value = 0; load() }
function openDetail(h) { detail.value = h }
function continueChat(h) {
  router.push({ path: '/ai/assistant', query: { session: h.sessionId } })
}
async function remove(h) {
  if (!confirm(`确认删除该条历史？`)) return
  await http.delete(`/ai/history/${h.id}`)
  if (history.value.length === 1 && page.value > 0) page.value--
  load()
}

onMounted(load)
</script>

<style scoped>
.lnk { color: var(--primary); margin-right: 10px; cursor: pointer; font-size: 12px; }
.lnk:hover { text-decoration: underline; }
.lnk.danger { color: var(--danger); }
.qa-q { background: var(--primary); color: #fff; padding: 10px 14px; border-radius: 8px; margin-bottom: 10px; font-size: 12px; }
.qa-a { background: var(--bg-body); padding: 10px 14px; border-radius: 8px; font-size: 12px; line-height: 1.7; }
</style>
