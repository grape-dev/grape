<template>
  <teleport to="body">
    <div v-if="open" id="feedbackOverlay" class="fb-overlay" @click="onOverlayClick">
      <div class="fb-dialog">
        <div class="fb-title">{{ tOr('title', '问题反馈') }}</div>

        <!-- 任务上下文卡片（feedback-task-context：失败事件触发时自动搜集） -->
        <div v-if="contextState === 'loading'" class="fb-ctx fb-ctx-loading">
          📋 {{ tOr('context_loading', '正在获取任务上下文...') }}
        </div>
        <div v-else-if="contextState === 'error'" class="fb-ctx fb-ctx-error">
          ⚠️
          {{
            tOr(
              'context_failed',
              '任务上下文获取失败，可直接填写提交（提交时服务端会再次尝试获取）'
            )
          }}
        </div>
        <div v-else-if="contextState === 'ok' && context" class="fb-ctx">
          <div class="fb-ctx-head">
            📋 {{ tOr('context_title', '任务上下文（自动搜集，只读）') }}
          </div>
          <div class="fb-ctx-row">
            <span class="fb-ctx-label">仓库</span
            ><span class="fb-ctx-val">{{ context.repo_key || '—' }}</span>
          </div>
          <div v-if="context.issue_number" class="fb-ctx-row">
            <span class="fb-ctx-label">Issue</span
            ><span class="fb-ctx-val">#{{ context.issue_number }}</span>
          </div>
          <div v-if="context.agent_type" class="fb-ctx-row">
            <span class="fb-ctx-label">Agent</span
            ><span class="fb-ctx-val">{{ context.agent_type }}</span>
          </div>
          <div class="fb-ctx-row">
            <span class="fb-ctx-label">任务</span><span class="fb-ctx-val">{{ taskLine }}</span>
          </div>
          <div class="fb-ctx-row">
            <span class="fb-ctx-label">引擎</span
            ><span class="fb-ctx-val" v-html="engineLine"></span>
          </div>
          <div v-if="context.fail_summary" class="fb-ctx-summary">
            <b>{{ tOr('context_fail_summary', '失败摘要') }}：</b>
            <span v-if="!summaryExpanded" class="fb-ctx-sum-short"
              >{{ context.fail_summary.substring(0, 200)
              }}{{ context.fail_summary.length > 200 ? '… ' : '' }}</span
            >
            <a
              v-if="context.fail_summary.length > 200 && !summaryExpanded"
              class="fb-ctx-toggle"
              @click.prevent="summaryExpanded = true"
            >
              {{ tOr('context_expand', '展开') }}</a
            >
            <div v-if="summaryExpanded" class="fb-ctx-sum-full">{{ context.fail_summary }}</div>
            <a
              v-if="context.fail_summary.length > 200 && summaryExpanded"
              class="fb-ctx-toggle"
              @click.prevent="summaryExpanded = false"
            >
              {{ tOr('context_collapse', '收起') }}</a
            >
          </div>
        </div>

        <!-- 类型选择 -->
        <div class="fb-cats">
          <button
            v-for="c in categories"
            :key="c.key"
            type="button"
            class="fb-cat-btn"
            :class="{ active: selectedCategory === c.key }"
            @click="selectedCategory = c.key"
          >
            <span class="fb-cat-icon">{{ c.icon }}</span>
            <span>{{ c.label }}</span>
          </button>
        </div>

        <!-- 标题 / 描述 -->
        <input
          id="fbTitle"
          v-model="formTitle"
          type="text"
          :placeholder="tOr('placeholder_title', '一句话概述问题')"
          class="fb-input"
        />
        <textarea
          id="fbDesc"
          v-model="formDesc"
          rows="4"
          :placeholder="tOr('placeholder_desc', '请详细描述遇到的问题、复现步骤、期望行为...')"
          class="fb-textarea"
        ></textarea>

        <!-- 附件 -->
        <div class="fb-file-row">
          <input
            id="fbFile"
            type="file"
            accept="image/*,.zip"
            style="display: none"
            @change="onFileChange"
          />
          <button id="fbFileBtn" type="button" class="fb-file-btn" @click="triggerFile">
            📎 {{ tOr('choose_file', '选择附件') }}
          </button>
          <span id="fbFileName" class="fb-file-name">{{ fileName }}</span>
        </div>

        <!-- 按钮 -->
        <div class="fb-actions">
          <button id="fbCancel" type="button" class="fb-cancel" @click="close">
            {{ tOr('cancel', '取消') }}
          </button>
          <button
            id="fbSubmit"
            type="button"
            class="fb-submit"
            :disabled="submitting"
            @click="submit"
          >
            {{ submitting ? '...' : tOr('submit', '提交反馈') }}
          </button>
        </div>

        <!-- 我的反馈入口 -->
        <div class="fb-myfeedback">
          <a id="fbMyFeedback" @click="goMyFeedback">{{
            tOr('view_my_feedback', '查看我的反馈 →')
          }}</a>
        </div>
      </div>
    </div>
  </teleport>
</template>

<script setup>
// Batch 16 (feedback): equivalent of legacy feedback.js — a global body-level dialog
// (not a page view). Contract: window.openFeedbackDialog(opts) with optional
// { event_seq } for failed-event task-context auto collection.
import { ref } from 'vue'

const G = globalThis

const open = ref(false)
const selectedCategory = ref('bug')
const formTitle = ref('')
const formDesc = ref('')
const fileName = ref('')
const attachmentUrl = ref('')
const attachmentName = ref('')
const contextSeq = ref(0)
const context = ref(null)
const contextState = ref('') // '' | loading | ok | error
const summaryExpanded = ref(false)
const submitting = ref(false)

const categories = [
  { key: 'bug', label: 'Bug 缺陷', icon: '🐛' },
  { key: 'suggestion', label: '功能建议', icon: '💡' },
  { key: 'question', label: '使用疑问', icon: '❓' },
  { key: 'other', label: '其他', icon: '📝' },
]

function t(key) {
  try {
    return G.I18N.t('app.feedback.' + key)
  } catch (e) {
    return ''
  }
}
function tOr(key, fallback) {
  const v = t(key)
  return v === 'app.feedback.' + key || !v ? fallback : v
}
// category labels are dynamic i18n — resolve at open time via method
function catLabel(c) {
  if (c.key === 'bug') return tOr('category_bug', 'Bug 缺陷')
  if (c.key === 'suggestion') return tOr('category_suggestion', '功能建议')
  if (c.key === 'question') return tOr('category_question', '使用疑问')
  return tOr('category_other', '其他')
}

const taskLine = ref('未派发')
const engineLine = ref('未派发')

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function show(opts) {
  opts = opts || {}
  // legacy toggle behavior: clicking again closes the existing dialog
  if (open.value) {
    close()
    return
  }
  contextSeq.value = opts.event_seq || 0
  selectedCategory.value = 'bug'
  formTitle.value = ''
  formDesc.value = ''
  fileName.value = ''
  attachmentUrl.value = ''
  attachmentName.value = ''
  summaryExpanded.value = false
  submitting.value = false
  open.value = true
  // resolve category labels (i18n may load after module init)
  categories.forEach((c) => {
    c.label = catLabel(c)
  })
  if (contextSeq.value) {
    contextState.value = 'loading'
    G.API.get('/feedback/context/' + contextSeq.value)
      .then((ctx) => {
        if (!ctx) {
          contextState.value = 'error'
          return
        }
        context.value = ctx
        contextState.value = 'ok'
        const task = ctx.task || null
        const engine = ctx.engine || null
        taskLine.value = task
          ? esc(
              (task.task_id || '—') +
                (task.task_status
                  ? '（' +
                    task.task_status +
                    (task.exit_code !== null && task.exit_code !== undefined
                      ? '，exit ' + task.exit_code
                      : '') +
                    '）'
                  : '')
            )
          : '未派发'
        engineLine.value = engine
          ? '<b>' +
            esc(engine.runtime || '—') +
            '</b>' +
            (engine.engine_instance ? ' · ' + esc(engine.engine_instance) : '') +
            (engine.engine_ip ? ' · ' + esc(engine.engine_ip) : '') +
            (engine.health_score !== null && engine.health_score !== undefined
              ? ' · 健康分 ' + engine.health_score
              : '')
          : '未派发'
      })
      .catch(() => {
        contextState.value = 'error'
      })
  } else {
    contextState.value = ''
    context.value = null
  }
}

function close() {
  open.value = false
}

function onOverlayClick(e) {
  if (e.target === e.currentTarget) close()
}

function goMyFeedback() {
  close()
  G.app.nav('my-feedback')
}

function triggerFile() {
  const input = document.getElementById('fbFile')
  if (input) input.click()
}

function onFileChange(e) {
  const file = e.target.files[0]
  if (!file) return
  if (file.size > 5 * 1024 * 1024) {
    if (G.app && G.app.showToast)
      G.app.showToast('附件不能超过 5MB（当前 ' + Math.round(file.size / 1024) + 'KB）')
    e.target.value = ''
    return
  }
  fileName.value = file.name + '（上传中...）'
  const fd = new FormData()
  fd.append('file', file)
  G.API.upload('/feedback/upload', fd)
    .then((resp) => {
      if (resp && resp.error) {
        attachmentUrl.value = ''
        fileName.value = ''
        if (G.app && G.app.showToast) G.app.showToast('上传失败: ' + resp.error)
      } else {
        attachmentUrl.value = resp.url || ''
        attachmentName.value = resp.original_name || file.name || ''
        fileName.value = attachmentName.value + ' ✓'
      }
    })
    .catch((err) => {
      attachmentUrl.value = ''
      let msg = '上传失败'
      if (err && err.message) msg = '上传失败(' + err.message + ')'
      fileName.value = msg
      if (G.app && G.app.showToast) G.app.showToast(msg)
    })
  e.target.value = ''
}

function submit() {
  const title = formTitle.value.trim()
  const desc = formDesc.value.trim()
  if (!title) {
    if (G.app && G.app.showToast) G.app.showToast(tOr('placeholder_title', '请输入标题'))
    const el = document.getElementById('fbTitle')
    if (el) el.focus()
    return
  }
  if (!desc) {
    if (G.app && G.app.showToast) G.app.showToast(tOr('placeholder_desc', '请输入描述'))
    const el = document.getElementById('fbDesc')
    if (el) el.focus()
    return
  }
  let repoKey = ''
  try {
    const r = typeof G.app !== 'undefined' && G.app.currentRepo ? G.app.currentRepo() : null
    if (r) repoKey = (r.owner || '') + '/' + (r.name || '')
  } catch (e) {
    /* ignore */
  }
  submitting.value = true
  G.API.post('/feedback', {
    category: selectedCategory.value,
    title,
    description: desc,
    page_url: window.location.hash || '',
    repo_key: repoKey,
    attachment_url: attachmentUrl.value || '',
    attachment_name: attachmentName.value || '',
    event_seq: contextSeq.value || 0,
  })
    .then((resp) => {
      if (resp && resp.error) {
        if (G.app && G.app.showToast) G.app.showToast(resp.error)
        submitting.value = false
        return
      }
      close()
      if (G.app && G.app.showToast) G.app.showToast(tOr('success', '反馈已提交，感谢您的反馈！'))
    })
    .catch((err) => {
      const msg = (err && err.message) || tOr('failed', '反馈提交失败，请稍后重试')
      if (G.app && G.app.showToast) G.app.showToast(msg)
      submitting.value = false
    })
}

defineExpose({ show, close })
</script>

<style scoped>
.fb-overlay {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: rgba(0, 0, 0, 0.25);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  z-index: 1001;
  opacity: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
.fb-dialog {
  width: 480px;
  max-width: 92vw;
  max-height: 90vh;
  overflow-y: auto;
  background: var(--c, #fff);
  border-radius: 16px;
  padding: 24px 28px;
  box-shadow: 0 12px 50px rgba(0, 0, 0, 0.18);
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', sans-serif;
}
.fb-title {
  font-size: 17px;
  font-weight: 600;
  color: var(--t, #1d1d1f);
  margin-bottom: 16px;
}
.fb-ctx {
  background: rgba(0, 0, 0, 0.04);
  border-radius: 10px;
  padding: 10px 14px;
  margin-bottom: 16px;
  font-size: 12px;
}
.fb-ctx-loading {
  color: var(--t3, #86868b);
}
.fb-ctx-error {
  background: rgba(255, 59, 48, 0.06);
  color: var(--r);
}
.fb-ctx-head {
  font-size: 12px;
  font-weight: 600;
  color: var(--t2, #3a3a3c);
  margin-bottom: 4px;
}
.fb-ctx-row {
  display: flex;
  gap: 8px;
  line-height: 1.7;
}
.fb-ctx-label {
  color: var(--t3, #86868b);
  flex: 0 0 48px;
  font-size: 12px;
}
.fb-ctx-val {
  color: var(--t, #1d1d1f);
  font-size: 12px;
  word-break: break-all;
}
.fb-ctx-summary {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed rgba(0, 0, 0, 0.1);
  font-size: 12px;
  color: var(--t2, #3a3a3c);
  line-height: 1.6;
}
.fb-ctx-sum-full {
  white-space: pre-wrap;
}
.fb-ctx-toggle {
  color: var(--a-text);
  text-decoration: none;
  cursor: pointer;
  font-weight: 500;
  white-space: nowrap;
}
.fb-cats {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}
.fb-cat-btn {
  flex: 1;
  padding: 10px 4px;
  border-radius: 10px;
  border: 1.5px solid var(--b, rgba(0, 0, 0, 0.1));
  background: var(--c, #fff);
  font-size: 12px;
  font-weight: 500;
  color: var(--t2, #3a3a3c);
  cursor: pointer;
  font-family: inherit;
  transition: all 0.15s;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}
.fb-cat-btn.active {
  border-color: var(--a-text);
  background: rgba(0, 113, 227, 0.06);
  color: var(--a-text);
}
.fb-cat-icon {
  font-size: 18px;
}
.fb-input {
  width: 100%;
  box-sizing: border-box;
  padding: 10px 14px;
  border-radius: 10px;
  border: 1.5px solid var(--b, rgba(0, 0, 0, 0.12));
  background: var(--c, #fff);
  font-size: 14px;
  color: var(--t, #1d1d1f);
  font-family: inherit;
  margin-bottom: 12px;
  outline: none;
}
.fb-textarea {
  width: 100%;
  box-sizing: border-box;
  padding: 10px 14px;
  border-radius: 10px;
  border: 1.5px solid var(--b, rgba(0, 0, 0, 0.12));
  background: var(--c, #fff);
  font-size: 13px;
  color: var(--t, #1d1d1f);
  font-family: inherit;
  margin-bottom: 12px;
  outline: none;
  resize: vertical;
  line-height: 1.5;
}
.fb-file-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
}
.fb-file-btn {
  padding: 8px 14px;
  border-radius: 8px;
  border: 1.5px solid var(--b, rgba(0, 0, 0, 0.12));
  background: var(--c, #fff);
  font-size: 12px;
  font-weight: 500;
  color: var(--t2, #3a3a3c);
  cursor: pointer;
  font-family: inherit;
}
.fb-file-name {
  font-size: 12px;
  color: var(--t3, #86868b);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}
.fb-actions {
  display: flex;
  gap: 10px;
  justify-content: flex-end;
}
.fb-cancel {
  padding: 9px 20px;
  border-radius: 10px;
  border: none;
  background: var(--b, rgba(0, 0, 0, 0.06));
  font-size: 13px;
  font-weight: 500;
  color: var(--t, #1d1d1f);
  cursor: pointer;
  font-family: inherit;
}
.fb-submit {
  padding: 9px 24px;
  border-radius: 10px;
  border: none;
  background: var(--a);
  font-size: 13px;
  font-weight: 600;
  color: var(--on-accent);
  cursor: pointer;
  font-family: inherit;
}
.fb-submit:disabled {
  opacity: 0.6;
}
.fb-myfeedback {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--b, rgba(0, 0, 0, 0.08));
  text-align: center;
}
.fb-myfeedback a {
  font-size: 12px;
  color: var(--a-text);
  text-decoration: none;
  cursor: pointer;
  font-weight: 500;
}
</style>
