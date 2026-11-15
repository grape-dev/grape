<template>
  <div class="cv">
    <div class="chat-wrap cv__wrap">
      <!-- 会话列表 -->
      <div id="convList" class="chat-convlist">
        <div class="cv__newbar">
          <button class="bt bt-p bt-s cv__newbtn" @click="newConversation">
            {{ t('conversation.new') }}
          </button>
        </div>
        <div
          v-for="c in convs"
          :key="c.id"
          :class="['conv-item', { cv__active: c.id === currentConvId }]"
          @click="switchConversation(c.id)"
        >
          <div class="conv-title">{{ convTitle(c) }}</div>
          <div class="conv-meta">{{ convMeta(c) }}</div>
          <span class="conv-del" @click.stop="askDeleteConv(c, $event)">✕</span>
        </div>
        <div v-if="!convs.length" class="cv__emptylist">{{ t('conversation.empty_hint') }}</div>
      </div>

      <!-- 主区 -->
      <div class="chat-main">
        <div class="chat-hdr">
          <h2><span v-html="hdrTitle"></span></h2>
          <span v-show="engineBadge" class="chat-engine-badge">{{ engineBadge }}</span>
        </div>
        <div id="chatBody" ref="chatBody" class="chat-body">
          <div v-if="bodyLoading" class="cv__loading">{{ tc('loading') }}</div>
          <div v-else-if="bodyError" class="error">{{ t('load_failed') }}</div>
          <div v-else-if="!messages.length" class="cv__starthint">
            {{ hasConv ? t('conversation.start_new') : t('conversation.start_hint') }}
          </div>
          <template v-else>
            <div
              v-for="(m, i) in messages"
              :key="m.id || i"
              :class="['msg', m.role === 'user' ? 'r' : 'l', m.recalled ? 'chat-msg-recalled' : '']"
              :data-msg-id="m.id || ''"
            >
              <!-- Issue #442：已撤回 → 原地提示 + 重新编辑；撤回入口只在有 id 的用户消息上悬停出现 -->
              <template v-if="m.recalled">
                <span>{{ t('recall.hint') }}</span>
                <a v-if="m.id" href="javascript:void(0)" @click.prevent="reEditRecalled(m)">{{
                  t('recall.re_edit')
                }}</a>
              </template>
              <template v-else>
                <button
                  v-if="m.role === 'user' && m.id"
                  class="msg-recall-btn"
                  type="button"
                  :title="t('recall.action')"
                  @click.stop="recallMessage(m, i)"
                >
                  ↺ {{ t('recall.action') }}
                </button>
                <!-- eslint-disable-next-line vue/no-v-html — 老代码用 renderMarkdown+chat_img 替换产出 HTML -->
                <span v-html="renderMsg(m)"></span>
              </template>
            </div>
          </template>
          <!-- 进行中任务的流式气泡 -->
          <div v-if="streaming" :id="streaming.elId" class="msg l">
            <div v-if="streaming.stepText" class="chat-ticker">{{ streaming.stepText }}</div>
            <span class="chat-dots"><span></span><span></span><span></span></span>
          </div>
        </div>

        <!-- 图片/视频预览区 -->
        <div v-show="pendingImages.length || pendingVideo" id="chatImgPreview" class="cv__preview">
          <div v-for="(img, i) in pendingImages" :key="'img' + i" class="cv__pv-item">
            <img :src="img.data" @click="G.window.open(img.data, '_blank')" />
            <button class="cv__pv-x" :title="t('remove_image')" @click="removeImage(i)">✕</button>
          </div>
          <div
            v-if="pendingImages.length && pendingImages.length < MAX_IMAGES"
            class="cv__pv-count"
          >
            {{ pendingImages.length + '/' + MAX_IMAGES }}
          </div>
          <div v-if="pendingVideo" class="cv__pv-item" data-video-preview="1">
            <div
              class="cv__pv-video"
              :title="pendingVideo.name"
              @click="G.window.open(pendingVideo.data, '_blank')"
            >
              <svg
                width="24"
                height="24"
                viewBox="0 0 24 24"
                fill="none"
                stroke="var(--t2)"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <polygon points="23 7 16 12 23 17 23 7" />
                <rect x="1" y="5" width="15" height="14" rx="2" ry="2" />
              </svg>
            </div>
            <button class="cv__pv-x" @click="removeVideo">✕</button>
          </div>
        </div>

        <!-- 输入区 -->
        <div class="chat-in">
          <label class="chat-img-btn cv__tool-btn" :title="t('upload_image')">
            <svg
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
              <circle cx="8.5" cy="8.5" r="1.5" />
              <polyline points="21 15 16 10 5 21" />
            </svg>
            <input
              type="file"
              accept="image/*"
              multiple
              class="cv__hide"
              @change="onImageSelect($event)"
            />
          </label>
          <label class="chat-img-btn cv__tool-btn" :title="t('video_tooltip')">
            <svg
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <polygon points="23 7 16 12 23 17 23 7" />
              <rect x="1" y="5" width="15" height="14" rx="2" ry="2" />
            </svg>
            <input type="file" accept="video/*" class="cv__hide" @change="onVideoSelect($event)" />
          </label>
          <textarea
            id="chatInput"
            v-model="draft"
            rows="1"
            :placeholder="t('placeholder')"
            @input="onChatInput"
            @keydown.enter.exact.prevent="sendChatStream"
          ></textarea>
          <button
            id="chatModelBtn"
            :class="['chat-inpicker-btn', { active: pickerOpen === 'model' }]"
            :title="t('engine_config_tip')"
            @click="togglePicker('model')"
          >
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <rect x="4" y="4" width="16" height="16" rx="2" />
              <rect x="9" y="9" width="6" height="6" />
              <line x1="9" y1="2" x2="9" y2="4" />
              <line x1="15" y1="2" x2="15" y2="4" />
              <line x1="9" y1="20" x2="9" y2="22" />
              <line x1="15" y1="20" x2="15" y2="22" />
              <line x1="20" y1="9" x2="22" y2="9" />
              <line x1="20" y1="14" x2="22" y2="14" />
              <line x1="2" y1="9" x2="4" y2="9" />
              <line x1="2" y1="14" x2="4" y2="14" />
            </svg>
          </button>
          <button
            id="chatSkillBtn"
            :class="['chat-inpicker-btn', { active: pickerOpen === 'skill' }]"
            :title="t('skill_config_tip')"
            @click="togglePicker('skill')"
          >
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path d="M12 2L2 7l10 5 10-5-10-5z" />
              <path d="M2 17l10 5 10-5" />
              <path d="M2 12l10 5 10-5" />
            </svg>
          </button>
          <button class="bt bt-p" @click="sendChatStream">{{ t('send') }}</button>
        </div>
      </div>
    </div>

    <!-- ═══ 引擎/技能选择浮层 ═══ -->
    <Teleport to="body">
      <div v-if="pickerOpen" class="chat-picker-overlay" @click.self="closePicker">
        <div v-if="pickerOpen === 'model'" class="chat-picker-pop cv__pop">
          <div class="cv__pop-label">{{ t('engine_label') }}</div>
          <div class="cv__pop-list">
            <div v-if="!backends.length" class="cv__pop-hint">{{ tc('loading') }}</div>
            <div
              v-for="b in backends"
              :key="(b.runtime_id || '') + b.name"
              :class="['chat-picker-item', { active: isBackendActive(b) }]"
              @click="saveModelConfig(b)"
            >
              <span class="cv__pop-ic">{{ b.scope === 'local' ? '🖥' : '☁' }}</span>
              <span class="cv__pop-name">{{ b.display_name || b.name }}</span>
              <span v-if="isBackendActive(b)" class="cv__pop-cur">
                {{ t('current_engine') }}
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="var(--a)"
                  stroke-width="3"
                >
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </span>
            </div>
          </div>
        </div>
        <div v-else class="chat-picker-pop cv__pop">
          <div class="cv__pop-label">{{ t('skill_label') }}</div>
          <div class="cv__scope-tabs">
            <button
              v-for="s in ['official', 'repo', 'user']"
              :key="s"
              :class="['cv__scope-tab', { active: skillScope === s }]"
              @click="switchSkillScope(s)"
            >
              {{ scopeLabel(s) }}
            </button>
          </div>
          <div class="cv__pop-list">
            <div v-if="skillLoading" class="cv__pop-hint">{{ tc('loading') }}</div>
            <div v-else-if="!skillItems.length && skillScope !== 'official'" class="cv__pop-hint">
              {{ skillScope === 'repo' ? t('skill.no_repo_skill') : t('skill.no_personal_skill') }}
            </div>
            <template v-if="skillScope === 'official'">
              <div
                :class="['chat-picker-item', { active: !configData.skill_id }]"
                @click="saveSkillConfig(0)"
              >
                <span class="cv__pop-name">{{ t('skill.default_official') }}</span>
                <svg
                  v-if="!configData.skill_id"
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="var(--a)"
                  stroke-width="3"
                >
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </div>
            </template>
            <div
              v-for="s in skillItems"
              :key="s.id"
              :class="[
                'chat-picker-item',
                { active: String(s.id) === String(configData.skill_id) },
              ]"
              @click="saveSkillConfig(s.id)"
            >
              <span class="cv__pop-name">{{ s.display_name || s.name }}</span>
              <svg
                v-if="String(s.id) === String(configData.skill_id)"
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="var(--a)"
                stroke-width="3"
              >
                <polyline points="20 6 9 17 4 12" />
              </svg>
            </div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ═══ 会话删除确认气泡 ═══ -->
    <Teleport to="body">
      <div v-if="delPopover.open" class="chat-del-overlay" @click.self="closeDelPopover">
        <div class="chat-del-pop cv__delpop" :style="delPopover.pos">
          <div class="chat-del-title">{{ t('conversation.delete_confirm') }}</div>
          <div class="chat-del-btns">
            <button class="chat-del-cancel" @click="closeDelPopover">{{ tc2('cancel') }}</button>
            <button class="chat-del-ok" @click="doDeleteConv">{{ tc2('confirm') }}</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ═══ 飞书扫码配置弹窗 ═══ -->
    <Teleport to="body">
      <div v-if="onboard.open" class="cv__onboard-mask" @click.self="cancelOnboard">
        <div class="cv__onboard">
          <div class="cv__onboard-hdr">
            <b>{{ t('onboard.modal_title') }}</b>
            <span class="cv__onboard-x" @click="cancelOnboard">✕</span>
          </div>
          <div class="cv__onboard-body">
            <template v-if="onboard.status === 'pending'">{{
              t('onboard.generating_qr')
            }}</template>
            <template v-else-if="onboard.status === 'waiting_confirm' && onboard.qrUrl">
              <div class="cv__qr-wrap"><div :id="'onboardQr' + uid" class="cv__qr"></div></div>
              <div class="cv__onboard-t">{{ t('onboard.step1_scan') }}</div>
              <div class="cv__onboard-t3">{{ t('onboard.step2_confirm') }}</div>
              <div :id="'onboardCountdown' + uid" class="cv__onboard-pk">
                {{ t('onboard.waiting_confirm') }}
              </div>
              <div class="cv__onboard-warn">{{ t('onboard.close_warning') }}</div>
            </template>
            <template v-else-if="onboard.status === 'connected'">
              <div class="cv__onboard-big">✅</div>
              <div class="cv__onboard-t">{{ t('onboard.success_title') }}</div>
              <div class="cv__onboard-t2">
                App ID: <span class="cv__mono">{{ onboard.appId || '' }}</span>
              </div>
              <div class="cv__onboard-t2">{{ t('onboard.secret_saved') }}</div>
              <div class="cv__onboard-pk">{{ t('onboard.bot_connected') }}</div>
              <button class="bt bt-p bt-s cv__onboard-btn" @click="closeOnboardModal(true)">
                {{ t('onboard.close') }}
              </button>
            </template>
            <template v-else-if="onboard.status === 'failed'">
              <div class="cv__onboard-big">❌</div>
              <div class="cv__onboard-r">{{ t('onboard.failed_title') }}</div>
              <div class="cv__onboard-t2">{{ onboardErrorMsg }}</div>
              <div class="cv__onboard-btns">
                <button class="bt bt-o bt-s" @click="restartOnboard">
                  {{ t('onboard.rescan') }}
                </button>
                <button class="bt bt-p bt-s" @click="closeOnboardModal()">
                  {{ t('onboard.cancel') }}
                </button>
              </div>
            </template>
            <template v-else>{{ t('onboard.generating_qr') }}</template>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'

const G = globalThis
const uid = Math.random().toString(36).slice(2, 8)

// ── I18N（chat.* 命名空间；规避 t(key) 返回 key 本身的陷阱）──
function t(key, params) {
  const full = 'chat.' + key
  const v = G.I18N.t(full, params)
  return v && v !== full ? v : DEFAULTS[key] || key
}
function tc(key) {
  const full = 'common.' + key
  const v = G.I18N.t(full)
  return v && v !== full
    ? v
    : key === 'loading'
      ? '加载中...'
      : key === 'cancel'
        ? '取消'
        : key === 'confirm'
          ? '确认'
          : key
}
function tc2(key) {
  const v = G.I18N.t('common.' + key)
  return v && v !== 'common.' + key ? v : key
}
const DEFAULTS = {
  'header.title': 'Chat Agent',
  placeholder: '输入消息，Enter 发送，Shift+Enter 换行',
  send: '发送',
  upload_image: '上传图片',
  video_tooltip: '上传视频（≤50MB）',
  remove_image: '移除图片',
  engine_config_tip: '切换引擎',
  skill_config_tip: '选择技能',
  engine_label: '引擎',
  skill_label: '技能',
  current_engine: '当前',
  'skill.default_official': '默认（官方技能）',
  'skill.no_repo_skill': '此仓库暂无 Chat 技能',
  'skill.no_personal_skill': '暂无个人 Chat 技能',
  'conversation.new': '＋ 新会话',
  'conversation.default_title': '新会话',
  'conversation.empty_hint': '暂无会话',
  'conversation.start_hint': '选择或创建一个会话开始',
  'conversation.start_new': '开始新的对话',
  'conversation.delete_confirm': '删除此会话？',
  'conversation.deleted': '已删除',
  'conversation.create_conv_failed': '创建会话失败',
  'conversation.create_failed': '创建会话失败',
  'conversation.delete_failed': '删除失败',
  no_reply: '（无回复）',
  load_failed: '加载失败',
  'config.saved': '已保存',
  'config.save_failed': '保存失败',
  // Issue #442：撤回
  'recall.action': '撤回',
  'recall.confirm': '撤回会删除该消息，并停止 agent 的执行',
  'recall.hint': '消息已撤回',
  'recall.re_edit': '重新编辑',
  'recall.failed': '撤回失败: {msg}',
  'skill.select_required': '请选择一个技能',
  image_max_reached: '最多 {max} 张图片',
  image_max_exceeded: '最多 {max} 张图片，超出部分已忽略',
  image_too_large: '图片/视频过大',
  'onboard.modal_title': '扫码配置飞书机器人',
  'onboard.generating_qr': '正在生成二维码...',
  'onboard.step1_scan': '1. 用飞书扫码',
  'onboard.step2_confirm': '2. 在飞书中确认授权',
  'onboard.waiting_confirm': '等待确认...',
  'onboard.close_warning': '关闭窗口将取消本次配置',
  'onboard.success_title': '配置成功',
  'onboard.secret_saved': 'App Secret 已安全保存',
  'onboard.bot_connected': '机器人已连接',
  'onboard.close': '完成',
  'onboard.failed_title': '配置失败',
  'onboard.rescan': '重新扫码',
  'onboard.cancel': '取消',
  'onboard.qr_expired': '二维码已过期',
  'onboard.error_access_denied': '授权被拒绝',
  'onboard.error_unknown': '未知错误',
  'onboard.confirm_close': '本次扫码配置尚未完成，关闭将取消配置，确认关闭？',
  'onboard.confirm_restart':
    '当前已有飞书机器人正在配置中，是否放弃并重新开始配置？（旧配置不会保存）',
  'onboard.confirm_rebind':
    '当前已绑定飞书机器人（{appId}）。更换后将使旧配置失效且不可恢复，确认继续扫码换绑？',
  'onboard.start_failed': '启动配置失败',
  'onboard.waiting_countdown': '等待确认（{time}）',
}
function scopeLabel(s) {
  const key = 'chat.scope_short.' + (s === 'official' ? 'global' : s)
  const v = G.I18N.t(key)
  return v && v !== key ? v : s === 'official' ? '全局' : s === 'repo' ? '仓库' : '个人'
}

// ── 老壳共享工具 ──
function fmtLocalTime(utcStr) {
  return G.window.fmtLocalTime ? G.window.fmtLocalTime(utcStr) : String(utcStr || '')
}
function vcsUrl(repoKey, path) {
  return G.window._vcsUrl ? G.window._vcsUrl(repoKey, path) : ''
}
function renderMarkdown(text) {
  return G.window.renderMarkdown ? G.window.renderMarkdown(text) : String(text || '')
}

// Issue 前缀剥离 + #N 链接化（与老 _stripIssuePrefix / linkifyIssueNumbers 等价）
function stripIssuePrefix(title) {
  // 后端标题有两种写法：「[Issue #12] 摘要」和「Issue #12 · 摘要」，
  // 两种都要剥掉，否则列表里会先出现「Issue #12」再被 meta 的「#12」重复一遍。
  const m = String(title || '').match(/^\[?Issue\s*#(\d+)\]?\s*(?:[·:：\-—]\s*)?(.*)$/i)
  return m
    ? { issueNumber: parseInt(m[1], 10), title: (m[2] || '').trim() }
    : { issueNumber: 0, title: String(title || '') }
}
function esc(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}
function linkifyIssueNumbers(safeHtml, repoKey) {
  return String(safeHtml || '').replace(/#(\d{1,6})\b/g, function (m, num) {
    const url = vcsUrl(repoKey || '', '/issues/' + num)
    return url ? '<a href="' + url + '" target="_blank" rel="noopener">#' + num + '</a>' : m
  })
}

// ── 会话状态 ──
const props = defineProps({ openConvId: { type: String, default: '' } })
const convs = ref([])
const currentConvId = ref(null)
const messages = ref([])
const bodyLoading = ref(false)
const bodyError = ref(false)
const draft = ref('')
const hdrTitle = ref('')
const engineBadge = ref('')
const hasConv = computed(() => !!currentConvId.value)

// props.openConvId 是外部导航参数：conv_id 直连 或 issue:N:repoKey
let openTarget = props.openConvId || null
let issueParam = null
if (openTarget && openTarget.startsWith('issue:')) {
  const parts = openTarget.split(':')
  if (parts.length >= 3) issueParam = { num: parts[1], repo: parts.slice(2).join(':') }
  openTarget = null
}

function currentRepo() {
  try {
    const c = G.app.currentRepo()
    return c || null
  } catch (e) {
    return null
  }
}
function repoKeyOf() {
  const c = currentRepo()
  return c ? c.repoKey || c.fullName || '' : ''
}
function convTitle(c) {
  const s = stripIssuePrefix(c.title || '')
  // 只有 Issue 号、没有摘要的会话（标题就是「Issue #12」）→ 标题直接用「Issue #12」，
  // 这时 meta 不再重复号；有摘要则标题只放摘要。
  if (s.issueNumber && !s.title) return 'Issue #' + s.issueNumber
  return s.title || (c.title ? '' : t('conversation.default_title')) || 'Issue #' + s.issueNumber
}
function convMeta(c) {
  const s = stripIssuePrefix(c.title || '')
  const issueNum = c.issue_number && c.issue_number > 0 ? c.issue_number : s.issueNumber
  const ts = fmtLocalTime(c.created_at).substring(0, 16)
  // 标题已经显示 Issue 号时不再重复
  if (issueNum > 0 && s.title) return '#' + issueNum + ' · ' + ts
  return ts
}

// ── 草稿持久化（Issue #407，sessionStorage 1h）──
const DRAFT_TTL = 3600000
function draftKey(convId) {
  return 'chatDraft_' + convId
}
function saveDraft(convId, text) {
  if (!convId) return
  try {
    if (!text) {
      G.window.sessionStorage.removeItem(draftKey(convId))
      return
    }
    G.window.sessionStorage.setItem(
      draftKey(convId),
      JSON.stringify({ text: String(text), ts: Date.now() })
    )
  } catch (e) {
    /* storage 不可用降级 */
  }
}
function restoreDraft(convId) {
  if (!convId) return null
  try {
    const raw = G.window.sessionStorage.getItem(draftKey(convId))
    if (!raw) return null
    const o = JSON.parse(raw)
    if (o && o.ts && Date.now() - o.ts > DRAFT_TTL) {
      G.window.sessionStorage.removeItem(draftKey(convId))
      return null
    }
    return o && typeof o.text === 'string' ? o.text : null
  } catch (e) {
    return null
  }
}
function clearDraft(convId) {
  if (convId) {
    try {
      G.window.sessionStorage.removeItem(draftKey(convId))
    } catch (e) {
      /* ignore：与老代码同款静默降级 */
    }
  }
}
function onDraftInput() {
  saveDraft(currentConvId.value, draft.value)
}
function onChatInput(ev) {
  autoResize(ev)
  onDraftInput()
}

// 最后会话记忆（Issue #407）
function saveLastConv(convId) {
  try {
    convId
      ? G.window.sessionStorage.setItem('chatLastConv', convId)
      : G.window.sessionStorage.removeItem('chatLastConv')
  } catch (e) {
    /* storage 不可用降级 */
  }
}
function getLastConv() {
  try {
    return G.window.sessionStorage.getItem('chatLastConv')
  } catch (e) {
    return null
  }
}

// ── 任务状态持久化（v31，sessionStorage）──
function getStoredTask(convId) {
  try {
    const s = G.window.sessionStorage.getItem('chat_task_' + convId)
    return s ? JSON.parse(s) : null
  } catch (e) {
    return null
  }
}
function setStoredTask(convId, task) {
  try {
    G.window.sessionStorage.setItem('chat_task_' + convId, JSON.stringify(task))
  } catch (e) {
    /* storage 不可用降级 */
  }
}
function clearStoredTask(convId) {
  try {
    G.window.sessionStorage.removeItem('chat_task_' + convId)
  } catch (e) {
    /* storage 不可用降级 */
  }
}

// ── 图片/视频附件（Issue #407：IndexedDB 持久化 + 编辑守卫）──
const MAX_IMAGES = 9
const MAX_VIDEO_SIZE = 50 * 1024 * 1024
const pendingImages = ref([])
const pendingVideo = ref(null)
function updateEditingGuard() {
  try {
    G.window._chatEditing = pendingImages.value.length > 0 || !!pendingVideo.value
  } catch (e) {
    /* storage 不可用降级 */
  }
}
// IndexedDB（grapeChatDB.chatImages）
const IMG_TTL = 3600000
let imgDB = null
let imgDBPromise = null
function openImgDB() {
  if (imgDB) return Promise.resolve(imgDB)
  if (imgDBPromise) return imgDBPromise
  const idb = G.window.indexedDB
  if (!idb) return Promise.resolve(null)
  imgDBPromise = new Promise(function (resolve) {
    try {
      const req = idb.open('grapeChatDB', 1)
      req.onupgradeneeded = function (e) {
        const db = e.target.result
        if (!db.objectStoreNames.contains('chatImages'))
          db.createObjectStore('chatImages', { keyPath: 'convId' })
      }
      req.onsuccess = function (e) {
        imgDB = e.target.result
        resolve(imgDB)
      }
      req.onerror = function () {
        imgDB = null
        resolve(null)
      }
    } catch (e) {
      imgDB = null
      resolve(null)
    }
  })
  return imgDBPromise
}
function saveChatImages(convId, images) {
  if (!convId) return
  openImgDB().then(function (db) {
    if (!db) return
    try {
      const store = db.transaction('chatImages', 'readwrite').objectStore('chatImages')
      if (!images || images.length === 0) store.delete(String(convId))
      else store.put({ convId: String(convId), images: images, ts: Date.now() })
    } catch (e) {
      /* ignore：与老代码同款静默降级 */
    }
  })
}
function loadChatImages(convId) {
  return openImgDB().then(function (db) {
    if (!db || !convId) return []
    return new Promise(function (resolve) {
      try {
        const req = db
          .transaction('chatImages', 'readonly')
          .objectStore('chatImages')
          .get(String(convId))
        req.onsuccess = function () {
          const rec = req.result
          if (!rec) return resolve([])
          if (rec.ts && Date.now() - rec.ts > IMG_TTL) {
            clearChatImages(convId)
            return resolve([])
          }
          resolve(rec.images && Array.isArray(rec.images) ? rec.images : [])
        }
        req.onerror = function () {
          resolve([])
        }
      } catch (e) {
        resolve([])
      }
    })
  })
}
function clearChatImages(convId) {
  if (!convId) return
  openImgDB().then(function (db) {
    if (!db) return
    try {
      db.transaction('chatImages', 'readwrite').objectStore('chatImages').delete(String(convId))
    } catch (e) {
      /* ignore：与老代码同款静默降级 */
    }
  })
}
function onImageSelect(ev) {
  const files = ev.target.files
  if (!files || !files.length) return
  const remaining = MAX_IMAGES - pendingImages.value.length
  if (remaining <= 0) {
    G.app.showToast(t('image_max_reached', { max: MAX_IMAGES }))
    ev.target.value = ''
    return
  }
  const toLoad = Math.min(files.length, remaining)
  if (files.length > remaining) G.app.showToast(t('image_max_exceeded', { max: MAX_IMAGES }))
  let loaded = 0
  for (let i = 0; i < toLoad; i++) {
    ;(function (file) {
      if (file.size > 5 * 1024 * 1024) {
        G.app.showToast(t('image_too_large') + ': ' + (file.name || ''))
        return
      }
      const reader = new FileReader()
      reader.onload = function (e) {
        pendingImages.value.push({ data: e.target.result, name: file.name || '图片' })
        loaded++
        if (loaded === toLoad || pendingImages.value.length === MAX_IMAGES) {
          saveChatImages(currentConvId.value, pendingImages.value)
          updateEditingGuard()
        }
      }
      reader.readAsDataURL(file)
    })(files[i])
  }
  ev.target.value = ''
}
function removeImage(idx) {
  pendingImages.value.splice(idx, 1)
  saveChatImages(currentConvId.value, pendingImages.value)
  updateEditingGuard()
}
function onVideoSelect(ev) {
  const files = ev.target.files
  if (!files || !files.length) return
  const file = files[0]
  if (file.size > MAX_VIDEO_SIZE) {
    G.app.showToast(t('image_too_large') + ': ' + (file.name || ''))
    ev.target.value = ''
    return
  }
  const reader = new FileReader()
  reader.onload = function (e) {
    pendingVideo.value = { data: e.target.result, name: file.name || 'video' }
    updateEditingGuard()
  }
  reader.readAsDataURL(file)
  ev.target.value = ''
}
function removeVideo() {
  pendingVideo.value = null
  updateEditingGuard()
}
function clearVideo() {
  pendingVideo.value = null
  updateEditingGuard()
}

// ── 会话列表 ──
function loadConversations(autoSelect) {
  const repoKey = issueParam && issueParam.repo ? issueParam.repo : openTarget ? '' : repoKeyOf()
  G.API.get('/chat/conversations' + (repoKey ? '?repo_key=' + encodeURIComponent(repoKey) : ''))
    .then(function (d) {
      convs.value = d.conversations || []
      if (!convs.value.length) {
        messages.value = []
        bodyLoading.value = false
      }
      if (autoSelect && !currentConvId.value) {
        if (issueParam) {
          G.API.post('/chat/conversations/ensure-issue', {
            repo_key: issueParam.repo,
            issue_number: parseInt(issueParam.num, 10),
          })
            .then(function (r) {
              loadConversations(false)
              if (r.conversation_id) switchConversation(r.conversation_id)
            })
            .catch(function () {
              if (convs.value.length) switchConversation(convs.value[0].id)
            })
        } else if (convs.value.length) {
          const lastConv = getLastConv()
          const target =
            openTarget &&
            convs.value.some(function (c) {
              return c.id === openTarget
            })
              ? openTarget
              : lastConv &&
                  convs.value.some(function (c) {
                    return c.id === lastConv
                  })
                ? lastConv
                : convs.value[0].id
          openTarget = null
          switchConversation(target)
        }
      }
    })
    .catch(function () {})
}
function newConversation() {
  const repo = currentRepo()
  G.API.post('/chat/conversations', repo ? { repo_key: repoKeyOf() } : {})
    .then(function (d) {
      if (currentConvId.value && currentConvId.value !== d.conversation_id) {
        saveDraft(currentConvId.value, draft.value)
        draft.value = ''
        saveChatImages(currentConvId.value, pendingImages.value)
        pendingImages.value = []
        clearVideo()
      }
      currentConvId.value = d.conversation_id
      saveLastConv(d.conversation_id)
      loadConversations()
      messages.value = []
      hdrTitle.value = ''
    })
    .catch(function (e) {
      G.app.showToast(t('conversation.create_failed') + (e.message ? ': ' + e.message : ''))
    })
}
const delPopover = reactive({ open: false, convId: '', pos: {} })
function askDeleteConv(c, ev) {
  delPopover.convId = c.id
  delPopover.pos = {
    left: Math.min(ev.clientX, G.window.innerWidth - 240) + 'px',
    top: Math.min(ev.clientY + 8, G.window.innerHeight - 120) + 'px',
  }
  delPopover.open = true
}
function closeDelPopover() {
  delPopover.open = false
}
function doDeleteConv() {
  const convId = delPopover.convId
  closeDelPopover()
  clearDraft(convId)
  clearChatImages(convId)
  G.API.del('/chat/conversations/' + convId)
    .then(function () {
      if (currentConvId.value === convId) {
        currentConvId.value = null
        saveLastConv(null)
        messages.value = []
        hdrTitle.value = ''
      }
      loadConversations()
      G.app.showToast(t('conversation.deleted'))
    })
    .catch(function (e) {
      G.app.showToast(t('conversation.delete_failed') + (e.message ? ': ' + e.message : ''))
    })
}

// ── 消息渲染 ──
// ═══════════ Issue #442：消息撤回（二次确认 → 终止该轮执行 → 原文可重新编辑） ═══════════
const recallTexts = new Map() // msgId → 原文（剥离 [chat_img:] 标记，供「重新编辑」）
// 飞书/Web 双通道同一条消息可能重复下发（同 id 或同 task 的 agent 回复）→ 渲染前去重，
// 避免对话框出现两个气泡。规则：普通消息按 id 保留首条；agent 回复按 task_id 保留最新一条。
function dedupeMessages(list) {
  const out = []
  const seenId = new Set()
  ;(list || []).forEach(function (m) {
    if (!m) return
    if (m.id != null) {
      const k = String(m.id)
      if (seenId.has(k)) return
      seenId.add(k)
    }
    if (m.role === 'agent' && m.task_id != null) {
      const idx = out.findIndex(function (x) {
        return x.role === 'agent' && x.task_id === m.task_id
      })
      if (idx >= 0) {
        out[idx] = m
        return
      }
    }
    out.push(m)
  })
  return out
}
function stripChatImgMarkers(content) {
  return String(content || '')
    .replace(/\[chat_img:[^\]]+\]/g, '')
    .trim()
}
// 消息列表变化时登记用户消息原文（撤回后仍能重新编辑）
watch(messages, function (list) {
  ;(list || []).forEach(function (m) {
    const id = m && parseInt(m.id, 10)
    if (!id || m.role !== 'user' || m.recalled) return
    const text = stripChatImgMarkers(m.content || '')
    if (text) recallTexts.set(id, text)
  })
})
// 当前 SSE 的 AbortController（必须先声明：switchConversation 在 setup 期间就会读它）
let streamAbort = null

function recallMessage(m, idx) {
  const id = parseInt(m && m.id, 10) || 0
  if (!id) return
  G.app.showConfirm(
    t('recall.confirm'),
    function () {
      G.API.post('/chat/messages/' + id + '/recall')
        .then(function () {
          // 原地换成撤回提示（服务端软删除，刷新后该条不再返回）
          const cur = messages.value[idx]
          if (cur && (parseInt(cur.id, 10) || 0) === id) {
            messages.value[idx] = { role: 'user', id: id, recalled: true }
          }
          // 终止该轮的前端表现：中止 SSE + 移除三点占位（后端已终止任务）
          if (streamAbort) {
            streamAbort.abort()
            streamAbort = null
          }
          streaming.value = null
        })
        .catch(function (e) {
          G.app.showToast(t('recall.failed', { msg: (e && e.message) || '' }))
        })
    },
    { danger: true, confirmText: t('recall.action') }
  )
}
// 「重新编辑」：原文追加到输入框现有内容之后（不覆盖）
function reEditRecalled(m) {
  const id = parseInt(m && m.id, 10) || 0
  const text = recallTexts.get(id) || ''
  if (!text) return
  draft.value = draft.value ? draft.value + '\n' + text : text
  nextTick(function () {
    const el = G.window.document.getElementById('chatInput')
    if (el) {
      el.focus()
      if (typeof G.window._autoResizeTextarea === 'function') G.window._autoResizeTextarea(el)
    }
  })
}

function renderMsg(m) {
  // 流式转正气泡带 _html（SSE 阶段渲染好的 markdown），优先使用
  if (m._html) return m._html
  let rendered = renderMarkdown(m.content || '')
  rendered = rendered.replace(/\[chat_img:([^\]]+)\]/g, function (match, url) {
    return (
      '<img src="' +
      url +
      '?thumb=1" style="max-width:140px;max-height:140px;border-radius:6px;margin-top:4px;display:inline-block;cursor:pointer" onclick="window.open(\'' +
      url +
      "','_blank')\">"
    )
  })
  const repoKey = (convMeta2.value && convMeta2.value.repo_key) || ''
  return linkifyIssueNumbers(rendered, repoKey)
}
const convMeta2 = ref(null)

function switchConversation(convId) {
  if (currentConvId.value !== convId) {
    // 切会话即中止上一个会话的 SSE：不中止会白占连接与定时器（事件已被 isOwnerConv 丢弃）；
    // 任务态留在 storedTask，切回时由 resume 逻辑重新订阅。
    if (streamAbort) { try { streamAbort.abort() } catch (e) {} streamAbort = null }
    streaming.value = null
    if (currentConvId.value) saveDraft(currentConvId.value, draft.value)
    draft.value = ''
    if (currentConvId.value) saveChatImages(currentConvId.value, pendingImages.value)
    pendingImages.value = []
    clearVideo()
  }
  currentConvId.value = convId
  saveLastConv(convId)
  loadConversations()
  bodyLoading.value = true
  bodyError.value = false
  G.API.get('/chat/messages/' + convId)
    .then(function (d) {
      const msgs = d.messages || []
      const conv = d.conversation || {}
      convMeta2.value = conv
      // 头部标题（Issue 前缀链接化）
      const titleBase = conv.title || 'Personal Assistant'
      const stripped = stripIssuePrefix(titleBase)
      let safeTitle = esc(stripped.title || titleBase)
      let prefix = ''
      if (conv.conv_type === 'task' && conv.issue_number) {
        const url = vcsUrl(conv.repo_key || '', '/issues/' + conv.issue_number)
        prefix =
          '<a href="' +
          url +
          '" target="_blank" rel="noopener">Issue #' +
          conv.issue_number +
          '</a> · '
      }
      hdrTitle.value = prefix + linkifyIssueNumbers(safeTitle, conv.repo_key || '')
      messages.value = dedupeMessages(msgs)
      bodyLoading.value = false
      nextTick(scrollBody)
      // v31: 最后一条 user 消息带 task_id → 检查运行中任务并 SSE 重连
      const lastMsg = msgs[msgs.length - 1]
      if (lastMsg && lastMsg.role === 'user' && lastMsg.task_id) {
        const hasAgentReply = msgs.some(function (m) {
          return m.role === 'agent' && m.task_id === lastMsg.task_id
        })
        G.API.get('/chat/task/' + lastMsg.task_id + '/status')
          .then(function (st) {
            if (st.status === 'running' && !hasAgentReply) {
              if (currentConvId.value !== convId) return
              // 复用上次的气泡 id，并**先立起 thinking 气泡**：startStream 只在首个
              // text 事件才建气泡，agent 还在思考（无 text）时不会有任何指示，
              // 表现就是「切走再切回来，流式气泡不见了」（走外部菜单回来时有
              // resumeChatTask 帮忙立气泡，所以那条路径正常）。
              const _stored = getStoredTask(convId)
              const elId = (_stored && _stored.el_id) || 't' + Date.now()
              setStoredTask(convId, {
                task_id: lastMsg.task_id,
                conv_id: convId,
                el_id: elId,
                status: 'running',
              })
              if (!streaming.value) streaming.value = { elId: elId, stepText: '' }
              startStream(lastMsg.task_id, elId, convId)
            } else if (st.status === 'done' || st.status === 'error' || hasAgentReply) {
              if (hasAgentReply) clearStoredTask(convId)
              else {
                G.API.get('/chat/messages/' + convId).then(function (d2) {
                  messages.value = dedupeMessages(d2.messages || [])
                  loadConversations()
                })
                clearStoredTask(convId)
              }
            } else {
              clearStoredTask(convId)
            }
          })
          .catch(function () {
            clearStoredTask(convId)
          })
      }
      // 草稿回填
      const _draft = restoreDraft(convId)
      if (_draft) draft.value = _draft
      // 图片附件回填（守卫防竞态）
      loadChatImages(convId).then(function (imgs) {
        if (currentConvId.value !== convId) return
        if (imgs && imgs.length > 0) pendingImages.value = imgs.slice(0, MAX_IMAGES)
        updateEditingGuard()
      })
    })
    .catch(function () {
      bodyLoading.value = false
      bodyError.value = true
    })
}
function scrollBody() {
  const el = document.getElementById('chatBody')
  if (el) el.scrollTop = el.scrollHeight
}
function autoResize(ev) {
  const el = ev && ev.target
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 120) + 'px'
}

// ── 发送 + SSE 流式（v31 submit/stream 分离）──
const streaming = ref(null) // {elId, stepText}
function sendChatStream() {
  const text = (draft.value || '').trim()
  if (!text && pendingImages.value.length === 0 && !pendingVideo.value) return
  if (!currentConvId.value) {
    G.API.post('/chat/conversations', currentRepo() ? { repo_key: repoKeyOf() } : {})
      .then(function (d) {
        currentConvId.value = d.conversation_id
        saveLastConv(d.conversation_id)
        loadConversations()
        doSend()
      })
      .catch(function (e) {
        G.app.showToast(t('conversation.create_conv_failed', { msg: e.message }))
      })
    return
  }
  doSend()
}
function doSend() {
  const msg = (draft.value || '').trim()
  const imgs = pendingImages.value.slice()
  const vid = pendingVideo.value ? pendingVideo.value.data : ''
  if (!msg && imgs.length === 0) return
  const convId = currentConvId.value
  draft.value = ''
  clearDraft(convId)
  pendingImages.value = []
  clearVideo()
  // 用户消息立即上屏
  const userMsg = { role: 'user', content: msg }
  messages.value.push(userMsg)
  // 流式占位气泡
  const tid = 't' + Date.now()
  streaming.value = { elId: tid, stepText: '' }
  nextTick(scrollBody)
  const hasMedia = imgs.length > 0 || !!vid
  G.API.post(
    '/chat/submit',
    {
      message: msg || '请帮我分析这些图片',
      repo_key: repoKeyOf(),
      conversation_id: convId,
      image_data:
        imgs.length > 0
          ? JSON.stringify(
              imgs.map(function (i) {
                return i.data
              })
            )
          : '',
      video_data: vid || '',
    },
    hasMedia ? 120000 : undefined
  )
    .then(function (r) {
      const cid = r.conversation_id || convId
      if (r.status === 'error') {
        finishStream(renderMarkdown(r.error || t('no_reply')))
        clearStoredTask(cid)
        loadConversations()
        return
      }
      setStoredTask(cid, { task_id: r.task_id, conv_id: cid, el_id: tid, status: 'running' })
      startStream(r.task_id, tid, cid)
    })
    .catch(function (e) {
      finishStream(renderMarkdown((e && e.message) || '请求失败'))
      // 发送失败 → 恢复草稿（文本未上服务器不能丢）
      if (convId && !draft.value) {
        draft.value = msg
        saveDraft(convId, msg)
      }
    })
}
function finishStream(html, convId) {
  // 流式气泡转正：追加为普通 agent 消息（submit 失败/立即错误路径）
  // convId 给了且已切走 → 不写 UI（否则 A 会话的失败气泡会追加到 B 会话列表）
  if (convId && currentConvId.value !== convId) return
  if (streaming.value) {
    messages.value.push({ role: 'agent', content: '__stream__' })
    const last = messages.value[messages.value.length - 1]
    last._html = html
    streaming.value = null
  }
}
async function startStream(taskId, elId, convId) {
  if (streamAbort) {
    streamAbort.abort()
    streamAbort = null
  }
  const ac = new AbortController()
  streamAbort = ac
  // 归属会话：切走后 SSE 仍在吐事件（后端继续执行），必须丢弃，否则思考日志/正文
  // 会写进「当前」会话的消息列表——用户在 B 会话看到 A 会话的思考滚动。
  const isOwnerConv = () => currentConvId.value === convId
  let finalText = ''
  try {
    const token = (await G.API.ensureTokenValid()) || ''
    const resp = await fetch('/api/chat/task/' + taskId + '/stream', {
      headers: { Authorization: 'Bearer ' + token },
      signal: ac.signal,
    })
    if (!resp.ok || !resp.body) {
      finishStream(tc('error') || '错误')
      clearStoredTask(convId)
      return
    }
    const reader = resp.body.getReader()
    const dec = new TextDecoder()
    let buf = ''
    // 单气泡流式：一个 agent 气泡，_html 随 chunk 更新（不重复 push）
    let bubbleIdx = -1
    function upsertBubble(html) {
      if (bubbleIdx < 0) {
        messages.value.push({ role: 'agent', content: '__stream__' })
        bubbleIdx = messages.value.length - 1
      }
      messages.value[bubbleIdx]._html = html
    }
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() || ''
      lines.forEach(function (line) {
        if (!line.startsWith('data: ')) return
        if (!isOwnerConv()) return   // 已切到别的会话：丢弃事件，不写 UI
        try {
          const d = JSON.parse(line.substring(6))
          if (d.type === 'done') {
            finalText = d.text || finalText
            upsertBubble(renderMarkdown(finalText) || t('no_reply'))
            streaming.value = null
          } else if (d.type === 'error') {
            upsertBubble(renderMarkdown(d.text || '') || t('no_reply'))
            streaming.value = null
          } else if (d.type === 'text' && d.text) {
            finalText += d.text
            upsertBubble(renderMarkdown(finalText))
          } else if (d.text && streaming.value) {
            // 中间步骤 → ticker + dots 动画
            const prefix = d.type === 'step_start' ? '▸ ' : d.type === 'step_finish' ? '✓ ' : ''
            streaming.value.stepText = prefix + d.text
          }
        } catch (e) {
          /* ignore：与老代码同款静默降级 */
        }
      })
      if (!isOwnerConv()) { try { ac.abort() } catch (e) {} break }
      nextTick(scrollBody)
    }
    // SSE 结束仍无 done（异常断流）→ 保留任务态供切回恢复
    if (isOwnerConv() && streaming.value) streaming.value = null
    clearStoredTask(convId)
    if (isOwnerConv()) loadConversations()
  } catch (e) {
    if (e && e.name === 'AbortError') return
    // 连接断开 → 后端继续执行，保留任务态供切回恢复
    if (streaming.value && isOwnerConv()) streaming.value.stepText = ''
  }
}

// ── 引擎/技能选择 ──
const pickerOpen = ref(null)
const configData = ref({})
const backends = ref([])
const skillScope = ref('official')
const skillItems = ref([])
const skillLoading = ref(false)
function togglePicker(type) {
  pickerOpen.value = pickerOpen.value === type ? null : type
  if (pickerOpen.value === 'skill') loadSkillItems()
}
function switchSkillScope(s) {
  skillScope.value = s
  loadSkillItems()
}
function closePicker() {
  pickerOpen.value = null
}
function isBackendActive(b) {
  const d = configData.value || {}
  const backend = d.backend || {}
  const curType = (backend.type || '').toLowerCase()
  const sel = d.selectedEngine || {}
  const selRid = parseInt(sel.runtime_id || 0, 10)
  const rid = parseInt(b.runtime_id || 0, 10)
  return (selRid > 0 && rid === selRid) || (selRid <= 0 && b.name.toLowerCase() === curType)
}
function loadConfigData() {
  const repo = currentRepo()
  const qs = repo ? '?repo=' + encodeURIComponent(repo.fullName) : ''
  G.API.get('/chat/config' + qs)
    .then(function (d) {
      configData.value = d
      backends.value = d._backends || []
      skillScope.value =
        d.skill_scope === 'repo' ? 'repo' : d.skill_scope === 'user' ? 'user' : 'official'
      // 引擎徽标（Issue #400）
      const sel = (d && d.selectedEngine) || {}
      const name = sel.display_name || sel.name || ''
      if (name) engineBadge.value = (sel.scope === 'local' ? '🖥' : '☁') + ' ' + name
      else engineBadge.value = ''
    })
    .catch(function () {})
}
function loadSkillItems() {
  skillLoading.value = true
  const repo = repoKeyOf()
  let qs = '?scope=' + skillScope.value
  if (repo) qs += '&repo=' + encodeURIComponent(repo)
  G.API.get('/skills' + qs)
    .then(function (data) {
      skillItems.value = (data.skills || []).filter(function (s) {
        return s.agent_type === 'chat'
      })
      skillLoading.value = false
    })
    .catch(function () {
      skillItems.value = []
      skillLoading.value = false
    })
}
function saveModelConfig(b) {
  const repo = currentRepo()
  const qs = repo ? '?repo=' + encodeURIComponent(repo.fullName) : ''
  const body = { backend: b.name }
  if (b.runtime_id) body.runtime_id = parseInt(b.runtime_id, 10)
  G.API.put('/chat/config' + qs, body)
    .then(function () {
      G.app.showToast(t('config.saved'))
      loadConfigData()
    })
    .catch(function (e) {
      G.app.showToast(t('config.save_failed') + (e.message ? ': ' + e.message : ''))
    })
    .finally(closePicker)
}
function saveSkillConfig(skillId) {
  if (skillScope.value !== 'official' && !skillId) {
    G.app.showToast(t('skill.select_required'))
    return
  }
  const repo = currentRepo()
  const qs = repo ? '?repo=' + encodeURIComponent(repo.fullName) : ''
  G.API.put('/chat/config' + qs, { skill_id: skillId })
    .then(function () {
      G.app.showToast(t('config.saved'))
      if (configData.value) configData.value.skill_id = skillId
    })
    .catch(function (e) {
      G.app.showToast(t('config.save_failed') + (e.message ? ': ' + e.message : ''))
    })
    .finally(closePicker)
}

// ── 飞书扫码配置（复用组件：Chat/VOD）──
const onboard = reactive({ open: false, status: 'pending', qrUrl: '', appId: '', error: '' })
let onboardTimer = null
let onboardTaskId = null
let onboardExpireAt = 0
let onboardQrRendered = false
let onboardBotType = 'chat_bot'
let onboardRepoKey = ''
let countdownTimer = null
const onboardErrorMsg = computed(function () {
  const s = String(onboard.error || '')
  if (s.includes('access_denied')) return t('onboard.error_access_denied')
  if (s.includes('expired') || s.includes('timeout')) return t('onboard.qr_expired')
  if (s.includes('已被')) return s
  return s || t('onboard.error_unknown')
})
function startFeishuOnboard(botType, repoKey, skipRebindCheck) {
  onboardBotType = botType || 'chat_bot'
  onboardRepoKey = repoKey || ''
  if (!skipRebindCheck) {
    G.API.get('/chat/config')
      .then(function (cfg) {
        const existing = (cfg && cfg.im && cfg.im.feishu && cfg.im.feishu.app_id) || ''
        if (existing) {
          G.app.showConfirm(
            t('onboard.confirm_rebind', { appId: existing }),
            function () {
              startFeishuOnboard(onboardBotType, onboardRepoKey, true)
            },
            { danger: true }
          )
          return
        }
        doStartOnboard()
      })
      .catch(function () {
        doStartOnboard()
      })
    return
  }
  doStartOnboard()
  function doStartOnboard() {
    const body = { bot_type: onboardBotType }
    if (onboardRepoKey) body.repo_key = onboardRepoKey
    G.API.post('/feishu/onboard/start', body)
      .then(function (d) {
        onboardTaskId = d.task_id
        onboard.open = true
        onboard.status = 'pending'
        onboardQrRendered = false
        pollOnboardStatus()
      })
      .catch(function (e) {
        if (e && e.status === 409) {
          const oldTaskId = (e.detail && e.detail.task_id) || ''
          G.app.showConfirm(
            t('onboard.confirm_restart'),
            function () {
              if (oldTaskId) {
                G.API.post('/feishu/onboard/cancel', { task_id: oldTaskId }).finally(function () {
                  startFeishuOnboard(onboardBotType, onboardRepoKey, true)
                })
              } else startFeishuOnboard(onboardBotType, onboardRepoKey, true)
            },
            { danger: true }
          )
        } else {
          G.app.showToast(t('onboard.start_failed') + (e.message ? ': ' + e.message : ''))
        }
      })
  }
}
function pollOnboardStatus() {
  if (onboardTimer) clearInterval(onboardTimer)
  onboardTimer = setInterval(async function () {
    if (!onboardTaskId) {
      clearInterval(onboardTimer)
      onboardTimer = null
      return
    }
    try {
      const d = await G.API.get('/feishu/onboard/status?task_id=' + onboardTaskId)
      onboard.status = d.status
      onboard.appId = d.app_id || onboard.appId
      onboard.error = d.error || ''
      if (d.status === 'waiting_confirm' && d.qr_url && !onboardQrRendered) {
        onboardQrRendered = true
        onboard.qrUrl = d.qr_url
        onboardExpireAt = d.expire_in ? Date.now() + d.expire_in * 1000 : 0
        nextTick(function () {
          const el = document.getElementById('onboardQr' + uid)
          if (el && G.window.QRCode)
            new G.window.QRCode(el, { text: d.qr_url, width: 200, height: 200 })
          startCountdown()
        })
      }
      if (d.status === 'connected' || d.status === 'failed') {
        clearInterval(onboardTimer)
        onboardTimer = null
      }
    } catch (e) {
      clearInterval(onboardTimer)
      onboardTimer = null
    }
  }, 2000)
}
function stopOnboardPoll() {
  if (onboardTimer) {
    clearInterval(onboardTimer)
    onboardTimer = null
  }
}
function startCountdown() {
  if (countdownTimer) clearInterval(countdownTimer)
  countdownTimer = setInterval(function () {
    const el = document.getElementById('onboardCountdown' + uid)
    if (!el || !onboardExpireAt) {
      clearInterval(countdownTimer)
      countdownTimer = null
      return
    }
    const remain = Math.max(0, Math.round((onboardExpireAt - Date.now()) / 1000))
    if (remain <= 0) {
      el.textContent = t('onboard.qr_expired')
      clearInterval(countdownTimer)
      countdownTimer = null
      return
    }
    const mm = String(Math.floor(remain / 60)).padStart(2, '0')
    const ss = String(remain % 60).padStart(2, '0')
    el.textContent = t('onboard.waiting_countdown', { time: mm + ':' + ss })
  }, 1000)
}
function restartOnboard() {
  const botType = onboardBotType || 'chat_bot'
  const repoKey = onboardRepoKey || ''
  onboardTaskId = null
  onboardExpireAt = 0
  onboardQrRendered = false
  onboard.status = 'pending'
  onboard.qrUrl = ''
  startFeishuOnboard(botType, repoKey)
}
function cancelOnboard() {
  if (onboardTaskId) {
    G.app.showConfirm(
      t('onboard.confirm_close'),
      function () {
        if (onboardTaskId)
          G.API.post('/feishu/onboard/cancel', { task_id: onboardTaskId }).catch(function () {})
        closeOnboardModal()
      },
      { danger: true }
    )
    return
  }
  closeOnboardModal()
}
function closeOnboardModal(refresh) {
  onboardTaskId = null
  onboardExpireAt = 0
  onboardQrRendered = false
  onboard.open = false
  onboard.status = 'pending'
  onboard.qrUrl = ''
  if (onboardTimer) {
    clearInterval(onboardTimer)
    onboardTimer = null
  }
  if (countdownTimer) {
    clearInterval(countdownTimer)
    countdownTimer = null
  }
  if (refresh) {
    // 按当前所处页面刷新对应数据源（im-channel 页 / VOD 页 / chat 页）
    if (
      G.window._currentView === 'im-channel' &&
      typeof G.window.loadImChannelConfig === 'function'
    ) {
      G.window.loadImChannelConfig()
    } else if (onboardBotType === 'vod_bot' && typeof G.window.refreshVodOnboard === 'function') {
      G.window.refreshVodOnboard()
    } else {
      loadConfigData()
    }
  }
}

// ── app.js nav 契约 ──
// app.js nav('chat', param) 调 renderChat(param)；离开 chat 页时 app.js 直接 abort window._chatStreamAbort
G.window.resumeChatTask = function () {
  // 切回 Chat 页恢复活跃查询（app.js nav('chat') 后 setTimeout 调用）
  if (!currentConvId.value) return
  const task = getStoredTask(currentConvId.value)
  if (task && task.task_id) {
    G.API.get('/chat/task/' + task.task_id + '/status')
      .then(function (st) {
        if (st.status === 'running') {
          G.API.get('/chat/messages/' + currentConvId.value)
            .then(function (d2) {
              const msgs2 = d2.messages || []
              const hasAgent = msgs2.some(function (m) {
                return m.role === 'agent' && m.task_id === task.task_id
              })
              if (hasAgent) {
                clearStoredTask(currentConvId.value)
                messages.value = msgs2
                loadConversations()
                return
              }
              if (!streaming.value) {
                streaming.value = { elId: task.el_id, stepText: '' }
                startStream(task.task_id, task.el_id, currentConvId.value)
              }
            })
            .catch(function () {
              if (!streaming.value) {
                streaming.value = { elId: task.el_id, stepText: '' }
                startStream(task.task_id, task.el_id, currentConvId.value)
              }
            })
        } else {
          clearStoredTask(currentConvId.value)
          G.API.get('/chat/messages/' + currentConvId.value)
            .then(function (d) {
              messages.value = d.messages || []
              loadConversations()
            })
            .catch(function () {})
        }
      })
      .catch(function () {})
  } else {
    G.API.get('/chat/messages/' + currentConvId.value)
      .then(function (d) {
        const msgs = d.messages || []
        const lastMsg = msgs[msgs.length - 1]
        if (lastMsg && lastMsg.role === 'user' && lastMsg.task_id) {
          setStoredTask(currentConvId.value, {
            task_id: lastMsg.task_id,
            conv_id: currentConvId.value,
            status: 'running',
          })
          G.window.resumeChatTask()
        }
      })
      .catch(function () {})
  }
}
// app.js 离开 chat 页调用 _stopOnboardPoll / abort _chatStreamAbort（模块级变量在 Vue 域内，导出同名句柄）
G.window._stopOnboardPoll = stopOnboardPoll
try {
  G.window._chatStreamAbort = null
} catch (e) {
  /* ignore：与老代码同款静默降级 */
}
// 让 app.js 的 abort 语句能触达本实例的 AbortController：
// app.js 引用的是 window._chatStreamAbort，我们同步一个 setter 桥
let _lastAbortRef = null
// 同步 streamAbort 引用到 window._chatStreamAbort（app.js 离开 chat 页时 abort 它）
function syncAbortBridge() {
  if (streamAbort !== _lastAbortRef) {
    _lastAbortRef = streamAbort
    G.window._chatStreamAbort = streamAbort
  }
}

onBeforeUnmount(function () {
  stopOnboardPoll()
  if (countdownTimer) clearInterval(countdownTimer)
  if (streamAbort) streamAbort.abort()
  syncAbortBridge()
  try {
    G.window._chatEditing = false
  } catch (e) {
    /* storage 不可用降级 */
  }
})

onMounted(function () {
  G.window._currentView = 'chat'
  loadConversations(true)
  loadConfigData()
})
</script>

<style scoped>
.cv {
  min-height: 100%;
}
.cv__wrap {
  height: calc(100vh - 96px);
}
.cv__newbar {
  padding: 10px 12px;
  border-bottom: 1px solid var(--b);
}
.cv__newbtn {
  width: 100%;
}
.cv__active {
  background: var(--b);
}
.cv__emptylist {
  padding: 20px;
  text-align: center;
  color: var(--t3);
  font-size: 12px;
}
.cv__loading {
  text-align: center;
  color: var(--t3);
  padding: 60px 20px;
}
.cv__starthint {
  text-align: center;
  color: var(--t3);
  padding: 40px;
}
.cv__preview {
  padding: 6px 12px;
  border-top: 1px solid var(--b);
  flex-wrap: wrap;
  gap: 6px;
  display: flex;
}
.cv__pv-item {
  position: relative;
  display: inline-block;
}
.cv__pv-item img {
  width: 56px;
  height: 56px;
  border-radius: 4px;
  object-fit: cover;
  cursor: pointer;
}
.cv__pv-video {
  width: 56px;
  height: 56px;
  border-radius: 4px;
  background: var(--bg);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}
.cv__pv-x {
  position: absolute;
  top: -4px;
  right: -4px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.6);
  color: var(--on-accent);
  border: none;
  cursor: pointer;
  font-size: 12px;
  line-height: 18px;
  text-align: center;
  padding: 0;
}
.cv__pv-count {
  font-size: 11px;
  color: var(--t3);
  align-self: center;
}
.cv__hide {
  display: none;
}
.cv__tool-btn {
  cursor: pointer;
  display: flex;
  align-items: center;
  padding: 0 10px;
  border-radius: 6px;
  color: var(--t3);
}
/* 选择浮层（复用壳里 .chat-picker-* 类） */
.cv__pop {
  left: auto;
  right: auto;
}
.cv__pop-label {
  font-size: 11px;
  font-weight: 700;
  color: var(--t3);
  padding: 8px 14px 6px;
  letter-spacing: 0.3px;
  text-transform: uppercase;
}
.cv__pop-list {
  max-height: 280px;
  overflow-y: auto;
  padding: 0 6px 6px;
}
.cv__pop-hint {
  padding: 16px;
  text-align: center;
  color: var(--t3);
  font-size: 12px;
}
.cv__pop-ic {
  font-size: 14px;
}
.cv__pop-name {
  flex: 1;
  font-size: 12px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cv__pop-cur {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
  color: var(--a);
}
.cv__scope-tabs {
  display: flex;
  gap: 4px;
  margin: 0 14px 8px;
}
.cv__scope-tab {
  flex: 1;
  padding: 5px 8px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 11px;
  background: var(--c);
  color: var(--t2);
  cursor: pointer;
  transition: all 0.15s;
}
.cv__scope-tab.active {
  background: var(--a);
  color: var(--on-accent);
  border-color: var(--a);
}
/* 删除气泡（复用 .chat-del-* 壳类） */
.cv__delpop {
}
/* 飞书扫码弹窗 */
.cv__onboard-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  z-index: 1001;
  display: flex;
  align-items: center;
  justify-content: center;
}
.cv__onboard {
  width: 340px;
  background: var(--bg);
  border: 1px solid var(--b);
  border-radius: 12px;
  padding: 20px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.25);
  font-size: 12px;
}
.cv__onboard-hdr {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}
.cv__onboard-hdr b {
  font-size: 14px;
}
.cv__onboard-x {
  cursor: pointer;
  color: var(--t3);
}
.cv__onboard-body {
  text-align: center;
  color: var(--t3);
  padding: 10px 0;
}
.cv__qr-wrap {
  background: var(--c);
  border: 1px solid var(--b);
  border-radius: 8px;
  padding: 12px;
  display: inline-block;
  margin-bottom: 10px;
}
.cv__onboard-t {
  color: var(--t);
  margin-bottom: 4px;
}
.cv__onboard-t2 {
  color: var(--t2);
  font-size: 11px;
  margin-bottom: 2px;
}
.cv__onboard-t3 {
  color: var(--t3);
  margin-bottom: 10px;
}
.cv__onboard-pk {
  color: var(--pk);
  font-size: 11px;
  margin-bottom: 8px;
}
.cv__onboard-warn {
  color: var(--r);
  font-size: 11px;
  font-weight: 600;
  border-top: 1px solid var(--b);
  padding-top: 8px;
}
.cv__onboard-big {
  font-size: 34px;
  margin-bottom: 8px;
}
.cv__onboard-r {
  color: var(--r);
  font-weight: 600;
  margin-bottom: 8px;
}
.cv__onboard-btn {
  width: 100%;
  margin-top: 4px;
}
.cv__onboard-btns {
  display: flex;
  gap: 8px;
}
.cv__mono {
  font-family: monospace;
}

/* 链接（Issue #N、正文里的 #123）用 accent 文字档，别用浏览器默认的深蓝 #0000EE。
   注意这些 <a> 是 v-html 生成的（没有 data-v），必须走 :deep() 才匹配得到。 */
.chat-hdr :deep(a),
.chat-body :deep(a),
.conv-item :deep(a) {
  color: var(--a-text);
  text-decoration: none;
}
.chat-hdr :deep(a:hover),
.chat-body :deep(a:hover),
.conv-item :deep(a:hover) {
  text-decoration: underline;
}
</style>
