<template>
  <div class="ev">
    <button v-if="backToTask" class="bt bt-o bt-s ev__back" @click="goBackToTask">
      ← {{ t('back_to_requirement') }}
    </button>
    <button v-else class="bt bt-o bt-s ev__back" @click="G.window.history.back()">
      ← {{ tc('back') }}
    </button>

    <div
      id="evDetailGrid"
      class="ev__grid"
      :style="{ gridTemplateColumns: convPanelVisible ? '1fr 300px' : '1fr' }"
    >
      <!-- 左栏大框 -->
      <div class="cd ev__main">
        <!-- 顶部导航栏 -->
        <div class="ev__navbar">
          <button
            class="bt bt-o bt-s"
            :disabled="!data.prev_seq"
            :title="t('prev_event_title', { name: agentName })"
            @click="navEvent(data.prev_seq)"
          >
            ‹
          </button>
          <span class="ts t3 ev__seq">Event #{{ data.seq || '—' }}</span>
          <button
            class="bt bt-o bt-s"
            :disabled="!data.next_seq"
            :title="t('next_event_title', { name: agentName })"
            @click="navEvent(data.next_seq)"
          >
            ›
          </button>
          <button
            v-if="data.status === 'failed'"
            class="bt bt-o bt-s ev__feedback"
            :title="t('btn_feedback_title')"
            @click="openFeedback"
          >
            {{ t('btn_feedback') }}
          </button>
        </div>

        <!-- 区块1：Context & Info（可折叠，默认展开） -->
        <div class="ev-section ev__section">
          <div class="ev-section-header ev__secheader" @click="toggleSection('ctxInfo')">
            <span :class="['ev-arrow', { ev__arrowOpen: openSections.ctxInfo }]">▼</span>
            <span class="ev__sectitle">{{ t('section_context_info') }}</span>
            <span id="evInfoSummary" class="ts t3 ev__secmeta">{{ infoSummaryText }}</span>
          </div>
          <div v-show="openSections.ctxInfo" class="ev__ctxgrid">
            <div>
              <h3 class="ev__h3">{{ t('section_context') }}</h3>
              <div class="tx t2 ev__ctxlines">
                <div>Trigger: {{ data.event_type || '—' }}</div>
                <div>Repository: {{ data.repo_key || '' }}</div>
                <div>
                  {{ isPR ? 'PR' : 'Issue' }}:
                  <a
                    v-if="issueUrl"
                    :href="issueUrl"
                    target="_blank"
                    rel="noopener"
                    class="ev__link"
                    >{{ issueLabel }}</a
                  ><template v-else>{{ issueLabel }}</template>
                </div>
                <div>Actor: {{ data.actor || '—' }}</div>
                <div>Time: {{ fmtLocalTime(data.created_at) || '' }}</div>
              </div>
            </div>
            <div>
              <h3 class="ev__h3">{{ t('section_info') }}</h3>
              <div class="ev__infolines">
                <div>
                  <span class="ev__lbl">Seq:</span> <b>#{{ data.seq || '—' }}</b>
                </div>
                <div id="evEngineRow" v-html="engineText"></div>
                <div><span class="ev__lbl">Skill:</span> {{ data.skill || agentType }}</div>
                <div id="evDurationRow" v-html="durationText"></div>
                <div id="evStatusRow" @click="onInfoClick($event)" v-html="statusText"></div>
                <div id="evFailReason" v-html="failReasonText"></div>
                <div id="evLogRow" @click="onInfoClick($event)" v-html="logRowText"></div>
                <!-- Issue #35 遗留：老页把门禁徽标放在信息区但模板里恒 display:none（从未被
                     打开过），Vue 移植时照抄了 HTML 却漏掉了隐藏 → 信息区多出一行
                     「✔ 评论开放中」。状态本身已由右侧对话面板的输入行表达，这里恢复隐藏。 -->
                <div v-if="gateLoaded" id="convGate" v-html="gateText"></div>
              </div>
            </div>
          </div>
        </div>

        <!-- 区块2：接收上下文 prompt（可折叠，默认折叠） -->
        <div class="ev-section ev__section">
          <div class="ev-section-header ev__secheader" @click="togglePromptSection">
            <span :class="['ev-arrow', { ev__arrowOpen: openSections.promptBody }]">▶</span>
            <span class="ev__sectitle">{{ t('section_prompt') }}</span>
            <span id="promptMeta" class="ts t3 ev__secmeta">{{ promptMeta }}</span>
            <button
              v-show="openSections.promptBody"
              class="ev__fsbtn"
              :title="t('fullscreen_title')"
              @click.stop="togglePromptFullscreen"
            >
              {{ t('fullscreen_btn') }}
            </button>
          </div>
          <div v-show="openSections.promptBody" class="ev__promptbody">
            <div class="ev__promptcontent">{{ promptContent }}</div>
          </div>
        </div>

        <!-- 区块3：处理记录 -->
        <h3 class="ev__h3 ev__proctitle">{{ t('section_processing') }}</h3>
        <div id="evProcessingBody" class="ev__processing" v-html="processingText"></div>
        <!-- 对话产出 -->
        <div v-if="outcomeVisible" id="convOutcome" class="cd mt12 ev__outcome">
          <h3 class="ev__outcometitle">{{ t('outcome_title') }}</h3>
          <div v-if="knowledgeCards.length" class="ev__outcomeblock">
            <b>{{ t('outcome_knowledge') }}</b>
            <div class="ev__kcards">
              <div
                v-for="scope in knowledgeCards"
                :key="scope"
                class="k-card"
                @click="showKnowledgeDetail(scope)"
              >
                <span class="k-card-label">{{ scopeIcons[scope] }} {{ scopeLabels[scope] }}</span>
                <span class="k-card-summary">{{ knowledgeSummary(scope) }}</span>
                <button class="k-card-x" @click.stop="quickDeleteKnowledge(scope)">&times;</button>
              </div>
            </div>
          </div>
          <div v-if="flowRows.length" class="ev__outcomeblock">
            <b>{{ t('outcome_flow') }}</b>
            <div v-for="(r, i) in flowRows" :key="i" class="ev__flowrow">{{ r }}</div>
          </div>
        </div>
      </div>

      <!-- 右栏：对话面板 -->
      <div id="convPanel" class="cd ev__convpanel" :class="{ 'ev__convpanel--fs': convFs }">
        <div class="ev__convhdr">
          <h3 class="ev__convtitle">{{ t('section_conversation', { name: agentName }) }}</h3>
          <div class="ev__convbtns">
            <button
              class="ev__clearbtn"
              :title="t('clear_conv_title')"
              @click="askClearConv($event)"
            >
              {{ t('clear_conv_btn') }}
            </button>
            <button
              class="ev__fsbtn"
              :title="convFs ? t('exit_fullscreen_title') : t('fullscreen_title')"
              @click="toggleConvFullscreen"
            >
              {{ convFs ? t('fullscreen_restore_btn') : t('fullscreen_btn') }}
            </button>
          </div>
        </div>
        <div id="convMessages" ref="convMessages" class="ev__convmsgs">
          <div
            v-for="(m, i) in convMessages"
            :key="i"
            :class="['ev__bubblerow', m.role === 'user' ? 'ev__bubblerow--mine' : '']"
          >
            <div v-if="m.role === 'system'" class="ev__sysbubble">{{ m.content }}</div>
            <!-- eslint-disable-next-line vue/no-v-html — renderMarkdown 产出 -->
            <div
              v-else
              :class="['ev__bubble', m.role === 'user' ? 'ev__bubble--mine' : '']"
              v-html="renderBubble(m)"
            ></div>
          </div>
          <div v-if="convStreaming" class="ev__bubblerow">
            <div class="ev__bubble">
              <span class="chat-dots"><span></span><span></span><span></span></span>
            </div>
          </div>
          <div v-if="!convMessages.length" class="ev__convhint">
            {{ t('conv_start_hint', { name: agentType }) }}
          </div>
        </div>
        <div v-if="gateOpen" id="convInputRow" class="ev__convinput">
          <textarea
            id="convInput"
            v-model="convDraft"
            rows="1"
            :placeholder="t('conv_input_placeholder')"
            @input="autoResize($event)"
            @keydown.enter.exact.prevent="sendConversation"
          ></textarea>
          <button class="bt bt-p bt-s" @click="sendConversation">{{ t('btn_send') }}</button>
        </div>
      </div>
    </div>

    <!-- ═══ 任务日志模态（SSE 实时 / 内联） ═══ -->
    <Teleport to="body">
      <div v-if="taskLog.open" class="ev__tl-overlay" @click.self="closeTaskLog">
        <div class="ev__tl-box" :class="{ 'ev__tl-box--fs': tlFs }">
          <div class="ev__tl-hdr">
            <span class="ev__tl-title"
              >{{ taskLog.inline ? t('log_title_inline') : t('log_title_realtime') }}
              <span class="ev__tl-sub">· {{ taskLog.title }}</span></span
            >
            <span v-if="!taskLog.inline" class="ev__tl-status" v-html="taskLog.statusHtml"></span>
            <span v-else class="ev__tl-count">{{ taskLog.inlineCount }}{{ t('log_count_suffix') }}</span>
            <button class="ev__tl-btn" :title="t('log_copy_title')" @click="copyTaskLog">
              {{ t('log_copy') }}
            </button>
            <button
              v-if="!taskLog.inline && killBtnVisible"
              class="ev__tl-btn ev__tl-kill"
              :title="t('log_kill_title')"
              @click="terminateCurrentTask"
            >
              {{ t('log_kill') }}
            </button>
            <button
              v-if="!taskLog.inline"
              class="ev__tl-btn"
              :title="tlFs ? t('exit_fullscreen_title') : t('fullscreen_title')"
              @click="toggleTaskLogFullscreen"
            >
              {{ tlFs ? t('fullscreen_restore_btn') : t('fullscreen_btn') }}
            </button>
            <button class="ev__tl-btn" @click="closeTaskLog">{{ t('log_close') }}</button>
          </div>
          <div v-if="!taskLog.inline" class="ev__tl-info" v-html="taskLog.infoHtml"></div>
          <div ref="tlBody" class="ev__tl-body">
            <div v-if="taskLog.inline" v-html="taskLog.inlineHtml"></div>
            <div v-else v-html="taskLog.logHtml"></div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ═══ 重新派发确认 ═══ -->
    <Teleport to="body">
      <div v-if="redispatch.open" class="ev__glass-mask" @click.self="redispatch.open = false">
        <div class="ev__glass">
          <div class="ev__glass-ic ev__glass-ic--orange">🔄</div>
          <div>
            <div class="ev__glass-title">{{ t('redispatch_title') }}</div>
            <div class="ev__glass-sub">Seq #{{ redispatch.seq }}</div>
          </div>
          <div class="ev__glass-desc">{{ t('redispatch_desc') }}</div>
          <div class="ev__glass-btns">
            <button class="bt bt-o bt-s" @click="redispatch.open = false">
              {{ t('redispatch_cancel') }}
            </button>
            <button class="bt bt-p bt-s" @click="doRedispatch">
              {{ t('redispatch_confirm') }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ═══ A2A 补发选择 ═══ -->
    <Teleport to="body">
      <div v-if="a2aRedo.open" class="ev__glass-mask" @click.self="a2aRedo.open = false">
        <div class="ev__glass">
          <div class="ev__glass-ic ev__glass-ic--blue">🤝</div>
          <div>
            <div class="ev__glass-title">A2A 重新派发</div>
            <div class="ev__glass-sub">Seq #{{ a2aRedo.seq }}</div>
          </div>
          <div class="ev__glass-desc">
            该任务已完成但未交办下游 Agent。选择目标 Agent 补发一次 A2A 交办（不影响原任务结果）：
          </div>
          <div class="ev__a2a-list">
            <div
              v-for="a in ['vod', 'issue', 'coding', 'qa', 'sre']"
              :key="a"
              :class="[
                'ev__a2a-item',
                {
                  'ev__a2a-item--sel': a2aRedo.sel === a,
                  'ev__a2a-item--off': a2aRedo.hasSceneInfo && !a2aRedo.inScene.includes(a),
                },
              ]"
              @click="pickA2aRedo(a)"
            >
              <span class="ev__a2a-ic">{{ A2A_ICONS[a] }}</span>
              <span class="ev__a2a-name">{{ A2A_NAMES[a] }}</span>
              <span v-if="a2aRedo.hasSceneInfo && !a2aRedo.inScene.includes(a)" class="ev__a2a-hint"
                >不在场景</span
              >
              <span v-else-if="a2aRedo.doneAgents.includes(a)" class="ev__a2a-warn"
                >⚠ 已执行过，补发将重跑</span
              >
              <span v-else-if="a2aRedo.sel === a" class="ev__a2a-cur">已选</span>
            </div>
          </div>
          <div class="ev__glass-btns">
            <button class="bt bt-o bt-s" @click="a2aRedo.open = false">取消</button>
            <button
              class="bt bt-p bt-s"
              :style="{ opacity: a2aRedo.sel ? 1 : 0.5 }"
              @click="doA2aRedo"
            >
              补发
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ═══ 本机重试确认 ═══ -->
    <Teleport to="body">
      <div v-if="localRetry.open" class="ev__glass-mask" @click.self="localRetry.open = false">
        <div class="ev__glass">
          <div class="ev__glass-ic ev__glass-ic--green">🖥</div>
          <div>
            <div class="ev__glass-title">{{ t('local_retry_title') }}</div>
            <div class="ev__glass-sub">Seq #{{ localRetry.seq }}</div>
          </div>
          <div class="ev__glass-desc">{{ t('local_retry_desc') }}</div>
          <div class="ev__glass-btns">
            <button class="bt bt-o bt-s" @click="localRetry.open = false">
              {{ t('local_retry_cancel') }}
            </button>
            <button class="bt bt-g bt-s" @click="doLocalRetry">
              {{ t('local_retry_confirm') }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ═══ 知识详情弹窗 ═══ -->
    <Teleport to="body">
      <div
        v-if="knowledgeModal.open"
        class="km-overlay show"
        @click.self="knowledgeModal.open = false"
      >
        <div class="km-modal">
          <div class="km-hdr">
            {{ scopeLabels[knowledgeModal.scope] }}{{ t('outcome_knowledge') }}
          </div>
          <!-- eslint-disable-next-line vue/no-v-html — renderMarkdown 产出 -->
          <div class="km-body" v-html="knowledgeModalHtml"></div>
          <div class="km-footer">
            <button class="km-close" @click="knowledgeModal.open = false">
              {{ t('knowledge_modal_close') }}
            </button>
            <button class="km-del" @click="deleteKnowledge(knowledgeModal.scope)">
              {{ t('delete_knowledge_btn') }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ═══ 清空对话气泡确认 ═══ -->
    <Teleport to="body">
      <div v-if="clearConv.open" class="chat-del-overlay" @click.self="clearConv.open = false">
        <div class="chat-del-pop" :style="clearConv.pos">
          <div class="chat-del-title">{{ t('clear_conv_confirm') }}</div>
          <div class="chat-del-btns">
            <button class="chat-del-cancel" @click="clearConv.open = false">
              {{ tc('cancel') }}
            </button>
            <button class="chat-del-ok" @click="doClearConv">{{ tc('confirm') }}</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ═══ project 级知识删除二次确认 ═══ -->
    <Teleport to="body">
      <div v-if="projectDel.open" class="chat-del-overlay" @click.self="projectDel.open = false">
        <div class="chat-del-pop ev__centerpop">
          <div class="chat-del-title" style="color: var(--r)">
            {{ t('knowledge_delete_confirm_project') }}
          </div>
          <div class="chat-del-btns">
            <button class="chat-del-cancel" @click="projectDel.open = false">
              {{ tc('cancel') }}
            </button>
            <button class="chat-del-ok" @click="confirmProjectDelete">{{ tc('confirm') }}</button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'

const G = globalThis

// ── I18N（events.*）──
function t(key, params) {
  const full = 'events.' + key
  const v = G.I18N.t(full, params)
  return v && v !== full ? v : DEFAULTS[key] || key
}
function tc(key) {
  const full = 'common.' + key
  const v = G.I18N.t(full)
  return v && v !== full
    ? v
    : key === 'back'
      ? '返回'
      : key === 'cancel'
        ? '取消'
        : key === 'confirm'
          ? '确认'
          : key
}
const DEFAULTS = {
  back_to_requirement: '返回需求详情',
  section_context_info: '事件上下文与信息',
  section_context: '上下文',
  section_info: '信息',
  section_prompt: '接收上下文（Prompt）',
  section_processing: 'Agent 处理记录与答复结论',
  section_conversation: '{name} 对话',
  outcome_title: '对话产出',
  outcome_knowledge: '知识沉淀',
  outcome_flow: '流程操作',
  prev_event_title: '上一个事件（{name}）',
  next_event_title: '下一个事件（{name}）',
  btn_feedback: '反馈',
  btn_feedback_title: '针对该失败事件提交问题反馈（自动携带仓库/任务/引擎上下文）',
  btn_redispatch: '重新派发',
  btn_local_retry: '本机重试',
  btn_a2a_redo: 'A2A 补发',
  btn_terminate: '终止',
  terminate_title: '终止当前任务',
  redispatch_title: '重新派发确认',
  redispatch_desc: '将此事件重新派发给可用引擎执行，原任务上下文保留。',
  redispatch_cancel: '取消',
  redispatch_confirm: '确认派发',
  local_retry_title: '本机重试',
  local_retry_desc: '在本机直接重跑该任务（不经引擎调度）。',
  local_retry_cancel: '取消',
  local_retry_confirm: '本机重试',
  clear_conv_btn: '清空对话',
  clear_conv_title: '清空该事件的对话记录',
  clear_conv_confirm: '清空该事件的所有对话？',
  clear_conv_done: '已清空',
  knowledge_modal_close: '关闭',
  delete_knowledge_btn: '删除知识',
  knowledge_delete_confirm_project: '确认删除项目级知识？该仓库下所有需求共享，删除影响全局。',
  knowledge_deleted: '已删除',
  conv_start_hint: '向 {name} 发起对话…',
  conv_input_placeholder: '输入消息，Enter 发送',
  btn_send: '发送',
  no_reply: '（无回复）',
  error_reply: '回复出错',
  send_failed_paren: '（发送失败）',
  fullscreen_btn: '全屏',
  fullscreen_title: '全屏查看',
  fullscreen_restore_btn: '还原',
  exit_fullscreen_title: '退出全屏',
  view_realtime_log: '查看实时日志',
  view_log: '查看日志',
  log_title_realtime: '实时运行日志',
  log_title_inline: '事件日志',
  log_count_suffix: ' 条',
  log_copy: '复制',
  log_copy_title: '复制日志',
  log_copied: '已复制',
  log_copy_failed: '复制失败',
  log_kill: '终止',
  log_kill_title: '终止任务',
  log_close: '关闭',
  status_connecting: '连接中…',
  status_connected: '已连接',
  status_disconnected: '连接断开，重连中…',
  status_stream_end: '已结束',
  status_engine_waiting: '引擎执行中，等待输出…',
  status_terminating: '终止指令已发送…',
  task_terminated_sent: '终止指令已发送',
  terminate_failed: '终止失败：{msg}',
  a2a_redo_confirm: '该 Agent 此前已执行过，补发将重跑。确认继续？',
  a2a_redo_success: '已补发（事件 #{seq}）',
  a2a_redo_failed: '补发失败：{msg}',
  a2a_redo_skipped: '已跳过：{reason}',
  a2a_redo_unknown: '未知返回',
  redispatch_toast_success: '已重新派发',
  redispatch_toast_failed: '派发失败：{msg}',
  redispatch_token_stale: '重派已发出，但任务凭据未能刷新：重跑时无法调用 grape CLI（git-repo / issues / a2a 等接口），请重建任务后再试',
  local_retry_toast_success: '已开始本机重试',
  local_retry_toast_failed: '本机重试失败：{msg}',
  prompt_expand_hint: '展开查看接收上下文',
  loading_prompt: '加载中…',
  no_task: '无关联任务',
  prompt_empty: '（空）',
  prompt_truncated: '\n\n（已截断，共 {len} 字符）',
  char_count: '{len} 字符',
  no_conclusion: '（暂无结论）',
  no_inline_log: '暂无日志',
  load_log_failed: '日志加载失败：{msg}',
  gate_open: '对话已开放',
  gate_closed: '对话未开放',
}
const scopeLabels = computed(() => ({
  project: t('scope_project'),
  issue: t('scope_issue'),
  issue_agent: t('scope_agent_solo'),
}))
const scopeIcons = { project: '📦', issue: '📋', issue_agent: '🤖' }
const A2A_NAMES = {
  vod: 'VOD Agent',
  issue: 'Issue Agent',
  coding: 'Coding Agent',
  qa: 'QA Agent',
  sre: 'SRE Agent',
}
const A2A_ICONS = { vod: '🔍', issue: '📝', coding: '🤖', qa: '✅', sre: '🔧' }

// ── helpers ──
function fmtLocalTime(utcStr) {
  return G.window.fmtLocalTime ? G.window.fmtLocalTime(utcStr) : String(utcStr || '')
}
function vcsUrl(repoKey, path) {
  return G.window._vcsUrl ? G.window._vcsUrl(repoKey, path) : ''
}
function renderMarkdown(text) {
  return G.window.renderMarkdown ? G.window.renderMarkdown(text) : String(text || '')
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

// ── props & 数据 ──
const props = defineProps({ initialData: { type: Object, default: null } })
const data = ref(props.initialData || {})
const agentType = computed(() => data.value.agent_type || 'vod')
const agentName = computed(() => {
  const at = data.value.agent_type
  return (
    (G.window._AGENT_TYPES &&
      at &&
      G.window._AGENT_TYPES[at] &&
      G.window._AGENT_TYPES[at].display_name) ||
    at ||
    'vod'
  )
})
const backToTask = computed(() => G.window._eventFromTask && G.window._eventFromTask.taskId)
function goBackToTask() {
  const taskId = G.window._eventFromTask && G.window._eventFromTask.taskId
  if (taskId && G.window._backToTaskDetail) G.window._backToTaskDetail(taskId)
}

// Issue/PR 链接
const isPR = computed(() => String(data.value.issue_id || '').startsWith('#pr_'))
const issueLabel = computed(() => {
  const raw = String(data.value.issue_id || '')
  const num = isPR.value ? raw.replace(/^#pr_/, '').split('_')[0] : raw.replace(/^#/, '')
  return isPR.value ? 'PR #' + num : raw ? 'Issue ' + raw : 'Issue —'
})
const issueUrl = computed(() => {
  const raw = String(data.value.issue_id || '')
  const num = isPR.value ? raw.replace(/^#pr_/, '').split('_')[0] : raw.replace(/^#/, '')
  if (!num || !data.value.repo_key) return ''
  return vcsUrl(data.value.repo_key, isPR.value ? '/pull/' + num : '/issues/' + num)
})

// ── 动态片段（Issue #342 等价：全量渲染与轮询更新共用）──
const infoSummaryText = computed(() => {
  const d = data.value
  return (
    (d.event_type || '—') +
    ' · ' +
    (d.actor || '—') +
    ' · ' +
    (d.status === 'completed' ? '✅' : d.status === 'failed' ? '❌' : '⏳') +
    ' ' +
    (d.duration ? d.duration + 's' : '—')
  )
})
const engineText = computed(() => {
  const d = data.value
  let inner
  if (!d.engine_instance) inner = '<span class="ts t3">' + esc(t('engine_undispatched')) + '</span>'
  else {
    const h = d.engine_health
    let health = ''
    if (h && h.score !== null && h.score !== undefined) {
      const sc = h.score === 0 ? 'var(--r)' : h.score < 50 ? 'var(--y)' : 'var(--g)'
      const hb = h.heartbeat_alive === false ? ' · 失联' : ''
      health =
        ' · <span style="color:' +
        sc +
        ';font-weight:600">健康分 ' +
        h.score +
        '</span>' +
        (h.consecutive_failures > 0
          ? ' <span class="ts t3" style="font-size:10px">连续失败 ' +
            h.consecutive_failures +
            '</span>'
          : '') +
        hb
    }
    inner =
      '<b>' +
      esc(d.runtime || d.backend || 'OpenCode') +
      '</b>' +
      (d.engine_device ? '<span class="ts t3"> · ' + esc(d.engine_device) + '</span>' : '') +
      (d.engine_ip ? '<span class="ts t3"> · ' + esc(d.engine_ip) + '</span>' : '') +
      health
    // Issue #1255：引擎行补模型段（用量优先；已派发但无模型 → 「模型未知」；未派发不展示）
    inner += modelSegment(d)
  }
  return '<span style="color:var(--t3)">Engine:</span> ' + inner
})
// Issue #1255：引擎模型段——后端 engine_model 由 obs_llm_usage 聚合优先，缺失时给统一占位
function modelSegment(d) {
  if (!d || !d.engine_instance) return ''
  if (d.engine_model)
    return (
      ' · <span style="color:var(--t3)">' +
      esc(t('model') || '模型') +
      '</span> <b>' +
      esc(d.engine_model) +
      '</b>'
    )
  return ' · <span class="ts t3">' + esc(t('model_unknown') || '模型未知') + '</span>'
}
const durationText = computed(() => {
  const d = data.value
  return '<span style="color:var(--t3)">Duration:</span> ' + (d.duration ? d.duration + 's' : '—')
})
function redispatchVisible(d) {
  const _ts = d.started_at ? new Date(d.started_at).getTime() : 0
  const _created = d.created_at ? new Date(d.created_at).getTime() : 0
  return (
    d.status === 'failed' ||
    // Issue #566：cancelled 与 failed 同样可重新派发
    d.status === 'cancelled' ||
    ((d.status === 'running' || d.status === 'pending') &&
      ((_ts && Date.now() - _ts > 10 * 60 * 1000) ||
        (!_ts && _created && Date.now() - _created > 10 * 60 * 1000))) ||
    (d.task && d.task.task_status === 'dispatched')
  )
}
const statusText = computed(() => {
  const d = data.value
  const rv = redispatchVisible(d)
  const a2a = d.status === 'completed' && d.issue_id && !String(d.issue_id).startsWith('#pr_')
  const label =
    d.status === 'completed'
      ? t('status_success')
      : d.status === 'failed'
        ? t('status_failed')
        : d.status === 'cancelled'
          ? // Issue #566：cancelled 独立标签（此前被拼成「⏳ cancelled」）
            t('status_cancelled') || '⊘ 已取消'
          : d.status === 'pending'
            ? t('status_pending') || '排队中'
            : t('status_running_prefix') + d.status
  let html =
    '<span class="' +
    (d.status === 'completed'
      ? 'tgx'
      : d.status === 'failed'
        ? 'tr'
        : d.status === 'cancelled'
          ? 't3'
          : d.status === 'pending'
            ? ''
            : 'ty') +
    '">' +
    esc(label) +
    '</span>'
  if (rv)
    html +=
      ' <button class="bt bt-o bt-s" style="margin-left:8px" data-ev-act="redispatch">' +
      esc(t('btn_redispatch')) +
      '</button> <button class="bt bt-o bt-s" style="margin-left:4px" data-ev-act="localretry">' +
      esc(t('btn_local_retry')) +
      '</button>'
  if (a2a)
    html +=
      ' <button class="bt bt-o bt-s" style="margin-left:8px" data-ev-act="a2aredo">' +
      esc(t('btn_a2a_redo')) +
      '</button>'
  if (d.status === 'running' || d.status === 'pending')
    html +=
      ' <button class="bt bt-r bt-s" style="margin-left:8px" data-ev-act="terminate" title="' +
      esc(t('terminate_title')) +
      '">' +
      esc(t('btn_terminate')) +
      '</button>'
  return html
})
const failExpanded = ref(false)
const failReasonText = computed(() => {
  const d = data.value
  if (d.status !== 'failed') return ''
  // Issue #494：result 为空/不足时补 task.errors 归因（超时等失败此前零归因）
  const txt = (d.result || '').toString().trim()
  const errs = ((d.task && d.task.task_errors) || [])
    .map((e) => String(e || '').trim())
    .filter(Boolean)
  const parts = []
  if (txt) parts.push(txt)
  if (errs.length && errs.join('\n') !== txt) parts.push(errs.join('\n'))
  const full = parts.join('\n')
  if (!full)
    return (
      '<div style="margin-top:6px;font-size:11px;color:var(--t3,#86868b)">' +
      esc(t('no_fail_reason')) +
      '</div>'
    )
  // 长文本折叠：默认 200 字，展开/收起走事件委托
  const LIMIT = 200
  const short = full.length > LIMIT ? full.slice(0, LIMIT) + '…' : full
  const expandable = full.length > LIMIT
  const shown = expandable && failExpanded.value ? full : short
  let html =
    '<div class="ev-fail-box" style="margin-top:6px;background:rgba(255,59,48,.08);border:1px solid rgba(255,59,48,.18);border-radius:10px;padding:8px 12px;font-size:12px;color:#d32f2f;line-height:1.6"><b>' +
    esc(t('fail_reason')) +
    '：</b><span style="white-space:pre-wrap">' +
    esc(shown) +
    '</span>'
  if (expandable) {
    html +=
      ' <a href="javascript:void(0)" data-ev-act="failtoggle" style="color:var(--a);white-space:nowrap">' +
      esc(t(failExpanded.value ? 'fail_collapse' : 'fail_expand')) +
      '</a>'
  }
  html += '</div>'
  return html
})
// 换事件时收起失败原因（避免上一条的展开状态串到下一条）
watch(
  () => data.value && data.value.seq,
  () => {
    failExpanded.value = false
  }
)
const logRowText = computed(() => {
  const d = data.value
  if (d.task && d.task.task_id)
    return (
      '<div style="margin-top:4px"><a href="javascript:void(0)" data-ev-act="tasklog" style="color:var(--a);text-decoration:underline">' +
      esc(t('view_realtime_log')) +
      '</a></div>'
    )
  return (
    '<div style="margin-top:4px"><a href="javascript:void(0)" data-ev-act="inlinelog" style="color:var(--a);text-decoration:underline">' +
    esc(t('view_log')) +
    '</a></div>'
  )
})
function buildStructuredHtml(o) {
  if (!o) return ''
  const actions = Array.isArray(o.actions)
    ? o.actions
        .map(function (a) {
          return '<div>• ' + esc(a) + '</div>'
        })
        .join('')
    : ''
  const arts = Array.isArray(o.artifacts)
    ? o.artifacts
        .map(function (a) {
          return '<span class="tgx" style="margin-right:6px">' + esc(a) + '</span>'
        })
        .join('')
    : ''
  let html = ''
  if (o.conclusion)
    html +=
      '<div style="font-size:12px;margin-bottom:3px"><b>' +
      esc(t('label_conclusion')) +
      '</b> ' +
      esc(o.conclusion) +
      '</div>'
  if (actions)
    html +=
      '<div style="font-size:12px;margin-bottom:3px"><b>' +
      esc(t('label_actions')) +
      '</b> ' +
      actions +
      '</div>'
  if (arts)
    html +=
      '<div style="font-size:12px;margin-bottom:3px"><b>' +
      esc(t('label_artifacts')) +
      '</b> ' +
      arts +
      '</div>'
  if (o.next_step)
    html +=
      '<div style="font-size:12px;margin-bottom:3px"><b>' +
      esc(t('label_next_step')) +
      '</b> ' +
      esc(o.next_step) +
      '</div>'
  if (o.issue_summary)
    html +=
      '<div style="font-size:12px;color:var(--a)"><b>' +
      esc(t('label_issue_summary')) +
      '</b> ' +
      esc(o.issue_summary) +
      '</div>'
  return html
}
function tryStructuredResult(r) {
  if (!r) return null
  const s = String(r).trim()
  if (!(s.startsWith('{') && s.endsWith('}'))) return null
  try {
    const o = JSON.parse(s)
    return o && (o.conclusion || o.actions || o.artifacts || o.issue_summary) ? o : null
  } catch (e) {
    return null
  }
}
const processingText = computed(() => {
  const d = data.value
  const result = d.result || d.summary || ''
  const arch = d.archive || null
  const structuredHtml = buildStructuredHtml((arch && arch.output) || tryStructuredResult(result))
  if (structuredHtml)
    return (
      '<div style="margin-bottom:12px;padding:10px 12px;background:var(--bg);border-radius:8px;border-left:3px solid var(--a)">' +
      structuredHtml +
      '</div>'
    )
  return result
    ? renderMarkdown(result)
    : '<div style="color:var(--t3)">' + esc(t('no_conclusion')) + '</div>'
})

// statusText/logRowText 里动态按钮用事件委托（@click 绑在容器 div 上，v-html 渲染前后都有效）
function onInfoClick(ev) {
  const a = ev.target.closest('[data-ev-act]')
  if (!a) return
  const act = a.dataset.evAct
  if (act === 'redispatch') ((redispatch.open = true), (redispatch.seq = data.value.seq))
  else if (act === 'localretry') ((localRetry.open = true), (localRetry.seq = data.value.seq))
  else if (act === 'a2aredo') openA2aRedo(data.value.seq)
  else if (act === 'terminate') doTerminateEvent(data.value.seq)
  else if (act === 'tasklog') openTaskLog(data.value.task.task_id, 'seq#' + data.value.seq)
  else if (act === 'inlinelog') openInlineLog(data.value.seq, 'seq#' + data.value.seq)
  else if (act === 'failtoggle') failExpanded.value = !failExpanded.value // Issue #494：失败原因展开/收起
}
// ── 折叠区块 ──
const openSections = reactive({ ctxInfo: true, promptBody: false })
function toggleSection(id) {
  openSections[id] = !openSections[id]
}
function togglePromptSection() {
  openSections.promptBody = !openSections.promptBody
  if (openSections.promptBody) loadEventPrompt()
}

// ── prompt 加载 ──
const promptMeta = ref('')
const promptContent = ref('')
let promptLoadedSeq = 0
function loadEventPrompt() {
  const taskId = data.value.task && data.value.task.task_id
  const seq = data.value.seq
  if (!taskId) {
    promptContent.value = t('no_task')
    promptMeta.value = ''
    return
  }
  if (promptLoadedSeq === seq) return
  promptLoadedSeq = seq
  promptContent.value = t('loading_prompt')
  G.API.get('/task-logs/' + encodeURIComponent(taskId) + '/info')
    .then(function (info) {
      if (!info) {
        promptContent.value = t('load_failed')
        return
      }
      const prompt = info.prompt || ''
      const fullLen = info.prompt_full_length || 0
      promptMeta.value = fullLen > 0 ? t('char_count', { len: fullLen }) : ''
      if (!prompt) promptContent.value = t('prompt_empty')
      else {
        const truncated =
          fullLen > prompt.length ? '\n\n' + t('prompt_truncated', { len: fullLen }) : ''
        promptContent.value = prompt + truncated
      }
    })
    .catch(function (e) {
      if (e && e.name === 'AbortError' && e.silent) return
      promptContent.value = t('load_failed')
    })
}
// prompt 全屏
let promptFs = false
function togglePromptFullscreen() {
  const body = document.querySelector('.ev__promptbody')
  if (!body) return
  if (!promptFs) {
    if (!openSections.promptBody) return
    const fs = document.createElement('div')
    fs.id = 'promptFullscreen'
    fs.style.cssText =
      'position:fixed;inset:0;z-index:1001;background:#000;display:flex;flex-direction:column;box-sizing:border-box'
    const bar = document.createElement('div')
    bar.style.cssText =
      'display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid #2a3140;flex-shrink:0'
    const title = document.createElement('span')
    title.style.cssText = 'font-size:13px;font-weight:600;color: var(--on-accent);margin-right:auto'
    title.textContent = t('section_prompt')
    const btn = document.createElement('button')
    btn.textContent = t('fullscreen_restore_btn')
    btn.style.cssText =
      'border:1px solid #3a4150;background:transparent;color:#c8d0e0;border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer'
    btn.onclick = togglePromptFullscreen
    bar.appendChild(title)
    bar.appendChild(btn)
    fs.appendChild(bar)
    body.style.maxHeight = 'none'
    body.style.flex = '1'
    body.style.borderRadius = '0'
    fs.appendChild(body)
    document.body.appendChild(fs)
    promptFs = true
  } else {
    const fs = document.getElementById('promptFullscreen')
    if (!fs) return
    // 还原到组件里的原位置：重新挂到 ev 容器
    const evRoot = document.querySelector('.ev')
    body.style.maxHeight = '400px'
    body.style.flex = ''
    body.style.borderRadius = '8px'
    if (evRoot) evRoot.appendChild(body)
    fs.remove()
    promptFs = false
  }
}

// ── 对话面板（Issue #35 条件渲染）──
const gateOpen = ref(false)
const gateLoaded = ref(false)
const gateText = computed(() =>
  gateOpen.value
    ? '<span class="tgx">' + esc(t('gate_open')) + '</span>'
    : '<span class="tr">' + esc(t('gate_closed')) + '</span>'
)
const convPanelVisible = ref(false)
const convMessages = ref([])
const convDraft = ref('')
const convStreaming = ref(false)
const knowledge = ref({})
const knowledgeCards = computed(() =>
  Object.keys(knowledge.value || {}).filter(function (s) {
    return knowledge.value[s]
  })
)
const flowRows = computed(() =>
  (Array.isArray(convMessages.value) ? convMessages.value : [])
    .filter(function (m) {
      return m.role === 'system' && (m.content || '').indexOf('🔄') === 0
    })
    .map(function (m) {
      return m.content
    })
)
const outcomeVisible = computed(() => knowledgeCards.value.length > 0 || flowRows.value.length > 0)
function knowledgeSummary(scope) {
  const content = knowledge.value[scope] || ''
  return (
    content
      .substring(0, 30)
      .replace(/\n/g, ' ')
      .replace(/- \[seq[^\]]*\] /g, '') +
    '… (' +
    content.length +
    '字)'
  )
}
function renderBubble(m) {
  return renderMarkdown(m.content || '')
}
function loadConversation() {
  const seq = data.value.seq
  if (!seq) return
  G.API.get('/events/' + seq + '/thread')
    .then(function (d) {
      const gate = d.gate || {}
      const msgs = Array.isArray(d.messages) ? d.messages : []
      const hasMsgs = msgs.length > 0
      gateOpen.value = !!gate.open
      gateLoaded.value = true
      convPanelVisible.value = hasMsgs || !!gate.open
      convMessages.value = msgs
      knowledge.value = d.knowledge || {}
      nextTick(function () {
        const el = document.getElementById('convMessages')
        if (el) el.scrollTop = el.scrollHeight
      })
    })
    .catch(function () {
      /* ignore：与老代码同款静默降级 */
    })
}
function autoResize(ev) {
  const el = ev && ev.target
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 120) + 'px'
}

// 对话发送（v31 submit/stream）
let convStreamAbort = null
function setConvTask(seq, task) {
  try {
    G.window.sessionStorage.setItem('conv_task_' + seq, JSON.stringify(task))
  } catch (e) {
    /* ignore */
  }
}
function getConvTask(seq) {
  try {
    const s = G.window.sessionStorage.getItem('conv_task_' + seq)
    return s ? JSON.parse(s) : null
  } catch (e) {
    return null
  }
}
function clearConvTask(seq) {
  try {
    G.window.sessionStorage.removeItem('conv_task_' + seq)
  } catch (e) {
    /* ignore */
  }
}
function sendConversation() {
  const seq = data.value.seq
  const msg = (convDraft.value || '').trim()
  if (!msg || !seq) return
  convDraft.value = ''
  convMessages.value.push({ role: 'user', content: msg })
  convStreaming.value = true
  nextTick(scrollConv)
  G.API.post('/events/' + seq + '/chat/submit', { message: msg })
    .then(function (r) {
      if (r.status === 'error' || !r.task_id) {
        convStreaming.value = false
        convMessages.value.push({ role: 'agent', content: r.error || t('send_failed_paren') })
        return
      }
      setConvTask(seq, { task_id: r.task_id, status: 'running' })
      streamConvTask(r.task_id, seq)
    })
    .catch(function () {
      convStreaming.value = false
      convMessages.value.push({ role: 'agent', content: t('send_failed_paren') })
    })
}
function upsertConvBubble(html) {
  // 单气泡 upsert（批6同款）
  let idx = -1
  for (let i = convMessages.value.length - 1; i >= 0; i--) {
    if (convMessages.value[i]._streaming) {
      idx = i
      break
    }
  }
  if (idx < 0) {
    convMessages.value.push({ role: 'agent', content: '', _streaming: true })
    idx = convMessages.value.length - 1
  }
  convMessages.value[idx]._html = html
  nextTick(scrollConv)
}
async function streamConvTask(taskId, seq) {
  if (convStreamAbort) {
    convStreamAbort.abort()
    convStreamAbort = null
  }
  const ac = new AbortController()
  convStreamAbort = ac
  let finalText = ''
  try {
    const token = (await G.API.ensureTokenValid()) || ''
    const resp = await fetch('/api/events/chat/task/' + taskId + '/stream', {
      headers: { Authorization: 'Bearer ' + token },
      signal: ac.signal,
    })
    if (!resp.ok || !resp.body) {
      upsertConvBubble(t('send_failed_paren'))
      convStreaming.value = false
      clearConvTask(seq)
      return
    }
    const reader = resp.body.getReader()
    const dec = new TextDecoder()
    let buf = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() || ''
      lines.forEach(function (line) {
        if (!line.startsWith('data: ')) return
        try {
          const d = JSON.parse(line.substring(6))
          if (d.type === 'done') {
            finalText = d.text || finalText
            upsertConvBubble(renderMarkdown(finalText) || t('no_reply'))
            convStreaming.value = false
            clearConvTask(seq)
            loadConversation()
          } else if (d.type === 'error') {
            upsertConvBubble(renderMarkdown(d.text || '') || t('error_reply'))
            convStreaming.value = false
            clearConvTask(seq)
          } else if (d.type === 'text' && d.text) {
            finalText += d.text
            upsertConvBubble(renderMarkdown(finalText))
          } else if (d.type === 'thinking') {
            convStreaming.value = true
          }
        } catch (e) {
          /* ignore */
        }
      })
      nextTick(scrollConv)
    }
  } catch (e) {
    if (e && e.name === 'AbortError') return
    convStreaming.value = true
  }
}
function scrollConv() {
  const el = document.getElementById('convMessages')
  if (el) el.scrollTop = el.scrollHeight
}
// resume（app.js 切回 event 页时调）
G.window.resumeConvTask = function () {
  const seq = data.value.seq
  if (!seq) return
  const task = getConvTask(seq)
  if (task && task.task_id) {
    G.API.get('/events/chat/task/' + task.task_id + '/status')
      .then(function (st) {
        if (st.status === 'running') {
          convStreaming.value = true
          streamConvTask(task.task_id, seq)
        } else {
          clearConvTask(seq)
          loadConversation()
        }
      })
      .catch(function () {
        /* ignore：与老代码同款静默降级 */
      })
  } else {
    G.API.get('/events/' + seq + '/thread')
      .then(function (d) {
        const msgs = d.messages || []
        const lastMsg = msgs[msgs.length - 1]
        if (lastMsg && lastMsg.role === 'user' && lastMsg.task_id) {
          setConvTask(seq, { task_id: lastMsg.task_id, status: 'running' })
          G.window.resumeConvTask()
        }
      })
      .catch(function () {
        /* ignore：与老代码同款静默降级 */
      })
  }
}
// 对话全屏（CSS 类切换，DOM 不搬运）
const convFs = ref(false)
function toggleConvFullscreen() {
  convFs.value = !convFs.value
}

// 清空对话
const clearConv = reactive({ open: false, pos: {} })
function askClearConv(ev) {
  clearConv.pos = {
    left: Math.min(ev.clientX, G.window.innerWidth - 240) + 'px',
    top: Math.min(ev.clientY + 10, G.window.innerHeight - 140) + 'px',
  }
  clearConv.open = true
}
function doClearConv() {
  clearConv.open = false
  const seq = data.value.seq
  G.API.del('/events/' + seq + '/chat')
    .then(function (d) {
      if (d.ok) {
        G.app.showToast(t('clear_conv_done'))
        loadConversation()
      } else G.app.showToast((d.detail && d.detail.message) || '清空失败')
    })
    .catch(function (e) {
      G.app.showToast(e.message || '清空失败')
    })
}

// ── 知识弹窗/删除 ──
const knowledgeModal = reactive({ open: false, scope: '' })
const knowledgeModalHtml = computed(function () {
  return renderMarkdown((knowledge.value || {})[knowledgeModal.scope] || '')
})
function showKnowledgeDetail(scope) {
  knowledgeModal.scope = scope
  knowledgeModal.open = true
}
const projectDel = reactive({ open: false, scope: '' })
function quickDeleteKnowledge(scope) {
  if (scope === 'project') projectDel.open = true
  else deleteKnowledge(scope)
}
function confirmProjectDelete() {
  projectDel.open = false
  deleteKnowledge(projectDel.scope)
}
function deleteKnowledge(scope) {
  knowledgeModal.open = false
  G.API.del('/events/' + data.value.seq + '/knowledge/' + scope)
    .then(function (d) {
      if (d.ok) {
        G.app.showToast(t('knowledge_deleted'))
        loadConversation()
      } else G.app.showToast((d.detail && d.detail.message) || '删除失败')
    })
    .catch(function (e) {
      G.app.showToast(e.message || '删除失败')
    })
}

// ── 轮询（Issue #342：非终态 5s 局部更新，指纹防抖）──
let pollTimer = null
let pollSeq = 0
let pollBusy = false
let lastFp = ''
function fingerprint(d) {
  return JSON.stringify([
    d.status,
    d.duration,
    d.result,
    d.summary,
    d.engine_instance,
    d.runtime,
    d.backend,
    d.engine_device,
    d.engine_ip,
    (d.engine_health && d.engine_health.score) || null,
    d.archive && d.archive.output ? JSON.stringify(d.archive.output) : null,
    (d.task && d.task.task_status) || '',
    (d.task && d.task.task_id) || '',
    (d.agent_logs || []).length,
  ])
}
function startPoll(d) {
  const st = (d && d.status) || ''
  if (st === 'completed' || st === 'failed' || st === 'cancelled') {
    stopPoll()
    return
  }
  const seq = d && d.seq
  if (!seq) {
    stopPoll()
    return
  }
  if (pollTimer && pollSeq === seq) return
  stopPoll()
  pollSeq = seq
  pollTimer = setInterval(function () {
    if (pollBusy) return
    if (G.window._currentView !== 'event') {
      stopPoll()
      return
    }
    pollBusy = true
    G.API.get('/events/detail/' + pollSeq)
      .then(function (d2) {
        pollBusy = false
        if (G.window._currentView !== 'event' || pollSeq !== d2.seq) return
        if (d2.status === 'completed' || d2.status === 'failed' || d2.status === 'cancelled') {
          stopPoll()
          data.value = d2
          return
        }
        const fp = fingerprint(d2)
        if (fp === lastFp) return
        lastFp = fp
        data.value = d2
      })
      .catch(function () {
        pollBusy = false
      })
  }, 5000)
}
function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  pollSeq = 0
}
G.window._stopEventPoll = stopPoll

// ── 导航 ──
function navEvent(seq) {
  if (!seq || seq < 1) return
  G.API.get('/events/detail/' + seq)
    .then(function (d) {
      const titleEl = document.getElementById('pageTitle')
      if (titleEl) titleEl.textContent = G.I18N.t('events.page_title_prefix', { seq: seq })
      data.value = d
      lastFp = fingerprint(d)
      openSections.ctxInfo = true
      openSections.promptBody = false
      promptLoadedSeq = 0
      loadConversation()
      startPoll(d)
      try {
        G.window.history.replaceState(null, '', '#event/' + seq)
      } catch (e) {
        /* ignore */
      }
    })
    .catch(function (e) {
      const status = (e && e.status) || 0
      if (status === 403) {
        G.app.showToast(t('nav_forbidden', { seq: seq }))
        return
      }
      const detail = (e && e.detail) || {}
      const maxSeq = detail.max_seq
      if (maxSeq && maxSeq > 0 && maxSeq !== seq) navEvent(maxSeq)
    })
}
// 契约由入口 entries/event-view.js 持有（fresh mount + initialData 注入）。
// 组件内只暴露「就地更新」内部钩子，不占用 window.renderEventDetail —— 否则
// nav('event', seq) 第二次进来会命中本函数：它只改 data/轮询，不重建 DOM，
// 而 nav 已把主区写成 loading 占位，页面就永久停在「加载中...」（刷新才恢复）。
G.window.__eventViewApply = function (d) {
  data.value = d
  lastFp = fingerprint(d)
  loadConversation()
  startPoll(d)
}

// ── 反馈 ──
function openFeedback() {
  if (G.window.openFeedbackDialog) G.window.openFeedbackDialog({ event_seq: data.value.seq })
}

// ── 重派发/本机重试/A2A ──
const redispatch = reactive({ open: false, seq: 0 })
function doRedispatch() {
  redispatch.open = false
  G.API.post('/events/' + redispatch.seq + '/redispatch')
    .then(function (d) {
      // Issue #2508: token_reissued=false means the task credential was not
      // refreshed, so the rerun cannot call the grape CLI at all.
      if (d && d.token_reissued === false) {
        G.app.showToast(t('redispatch_token_stale'))
      } else {
        G.app.showToast(t('redispatch_toast_success'))
      }
      G.app.nav('event', (d && d.event_seq) ? d.event_seq : redispatch.seq)
    })
    .catch(function (e) {
      G.app.showToast(
        t('redispatch_toast_failed', { msg: (e.detail && e.detail.message) || e.message || e })
      )
    })
}
const localRetry = reactive({ open: false, seq: 0 })
function doLocalRetry() {
  localRetry.open = false
  G.API.post('/events/' + localRetry.seq + '/redispatch?local_only=true')
    .then(function () {
      G.app.showToast(t('local_retry_toast_success'))
      G.app.nav('event', localRetry.seq)
    })
    .catch(function (e) {
      G.app.showToast(
        t('local_retry_toast_failed', { msg: (e.detail && e.detail.message) || e.message || e })
      )
    })
}
const a2aRedo = reactive({
  open: false,
  seq: 0,
  sel: '',
  inScene: [],
  doneAgents: [],
  next: '',
  hasSceneInfo: false,
})
function openA2aRedo(seq) {
  G.API.get('/events/' + seq + '/a2a-redo/scene')
    .then(function (s) {
      showA2aRedo(seq, s || {})
    })
    .catch(function () {
      showA2aRedo(seq, {})
    })
}
function showA2aRedo(seq, sceneInfo) {
  const agents = ['vod', 'issue', 'coding', 'qa', 'sre']
  a2aRedo.seq = seq
  a2aRedo.inScene = sceneInfo.agents_in_scene || []
  a2aRedo.doneAgents = sceneInfo.upstream_agents || []
  a2aRedo.next = sceneInfo.next_agent || ''
  a2aRedo.hasSceneInfo = a2aRedo.inScene.length > 0
  a2aRedo.sel = a2aRedo.next && agents.includes(a2aRedo.next) ? a2aRedo.next : ''
  a2aRedo.open = true
}
function pickA2aRedo(a) {
  if (a2aRedo.hasSceneInfo && !a2aRedo.inScene.includes(a)) return
  a2aRedo.sel = a
}
function doA2aRedo() {
  const agent = a2aRedo.sel
  const seq = a2aRedo.seq
  if (!agent) return
  const fire = function () {
    a2aRedo.open = false
    G.API.post('/events/' + seq + '/a2a-redo', { agent: agent })
      .then(function (d) {
        if (d && d.skipped) G.app.showToast(t('a2a_redo_skipped', { reason: d.skipped }))
        else if (d && d.seq) {
          G.app.showToast(t('a2a_redo_success', { seq: d.seq }))
          G.app.nav('event', d.seq)
        } else
          G.app.showToast(
            t('a2a_redo_failed', { msg: (d && (d.error || d.message)) || t('a2a_redo_unknown') })
          )
      })
      .catch(function (e) {
        G.app.showToast(
          t('a2a_redo_failed', { msg: (e.detail && e.detail.message) || e.message || e })
        )
      })
  }
  if (a2aRedo.doneAgents.includes(agent)) {
    G.app.showConfirm(t('a2a_redo_confirm'), fire, { danger: true })
    return
  }
  fire()
}

// ── 终止 ──
function doTerminateEvent(seq) {
  G.API.post('/events/' + seq + '/terminate')
    .then(function () {
      G.app.showToast(t('task_terminated_sent'))
      G.API.get('/events/detail/' + seq).then(function (d) {
        data.value = d
      })
    })
    .catch(function (e) {
      G.app.showToast(t('terminate_failed', { msg: e.message || e }))
    })
}

// ── 任务日志模态（SSE 实时 + 内联）──
const taskLog = reactive({
  open: false,
  inline: false,
  title: '',
  taskId: '',
  seq: 0,
  following: true,
  statusHtml: '',
  infoHtml: '',
  logHtml: '',
  inlineHtml: '',
  inlineCount: 0,
  hasMsg: false,
})
const killBtnVisible = ref(false)
let tlAborter = null
let tlWaitTimer = null
let tlStatusTimer = null
function tlStatus(text, color) {
  taskLog.statusHtml = '<span style="color:' + (color || 'var(--t3)') + '">' + esc(text) + '</span>'
}
function tlLine(payload) {
  const type = payload.type || 'log'
  const time = payload.created_at
    ? '<span style="color:#7a85a0;margin-right:6px;font-size:13px">' +
      fmtLocalTime(payload.created_at).substring(11, 19) +
      '</span>'
    : ''
  if (type === 'tool_use') {
    let inputHtml = ''
    try {
      inputHtml =
        '<div style="margin:4px 0 4px 24px;white-space:pre-wrap;word-break:break-all;color:#b8c2d6;font-size:13px">' +
        esc(JSON.stringify(payload.input || {}, null, 2)) +
        '</div>'
    } catch (e) {
      /* ignore */
    }
    return (
      '<div style="margin-bottom:8px">' +
      time +
      '🔧 <span style="color:#5b9eff;font-weight:600">' +
      esc(payload.tool || 'tool') +
      '</span>' +
      inputHtml +
      '</div>'
    )
  }
  const icon =
    type === 'error'
      ? '❌'
      : type === 'task:retry'
        ? '↻'
        : type === 'reasoning'
          ? '🤔'
          : type === 'step_start'
            ? '▶'
            : type === 'step_finish'
              ? '✅'
              : type === 'text'
                ? '💬'
                : '·'
  const color =
    type === 'error'
      ? '#ff5b5b'
      : type === 'task:retry'
        ? '#f0a741'
        : type === 'reasoning'
          ? '#a0aac0'
          : '#e0e6f0'
  return (
    '<div style="margin-bottom:8px">' +
    time +
    icon +
    ' <span style="color:' +
    color +
    ';white-space:pre-wrap;word-break:break-all">' +
    esc(payload.content || '') +
    '</span></div>'
  )
}
// Issue #701：设备显示名唯一来源 runtime_credentials.name，空名统一占位「未命名设备」
function logWorkerName(v) {
  const s = String(v || '').trim()
  if (s) return s
  const lbl = tc('unnamed_worker')
  return lbl && lbl !== 'unnamed_worker' ? lbl : '未命名设备'
}
function fmtTok(v) {
  v = Number(v) || 0
  return v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : v >= 1000 ? (v / 1000).toFixed(1) + 'K' : String(v)
}
// Issue #1255：执行模型 + 该任务真实 token 明细（主模型内联 ↑in ↓out，其余模型收成 +N 悬浮）。
// 与老页 events.js 的 _tlLoadInfo 同口径；Vue 移植时整段丢失，实时日志只剩 CPU/MEM。
function logModelSegment(info) {
  if (!info.model)
    return '🧠 <span style="color:var(--t3)">' + esc(t('model_unknown')) + '</span>'
  const ms = Array.isArray(info.models) ? info.models : []
  const top = ms.find(function (m) {
    return m && m.model === info.model
  }) || ms[0] || null
  let out = '🧠 ' + esc(info.model)
  if (top && (top.input || top.output))
    out +=
      ' <span style="color:var(--t3)">· ↑' +
      fmtTok(top.input) +
      ' ↓' +
      fmtTok(top.output) +
      '</span>'
  const others = ms.filter(function (m) {
    return m && m.model !== info.model
  })
  const tip = others
    .map(function (m) {
      return m.model + ' (↑' + fmtTok(m.input) + ' ↓' + fmtTok(m.output) + ')'
    })
    .join('、')
  if (tip) out += ' <span class="t3" title="' + esc(tip) + '">+' + others.length + '</span>'
  return out
}
function openTaskLogModal(taskId, title) {
  closeTaskLog()
  taskLog.open = true
  taskLog.inline = false
  taskLog.taskId = taskId
  taskLog.title = title || taskId
  taskLog.seq = 0
  taskLog.following = true
  taskLog.logHtml = ''
  taskLog.hasMsg = false
  tlStatus(t('status_connecting'))
  G.API.get('/task-logs/' + encodeURIComponent(taskId) + '/info')
    .then(function (info) {
      if (!info) return
      const bits = []
      if (info.agent_name)
        bits.push(
          '🤖 ' +
            esc(info.agent_name) +
            (info.agent_version
              ? ' <span style="color:var(--t3)">v' + esc(info.agent_version) + '</span>'
              : '')
        )
      // [B5 沙箱迁移 → spec 2026-11-14 cred_uid8 修正] 云沙箱引擎：🖥 Provider 名 + #cred_uid8
      // （title 留 systemctl 服务名线索；服务名实为 sandbox-worker-<credential_uid 前 8 位>，
      //  非 credential_id——走读误判已勘误，spec 2026-11-14-eventview-cred-uid8-fix）
      if (info.sandbox_provider)
        bits.push(
          '🖥 ' +
            esc(info.sandbox_provider) +
            (info.credential_uid8
              ? ' <span style="color:var(--t3)" title="Credential (systemctl: sandbox-worker-' +
                esc(info.credential_uid8) +
                ')">#' +
                esc(info.credential_uid8) +
                '</span>'
              : '')
        )
      // 🌐 沙箱 id 位本期不激活（spec 拍板②：task→sandbox 关联键未核，后端不下发 sandbox_id）
      if (info.sandbox_id) bits.push('🌐 ' + esc(info.sandbox_id))
      if (info.device_name) bits.push('🖥 ' + esc(info.device_name))
      if (info.last_ip) bits.push('🌐 ' + esc(info.last_ip))
      if (info.owner_name) bits.push('👤 ' + esc(info.owner_name))
      if (info.spec) bits.push('⚙️ ' + esc(info.spec))
      bits.push(logModelSegment(info))
      // [B5 沙箱迁移] 云沙箱引擎的心跳指标是 worker 宿主机的（非沙箱容器）——不展示防语义误导
      if (info.cpu_percent && !info.sandbox_provider)
        bits.push('📊 CPU ' + Number(info.cpu_percent).toFixed(1) + '%')
      if (info.memory_percent && !info.sandbox_provider)
        bits.push('💾 MEM ' + Number(info.memory_percent).toFixed(1) + '%')
      taskLog.infoHtml = bits
        .map(function (b) {
          return '<span>' + b + '</span>'
        })
        .join('')
    })
    .catch(function () {
      /* ignore：与老代码同款静默降级 */
    })
  taskLogStream()
  tlCheckTaskStatus()
  tlStatusTimer = setInterval(tlCheckTaskStatus, 5000)
}
function openTaskLog(taskId, title) {
  openTaskLogModal(taskId, title)
}
async function taskLogStream() {
  const token = (await G.API.ensureTokenValid()) || ''
  const ac = new AbortController()
  tlAborter = ac
  fetch('/api/task-logs/' + encodeURIComponent(taskLog.taskId) + '/stream?since=' + taskLog.seq, {
    headers: { Authorization: 'Bearer ' + token },
    signal: ac.signal,
  })
    .then(function (resp) {
      if (!resp.ok) throw new Error('HTTP ' + resp.status)
      tlStatus(t('status_connected'), 'var(--g, #2ecc71)')
      taskLog.hasMsg = false
      const reader = resp.body.getReader()
      const dec = new TextDecoder()
      let buf = ''
      function read() {
        return reader.read().then(function (_a2) {
          const done = _a2.done,
            value = _a2.value
          if (done) {
            tlStatus(t('status_stream_end'), 'var(--t3)')
            killBtnVisible.value = false
            return
          }
          buf += dec.decode(value, { stream: true })
          const parts = buf.split('\n\n')
          buf = parts.pop()
          for (const part of parts) {
            if (!part.startsWith('data: ')) continue
            let evt
            try {
              evt = JSON.parse(part.slice(6))
            } catch (e) {
              continue
            }
            if (typeof evt.seq === 'number' && evt.seq <= taskLog.seq) continue
            taskLog.seq =
              typeof evt.seq === 'number' && evt.seq > taskLog.seq ? evt.seq : taskLog.seq
            if (!taskLog.hasMsg) {
              taskLog.hasMsg = true
              tlStatus(t('status_connected'), 'var(--g, #2ecc71)')
            }
            taskLog.logHtml += tlLine(evt)
            if (taskLog.following)
              nextTick(function () {
                const el = document.querySelector('.ev__tl-body')
                if (el) el.scrollTop = el.scrollHeight
              })
          }
          return read()
        })
      }
      return read()
    })
    .catch(function (err) {
      if (err && err.name === 'AbortError') return
      tlStatus(t('status_disconnected'), 'var(--r, #e74c3c)')
      setTimeout(function () {
        if (taskLog.open && !taskLog.inline) taskLogStream()
      }, 2000)
    })
}
function tlCheckTaskStatus() {
  if (!taskLog.taskId || taskLog.inline) return
  G.API.get('/runtime/tasks/' + encodeURIComponent(taskLog.taskId) + '/status')
    .then(function (d) {
      if (!d || !d.status) return
      killBtnVisible.value = d.status === 'running' || d.status === 'dispatched'
    })
    .catch(function () {
      /* ignore：与老代码同款静默降级 */
    })
}
function terminateCurrentTask() {
  if (!taskLog.taskId) return
  G.API.post('/runtime/tasks/' + encodeURIComponent(taskLog.taskId) + '/cancel')
    .then(function () {
      tlStatus(t('status_terminating'), 'var(--r, #e74c3c)')
      killBtnVisible.value = false
    })
    .catch(function (e) {
      G.app.showToast(t('terminate_failed', { msg: e.message || e }))
    })
}
function copyTaskLog() {
  const el = document.querySelector('.ev__tl-body')
  if (!el) return
  const txt = el.innerText
  if (G.window.navigator.clipboard && G.window.navigator.clipboard.writeText) {
    G.window.navigator.clipboard.writeText(txt).then(
      function () {
        G.app.showToast(t('log_copied'))
      },
      function () {
        G.app.showToast(t('log_copy_failed'))
      }
    )
  } else {
    const ta = document.createElement('textarea')
    ta.value = txt
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy')
      G.app.showToast(t('log_copied'))
    } catch (e) {
      G.app.showToast(t('log_copy_failed'))
    }
    ta.remove()
  }
}
function openInlineLog(seq, title) {
  closeTaskLog()
  if (!seq) return
  G.API.get('/events/detail/' + seq)
    .then(function (d) {
      const logs = d.agent_logs || []
      let bodyHtml
      if (logs.length) {
        bodyHtml = logs
          .map(function (l) {
            if (!l) return ''
            const type = l.type || 'log'
            const time = l.time
              ? '<span style="color:#7a85a0;margin-right:6px">' +
                (l.time.substring(11, 19) || '') +
                '</span>'
              : ''
            const msg = l.msg || ''
            if (type === 'tool_use') {
              let toolName = msg
              let inputHtml = ''
              try {
                const parsed = JSON.parse(msg)
                if (parsed.tool) toolName = esc(parsed.tool)
                if (parsed.input)
                  inputHtml =
                    '<div style="margin:4px 0 4px 24px;white-space:pre-wrap;word-break:break-all;color:#b8c2d6;font-size:13px">' +
                    esc(JSON.stringify(parsed.input, null, 2)) +
                    '</div>'
              } catch (e) {
                /* ignore */
              }
              return (
                '<div style="margin-bottom:8px">' +
                time +
                '🔧 <span style="color:#5b9eff;font-weight:600">' +
                toolName +
                '</span>' +
                inputHtml +
                '</div>'
              )
            }
            const icon =
              type === 'error'
                ? '❌'
                : type === 'task:retry'
                  ? '↻'
                  : type === 'reasoning'
                    ? '🤔'
                    : type === 'step_start'
                      ? '▶'
                      : type === 'text'
                        ? '💬'
                        : '·'
            const color =
              type === 'error'
                ? '#ff5b5b'
                : type === 'task:retry'
                  ? '#f0a741'
                  : type === 'reasoning'
                    ? '#a0aac0'
                    : '#e0e6f0'
            return (
              '<div style="margin-bottom:8px">' +
              time +
              icon +
              ' <span style="color:' +
              color +
              ';white-space:pre-wrap;word-break:break-all">' +
              esc(msg) +
              '</span></div>'
            )
          })
          .join('')
      } else {
        bodyHtml =
          '<div style="margin-bottom:8px">❌ <span style="color:#ff5b5b;white-space:pre-wrap;word-break:break-all">' +
          esc(d.result || t('no_inline_log')) +
          '</span></div>'
      }
      taskLog.open = true
      taskLog.inline = true
      taskLog.title = title || 'seq#' + seq
      taskLog.inlineCount = logs.length
      taskLog.inlineHtml = bodyHtml
    })
    .catch(function (e) {
      G.app.showToast(t('load_log_failed', { msg: e.message }))
    })
}
function closeTaskLog() {
  taskLog.open = false
  if (tlAborter) {
    tlAborter.abort()
    tlAborter = null
  }
  if (tlWaitTimer) {
    clearTimeout(tlWaitTimer)
    tlWaitTimer = null
  }
  if (tlStatusTimer) {
    clearInterval(tlStatusTimer)
    tlStatusTimer = null
  }
  killBtnVisible.value = false
  tlFs.value = false
}
G.window.openTaskLog = openTaskLog
G.window.closeTaskLog = closeTaskLog

// 全屏切换（task log）
const tlFs = ref(false)
function toggleTaskLogFullscreen() {
  tlFs.value = !tlFs.value
}

// Esc 退出全屏（对话/任务日志），与事件页 v1 的键位一致
function onFsKeydown(e) {
  if (e.key !== 'Escape') return
  if (tlFs.value) tlFs.value = false
  if (convFs.value) convFs.value = false
}

onBeforeUnmount(function () {
  stopPoll()
  closeTaskLog()
  document.removeEventListener('keydown', onFsKeydown)
  if (convStreamAbort) convStreamAbort.abort()
})

onMounted(function () {
  document.addEventListener('keydown', onFsKeydown)
  if (data.value.seq) {
    lastFp = fingerprint(data.value)
    loadConversation()
    startPoll(data.value)
  }
})
</script>

<style scoped>
.ev {
  min-height: 100%;
}
.ev__back {
  margin-bottom: 12px;
}
.ev__grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 14px;
  height: calc(100vh - 160px);
  overflow: hidden;
}
.ev__main {
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.ev__navbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  margin-bottom: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--b);
}
.ev__seq {
  font-size: 12px;
  font-weight: 600;
}
.ev__feedback {
  margin-left: auto;
}
.ev__section {
  flex-shrink: 0;
  margin-bottom: 8px;
}
.ev__secheader {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  padding: 6px 0;
  user-select: none;
}
.ev-arrow {
  font-size: 10px;
  transition: transform 0.2s;
  display: inline-block;
}
.ev__arrowOpen {
  transform: rotate(0deg);
}
.ev__sectitle {
  font-size: 12px;
  font-weight: 600;
}
.ev__secmeta {
  font-size: 11px;
  margin-left: auto;
}
/* 次要按钮（全屏/日志工具）：透明底 + 淡蓝描边 + 蓝字，hover 实心蓝 */
.ev__fsbtn {
  border: 1px solid color-mix(in srgb, var(--a) 42%, transparent);
  background: transparent;
  color: var(--a);
  border-radius: 999px;
  padding: 3px 12px;
  font-size: 12px;
  cursor: pointer;
  transition:
    background 0.18s,
    color 0.18s,
    border-color 0.18s;
}
.ev__fsbtn:hover {
  background: var(--a);
  border-color: var(--a);
  color: var(--on-accent);
}
.ev__ctxgrid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--b);
}
.ev__h3 {
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 6px;
}
.ev__ctxlines {
  line-height: 1.6;
}
.ev__infolines {
  font-size: 11px;
  color: var(--t2);
  line-height: 1.8;
}
/* 门禁徽标：老页在信息区恒隐藏（模板 display:none 且无人打开），状态由右侧对话面板表达 */
.ev__infolines #convGate {
  display: none;
}
.ev__lbl {
  color: var(--t3);
}
.ev__link {
  color: var(--a);
  text-decoration: underline;
}
.ev__promptbody {
  max-height: 400px;
  overflow-y: auto;
  background: #000;
  color: var(--on-accent);
  border-radius: 8px;
  padding: 14px 18px 28px;
  font-size: 14px;
  font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', Consolas, Menlo, monospace;
  line-height: 1.75;
  letter-spacing: 0.2px;
  white-space: pre-wrap;
  word-break: break-all;
}
.ev__promptcontent {
  color: #e0e6f0;
}
.ev__proctitle {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 6px;
  flex-shrink: 0;
}
.ev__processing {
  flex: 1;
  overflow-y: auto;
  background: var(--bg);
  color: var(--t);
  border-radius: 8px;
  padding: 14px;
  font-size: 13px;
  line-height: 1.6;
}
.ev__outcome {
  padding: 10px;
  font-size: 11px;
  flex-shrink: 0;
}
.ev__outcometitle {
  font-size: 11px;
  font-weight: 600;
  margin-bottom: 4px;
  color: var(--a);
}
.ev__outcomeblock {
  margin-top: 2px;
}
.ev__kcards {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 4px;
}
.ev__flowrow {
  padding: 3px 0;
  border-bottom: 1px dashed var(--b);
}
/* 对话面板 */
.ev__convpanel {
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: 100%;
  overflow: hidden;
}
/* 对话全屏：CSS 类切换（DOM 不搬运），沿用事件页 v1 的 fixed 铺满语义 */
.ev__convpanel--fs {
  position: fixed;
  inset: 0;
  z-index: 1001;
  height: auto;
  border-radius: 0;
  border: none;
  padding: 18px;
  box-sizing: border-box;
  background: var(--bg, #11151c);
}
.ev__convhdr {
  flex-shrink: 0;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.ev__convtitle {
  font-size: 12px;
  font-weight: 600;
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}
.ev__convbtns {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-shrink: 0;
}
/* 删除/危险（清空对话）：透明底 + 红描边 + 红字，hover 实心红 */
.ev__clearbtn {
  border: 1px solid color-mix(in srgb, var(--r) 55%, transparent);
  background: transparent;
  color: var(--r);
  border-radius: 999px;
  padding: 3px 12px;
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
  transition:
    background 0.18s,
    color 0.18s,
    border-color 0.18s;
}
.ev__clearbtn:hover {
  background: var(--r);
  border-color: var(--r);
  color: var(--on-accent);
}
.ev__convmsgs {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 12px;
}
.ev__bubblerow {
  display: flex;
  justify-content: flex-start;
}
.ev__bubblerow--mine {
  justify-content: flex-end;
}
.ev__sysbubble {
  max-width: 92%;
  padding: 5px 10px;
  border-radius: 8px;
  font-size: 11px;
  color: var(--t2);
  background: var(--chip-bg);
  text-align: center;
  line-height: 1.4;
}
.ev__bubble {
  max-width: 85%;
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.5;
  background: var(--bg);
  color: var(--t);
  border: 1px solid var(--b);
}
.ev__bubble--mine {
  background: var(--a);
  color: var(--on-accent);
  border: none;
}
.ev__convhint {
  color: var(--t3);
  font-size: 11px;
}
.ev__convinput {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}
.ev__convinput textarea {
  flex: 1;
  padding: 6px 10px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 12px;
  background: var(--bg);
  resize: vertical;
  max-height: 120px;
  overflow-y: auto;
}
/* 任务日志模态 */
.ev__tl-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  z-index: 1001;
  display: flex;
  align-items: center;
  justify-content: center;
}
.ev__tl-box {
  width: min(960px, 94vw);
  height: min(720px, 90vh);
  background: var(--bg, #11151c);
  border: 1px solid var(--b, #2a3140);
  border-radius: 10px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.5);
}
.ev__tl-box:fullscreen {
  width: 100vw;
  height: 100vh;
  border-radius: 0;
}
/* 任务日志全屏（按钮切换，不依赖浏览器 Fullscreen API） */
.ev__tl-box--fs {
  width: 100vw;
  height: 100vh;
  max-width: none;
  max-height: none;
  border-radius: 0;
  border: none;
}
.ev__tl-hdr {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--b, #2a3140);
  flex-shrink: 0;
}
.ev__tl-title {
  font-size: 13px;
  font-weight: 600;
}
.ev__tl-sub {
  color: var(--t3);
  font-weight: 400;
}
.ev__tl-status {
  font-size: 11px;
  margin-left: auto;
}
/* 内联日志：老页在标题栏显示「N 条」（events.js openInlineLog），Vue 移植时漏掉 */
.ev__tl-count {
  font-size: 11px;
  margin-left: auto;
  color: var(--t3);
}
/* 日志弹窗工具按钮（Teleport 到 body，不进 #pageContent，所以样式只能写这里）*/
.ev__tl-btn {
  border: 1px solid color-mix(in srgb, var(--a) 42%, transparent);
  background: transparent;
  color: var(--a);
  border-radius: 999px;
  padding: 3px 12px;
  font-size: 12px;
  cursor: pointer;
  transition:
    background 0.18s,
    color 0.18s,
    border-color 0.18s;
}
.ev__tl-btn:hover {
  background: var(--a);
  border-color: var(--a);
  color: var(--on-accent);
}
.ev__tl-kill {
  border-color: color-mix(in srgb, var(--r) 55%, transparent);
  color: var(--r);
}
.ev__tl-kill:hover {
  background: var(--r);
  border-color: var(--r);
  color: var(--on-accent);
}
.ev__tl-info {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  padding: 7px 14px;
  border-bottom: 1px solid var(--b, #2a3140);
  flex-shrink: 0;
  font-size: 11px;
  color: var(--t2, #8a94a6);
}
.ev__tl-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  background: #000;
  color: var(--on-accent);
  padding: 14px 18px;
  font-size: 15px;
  font-family:
    'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Source Code Pro', 'SF Mono', Consolas,
    'Liberation Mono', Menlo, monospace;
  line-height: 1.75;
  letter-spacing: 0.2px;
}
/* 玻璃弹窗（重派发/A2A/本机重试） */
.ev__glass-mask {
  position: fixed;
  inset: 0;
  z-index: 1001;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.45);
  backdrop-filter: blur(6px);
}
.ev__glass {
  width: 340px;
  background: var(--glass-strong);
  backdrop-filter: blur(28px);
  border-radius: 18px;
  border: 1px solid var(--hairline);
  box-shadow:
    0 24px 64px rgba(0, 0, 0, 0.5),
    0 2px 8px rgba(0, 0, 0, 0.24);
  overflow: hidden;
  padding: 18px 20px 14px;
}
.ev__glass-ic {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 17px;
  flex-shrink: 0;
  margin-bottom: 10px;
}
.ev__glass-ic--orange {
  background: var(--y);
  box-shadow: 0 4px 12px color-mix(in srgb, var(--y) 30%, transparent);
}
.ev__glass-ic--blue {
  background: var(--a);
  box-shadow: 0 4px 12px color-mix(in srgb, var(--a) 30%, transparent);
}
.ev__glass-ic--green {
  background: var(--g-fill);
  box-shadow: 0 4px 12px color-mix(in srgb, var(--g) 30%, transparent);
}
.ev__glass-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--t, #1d1d1f);
  letter-spacing: -0.2px;
}
.ev__glass-sub {
  font-size: 11px;
  color: var(--t3, #86868b);
}
.ev__glass-desc {
  font-size: 12px;
  color: var(--t2, #6e6e73);
  line-height: 1.6;
  margin-bottom: 14px;
}
.ev__glass-btns {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
.ev__a2a-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 14px;
}
.ev__a2a-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 12px;
  cursor: pointer;
  border: 1px solid var(--b);
  background: color-mix(in srgb, var(--c) 60%, transparent);
  transition: all 0.15s;
}
.ev__a2a-item--sel {
  border-color: color-mix(in srgb, var(--a) 45%, transparent);
  background: color-mix(in srgb, var(--a) 8%, transparent);
}
.ev__a2a-item--off {
  opacity: 0.4;
  cursor: not-allowed;
  background: var(--chip-bg);
}
.ev__a2a-ic {
  font-size: 15px;
}
.ev__a2a-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--t, #1d1d1f);
}
.ev__a2a-hint {
  font-size: 10px;
  color: var(--t3, #86868b);
  margin-left: auto;
}
.ev__a2a-warn {
  font-size: 10px;
  color: var(--y);
  margin-left: auto;
}
.ev__a2a-cur {
  font-size: 11px;
  color: var(--a, #0071e3);
  margin-left: auto;
}
.ev__centerpop {
  left: 50% !important;
  top: 50% !important;
  transform: translate(-50%, -50%);
}
</style>
