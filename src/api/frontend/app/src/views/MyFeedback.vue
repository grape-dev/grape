<template>
  <div class="myfb">
    <div class="myfb__bar">
      <h2>{{ t('my_feedback') }}</h2>
      <span class="myfb__count">{{ t('total_count', { n: total }) }}</span>
      <div class="sp"></div>
      <AGButton variant="plain" @click="openNew">{{ t('new_feedback') }}</AGButton>
    </div>

    <div v-if="loading" class="myfb__hint">{{ t('loading') }}</div>
    <div v-else-if="error" class="myfb__hint">{{ error }}</div>

    <AGCard v-else-if="!items.length" class="myfb__empty" :padded="true">
      <div class="myfb__empty-icon">📝</div>
      <div class="myfb__empty-text">{{ t('no_feedback') }}</div>
      <AGButton @click="openNew">{{ t('submit_first') }}</AGButton>
    </AGCard>

    <AGCard v-else :padded="false" class="myfb__list">
      <div v-for="item in items" :key="item.id" class="myfb__row" @click="goDetail(item.id)">
        <div class="myfb__row-head">
          <span :class="['myfb__status', `st-${item.status}`]">{{ statusLabel(item.status) }}</span>
          <span class="myfb__title">{{ item.title || '—' }}</span>
          <!-- Issue #673：反馈转换成需求后展示 Issue 链接（点击不触发整行跳详情） -->
          <a
            v-if="item.issue_url"
            :href="item.issue_url"
            target="_blank"
            rel="noopener"
            class="myfb__issue"
            :title="t('field_issue')"
            @click.stop
            >#{{ item.issue_number || '' }}</a
          >
          <span class="myfb__time">{{ fmtTime(item.created_at, 16) }}</span>
          <AGButton variant="danger" size="sm" class="myfb__del" @click.stop="del(item.id, $event)">
            {{ t('delete') }}
          </AGButton>
        </div>
        <div class="myfb__row-meta">
          <span>{{ catIcon(item.category) }} {{ item.category || '' }}</span>
          <span v-if="item.attachment_url" class="myfb__attach">
            <img v-if="isImg(item.attachment_url)" :src="item.attachment_url" alt="" />
            <span class="myfb__attach-name">{{
              (isImg(item.attachment_url) ? '' : '📎 ') +
              (item.attachment_name || t('attachment_label'))
            }}</span>
          </span>
          <span v-if="item.handler_remark" class="myfb__handler">
            {{ t('field_handler_remark') }}: {{ item.handler_remark.slice(0, 40)
            }}{{ item.handler_remark.length > 40 ? '...' : '' }}
          </span>
        </div>
        <div v-if="item.description" class="myfb__desc">{{ item.description }}</div>
      </div>
    </AGCard>

    <div v-if="totalPages > 1" class="myfb__pager">
      <span>{{ t('page_info', { cur: page + 1, total: totalPages }) }}</span>
      <AGButton variant="plain" size="sm" :disabled="page <= 0" @click="turn(page - 1)">
        {{ t('prev_page') }}
      </AGButton>
      <AGButton
        variant="plain"
        size="sm"
        :disabled="page >= totalPages - 1"
        @click="turn(page + 1)"
      >
        {{ t('next_page') }}
      </AGButton>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
const G = globalThis

import AGCard from '@/components/ui/AGCard.vue'
import AGButton from '@/components/ui/AGButton.vue'

const page = ref(0)
const size = 10
const items = ref([])
const total = ref(0)
const totalPages = ref(1)
const loading = ref(true)
const error = ref('')

function t(key, params) {
  return G.I18N.t('app.feedback.' + key, params)
}
function statusLabel(st) {
  const map = {
    open: t('status_open'),
    in_progress: t('status_in_progress'),
    resolved: t('status_resolved'),
    closed: t('status_closed'),
  }
  return map[st] || st
}
function catIcon(c) {
  return { bug: '🐛', suggestion: '💡', question: '❓', other: '📝' }[c] || '📝'
}
function isImg(url) {
  return /\.(png|jpg|jpeg|webp|gif)$/i.test(url || '')
}
function fmtTime(s, len) {
  return (s || '').slice(0, len).replace('T', ' ')
}
function goDetail(id) {
  G.app.nav('my-feedback-detail', id)
}
function openNew() {
  G.openFeedbackDialog()
}

function load() {
  loading.value = true
  error.value = ''
  G.API.get('/feedback/mine?page=' + page.value + '&size=' + size)
    .then(function (resp) {
      items.value = resp.items || []
      total.value = resp.total || 0
      totalPages.value = resp.total_pages || 1
      loading.value = false
    })
    .catch(function (err) {
      error.value = t('load_failed', { msg: (err && err.message) || '' })
      loading.value = false
    })
}
function turn(p) {
  page.value = p
  load()
}
function del(fid, event) {
  G._grapeConfirm(
    {
      title: t('delete_confirm_title'),
      message: t('delete_confirm_msg'),
      confirmText: t('delete'),
      danger: true,
      onConfirm: function () {
        G.API.deleteFeedback(fid)
          .then(function () {
            load()
          })
          .catch(function (err) {
            error.value = t('delete_failed', { msg: (err && err.message) || '' })
            setTimeout(load, 2000)
          })
      },
    },
    event
  )
}

onMounted(load)
</script>

<style scoped>
.myfb {
  color: var(--c-text);
  font-family: var(--f-family);
}
.myfb__bar {
  display: flex;
  align-items: center;
  gap: var(--s-2);
  margin-bottom: var(--s-4);
}
.myfb__bar h2 {
  font-size: var(--f-head-2);
  font-weight: 600;
  margin: 0;
}
.myfb__count {
  font-size: var(--f-caption);
  color: var(--c-text-3);
}
.sp {
  flex: 1;
}
.myfb__hint {
  padding: var(--s-5);
  color: var(--c-text-2);
}
.myfb__empty {
  padding: var(--s-6);
  text-align: center;
  color: var(--c-text-3);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--s-3);
}
.myfb__empty-icon {
  font-size: 32px;
}
.myfb__empty-text {
  font-size: var(--f-body);
}
.myfb__list {
  overflow: hidden;
}
.myfb__row {
  padding: var(--s-3) var(--s-4);
  border-bottom: 1px solid var(--glass-border);
  cursor: pointer;
  transition: background 0.15s;
}
.myfb__row:last-child {
  border-bottom: none;
}
.myfb__row:hover {
  background: var(--glass-bg);
}
.myfb__row-head {
  display: flex;
  align-items: center;
  gap: var(--s-2);
  margin-bottom: var(--s-1);
}
.myfb__status {
  font-weight: 600;
  font-size: var(--f-caption);
  flex-shrink: 0;
}
.myfb__status.st-open {
  color: var(--c-accent);
}
.myfb__status.st-in_progress {
  color: var(--c-warning);
}
.myfb__status.st-resolved {
  color: var(--c-success);
}
.myfb__status.st-closed {
  color: var(--c-text-3);
}
.myfb__title {
  font-size: var(--f-body);
  font-weight: 600;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.myfb__time {
  font-size: 11px;
  color: var(--c-text-3);
  flex-shrink: 0;
}
/* Issue #673：关联网 Issue 链接 */
.myfb__issue {
  font-size: 12px;
  color: var(--c-accent, #0071e3);
  font-weight: 500;
  text-decoration: none;
  flex-shrink: 0;
}
.myfb__issue:hover {
  text-decoration: underline;
}
.myfb__del {
  flex-shrink: 0;
}
.myfb__row-meta {
  display: flex;
  align-items: center;
  gap: var(--s-2);
  font-size: var(--f-caption);
  color: var(--c-text-2);
}
.myfb__attach {
  display: inline-flex;
  align-items: center;
  gap: var(--s-1);
}
.myfb__attach img {
  width: 24px;
  height: 24px;
  border-radius: 4px;
  object-fit: cover;
}
.myfb__attach-name {
  color: var(--c-text-3);
}
.myfb__handler {
  color: var(--c-text-3);
  margin-left: auto;
}
.myfb__desc {
  font-size: var(--f-caption);
  color: var(--c-text-2);
  margin-top: var(--s-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.myfb__pager {
  display: flex;
  align-items: center;
  gap: var(--s-3);
  padding: var(--s-3) 0;
  font-size: var(--f-caption);
  color: var(--c-text-2);
}
</style>
