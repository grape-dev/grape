<template>
  <div class="mfd">
    <div v-if="!feedbackId" class="mfd__hint">{{ t('missing_id') }}</div>
    <div v-else-if="loading" class="mfd__hint">{{ t('loading') }}</div>
    <div v-else-if="error" class="mfd__hint">{{ error }}</div>
    <template v-else-if="item">
      <div class="mfd__actions">
        <AGButton variant="plain" size="sm" @click="back">{{ t('back_to_list') }}</AGButton>
        <AGButton variant="danger" size="sm" @click="del($event)">{{ t('delete') }}</AGButton>
      </div>

      <AGCard class="mfd__card">
        <div class="mfd__head">
          <span :class="['mfd__status', `st-${item.status}`]">{{ statusLabel(item.status) }}</span>
          <h2 class="mfd__title">{{ item.title || '—' }}</h2>
        </div>
        <div class="mfd__grid">
          <div>
            <span class="mfd__label">{{ t('field_type') }}</span>
            {{ catIcon(item.category) }} {{ catLabel(item.category) }}
          </div>
          <div>
            <span class="mfd__label">{{ t('field_status') }}</span>
            <span :class="['mfd__status', `st-${item.status}`]">{{
              statusLabel(item.status)
            }}</span>
          </div>
          <!-- Issue #673：反馈转换成的 Issue 链接 + 处理进度 -->
          <div>
            <span class="mfd__label">{{ t('field_issue') }}</span>
            <template v-if="item.issue_url">
              <a :href="item.issue_url" target="_blank" rel="noopener" class="mfd__issue"
                >#{{ item.issue_number || '' }}</a
              >
              <span class="mfd__issue-prog">{{
                t('issue_progress', { status: statusLabel(item.status) })
              }}</span>
            </template>
            <span v-else class="mfd__muted">{{ t('issue_none') }}</span>
          </div>
          <div>
            <span class="mfd__label">{{ t('field_created_at') }}</span>
            {{ fmtTime(item.created_at, 19) }}
          </div>
          <div>
            <span class="mfd__label">{{ t('field_updated_at') }}</span>
            {{ fmtTime(item.updated_at, 19) }}
          </div>
        </div>
      </AGCard>

      <AGCard class="mfd__card">
        <div class="mfd__sec">{{ t('field_description') }}</div>
        <div class="mfd__desc">{{ item.description || '—' }}</div>
      </AGCard>

      <AGCard v-if="item.attachment_url" class="mfd__card">
        <div class="mfd__sec">{{ t('field_attachment') }}</div>
        <div class="mfd__attach">
          <img
            v-if="isImg(item.attachment_url)"
            :src="item.attachment_url"
            :alt="item.attachment_name || t('attachment_label')"
            @click="openAttach"
          />
          <div class="mfd__attach-link">
            <span class="mfd__attach-icon">{{ isImg(item.attachment_url) ? '🖼️' : '📎' }}</span>
            <a :href="item.attachment_url" target="_blank">{{
              item.attachment_name || t('attachment_label')
            }}</a>
          </div>
        </div>
      </AGCard>

      <AGCard v-if="item.handler_name || item.handler_remark" class="mfd__card">
        <div class="mfd__sec">{{ t('field_handler') }}</div>
        <div class="mfd__handler">
          <div v-if="item.handler_name">
            <span class="mfd__label">{{ t('field_handler_name') }}：</span>{{ item.handler_name }}
          </div>
          <div v-if="item.handler_remark">
            <span class="mfd__label">{{ t('field_handler_remark') }}：</span
            >{{ item.handler_remark }}
          </div>
        </div>
      </AGCard>
    </template>
    <div v-else class="mfd__hint">{{ t('not_found') }}</div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
const G = globalThis

import AGCard from '@/components/ui/AGCard.vue'
import AGButton from '@/components/ui/AGButton.vue'

const props = defineProps({ feedbackId: { type: [String, Number], default: '' } })

const item = ref(null)
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
function catLabel(c) {
  const map = {
    bug: t('category_bug'),
    suggestion: t('category_suggestion'),
    question: t('category_question'),
    other: t('category_other'),
  }
  return map[c] || c
}
function isImg(url) {
  return /\.(png|jpg|jpeg|webp|gif)$/i.test(url || '')
}
function fmtTime(s, len) {
  return (s || '').slice(0, len).replace('T', ' ')
}
function back() {
  G.app.nav('my-feedback')
}
function openAttach() {
  window.open(item.value.attachment_url, '_blank')
}
function del(event) {
  G._grapeConfirm(
    {
      title: t('delete_confirm_title'),
      message: t('delete_confirm_msg'),
      confirmText: t('delete'),
      danger: true,
      onConfirm: function () {
        G.API.deleteFeedback(item.value.id)
          .then(function () {
            G.app.nav('my-feedback')
          })
          .catch(function (err) {
            error.value = t('delete_failed', { msg: (err && err.message) || '' })
            item.value = null
          })
      },
    },
    event
  )
}

onMounted(function () {
  if (!props.feedbackId) {
    loading.value = false
    return
  }
  G.API.get('/feedback/mine?page=0&size=100')
    .then(function (resp) {
      const items = resp.items || []
      for (let i = 0; i < items.length; i++) {
        if (String(items[i].id) === String(props.feedbackId)) {
          item.value = items[i]
          break
        }
      }
      loading.value = false
    })
    .catch(function (err) {
      error.value = t('load_failed', { msg: (err && err.message) || '' })
      loading.value = false
    })
})
</script>

<style scoped>
.mfd {
  color: var(--c-text);
  font-family: var(--f-family);
}
.mfd__hint {
  padding: var(--s-5);
  color: var(--c-text-2);
}
.mfd__actions {
  display: flex;
  gap: var(--s-2);
  margin-bottom: var(--s-3);
}
.mfd__card {
  padding: var(--s-5) var(--s-6);
  margin-bottom: var(--s-3);
}
.mfd__head {
  display: flex;
  align-items: center;
  gap: var(--s-2);
  margin-bottom: var(--s-4);
}
.mfd__status {
  font-weight: 600;
  font-size: var(--f-body);
  flex-shrink: 0;
}
.mfd__status.st-open {
  color: var(--c-accent);
}
.mfd__status.st-in_progress {
  color: var(--c-warning);
}
.mfd__status.st-resolved {
  color: var(--c-success);
}
.mfd__status.st-closed {
  color: var(--c-text-3);
}
.mfd__title {
  font-size: 17px;
  font-weight: 600;
  margin: 0;
  flex: 1;
}
.mfd__grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--s-2) var(--s-5);
  font-size: var(--f-caption);
  color: var(--c-text-2);
}
.mfd__label {
  color: var(--c-text-3);
  display: inline-block;
  min-width: 60px;
}
/* Issue #673：关联 Issue 链接与处理进度 */
.mfd__issue {
  color: var(--c-accent, #0071e3);
  font-weight: 600;
  text-decoration: none;
}
.mfd__issue:hover {
  text-decoration: underline;
}
.mfd__issue-prog {
  margin-left: 6px;
  font-size: 11px;
  color: var(--c-text-3);
}
.mfd__muted {
  color: var(--c-text-3);
}
.mfd__sec {
  font-size: var(--f-caption);
  font-weight: 600;
  color: var(--c-text-2);
  margin-bottom: var(--s-2);
}
.mfd__desc {
  font-size: var(--f-body);
  color: var(--c-text);
  white-space: pre-wrap;
  line-height: 1.7;
}
.mfd__attach {
  display: flex;
  align-items: flex-start;
  gap: var(--s-4);
  flex-wrap: wrap;
}
.mfd__attach img {
  max-width: 280px;
  max-height: 280px;
  border-radius: var(--r-control);
  border: 1px solid var(--glass-border);
  cursor: pointer;
  object-fit: contain;
}
.mfd__attach-link {
  display: flex;
  align-items: center;
  gap: var(--s-2);
}
.mfd__attach-icon {
  font-size: 22px;
}
.mfd__attach-link a {
  color: var(--c-accent);
  font-weight: 500;
  text-decoration: none;
}
.mfd__handler {
  font-size: var(--f-caption);
  color: var(--c-text-2);
  line-height: 1.8;
}
</style>
