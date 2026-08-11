<template>
  <div class="cd">
    <div class="cd-h">
      <h2>AI 历史 <span class="c">{{ total }} 条</span></h2>
      <div class="flex gap8">
        <input v-model="keyword" placeholder="按问题搜索" style="width:180px;padding:5px 10px;font-size:12px;border:1px solid var(--b);border-radius:6px;outline:none" @keyup.enter="search" />
        <button class="bt bt-o bt-s" @click="search">搜索</button>
        <button class="bt bt-o bt-s" @click="reset">重置</button>
      </div>
    </div>
    <div class="tw">
      <table>
        <thead>
          <tr><th>ID</th><th>问题</th><th>会话 ID</th><th>时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="h in history" :key="h.id">
            <td>{{ h.id }}</td>
            <td style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ h.query }}</td>
            <td class="t3" style="font-size:11px">{{ h.sessionId }}</td>
            <td class="t3">{{ h.createdAt }}</td>
            <td>
              <a class="lnk" @click="openDetail(h)">查看</a>
              <a class="lnk" @click="continueChat(h)">继续对话</a>
              <a class="lnk danger" @click="remove(h)">删除</a>
            </td>
          </tr>
          <tr v-if="!history.length">
            <td colspan="5" class="ts t2" style="padding:20px;text-align:center">暂无历史记录</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="total > 0" class="pager">
      第 {{ page + 1 }} / {{ totalPages }} 页（共 {{ total }} 条）
      <button class="bt bt-o bt-s" :disabled="page <= 0" @click="page--; load()">上一页</button>
      <button class="bt bt-o bt-s" :disabled="page >= totalPages - 1" @click="page++; load()">下一页</button>
    </div>
  </div>

  <div v-if="detail" class="modal">
    <div class="modal-box" style="width:640px">
      <h4>问答详情</h4>
      <div class="qa">
        <div class="qa-q"><b>问：</b>{{ detail.query }}</div>
        <div class="qa-a"><b>答：</b><span style="white-space:pre-wrap">{{ detail.answer }}</span></div>
        <p class="t3" style="font-size:11px">会话 {{ detail.sessionId }} ｜ {{ detail.createdAt }}</p>
      </div>
      <div class="modal-actions">
        <button class="bt bt-p" @click="continueChat(detail)">继续对话</button>
        <button class="bt bt-o" @click="detail = null">关闭</button>
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
.lnk { color: var(--a); margin-right: 10px; cursor: pointer; font-size: 12px; }
.lnk:hover { text-decoration: underline; }
.lnk.danger { color: var(--r); }
.qa-q { background: var(--a); color: #fff; padding: 10px 14px; border-radius: 8px; margin-bottom: 10px; font-size: 12px; }
.qa-a { background: var(--bg); padding: 10px 14px; border-radius: 8px; font-size: 12px; line-height: 1.7; }
</style>
