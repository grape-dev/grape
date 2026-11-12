<template>
  <div class="tsk-page isk">
    <!-- ═══ 加载/错误态 ═══ -->
    <div v-if="loadError" class="error">{{ loadError }}</div>
    <div v-else-if="!loaded" class="loading">{{ t('loading') }}</div>

    <!-- ═══ 主视图（列表/看板） ═══ -->
    <template v-else-if="!detailView">
      <!-- ═══ 顶部：标题 + 部署URL + 视图Tab + 工具栏 ═══ -->
      <div class="tsk-top">
        <h1>{{ t('page_title') }}</h1>
        <div class="isk-deploy">
          <!-- 部署访问 URL：非编辑态显示小地球，编辑态显示输入框 -->
          <template v-if="deployEditing">
            <input
              v-model="deployDraft"
              class="isk-deploy-input"
              :placeholder="td('deploy_placeholder')"
              @keydown.enter.exact.prevent="saveDeployUrl"
            />
            <button class="tsk-btn tsk-btn-primary isk-deploy-btn" @click="saveDeployUrl">
              {{ tc('save') }}
            </button>
            <button class="tsk-btn tsk-btn-ghost isk-deploy-btn" @click="deployEditing = false">
              {{ tc('cancel') }}
            </button>
          </template>
          <a
            v-else-if="deployUrl"
            :href="deployUrl"
            target="_blank"
            rel="noopener"
            class="isk-deploy-link"
            :title="deployUrl"
            @contextmenu.prevent="startDeployEdit"
            @mousedown="deployLongPressStart"
            @mouseup="deployLongPressEnd"
            @mouseleave="deployLongPressEnd"
            @touchend="deployLongPressEnd"
            >🌐</a
          >
          <span
            v-else
            class="isk-deploy-empty"
            :title="td('deploy_hint')"
            @click="startDeployEdit"
            @contextmenu.prevent="startDeployEdit"
            >🌐</span
          >
        </div>
        <div class="isk-viewtabs">
          <button :class="{ on: viewMode === 'mine' }" @click="switchViewMode('mine')">
            {{ t('view_mine') }}
          </button>
          <button :class="{ on: viewMode === 'repo_all' }" @click="switchViewMode('repo_all')">
            {{ t('view_repo_all') }}
          </button>
        </div>
        <div class="tsk-controls">
          <button class="tsk-btn tsk-btn-primary" @click="openCreateModal">
            {{ t('create.btn_new') }}
          </button>
          <!-- 筛选 popover：状态多选 + 创建人多选 + 时间排序 -->
          <div class="isk-pop-wrap">
            <button class="tsk-tab isk-filter-btn" @click="filterPopover = !filterPopover">
              <span>⌥</span> {{ t('filter') }}
              <span v-if="filterActive" class="isk-dot"></span>
            </button>
            <div v-if="filterPopover" class="isk-popover">
              <div class="isk-pop-head">
                <span>{{ t('filter') }}</span>
                <button @click="filterPopover = false">✕</button>
              </div>
              <div class="isk-pop-sec" @click="statusExpanded = !statusExpanded">
                {{ t('filter_status') }}<span>{{ statusExpanded ? '▾' : '▸' }}</span>
              </div>
              <div v-if="statusExpanded" class="isk-pop-body">
                <label class="isk-chk" @click="clearStatusFilter">
                  <span class="isk-box"></span>
                  {{ t('tab.all') }} <span class="isk-cnt">{{ tasks.length }}</span>
                </label>
                <label
                  v-for="o in statusOpts"
                  :key="o.k"
                  class="isk-chk"
                  :class="{ on: filterStatuses.includes(o.k) }"
                  @click="toggleFilterStatus(o.k)"
                >
                  <span class="isk-box" :class="{ on: filterStatuses.includes(o.k) }">
                    <i v-if="filterStatuses.includes(o.k)">✓</i>
                  </span>
                  {{ o.lbl }} <span class="isk-cnt">{{ countStatus(o.k) }}</span>
                </label>
              </div>
              <div class="isk-pop-sec" @click="creatorExpanded = !creatorExpanded">
                {{ t('filter_creator') }}<span>{{ creatorExpanded ? '▾' : '▸' }}</span>
              </div>
              <div v-if="creatorExpanded" class="isk-pop-body">
                <label class="isk-chk" @click="clearAssigneeFilter">
                  <span class="isk-box"></span>
                  {{ t('tab.all') }} <span class="isk-cnt">{{ tasks.length }}</span>
                </label>
                <label
                  v-for="a in assignees"
                  :key="a"
                  class="isk-chk"
                  :class="{ on: filterAssignees.includes(a) }"
                  @click="toggleFilterAssignee(a)"
                >
                  <span class="isk-box" :class="{ on: filterAssignees.includes(a) }">
                    <i v-if="filterAssignees.includes(a)">✓</i>
                  </span>
                  {{ a }} <span class="isk-cnt">{{ countAssignee(a) }}</span>
                </label>
              </div>
              <button class="isk-sort-btn" @click="toggleSort">
                {{ t('sort.time') }} {{ sortOrder === 'desc' ? '↓' : '↑' }}
              </button>
            </div>
          </div>
          <div style="flex: 1"></div>
          <!-- 视图切换 popover：列表/看板 -->
          <div class="isk-pop-wrap">
            <button class="tsk-tab" @click="viewPopover = !viewPopover">
              <span>{{ taskView === 'board' ? '▦' : '☰' }}</span>
              {{ taskView === 'board' ? t('view_board') : t('view_list') }}
              <span class="isk-caret">▾</span>
            </button>
            <div v-if="viewPopover" class="isk-popover isk-pop-right">
              <label
                v-for="k in ['list', 'board']"
                :key="k"
                class="isk-vopt"
                :class="{ on: taskView === k }"
                @click="switchTaskView(k)"
              >
                <span>{{ k === 'list' ? '☰' : '▦' }}</span>
                {{ k === 'list' ? t('view_list') : t('view_board') }}
                <span v-if="taskView === k" class="isk-vcheck">✓</span>
              </label>
            </div>
          </div>
        </div>
      </div>

      <!-- ═══ 搜索框（列表/看板共用） ═══ -->
      <div class="isk-searchwrap">
        <div class="isk-searchbox">
          <span class="isk-search-ic">🔍</span>
          <input
            v-model="queryDraft"
            :placeholder="t('search_placeholder')"
            @keydown.enter.exact.prevent="applySearch"
            @change="applySearch"
          />
        </div>
      </div>

      <!-- ═══ 列表模式：左列表 + 右详情 ═══ -->
      <div v-if="taskView === 'list'" class="tsk-layout isk-layout">
        <div ref="listScrollEl" class="tsk-list isk-list">
          <template v-if="pageRows.length">
            <div
              v-for="tk in pageRows"
              :key="tk.id"
              :class="['tsk-row', sel && sel.id === tk.id ? 'tsk-row-sel' : '']"
              @click="sel = tasksById[tk.id] || tk"
              @dblclick="openTaskConversation(tk)"
            >
              <div :class="['tsk-r-ic', statusInfo(tk).rIc]">{{ statusInfo(tk).ic }}</div>
              <div class="tsk-r-main">
                <div class="tsk-r-title">
                  {{
                    (tk.is_pr_event ? 'PR #' : 'Issue #') +
                    tk.issue_number +
                    ' · ' +
                    (tk.title || '')
                  }}
                </div>
                <div class="tsk-r-meta">
                  ⏱ {{ fmtLocal(tk.created_at).substring(0, 16) }} · {{ bareRepo(tk.repo_key)
                  }}{{ cardOwner(tk) ? ' · 👤 ' + cardOwner(tk) : '' }}
                </div>
              </div>
              <span v-if="tk.is_pr_event" class="isk-pr-pill">PR</span>
              <span
                v-else
                :class="['tsk-tag', statusInfo(tk).cls]"
                v-html="statusInfo(tk).text"
              ></span>
            </div>
          </template>
          <div v-else class="tsk-empty">
            <div>🗒</div>
            <h3>{{ t('empty.no_tasks_title') }}</h3>
            <p>{{ t('empty.no_tasks_desc') }}</p>
          </div>
          <div style="flex: 1"></div>
          <!-- 分屏：尾部哨兵（IntersectionObserver 触底续拉；has_more=false 收尾） -->
          <div v-if="serverPagedView" ref="listSentinelEl" class="isk-sentinel">
            <span v-if="tasksAppendLoading || tasksLoading">{{ tc('loading') }}</span>
            <span v-else-if="tasksHasMore" @click="loadMoreList">↓ {{ t('load_more') }}</span>
            <span v-else class="isk-pager-sum"
              >{{ t('tab.all') }} {{ filtered.length }}
              <template v-if="tasksTotal !== null">/ {{ tasksTotal }}</template></span
            >
          </div>
          <!-- 分页（我的需求：全量拉取后本地切页） -->
          <div v-else-if="totalPages > 1" class="isk-pager">
            <button class="tsk-btn tsk-btn-ghost" :disabled="page <= 1" @click="prevPage">‹</button>
            <template v-for="pn in pageNums" :key="pn">
              <span v-if="pn === '…'" class="isk-ellipsis">…</span>
              <button
                v-else
                class="tsk-btn tsk-btn-ghost"
                :class="{ on: pn === page }"
                @click="page = pn"
              >
                {{ pn }}
              </button>
            </template>
            <button class="tsk-btn tsk-btn-ghost" :disabled="page >= totalPages" @click="nextPage">
              ›
            </button>
            <span class="isk-pager-sum">{{
              t('pager.summary', { total: filtered.length, page, pages: totalPages })
            }}</span>
          </div>
        </div>
        <div class="tsk-detail isk-detail">
          <template v-if="sel">
            <div v-html="detailCardHtml"></div>
            <div v-html="reviewBannerHtml"></div>
            <div v-html="agentStatusCardHtml"></div>
            <!-- 附件区 -->
            <div class="isk-attach-card">
              <div class="isk-card-tt">{{ td('attachments') }}</div>
              <div class="isk-attach-list">
                <div v-if="attachments.state === 'loading'" class="isk-muted">
                  {{ tc('loading') }}
                </div>
                <div v-else-if="attachments.state === 'error'" class="isk-muted">
                  {{ td('attach_load_failed') }}
                </div>
                <div v-else-if="!attachments.list.length" class="isk-muted">
                  {{ td('no_attachments') }}
                </div>
                <div v-for="a in attachments.list" :key="a.filename" class="isk-attach-item">
                  <img v-if="a.is_image" :src="a.url" class="isk-attach-thumb" />
                  <span v-else class="isk-attach-clip">📎</span>
                  <a :href="a.url" target="_blank" rel="noopener">{{ a.filename }}</a>
                  <span class="isk-muted">{{ formatSize(a.size) }}</span>
                  <button class="isk-attach-del" @click="deleteAttach(a.filename)">删除</button>
                </div>
              </div>
              <label v-if="sel.status === 'active'" class="isk-upload-label">
                ⬆ {{ td('upload_attach') }}
                <input type="file" style="display: none" @change="onAttachSelect" />
              </label>
            </div>
            <!-- 操作按钮行 -->
            <div class="isk-actions">
              <button class="tsk-btn tsk-btn-ghost" @click="openIssueLink(sel)">
                ↗ {{ td('view_issue') }}
              </button>
              <button class="tsk-btn tsk-btn-ghost" @click="openTaskConversation(sel)">
                {{ td('go_conversation') }}
              </button>
              <button class="tsk-btn tsk-btn-ghost" @click="openCopyModal(sel)">
                ⎘ {{ td('copy_task') }}
              </button>
              <template v-if="!sel.is_pr_event">
                <button
                  v-if="sel.status !== 'completed' && sel.status !== 'cancelled'"
                  class="tsk-btn tsk-btn-primary"
                  @click="confirmTaskDone(sel, $event)"
                >
                  ✓ {{ t('confirm_done') }}
                </button>
                <button
                  v-if="sel.status !== 'completed' && sel.status !== 'cancelled'"
                  :class="
                    isRepoAdmin ? 'tsk-btn tsk-btn-primary isk-admin-btn' : 'tsk-btn tsk-btn-ghost'
                  "
                  :disabled="!isRepoAdmin"
                  :title="isRepoAdmin ? '' : t('admin_only_hint')"
                  @click="adminConfirmTaskDone(sel, $event)"
                >
                  🛡 {{ t('admin_confirm_done') }}
                </button>
                <button
                  v-if="sel.status !== 'completed' && sel.status !== 'cancelled'"
                  class="tsk-btn tsk-btn-ghost isk-danger"
                  @click="cancelTask(sel, $event)"
                >
                  ✕ {{ t('cancel_task') }}
                </button>
                <button
                  v-if="sel.status === 'cancelled'"
                  class="tsk-btn tsk-btn-primary"
                  @click="resumeTask(sel)"
                >
                  ↻ {{ t('resume_task') }}
                </button>
              </template>
              <button
                class="tsk-btn tsk-btn-ghost isk-handoff"
                @click="openCrossPipelineModal(sel)"
              >
                ⇄ {{ t2('cross_pipeline.handoff_btn') }}
              </button>
            </div>
            <!-- 流水线区（异步加载） -->
            <div class="isk-pipeline-card">
              <div
                v-if="pipeline.loading"
                class="isk-muted"
                style="text-align: center; padding: 20px"
              >
                {{ t('loading') }}...
              </div>
              <template
                v-else-if="
                  pipeline.data &&
                  (pipeline.data.nodes || []).length + (pipeline.data.events || []).length > 0
                "
              >
                <div @click="onPipelineClick($event)" v-html="pipelineCardHtml"></div>
              </template>
            </div>
          </template>
          <div v-else class="tsk-empty" style="border: none">
            <div>🗒</div>
            <h3>{{ t('empty.select_task_title') }}</h3>
            <p>{{ t('empty.select_task_desc') }}</p>
          </div>
        </div>
      </div>

      <!-- ═══ 看板模式：六状态列 ═══ -->
      <div v-else class="isk-boardwrap">
        <div
          v-for="col in boardCols"
          :key="col.k"
          class="isk-board-col"
          :class="{ dim: col.k === 'cancelled' }"
        >
          <div class="isk-board-head">
            <span
              class="isk-board-dot"
              :style="{ background: col.color, boxShadow: '0 0 6px ' + col.color + '40' }"
            ></span>
            <span class="isk-board-name">{{ col.lbl }}</span>
            <span class="isk-board-cnt">{{ boardColCount(col.k) }}</span>
          </div>
          <div class="isk-board-body" @scroll.passive="onBoardScroll(col.k, $event)">
            <div v-if="!boardGrouped[col.k].length" class="isk-board-empty">
              {{ t('empty.no_tasks_desc') }}
            </div>
            <div
              v-for="tk in boardGrouped[col.k]"
              :key="tk.id"
              :class="[
                'isk-bcard',
                isRunning(tk) ? 'tsk-card-radar' : '',
                sel && sel.id === tk.id ? 'isk-bcard-sel' : '',
              ]"
              @click="openDetailFromBoard(tk)"
            >
              <div v-if="isRunning(tk)" class="tsk-radar-sweep" :style="radarBg(tk)"></div>
              <div class="isk-bcard-in">
                <div class="isk-bcard-top">
                  <span class="isk-muted">{{ tk.issue_number ? '#' + tk.issue_number : '' }}</span>
                  <span v-if="tk.is_pr_event" class="isk-pr-pill">PR</span>
                  <span v-else class="isk-stage" :style="stageStyle(tk)">{{ stageLabel(tk) }}</span>
                </div>
                <p class="isk-bcard-title">{{ tk.title || '' }}</p>
                <div class="isk-bcard-meta">
                  <span class="isk-ellipsis">{{
                    cardOwner(tk) ? '👤 ' + cardOwner(tk) : bareRepo(tk.repo_key)
                  }}</span>
                  <span>{{ (tk.created_at || '').substring(5, 10) }}</span>
                </div>
              </div>
            </div>
            <!-- 列底：分屏提示（仅「仓库全部」看板；列内触底加载该列下一页） -->
            <div v-if="boardPagedView" class="isk-board-foot">
              <span v-if="boardCursorOf(col.k).loading">{{ tc('loading') }}</span>
              <span v-else-if="boardCursorOf(col.k).hasMore">↓ {{ t('load_more') }}</span>
              <span v-else-if="boardGrouped[col.k].length"
                >— {{ t('tab.all') }} {{ boardGrouped[col.k].length }} —</span
              >
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- ═══ 全屏任务详情（看板/深链进入） ═══ -->
    <div v-if="detailView" class="isk-detailpage">
      <!-- PR 详情 -->
      <template v-if="detailView.is_pr_event">
        <div class="isk-dp-head">
          <button class="tsk-btn tsk-btn-ghost" @click="closeDetail">
            {{ t('back_to_board') }}
          </button>
          <h1 class="isk-dp-title">{{ detailView.title || '' }}</h1>
        </div>
        <div class="isk-dp-actions">
          <button class="tsk-btn tsk-btn-ghost" @click="openPrLink(detailView)">查看 PR ↗</button>
          <button class="tsk-btn tsk-btn-ghost" @click="G.app.nav('event', detailView.pr_seq)">
            事件详情
          </button>
          <button class="tsk-btn tsk-btn-primary" @click="openPrInterveneModal(detailView)">
            介入解决
          </button>
        </div>
        <div class="isk-card">
          <div class="isk-card-tt">PR 审查</div>
          <div class="isk-row">
            <span>状态</span><b :style="{ color: prStatus.color }">{{ prStatus.text }}</b>
          </div>
          <div class="isk-row">
            <span>PR</span
            ><b
              ><a :href="prLink" target="_blank" rel="noopener" class="isk-link"
                >#{{ detailView.issue_number }} ↗</a
              ></b
            >
          </div>
          <div class="isk-row">
            <span>仓库</span><b>{{ bareRepo(detailView.repo_key) }}</b>
          </div>
          <div class="isk-row">
            <span>创建人</span><b>{{ detailView.creator || '—' }}</b>
          </div>
          <div class="isk-row">
            <span>触发时间</span><b>{{ fmtLocal(detailView.created_at).substring(0, 16) }}</b>
          </div>
          <div class="isk-row isk-row-last">
            <span>事件数</span><b>{{ detailView.events_count || 0 }}</b>
          </div>
        </div>
        <div class="isk-card">
          <div class="isk-card-tt">QA 审查结果</div>
          <template v-if="!prQaHtml">{{ tc('loading') }}...</template>
          <template v-else><div v-html="prQaHtml"></div></template>
        </div>
      </template>
      <!-- 普通任务详情 -->
      <template v-else>
        <div class="isk-dp-head">
          <button class="tsk-btn tsk-btn-ghost" @click="backFromDetail">{{ backLabel }}</button>
          <h1 class="isk-dp-title">{{ detailView.title || '' }}</h1>
        </div>
        <div class="isk-dp-actions">
          <button class="tsk-btn tsk-btn-ghost" @click="openIssueLink(detailView)">
            ↗ {{ td('view_issue') }}
          </button>
          <button class="tsk-btn tsk-btn-ghost" @click="openTaskConversation(detailView)">
            {{ td('go_conversation') }}
          </button>
          <button class="tsk-btn tsk-btn-ghost" @click="openCopyModal(detailView)">
            ⎘ {{ td('copy_task') }}
          </button>
          <template v-if="detailView.status !== 'completed' && detailView.status !== 'cancelled'">
            <button class="tsk-btn tsk-btn-primary" @click="confirmTaskDone(detailView, $event)">
              ✓ {{ t('confirm_done') }}
            </button>
            <button
              :class="
                isRepoAdmin ? 'tsk-btn tsk-btn-primary isk-admin-btn' : 'tsk-btn tsk-btn-ghost'
              "
              :disabled="!isRepoAdmin"
              :title="isRepoAdmin ? '' : t('admin_only_hint')"
              @click="adminConfirmTaskDone(detailView, $event)"
            >
              🛡 {{ t('admin_confirm_done') }}
            </button>
            <button
              class="tsk-btn tsk-btn-ghost isk-danger"
              @click="cancelTask(detailView, $event)"
            >
              ✕ {{ t('cancel_task') }}
            </button>
          </template>
          <button
            v-if="detailView.status === 'cancelled'"
            class="tsk-btn tsk-btn-primary"
            @click="resumeTask(detailView)"
          >
            ↻ {{ t('resume_task') }}
          </button>
          <button
            class="tsk-btn tsk-btn-ghost isk-handoff"
            @click="openCrossPipelineModal(detailView)"
          >
            ⇄ {{ t2('cross_pipeline.handoff_btn') }}
          </button>
        </div>
        <div v-html="detailCardHtml"></div>
        <div v-html="reviewBannerHtml"></div>
        <div v-html="agentStatusCardHtml"></div>
        <div class="isk-attach-card">
          <div class="isk-card-tt">{{ td('attachments') }}</div>
          <div class="isk-attach-list">
            <div v-if="attachments.state === 'loading'" class="isk-muted">{{ tc('loading') }}</div>
            <div v-else-if="attachments.state === 'error'" class="isk-muted">
              {{ td('attach_load_failed') }}
            </div>
            <div v-else-if="!attachments.list.length" class="isk-muted">
              {{ td('no_attachments') }}
            </div>
            <div v-for="a in attachments.list" :key="a.filename" class="isk-attach-item">
              <img v-if="a.is_image" :src="a.url" class="isk-attach-thumb" />
              <span v-else class="isk-attach-clip">📎</span>
              <a :href="a.url" target="_blank" rel="noopener">{{ a.filename }}</a>
              <span class="isk-muted">{{ formatSize(a.size) }}</span>
              <button class="isk-attach-del" @click="deleteAttach(a.filename)">删除</button>
            </div>
          </div>
          <label v-if="detailView.status === 'active'" class="isk-upload-label">
            ⬆ {{ td('upload_attach') }}
            <input type="file" style="display: none" @change="onAttachSelect" />
          </label>
        </div>
        <div class="isk-pipeline-card">
          <div v-if="pipeline.loading" class="isk-muted" style="text-align: center; padding: 20px">
            {{ t('loading') }}...
          </div>
          <template
            v-else-if="
              pipeline.data &&
              (pipeline.data.nodes || []).length + (pipeline.data.events || []).length > 0
            "
          >
            <div @click="onPipelineClick($event)" v-html="pipelineCardHtml"></div>
          </template>
        </div>
      </template>
    </div>

    <!-- ═══ 通用弹窗：新建/复制/PR介入/跨流水线移交 ═══ -->
    <div v-if="modal.open" class="isk-modal-ov">
      <div class="isk-modal">
        <div class="isk-modal-head">
          <h3>{{ modalTitle() }}</h3>
          <button class="isk-modal-close" @click="modal.open = false">✕</button>
        </div>
        <div class="isk-modal-body">
          <div class="isk-modal-meta">
            {{ t('create.repo_label') }}<b>{{ defaultRepoName() || t('create.repo_empty') }}</b>
          </div>
          <div class="isk-modal-meta">
            {{ t('create.creator_label') }}<b>{{ currentUsername() || '—' }}</b>
          </div>
          <!-- 跨流水线移交 -->
          <template v-if="modal.mode === 'cross'">
            <div class="isk-modal-meta">
              {{ td('cross_pipeline.issue_label') }}:
              <b
                >#{{ modal.task ? modal.task.issue_number : '' }}
                {{ modal.task ? modal.task.title : '' }}</b
              >
            </div>
            <div class="isk-fld">
              <label>
                {{ td('cross_pipeline.target_scene') }}
                <select v-model="modal.sceneId" @change="modal.crossHint = !!modal.sceneId">
                  <option value="">
                    {{
                      modal.submitting
                        ? td('cross_pipeline.loading_scenes')
                        : td('cross_pipeline.scene_placeholder')
                    }}
                  </option>
                  <option v-for="s in modal.crossTargets" :key="s.id" :value="s.id">
                    {{ s.display_name || s.name || s.id }}
                  </option>
                </select>
              </label>
            </div>
            <div v-if="modal.crossHint" class="isk-cross-hint">
              {{ td('cross_pipeline.assignee_hint') }}
            </div>
          </template>
          <!-- 创建/复制/PR介入 -->
          <template v-else>
            <div class="isk-fld">
              <label>
                {{ t('create.title_label') }} <span class="isk-required">*</span>
                <span class="isk-fld-hint">{{ t('create.title_hint') }}</span>
                <input v-model="modal.title" :placeholder="t('create.title_placeholder')" />
              </label>
            </div>
            <div class="isk-fld">
              <label>
                {{ t('create.desc_label') }} <span class="isk-required">*</span>
                <!-- Issue #460：标题一键同步进描述（幂等去重，空行分隔） -->
                <span
                  class="isk-sync-title"
                  role="button"
                  :title="t('create.sync_title_to_desc')"
                  :aria-label="t('create.sync_title_to_desc')"
                  @click.prevent="syncTitleToDesc"
                  >↧</span
                >
                <span class="isk-fld-hint">{{ t('create.desc_hint') }}</span>
                <textarea
                  v-model="modal.desc"
                  rows="4"
                  :placeholder="t('create.desc_placeholder')"
                ></textarea>
              </label>
            </div>
            <div class="isk-fld">
              <label>
                {{ t('create.scene_label') }}
                <span class="isk-fld-hint">{{ t('create.scene_hint') }}</span>
                <select v-model="modal.sceneId" @change="updateSceneWarn">
                  <option value="">{{ t('create.scene_vod_auto') }}</option>
                  <option v-for="s in modal.scenes" :key="s.id" :value="s.id">{{ s.label }}</option>
                </select>
              </label>
              <div v-if="modal.sceneWarn" class="isk-scene-warn">
                ⚠️ {{ modal.sceneWarn.reason }}
                <a @click="goSceneRebind">去换绑 →</a>
              </div>
            </div>
            <div class="isk-fld-hint">{{ t('create.assignee_hint') }}</div>
            <!-- 附件（创建/PR介入） -->
            <div v-if="modal.mode !== 'copy'" class="isk-fld">
              <span style="font-size: 12px"
                >附件 <span class="isk-fld-hint">（可选，创建 Issue 时同步附加）</span></span
              >
              <div class="isk-attach-row">
                <div v-for="(a, i) in createAttachments" :key="i" class="isk-attach-chip">
                  <span>📎 {{ a.filename }}</span>
                  <span v-if="!a.loading" class="isk-fld-hint">{{ formatSize(a.size) }}</span>
                  <span v-else class="isk-fld-hint">…</span>
                  <button @click="removeModalAttach(i)">✕</button>
                </div>
              </div>
              <label class="isk-upload-label">
                ⬆ 添加附件
                <input type="file" style="display: none" @change="onModalAttachSelect" />
              </label>
            </div>
          </template>
          <div v-if="modal.error" class="isk-modal-err">{{ modal.error }}</div>
        </div>
        <div class="isk-modal-foot">
          <button class="tsk-btn tsk-btn-ghost" @click="modal.open = false">
            {{ t('create.btn_cancel') }}
          </button>
          <button class="tsk-btn tsk-btn-primary" :disabled="modal.submitting" @click="submitModal">
            {{ submitBtnLabel() }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'

const G = globalThis

// Issue #1740：「我的需求 / 仓库全部」视图模式持久化（刷新后保持上次选择）
const VIEW_MODE_KEY = 'tasks_view_mode'
function loadViewMode() {
  try {
    const saved = localStorage.getItem(VIEW_MODE_KEY)
    if (saved === 'mine' || saved === 'repo_all') return saved
  } catch (e) {
    /* localStorage 不可用 → 回退默认 */
  }
  return 'mine'
}
function saveViewMode(mode) {
  try {
    if (mode === 'mine' || mode === 'repo_all') localStorage.setItem(VIEW_MODE_KEY, mode)
  } catch (e) {
    /* ignore */
  }
}

const loaded = ref(false)
const props = defineProps({ initialParam: { type: [String, Number], default: null } })
const tasks = ref([]) // Issue #926：已加载卡片缓存（分屏增量追加，不是全量）
const sel = ref(null) // 列表模式选中
const detailView = ref(null) // 全屏详情（看板/深链）
const query = ref('')
const queryDraft = ref('')
const page = ref(1) // 客户端分页页码（仅「我的需求」视图，全量拉取后本地切页）
const taskView = ref('board') // 'board' | 'list'（老代码默认 board）
const viewMode = ref(loadViewMode()) // 'mine' | 'repo_all'
const sortOrder = ref('desc')
const filterStatuses = ref([])
const filterAssignees = ref([])
const filterPopover = ref(false)
const viewPopover = ref(false)
const statusExpanded = ref(false)
const creatorExpanded = ref(false)
const loadError = ref('')
const attachments = reactive({ state: 'idle', list: [] })
const pipeline = reactive({ loading: false, data: null })
const deployUrl = ref('')
const deployEditing = ref(false)
const deployDraft = ref('')
let deployLongPressTimer = null
let pollTimer = null
const PER_PAGE = 15 // 「我的需求」客户端分页（全量拉取后本地切页）
const SERVER_PAGE = 30 // Issue #926：服务端分屏每屏条数（滚动追加）
// Issue #926：服务端分页状态（仅「仓库全部」视图生效；我的需求条数有限，保持全量拉取）
const tasksHasMore = ref(false) // 服务端 has_more，false 时停止滚动追加
const tasksTotal = ref(null) // 服务端 total；null = 未给精确值（q 模式省 COUNT）
const tasksLoading = ref(false) // 分屏请求在途（列表尾 sentinel 展示「加载中」）
const tasksAppendLoading = ref(false) // 滚动追加在途（防并发重复拉同屏）
let taskReqSeq = 0 // Issue #2984d027a：请求序号——重置类请求让旧在途响应失效
let taskServerPage = 1 // 下一次要拉的服务端页码（成功后才推进）
const listScrollEl = ref(null) // 列表滚动容器（IntersectionObserver 的 root）
const listSentinelEl = ref(null) // 列表尾部哨兵
let listObserver = null
// 深链/场景跳转：从 window 实时读（onMounted 已在 loadTasks 前设置，dashboard.js 场景跳转先设再 nav）

// ═══════════ I18N ═══════════
const TASK_DEFAULTS = {
  page_title: '需求中心',
  view_mine: '我的需求',
  view_repo_all: '仓库全部',
  view_list: '列表',
  view_board: '看板',
  filter: '筛选',
  filter_status: '状态',
  filter_creator: '创建人',
  'sort.time': '时间排序',
  loading: '加载中...',
  back_to_board: '返回看板',
  confirm_done: '确认完成',
  admin_confirm_done: '管理员确认完成',
  admin_only_hint: '仅仓库管理员可用',
  cancel_task: '取消任务',
  resume_task: '恢复任务',
  'create.btn_new': '新建需求',
  'empty.no_tasks_title': '暂无需求',
  'empty.no_tasks_desc': '无需求',
  'empty.select_task_title': '选择一个需求',
  'empty.select_task_desc': '从左侧列表选择需求查看详情',
  'pager.summary': '共 {total} 条 · 第 {page}/{pages} 页',
  'tab.all': '全部',
  load_more: '滚动加载更多',
  load_failed: '加载失败：{msg}',
  'status.tag_pending': '待处理',
  'status.tag_processing': '处理中',
  'status.tag_awaiting_input': '待澄清',
  'status.tag_pending_review': '待审核',
  'status.tag_completed': '已完成',
  'status.tag_cancelled': '已取消',
  'status.detail_completed': '已完成',
  'status.detail_processing': '处理中',
  'status.detail_pending': '待处理',
  'status.reason_issue_closed': '（Issue 已关闭）',
  'status.reason_reassigned': '（已转派）',
  'status.reason_bot_takeover': '（引擎接管）',
  'agent_status.title': 'Agent 节点状态',
  'agent_status.pending': '待执行',
  'agent_status.running': '执行中',
  'agent_status.completed': '已完成',
  'agent_status.failed': '失败',
  'agent_status.cancelled': '已取消',
  pipeline_scene: '流水线',
  event_history: '事件历史',
  scene_default_badge: '默认',
  multi_task_badge: '多任务',
  single_task_badge: '单任务',
  parent_task: '父任务',
  task_mode: '任务模式',
  deeplink_not_found: '未找到需求 #{n}，已返回列表',
  processing: '处理中...',
  confirm: '确认',
  cancel: '取消',
  task_done_toast: '任务已确认完成',
  task_cancelled_toast: '任务已取消',
  task_resumed_toast: '任务已恢复',
  operation_failed: '操作失败：{msg}',
}
const ISSUE_DEFAULTS = {
  detail_title: '需求详情',
  'detail.status': '状态',
  'detail.issue_id': 'ID',
  'detail.assignee': '创建人',
  'review_status.pending_review_hint': '需求待人工确认：阶段执行完成 ≠ 需求/生产发布完成',
  'review_status.sre_pending': 'SRE 验收任务在途，尚无结论',
  'review_status.sre_unavailable': '暂无 SRE 验收结论（无关联任务或结果不可读）',
  'review_status.sre_task_ref': '验收任务',
  'review_status.sre_result_title': 'SRE 验收结论',
  'detail.repo': '仓库',
  'detail.created_at': '创建时间',
  'detail.completed_at': '完成时间',
  'detail.view_issue': '查看 Issue ↗',
  'detail.go_conversation': '进入对话',
  'detail.copy_task': '复制需求',
  'cross_pipeline.handoff_btn': '移交到其他流水线',
}
function t(key, params) {
  const full = 'tasks.' + key
  const v = G.I18N.t(full, params)
  return v && v !== full ? v : TASK_DEFAULTS[key] || key
}
function td(key, params) {
  const full = 'issues.' + key
  const v = G.I18N.t(full, params)
  return v && v !== full ? v : ISSUE_DEFAULTS[key] || key
}
function t2(key, params) {
  return td(key, params)
}
function tc(key) {
  const map = {
    save: '保存',
    cancel: '取消',
    loading: '加载中...',
    confirm: '确认',
  }
  return map[key] || key
}

// ═══════════ 工具函数 ═══════════
const API = () => G.API
const app = () => G.app
function fmtLocal(s) {
  return G.fmtLocalTime ? G.fmtLocalTime(s) : String(s || '')
}
function vcsUrl(repoKey, path) {
  return G._vcsUrl ? G._vcsUrl(repoKey, path) : ''
}
function bareRepo(rk) {
  return String(rk || '').replace(/^(github|gitcode):/, '')
}
// Issue #805：卡片上展示的「人」——需求卡是创建人（creator），PR 伪卡是触发人（assignee）。
// 回退链：creator → assignee，兼容旧库里 created_by 为空的行。
function cardOwner(tk) {
  if (!tk) return ''
  if (tk.is_pr_event) return tk.assignee || tk.creator || ''
  return tk.creator || tk.assignee || ''
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}
function currentRepo() {
  return app().currentRepo ? app().currentRepo() : null
}
// 事件时间友好格式（与老 _fmtEventTime 等价）
function fmtEventTime(utcStr) {
  if (!utcStr) return ''
  const local = fmtLocal(utcStr)
  if (!local) return utcStr
  const d = new Date(local.replace(' ', 'T'))
  if (isNaN(d.getTime())) return local
  let diff = Date.now() - d.getTime()
  if (diff < 0) diff = 0
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return mins + '分钟前'
  const hours = Math.floor(mins / 60)
  if (hours < 24) return hours + '小时前'
  const pad = (n) => String(n).padStart(2, '0')
  return (
    pad(d.getMonth() + 1) +
    '-' +
    pad(d.getDate()) +
    ' ' +
    pad(d.getHours()) +
    ':' +
    pad(d.getMinutes())
  )
}
function formatSize(bytes) {
  if (bytes < 1024) return bytes + 'B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + 'KB'
  return (bytes / 1024 / 1024).toFixed(1) + 'MB'
}

// ═══════════ 排序偏好（localStorage per-repo，Issue #42） ═══════════
function sortPrefKey() {
  const repo = currentRepo()
  return 'task_sort_' + (repo ? repo.fullName : 'all')
}
function loadSortPref() {
  try {
    const saved = JSON.parse(G.localStorage.getItem(sortPrefKey()) || '{}')
    if (saved.order === 'asc' || saved.order === 'desc') sortOrder.value = saved.order
  } catch (e) {
    /* ignore */
  }
}
function saveSortPref() {
  try {
    G.localStorage.setItem(sortPrefKey(), JSON.stringify({ order: sortOrder.value, mode: 'time' }))
  } catch (e) {
    /* ignore */
  }
}
function toggleSort() {
  sortOrder.value = sortOrder.value === 'desc' ? 'asc' : 'desc'
  saveSortPref()
  page.value = 1
  loadTasks()
}

// ═══════════ 状态映射（与老 _mapStatusL/_mapStatus 等价） ═══════════
function mapStatusL(tk) {
  if (tk.status === 'cancelled') return 'cancelled'
  if (tk.status === 'completed' || tk.stage === 'done') return 'done'
  if (tk.status === 'pending_review' || tk.stage === 'pending_review') return 'pending_review'
  if (tk.status === 'awaiting_input') return 'awaiting_input'
  if (['vod', 'issue', 'coding', 'qa', 'sre'].includes(tk.stage)) return 'in_progress'
  if (tk.status === 'processing') return 'in_progress'
  return 'pending'
}
function stripTag(v) {
  return String(v || '').replace(/<[^>]*>/g, '')
}
const STATUS_LABELS = computed(() => ({
  pending: stripTag(t('status.tag_pending')) || '待处理',
  in_progress: stripTag(t('status.tag_processing')) || '处理中',
  awaiting_input: t('status.tag_awaiting_input') || '待澄清',
  pending_review: t('status.tag_pending_review') || '待审核',
  done: stripTag(t('status.tag_completed')) || '已完成',
  cancelled: t('status.tag_cancelled') || '已取消',
}))
const STATUS_COLORS = {
  pending: 'var(--t3)',
  in_progress: '#3b82f6',
  awaiting_input: '#f59e0b',
  pending_review: '#10b981',
  done: 'var(--g)',
  cancelled: 'var(--t3)',
}
const STAGE_LABELS = computed(() => ({
  vod: 'VOD',
  issue: 'ISSUE',
  coding: 'Coding',
  qa: 'QA',
  sre: 'SRE',
  pending_review: t('status.tag_pending_review') || '待审核',
  done: t('status.tag_completed') || '已完成',
  '': '—',
}))
const STAGE_COLORS = {
  vod: '#ec4899',
  issue: '#ef4444',
  coding: '#3b82f6',
  qa: '#a855f7',
  sre: '#f59e0b',
  pending_review: '#10b981',
  done: 'var(--g)',
  '': 'var(--t3)',
}
function statusInfo(tk) {
  const ms = mapStatusL(tk)
  const m = {
    pending: {
      ic: '#',
      cls: 'tsk-tag-p',
      rIc: 'tsk-ic-p',
      text: t('status.tag_pending') || '待处理',
    },
    in_progress: {
      ic: '⏳',
      cls: 'tsk-tag-pr',
      rIc: 'tsk-ic-pr',
      text: t('status.tag_processing') || '处理中',
    },
    awaiting_input: {
      ic: '?',
      cls: 'tsk-tag-pr',
      rIc: 'tsk-ic-pr',
      text: t('status.tag_awaiting_input') || '待澄清',
    },
    pending_review: {
      ic: '◉',
      cls: 'tsk-tag-pr',
      rIc: 'tsk-ic-pr',
      text: t('status.tag_pending_review') || '待审核',
    },
    done: {
      ic: '✓',
      cls: 'tsk-tag-d',
      rIc: 'tsk-ic-d',
      text: t('status.tag_completed') || '已完成',
    },
    cancelled: {
      ic: '✕',
      cls: 'tsk-tag-d',
      rIc: 'tsk-ic-d',
      text: t('status.tag_cancelled') || '已取消',
    },
  }
  return m[ms] || m.pending
}

// ═══════════ 过滤/分页 ═══════════
const tasksById = computed(() => {
  const m = {}
  tasks.value.forEach((tk) => {
    m[tk.id] = tk
  })
  return m
})
// 同步到 window（与老代码 window._tasksById 契约一致，跨模块引用）
watch(
  tasksById,
  function (m) {
    G.window._tasksById = m
  },
  { immediate: true }
)
const filtered = computed(() => {
  let list = tasks.value
  if (filterStatuses.value.length > 0) {
    list = list.filter((tk) => filterStatuses.value.includes(mapStatusL(tk)))
  }
  if (filterAssignees.value.length > 0) {
    // Issue #805：值域改为创建人（cardOwner），与后端 creators= 过滤同口径
    list = list.filter((tk) => filterAssignees.value.includes(cardOwner(tk)))
  }
  if (query.value) {
    const q = query.value.toLowerCase().trim()
    if (q) {
      list = list.filter((tk) => {
        const haystack = [
          String(tk.id || ''),
          tk.title || '',
          tk.task_description || '',
          String(tk.issue_number || ''),
          tk.repo_key || '',
          tk.assignee || '',
          tk.creator || '',
          tk.scene_name || '',
          tk.status || '',
          tk.repo_display || '',
        ]
          .join(' ')
          .toLowerCase()
        return haystack.includes(q)
      })
    }
  }
  return list
})
// Issue #926：分页模式判定
// - 列表分屏：「仓库全部」+ 列表视图（服务端 per_page 滚动追加）
// - 看板按列分屏：「仓库全部」+ 看板视图（每列一条游标，各拉各的）
// 「我的需求」条数有限，保持全量拉取 + 客户端分页（与老口径一致）
function repoOf() {
  return currentRepo()
}
const serverPagedView = computed(
  () => viewMode.value === 'repo_all' && taskView.value === 'list' && !!repoOf()
)
const boardPagedView = computed(
  () => viewMode.value === 'repo_all' && taskView.value === 'board' && !!repoOf()
)
const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / PER_PAGE)))
function clampPage() {
  if (page.value > totalPages.value) page.value = 1
  if (page.value < 1) page.value = 1
}
const pageRows = computed(() => {
  if (serverPagedView.value) return filtered.value // 服务端已按屏加载，本地不再切页
  const s = (page.value - 1) * PER_PAGE
  return filtered.value.slice(s, s + PER_PAGE)
})
// 分页页码（与老代码逻辑一致：最多7个 + 首尾 + 省略号）
const pageNums = computed(() => {
  const tp = totalPages.value
  if (tp <= 1) return []
  let s = Math.max(1, page.value - 3)
  let e = Math.min(tp, s + 6)
  if (e - s < 6) s = Math.max(1, e - 6)
  const out = []
  if (s > 1) {
    out.push(1)
    if (s > 2) out.push('…')
  }
  for (let i = s; i <= e; i++) out.push(i)
  if (e < tp) {
    if (e < tp - 1) out.push('…')
    out.push(tp)
  }
  return out
})
const statusOpts = computed(() => [
  { k: 'pending', lbl: STATUS_LABELS.value.pending },
  { k: 'in_progress', lbl: STATUS_LABELS.value.in_progress },
  { k: 'pending_review', lbl: STATUS_LABELS.value.pending_review },
  { k: 'done', lbl: STATUS_LABELS.value.done },
  { k: 'cancelled', lbl: STATUS_LABELS.value.cancelled },
])
const assignees = computed(() =>
  [...new Set(tasks.value.map((tk) => cardOwner(tk)).filter(Boolean))].sort()
)
const filterActive = computed(
  () => filterStatuses.value.length > 0 || filterAssignees.value.length > 0
)
function countStatus(k) {
  return tasks.value.filter((tk) => mapStatusL(tk) === k).length
}
function countAssignee(a) {
  return tasks.value.filter((tk) => cardOwner(tk) === a).length
}
function toggleFilterStatus(k) {
  const i = filterStatuses.value.indexOf(k)
  if (i >= 0) filterStatuses.value.splice(i, 1)
  else filterStatuses.value.push(k)
  page.value = 1
  // Issue #926：状态筛选映射 statuses= 走服务端过滤 → 重置回第 1 屏重拉
  if (repoOf() && viewMode.value === 'repo_all') resetAndLoad()
}
function toggleFilterAssignee(a) {
  const i = filterAssignees.value.indexOf(a)
  if (i >= 0) filterAssignees.value.splice(i, 1)
  else filterAssignees.value.push(a)
  page.value = 1
  loadTasks() // 创建人过滤走后端参数
}
function clearAssigneeFilter() {
  filterAssignees.value = []
  page.value = 1
  loadTasks()
}
function clearStatusFilter() {
  filterStatuses.value = []
  page.value = 1
  if (repoOf() && viewMode.value === 'repo_all') resetAndLoad()
}
function switchTaskView(k) {
  taskView.value = k
  viewPopover.value = false
  page.value = 1
  // Issue #1077-F：板↔列表切换 → 分页链路必须从头拉（列表走全局分屏，看板走列游标）
  if (repoOf() && viewMode.value === 'repo_all') resetAndLoad()
}
function prevPage() {
  page.value--
  clampPage()
}
function nextPage() {
  page.value++
  clampPage()
}
function applySearch() {
  query.value = queryDraft.value
  page.value = 1
  // Issue #926：分屏列表带 q= 走服务端搜索
  if (serverPagedView.value) resetAndLoad()
}
function switchViewMode(mode) {
  if (viewMode.value === mode) return
  viewMode.value = mode
  saveViewMode(mode) // Issue #1740：记住上次视图模式
  sel.value = null
  page.value = 1
  filterStatuses.value = []
  filterAssignees.value = []
  loadTasks()
}

// ═══════════ 看板 ═══════════
const boardCols = computed(() => [
  { k: 'pending', lbl: STATUS_LABELS.value.pending, color: STATUS_COLORS.pending },
  { k: 'in_progress', lbl: STATUS_LABELS.value.in_progress, color: STATUS_COLORS.in_progress },
  {
    k: 'awaiting_input',
    lbl: STATUS_LABELS.value.awaiting_input,
    color: STATUS_COLORS.awaiting_input,
  },
  {
    k: 'pending_review',
    lbl: STATUS_LABELS.value.pending_review,
    color: STATUS_COLORS.pending_review,
  },
  { k: 'done', lbl: STATUS_LABELS.value.done, color: STATUS_COLORS.done },
  { k: 'cancelled', lbl: STATUS_LABELS.value.cancelled, color: STATUS_COLORS.cancelled },
])
const boardGrouped = computed(() => {
  const grouped = {}
  boardCols.value.forEach((c) => {
    grouped[c.k] = []
  })
  filtered.value.forEach((tk) => {
    grouped[mapStatusL(tk)].push(tk)
  })
  return grouped
})

// ═══════════ Issue #1077-F：看板按列独立分页 ═══════════
// 每列一条游标，首屏 30 条；列内触底只拉该列下一页（不靠整页哨兵）。
// 约束：服务端只能按 issue.status 过滤，而看板列是 status+stage 的派生映射，
// 所以「待处理」「进行中」共享同一游标（statuses=active,processing），
// 行最终落哪一列仍由 mapStatusL 决定（同一行不会重复渲染）。
const BOARD_CURSOR_OF_COL = {
  pending: 'active_flow',
  in_progress: 'active_flow',
  awaiting_input: 'awaiting_input',
  pending_review: 'pending_review',
  done: 'done',
  cancelled: 'cancelled',
}
const BOARD_CURSOR_STATUSES = {
  active_flow: 'active,processing',
  awaiting_input: 'awaiting_input',
  pending_review: 'pending_review',
  done: 'completed',
  cancelled: 'cancelled',
}
const colCursor = reactive({}) // cursor -> {page, hasMore, loading, total, seq}
function resetBoardCursors() {
  Object.keys(BOARD_CURSOR_STATUSES).forEach((k) => {
    colCursor[k] = { page: 1, hasMore: true, loading: false, total: null, seq: 0 }
  })
  tasks.value = []
  sel.value = null
  tasksHasMore.value = false
  tasksTotal.value = null
  taskServerPage = 1
  G.window._tasksById = {}
}
resetBoardCursors() // 先建空游标：看板分屏关闭时模板也能安全读取
function boardCursorOf(colKey) {
  return colCursor[BOARD_CURSOR_OF_COL[colKey]] || { page: 1, hasMore: false, loading: false }
}
function mergeBoardItems(items) {
  const byId = {}
  tasks.value.forEach((t) => {
    byId[t.id] = t
  })
  items.forEach((t) => {
    if (byId[t.id]) Object.assign(byId[t.id], t)
    else tasks.value.push(t)
  })
  if (sel.value) {
    const upd = tasks.value.find((t) => t.id === sel.value.id)
    sel.value = upd || null
  }
  if (detailView.value) {
    const upd = tasks.value.find((t) => t.id === detailView.value.id)
    if (upd) detailView.value = upd
  }
}
// 取某游标的一页（merge=true 只刷新第 1 屏，用于轮询对账）
function boardFetch(key, opts) {
  const repo = repoOf()
  const cur = colCursor[key]
  if (!repo || !cur) return Promise.resolve()
  const merge = !!(opts && opts.merge)
  if (cur.loading) return Promise.resolve()
  if (!merge && !cur.hasMore) return Promise.resolve()
  cur.loading = true
  const seq = ++cur.seq
  const page = merge ? 1 : cur.page
  let url =
    '/issues/' +
    repo.owner +
    '/' +
    repo.name +
    '?sort_order=' +
    sortOrder.value +
    '&mode=time&per_page=' +
    SERVER_PAGE +
    '&page=' +
    page +
    '&statuses=' +
    encodeURIComponent(BOARD_CURSOR_STATUSES[key])
  if (query.value) url += '&q=' + encodeURIComponent(query.value)
  if (filterAssignees.value.length)
    url += '&creators=' + encodeURIComponent(filterAssignees.value.join(','))
  return API()
    .get(url)
    .then(function (d) {
      if (seq !== cur.seq) return // 已被本列更新的请求取代
      cur.loading = false
      const items = d.issues || d.tasks || []
      if (!merge) cur.page = page + 1
      cur.hasMore = !!d.has_more
      cur.total = d.total === null || d.total === undefined ? null : d.total
      mergeBoardItems(items)
      loaded.value = true
      loadError.value = ''
    })
    .catch(function (e) {
      if (seq !== cur.seq) return
      cur.loading = false
      if (e && e.name === 'AbortError' && e.silent) return
      if (G.window._currentView !== 'tasks') return
      loadError.value = t('load_failed', { msg: e.message })
      loaded.value = true
    })
}
function loadBoardAll(opts) {
  return Promise.all(Object.keys(BOARD_CURSOR_STATUSES).map((k) => boardFetch(k, opts)))
}
// 列内滚到接近底部 → 只加载该列所属游标的下一页
function onBoardScroll(colKey, ev) {
  if (!boardPagedView.value) return
  const el = ev && ev.target
  if (!el) return
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 48) {
    boardFetch(BOARD_CURSOR_OF_COL[colKey])
  }
}
// 列头计数：单列游标给「已加载/服务端总数」；共享游标（待处理+进行中）只给已加载数，
// 因为一份 total 覆盖两列，直接展示会误导。
function boardColCount(colKey) {
  const loadedN = boardGrouped.value[colKey] ? boardGrouped.value[colKey].length : 0
  if (!boardPagedView.value) return String(loadedN)
  const key = BOARD_CURSOR_OF_COL[colKey]
  const cur = colCursor[key]
  const shared = boardCols.value.filter((c) => BOARD_CURSOR_OF_COL[c.k] === key).length > 1
  if (shared || !cur || cur.total === null || cur.total === undefined) return String(loadedN)
  return loadedN + '/' + cur.total
}

function isRunning(tk) {
  return (
    mapStatusL(tk) === 'in_progress' &&
    tk.is_running &&
    ['vod', 'issue', 'coding', 'qa', 'sre'].includes(tk.stage || '')
  )
}
function stageLabel(tk) {
  const st = tk.stage || ''
  return STAGE_LABELS.value[st] || st || '—'
}
function stageColor(tk) {
  return STAGE_COLORS[tk.stage || ''] || 'var(--t3)'
}
function stageStyle(tk) {
  const c = stageColor(tk)
  return { color: c, background: 'color-mix(in srgb, ' + c + ' 9%, transparent)' }
}
function radarBg(tk) {
  const c = stageColor(tk)
  return {
    background: 'conic-gradient(from 0deg, transparent 0deg, ' + c + '14 40deg, transparent 90deg)',
  }
}

// ═══════════ 详情卡 HTML（等价老 _detailHtml，computed 替代手工 DOM patch） ═══════════
const detailTarget = computed(
  () => detailView.value || (sel.value && !sel.value.is_pr_event ? sel.value : null)
)
const detailCardHtml = computed(() => {
  const tk = detailTarget.value
  if (!tk || tk.is_pr_event) return ''
  const isDone = tk.status === 'completed'
  const isProc = tk.status === 'processing'
  const isCancelled = tk.status === 'cancelled'
  const reasonText =
    tk.completed_reason === 'issue_closed'
      ? t('status.reason_issue_closed')
      : tk.completed_reason === 'reassigned'
        ? t('status.reason_reassigned')
        : tk.completed_reason === 'bot_takeover'
          ? t('status.reason_bot_takeover')
          : tk.completed_reason === 'user_cancelled'
            ? t('cancel_task') || '已取消'
            : tk.completed_reason || ''
  const tagText = isCancelled
    ? t('status.tag_cancelled') || '已取消'
    : isDone
      ? t('status.detail_completed') + reasonText
      : isProc
        ? t('status.detail_processing')
        : t('status.detail_pending')
  const sceneName = tk.scene_id ? tk.scene_name || tk.scene_id : '—'
  const sceneBadge =
    tk.scene_source === 'vod'
      ? ''
      : tk.scene_id && tk.scene_source === 'repo_default'
        ? ' <span style="font-size:10px;color:var(--t3)">' +
          esc(t('scene_default_badge')) +
          '</span>'
        : ''
  const subtaskBadge = tk.is_subtask_mode
    ? '<span class="tg tg-q" style="font-size:10px">' + esc(t('multi_task_badge')) + '</span>'
    : '<span class="ts t3" style="font-size:10px">' + esc(t('single_task_badge')) + '</span>'
  const parentIssue = tk.parent_issue_number
    ? ' · ' + t('parent_task') + ' #' + tk.parent_issue_number
    : ''
  const issueLink = vcsUrl(tk.repo_key, (tk.is_pr ? '/pull/' : '/issues/') + tk.issue_number)
  const statusColor = isDone
    ? 'var(--g)'
    : isCancelled
      ? 'var(--t3)'
      : isProc
        ? '#3b82f6'
        : 'var(--t3)'
  return `
    <div class="isk-card isk-card-rel">
      <div class="isk-card-tt">${esc(td('detail_title'))}</div>
      <div class="isk-row"><span>${esc(td('detail.status'))}</span><b style="color:${statusColor}">${esc(tagText)}</b></div>
      <div class="isk-row"><span>${esc(td('detail.issue_id'))}</span><b>#${tk.id}</b></div>
      <div class="isk-row"><span>${esc(td('detail.assignee'))}</span><b>${esc(tk.creator || tk.assignee || '—')}</b></div>
      <div class="isk-row"><span>${tk.is_pr ? 'PR' : 'Issue'}</span><b><a href="${esc(issueLink)}" target="_blank" rel="noopener" class="isk-link">#${tk.issue_number} ↗</a></b></div>
      <div class="isk-row"><span>${esc(td('detail.repo'))}</span><b>${esc(bareRepo(tk.repo_key))}</b></div>
      <div class="isk-row"><span>Stage</span><b>${esc(stageLabel(tk))}</b></div>
      <div class="isk-row"><span>${esc(t('pipeline_scene'))}</span><b>${esc(sceneName)}${sceneBadge}</b></div>
      <div class="isk-row"><span>${esc(t('task_mode'))}</span><b>${subtaskBadge}${esc(parentIssue)}</b></div>
      <div class="isk-row"><span>${esc(td('detail.created_at'))}</span><b>${esc(fmtLocal(tk.created_at).substring(0, 16))}</b></div>
      ${tk.completed_at ? `<div class="isk-row"><span>${esc(td('detail.completed_at'))}</span><b>${esc(fmtLocal(tk.completed_at).substring(0, 16))}</b></div>` : ''}
      ${tk.task_description ? `<div class="isk-row isk-row-col isk-row-last"><span style="margin-bottom:4px">${esc(td('detail.no_description') || '任务描述')}</span><div class="isk-desc">${esc(tk.task_description)}</div></div>` : ''}
    </div>`
})

// ═══════════ Agent 节点状态卡（Issue #126 等价） ═══════════
const agentStatusCardHtml = computed(() => {
  const tk = detailTarget.value
  if (!tk || !(tk.agent_statuses || []).length) return ''
  const sm = {
    pending: {
      color: 'var(--t3)',
      bg: 'var(--chip-bg)',
      icon: '○',
      label: t('agent_status.pending'),
    },
    running: { color: '#3b82f6', bg: '#3b82f6', icon: '●', label: t('agent_status.running') },
    completed: { color: 'var(--g)', bg: 'var(--g)', icon: '✓', label: t('agent_status.completed') },
    failed: { color: '#ef4444', bg: '#ef4444', icon: '✕', label: t('agent_status.failed') },
    cancelled: {
      color: 'var(--t3)',
      bg: 'var(--chip-bg)',
      icon: '⊘',
      label: t('agent_status.cancelled'),
    },
  }
  const statuses = tk.agent_statuses
  let html =
    '<div class="isk-card"><div class="isk-card-tt">' + esc(t('agent_status.title')) + '</div>'
  html += '<div class="isk-nodebar">'
  statuses.forEach(function (s, i) {
    const si = sm[s.status] || sm.pending
    const isRunningS = s.status === 'running'
    const isCompletedS = s.status === 'completed'
    const isFailedS = s.status === 'failed'
    const pulseStyle = isRunningS ? ';animation:iskPulse 1.2s ease-in-out infinite' : ''
    const seq = s.latest_seq
    const clickable = (isRunningS || isCompletedS || isFailedS) && seq
    const clickAttr = clickable ? ` data-isk-jump="${seq}" style="cursor:pointer"` : ''
    html += `<div class="isk-node"${clickAttr}>`
    html += `<div class="isk-node-dot" style="background:${si.bg}${pulseStyle}">${si.icon}</div>`
    html += `<span class="isk-node-lbl" style="color:${si.color}">${esc(s.label || s.agent || '')}</span>`
    html += '</div>'
    if (i < statuses.length - 1) {
      const lineCol = isCompletedS ? 'var(--g)' : isFailedS ? '#ef4444' : 'rgba(0,0,0,.1)'
      html += `<div class="isk-node-line" style="background:${lineCol}"></div>`
    }
  })
  html += '</div></div>'
  return html
})

// ═══════════ Issue #539：需求级业务状态 + SRE 验收结论 ═══════════
// 数据来自 GET /issues/{id}/pipeline（review_status / sre_result）。
// 目的：把「节点/事件执行结束」和「需求待人工确认 / 生产发布完成」分开说清，
// 避免用户把节点 completed 误读成需求完成。
const reviewBannerHtml = computed(() => {
  const data = pipelineData()
  const reviewStatus = data.review_status || ''
  const sre = data.sre_result || null
  const rr = (sre && sre.result_ref) || null
  if (reviewStatus !== 'pending_review' && !rr) return ''
  let html = ''
  if (reviewStatus === 'pending_review') {
    html +=
      '<div class="isk-review-banner"><span class="isk-review-ic">◉</span><span><b>' +
      esc(t('status.tag_pending_review')) +
      '</b> — ' +
      esc(td('review_status.pending_review_hint')) +
      '</span></div>'
  }
  if (rr) {
    const tr = (sre && sre.task_ref) || {}
    let body
    if (rr.status === 'available' && rr.summary) {
      body = '<div class="isk-sre-summary">' + esc(String(rr.summary)) + '</div>'
    } else if (rr.status === 'pending') {
      body = '<div class="isk-sre-muted">' + esc(td('review_status.sre_pending')) + '</div>'
    } else {
      body = '<div class="isk-sre-muted">' + esc(td('review_status.sre_unavailable')) + '</div>'
    }
    let trLine = ''
    if (tr.task_id) {
      trLine =
        '<div class="isk-sre-tref">' +
        esc(td('review_status.sre_task_ref')) +
        ': <b>' +
        esc(String(tr.task_id)) +
        '</b>' +
        (tr.task_status ? ' (' + esc(String(tr.task_status)) + ')' : '') +
        '</div>'
    }
    html +=
      '<div class="isk-card"><div class="isk-card-tt">' +
      esc(td('review_status.sre_result_title')) +
      '</div>' +
      body +
      trLine +
      '</div>'
  }
  return html
})

// ═══════════ 流水线卡片（等价老 _renderPipelineSection，data-isk-act 委托） ═══════════
function pipelineData() {
  return pipeline.data || {}
}
const pipelineCardHtml = computed(() => {
  const data = pipelineData()
  const nodes = data.nodes || []
  const events = data.events || []
  if (!nodes.length && !events.length) return ''
  let html = '<div class="isk-card isk-card-click" data-isk-act="pipepage">'
  html += '<div class="isk-card-tt">' + esc(t('pipeline_scene')) + '</div>'
  // Issue #329: 接续来源标注
  const sourceIssue =
    data.source_issue_number ||
    (data.carried_context && data.carried_context.source_issue_number) ||
    ''
  const sourceScene =
    (data.carried_context && data.carried_context.source_scene_name) ||
    (data.carried_context && data.carried_context.source_scene_id) ||
    ''
  if (sourceIssue) {
    let label = '⇄ 接续自 #' + sourceIssue
    if (sourceScene) label += '（' + sourceScene + '）'
    html += `<div class="isk-contin">${label}</div>`
  }
  // 节点进度条
  if (nodes.length > 0) {
    html += '<div class="isk-flowbar">'
    nodes.forEach(function (n, i) {
      const ns = n.node_status
      const isCompleted = ns === 'completed' || ns === 2
      const isCurrent = ns === 'current' || ns === 1
      const isFailed = ns === 'failed' || ns === 3
      const isCancelledN = ns === 'cancelled' || ns === 4
      const isSkipped = ns === 'skipped' || ns === 5
      const col = isCompleted
        ? 'var(--g)'
        : isCurrent
          ? 'var(--a-text)'
          : isFailed
            ? 'var(--r)'
            : isCancelledN
              ? 'var(--t3)'
              : isSkipped
                ? 'var(--t3)'
                : 'var(--t3)'
      const bg = isCompleted
        ? 'var(--g)'
        : isCurrent
          ? 'var(--a-text)'
          : isFailed
            ? 'var(--r)'
            : isCancelledN
              ? 'var(--t3)'
              : isSkipped
                ? 'var(--t3)'
                : 'var(--chip-bg)'
      const pulse = isCurrent ? ';animation:iskPulse 1.2s ease-in-out infinite' : ''
      const nodeSeq = (n.latest_event && n.latest_event.seq) || ''
      // Issue #566：failed/cancelled 终态节点也可点进事件详情执行重新派发
      const clickable = isCurrent || isCompleted || isFailed || isCancelledN
      const clickAttr = clickable
        ? nodeSeq
          ? ` data-isk-jump="${nodeSeq}"`
          : ' data-isk-act="pipepage"'
        : ''
      const cursor = clickable ? ';cursor:pointer' : ''
      const icon = isCompleted ? '✓' : isFailed ? '✕' : isCancelledN ? '⊘' : isSkipped ? '–' : i + 1
      html += `<div class="isk-fnode"${clickAttr} style="cursor:${clickable ? 'pointer' : 'default'}">`
      html += `<div class="isk-fdot" style="background:${bg}${pulse}">${icon}</div>`
      html += `<span class="isk-flbl" style="color:${col}${cursor}">${esc(n.name || n.id || '')}</span>`
      html += '</div>'
      if (i < nodes.length - 1) {
        const nextNs = nodes[i + 1].node_status
        const nextCompleted = nextNs === 'completed' || nextNs === 2
        const lineCol = nextCompleted || isCompleted ? 'var(--g)' : 'rgba(0,0,0,.1)'
        html += `<div class="isk-fline" style="background:${lineCol}"></div>`
      }
    })
    html += '</div>'
  }
  // 事件历史
  if (events.length > 0) {
    html += '<div class="isk-card-tt isk-mt4">' + esc(t('event_history')) + '</div>'
    html += '<div class="isk-evlist">'
    events.forEach(function (e, i) {
      const isLast = i === events.length - 1
      const statusCol =
        e.status === 'completed'
          ? 'var(--g)'
          : e.status === 'failed'
            ? '#ef4444'
            : e.status === 'cancelled'
              ? 'var(--t3)'
              : '#3b82f6'
      const statusIcon =
        e.status === 'completed'
          ? '✓'
          : e.status === 'failed'
            ? '✕'
            : e.status === 'cancelled'
              ? '○'
              : '●'
      const isCrossScene =
        e.event_type === 'cross_scene_handoff' || e.event_type === 'assignee_changed'
      const crossIcon = isCrossScene ? '⇄ ' : ''
      const crossBadge =
        isCrossScene && e.scene_id
          ? ' <span class="isk-cross-badge">' + esc(e.scene_id) + '</span>'
          : ''
      const evSeq = e.seq || ''
      const evAttr = evSeq ? ` data-isk-jump="${evSeq}"` : ''
      html += `<div class="isk-evrow"${evAttr}>`
      html += '<div class="isk-ev-left">'
      html += `<div class="isk-ev-dot" style="color:${statusCol};background:color-mix(in srgb, ${statusCol} 10%, transparent)">${statusIcon}</div>`
      if (!isLast) html += '<div class="isk-ev-line"></div>'
      html += '</div>'
      html += '<div class="isk-ev-main">'
      html += `<div class="isk-ev-head"><span class="isk-ev-type">${crossIcon}${esc(e.agent_type || e.event_type || '')}${crossBadge}</span><span class="isk-ev-time">${esc(fmtEventTime(e.created_at))}</span></div>`
      if (e.title) html += `<div class="isk-ev-title">${esc(e.title)}</div>`
      if (e.summary) html += `<div class="isk-ev-sum">${esc(e.summary)}</div>`
      html += '</div></div>'
    })
    html += '</div>'
  }
  html += '</div>'
  return html
})
// 流水线卡片内点击：委托分发（v-html 坑，批8同款）
function onPipelineClick(ev) {
  const jump = ev.target.closest('[data-isk-jump]')
  if (jump) {
    ev.stopPropagation()
    jumpToNodeEvent(Number(jump.dataset.iskJump))
    return
  }
  const act = ev.target.closest('[data-isk-act="pipepage"]')
  if (act) {
    ev.stopPropagation()
    jumpToPipePage()
  }
}
function jumpToNodeEvent(seq) {
  if (!seq) return
  G.window._taskDetailFromScene = null
  const tk = currentDetailTask()
  G.window._eventFromTask = tk && tk.id ? { taskId: tk.id } : null
  G.window._eventNavFromTask = true
  if (tk && tk.repo_key) setRepoFromKey(tk.repo_key)
  G.window.location.hash = '#event/' + seq
}
function jumpToPipePage() {
  const tk = currentDetailTask()
  if (!tk || !tk.repo_key) return
  setRepoFromKey(tk.repo_key)
  const issueNum = tk.issue_number || ''
  app().nav('pipe', issueNum ? String(issueNum) : undefined)
}
function setRepoFromKey(repoKeyRaw) {
  const rk = String(repoKeyRaw).replace(/^(github|gitcode):/, '')
  const parts = rk.split('/')
  if (parts.length < 2) return
  G.sessionStorage.setItem(
    'currentRepo',
    JSON.stringify({
      owner: parts[0],
      name: parts[1],
      fullName: parts[0] + '/' + parts[1],
      repoKey: repoKeyRaw,
    })
  )
}
function currentDetailTask() {
  return detailView.value || (sel.value && !sel.value.is_pr_event ? sel.value : null)
}

// ═══════════ PR 详情 ═══════════
const prLink = computed(() => {
  const tk = detailView.value
  if (!tk || !tk.repo_key) return ''
  return vcsUrl(tk.repo_key, '/pull/' + tk.issue_number)
})
const prStatus = computed(() => {
  const st = detailView.value ? detailView.value.status : ''
  return {
    color:
      st === 'completed'
        ? 'var(--g)'
        : st === 'cancelled'
          ? 'var(--r)'
          : st === 'processing'
            ? '#3b82f6'
            : 'var(--t3)',
    text:
      st === 'completed'
        ? '审查完成'
        : st === 'cancelled'
          ? '审查失败'
          : st === 'processing'
            ? '审查中'
            : '待处理',
  }
})
// QA 审查历史（等价老 qa_history 渲染）
const prQaHtml = ref('')
function qaVerdictHtml(verdict) {
  if (verdict === 'pass') return '<span class="isk-verdict pass">✓ 审查通过</span>'
  if (verdict === 'fail') return '<span class="isk-verdict fail">✕ 需要修改</span>'
  return '<span class="isk-verdict neutral">— 待定</span>'
}
function renderQaHistory(qaHistory) {
  const valid = (qaHistory || []).filter(
    (qa) => (qa.result && qa.result.length) || (qa.scene_id && qa.scene_id.length)
  )
  if (!valid.length) return ''
  return (
    valid
      .map(function (qa, idx) {
        const result = qa.result || ''
        const sceneName = qa.scene_name || qa.scene_id || ''
        const status = qa.status || ''
        const createdAt = (qa.created_at || '').substring(0, 16)
        const text = result.substring(0, 800) + (result.length > 800 ? '...' : '')
        let verdict = 'neutral'
        const passM = text.match(/(审查通过|审核通过|通过|无风险|安全)/)
        const failM = text.match(/(审查不通过|审核不通过|不通过|有风险|存在问题|建议修改)/)
        if (passM && !failM) verdict = 'pass'
        else if (failM) verdict = 'fail'
        let structured = null
        try {
          const parsed = JSON.parse(result)
          if (parsed && parsed.conclusion) {
            structured = parsed
            if (parsed.conclusion.indexOf('通过') >= 0 && parsed.conclusion.indexOf('不通过') < 0)
              verdict = 'pass'
            else if (parsed.conclusion.indexOf('不通过') >= 0) verdict = 'fail'
          }
        } catch (e) {
          /* ignore */
        }
        const sceneBadge = sceneName
          ? '<span class="isk-scene-badge">' + esc(sceneName) + '</span>'
          : ''
        const statusBadge =
          status === 'running' ? '<span class="isk-qa-running">⚡ 进行中</span>' : ''
        let body
        if (structured) {
          body =
            '<div class="isk-qa-body"><b>结论：</b>' +
            esc(structured.conclusion) +
            '</div>' +
            (structured.actions
              ? '<div class="isk-qa-body"><b>操作：</b>' +
                structured.actions.map((a) => '<div>• ' + esc(a) + '</div>').join('') +
                '</div>'
              : '') +
            (structured.artifacts
              ? '<div class="isk-qa-body"><b>产物：</b> ' +
                structured.artifacts.map(esc).join(', ') +
                '</div>'
              : '')
        } else if (result) {
          body = '<div class="isk-qa-pre">' + esc(text) + '</div>'
        } else {
          body = '<div class="isk-muted isk-qa-small">暂无审查内容</div>'
        }
        return (
          '<div class="isk-qa-item' +
          (idx > 0 ? ' isk-qa-sep' : '') +
          '">' +
          '<div class="isk-qa-head">' +
          qaVerdictHtml(verdict) +
          sceneBadge +
          statusBadge +
          '<span class="isk-qa-time">' +
          esc(createdAt) +
          '</span></div>' +
          body +
          '</div>'
        )
      })
      .join('') || ''
  )
}
function loadPrQa() {
  const tk = detailView.value
  if (!tk || !tk.is_pr_event) return
  const hist = tk.qa_history || []
  if (hist.length > 0) {
    prQaHtml.value =
      renderQaHistory(hist) || '<div class="isk-muted isk-qa-small">暂无审查结果</div>'
    return
  }
  API()
    .get('/events/detail/' + tk.pr_seq)
    .then(function (data) {
      const result = data.result || data.summary || ''
      if (!result) {
        prQaHtml.value = '<div class="isk-muted isk-qa-small">暂无审查结果</div>'
        return
      }
      try {
        const parsed = JSON.parse(result)
        if (parsed && parsed.conclusion) {
          prQaHtml.value =
            '<div class="isk-qa-body"><b>结论：</b>' +
            esc(parsed.conclusion) +
            '</div>' +
            (parsed.actions
              ? '<div class="isk-qa-body"><b>操作：</b>' +
                parsed.actions.map((a) => '<div>• ' + esc(a) + '</div>').join('') +
                '</div>'
              : '') +
            (parsed.artifacts
              ? '<div class="isk-qa-body"><b>产物：</b> ' +
                parsed.artifacts.map(esc).join(', ') +
                '</div>'
              : '')
          return
        }
      } catch (e) {
        /* ignore */
      }
      const text = result.substring(0, 800) + (result.length > 800 ? '...' : '')
      const passM = text.match(/(审查通过|审核通过|通过|无风险|安全)/)
      const failM = text.match(/(审查不通过|审核不通过|不通过|有风险|存在问题|建议修改)/)
      const verdict = passM && !failM ? 'pass' : failM ? 'fail' : 'neutral'
      const vHtml =
        verdict === 'pass'
          ? '<span class="isk-verdict pass lg">✓ 审查通过</span>'
          : verdict === 'fail'
            ? '<span class="isk-verdict fail lg">✕ 需要修改</span>'
            : ''
      prQaHtml.value = vHtml + '<div class="isk-qa-pre">' + esc(text) + '</div>'
    })
    .catch(function () {
      prQaHtml.value = '<div class="isk-muted isk-qa-small">加载失败</div>'
    })
}

// ═══════════ 详情打开/关闭 ═══════════
function openDetailFromBoard(tk) {
  detailView.value = tk
  stopPoll() // 老代码进详情停轮询防覆盖
  loadDetailExtras(tk)
}
function closeDetail() {
  detailView.value = null
  prQaHtml.value = ''
}
const backLabel = computed(() => {
  if (G.window._taskDetailFromScene)
    return (
      '← ' +
      (G.I18N && G.I18N.t('dashboard.scene_detail')
        ? G.I18N.t('dashboard.scene_detail')
        : '场景详情')
    )
  return t('back_to_board') || '返回看板'
})
function backFromDetail() {
  const fromScene = G.window._taskDetailFromScene
  if (fromScene) {
    G.window._taskDetailFromScene = null
    G.window.history.back()
    return
  }
  closeDetail()
}
function loadDetailExtras(tk) {
  // 附件
  attachments.state = 'loading'
  attachments.list = []
  API()
    .get('/issues/' + tk.id + '/attachments')
    .then(function (data) {
      attachments.state = 'ok'
      attachments.list = data.attachments || []
    })
    .catch(function () {
      attachments.state = 'error'
    })
  // 流水线
  pipeline.loading = true
  pipeline.data = null
  API()
    .get('/issues/' + tk.id + '/pipeline')
    .then(function (data) {
      pipeline.loading = false
      pipeline.data = data
    })
    .catch(function () {
      pipeline.loading = false
      pipeline.data = null
    })
  // PR QA
  if (tk.is_pr_event) loadPrQa()
}
// 详情目标变化时刷新异步区
watch(detailTarget, function (tk) {
  if (tk && !detailView.value) loadDetailExtras(tk) // 列表模式切换选中
})

// ═══════════ 附件上传/删除 ═══════════
function onAttachSelect(e) {
  const file = e.target.files && e.target.files[0]
  if (!file) return
  const tk = detailTarget.value
  if (!tk) return
  const formData = new FormData()
  formData.append('file', file)
  attachments.state = 'loading'
  API()
    .upload('/issues/' + tk.id + '/attachments', formData)
    .then(function () {
      loadDetailExtras(tk)
    })
    .catch(function (err) {
      if (err && err.name === 'AbortError' && err.silent) return
      app().showToast(td('upload_failed', { msg: err.message }))
      loadDetailExtras(tk)
    })
  e.target.value = ''
}
function deleteAttach(filename) {
  const tk = detailTarget.value
  if (!tk) return
  app().showConfirm(
    td('confirm_delete_attach'),
    function () {
      API()
        .del('/issues/' + tk.id + '/attachments/' + filename)
        .then(function () {
          loadDetailExtras(tk)
        })
        .catch(function (err) {
          if (err && err.name === 'AbortError' && err.silent) return
          app().showToast(
            t('operation_failed', { msg: (err && err.detail && err.detail.message) || err.message })
          )
        })
    },
    { danger: true }
  )
}

// ═══════════ 任务操作（确认/管理员确认/取消/恢复，_withTaskLoading 等价） ═══════════
const actionBusy = ref(false)
function withTaskLoading(apiCall, successMsg, errorFn) {
  actionBusy.value = true
  apiCall()
    .then(function () {
      if (G.window._currentView !== 'tasks') {
        actionBusy.value = false
        return
      }
      actionBusy.value = false
      app().showToast(successMsg)
      loadTasks()
    })
    .catch(function (e) {
      if (e && e.name === 'AbortError' && e.silent) {
        actionBusy.value = false
        return
      }
      if (G.window._currentView !== 'tasks') {
        actionBusy.value = false
        return
      }
      actionBusy.value = false
      app().showToast(errorFn(e))
      loadTasks()
    })
}
// Issue #393: 仓库管理员判定（repo_member_cache）
const isRepoAdmin = computed(function () {
  const tk = detailTarget.value
  if (!tk) return false
  try {
    const cached = JSON.parse(G.localStorage.getItem('repo_member_cache') || 'null')
    const repos = (cached && cached.repos) || []
    const rk = String(tk.repo_key || '')
    const bare = rk.replace(/^(github|gitcode):/, '')
    const hit = repos.find(function (r) {
      return r.repo_key === rk || r.repo_key === bare || r.full_name === bare
    })
    return !!(hit && (hit.role === 'Admin' || hit.can_host))
  } catch (e) {
    return false
  }
})
function confirmTaskDone(tk, event) {
  grapeConfirm(
    {
      title: t('confirm_done'),
      message: t('confirm_done_message'),
      confirmText: t('confirm_done'),
      onConfirm: function () {
        withTaskLoading(
          function () {
            return API().post('/issues/' + tk.id + '/confirm-done', {
              confirmation: 'user_confirmed',
            })
          },
          t('task_done_toast'),
          function (e) {
            return t('operation_failed', { msg: (e.detail && e.detail.message) || e.message })
          }
        )
      },
    },
    event
  )
}
function adminConfirmTaskDone(tk, event) {
  grapeConfirm(
    {
      title: t('admin_confirm_done'),
      message: t('admin_confirm_done_message'),
      confirmText: t('admin_confirm_done'),
      onConfirm: function () {
        withTaskLoading(
          function () {
            return API().post('/issues/' + tk.id + '/admin-confirm-done')
          },
          t('task_done_toast'),
          function (e) {
            return t('operation_failed', { msg: (e.detail && e.detail.message) || e.message })
          }
        )
      },
    },
    event
  )
}
function cancelTask(tk, event) {
  grapeConfirm(
    {
      title: t('cancel_task'),
      message: t('cancel_task_message'),
      confirmText: t('cancel_task'),
      danger: true,
      onConfirm: function () {
        withTaskLoading(
          function () {
            return API().post('/issues/' + tk.id + '/cancel')
          },
          t('task_cancelled_toast'),
          function (e) {
            return t('operation_failed', { msg: (e.detail && e.detail.message) || e.message })
          }
        )
      },
    },
    event
  )
}
function resumeTask(tk) {
  withTaskLoading(
    function () {
      return API().post('/issues/' + tk.id + '/resume')
    },
    t('task_resumed_toast'),
    function (e) {
      return t('operation_failed', { msg: (e.detail && e.detail.message) || e.message })
    }
  )
}

// ═══════════ grapeConfirm 等价（repos.js 跨模块引用 window._grapeConfirm！保留契约） ═══════════
window._grapeConfirm = function (opts, event) {
  const { title, message, confirmText, cancelText, onConfirm, danger } = opts
  let posX = G.window.innerWidth / 2
  let posY = G.window.innerHeight / 2
  if (event && event.clientX) {
    posX = event.clientX
    posY = event.clientY
  }
  const overlay = document.createElement('div')
  overlay.style.cssText =
    'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.2);backdrop-filter:blur(3px);z-index:1001;opacity:0;transition:opacity .2s'
  const dlg = document.createElement('div')
  const dlgW = 260
  let dlgX = posX - dlgW / 2
  let dlgY = posY - 50
  if (dlgX < 12) dlgX = 12
  if (dlgX + dlgW > G.window.innerWidth - 12) dlgX = G.window.innerWidth - dlgW - 12
  if (dlgY < 12) dlgY = 12
  if (dlgY > G.window.innerHeight - 160) dlgY = G.window.innerHeight - 160
  dlg.style.cssText =
    'position:fixed;left:' +
    dlgX +
    'px;top:' +
    dlgY +
    'px;background:var(--c,#fff);border-radius:12px;padding:14px 18px;max-width:' +
    dlgW +
    'px;width:90%;box-shadow:0 6px 30px rgba(0,0,0,.12);transform:scale(1.05);transition:transform .2s cubic-bezier(.4,0,.2,1)'
  dlg.innerHTML =
    '<div style="font-size:13px;font-weight:600;color:var(--t,#1d1d1f);margin-bottom:4px;text-align:center">' +
    (title || t('confirm') || '确认') +
    '</div>' +
    '<div style="font-size:11px;color:var(--t2,#3a3a3c);line-height:1.35;text-align:center;margin-bottom:12px;white-space:pre-line">' +
    (message || '') +
    '</div>' +
    '<div style="display:flex;gap:6px">' +
    '<button id="_grapeCancel" style="flex:1;padding:6px 0;border-radius:8px;border:none;background:rgba(0,0,0,.06);font-size:11px;font-weight:500;color:var(--t,#1d1d1f);cursor:pointer;font-family:inherit">' +
    (cancelText || t('cancel') || '取消') +
    '</button>' +
    '<button id="_grapeOK" style="flex:1;padding:6px 0;border-radius:8px;border:none;background:' +
    (danger ? 'var(--r)' : 'var(--a-text)') +
    ';font-size:11px;font-weight:600;color: var(--on-accent);cursor:pointer;font-family:inherit">' +
    (confirmText || t('confirm') || '确认') +
    '</button></div>'
  overlay.appendChild(dlg)
  document.body.appendChild(overlay)
  G.window.requestAnimationFrame(function () {
    overlay.style.opacity = '1'
    dlg.style.transform = 'scale(1)'
  })
  function _close() {
    overlay.style.opacity = '0'
    dlg.style.transform = 'scale(1.05)'
    setTimeout(function () {
      overlay.remove()
    }, 200)
  }
  document.getElementById('_grapeCancel').onclick = _close
  document.getElementById('_grapeOK').onclick = function () {
    _close()
    if (onConfirm) setTimeout(onConfirm, 100)
  }
  overlay.onclick = function (e) {
    if (e.target === overlay) _close()
  }
}
function grapeConfirm(opts, event) {
  G.window._grapeConfirm(opts, event)
}

// ═══════════ 进入对话（openTaskConversation 等价） ═══════════
function openTaskConversation(tk) {
  const issueNum = tk && tk.issue_number ? String(tk.issue_number) : ''
  const repoKey = tk && tk.repo_key ? tk.repo_key : ''
  const param =
    issueNum && repoKey
      ? 'issue:' + issueNum + ':' + repoKey
      : (tk && tk.conversation_id) || undefined
  const go = () => app().nav('chat', param)
  if (tk && tk.id && tk.status === 'active') {
    API()
      .post('/issues/' + tk.id + '/start')
      .then(function (d) {
        if (tasksById.value[tk.id] && d && d.status) tasksById.value[tk.id].status = d.status
        go()
      })
      .catch(function () {
        go()
      })
  } else {
    go()
  }
}
function openIssueLink(tk) {
  // Issue #290：查看 Issue 入口统一 /issues/（PR 专属入口走 openPrLink）
  G.window.open(vcsUrl(tk.repo_key, '/issues/' + tk.issue_number), '_blank')
}
function openPrLink(tk) {
  G.window.open(vcsUrl(tk.repo_key, '/pull/' + tk.issue_number), '_blank')
}

// ═══════════ 弹窗（通用：创建/复制/PR介入/跨流水线移交） ═══════════
const modal = reactive({
  open: false,
  mode: 'create', // 'create' | 'copy' | 'pr' | 'cross'
  task: null,
  title: '',
  desc: '',
  sceneId: '',
  scenes: [],
  sceneWarn: null, // {reason, sceneId}
  error: '',
  submitting: false,
  crossTargets: [],
  crossHint: false,
})
const createAttachments = ref([])
function currentUsername() {
  try {
    return JSON.parse(G.localStorage.getItem('user') || '{}').username || ''
  } catch (e) {
    return ''
  }
}
function modalTitle() {
  if (modal.mode === 'copy') return t('copy.modal_title')
  if (modal.mode === 'pr') return '介入解决 PR#' + (modal.task ? modal.task.issue_number : '')
  if (modal.mode === 'cross') return '⇄ ' + (td('cross_pipeline.modal_title') || '跨流水线移交')
  return t('create.modal_title')
}
function submitBtnLabel() {
  if (modal.submitting) {
    if (modal.mode === 'cross') return td('cross_pipeline.btn_submitting') || '移交中...'
    return t(modal.mode === 'copy' ? 'copy.btn_copying' : 'create.btn_creating')
  }
  if (modal.mode === 'cross') return td('cross_pipeline.btn_submit') || '确认移交'
  if (modal.mode === 'copy') return t('copy.btn_copy')
  return t('create.btn_create')
}
function defaultRepoName() {
  const repo = currentRepo()
  if (repo) return repo.fullName
  if (modal.task) return bareRepo(modal.task.repo_key)
  return ''
}
// Issue #460：同步标题到描述——把标题追加进描述（幂等去重，空行分隔）
function syncTitleToDesc() {
  const title = (modal.title || '').trim()
  if (!title) return
  const desc = modal.desc || ''
  if (desc.indexOf(title) >= 0) return
  modal.desc = desc ? desc + '\n\n' + title : title
}
function openCreateModal() {
  modal.open = true
  modal.mode = 'create'
  modal.task = null
  modal.title = ''
  modal.desc = ''
  modal.sceneId = ''
  modal.error = ''
  modal.submitting = false
  modal.sceneWarn = null
  createAttachments.value = []
  loadScenes(defaultRepoName(), true)
}
function openCopyModal(tk) {
  modal.open = true
  modal.mode = 'copy'
  modal.task = tk
  modal.title = (tk.title || '') + (t('copy.title_suffix') || '')
  const issueUrl = vcsUrl(tk.repo_key, '/issues/' + tk.issue_number)
  let desc = tk.task_description || ''
  if (desc) desc += '\n\n'
  desc += (t('copy.issue_link_label') || 'Issue 链接') + ': ' + issueUrl
  modal.desc = desc
  modal.sceneId = ''
  modal.error = ''
  modal.submitting = false
  modal.sceneWarn = null
  createAttachments.value = []
  loadScenes(defaultRepoName(), false)
}
function openPrInterveneModal(tk) {
  if (!tk || !tk.is_pr_event) return
  const prUrl = vcsUrl(tk.repo_key, '/pull/' + tk.issue_number) || ''
  // qa_history 提取去重场景
  const scenes = {}
  ;(tk.qa_history || []).forEach(function (qa) {
    const sid = qa.scene_id || ''
    if (sid && !scenes[sid]) scenes[sid] = qa.scene_name || qa.scene_id || ''
  })
  const sceneList = Object.keys(scenes)
  if (sceneList.length === 0) {
    app().showToast(td('no_scene_for_pr'))
    return
  }
  modal.open = true
  modal.mode = 'pr'
  modal.task = tk
  modal.title = '介入解决 PR#' + tk.issue_number
  modal.desc = 'PR 链接: ' + prUrl + '\n\n请描述需要介入解决的问题:'
  modal.scenes = sceneList.map(function (sid) {
    return { id: sid, label: scenes[sid] }
  })
  modal.sceneId = sceneList[0]
  modal.error = ''
  modal.submitting = false
  modal.sceneWarn = null
  createAttachments.value = []
}
function openCrossPipelineModal(tk) {
  if (!tk) return
  modal.open = true
  modal.mode = 'cross'
  modal.task = tk
  modal.error = ''
  modal.submitting = true // targets 加载完前禁用提交
  modal.crossHint = false
  modal.crossTargets = []
  API()
    .get('/issues/' + tk.id + '/handoff/targets')
    .then(function (d) {
      modal.submitting = false
      modal.crossTargets = d.targets || []
    })
    .catch(function (e) {
      modal.submitting = false
      const msg = (e && e.detail && e.detail.message) || (e && e.message) || String(e)
      modal.error = (td('cross_pipeline.err_failed') || '移交失败: ') + msg
    })
}
function loadScenes(repoName, withOfflineWarn) {
  modal.scenes = []
  API()
    .get('/scenes/with-pipeline?repo=' + encodeURIComponent(repoName))
    .then(function (d) {
      modal.scenes = (d.scenes || []).map(function (s) {
        return {
          id: s.id,
          label:
            (s.display_name || s.name || s.id) +
            (s.scope === 'repo' ? ' 【仓库级】' : '') +
            (withOfflineWarn && s.all_offline ? ' ⚠️' : ''),
          all_offline: !!s.all_offline,
          offline_nodes: s.offline_nodes || [],
          sre_unbound: !!s.sre_unbound,
        }
      })
      updateSceneWarn()
    })
    .catch(function () {
      /* ignore */
    })
}
function updateSceneWarn() {
  modal.sceneWarn = null
  if (modal.mode !== 'create') return
  const s = modal.scenes.find((x) => x.id === modal.sceneId)
  if (!s || !s.all_offline) return
  const nodes = (s.offline_nodes || []).join('、')
  modal.sceneWarn = {
    sceneId: s.id,
    reason: s.sre_unbound
      ? 'SRE 节点未选择机器（SRE 不支持广播模式），部署任务无法执行。请为 SRE 节点绑定个人或仓库级机器。'
      : '该场景绑定的引擎当前全部离线' +
        (nodes ? '（离线节点：' + nodes + '）' : '') +
        '，创建后任务将无法派发。',
  }
}
function goSceneRebind() {
  modal.open = false
  app().nav('scene-detail-local', modal.sceneWarn ? modal.sceneWarn.sceneId : '')
}
// 创建弹窗附件
function onModalAttachSelect(e) {
  const file = e.target.files && e.target.files[0]
  if (!file) return
  const repo = currentRepo()
  const repoKey = repo ? repo.repoKey || repo.fullName || '' : ''
  const formData = new FormData()
  formData.append('file', file)
  const loadingItem = { filename: file.name, safe_name: '', size: 0, url: '', loading: true }
  createAttachments.value.push(loadingItem)
  API()
    .upload('/issues/attachments/pre-upload?repo_key=' + encodeURIComponent(repoKey), formData)
    .then(function (d) {
      const i = createAttachments.value.indexOf(loadingItem)
      if (i >= 0) {
        if (d.error) {
          createAttachments.value.splice(i, 1)
          app().showToast(td('upload_failed', { msg: d.error }), null, 6000)
        } else {
          createAttachments.value[i] = {
            filename: d.filename,
            safe_name: d.safe_name,
            size: d.size,
            url: d.url,
            loading: false,
          }
        }
      }
    })
    .catch(function (err) {
      const i = createAttachments.value.indexOf(loadingItem)
      if (i >= 0) createAttachments.value.splice(i, 1)
      if (err && err.name === 'AbortError' && err.silent) return
      app().showToast(td('upload_failed', { msg: err.message }), null, 6000)
    })
  e.target.value = ''
}
function removeModalAttach(idx) {
  const a = createAttachments.value[idx]
  if (!a) return
  const repo = currentRepo()
  const repoKey = repo ? repo.repoKey || repo.fullName || '' : ''
  API()
    .del(
      '/issues/attachments/pre-upload/' + a.safe_name + '?repo_key=' + encodeURIComponent(repoKey)
    )
    .then(function () {
      createAttachments.value.splice(idx, 1)
    })
    .catch(function () {
      createAttachments.value.splice(idx, 1)
    })
}
// 提交
function submitModal() {
  const repo = currentRepo()
  const repoKey = repo ? repo.repoKey || repo.fullName || '' : ''
  if (modal.mode === 'cross') {
    submitCrossPipeline()
    return
  }
  const isCopy = modal.mode === 'copy'
  if (!repoKey.trim()) {
    modal.error = t('create.err_no_repo')
    return
  }
  if (!modal.title.trim()) {
    modal.error = isCopy ? t('copy.err_no_title') : t('create.err_no_title')
    return
  }
  if (!modal.desc.trim()) {
    modal.error = isCopy ? t('copy.err_no_desc') : t('create.err_no_desc')
    return
  }
  modal.submitting = true
  let owner = repo ? repo.owner || '' : ''
  let rname = repo ? repo.name || '' : ''
  if (!owner || !rname) {
    const p = String(repoKey).split('/')
    owner = p[0] || ''
    rname = p[1] || ''
  }
  const payload = {
    repo_key: repoKey.trim(),
    title: modal.title.trim(),
    description: modal.desc.trim(),
    scene_id: modal.sceneId,
  }
  if (modal.mode === 'create' || modal.mode === 'pr') {
    payload.attachments = createAttachments.value.filter((a) => !a.loading).map((a) => a.safe_name)
  }
  API()
    .post('/issues/' + encodeURIComponent(owner) + '/' + encodeURIComponent(rname), payload)
    .then(function (d) {
      modal.open = false
      if (modal.mode === 'copy') app().showToast(t('copy.success_toast'))
      else if (d && d.message) app().showToast(t('create.success_prefix') + d.message)
      loadTasks()
    })
    .catch(function (e) {
      modal.submitting = false
      const msg = (e && e.detail && e.detail.message) || (e && e.message) || String(e)
      modal.error = t((isCopy ? 'copy' : 'create') + '.err_create_failed', { msg: msg })
    })
}
function submitCrossPipeline() {
  const tk = modal.task
  if (!tk) return
  if (!modal.sceneId) {
    modal.error = td('cross_pipeline.err_no_scene') || '请选择目标场景'
    return
  }
  const scene = modal.crossTargets.find((s) => s.id === modal.sceneId)
  const sceneName = (scene && (scene.display_name || scene.name)) || modal.sceneId
  const confirmMsg = (
    td('cross_pipeline.confirm_message') ||
    '即将把需求 #{issue} 移交到「{scene}」流水线。\n移交后由目标流水线接续处理，原流水线停止流转（历史记录保留）。\n确定继续吗？'
  )
    .replace('{issue}', tk.issue_number)
    .replace('{scene}', sceneName)
  grapeConfirm({
    title: td('cross_pipeline.confirm_title') || '确认移交',
    message: confirmMsg,
    confirmText: td('cross_pipeline.btn_submit') || '确认移交',
    onConfirm: function () {
      doCrossPipelineSubmit(tk, modal.sceneId, sceneName)
    },
  })
}
function doCrossPipelineSubmit(tk, targetSceneId, sceneName) {
  modal.submitting = true
  API()
    .post('/issues/' + tk.id + '/handoff', { target_scene_id: targetSceneId })
    .then(function (d) {
      modal.open = false
      if (d.skipped) {
        let skipMsg = td('cross_pipeline.skipped') || '移交已跳过'
        if (d.skipped === 'duplicate_handoff')
          skipMsg =
            td('cross_pipeline.skipped_dup') || '该需求已在此场景有未完成事件，已拦截重复移交'
        else if (d.skipped === 'target_scene_missing_node')
          skipMsg = td('cross_pipeline.skipped_no_vod') || '目标场景不含 vod 节点'
        app().showToast(skipMsg)
      } else if (d.seq > 0) {
        app().showToast(
          (td('cross_pipeline.success_toast') || '已移交至 {scene} 流水线').replace(
            '{scene}',
            sceneName
          )
        )
      } else {
        app().showToast(td('cross_pipeline.result_unknown') || '移交结果未知')
      }
      loadTasks()
    })
    .catch(function (e) {
      modal.submitting = false
      const msg = (e && e.detail && e.detail.message) || (e && e.message) || String(e)
      app().showToast((td('cross_pipeline.err_failed') || '移交失败: ') + msg)
      loadTasks()
    })
}

// ═══════════ 部署 URL ═══════════
function loadDeployLink() {
  const repo = currentRepo()
  if (!repo) return
  API()
    .get('/repos/deploy-url/' + repo.owner + '/' + repo.name)
    .then(function (d) {
      deployUrl.value = (d && d.deploy_url) || ''
    })
    .catch(function () {
      deployUrl.value = ''
    })
}
function startDeployEdit() {
  deployEditing.value = true
  deployDraft.value = deployUrl.value
}
function saveDeployUrl() {
  const repo = currentRepo()
  if (!repo) return
  const url = deployDraft.value.trim()
  if (url && !url.startsWith('http://') && !url.startsWith('https://')) {
    app().showToast(t('save_failed', { msg: 'URL 必须以 http:// 或 https:// 开头' }))
    return
  }
  API()
    .put('/repos/deploy-url', { full_name: repo.fullName, deploy_url: url })
    .then(function () {
      deployUrl.value = url
      deployEditing.value = false
      app().showToast(t('deploy_url_saved'))
    })
    .catch(function (e) {
      app().showToast(t('save_failed', { msg: (e.detail && e.detail.message) || e.message }))
    })
}
function deployLongPressStart() {
  if (deployLongPressTimer) clearTimeout(deployLongPressTimer)
  deployLongPressTimer = setTimeout(function () {
    startDeployEdit()
  }, 600)
}
function deployLongPressEnd() {
  if (deployLongPressTimer) {
    clearTimeout(deployLongPressTimer)
    deployLongPressTimer = null
  }
}

// ═══════════ 数据加载（loadTasks 等价，含深链/场景跳转链路） ═══════════
// Issue #926：分屏逐步加载——首屏 per_page=30，滚动触底按屏递增（append 按 id 去重合并）
//   opts.append：列表滚动追加下一屏；opts.merge：轮询只拉第 1 屏做增量对账；缺省：重置回第 1 屏
// Issue #2984d027a：重置类请求不再用 loading 闸门拦截（重复进页的第二发会被 nav 的 abort 吃掉 → 永久卡加载），
//   改由 taskReqSeq 让最新一发胜出；只有滚动追加做并发去重。
// 状态 → 服务端 issue.status 多值映射（statuses= 参数）；in_progress 下发 active+processing，
//   服务端粗筛后仍由 filtered 按 stage 精筛（保持旧口径）。
const TASK_STATUS_TO_API = {
  pending: ['active'],
  in_progress: ['active', 'processing'],
  awaiting_input: ['awaiting_input'],
  pending_review: ['pending_review'],
  done: ['completed'],
  cancelled: ['cancelled'],
}
// 重置回第 1 屏（筛选/搜索/板↔列表切换/换模式）：看板走列游标，列表走全局分屏
function resetAndLoad() {
  tasksHasMore.value = false
  tasksTotal.value = null
  taskServerPage = 1
  return loadTasks()
}
function loadTasks(opts) {
  const repo = repoOf()
  const repoKey = repo ? repo.repoKey || repo.fullName || '' : ''
  loadSortPref()
  const append = !!(opts && opts.append)
  const merge = !!(opts && opts.merge)
  // 看板按列分屏：整体刷新一律走列游标重拉（各列各拉下一页）
  if (!append && !merge && boardPagedView.value) {
    resetBoardCursors()
    return loadBoardAll()
  }
  if (append && tasksAppendLoading.value) return // 追加去重；重置请求一律放行
  const seq = ++taskReqSeq
  const reqPage = merge ? 1 : append ? taskServerPage : 1
  // 「仓库全部」视图：服务端分页 + q/statuses 过滤；「我的需求」：条数有限，保持全量拉取
  const usePaging = viewMode.value === 'repo_all' && !!repo
  let apiUrl
  if (usePaging) {
    const apiStatuses = [
      ...new Set(
        filterStatuses.value.reduce(function (acc, k) {
          return acc.concat(TASK_STATUS_TO_API[k] || [])
        }, [])
      ),
    ].join(',')
    apiUrl =
      '/issues/' +
      repo.owner +
      '/' +
      repo.name +
      '?sort_order=' +
      sortOrder.value +
      '&mode=time&per_page=' +
      SERVER_PAGE +
      '&page=' +
      reqPage
    if (query.value) apiUrl += '&q=' + encodeURIComponent(query.value)
    if (apiStatuses) apiUrl += '&statuses=' + encodeURIComponent(apiStatuses)
    if (filterAssignees.value.length)
      apiUrl += '&creators=' + encodeURIComponent(filterAssignees.value.join(','))
  } else {
    apiUrl =
      '/issues/mine?status=&repo_key=' +
      encodeURIComponent(repoKey) +
      '&sort_order=' +
      sortOrder.value +
      '&mode=time' +
      (filterAssignees.value.length
        ? '&creators=' + encodeURIComponent(filterAssignees.value.join(','))
        : '')
  }
  if (append) {
    tasksAppendLoading.value = true
  } else tasksLoading.value = true
  return API()
    .get(apiUrl)
    .then(function (d) {
      if (seq !== taskReqSeq) return // 已被更新的请求取代：loading 旗标归最新请求所有
      if (G.window._currentView !== 'tasks') {
        tasksLoading.value = false
        tasksAppendLoading.value = false
        return // stale guard
      }
      tasksAppendLoading.value = false
      tasksLoading.value = false
      const items = d.issues || d.tasks || []
      if (append || merge) {
        // 第 1 屏对账：未滚动时按第 1 屏清理已删除项（口径只覆盖已加载的第 1 屏）
        if (merge && taskServerPage <= 2) {
          const live = {}
          items.forEach(function (t) {
            live[t.id] = true
          })
          tasks.value = tasks.value.filter(function (t) {
            return live[t.id]
          })
        }
        // 按 id 去重合并：已有项原地更新字段（轮询不整表重排），新项尾部追加
        const byId = {}
        tasks.value.forEach(function (t) {
          byId[t.id] = t
        })
        items.forEach(function (t) {
          if (byId[t.id]) Object.assign(byId[t.id], t)
          else tasks.value.push(t)
        })
      } else {
        tasks.value = items
      }
      if (usePaging && d.page !== undefined) {
        tasksTotal.value = d.total === null || d.total === undefined ? null : d.total
        // 已滚动超过第 1 屏时，has_more 只能按已加载量对账（避免第 1 屏口径误停滚动）
        const beyondFirst = taskServerPage > 2
        tasksHasMore.value =
          beyondFirst && (append || merge)
            ? tasksTotal.value === null
              ? !!d.has_more
              : tasks.value.length < tasksTotal.value
            : !!d.has_more
        // 成功后才推进页码：失败不推进 → 重试不跳屏
        if (!merge) taskServerPage = reqPage + 1
      } else {
        tasksTotal.value = items.length
        tasksHasMore.value = false
      }
      loaded.value = true
      loadError.value = ''
      // Issue #175: 刷新选中/详情指向新对象（旧引用字段已过时）
      if (sel.value) {
        const updated = tasks.value.find((tk) => tk.id === sel.value.id)
        sel.value = updated || null
      }
      if (detailView.value) {
        const updatedD = tasks.value.find((tk) => tk.id === detailView.value.id)
        if (updatedD) detailView.value = updatedD
      }
      if (!sel.value && !detailView.value && tasks.value.length > 0) sel.value = tasks.value[0]
      // Issue #166/#341: pendingIssueNumber 深链匹配（从 window 实时读）
      const pendingIssueNumber = G.window._pendingIssueNumber
      const deeplinkIssueNumber = G.window._deeplinkIssueNumber
      const fromScene = G.window._taskDetailFromScene
      if (pendingIssueNumber) {
        const pinNum = pendingIssueNumber
        G.window._pendingIssueNumber = null
        const match = tasks.value.find(function (tk) {
          return String(tk.issue_number) === pinNum
        })
        if (match) {
          G.window._deeplinkIssueNumber = null
          openDetailFromBoard(match)
        } else if (repo && (serverPagedView.value || viewMode.value === 'repo_all')) {
          // Issue #926：分页后深链不再依赖全量已加载页 → 走详情接口直达
          G.window._taskDetailFromScene = null
          openDeeplinkDetailViaApi(repo, pinNum)
        } else if (fromScene && fromScene.latestSeq) {
          G.window._taskDetailFromScene = null
          app().nav('event', fromScene.latestSeq)
        } else {
          G.window._taskDetailFromScene = null
          if (deeplinkIssueNumber && G.window._deeplinkIssueNumber) {
            G.window._deeplinkIssueNumber = null
            app().showToast(t('deeplink_not_found', { n: deeplinkIssueNumber }))
          }
        }
      }
      // 事件页返回：_pendingTaskDetailId（批8 EventView 的 _backToTaskDetail 契约）
      if (G.window._pendingTaskDetailId) {
        const pid = G.window._pendingTaskDetailId
        G.window._pendingTaskDetailId = null
        if (tasksById.value[pid]) openDetailFromBoard(tasksById.value[pid])
      }
      if (append) {
        // 空页兜底：服务端 has_more 与空页同时出现时停止续拉，避免空转请求
        if (items.length === 0) tasksHasMore.value = false
        setTimeout(maybeLoadMore, 80) // 追加后哨兵可能仍在视口内 → 主动续拉补满首屏
      }
    })
    .catch(function (e) {
      if (seq !== taskReqSeq) return // 已被更新的请求取代，静默丢弃
      tasksAppendLoading.value = false
      tasksLoading.value = false
      if (e.name === 'AbortError' && e.silent) return
      if (G.window._currentView !== 'tasks') return
      G.window._pendingTaskDetailId = null
      G.window._deeplinkIssueNumber = null
      loadError.value = t('load_failed', { msg: e.message })
      loaded.value = true
    })
}

// Issue #926：深链/待打开编号不在已加载分屏内 → 按详情接口构造卡片直达详情
function openDeeplinkDetailViaApi(repo, issueNum) {
  const dn = G.window._deeplinkIssueNumber
  G.window._deeplinkIssueNumber = null
  function giveUp() {
    if (dn) app().showToast(t('deeplink_not_found', { n: dn }))
  }
  API()
    .get('/issues/' + repo.owner + '/' + repo.name + '/' + issueNum)
    .then(function (d) {
      const card = {
        id: d.issue_id,
        issue_number: d.number,
        number: d.number,
        title: d.title || '',
        status: d.task_status || 'active',
        task_status: d.task_status || '',
        stage: d.stage || '',
        owner_agent: d.owner_agent || 'vod',
        scene_id: d.scene_id || '',
        assignee: d.assignee || '',
        creator: d.creator || '',
        is_subtask_mode: d.is_subtask_mode || 0,
        parent_issue_id: d.parent_issue_id || '',
        repo_key: repo.repoKey || repo.fullName || '',
        created_at: '',
        events_count: d.events_count || 0,
        is_running: false,
      }
      if (card.id === null || card.id === undefined) {
        // 详情无内部卡片行（纯 VCS 兜底）→ 无法进详情交互，回退列表 + 提示
        giveUp()
        return
      }
      G.window._tasksById = G.window._tasksById || {}
      G.window._tasksById[card.id] = card
      G.window._pendingTaskDetailId = null
      openDetailFromBoard(card)
    })
    .catch(giveUp)
}

// Issue #926：列表滚动追加下一屏
function loadMoreList() {
  if (!serverPagedView.value) return
  if (!tasksHasMore.value || tasksAppendLoading.value) return
  loadTasks({ append: true })
}
// 哨兵可见性判定（IntersectionObserver 之外的兜底续拉；也用于首屏不满时自动补）
function maybeLoadMore() {
  if (!serverPagedView.value || !tasksHasMore.value || tasksAppendLoading.value) return
  const sn = listSentinelEl.value
  if (!sn) return
  const sr = sn.getBoundingClientRect()
  const el = listScrollEl.value
  // 列表容器自身可滚动时用容器下沿判定，否则用视口下沿（老页面是整页滚动）
  let bottom = window.innerHeight
  if (el && el.scrollHeight > el.clientHeight + 4) {
    bottom = el.getBoundingClientRect().bottom
  }
  if (sr.top <= bottom + 160) loadMoreList()
}
function setupListObserver() {
  teardownListObserver()
  if (typeof window === 'undefined' || !('IntersectionObserver' in window)) return
  if (!listSentinelEl.value) return
  const el = listScrollEl.value
  const scrollable = !!(el && el.scrollHeight > el.clientHeight + 4)
  listObserver = new window.IntersectionObserver(
    function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) maybeLoadMore()
      })
    },
    { root: scrollable ? el : null, rootMargin: '0px 0px 300px 0px', threshold: 0 }
  )
  listObserver.observe(listSentinelEl.value)
}
function teardownListObserver() {
  if (listObserver) {
    listObserver.disconnect()
    listObserver = null
  }
}

// ═══════════ 轮询（场景识别，30s） ═══════════
function startPoll() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = setInterval(function () {
    const needRefresh = tasks.value.some(function (tk) {
      return (
        ((tk.status === 'active' || tk.status === 'processing') && tk.scene_source !== 'vod') ||
        tk.status === 'processing'
      )
    })
    if (needRefresh) {
      // Issue #926：分屏视图只刷新第 1 屏做增量对账，不重置已滚动加载的分屏
      if (boardPagedView.value) loadBoardAll({ merge: true })
      else if (serverPagedView.value) loadTasks({ merge: true })
      else loadTasks()
    } else {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }, 30000)
}
function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}
// app.js 离开 tasks 时调用（typeof 判空安全，保持函数名契约）
G.window._stopScenePoll = stopPoll

// ═══════════ 跨模块桥接（window 契约） ═══════════
// 批8 EventView「返回需求详情」按钮引用（legacy 从未定义——本批补齐）
G.window._backToTaskDetail = function (taskId) {
  G.window._eventFromTask = null
  const tk = tasksById.value[taskId]
  if (!tk) {
    // 任务不在当前列表：nav 回列表
    app().nav('tasks')
    return
  }
  G.window._pendingTaskDetailId = taskId
  if (G.window._currentView !== 'tasks') app().nav('tasks')
  else loadTasks()
}
// 事件页跳流水线节点（老代码 _jumpToNodeEvent/_jumpToPipeline 全局契约）
G.window._jumpToNodeEvent = jumpToNodeEvent
G.window._jumpToPipeline = jumpToPipePage

// Issue #926：进入/离开列表分屏时挂载/卸载尾部哨兵观察器
watch(serverPagedView, function () {
  teardownListObserver()
  nextTick(function () {
    setupListObserver()
  })
})

onMounted(function () {
  G.window._currentView = 'tasks' // renderTasks 入口契约（app.js 已设，防御性再设）
  // Issue #341: 纯数字 param 视为深链 issue_number
  if (
    props.initialParam !== undefined &&
    props.initialParam !== null &&
    /^\d+$/.test(String(props.initialParam))
  ) {
    G.window._pendingIssueNumber = String(props.initialParam)
    G.window._deeplinkIssueNumber = String(props.initialParam)
  } else {
    G.window._deeplinkIssueNumber = null
  }
  G.window._pendingTaskDetailId = null
  loadTasks()
  startPoll()
  loadDeployLink()
})
onBeforeUnmount(function () {
  stopPoll()
  teardownListObserver()
  if (deployLongPressTimer) clearTimeout(deployLongPressTimer)
})
</script>

<style scoped>
/* ═══ IssuesView 局部样式（复用 legacy tsk-* 存量类 + 本视图增量） ═══ */
.isk {
  max-width: none;
  height: calc(100vh - 52px);
  display: flex;
  flex-direction: column;
}
.isk .loading,
.isk .error {
  padding: 40px;
  text-align: center;
}
/* 部署 URL */
.isk-deploy {
  margin-top: 4px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.isk-deploy-input {
  flex: 1;
  max-width: 400px;
  padding: 5px 8px;
  border: 0.5px solid var(--b, #ccc);
  border-radius: 8px;
  font-size: 12px;
  background: var(--c, #fff);
  color: var(--t, #1d1d1f);
}
.isk-deploy-btn {
  padding: 4px 14px;
  font-size: 12px;
  border-radius: 999px;
}
.isk-deploy-link {
  font-size: 12px;
  text-decoration: none;
}
.isk-deploy-empty {
  cursor: pointer;
  font-size: 12px;
  color: var(--t3, #86868b);
  opacity: 0.7;
}
.isk-deploy-empty:hover {
  opacity: 1;
}
/* 视图 Tab（我的需求/仓库全部） */
.isk-viewtabs {
  display: flex;
  gap: 0;
  margin-top: 8px;
  border-bottom: 0.5px solid var(--b, #e8e8ed);
}
.isk-viewtabs button {
  border: none;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  padding: 6px 16px;
  font-size: 13px;
  font-weight: 600;
  color: var(--t2, #3a3a3c);
  background: transparent;
  cursor: pointer;
  font-family: inherit;
}
.isk-viewtabs button.on {
  border-bottom-color: var(--a, #0071e3);
  color: var(--on-accent);
  background: var(--a, #0071e3);
}
/* 筛选 popover */
.isk-pop-wrap {
  position: relative;
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
}
.isk-filter-btn {
  border: 1px solid var(--b);
  border-radius: 999px;
  padding: 4px 14px;
  font-size: 11px;
  cursor: pointer;
  background: transparent;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.isk-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #3b82f6;
}
.isk-popover {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  background: var(--c, #fff);
  border: 0.5px solid var(--b, #e5e5ea);
  border-radius: 12px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
  padding: 12px;
  min-width: 220px;
  max-height: 440px;
  overflow-y: auto;
  z-index: 50;
}
.isk-pop-right {
  left: auto;
  right: 0;
  min-width: 140px;
  padding: 8px;
}
.isk-pop-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  padding-bottom: 6px;
  border-bottom: 0.5px solid var(--b, #e8e8ed);
  font-size: 11px;
  font-weight: 600;
  color: var(--t, #1d1d1f);
}
.isk-pop-head button {
  border: none;
  background: none;
  cursor: pointer;
  font-size: 14px;
  color: var(--t3, #86868b);
  padding: 2px 6px;
  border-radius: 6px;
  line-height: 1;
}
.isk-pop-sec {
  font-size: 10px;
  font-weight: 600;
  color: var(--t3, #86868b);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 6px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.isk-pop-body {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin-bottom: 12px;
}
.isk-chk {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 6px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
  color: var(--t3, #86868b);
}
.isk-chk.on {
  background: rgba(59, 130, 246, 0.06);
}
.isk-box {
  width: 14px;
  height: 14px;
  border-radius: 4px;
  border: 1.5px solid var(--b, #d0d7de);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 9px;
  color: var(--on-accent);
  flex-shrink: 0;
}
.isk-box.on {
  border-color: #3b82f6;
  background: #3b82f6;
}
.isk-cnt {
  font-size: 11px;
  margin-left: auto;
}
.isk-sort-btn {
  width: 100%;
  padding: 6px 8px;
  border: 0.5px solid var(--b, #e5e5ea);
  border-radius: 12px;
  font-size: 11px;
  cursor: pointer;
  background: var(--bg, #f5f5f7);
  color: var(--t, #1d1d1f);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
}
.isk-vopt {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 12px;
  color: var(--t, #1d1d1f);
}
.isk-vopt.on {
  background: rgba(59, 130, 246, 0.06);
}
.isk-vcheck {
  margin-left: auto;
  color: #3b82f6;
  font-size: 12px;
}
.isk-caret {
  font-size: 9px;
  color: var(--t3, #86868b);
}
/* 搜索 */
.isk-searchwrap {
  display: flex;
  justify-content: center;
  margin: 8px 0 4px;
  flex-shrink: 0;
}
.isk-searchbox {
  position: relative;
  width: 100%;
  max-width: 400px;
}
.isk-search-ic {
  position: absolute;
  left: 14px;
  top: 50%;
  transform: translateY(-50%);
  font-size: 13px;
  color: var(--t3, #86868b);
  pointer-events: none;
}
.isk-searchbox input {
  width: 100%;
  padding: 9px 14px 9px 36px;
  border: 0.5px solid var(--b, #e5e5ea);
  border-radius: 12px;
  font-size: 13px;
  background: var(--c, #fff);
  color: var(--t, #1d1d1f);
  outline: none;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
}
.isk-searchbox input:focus {
  border-color: rgba(59, 130, 246, 0.4);
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.08);
}
/* 布局 */
.isk-layout {
  flex: 1;
  min-height: 0;
}
.isk-list,
.isk-detail {
  overflow-y: auto;
  height: 100%;
}
.isk-detail {
  position: relative;
  top: 0;
}
/* 分页 */
.isk-pager {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 10px 16px;
  border-top: 1px solid var(--b, #e5e5ea);
  background: var(--bg, #f5f5f7);
  flex-shrink: 0;
}
.isk-pager button {
  padding: 3px 10px;
  font-size: 11px;
}
.isk-pager button.on {
  border-color: var(--a, #0071e3);
  color: var(--a, #0071e3);
  font-weight: 600;
}
.isk-pager button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.isk-ellipsis {
  color: var(--t3, #86868b);
  padding: 0 2px;
}
/* Issue #460：创建弹窗「同步标题到描述」图标 */
.isk-sync-title {
  cursor: pointer;
  color: var(--a-text);
  font-size: 14px;
  line-height: 1;
  padding: 0 2px;
  user-select: none;
}
.isk-sync-title:hover {
  opacity: 0.7;
}
.isk-pager-sum {
  margin-left: 8px;
  font-size: 10px;
  color: var(--t3, #86868b);
}
/* Issue #926：列表尾部哨兵（滚动追加/收尾提示） */
.isk-sentinel {
  padding: 12px 8px 16px;
  text-align: center;
  font-size: 11px;
  color: var(--t3, #86868b);
  cursor: pointer;
  user-select: none;
}
/* PR 药丸 */
.isk-pr-pill {
  font-size: 10px;
  font-weight: 500;
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(175, 82, 222, 0.1);
  color: #af52de;
  border: 1px solid rgba(175, 82, 222, 0.2);
}
/* 详情卡（v-html 内容） */
.isk :deep(.isk-card) {
  background: rgba(0, 0, 0, 0.02);
  border-radius: 14px;
  border: 0.5px solid var(--b, #e8e8ed);
  padding: 14px 16px;
  margin-bottom: 12px;
}
.isk :deep(.isk-card-rel) {
  position: relative;
}
.isk :deep(.isk-card-click) {
  cursor: pointer;
}
.isk :deep(.isk-card-tt) {
  font-size: 12px;
  font-weight: 600;
  color: var(--t3, #86868b);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 10px;
}
.isk :deep(.isk-mt4) {
  margin-top: 4px;
}
.isk :deep(.isk-row) {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 0;
  border-bottom: 0.5px solid rgba(0, 0, 0, 0.04);
  font-size: 12px;
}
.isk :deep(.isk-row span) {
  color: var(--t3, #86868b);
  font-weight: 400;
}
.isk :deep(.isk-row b) {
  font-size: 13px;
  color: var(--t, #1d1d1f);
  font-weight: 500;
  text-align: right;
  max-width: 60%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.isk :deep(.isk-row-col) {
  flex-direction: column;
  align-items: flex-start;
}
.isk :deep(.isk-row-last) {
  border-bottom: none;
}
.isk :deep(.isk-desc) {
  font-size: 13px;
  color: var(--t2, #3a3a3c);
  line-height: 1.5;
  white-space: pre-wrap;
  width: 100%;
}
.isk :deep(.isk-link) {
  color: var(--a, #0071e3);
  text-decoration: none;
  font-weight: 500;
}
.isk-muted {
  font-size: 12px;
  color: var(--t3, #86868b);
}
/* 附件 */
.isk-attach-card {
  background: rgba(0, 0, 0, 0.02);
  border-radius: 14px;
  border: 0.5px solid var(--b, #e8e8ed);
  padding: 14px 16px;
  margin-bottom: 12px;
}
.isk-attach-card .isk-card-tt {
  font-size: 12px;
  font-weight: 600;
  color: var(--t3, #86868b);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 10px;
}
.isk-attach-list {
  min-height: 30px;
}
.isk-attach-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  border-bottom: 0.5px solid rgba(0, 0, 0, 0.04);
  font-size: 12px;
}
.isk-attach-item a {
  color: var(--a, #0071e3);
  text-decoration: none;
  flex: 1;
}
.isk-attach-thumb {
  width: 32px;
  height: 32px;
  object-fit: cover;
  border-radius: 6px;
}
.isk-attach-clip {
  margin-right: 0;
}
.isk-attach-del {
  margin-left: 6px;
  font-size: 11px;
  color: var(--r, #ff3b30);
  background: none;
  border: none;
  cursor: pointer;
  padding: 2px 4px;
}
.isk-upload-label {
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border-radius: 8px;
  border: 0.5px solid rgba(0, 113, 227, 0.3);
  font-size: 12px;
  color: var(--a, #0071e3);
  width: fit-content;
  margin-top: 10px;
}
.isk-upload-label:hover {
  background: rgba(0, 113, 227, 0.06);
}
/* 操作按钮行 */
.isk-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-start;
  margin-top: 12px;
}
.isk-actions .tsk-btn {
  padding: 4px 14px;
  border-radius: 999px;
  font-size: 12px;
}
.isk-admin-btn {
  background: var(--g-fill);
}
.isk-danger {
  color: var(--r);
}
.isk-handoff {
  color: var(--a, #0071e3);
  border-color: rgba(0, 113, 227, 0.3);
}
/* 流水线卡片内部 */
.isk-pipeline-card {
  margin-top: 12px;
}
.isk :deep(.isk-contin) {
  font-size: 11px;
  color: var(--a, #0071e3);
  background: rgba(0, 113, 227, 0.06);
  border-radius: 8px;
  padding: 6px 10px;
  margin-bottom: 10px;
  display: flex;
  align-items: center;
  gap: 6px;
}
/* Issue #539：需求级待审横幅（阶段执行完成 ≠ 需求/生产发布完成） */
.isk :deep(.isk-review-banner) {
  font-size: 11px;
  color: #ff9f0a;
  background: rgba(255, 159, 10, 0.08);
  border-radius: 10px;
  padding: 8px 12px;
  margin-bottom: 10px;
  display: flex;
  align-items: center;
  gap: 6px;
  line-height: 1.5;
}
.isk :deep(.isk-review-ic) {
  font-size: 13px;
}
/* Issue #539：SRE 验收结论引用块 */
.isk :deep(.isk-sre-summary) {
  font-size: 12px;
  color: var(--t2, #3a3a3c);
  line-height: 1.6;
  background: rgba(0, 0, 0, 0.02);
  border-radius: 8px;
  padding: 10px 12px;
  white-space: pre-wrap;
}
.isk :deep(.isk-sre-muted) {
  font-size: 11px;
  color: var(--t3, #86868b);
}
.isk :deep(.isk-sre-tref) {
  font-size: 10px;
  color: var(--t3, #86868b);
  margin-top: 6px;
}
.isk :deep(.isk-flowbar) {
  display: flex;
  align-items: center;
  margin-bottom: 16px;
  padding: 10px;
  background: rgba(0, 0, 0, 0.02);
  border-radius: 10px;
}
.isk :deep(.isk-fnode) {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  flex: 1;
  min-width: 0;
}
.isk :deep(.isk-fdot) {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--on-accent);
  font-size: 11px;
  font-weight: 600;
  flex-shrink: 0;
  position: relative;
}
.isk :deep(.isk-flbl) {
  font-size: 9px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.isk :deep(.isk-fline) {
  height: 2px;
  flex: 0 0 14px;
  border-radius: 1px;
  margin-top: -12px;
}
.isk :deep(.isk-evlist) {
  display: flex;
  flex-direction: column;
}
.isk :deep(.isk-evrow) {
  display: flex;
  gap: 10px;
  padding-bottom: 10px;
  border-radius: 8px;
  cursor: default;
}
.isk :deep(.isk-evrow:last-child) {
  padding-bottom: 0;
}
.isk :deep(.isk-evrow[data-isk-jump]) {
  cursor: pointer;
}
.isk :deep(.isk-evrow[data-isk-jump]:hover) {
  background: rgba(0, 0, 0, 0.03);
}
.isk :deep(.isk-ev-left) {
  display: flex;
  flex-direction: column;
  align-items: center;
  flex-shrink: 0;
}
.isk :deep(.isk-ev-dot) {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 9px;
  font-weight: 600;
}
.isk :deep(.isk-ev-line) {
  width: 2px;
  flex: 1;
  background: rgba(0, 0, 0, 0.08);
  margin-top: 2px;
  min-height: 16px;
}
.isk :deep(.isk-ev-main) {
  flex: 1;
  min-width: 0;
  padding-top: 1px;
}
.isk :deep(.isk-ev-head) {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.isk :deep(.isk-ev-type) {
  font-size: 11px;
  font-weight: 600;
  color: var(--t, #1d1d1f);
}
.isk :deep(.isk-ev-time) {
  font-size: 10px;
  color: var(--t3, #86868b);
  white-space: nowrap;
}
.isk :deep(.isk-ev-title) {
  font-size: 10px;
  color: var(--t2, #3a3a3c);
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.isk :deep(.isk-ev-sum) {
  font-size: 9px;
  color: var(--t3, #86868b);
  margin-top: 2px;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.isk :deep(.isk-cross-badge) {
  font-size: 9px;
  color: var(--a, #0071e3);
  background: rgba(0, 113, 227, 0.08);
  border-radius: 999px;
  padding: 1px 6px;
}
/* Agent 节点状态卡 */
.isk :deep(.isk-nodebar) {
  display: flex;
  align-items: center;
  padding: 10px;
  background: rgba(0, 0, 0, 0.02);
  border-radius: 10px;
}
.isk :deep(.isk-node) {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  flex: 1;
  min-width: 0;
}
.isk :deep(.isk-node-dot) {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--on-accent);
  font-size: 11px;
  font-weight: 600;
  flex-shrink: 0;
}
.isk :deep(.isk-node-lbl) {
  font-size: 9px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.isk :deep(.isk-node-line) {
  height: 2px;
  flex: 0 0 14px;
  border-radius: 1px;
  margin-top: -12px;
}
/* 看板 */
.isk-boardwrap {
  flex: 1;
  display: flex;
  gap: 10px;
  overflow-x: auto;
  padding-bottom: 4px;
  flex-shrink: 0;
  min-height: 0;
}
.isk-board-col {
  min-width: 240px;
  max-width: 300px;
  flex: 1 1 240px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: transparent;
  border-radius: 14px;
  border: 0.5px solid var(--b, #e8e8ed);
  height: 100%;
  overflow: hidden;
}
.isk-board-col.dim {
  opacity: 0.65;
}
.isk-board-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 14px 10px;
  flex-shrink: 0;
}
.isk-board-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}
.isk-board-name {
  font-weight: 600;
  font-size: 13px;
  color: var(--t, #1d1d1f);
  letter-spacing: -0.2px;
}
.isk-board-cnt {
  font-size: 11px;
  color: var(--t3, #86868b);
  font-weight: 500;
  background: rgba(0, 0, 0, 0.05);
  border-radius: 999px;
  padding: 1px 8px;
}
.isk-board-body {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
  padding: 0 8px 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
/* Issue #1077-F：列底分屏提示 */
.isk-board-foot {
  padding: 8px 4px 4px;
  text-align: center;
  font-size: 11px;
  color: var(--t3, #86868b);
}
.isk-board-empty {
  padding: 24px 12px;
  text-align: center;
  color: var(--t3, #86868b);
  font-size: 12px;
}
.isk-bcard {
  position: relative;
  flex: 0 0 auto;
  min-height: 76px;
  background: var(--c, #fff);
  border: 0.5px solid var(--b, #e5e5ea);
  border-radius: 10px;
  padding: 10px 12px;
  cursor: pointer;
  transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
}
.isk-bcard:hover {
  border-color: rgba(59, 130, 246, 0.3);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}
.isk-bcard-sel {
  border-color: #3b82f6;
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.12);
}
.isk-bcard.tsk-card-radar {
  overflow: hidden;
}
.isk-bcard-in {
  position: relative;
  z-index: 1;
}
.isk-bcard-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  margin-bottom: 4px;
}
.isk-bcard-top .isk-muted {
  font-size: 11px;
  font-weight: 500;
}
.isk-stage {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 10px;
  font-weight: 600;
  border-radius: 4px;
  padding: 2px 6px;
}
.isk-bcard-title {
  margin: 0 0 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--t, #1d1d1f);
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.isk-bcard-meta {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 10px;
  color: var(--t3, #86868b);
}
.isk-bcard-meta .isk-ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 120px;
}
.isk-bcard-meta span:last-child {
  margin-left: auto;
  white-space: nowrap;
}
/* 全屏详情页 */
.isk-detailpage {
  padding: 0;
  max-width: 720px;
  margin: 0 auto;
  flex: 1;
  overflow-y: auto;
}
.isk-dp-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 6px;
}
.isk-dp-head .tsk-btn {
  padding: 6px 14px;
}
.isk-dp-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.isk-dp-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--b, #e5e5ea);
  margin-bottom: 12px;
}
.isk-dp-actions .tsk-btn {
  padding: 4px 14px;
  border-radius: 999px;
  font-size: 12px;
}
/* QA 审查 */
.isk :deep(.isk-verdict) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
}
.isk :deep(.isk-verdict.pass) {
  background: rgba(52, 199, 89, 0.1);
  color: var(--g);
}
.isk :deep(.isk-verdict.fail) {
  background: rgba(255, 59, 48, 0.1);
  color: var(--r);
}
.isk :deep(.isk-verdict.neutral) {
  background: rgba(142, 142, 147, 0.1);
  color: #8e8e93;
}
.isk :deep(.isk-verdict.lg) {
  padding: 4px 10px;
  font-size: 12px;
  margin-bottom: 10px;
}
.isk :deep(.isk-scene-badge) {
  display: inline-flex;
  align-items: center;
  padding: 3px 10px;
  border-radius: 999px;
  background: rgba(0, 113, 227, 0.08);
  color: var(--a, #0071e3);
  font-size: 11px;
  margin-left: 6px;
}
.isk :deep(.isk-qa-running) {
  font-size: 10px;
  color: #ff9f0a;
  margin-left: 6px;
}
.isk :deep(.isk-qa-item) {
  line-height: 1.5;
}
.isk :deep(.isk-qa-sep) {
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid #f2f2f7;
}
.isk :deep(.isk-qa-head) {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 8px;
}
.isk :deep(.isk-qa-time) {
  font-size: 10px;
  color: var(--t3, #86868b);
  margin-left: auto;
}
.isk :deep(.isk-qa-body) {
  font-size: 12px;
  color: var(--t2, #3a3a3c);
  margin-bottom: 4px;
}
.isk :deep(.isk-qa-pre) {
  font-size: 12px;
  color: var(--t2, #3a3a3c);
  line-height: 1.6;
  background: rgba(0, 0, 0, 0.02);
  border-radius: 8px;
  padding: 10px 12px;
  white-space: pre-wrap;
}
.isk :deep(.isk-qa-small) {
  font-size: 12px;
}
/* 通用弹窗（创建/复制/PR介入/跨流水线） */
.isk-modal-ov {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  z-index: 1001;
  display: flex;
  align-items: center;
  justify-content: center;
}
.isk-modal {
  width: min(520px, 92vw);
  background: var(--c, #fff);
  border: 0.5px solid #d2d2d7;
  border-radius: 14px;
  padding: 18px 20px;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.3);
}
.isk-modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 0 0 14px;
}
.isk-modal-head h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--t, #1d1d1f);
}
.isk-modal-close {
  background: none;
  border: none;
  font-size: 18px;
  color: var(--t3, #86868b);
  cursor: pointer;
  padding: 0;
  line-height: 1;
}
.isk-modal-close:hover {
  color: var(--t, #1d1d1f);
}
.isk-modal-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  font-size: 13px;
}
.isk-modal-meta {
  font-size: 11px;
  color: var(--t3, #555);
}
.isk-modal-meta b {
  color: var(--t, #1d1d1f);
}
.isk-fld label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
  color: var(--t2, #333);
}
.isk-fld-hint {
  font-size: 11px;
  color: var(--t3, #888);
}
.isk-fld input,
.isk-fld textarea,
.isk-fld select {
  padding: 7px 10px;
  border: 1px solid var(--b, #ccc);
  border-radius: 6px;
  background: var(--c, #fff);
  color: var(--t, #111);
  font-size: 13px;
  font-family: inherit;
}
.isk-fld textarea {
  resize: vertical;
}
.isk-required {
  color: var(--r);
}
.isk-attach-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-height: 30px;
}
.isk-attach-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  background: rgba(0, 113, 227, 0.06);
  border-radius: 6px;
  font-size: 11px;
}
.isk-attach-chip button {
  margin-left: 2px;
  color: var(--r, #ff3b30);
  background: none;
  border: none;
  cursor: pointer;
  font-size: 11px;
  padding: 0 2px;
}
.isk-modal-err {
  color: var(--r);
  font-size: 12px;
  background: rgba(255, 59, 48, 0.06);
  border: 0.5px solid rgba(255, 59, 48, 0.2);
  border-radius: 8px;
  padding: 8px 12px;
}
.isk-scene-warn {
  margin-top: 4px;
  padding: 8px 12px;
  border-radius: 8px;
  background: rgba(245, 158, 11, 0.08);
  border: 1px solid rgba(245, 158, 11, 0.3);
  font-size: 12px;
  color: #b45309;
  line-height: 1.6;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.isk-scene-warn a {
  color: var(--a, #0071e3);
  text-decoration: none;
  cursor: pointer;
  font-weight: 600;
  white-space: nowrap;
}
.isk-modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 18px;
}
.isk-cross-hint {
  font-size: 11px;
  color: var(--t3, #86868b);
  background: rgba(0, 113, 227, 0.04);
  border-radius: 8px;
  padding: 8px 12px;
}
/* 脉冲动画（老 _grapePulse 等价） */
@keyframes iskPulse {
  0%,
  100% {
    transform: scale(1);
    box-shadow: 0 0 0 0 rgba(59, 130, 246, 0.3);
  }
  50% {
    transform: scale(1.1);
    box-shadow: 0 0 0 6px rgba(59, 130, 246, 0);
  }
}
</style>
