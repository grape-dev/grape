<template>
  <div class="apv">
    <!-- ═══ 列表页 ═══ -->
    <template v-if="!detailId">
      <div class="cd">
        <div class="apv-head">
          <h2><span style="margin-right: 6px">⏰</span>{{ tt('title') }}</h2>
          <button class="bt bt-p" @click="showCreate()">{{ tt('list.create') }}</button>
        </div>
        <div class="apv-toolbar">
          <div class="apv-scopes">
            <button
              v-for="sc in ['all', 'enabled', 'disabled']"
              :key="sc"
              :class="['bt bt-s', scope === sc ? 'bt-tg' : 'bt-o']"
              @click="scope = sc"
            >
              {{ tt('list.scope_' + sc) }} <span class="c">{{ scopeCount(sc) }}</span>
            </button>
          </div>
          <input
            v-model="search"
            type="text"
            class="apv-search"
            :placeholder="tt('list.search_placeholder')"
          />
          <select v-model="sortField" class="apv-sel">
            <option value="name">{{ tt('list.sort_by_name') }}</option>
            <option value="lastRun">{{ tt('list.sort_by_last_run') }}</option>
            <option value="nextRun">{{ tt('list.sort_by_next_run') }}</option>
            <option value="created">{{ tt('list.sort_by_created') }}</option>
          </select>
          <button class="bt bt-s bt-o" @click="sortDir = sortDir === 'asc' ? 'desc' : 'asc'">
            {{ sortDir === 'asc' ? '↑' : '↓' }}
          </button>
          <span class="ts t3">{{ filtered.length }} / {{ tasks.length }}</span>
        </div>
        <!-- 表格 -->
        <div v-if="loadError" class="error">{{ loadError }}</div>
        <div v-else-if="!loaded" class="loading">{{ tc('loading') }}</div>
        <div v-else-if="tasks.length === 0" class="apv-empty">
          <div class="apv-empty-ic">⏰</div>
          <p class="apv-empty-t">{{ tt('list.empty_title') }}</p>
          <p class="apv-empty-d">{{ tt('list.empty_desc') }}</p>
          <div class="apv-tplgrid">
            <button
              v-for="tpl in TEMPLATES"
              :key="tpl.id"
              class="apv-tplcard"
              @click="showCreate(tpl.id)"
            >
              <span class="apv-tpl-ic">{{ tpl.icon }}</span>
              <div class="apv-tpl-body">
                <div class="apv-tpl-t">{{ tt(tpl.title_key) }}</div>
                <div class="apv-tpl-d">{{ tt(tpl.desc_key) }}</div>
              </div>
            </button>
          </div>
          <button class="bt bt-o apv-tpl-more" @click="showCreate()">
            {{ tt('list.empty_create') }}
          </button>
        </div>
        <div v-else-if="filtered.length === 0" class="apv-nomatch">{{ tt('list.no_match') }}</div>
        <div v-else class="apv-grid">
          <div class="apv-gridrow apv-gridhead">
            <div class="apv-c-ck"></div>
            <div class="apv-c-name apv-th" @click="sortBy('name')">
              {{ tt('list.col_name') }}
              <span v-if="sortField === 'name'">{{ sortDir === 'asc' ? '↑' : '↓' }}</span>
            </div>
            <div class="apv-c-cron apv-th">{{ tt('list.col_schedule') }}</div>
            <div class="apv-c-last apv-th" @click="sortBy('lastRun')">
              {{ tt('list.col_last_run') }}
              <span v-if="sortField === 'lastRun'">{{ sortDir === 'asc' ? '↑' : '↓' }}</span>
            </div>
            <div class="apv-c-next apv-th" @click="sortBy('nextRun')">
              {{ tt('list.col_next_run') }}
              <span v-if="sortField === 'nextRun'">{{ sortDir === 'asc' ? '↑' : '↓' }}</span>
            </div>
            <div class="apv-c-st apv-th">{{ tt('list.col_status') }}</div>
            <div class="apv-c-act apv-th"></div>
          </div>
          <div
            v-for="t in filtered"
            :key="t.id"
            :class="['apv-gridrow', selected.has(t.id) ? 'apv-row-sel' : '']"
          >
            <div class="apv-c-ck">
              <input
                type="checkbox"
                :checked="selected.has(t.id)"
                @click.stop="toggleSelect(t.id)"
              />
            </div>
            <div class="apv-c-name apv-cname" @click="goDetail(t.id)">
              <span>{{ t.name }}</span>
              <span v-if="t.work_branch" class="apv-wb-chip">🌿 {{ t.work_branch }}</span>
            </div>
            <div class="apv-c-cron apv-mono">{{ t.cron }}</div>
            <div class="apv-c-last">
              <span class="apv-dot" :style="{ background: lastColor(t) }"></span>
              <span class="apv-time">{{ relativeTime(t.last_run_at) }}</span>
            </div>
            <div class="apv-c-next">
              <span class="apv-time">{{ formatTime(t.next_run_at) }}</span>
            </div>
            <div class="apv-c-st">
              <!-- Issue #3733：已启用但指令为空 → 显式不可派发（不再只靠后端日志）。
                   只在启用状态下提示：暂停的任务本来就不派发，不该常年挂告警。 -->
              <span
                v-if="t.enabled && t.dispatchable === false"
                class="apv-warn"
                :title="dispatchReasonText(t.dispatch_state_reason, 'list')"
                >⚠</span
              >
              <span v-if="t.enabled" class="apv-on">●</span>
              <span v-else class="apv-off">○</span>
            </div>
            <div class="apv-c-act">
              <button class="apv-kebab" @click.stop="toggleRowMenu(t.id, $event)">⋯</button>
            </div>
          </div>
        </div>
        <!-- 批量操作条 -->
        <div v-if="selected.size > 0" class="apv-batchbar">
          <span class="ts">{{ selected.size }} selected</span>
          <button class="bt bt-s bt-o" @click="batchToggle(true)">
            {{ tt('list.batch_enable_all') }}
          </button>
          <button class="bt bt-s bt-o" @click="batchToggle(false)">
            {{ tt('list.batch_pause_all') }}
          </button>
          <button class="bt bt-s bt-r" @click="batchDelete()">
            {{ tt('list.batch_delete_all') }}
          </button>
          <button class="bt bt-s bt-o" @click="selected.clear()">
            {{ tt('list.batch_clear') }}
          </button>
        </div>
      </div>
    </template>

    <!-- ═══ 详情页 ═══ -->
    <template v-else>
      <div v-if="detailError" class="error">{{ detailError }}</div>
      <div v-else-if="!detail" class="loading">{{ tc('loading') }}</div>
      <div v-else>
        <!-- 标题行 -->
        <div class="apv-dp-head">
          <button class="bt bt-o bt-s" style="margin-right: auto" @click="G.app.nav('autopilot')">
            {{ tt('detail.back') }}
          </button>
          <span class="apv-dp-status">{{
            detail.enabled ? tt('detail.status_enabled') : tt('detail.status_paused')
          }}</span>
          <span
            class="apv-switch"
            :class="{ on: detail.enabled }"
            @click.stop="toggleEnabled(detail.id, !detail.enabled)"
            ><span class="apv-knob"></span
          ></span>
          <button class="bt bt-o" @click="editTask(detail.id)">{{ tt('detail.edit') }}</button>
          <button class="bt bt-p" @click="trigger(detail.id)">{{ tt('detail.trigger') }}</button>
          <button :class="['bt', subscribed ? 'bt-p' : 'bt-o']" @click="toggleSubscribe(detail.id)">
            {{ subscribed ? tt('detail.subscribed') : tt('detail.subscribe') }}
          </button>
        </div>
        <!-- Issue #3733：已启用但不可派发（指令为空）——原因可见 + 补齐入口。
             后端只在状态变更时留痕一次，任务页必须把这件事说清楚，否则运营只看到「不跑」。
             条件带 enabled：暂停态不算异常，只有在跑却永远不派发才提示。 -->
        <div v-if="detail.enabled && detail.dispatchable === false" class="apv-blocked">
          <span class="apv-blocked-ic">⚠</span>
          <div class="apv-blocked-txt">
            <b>{{ tt('detail.not_dispatchable_title') }}</b>
            <span>{{ dispatchReasonText(detail.dispatch_state_reason, 'detail') }}</span>
          </div>
          <button class="bt bt-p bt-s" @click="editTask(detail.id)">
            {{ tt('detail.not_dispatchable_fix') }}
          </button>
        </div>
        <!-- 任务信息卡 -->
        <div class="cd apv-card">
          <div class="cd-h">
            <h2>{{ tt('detail.info_title') }}</h2>
          </div>
          <div class="apv-infogrid">
            <div>
              <span class="apv-lbl">{{ tt('detail.name_label') }}</span> <b>{{ detail.name }}</b>
            </div>
            <div>
              <span class="apv-lbl">{{ tt('detail.repo_label') }}</span> {{ detail.repo_key }}
            </div>
            <div>
              <span class="apv-lbl">Skill:</span>
              {{ detail.display_name || detail.skill_name || '—' }}
            </div>
            <div>
              <span class="apv-lbl">{{ tt('detail.engine_label') }}</span> {{ engineNamesText }}
            </div>
            <div>
              <span class="apv-lbl">{{ tt('detail.max_retry_label') }}</span>
              {{ typeof detail.max_retry === 'number' ? detail.max_retry : 3 }}
            </div>
            <div>
              <span class="apv-lbl">{{ tt('detail.created_at_label') }}</span>
              {{ formatTime(detail.created_at) }}
            </div>
          </div>
          <div v-if="engineDetachedHtml" v-html="engineDetachedHtml"></div>
          <div class="apv-instr-wrap">
            <span class="apv-lbl" style="font-size: 12px">{{
              tt('detail.instruction_label')
            }}</span>
            <pre class="apv-instr">{{ detail.instruction || '' }}</pre>
          </div>
        </div>
        <!-- 时间表卡 -->
        <div class="cd apv-card">
          <div class="cd-h">
            <h2>{{ tt('detail.schedule_title') }}</h2>
          </div>
          <div class="apv-cronrow">
            <span style="font-size: 16px">🕒</span>
            <code class="apv-mono">{{ detail.cron }}</code>
            <span class="apv-lbl">→ {{ describeCron(detail.cron) }}</span>
          </div>
          <div v-if="detail.next_run_at" class="apv-cronsub">
            {{ tt('detail.next_run', { time: formatTime(detail.next_run_at) }) }}
          </div>
          <div v-if="detail.last_run_at" class="apv-cronsub">
            {{ tt('detail.last_run', { time: formatTime(detail.last_run_at) }) }}
          </div>
        </div>
        <!-- 高级选项卡 -->
        <div class="cd apv-card">
          <div class="cd-h apv-adv-head">
            <h2>{{ tt('detail.advanced_title') }}</h2>
            <button class="bt bt-p bt-s" @click="saveAdvanced()">{{ tt('advanced.save') }}</button>
          </div>
          <div class="apv-adv-wb">
            <label class="apv-lbl-s">{{ tt('advanced.work_branch_label') }}</label>
            <span
              class="apv-switch sm"
              :class="{ on: detailWbOn }"
              @click="detailWbOn = !detailWbOn"
            >
              <span class="apv-knob"></span>
            </span>
            <span class="apv-wb-lbl" :class="{ on: detailWbOn }">{{
              detailWbOn ? tt('advanced.enabled') : tt('advanced.disabled')
            }}</span>
          </div>
          <div class="apv-adv-wbsel" :class="{ off: !detailWbOn }">
            <select v-model="detailWbBranch" class="apv-input">
              <option v-if="!detailBranches.length" value="">{{ tc('loading') }}</option>
              <option v-for="br in detailBranches" :key="br.name" :value="br.name">
                {{ br.name }}{{ br.protected ? ' 🔒' : '' }}
              </option>
            </select>
          </div>
          <div>
            <label class="apv-lbl-s">{{ tt('advanced.custom_fields_label') }}</label>
            <div class="apv-cf-list">
              <div v-for="(f, i) in detailCustomFields" :key="i" class="apv-cf-row">
                <input
                  v-model="f.name"
                  class="apv-cf-key"
                  :class="{ 'cf-invalid': nameError(f.name) }"
                  placeholder="NAME"
                />
                <input
                  v-model="f.value"
                  :type="f.show ? 'text' : 'password'"
                  class="apv-cf-val"
                  placeholder="value"
                />
                <button class="apv-cf-eye" @click="f.show = !f.show">
                  {{ f.show ? '🙈' : '👁' }}
                </button>
                <button class="apv-cf-del" @click="detailCustomFields.splice(i, 1)">×</button>
              </div>
            </div>
            <button class="bt bt-o bt-s apv-cf-add" @click="addDetailField()">
              {{ tt('advanced.custom_fields_add') }}
            </button>
          </div>
        </div>
        <!-- 执行历史卡 -->
        <div class="cd">
          <div class="cd-h">
            <h2>{{ tt('detail.history_title') }}</h2>
          </div>
          <div v-if="execLoading" class="loading">{{ tc('loading') }}</div>
          <div v-else-if="execError" class="error">{{ execError }}</div>
          <div v-else-if="!executions.length" class="apv-nomatch">{{ tt('exec.no_records') }}</div>
          <template v-else>
            <div
              v-for="e in executions"
              :key="e.id"
              :class="['apv-exec-row', openExec.has(e.id) ? 'apv-exec-open' : '']"
              @click="toggleExecDetail(e)"
            >
              <div class="apv-exec-main">
                <span class="apv-exec-ic">{{ execIcon(e.status) }}</span>
                <span class="apv-exec-st" :style="{ color: execColor(e.status) }">{{
                  e.status
                }}</span>
                <span class="apv-exec-time">{{ formatTime(e.started_at || e.created_at) }}</span>
                <span class="apv-exec-res">{{ execResult(e) }}</span>
                <span v-if="e.has_findings" class="apv-badge">{{ tt('exec.has_issues') }}</span>
                <span class="apv-exec-rtry">{{
                  tt('exec.retry_count', { count: e.retry_count || 0 })
                }}</span>
                <button v-if="e.agent_task_id" class="apv-logbtn" @click.stop="openTaskLog(e)">
                  {{ tt('exec.log_btn') }}
                </button>
                <span class="apv-exec-arrow" :class="{ open: openExec.has(e.id) }">▼</span>
              </div>
              <div v-if="openExec.has(e.id)" class="apv-exec-detail" @click.stop>
                <div class="apv-exec-lbl">{{ tt('exec.result_label') }}</div>
                <pre ref="execPre" class="apv-exec-pre">{{
                  e.result || e.error || tt('exec.no_result')
                }}</pre>
                <!-- findings 懒加载 -->
                <div v-if="e.id === findingsLoadingFor" class="apv-muted">{{ tc('loading') }}</div>
                <div v-else-if="findingsMap[e.id]">
                  <div v-if="!findingsMap[e.id].length" class="apv-muted">
                    {{ tt('findings.none_found') }}
                  </div>
                  <template v-else>
                    <div class="apv-muted apv-findings-t">
                      {{ tt('findings.found_title', { count: findingsMap[e.id].length }) }}
                    </div>
                    <div v-for="f in findingsMap[e.id]" :key="f.id" class="apv-finding">
                      <span class="apv-find-ic">{{ sevIcon(f.severity) }}</span>
                      <div class="apv-find-body">
                        <div class="apv-find-t">
                          {{ f.title }}
                          <!-- Issue #697：疑似重复（根因不一致）需 owner 确认后再派发 -->
                          <span
                            v-if="f.dedup_level === 'suspected_duplicate'"
                            class="apv-dup-badge"
                            :title="f.dedup_note || ''"
                            >{{ tt('findings.suspected_dup_badge') }}</span
                          >
                        </div>
                        <div class="apv-find-d">{{ f.description || '' }}</div>
                        <div class="apv-find-sev" :style="{ color: sevColor(f.severity) }">
                          {{ f.severity || 'medium' }}{{ f.category ? ' · ' + f.category : '' }}
                        </div>
                      </div>
                      <div class="apv-find-act">
                        <span v-if="f.dispatched_issue_number" class="apv-muted">
                          {{ tt('findings.dispatched', { n: f.dispatched_issue_number }) }}
                        </span>
                        <button v-else class="bt bt-s bt-p" @click="dispatchFinding(e.id, f)">
                          {{ tt('findings.dispatch_btn') }}
                        </button>
                      </div>
                    </div>
                  </template>
                </div>
              </div>
            </div>
          </template>
        </div>
      </div>
    </template>

    <!-- ═══ 创建/编辑弹窗 ═══ -->
    <div v-if="dlg.open" class="apv-modal-ov">
      <div class="apv-dialog" :class="{ expanded: dlg.expanded }">
        <div class="apv-dlg-head">
          <span class="apv-dlg-title">
            <span class="apv-dlg-ic">⚡</span
            >{{ dlg.editingId ? tt('form.edit_title') : tt('form.create_title') }}
          </span>
          <div class="apv-dlg-tools">
            <button
              class="apv-dlg-tool"
              :title="dlg.expanded ? tt('form.shrink') : tt('form.expand')"
              @click="dlg.expanded = !dlg.expanded"
            >
              {{ dlg.expanded ? '⤡' : '⤢' }}
            </button>
            <button class="apv-dlg-close" @click="dlg.open = false">×</button>
          </div>
        </div>
        <div class="apv-dlg-body">
          <!-- 左列 -->
          <div class="apv-dlg-left">
            <div class="apv-fld">
              <label class="apv-lbl-s"
                >{{ tt('form.name_label') }} <span class="apv-req">*</span></label
              >
              <input
                v-model="dlg.name"
                type="text"
                :placeholder="tt('form.name_placeholder')"
                class="apv-input lg"
              />
              <div class="apv-hint">{{ tt('form.name_hint') }}</div>
            </div>
            <div class="apv-fld">
              <label class="apv-lbl-s">{{ tt('form.engine_label') }}</label>
              <div class="apv-pickerbtn" @click="openEnginePicker">
                <span
                  :style="{ color: selectedEngines.size ? 'var(--a-text)' : 'var(--g)', fontWeight: 500 }"
                >
                  {{
                    selectedEngines.size
                      ? tt('engine_picker.selected_count', { count: selectedEngines.size })
                      : tt('form.engine_broadcast')
                  }}
                </span>
              </div>
              <div class="apv-hint">{{ tt('form.engine_hint') }}</div>
            </div>
            <div class="apv-fld">
              <label class="apv-lbl-s">{{ tt('form.skill_label') }}</label>
              <div class="apv-pickerbtn" @click="openSkillPicker">
                <span :style="{ color: selectedSkill ? 'var(--a)' : 'var(--t3)', fontWeight: 500 }">
                  {{ selectedSkill || tt('form.skill_empty') }}
                </span>
              </div>
              <div class="apv-hint">{{ tt('form.skill_hint') }}</div>
            </div>
            <div class="apv-fld apv-dlg-instr">
              <div class="apv-instr-head">
                <label class="apv-lbl-s"
                  >{{ tt('form.instruction_label') }} <span class="apv-req">*</span></label
                >
                <button class="bt bt-s bt-o" :disabled="dlg.polishing" @click="polishInstruction()">
                  {{ dlg.polishing ? tt('form.polishing') : tt('form.polish_btn') }}
                </button>
              </div>
              <textarea
                v-model="dlg.instruction"
                rows="6"
                :placeholder="tt('form.instruction_placeholder')"
                class="apv-input area"
              ></textarea>
              <div class="apv-hint">{{ tt('form.instruction_hint') }}</div>
            </div>
          </div>
          <!-- 右列 -->
          <aside class="apv-dlg-right">
            <div class="apv-fld">
              <div class="apv-sect">
                {{ tt('form.schedule_title') }} <span class="apv-req">*</span>
              </div>
              <div class="apv-schedbox">
                <!-- 高级模式 -->
                <template v-if="sched.advanced">
                  <div class="apv-lbl-s">{{ tt('schedule.advanced_mode') }}</div>
                  <input
                    :value="sched.rawCron"
                    type="text"
                    class="apv-input mono"
                    :placeholder="tt('schedule.advanced_placeholder')"
                    @input="onRawCron($event)"
                  />
                  <div class="apv-hint">{{ tt('schedule.advanced_hint') }}</div>
                  <button class="bt bt-s bt-o apv-mt6" @click="exitAdvanced()">
                    {{ tt('schedule.back_to_structured') }}
                  </button>
                </template>
                <!-- 结构化模式 -->
                <template v-else>
                  <fieldset class="apv-fset">
                    <legend class="apv-lbl-s">{{ tt('schedule.time_legend') }}</legend>
                    <div class="apv-seg">
                      <button :class="{ on: sched.timeKind === 'at' }" @click="setTimeKind('at')">
                        {{ tt('schedule.time_at') }}
                      </button>
                      <button
                        :class="{ on: sched.timeKind === 'every' }"
                        @click="setTimeKind('every')"
                      >
                        {{ tt('schedule.time_every') }}
                      </button>
                    </div>
                    <div v-if="sched.timeKind === 'at'" class="apv-mt6">
                      <input
                        v-model="sched.timeAt"
                        type="time"
                        class="apv-input time"
                        @input="syncCron"
                      />
                    </div>
                    <template v-else>
                      <div class="apv-everyrow">
                        <div class="apv-inputgrp">
                          <span class="apv-igrp-lbl">{{ tt('schedule.every_prefix') }}</span>
                          <input
                            v-model.number="sched.everyInterval"
                            type="number"
                            min="1"
                            :max="sched.everyUnit === 'hours' ? 23 : 59"
                            class="apv-num"
                            @input="syncCron"
                          />
                          <select
                            v-model="sched.everyUnit"
                            class="apv-igrp-sel"
                            @change="onUnitChange"
                          >
                            <option value="minutes">{{ tt('schedule.unit_minutes') }}</option>
                            <option value="hours">{{ tt('schedule.unit_hours') }}</option>
                          </select>
                        </div>
                        <div class="apv-inputgrp">
                          <span class="apv-igrp-lbl">🕒</span>
                          <template v-if="sched.everyUnit === 'minutes'">
                            <select
                              v-model="sched.winFrom"
                              class="apv-igrp-sel flex"
                              @change="onWinFrom"
                            >
                              <option v-for="h in 24" :key="'f' + h" :value="pad(h - 1) + ':00'">
                                {{ pad(h - 1) }}:00
                              </option>
                            </select>
                            <span class="apv-tilde">~</span>
                            <select
                              v-model="sched.winTo"
                              class="apv-igrp-sel flex"
                              @change="onWinTo"
                            >
                              <option v-for="h in 24" :key="'t' + h" :value="pad(h - 1) + ':59'">
                                {{ pad(h - 1) }}:59
                              </option>
                            </select>
                          </template>
                          <template v-else>
                            <input
                              v-model="sched.winFrom"
                              type="time"
                              class="apv-igrp-sel flex"
                              @change="onWinFrom"
                            />
                            <span class="apv-tilde">~</span>
                            <input
                              v-model="sched.winTo"
                              type="time"
                              class="apv-igrp-sel flex"
                              @change="onWinTo"
                            />
                          </template>
                        </div>
                      </div>
                      <div v-if="sched.everyUnit === 'hours'" class="apv-minrow">
                        <span class="apv-hint">{{ tt('schedule.monthly_prefix') }}</span>
                        <input
                          v-model.number="sched.everyMinute"
                          type="number"
                          min="0"
                          max="59"
                          class="apv-num sm"
                          @input="syncCron"
                        />
                        <span class="apv-hint">{{ tt('schedule.monthly_suffix') }}</span>
                      </div>
                    </template>
                  </fieldset>
                  <fieldset class="apv-fset">
                    <legend class="apv-lbl-s">{{ tt('schedule.date_legend') }}</legend>
                    <div class="apv-dayrow">
                      <select v-model="sched.dayKind" class="apv-input flex" @change="syncCron">
                        <option value="every">{{ tt('schedule.day_every') }}</option>
                        <option value="weekly">{{ tt('schedule.day_weekly') }}</option>
                        <option value="monthly">{{ tt('schedule.day_monthly') }}</option>
                      </select>
                      <input
                        v-if="sched.dayKind === 'monthly'"
                        v-model.number="sched.monthlyDay"
                        type="number"
                        min="1"
                        max="31"
                        class="apv-input"
                        @input="syncCron"
                      />
                    </div>
                    <div v-if="sched.dayKind === 'weekly'" class="apv-weekdays">
                      <button
                        v-for="(dk, i) in DAY_KEYS"
                        :key="dk"
                        :class="{ on: sched.weeklyDays.includes(i) }"
                        @click="toggleDay(i)"
                      >
                        {{ tt('cron.' + dk) }}
                      </button>
                    </div>
                    <div
                      v-if="sched.dayKind === 'monthly' && sched.monthlyDay >= 29"
                      class="apv-hint"
                    >
                      {{ tt('schedule.feb_hint') }}
                    </div>
                  </fieldset>
                  <div class="apv-advlink">
                    <span @click="enterAdvanced()">{{ tt('schedule.advanced_link') }}</span>
                  </div>
                </template>
              </div>
              <!-- cron 预览 -->
              <div class="apv-cronpreview">
                <div class="apv-mono apv-cronstr">{{ cron }}</div>
                <div v-if="schedDesc" class="apv-cron-desc">🕒 {{ schedDesc }}</div>
                <div v-else class="apv-hint">{{ tt('schedule.advanced_expr') }}</div>
                <div v-if="nextRuns.length" class="apv-hint apv-nextrun-l">
                  {{ tt('schedule.next_run') }}
                </div>
                <div v-for="(nr, i) in nextRuns" :key="i" class="apv-hint">{{ nr }}</div>
                <div v-if="!nextRuns.length" class="apv-hint">
                  {{ tt('schedule.next_run_invalid') }}
                </div>
              </div>
            </div>
            <div class="apv-fld">
              <div class="apv-sect">{{ tt('form.settings_title') }}</div>
              <div class="apv-retryrow">
                <label class="apv-lbl-m">{{ tt('form.max_retry_label') }}</label>
                <input
                  v-model.number="dlg.maxRetry"
                  type="number"
                  min="0"
                  max="10"
                  class="apv-num"
                />
                <span class="apv-hint">{{ tt('form.max_retry_hint') }}</span>
              </div>
            </div>
            <div class="apv-fld">
              <div class="apv-sect">{{ tt('form.advanced_title') }}</div>
              <div class="apv-adv-wb">
                <label class="apv-lbl-s">{{ tt('form.work_branch_label') }}</label>
                <span class="apv-switch sm" :class="{ on: dlg.wbOn }" @click="dlg.wbOn = !dlg.wbOn">
                  <span class="apv-knob"></span>
                </span>
                <span class="apv-wb-lbl" :class="{ on: dlg.wbOn }">{{
                  dlg.wbOn ? tt('advanced.enabled') : tt('advanced.disabled')
                }}</span>
              </div>
              <div class="apv-adv-wbsel" :class="{ off: !dlg.wbOn }">
                <select v-model="dlg.workBranch" class="apv-input">
                  <option v-if="!dlgBranches.length" value="">{{ tc('loading') }}</option>
                  <option v-for="br in dlgBranches" :key="br.name" :value="br.name">
                    {{ br.name }}{{ br.protected ? ' 🔒' : '' }}
                  </option>
                </select>
                <div class="apv-hint">{{ tt('form.work_branch_hint') }}</div>
              </div>
              <div>
                <label class="apv-lbl-s">{{ tt('form.custom_fields_label') }}</label>
                <div class="apv-cf-list">
                  <div v-for="(f, i) in dlgCustomFields" :key="i" class="apv-cf-row">
                    <input
                      v-model="f.name"
                      class="apv-cf-key"
                      :class="{ 'cf-invalid': nameError(f.name) }"
                      placeholder="NAME"
                    />
                    <input
                      v-model="f.value"
                      :type="f.show ? 'text' : 'password'"
                      class="apv-cf-val"
                      placeholder="value"
                    />
                    <button class="apv-cf-eye" @click="f.show = !f.show">
                      {{ f.show ? '🙈' : '👁' }}
                    </button>
                    <button class="apv-cf-del" @click="dlgCustomFields.splice(i, 1)">×</button>
                  </div>
                </div>
                <button class="bt bt-o bt-s apv-cf-add" @click="addDlgField()">
                  {{ tt('form.custom_fields_add') }}
                </button>
                <div class="apv-hint">{{ tt('form.custom_fields_hint') }}</div>
              </div>
            </div>
          </aside>
        </div>
        <div class="apv-dlg-foot">
          <span class="apv-hint">{{ tt('form.required_hint') }}</span>
          <div class="apv-dlg-btns">
            <button class="bt bt-o" @click="dlg.open = false">{{ tc('cancel') }}</button>
            <button class="bt bt-p" :disabled="dlg.submitting" @click="submitForm()">
              {{
                dlg.submitting
                  ? tt('form.submitting')
                  : dlg.editingId
                    ? tt('form.btn_save')
                    : tt('form.btn_create')
              }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══ 引擎选择弹窗 ═══ -->
    <div v-if="engPicker.open" class="apv-modal-ov" @click.self="engPicker.open = false">
      <div class="apv-pickmodal">
        <h3>{{ tt('engine_picker.title') }}</h3>
        <div class="apv-pick-sub">
          {{ tt('engine_picker.available_count', { count: engines.length }) }}
        </div>
        <div class="apv-pick-tags">
          <span
            v-for="t in engAvailTypes"
            :key="'t' + t"
            :class="{ on: engPicker.type === t }"
            @click="engPicker.type = engPicker.type === t ? '' : t"
            >{{ t }}</span
          >
          <span v-if="engAvailTypes.length" class="apv-tag-sep"></span>
          <span
            v-for="s in engAvailScopes"
            :key="'s' + s"
            :class="{ on: engPicker.scope === s }"
            @click="engPicker.scope = engPicker.scope === s ? '' : s"
            >{{ scopeLabel(s) }}</span
          >
          <span style="flex: 1"></span>
          <span class="apv-tag-clear" @click="selectedEngines.clear()">{{
            tt('engine_picker.clear_all')
          }}</span>
        </div>
        <input
          v-model="engPicker.q"
          class="apv-pick-search"
          :placeholder="tt('engine_picker.search_placeholder')"
        />
        <div class="apv-pick-list">
          <div v-if="!engFiltered.length" class="apv-pick-empty">
            {{ tt('engine_picker.no_engines') }}
          </div>
          <template v-for="s in SCOPE_ORDER" :key="'grp' + s">
            <template v-if="engByScope[s]">
              <div class="apv-pick-group">{{ scopeLabel(s) }} ({{ engByScope[s].length }})</div>
              <label
                v-for="e in engByScope[s]"
                :key="e.id"
                class="apv-pick-item"
                :class="{ on: selectedEngines.has(engineKey(e)) }"
              >
                <input
                  type="checkbox"
                  :checked="selectedEngines.has(engineKey(e))"
                  @change="toggleEngine(engineKey(e))"
                />
                <div class="apv-pick-body">
                  <div class="apv-pick-name">
                    {{ e.alias || engineDisplayName(e) }}
                    <span v-if="e.alias" class="apv-pick-alias">({{ engineDisplayName(e) }})</span>
                    <span
                      v-for="tg in e.tags || []"
                      :key="tg"
                      class="apv-engtag"
                      :style="{ background: tagColor(tg) }"
                      >{{ tgTag(tg) }}</span
                    >
                    <span class="apv-scopetag" :style="{ background: scopeColor(e.scope) }">{{
                      scopeLabel(e.scope)
                    }}</span>
                  </div>
                  <div class="apv-pick-meta">
                    {{ engineDisplayName(e) !== e.name ? engineDisplayName(e) + ' · ' : ''
                    }}{{ e.host_name || e.worker_name || e.name }}
                  </div>
                </div>
                <span class="apv-pick-online">{{ tt('engine_picker.online') }}</span>
              </label>
            </template>
          </template>
          <!-- Issue #486：编辑态原绑定但已超出当前可见范围的引擎，仅可移除 -->
          <template v-if="detachedEngineKeys.length">
            <div class="apv-pick-group apv-detached-g">
              {{ tt('engine_picker.detached_group') }} ({{ detachedEngineKeys.length }})
            </div>
            <label
              v-for="eid in detachedEngineKeys"
              :key="'detached-' + eid"
              class="apv-pick-item apv-detached"
              :class="{ on: true }"
            >
              <input type="checkbox" :checked="true" @change="toggleEngine(eid)" />
              <div class="apv-pick-body">
                <div class="apv-pick-name">#{{ eid }}</div>
                <div class="apv-pick-meta">{{ tt('engine_picker.detached_hint') }}</div>
              </div>
              <span class="apv-pick-online apv-detached-warn">⚠️</span>
            </label>
          </template>
        </div>
        <div class="apv-pick-foot">
          <button class="bt bt-o" @click="engPicker.open = false">{{ tc('cancel') }}</button>
          <button class="bt bt-p" @click="engPicker.open = false">{{ tc('confirm') }}</button>
        </div>
      </div>
    </div>

    <!-- ═══ 技能选择弹窗 ═══ -->
    <div v-if="skillPicker.open" class="apv-modal-ov" @click.self="skillPicker.open = false">
      <div class="apv-pickmodal short">
        <h3>{{ tt('skill_picker.title') }}</h3>
        <div class="apv-pick-sub">
          {{ tt('skill_picker.available_count', { count: autoSkills.length }) }}
        </div>
        <input
          v-model="skillPicker.q"
          class="apv-pick-search"
          :placeholder="tt('skill_picker.search_placeholder')"
        />
        <div class="apv-pick-list">
          <div v-if="!skillFiltered.length" class="apv-pick-empty">
            {{ tt('skill_picker.no_skills') }}
          </div>
          <label class="apv-pick-item" :class="{ on: !selectedSkill }" @click="pickSkill('')">
            <input type="radio" :checked="!selectedSkill" name="apSkillRadio" />
            <div class="apv-pick-body">
              <div class="apv-pick-name">{{ tt('skill_picker.no_selection') }}</div>
              <div class="apv-pick-meta">{{ tt('skill_picker.no_selection_desc') }}</div>
            </div>
          </label>
          <label
            v-for="s in skillFiltered"
            :key="s.skill_name"
            class="apv-pick-item"
            :class="{ on: selectedSkill === s.skill_name }"
            @click="pickSkill(s.skill_name)"
          >
            <input type="radio" :checked="selectedSkill === s.skill_name" name="apSkillRadio" />
            <div class="apv-pick-body">
              <div class="apv-pick-name">
                {{ s.display_name }}
                <span
                  v-if="s.scope"
                  class="apv-scopetag"
                  :style="{ background: skillScopeColor(s.scope) }"
                  >{{ skillScopeLabel(s.scope) }}</span
                >
                <span class="apv-pick-alias">({{ s.skill_name }})</span>
              </div>
              <div class="apv-pick-meta">
                {{ s.description || tt('skill_picker.no_description') }}
              </div>
            </div>
          </label>
        </div>
        <div class="apv-pick-foot">
          <button class="bt bt-p" @click="skillPicker.open = false">{{ tc('confirm') }}</button>
        </div>
      </div>
    </div>

    <!-- ═══ 派发问题弹窗 ═══ -->
    <div v-if="dispatchDlg.open" class="apv-modal-ov" @click.self="dispatchDlg.open = false">
      <div class="apv-dispmodal">
        <div class="apv-dlg-head">
          <span class="apv-dlg-title">{{ tt('dispatch.title') }}</span>
          <button class="apv-dlg-close" @click="dispatchDlg.open = false">×</button>
        </div>
        <div class="apv-disp-body">
          <!-- Issue #697：疑似重复（根因不一致）派发前给 owner 明确提示 -->
          <div
            v-if="dispatchDlg.finding && dispatchDlg.finding.dedup_level === 'suspected_duplicate'"
            class="apv-dup-warn"
          >
            <span class="apv-dup-ic">⚠️</span>
            <div>
              <b>{{ tt('findings.suspected_dup_badge') }}</b>
              <div class="apv-dup-note">{{ dispatchDlg.finding.dedup_note || '' }}</div>
            </div>
          </div>
          <div class="apv-fld">
            <label class="apv-lbl-s">{{ tt('dispatch.field_title') }}</label>
            <input v-model="dispatchDlg.title" type="text" class="apv-input" />
          </div>
          <div class="apv-fld">
            <label class="apv-lbl-s">{{ tt('dispatch.field_desc') }}</label>
            <textarea v-model="dispatchDlg.desc" rows="5" class="apv-input area"></textarea>
          </div>
          <div class="apv-fld">
            <label class="apv-lbl-s">{{ tt('dispatch.field_scene') }}</label>
            <select v-model="dispatchDlg.sceneId" class="apv-input">
              <option value="">{{ tt('dispatch.scene_smart') }}</option>
              <option
                v-for="s in dispatchScenes"
                :key="s.id || s.scene_id"
                :value="s.id || s.scene_id"
              >
                {{ s.name || s.id }}
              </option>
            </select>
          </div>
        </div>
        <div class="apv-dlg-foot">
          <span></span>
          <div class="apv-dlg-btns">
            <button class="bt bt-o" @click="dispatchDlg.open = false">{{ tc('cancel') }}</button>
            <button class="bt bt-p" :disabled="dispatchDlg.submitting" @click="confirmDispatch()">
              {{ dispatchDlg.submitting ? tt('dispatch.creating') : tt('dispatch.confirm') }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══ 执行日志弹窗（与事件详情页同一套 UI：ev__tl-*） ═══ -->
    <Teleport to="body">
      <div v-if="logDlg.open" class="ev__tl-overlay" @click.self="closeLog()">
        <div class="ev__tl-box" :class="{ 'ev__tl-box--fs': logDlg.fs }">
          <div class="ev__tl-hdr">
            <span class="ev__tl-title"
              >{{ tt('exec.log_title_realtime') }}
              <span class="ev__tl-sub">· {{ logDlg.title }}</span></span
            >
            <span class="ev__tl-status" v-html="logDlg.statusHtml"></span>
            <button class="ev__tl-btn" @click="copyLog()">{{ tt('exec.log_copy') }}</button>
            <button class="ev__tl-btn" @click="logDlg.fs = !logDlg.fs">
              {{ logDlg.fs ? tt('exec.log_fs_exit') : tt('exec.log_fs') }}
            </button>
            <button class="ev__tl-btn" @click="closeLog()">{{ tt('exec.log_close') }}</button>
          </div>
          <div class="ev__tl-info" v-html="logDlg.infoHtml"></div>
          <div ref="logBody" class="ev__tl-body">
            <div v-if="logDlg.loading" class="apv-muted">{{ tt('exec.log_loading') }}</div>
            <div v-else-if="!logDlg.html && !logDlg.lines.length" class="apv-muted">
              {{ tt('exec.log_empty') }}
            </div>
            <div v-else v-html="logDlg.html || escapedPlainLog"></div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ═══ 行 kebab 菜单 ═══ -->
    <Teleport to="body">
      <div
        v-if="rowMenu.open"
        class="apv-rowmenu"
        :style="{ top: rowMenu.y + 'px', left: rowMenu.x + 'px' }"
      >
        <div @click="triggerFromMenu()">{{ tt('list.menu_trigger') }}</div>
        <div @click="toggleFromMenu()">
          {{ menuTaskEnabled ? tt('list.menu_pause') : tt('list.menu_enable') }}
        </div>
        <div @click="editFromMenu()">{{ tt('list.menu_edit') }}</div>
        <div class="apv-rowmenu-del" @click="deleteFromMenu()">{{ tt('list.menu_delete') }}</div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { providerFromRepoKey, repoKeyOf } from '../lib/repo-provider.js'
import { stripRepoPrefix } from '../lib/repo-key.js'
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'

const G = globalThis

// ═══ 模板（与老 _AP_TEMPLATES 等价，instruction 原文不翻译） ═══
const TEMPLATES = [
  {
    id: 'issue_patrol',
    icon: '📋',
    title_key: 'tpl_issue_patrol_t',
    desc_key: 'tpl_issue_patrol_d',
    cron: '0 9 * * *',
    instruction:
      '1. 列出所有打开的 Issue\n2. 找出超过 7 天无更新的 stale issue\n3. 检查是否有阻塞的 Issue 及其阻塞原因\n4. 汇总巡检报告，包含：stale 数量、阻塞数量、建议关闭列表',
  },
  {
    id: 'pr_review',
    icon: '🔀',
    title_key: 'tpl_pr_review_t',
    desc_key: 'tpl_pr_review_d',
    cron: '0 10 * * 1-5',
    instruction:
      '1. 列出所有打开的 PR\n2. 找出超过 24 小时未审查的 PR\n3. 为每个标记作者和摘要\n4. 提醒团队审查',
  },
  {
    id: 'bug_triage',
    icon: '🐛',
    title_key: 'tpl_bug_triage_t',
    desc_key: 'tpl_bug_triage_d',
    cron: '0 9 * * 1-5',
    instruction:
      '1. 列出所有未优先排序的 backlog Issue\n2. 评估每个的严重程度\n3. 设置优先级标签\n4. 添加评估说明',
  },
  {
    id: 'weekly_report',
    icon: '📊',
    title_key: 'tpl_weekly_report_t',
    desc_key: 'tpl_weekly_report_d',
    cron: '0 17 * * 1',
    instruction:
      '1. 收集过去 7 天完成的 Issue\n2. 收集进行中的 Issue\n3. 识别阻塞的 Issue\n4. 编写结构化周报',
  },
  {
    id: 'dep_audit',
    icon: '🛡',
    title_key: 'tpl_dep_audit_t',
    desc_key: 'tpl_dep_audit_d',
    cron: '0 8 * * 1',
    instruction: '1. 运行依赖审计工具\n2. 识别有安全漏洞的包\n3. 列出过期的依赖\n4. 输出修复建议',
  },
  {
    id: 'doc_check',
    icon: '🔍',
    title_key: 'tpl_doc_check_t',
    desc_key: 'tpl_doc_check_d',
    cron: '0 14 * * 1',
    instruction:
      '1. 列出过去 7 天的代码变更\n2. 检查相关文档是否更新\n3. 识别缺失文档的新功能\n4. 输出文档补全清单',
  },
]
const DAY_KEYS = ['day_sun', 'day_mon', 'day_tue', 'day_wed', 'day_thu', 'day_fri', 'day_sat']
const SCOPE_ORDER = ['user', 'repo', 'global', 'local']

// ═══ props / 状态 ═══
const props = defineProps({ initialDetailId: { type: [String, Number], default: null } })
const tasks = ref([])
const loaded = ref(false)
const loadError = ref('')
const scope = ref('all')
const sortField = ref('name')
const sortDir = ref('asc')
const search = ref('')
const selected = reactive(new Set())
// 详情页状态
const detailId = ref(null)
const detail = ref(null)
const detailError = ref('')
const subscribed = ref(false)
const executions = ref([])
const execLoading = ref(false)
const execError = ref('')
const openExec = reactive(new Set())
const findingsMap = ref({})
const findingsLoadingFor = ref(null)
const detailWbOn = ref(false)
const detailWbBranch = ref('')
const detailBranches = ref([])
detailBranches.value._isDetail = true
const detailCustomFields = ref([])
let logPollTimers = {}
// 弹窗状态
const dlg = reactive({
  open: false,
  editingId: null,
  name: '',
  instruction: '',
  maxRetry: 3,
  workBranch: '',
  wbOn: false,
  expanded: false,
  polishing: false,
  submitting: false,
  repoKey: '', // Issue #486：编辑态原任务仓库（新建时空 = 取当前仓库）
})
const dlgCustomFields = ref([])
const dlgBranches = ref([])
const selectedEngines = reactive(new Set())
const engines = ref([])
// Issue #486：引擎列表按表单所属仓库过滤并缓存，切仓需重拉（避免跨仓串缓存）
let enginesRepoKey = ''
const enginesLoaded = ref(false)
const selectedSkill = ref('')
const autoSkills = ref([])
const engPicker = reactive({ open: false, q: '', type: '', scope: '' })
const skillPicker = reactive({ open: false, q: '' })
const dispatchDlg = reactive({
  open: false,
  execId: null,
  finding: null,
  title: '',
  desc: '',
  sceneId: '',
  submitting: false,
})
const dispatchScenes = ref([])
const rowMenu = reactive({ open: false, id: null, x: 0, y: 0 })

// ═══ 工具 ═══
const API = () => G.API
const app = () => G.app
// 后端 dispatch_state_reason 只下发稳定 code（D1：不下发用户可见文案）。
// 已知 code → 语言包 key；未知/自由文本 → 回落该位置默认文案，避免直出非当前语言。
const _DISPATCH_REASON_I18N = {
  blocked_empty_instruction: { list: 'list.not_dispatchable',
                               detail: 'detail.not_dispatchable_desc' },
}

function dispatchReasonText(code, where) {
  const mapped = _DISPATCH_REASON_I18N[code] && _DISPATCH_REASON_I18N[code][where]
  return tt(mapped || (where === 'list' ? 'list.not_dispatchable'
                                        : 'detail.not_dispatchable_desc'))
}

function tt(key, vars) {
  return G.I18N.t('autopilot.' + key, vars)
}
function tc(key) {
  const map = { loading: '加载中...', cancel: '取消', confirm: '确认' }
  const v = G.I18N.t('common.' + key)
  return v && v !== 'common.' + key ? v : map[key] || key
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}
function pad(n) {
  return String(n).padStart(2, '0')
}
function relativeTime(utcStr) {
  if (!utcStr) return '—'
  const ts = Date.parse(utcStr)
  if (!ts) return utcStr
  const diff = Date.now() - ts
  if (diff < 60000) return 'just now'
  if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago'
  if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago'
  if (diff < 604800000) return Math.floor(diff / 86400000) + 'd ago'
  return Math.floor(diff / 604800000) + 'w ago'
}
function formatTime(utcStr) {
  if (!utcStr) return '—'
  const d = new Date(utcStr)
  if (isNaN(d)) return utcStr
  return d.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
function currentRepo() {
  return app().currentRepo ? app().currentRepo() : null
}
// 自定义字段名校验（复用 app.js 全局 _cfNameError / _cfComputeIsSecret）
function nameError(name) {
  if (!name) return ''
  const fn = G.window._cfNameError
  return fn ? fn(name) : ''
}
function computeIsSecret(name, isSecret) {
  const fn = G.window._cfComputeIsSecret
  return fn ? fn(name, isSecret) : !!isSecret
}
function collectFields(fields) {
  for (const f of fields) {
    const err = nameError(f.name)
    if (err) return { error: err }
  }
  return {
    fields: fields
      .filter((f) => (f.name || '').trim())
      .map((f) => ({
        name: f.name.trim(),
        value: (f.value || '').trim(),
        is_secret: computeIsSecret(f.name, f.show === false),
        scope: ['all'],
      })),
  }
}

// ═══ 列表逻辑 ═══
function loadTasks() {
  const repo = currentRepo()
  if (!repo) {
    tasks.value = []
    loaded.value = true
    return
  }
  const repoKey = repo.repoKey || repoKeyOf({ full_name: repo.fullName || '', provider: repo.provider })
  API()
    .get('/autopilot/tasks?repo_key=' + repoKey)
    .then(function (d) {
      if (G.window._currentView !== 'autopilot') return
      tasks.value = d.tasks || []
      loaded.value = true
      loadError.value = ''
    })
    .catch(function (e) {
      if (e.name === 'AbortError' && e.silent) return
      if (G.window._currentView !== 'autopilot') return
      loadError.value = tt('list.load_failed', { msg: e.message })
      loaded.value = true
    })
}
const filtered = computed(() => {
  let list = tasks.value.slice()
  if (scope.value === 'enabled') list = list.filter((t) => t.enabled)
  else if (scope.value === 'disabled') list = list.filter((t) => !t.enabled)
  if (search.value) {
    const q = search.value.toLowerCase()
    list = list.filter((t) => (t.name || '').toLowerCase().indexOf(q) >= 0)
  }
  const dir = sortDir.value === 'asc' ? 1 : -1
  list.sort(function (a, b) {
    if (sortField.value === 'name') return (a.name || '').localeCompare(b.name || '') * dir
    if (sortField.value === 'nextRun') {
      const av = a.next_run_at ? Date.parse(a.next_run_at) : 0
      const bv = b.next_run_at ? Date.parse(b.next_run_at) : 0
      return (av - bv) * dir
    }
    if (sortField.value === 'created')
      return (Date.parse(a.created_at) - Date.parse(b.created_at)) * dir
    const al = a.last_run_at ? Date.parse(a.last_run_at) : 0
    const bl = b.last_run_at ? Date.parse(b.last_run_at) : 0
    return (al - bl) * dir
  })
  return list
})
function scopeCount(sc) {
  if (sc === 'enabled') return tasks.value.filter((t) => t.enabled).length
  if (sc === 'disabled') return tasks.value.filter((t) => !t.enabled).length
  return tasks.value.length
}
function sortBy(field) {
  if (sortField.value === field) sortDir.value = sortDir.value === 'asc' ? 'desc' : 'asc'
  else {
    sortField.value = field
    sortDir.value = 'asc'
  }
}
function toggleSelect(id) {
  if (selected.has(id)) selected.delete(id)
  else selected.add(id)
}
function lastColor(t) {
  return t.last_run_at
    ? t.last_run_status === 'success'
      ? '#28a745'
      : t.last_run_status === 'failed'
        ? '#dc3545'
        : 'var(--t3)'
    : 'var(--t3)'
}
// kebab 菜单
function toggleRowMenu(id, e) {
  if (rowMenu.open && rowMenu.id === id) {
    rowMenu.open = false
    return
  }
  const rect = e.currentTarget.getBoundingClientRect()
  rowMenu.open = true
  rowMenu.id = id
  rowMenu.y = rect.bottom + 2
  rowMenu.x = rect.right - 140
}
const menuTask = computed(() => tasks.value.find((t) => t.id === rowMenu.id) || null)
const menuTaskEnabled = computed(() => (menuTask.value ? menuTask.value.enabled : false))
function closeRowMenu() {
  rowMenu.open = false
  rowMenu.id = null
}
function triggerFromMenu() {
  const id = rowMenu.id
  closeRowMenu()
  trigger(id)
}
function toggleFromMenu() {
  const t = menuTask.value
  closeRowMenu()
  if (t) toggleTask(t.id)
}
function editFromMenu() {
  const id = rowMenu.id
  closeRowMenu()
  editTask(id)
}
function deleteFromMenu() {
  const id = rowMenu.id
  closeRowMenu()
  deleteTask(id)
}
// 批量
function batchToggle(enabled) {
  selected.forEach(function (id) {
    API()
      .put('/autopilot/tasks/' + id, { enabled: !!enabled })
      .catch(function () {})
  })
  setTimeout(loadTasks, 300)
}
function batchDelete() {
  app().showConfirm(
    tt('list.confirm_delete_selected', { count: selected.size }),
    function () {
      Array.from(selected).forEach(function (id) {
        API()
          .del('/autopilot/tasks/' + id)
          .catch(function () {})
      })
      selected.clear()
      setTimeout(loadTasks, 300)
    },
    { danger: true }
  )
}
// 单个操作
function toggleTask(id) {
  const t = tasks.value.find(function (x) {
    return x.id === id
  })
  const enabled = t ? !t.enabled : false
  API()
    .put('/autopilot/tasks/' + id, { enabled: enabled })
    .then(function () {
      loadTasks()
    })
    .catch(function (e) {
      app().showToast(tt('op_failed', { msg: e.message || e }))
    })
}
function trigger(id) {
  API()
    .post('/autopilot/tasks/' + id + '/trigger')
    .then(function (d) {
      app().showToast(d.message || tt('trigger.done'))
      loadTasks()
      if (G.window._currentView === 'autopilot-detail' && String(detailId.value) === String(id)) {
        setTimeout(function () {
          loadExecutions(id)
        }, 2000)
      }
    })
    .catch(function (e) {
      app().showToast(tt('trigger.failed', { msg: e.message || e }))
    })
}
function deleteTask(id) {
  app().showConfirm(
    tt('list.confirm_delete_one'),
    function () {
      API()
        .del('/autopilot/tasks/' + id)
        .then(function () {
          app().showToast(tt('deleted'))
          loadTasks()
        })
        .catch(function (e) {
          app().showToast(tt('delete_failed', { msg: e.message || e }))
        })
    },
    { danger: true }
  )
}
function goDetail(id) {
  app().nav('autopilot-detail', String(id))
}

// ═══ Schedule 结构化模型（与老 _apParseCron/_apToCron 等价） ═══
const sched = reactive({
  advanced: false,
  rawCron: '',
  timeKind: 'at', // 'at' | 'every'
  timeAt: '09:00',
  everyInterval: 30,
  everyUnit: 'minutes',
  everyMinute: 0,
  winFrom: '',
  winTo: '',
  dayKind: 'every', // 'every' | 'weekly' | 'monthly'
  weeklyDays: [1],
  monthlyDay: 1,
})
const cron = ref('0 9 * * *')
function expandDow(dow) {
  const days = []
  if (dow === '*') return [0, 1, 2, 3, 4, 5, 6]
  dow.split(',').forEach(function (part) {
    if (part.indexOf('-') >= 0) {
      const r = part.split('-')
      for (let d = parseInt(r[0]); d <= parseInt(r[1]); d++) days.push(d)
    } else {
      days.push(parseInt(part))
    }
  })
  return days.length > 0 ? days : [1]
}
function dowFromDays(days) {
  const sorted = days.slice().sort(function (a, b) {
    return a - b
  })
  const parts = []
  let i = 0
  while (i < sorted.length) {
    let j = i
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j++
    parts.push(j > i ? sorted[i] + '-' + sorted[j] : String(sorted[i]))
    i = j + 1
  }
  return parts.join(',')
}
function parseCron(c) {
  const parts = String(c || '')
    .trim()
    .split(/\s+/)
  if (parts.length !== 5) {
    sched.advanced = true
    return
  }
  const m = parts[0]
  const h = parts[1]
  const dom = parts[2]
  const mon = parts[3]
  const dow = parts[4]
  if (mon !== '*') {
    sched.advanced = true
    return
  }
  sched.advanced = false
  if (dom !== '*' && dow === '*') {
    sched.dayKind = 'monthly'
    sched.monthlyDay = parseInt(dom) || 1
  } else if (dom === '*' && dow !== '*') {
    sched.dayKind = 'weekly'
    sched.weeklyDays = expandDow(dow)
  } else {
    sched.dayKind = 'every'
  }
  const mi = parseInt(m) || 0
  const hi = parseInt(h) || 0
  if (m.indexOf('*/') === 0) {
    sched.timeKind = 'every'
    sched.everyUnit = 'minutes'
    sched.everyInterval = parseInt(m.slice(2)) || 30
    sched.everyMinute = 0
    if (h !== '*' && h.indexOf(',') >= 0) {
      const hs = h.split(',')
      sched.winFrom = pad(parseInt(hs[0])) + ':00'
      sched.winTo = pad(parseInt(hs[hs.length - 1])) + ':59'
    } else {
      sched.winFrom = ''
      sched.winTo = ''
    }
  } else if (h.indexOf('*/') === 0) {
    sched.timeKind = 'every'
    sched.everyUnit = 'hours'
    sched.everyInterval = parseInt(h.slice(2)) || 1
    sched.everyMinute = mi
    sched.winFrom = ''
    sched.winTo = ''
  } else if (h.indexOf(',') >= 0 && mi === 0) {
    sched.timeKind = 'every'
    sched.everyUnit = 'hours'
    sched.everyInterval = 1
    sched.everyMinute = 0
    const hs2 = h.split(',')
    sched.winFrom = pad(parseInt(hs2[0])) + ':00'
    sched.winTo = pad(parseInt(hs2[hs2.length - 1])) + ':59'
  } else if (h.indexOf('-') >= 0) {
    const range = h.split('-')
    const step = 1
    let hi2 = range[1]
    if (hi2.indexOf('/') >= 0) {
      hi2 = hi2.split('/')[0]
    }
    sched.timeKind = 'every'
    sched.everyUnit = 'hours'
    sched.everyInterval = parseInt(h.split('/')[1] || step) || 1
    sched.everyMinute = mi
    sched.winFrom = pad(parseInt(range[0])) + ':' + pad(mi)
    sched.winTo = pad(parseInt(hi2)) + ':' + pad(mi)
  } else {
    sched.timeKind = 'at'
    sched.timeAt = pad(hi) + ':' + pad(mi)
  }
}
function toCron() {
  if (sched.advanced) return cron.value
  let m
  let h
  let dom = '*'
  let dow = '*'
  if (sched.dayKind === 'monthly') {
    dom = String(sched.monthlyDay)
  } else if (sched.dayKind === 'weekly') {
    if (sched.weeklyDays.length === 0 || sched.weeklyDays.length === 7) {
      dow = '*'
    } else {
      dow = dowFromDays(sched.weeklyDays)
    }
  }
  if (sched.timeKind === 'at') {
    const parts = String(sched.timeAt || '09:00').split(':')
    m = String(parseInt(parts[1]) || 0)
    h = String(parseInt(parts[0]) || 0)
  } else if (sched.everyUnit === 'minutes') {
    m = '*/' + sched.everyInterval
    if (sched.winFrom && sched.winTo) {
      const fh = parseInt(sched.winFrom.split(':')[0])
      const th = parseInt(sched.winTo.split(':')[0])
      const hours = []
      for (let hh = fh; hh <= th; hh++) hours.push(hh)
      h = hours.join(',')
    } else {
      h = '*'
    }
  } else {
    m = String(sched.everyMinute)
    if (sched.winFrom && sched.winTo) {
      const wfh = parseInt(sched.winFrom.split(':')[0])
      const wth = parseInt(sched.winTo.split(':')[0])
      h = sched.everyInterval === 1 ? wfh + '-' + wth : wfh + '-' + wth + '/' + sched.everyInterval
    } else {
      h = sched.everyInterval === 1 ? '*' : '*/' + sched.everyInterval
    }
  }
  return m + ' ' + h + ' ' + dom + ' * ' + dow
}
function syncCron() {
  cron.value = toCron()
}
function setTimeKind(kind) {
  sched.timeKind = kind
  syncCron()
}
function onUnitChange() {
  sched.everyInterval = Math.min(sched.everyInterval, sched.everyUnit === 'hours' ? 23 : 59)
  syncCron()
}
function onWinFrom() {
  if (sched.winFrom && sched.winTo) {
    const fh = parseInt(sched.winFrom.split(':')[0])
    const th = parseInt(sched.winTo.split(':')[0])
    if (th < fh) sched.winTo = sched.winFrom
  }
  if (sched.winFrom === '00:00' && (sched.winTo === '23:59' || sched.winTo === '24:00')) {
    sched.winFrom = ''
    sched.winTo = ''
  }
  syncCron()
}
function onWinTo() {
  if (sched.winFrom && sched.winTo) {
    const fh = parseInt(sched.winFrom.split(':')[0])
    const th = parseInt(sched.winTo.split(':')[0])
    if (th < fh) sched.winFrom = sched.winTo
  }
  if (sched.winFrom === '00:00' && (sched.winTo === '23:59' || sched.winTo === '24:00')) {
    sched.winFrom = ''
    sched.winTo = ''
  }
  syncCron()
}
function toggleDay(day) {
  const idx = sched.weeklyDays.indexOf(day)
  if (idx >= 0) {
    if (sched.weeklyDays.length > 1) sched.weeklyDays.splice(idx, 1)
  } else {
    sched.weeklyDays.push(day)
  }
  syncCron()
}
function enterAdvanced() {
  sched.advanced = true
  sched.rawCron = cron.value
}
function exitAdvanced() {
  sched.advanced = false
  parseCron(cron.value || '0 9 * * *')
  syncCron()
}
function onRawCron(e) {
  sched.rawCron = e.target.value
  cron.value = e.target.value.trim()
}
// 人类可读描述（与老 _apDescribeSchedule 等价）
const schedDesc = computed(() => {
  if (sched.advanced) return null
  let desc = ''
  if (sched.timeKind === 'at') {
    desc += sched.timeAt
  } else if (sched.everyUnit === 'minutes') {
    desc += tt('cron.every_minutes', { n: sched.everyInterval })
  } else {
    desc += tt('cron.every_hours', { n: sched.everyInterval })
    if (sched.everyMinute > 0)
      desc +=
        ' ' + tt('cron.every_minute_at', { n: sched.everyInterval, m: pad(sched.everyMinute) })
  }
  if (sched.timeKind === 'every' && sched.winFrom && sched.winTo) {
    desc += '（' + sched.winFrom + '-' + sched.winTo + '）'
  }
  desc += ' · '
  if (sched.dayKind === 'every') {
    desc += tt('cron.daily')
  } else if (sched.dayKind === 'weekly') {
    desc += tt('cron.weekly', {
      days: sched.weeklyDays
        .slice()
        .sort((a, b) => a - b)
        .map((d) => tt('cron.' + DAY_KEYS[d]))
        .join('、'),
    })
  } else {
    desc += tt('cron.monthly', { n: sched.monthlyDay })
  }
  return desc
})
// 未来 3 次运行（与老 _apComputeNextRuns 等价的暴力分钟步进）
const nextRuns = computed(() => {
  try {
    const parts = cron.value.trim().split(/\s+/)
    if (parts.length !== 5) return []
    const [m, h, dom, , dow] = parts
    let now = new Date()
    const results = []
    for (let iter = 0; iter < 20000 && results.length < 3; iter++) {
      now = new Date(now.getTime() + 60000)
      if (!cronMatch(m, now.getMinutes())) continue
      if (!cronMatch(h, now.getHours())) continue
      if (!cronMatch(dom, now.getDate())) continue
      if (!cronMatch(dow, now.getDay())) continue
      results.push(
        now.toLocaleString('zh-CN', {
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
        })
      )
    }
    return results
  } catch (e) {
    return []
  }
})
function cronMatch(pattern, value) {
  if (pattern === '*') return true
  if (pattern.indexOf('*/') === 0) {
    const step = parseInt(pattern.slice(2))
    return step > 0 && value % step === 0
  }
  if (pattern.indexOf(',') >= 0) {
    return pattern.split(',').some((p) => parseInt(p) === value)
  }
  if (pattern.indexOf('-') >= 0) {
    const r = pattern.split('-')
    let hi2 = r[1]
    if (hi2.indexOf('/') >= 0) hi2 = hi2.split('/')[0]
    return value >= parseInt(r[0]) && value <= parseInt(hi2)
  }
  return parseInt(pattern) === value
}
// 详情页静态描述（与老 _apDescribeCron 等价）
function dayName(d) {
  return tt('cron.' + DAY_KEYS[d])
}
function describeCron(c) {
  const parts = String(c || '')
    .trim()
    .split(/\s+/)
  if (parts.length !== 5) return c
  const m = parts[0]
  const h = parts[1]
  const dom = parts[2]
  const mon = parts[3]
  const dow = parts[4]
  const mi = parseInt(m) || 0
  const hi = parseInt(h) || 0
  const time = pad(hi) + ':' + pad(mi)
  if (m.indexOf('*/') === 0 && h.indexOf(',') >= 0) {
    const hs = h.split(',')
    return tt('cron.daily_window_minutes', {
      from: pad(parseInt(hs[0])),
      to: parseInt(hs[hs.length - 1]) + 1,
      n: m.slice(2),
    })
  }
  if (m.indexOf('*/') === 0) return tt('cron.every_n_minutes_exec', { n: m.slice(2) })
  if (h.indexOf('*/') === 0 && mi === 0) return tt('cron.every_n_hours_exec', { n: h.slice(2) })
  if (mi === 0 && h.indexOf(',') >= 0) {
    const hs2 = h.split(',')
    return tt('cron.daily_window_hours', {
      from: pad(parseInt(hs2[0])),
      to: parseInt(hs2[hs2.length - 1]) + 1,
    })
  }
  if (dom === '*' && mon === '*' && dow === '*') return tt('cron.daily_at', { time: time })
  if (dom === '*' && mon === '*' && dow !== '*') {
    if (dow === '1-5') return tt('cron.workdays_at', { time: time })
    const days = dow.split(',').map((d) => dayName(parseInt(d)) || d)
    return tt('cron.weekly_at', { days: days.join('、'), time: time })
  }
  if (dom !== '*' && mon === '*' && dow === '*')
    return tt('cron.monthly_at', { n: parseInt(dom), time: time })
  return c
}

// ═══ 详情页 ═══
const engineNamesText = computed(() => {
  const t = detail.value
  if (!t) return ''
  // Issue #1011：展示 uid 优先，旧数据（uids 空）回落 ids 不报错
  const engCount = t.engine_runtime_uids && t.engine_runtime_uids.length
    ? t.engine_runtime_uids.length
    : (t.engine_runtime_ids || []).length
  if (engCount) {
    return tt('detail.engine_count', { count: engCount })
  }
  return tt('detail.engine_unlimited')
})
const engineDetachedHtml = computed(() => {
  const t = detail.value
  if (!t || !t.engine_detached || !t.engine_detached.engines || !t.engine_detached.engines.length)
    return ''
  return (
    '<div class="apv-detach">' +
    (tt('detail.engine_detached_hint') || '绑定的引擎 ') +
    '<b>' +
    esc(t.engine_detached.engines.join('、')) +
    '</b>' +
    (tt('detail.engine_detached_rebind') ||
      ' 已离线（超 8 小时）被系统从场景移除，任务绑定仍保留，请自行调整或重新绑定。') +
    '</div>'
  )
})
function loadDetail(id) {
  detailId.value = id
  detail.value = null
  detailError.value = ''
  API()
    .get('/autopilot/tasks/' + id)
    .then(function (t) {
      if (G.window._currentView !== 'autopilot-detail') return
      detail.value = t
      detailWbOn.value = !!t.work_branch
      detailWbBranch.value = t.work_branch || ''
      detailCustomFields.value = (t.custom_fields || []).map((f) => ({
        name: f.name || '',
        value: f.value || '',
        show: !computeIsSecret(f.name, f.is_secret),
      }))
      loadExecutions(id)
      loadBranches(t.repo_key, detailBranches)
      API()
        .get('/autopilot/tasks/' + id + '/subscription')
        .then(function (d) {
          subscribed.value = !!d.subscribed
        })
        .catch(function () {})
    })
    .catch(function (e) {
      detailError.value = tt('detail.load_failed', { msg: e.message || e })
    })
}
function loadBranches(repoKeyRaw, target) {
  const repoKey = repoKeyRaw || ''
  if (!repoKey) return
  const rk = stripRepoPrefix(repoKey)
  const parts = rk.split('/')
  if (parts.length !== 2) return
  API()
    .get('/scenes/branches/' + parts[0] + '/' + parts[1] + '?repo_key=' + encodeURIComponent(rk))
    .then(function (b) {
      const branches = (b && b.branches) || []
      target.value = branches
      // 默认选第一项（若当前值为空）
      if (!detailWbBranch.value && target._isDetail && branches.length) {
        detailWbBranch.value = branches[0].name
      }
      if (!dlg.workBranch && !target._isDetail && branches.length) {
        dlg.workBranch = branches[0].name
      }
    })
    .catch(function () {})
}
function toggleEnabled(id, enabled) {
  API()
    .put('/autopilot/tasks/' + id, { enabled: !!enabled })
    .then(function () {
      app().showToast(enabled ? tt('toggle.enabled') : tt('toggle.paused'))
      if (detail.value && detail.value.id === id) detail.value.enabled = enabled
    })
    .catch(function (e) {
      app().showToast(tt('toggle.failed', { msg: e.message || e }))
    })
}
function toggleSubscribe(taskId) {
  const target = !subscribed.value
  const done = function () {
    subscribed.value = target
    app().showToast(target ? tt('detail.subscribe_on') : tt('detail.subscribe_off'))
  }
  if (subscribed.value) {
    API()
      .del('/autopilot/tasks/' + taskId + '/subscribe')
      .then(done)
      .catch((e) => app().showToast(e.message || e))
  } else {
    API()
      .post('/autopilot/tasks/' + taskId + '/subscribe', {})
      .then(done)
      .catch((e) => app().showToast(e.message || e))
  }
}
// ═══ 执行历史 ═══
const EXEC_ICONS = {
  success: '✅',
  failed: '❌',
  running: '🔄',
  pending: '⏳',
  retrying: '🔁',
  skipped: '⏭',
  dispatched: '📤',
  cancelled: '🚫',
}
const EXEC_COLORS = {
  success: '#28a745',
  failed: '#dc3545',
  running: 'var(--a-text)',
  pending: 'var(--t3)',
  retrying: 'var(--c-warning)',
  skipped: 'var(--t3)',
  dispatched: 'var(--a-text)',
  cancelled: 'var(--c-warning)',
}
function execIcon(st) {
  return EXEC_ICONS[st] || '•'
}
function execColor(st) {
  return EXEC_COLORS[st] || 'var(--t3)'
}
function execResult(e) {
  return e.error ? String(e.error).substring(0, 100) : String(e.result || '').substring(0, 100)
}
function loadExecutions(taskId) {
  execLoading.value = true
  execError.value = ''
  API()
    .get('/autopilot/executions?task_id=' + taskId + '&limit=50')
    .then(function (d) {
      execLoading.value = false
      executions.value = d.executions || []
    })
    .catch(function (e) {
      execLoading.value = false
      execError.value = tt('exec.load_failed', { msg: e.message || e })
    })
}
function toggleExecDetail(e) {
  if (openExec.has(e.id)) {
    openExec.delete(e.id)
    stopLogPoll(e.id)
  } else {
    openExec.add(e.id)
    // findings 懒加载
    if (!(e.id in findingsMap.value)) {
      findingsLoadingFor.value = e.id
      API()
        .get('/autopilot/executions/' + e.id + '/findings')
        .then(function (d) {
          findingsMap.value = Object.assign({}, findingsMap.value, { [e.id]: d.findings || [] })
        })
        .catch(function () {
          findingsMap.value = Object.assign({}, findingsMap.value, { [e.id]: [] })
        })
        .finally(function () {
          findingsLoadingFor.value = null
        })
    }
    // 进行中任务 → 日志轮询
    if ((e.status === 'running' || e.status === 'dispatched') && e.agent_task_id) {
      startLogPoll(e)
    }
  }
}
// ═══ 执行日志弹窗（与事件详情页 EventView 的 ev__tl-* 完全同一套视觉/交互） ═══
const logDlg = reactive({
  open: false,
  taskId: '',
  execId: null,
  title: '',
  status: '',
  statusHtml: '',
  infoHtml: '',
  html: '',
  lines: [],
  loading: false,
  fs: false,
})
const escapedPlainLog = computed(function () {
  return logDlg.lines
    .map(function (l) {
      return '<div style="margin-bottom:8px">' + escHtml(l) + '</div>'
    })
    .join('')
})
let logTimer = null
let logSince = 0
let logInfoLoaded = false

function escHtml(v) {
  return String(v == null ? '' : v)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}
function logTimeHtml(t) {
  if (!t) return ''
  const str = String(t)
  const m = str.indexOf('T') >= 0 ? str.split('T')[1] : str
  return '<span style="color:#7a85a0;margin-right:6px">' + escHtml(m.substring(0, 8)) + '</span>'
}
// 与 EventView.tlLine 同款：图标/配色/工具输入块
function logMsgHtml(m) {
  const type = m.type || ''
  if (type === 'tool_use') {
    let inputHtml = ''
    try {
      inputHtml =
        '<div style="margin:4px 0 4px 24px;white-space:pre-wrap;word-break:break-all;color:#b8c2d6;font-size:13px">' +
        escHtml(JSON.stringify(m.input || {}, null, 2)) +
        '</div>'
    } catch (e) {
      /* ignore */
    }
    return (
      '<div style="margin-bottom:8px">' +
      logTimeHtml(m.time) +
      '🔧 <span style="color:#5b9eff;font-weight:600">' +
      escHtml(m.tool || 'tool') +
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
    logTimeHtml(m.time) +
    icon +
    ' <span style="color:' +
    color +
    ';white-space:pre-wrap;word-break:break-all">' +
    escHtml(m.content || m.tool || '') +
    '</span></div>'
  )
}
function logStatusHtml(st) {
  const color =
    st === 'success'
      ? '#2ecc71'
      : st === 'failed'
        ? '#ff5b5b'
        : st === 'running' || st === 'dispatched'
          ? '#5b9eff'
          : '#8a94a6'
  return '<span style="color:' + color + '">●</span> ' + escHtml(st || '')
}
function logScrollToEnd() {
  setTimeout(function () {
    const el = document.querySelector('.ev__tl-body')
    if (el) el.scrollTop = el.scrollHeight
  }, 0)
}
function logLoadInfo(tid) {
  API()
    .get('/task-logs/' + encodeURIComponent(tid) + '/info')
    .then(function (info) {
      if (!info || !logDlg.open || logDlg.taskId !== tid) return
      const bits = []
      if (info.agent_name) {
        bits.push(
          '🤖 ' +
            escHtml(info.agent_name) +
            (info.agent_version
              ? ' <span style="color:var(--t3)">v' + escHtml(info.agent_version) + '</span>'
              : '')
        )
      }
      if (info.device_name) bits.push('🖥 ' + escHtml(info.device_name))
      if (info.last_ip) bits.push('🌐 ' + escHtml(info.last_ip))
      if (info.owner_name) bits.push('👤 ' + escHtml(info.owner_name))
      if (info.spec) bits.push('⚙️ ' + escHtml(info.spec))
      if (info.model) bits.push('🧠 ' + escHtml(info.model))
      if (info.cpu_percent) bits.push('📊 CPU ' + Number(info.cpu_percent).toFixed(1) + '%')
      if (info.memory_percent) bits.push('💾 MEM ' + Number(info.memory_percent).toFixed(1) + '%')
      logDlg.infoHtml = bits
        .map(function (b) {
          return '<span>' + b + '</span>'
        })
        .join('')
      if (info.status) {
        logDlg.status = info.status
        logDlg.statusHtml = logStatusHtml(info.status)
      }
      logInfoLoaded = true
    })
    .catch(function () {
      /* 静默降级，与 EventView 一致 */
    })
}
function pollLog() {
  if (!logDlg.open || !logDlg.taskId) return
  const tid = logDlg.taskId
  API()
    .get('/task-logs/' + encodeURIComponent(tid) + '/messages?since=' + logSince)
    .then(function (d) {
      if (!logDlg.open || logDlg.taskId !== tid) return
      const msgs = Array.isArray(d) ? d : ((d && d.messages) || [])
      logDlg.loading = false
      if (msgs.length) {
        logSince = msgs[msgs.length - 1].seq
        let add = ''
        msgs.forEach(function (m) {
          const line = logMsgHtml(m)
          if (line) add += line
        })
        if (add) {
          logDlg.html += add
          logScrollToEnd()
        }
      }
    })
    .catch(function (e) {
      logDlg.loading = false
      if (!logDlg.html) {
        logDlg.lines.push((e && e.message) || String(e))
      }
    })
  API()
    .get('/runtime/tasks/' + encodeURIComponent(tid) + '/status')
    .then(function (d) {
      if (!logDlg.open || logDlg.taskId !== tid) return
      if (d && d.status) {
        logDlg.status = d.status
        logDlg.statusHtml = logStatusHtml(d.status)
        if (d.status !== 'running' && d.status !== 'dispatched') {
          stopLogTimer()
          if (!logInfoLoaded) logLoadInfo(tid)
        }
      }
    })
    .catch(function () {})
}
function stopLogTimer() {
  if (logTimer) {
    clearInterval(logTimer)
    logTimer = null
  }
}
// 日志按钮：页面内弹窗（不依赖 event-view 的 window.openTaskLog）
function openTaskLog(e) {
  if (!e || !e.agent_task_id) return
  closeLog()
  logDlg.open = true
  logDlg.taskId = e.agent_task_id
  logDlg.execId = e.id
  logDlg.title = tt('exec.log_title', { id: e.id })
  logDlg.status = e.status || ''
  logDlg.statusHtml = logStatusHtml(e.status || '')
  logDlg.html = ''
  logDlg.lines = []
  logDlg.infoHtml = ''
  logDlg.loading = true
  logDlg.fs = false
  logSince = 0
  logInfoLoaded = false
  logLoadInfo(e.agent_task_id)
  pollLog()
  logTimer = setInterval(pollLog, 2000)
}
function closeLog() {
  logDlg.open = false
  logDlg.taskId = ''
  logDlg.html = ''
  logDlg.lines = []
  logDlg.infoHtml = ''
  logDlg.loading = false
  logDlg.fs = false
  logSince = 0
  stopLogTimer()
}
function copyLog() {
  const box = document.querySelector('.ev__tl-body')
  const text = box ? box.innerText : ''
  if (!text) return
  const nav = G.window.navigator
  if (nav && nav.clipboard && nav.clipboard.writeText) {
    nav.clipboard.writeText(text).then(
      function () {
        app().showToast(tt('exec.log_copied'))
      },
      function () {}
    )
    return
  }
  const ta = document.createElement('textarea')
  ta.value = text
  document.body.appendChild(ta)
  ta.select()
  try {
    document.execCommand('copy')
    app().showToast(tt('exec.log_copied'))
  } catch (err) {}
  ta.remove()
}
function startLogPoll(e) {
  if (logPollTimers[e.id]) clearInterval(logPollTimers[e.id])
  let since = 0
  logPollTimers[e.id] = setInterval(function () {
    API()
      .get('/task-logs/' + e.agent_task_id + '/messages?since=' + since)
      .then(function (d) {
        const msgs = Array.isArray(d) ? d : ((d && d.messages) || [])
        if (!msgs.length) return
        since = msgs[msgs.length - 1].seq
        const lines = msgs
          .map(function (m) {
            const ty = m.type || ''
            const content = m.content || m.tool || ''
            if (ty === 'text') return content.substring(0, 200)
            if (ty === 'step_start') return '▶ ' + content
            if (ty === 'step_finish') return '✓ ' + content
            if (ty === 'tool_use') return '🔧 ' + (m.tool || '')
            if (ty === 'error') return '❌ ' + content
            return ''
          })
          .filter(Boolean)
        if (lines.length) {
          // 追加到对应 pre（DOM 直更，等价老代码）
          const pres = document.querySelectorAll('.apv-exec-pre')
          pres.forEach(function (pre) {
            pre.textContent += lines.join('\n') + '\n'
            pre.scrollTop = pre.scrollHeight
          })
        }
      })
      .catch(function () {})
    // 检查执行是否结束
    API()
      .get('/autopilot/executions?task_id=' + detailId.value + '&limit=50')
      .then(function (d) {
        const cur = (d.executions || []).find(function (x) {
          return x.id === e.id
        })
        if (cur && cur.status !== 'running' && cur.status !== 'dispatched') {
          stopLogPoll(e.id)
          loadExecutions(detailId.value)
        }
      })
      .catch(function () {})
  }, 3000)
}
function stopLogPoll(execId) {
  if (logPollTimers[execId]) {
    clearInterval(logPollTimers[execId])
    delete logPollTimers[execId]
  }
}
function stopAllLogPolls() {
  Object.keys(logPollTimers).forEach(stopLogPoll)
}
// findings 徽标
function sevIcon(sev) {
  return { high: '🔴', medium: '🟡', low: '🔵' }[sev] || '•'
}
function sevColor(sev) {
  return { high: '#dc3545', medium: 'var(--c-warning)', low: 'var(--a-text)' }[sev] || 'var(--t3)'
}
// 高级选项保存
function saveAdvanced() {
  if (!detailId.value) return
  const wb = detailWbOn.value ? detailWbBranch.value || '' : ''
  const cf = collectFields(detailCustomFields.value)
  if (cf.error) {
    app().showToast(cf.error)
    return
  }
  API()
    .put('/autopilot/tasks/' + detailId.value, { work_branch: wb, custom_fields: cf.fields })
    .then(function () {
      app().showToast(tt('advanced.saved'))
    })
    .catch(function (e) {
      app().showToast(tt('advanced.save_failed', { msg: e.message || e }))
    })
}
function addDetailField() {
  detailCustomFields.value.push({ name: '', value: '', show: false })
}
// ═══ 派发问题 ═══
function dispatchFinding(execId, f) {
  dispatchDlg.open = true
  dispatchDlg.execId = execId
  dispatchDlg.finding = f
  dispatchDlg.title = f.title || ''
  dispatchDlg.desc = f.description || ''
  dispatchDlg.sceneId = ''
  dispatchDlg.submitting = false
  // 从详情 task 取 repo_key（详情页不在 currentRepo 上下文）
  const repo = stripRepoPrefix((detail.value && detail.value.repo_key) || '')
  API()
    .get('/scenes?repo=' + encodeURIComponent(repo))
    .then(function (d) {
      dispatchScenes.value = d.scenes || []
    })
    .catch(function (e) {
      app().showToast(tt('dispatch.scenes_load_failed', { msg: e.message || e }))
    })
}
function confirmDispatch() {
  if (!dispatchDlg.title.trim()) {
    app().showToast(tt('dispatch.title_required'))
    return
  }
  if (!dispatchDlg.desc.trim()) {
    app().showToast(tt('dispatch.desc_required'))
    return
  }
  dispatchDlg.submitting = true
  API()
    .post('/autopilot/executions/' + dispatchDlg.execId + '/dispatch', {
      finding_id: dispatchDlg.finding.id,
      title: dispatchDlg.title.trim(),
      description: dispatchDlg.desc.trim(),
      scene_id: dispatchDlg.sceneId,
    })
    .then(function (d) {
      app().showToast(tt('dispatch.created', { n: d.issue_number || '' }))
      dispatchDlg.open = false
      // 重载 findings
      delete findingsMap.value[dispatchDlg.execId]
      API()
        .get('/autopilot/executions/' + dispatchDlg.execId + '/findings')
        .then(function (dd) {
          findingsMap.value = Object.assign({}, findingsMap.value, {
            [dispatchDlg.execId]: dd.findings || [],
          })
        })
        .catch(function () {})
    })
    .catch(function (e) {
      dispatchDlg.submitting = false
      app().showToast(tt('dispatch.failed', { msg: e.message || e }))
    })
}

// ═══ 创建/编辑弹窗 ═══
function showCreate(templateId) {
  const tpl = templateId ? TEMPLATES.find((t) => t.id === templateId) : null
  dlg.open = true
  dlg.editingId = null
  dlg.repoKey = ''
  dlg.name = tpl ? tt(tpl.title_key) : ''
  dlg.instruction = tpl ? tpl.instruction : ''
  dlg.maxRetry = 3
  dlg.workBranch = ''
  dlg.wbOn = false
  dlg.expanded = false
  dlg.polishing = false
  dlg.submitting = false
  dlgCustomFields.value = []
  selectedSkill.value = ''
  cron.value = tpl ? tpl.cron : '0 9 * * *'
  parseCron(cron.value)
  syncCron()
  // 加载分支列表（当前仓库）
  const repo = currentRepo()
  if (repo) {
    // 前缀必须跟随仓库实际平台；原来先剥常见前缀再硬拼 'gitcode:'，gitee/gitlab 仓会去错平台拉分支
    const rkFull = repo.repoKey || repoKeyOf({ full_name: repo.fullName || '', provider: repo.provider })
    const prov = providerFromRepoKey(rkFull)
    const rk = rkFull.slice(rkFull.indexOf(':') + 1)
    if (prov && rk.split('/').length === 2) loadBranches(prov + ':' + rk, dlgBranches)
  }
}
function editTask(id) {
  dlg.editingId = id
  API()
    .get('/autopilot/tasks/' + id)
    .then(function (t) {
      dlg.open = true
      dlg.repoKey = t.repo_key || '' // Issue #486：引擎列表按该仓库过滤
      dlg.name = t.name || ''
      dlg.instruction = t.instruction || ''
      dlg.maxRetry = typeof t.max_retry === 'number' ? t.max_retry : 3
      dlg.workBranch = t.work_branch || ''
      dlg.wbOn = !!t.work_branch
      dlg.expanded = false
      dlg.polishing = false
      dlg.submitting = false
      selectedEngines.clear()
      // Issue #1011：引擎选择键 uid 优先——uid 模式任务直接用 uids；legacy 任务回落 ids
      const engineKeys = t.engine_runtime_uids && t.engine_runtime_uids.length
        ? t.engine_runtime_uids
        : t.engine_runtime_ids || []
      engineKeys.forEach((k) => selectedEngines.add(String(k)))
      selectedSkill.value = t.skill_name || ''
      dlgCustomFields.value = (t.custom_fields || []).map((f) => ({
        name: f.name || '',
        value: f.value || '',
        show: !computeIsSecret(f.name, f.is_secret),
      }))
      cron.value = t.cron || '0 9 * * *'
      parseCron(cron.value)
      syncCron()
      // 分支列表：用 task.repo_key
      loadBranches(t.repo_key, dlgBranches)
    })
    .catch(function (e) {
      app().showToast(tt('list.load_failed', { msg: e.message || e }))
    })
}
function addDlgField() {
  dlgCustomFields.value.push({ name: '', value: '', show: false })
}
// 润色指令（20s 超时）
function polishInstruction() {
  if (!dlg.instruction.trim()) {
    app().showToast(tt('form.polish_empty_instruction'))
    return
  }
  dlg.polishing = true
  API()
    .post('/autopilot/polish', { name: dlg.name, instruction: dlg.instruction }, 20000)
    .then(function (d) {
      if (d.instruction) {
        dlg.instruction = d.instruction
        app().showToast(tt('form.polish_done'))
      }
      dlg.polishing = false
    })
    .catch(function (e) {
      app().showToast(tt('form.polish_failed', { msg: e.message || e }))
      dlg.polishing = false
    })
}
// 提交
function submitForm() {
  const name = dlg.name.trim()
  const instruction = dlg.instruction.trim()
  let retry = parseInt(dlg.maxRetry, 10)
  if (isNaN(retry) || retry < 0 || retry > 10) retry = 3
  if (!name) {
    app().showToast(tt('form.name_required'))
    return
  }
  if (!instruction) {
    app().showToast(tt('form.instruction_required'))
    return
  }
  const cf = collectFields(dlgCustomFields.value)
  if (cf.error) {
    app().showToast(cf.error)
    return
  }
  const wb = dlg.wbOn ? dlg.workBranch || '' : ''
  const repo = currentRepo()
  const body = {
    name: name,
    repo_key: (repo && (repo.repoKey || repoKeyOf({ full_name: repo.fullName || '', provider: repo.provider }))) || '',
    instruction: instruction,
    skill_name: selectedSkill.value,
    cron: cron.value,
    max_retry: retry,
    // Issue #1011 姊妹字段升格：选中引擎全部有 uid → 提交 engine_runtime_uids（清空 ids）；
    // 存在无 uid 引擎（未迁移）→ 回落 legacy ids 提交（两字段不同时非空）
    engine_runtime_uids: [],
    engine_runtime_ids: [],
    custom_fields: cf.fields,
    work_branch: wb,
  }
  {
    const selKeys = Array.from(selectedEngines)
    const uidSel = []
    const idSel = []
    selKeys.forEach(function (k) {
      const e = engineByKey(k)
      if (e && e.engine_uid) uidSel.push(String(e.engine_uid))
      else if (/^\d+$/.test(String(k))) idSel.push(parseInt(k, 10))
      else if (e) idSel.push(e.id)
    })
    if (selKeys.length && uidSel.length === selKeys.length) {
      body.engine_runtime_uids = uidSel
      body.engine_runtime_ids = []
    } else {
      body.engine_runtime_uids = []
      body.engine_runtime_ids = idSel
    }
  }
  if (!dlg.editingId) body.enabled = true
  dlg.submitting = true
  const req = dlg.editingId
    ? API().put('/autopilot/tasks/' + dlg.editingId, body)
    : API().post('/autopilot/tasks', body)
  req
    .then(function () {
      app().showToast(dlg.editingId ? tt('form.saved') : tt('form.created'))
      dlg.open = false
      loadTasks()
      // 详情页编辑后刷新
      if (
        G.window._currentView === 'autopilot-detail' &&
        detailId.value &&
        String(detailId.value) === String(dlg.editingId)
      ) {
        loadDetail(detailId.value)
      }
    })
    .catch(function (e) {
      dlg.submitting = false
      app().showToast(
        tt(dlg.editingId ? 'form.save_failed' : 'form.create_failed', { msg: e.message || e })
      )
    })
}

// ═══ 引擎选择器 ═══
// Issue #1310：引擎显示名统一「引擎名（凭据名）」——instance_name 仅作最后兼容
function engineDisplayName(e) {
  if (!e) return ''
  return e.display_name || e.engine_name || e.name || e.instance_name || ''
}
// Issue #486：表单对应仓库 full_name（无平台前缀）；编辑态用原任务仓库，新建用当前仓库
function formRepoKey() {
  let rk = stripRepoPrefix(dlg.repoKey || '')
  if (!rk) {
    const cur = currentRepo()
    rk = stripRepoPrefix((cur && (cur.repoKey || cur.fullName)) || '')
  }
  return rk.split('/').length === 2 ? rk : ''
}
function loadEngines(force) {
  const key = formRepoKey()
  if (!force && enginesLoaded.value && enginesRepoKey === key) return Promise.resolve()
  // 后端按 #277 口径过滤：当前仓库 + 本人 + 全局引擎
  const qs = key ? '?repo=' + encodeURIComponent(key) : ''
  return API()
    .get('/settings/agent-runtimes' + qs)
    .then(function (d) {
      engines.value = (d.runtimes || d || []).filter((e) => e.status === 'online')
      enginesRepoKey = key
      enginesLoaded.value = true
    })
    .catch(function () {
      engines.value = []
      enginesRepoKey = key
      enginesLoaded.value = true
    })
}
function openEnginePicker() {
  loadEngines()
  engPicker.open = true
  engPicker.q = ''
  engPicker.type = ''
  engPicker.scope = ''
}
// Issue #1011：选择键 uid 优先（无 uid 引擎回落 id）——字符串键统一存 Set
function engineKey(e) {
  return String((e && e.engine_uid) || (e && e.id) || '')
}
// Issue #1011：选择键 → 引擎行（uid 优先，id 兜底；键统一字符串比较）
function engineByKey(key) {
  const k = String(key)
  for (let i = 0; i < engines.value.length; i++) {
    const e = engines.value[i]
    if (String(e.id) === k || String(e.engine_uid || '') === k) return e
  }
  return null
}
function toggleEngine(key) {
  if (selectedEngines.has(key)) {
    selectedEngines.delete(key)
    return
  }
  // Issue #486：已超出当前可见范围的引擎只允许取消，不允许重新选中
  if (detachedEngineKeys.value.indexOf(key) >= 0) return
  selectedEngines.add(key)
}
// Issue #486：编辑态原绑定但不在当前可见列表里的引擎（仅可移除）
// Issue #1011：双轨匹配——选中键按 uid 或 id 都算可见（防 uid 任务被误判 detached）
const detachedEngineKeys = computed(() => {
  if (!enginesLoaded.value) return []
  const visible = new Set(engines.value.map((e) => engineKey(e)))
  return Array.from(selectedEngines).filter((k) => !visible.has(k))
})
const engAvailTypes = computed(() => {
  const types = []
  engines.value.forEach(function (e) {
    const n = (e.name || '').toLowerCase()
    const tn = n.includes('hermes')
      ? 'hermes'
      : n.includes('opencode')
        ? 'opencode'
        : n.includes('codearts')
          ? 'codearts'
          : n.includes('codex')
            ? 'codex'
            : ''
    if (tn && !types.includes(tn)) types.push(tn)
  })
  return types
})
const engAvailScopes = computed(() => {
  const scopes = []
  engines.value.forEach(function (e) {
    const sc = e.scope || 'global'
    if (SCOPE_ORDER.includes(sc) && !scopes.includes(sc)) scopes.push(sc)
  })
  return scopes
})
const engFiltered = computed(() => {
  const q = engPicker.q.toLowerCase().trim()
  return engines.value.filter(function (e) {
    if (q) {
      const hay = [
        engineDisplayName(e),
        e.name,
        e.alias,
        e.host_name,
        e.worker_name,
        e.instance_name,
        e.scope,
        (e.tags || []).join(' '),
      ]
        .join(' ')
        .toLowerCase()
      if (hay.indexOf(q) < 0) return false
    }
    if (engPicker.type) {
      const ename = (e.name || '').toLowerCase()
      if (ename.indexOf(engPicker.type) < 0) return false
    }
    if (engPicker.scope && (e.scope || 'global') !== engPicker.scope) return false
    return true
  })
})
const engByScope = computed(() => {
  const groups = {}
  engFiltered.value.forEach(function (e) {
    const s = e.scope || 'global'
    if (!groups[s]) groups[s] = []
    groups[s].push(e)
  })
  return groups
})
const ENG_TAG_COLORS = {
  all: '#6366f1',
  vod: '#ec4899',
  issue: '#ef4444',
  coding: '#3b82f6',
  qa: '#a855f7',
  sre: '#f59e0b',
}
const SCOPE_COLORS = { global: '#6366f1', user: 'var(--g)', repo: '#f59e0b', local: 'var(--t3)' }
function tagColor(t) {
  return ENG_TAG_COLORS[t] || '#999'
}
function tgTag(t) {
  const tn = typeof t === 'string' ? t : t.tag || ''
  return tn === 'all' ? 'All' : tn.charAt(0).toUpperCase() + tn.slice(1)
}
function scopeColor(s) {
  return SCOPE_COLORS[s] || '#999'
}
function scopeLabel(s) {
  const v = tt('engine_picker.scope_' + s)
  return v && v !== 'autopilot.engine_picker.scope_' + s ? v : s
}

// ═══ 技能选择器 ═══
function openSkillPicker() {
  // 每次打开重新拉取（老代码注释：全局缓存切仓库不失效）
  const repo = currentRepo()
  const url =
    '/skills?agent_type=auto&page_size=0' +
    (repo ? '&repo=' + encodeURIComponent(repo.fullName || '') : '')
  API()
    .get(url)
    .then(function (d) {
      autoSkills.value = (d.skills || []).map(function (s) {
        return {
          skill_name: s.name || s.skill_name,
          display_name: s.display_name || s.name || s.skill_name,
          description: s.description || '',
          scope: s.scope || '',
        }
      })
      skillPicker.open = true
      skillPicker.q = ''
    })
    .catch(function () {
      autoSkills.value = []
      skillPicker.open = true
      skillPicker.q = ''
    })
}
function pickSkill(name) {
  selectedSkill.value = name
}
const SKILL_SCOPE_COLORS = { official: '#6366f1', user: 'var(--g)', repo: '#f59e0b' }
function skillScopeColor(s) {
  return SKILL_SCOPE_COLORS[s] || '#999'
}
function skillScopeLabel(s) {
  const v = tt('skill_picker.scope_' + s)
  return v && v !== 'autopilot.skill_picker.scope_' + s ? v : s
}
const skillFiltered = computed(() => {
  const q = skillPicker.q.toLowerCase().trim()
  if (!q) return autoSkills.value
  return autoSkills.value.filter(function (s) {
    const hay = [s.skill_name, s.display_name, s.description].join(' ').toLowerCase()
    return hay.indexOf(q) >= 0
  })
})

// ═══ 生命周期 ═══
function onDocClick(e) {
  // 关闭 kebab 菜单
  if (rowMenu.open && !e.target.closest('.apv-rowmenu') && !e.target.closest('.apv-kebab')) {
    closeRowMenu()
  }
}
onMounted(function () {
  document.addEventListener('click', onDocClick)
  if (props.initialDetailId) {
    G.window._currentView = 'autopilot-detail' // renderAutopilotDetail 契约（入口已设，防御）
    loadDetail(props.initialDetailId)
  } else {
    G.window._currentView = 'autopilot'
    loadTasks()
  }
})
onBeforeUnmount(function () {
  document.removeEventListener('click', onDocClick)
  stopAllLogPolls()
  closeLog()
})
</script>

<style scoped>
.apv {
  min-height: 100%;
}
.apv-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.apv-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}
.apv-scopes {
  display: flex;
  gap: 0;
}
.apv-search {
  flex: 1;
  min-width: 120px;
  padding: 6px 10px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 12px;
  background: var(--c);
  color: var(--t);
  outline: none;
}
.apv-sel {
  padding: 6px 10px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 12px;
  background: var(--c);
  color: var(--t);
}
/* 表格 */
.apv-grid {
  display: grid;
  grid-template-columns: 32px minmax(120px, 1fr) 130px 110px 110px 60px 36px;
  gap: 0;
  font-size: 12px;
  margin-top: 12px;
}
.apv-gridrow {
  display: contents;
}
.apv-gridrow > div {
  border-bottom: 1px solid var(--b);
}
.apv-gridhead > div {
  height: 36px;
  display: flex;
  align-items: center;
  padding: 0 8px;
  color: var(--t2);
  font-size: 11px;
  font-weight: 600;
  border-bottom: 2px solid var(--b);
}
.apv-gridrow > div {
  height: 44px;
  display: flex;
  align-items: center;
  padding: 0 8px;
}
.apv-th {
  cursor: pointer;
}
.apv-c-ck {
  padding: 0 8px;
}
.apv-c-ck input {
  cursor: pointer;
  accent-color: var(--a-text);
}
.apv-c-name {
  cursor: pointer;
  overflow: hidden;
}
.apv-cname {
  font-weight: 500;
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}
.apv-wb-chip {
  margin-left: 6px;
  font-size: 10px;
  color: var(--t3);
  font-weight: 400;
}
.apv-c-cron {
  color: var(--t3);
}
.apv-mono {
  font-family: monospace;
  font-size: 10px;
  color: var(--t3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.apv-c-last {
  gap: 4px;
}
.apv-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}
.apv-time {
  font-size: 11px;
  color: var(--t2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}
.apv-on {
  color: #28a745;
  font-size: 11px;
}
.apv-off {
  color: var(--t3);
  font-size: 11px;
}
/* Issue #3733：不可派发徽标（列表状态列） */
.apv-warn {
  color: var(--c-warning);
  font-size: 11px;
  margin-right: 2px;
  cursor: help;
}
/* Issue #3733：详情页不可派发提示条（原因 + 补齐入口） */
.apv-blocked {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  margin-bottom: 16px;
  border: 1px solid var(--c-warning);
  border-radius: 8px;
  background: color-mix(in srgb, var(--c-warning) 8%, transparent);
}
.apv-blocked-ic {
  font-size: 14px;
  line-height: 1.4;
  color: var(--c-warning);
  flex-shrink: 0;
}
.apv-blocked-txt {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--t2);
}
.apv-blocked-txt b {
  color: var(--a-text);
  font-weight: 600;
}
.apv-c-act {
  padding: 0 4px;
  justify-content: center;
  position: relative;
}
.apv-kebab {
  border: none;
  background: transparent;
  cursor: pointer;
  padding: 4px;
  border-radius: 4px;
  color: var(--t3);
  font-size: 16px;
  line-height: 1;
}
.apv-row-sel > div {
  background: rgba(49, 104, 224, 0.06);
}
.apv-gridrow:not(.apv-gridhead):hover > div {
  background: rgba(0, 0, 0, 0.03);
}
/* 空状态 */
.apv-empty {
  padding: 40px 0;
  text-align: center;
}
.apv-empty-ic {
  font-size: 32px;
  margin-bottom: 8px;
}
.apv-empty-t {
  font-size: 14px;
  color: var(--t2);
  margin-bottom: 4px;
}
.apv-empty-d {
  font-size: 12px;
  color: var(--t3);
  margin-bottom: 24px;
}
.apv-tplgrid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 10px;
  max-width: 720px;
  margin: 0 auto;
}
.apv-tplcard {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--b);
  border-radius: 10px;
  background: var(--c);
  cursor: pointer;
  text-align: left;
  transition: all 0.15s;
  font-family: inherit;
}
.apv-tplcard:hover {
  border-color: var(--a);
}
.apv-tpl-ic {
  font-size: 20px;
  flex-shrink: 0;
}
.apv-tpl-t {
  font-size: 13px;
  font-weight: 600;
  color: var(--t);
}
.apv-tpl-d {
  font-size: 11px;
  color: var(--t2);
  margin-top: 2px;
}
.apv-tpl-more {
  margin-top: 16px;
}
.apv-nomatch {
  padding: 24px;
  text-align: center;
  color: var(--t3);
}
/* 批量条 */
.apv-batchbar {
  margin-top: 8px;
  padding: 8px 12px;
  background: var(--bg);
  border-radius: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
}
/* 详情页 */
.apv-dp-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
}
.apv-dp-status {
  font-size: 12px;
  color: var(--t2);
}
.apv-switch {
  position: relative;
  width: 36px;
  height: 20px;
  border-radius: 10px;
  background: var(--b);
  transition: background 0.2s;
  cursor: pointer;
  flex-shrink: 0;
  display: inline-block;
}
.apv-switch.on {
  background: #28a745;
}
.apv-switch.sm {
  width: 32px;
  height: 18px;
  border-radius: 9px;
}
.apv-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--on-accent);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
  transition: left 0.2s;
}
.apv-switch.on .apv-knob {
  left: 18px;
}
.apv-switch.sm .apv-knob {
  width: 14px;
  height: 14px;
}
.apv-switch.sm.on .apv-knob {
  left: 16px;
}
.apv-card {
  margin-bottom: 16px;
}
.apv-infogrid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  font-size: 13px;
}
.apv-lbl {
  color: var(--t2);
}
.apv-detach {
  background: rgba(255, 59, 48, 0.08);
  border: 1px solid rgba(255, 59, 48, 0.18);
  border-radius: 12px;
  padding: 10px 14px;
  margin-top: 12px;
  font-size: 11px;
  color: var(--r);
  line-height: 1.6;
}
.apv-instr-wrap {
  margin-top: 12px;
}
.apv-instr {
  white-space: pre-wrap;
  font-size: 13px;
  margin-top: 4px;
  padding: 10px;
  background: var(--bg);
  border-radius: 6px;
  font-family: inherit;
}
.apv-cronrow {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
}
.apv-cronrow .apv-mono {
  font-size: 13px;
  color: var(--t);
}
.apv-cronsub {
  margin-top: 8px;
  font-size: 12px;
  color: var(--t2);
}
.apv-cronsub + .apv-cronsub {
  margin-top: 4px;
}
/* 高级选项 */
.apv-adv-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.apv-adv-wb {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.apv-lbl-s {
  font-size: 12px;
  font-weight: 600;
  color: var(--t2);
}
.apv-lbl-m {
  font-size: 13px;
  color: var(--t3);
  white-space: nowrap;
}
.apv-wb-lbl {
  font-size: 11px;
  color: var(--t3);
}
.apv-wb-lbl.on {
  color: #28a745;
}
.apv-adv-wbsel {
  margin-bottom: 12px;
}
.apv-adv-wbsel.off {
  opacity: 0.5;
  pointer-events: none;
}
.apv-input {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--b);
  border-radius: 8px;
  font-size: 13px;
  background: var(--c);
  color: var(--t);
  outline: none;
  min-width: 0;
}
.apv-input.lg {
  font-size: 14px;
  font-weight: 500;
}
.apv-input.area {
  resize: none;
  font-family: inherit;
}
/* 自定义字段 */
.apv-cf-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.apv-cf-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.apv-cf-key {
  width: 130px;
  flex-shrink: 0;
  padding: 6px 8px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 12px;
  background: var(--c);
  color: var(--t);
  outline: none;
}
.apv-cf-val {
  flex: 1;
  min-width: 0;
  padding: 6px 8px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 12px;
  background: var(--c);
  color: var(--t);
  outline: none;
}
.cf-invalid {
  border-color: var(--r, #dc3545) !important;
}
.apv-cf-eye {
  border: 1px solid var(--b);
  background: var(--c);
  border-radius: 999px;
  width: 26px;
  height: 26px;
  padding: 0;
  font-size: 12px;
  cursor: pointer;
  color: var(--t3);
  flex-shrink: 0;
}
.apv-cf-del {
  border: none;
  background: transparent;
  font-size: 16px;
  cursor: pointer;
  color: var(--t3);
  padding: 0 4px;
  flex-shrink: 0;
}
.apv-cf-add {
  margin-top: 8px;
  font-size: 12px;
  padding: 4px 14px;
}
/* 执行历史 */
.apv-exec-row {
  border-bottom: 1px solid var(--b);
  cursor: pointer;
  transition: background 0.15s;
}
.apv-exec-row:hover {
  background: rgba(0, 0, 0, 0.02);
}
.apv-exec-main {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
}
.apv-exec-ic {
  font-size: 14px;
  flex-shrink: 0;
}
.apv-exec-st {
  font-size: 12px;
  font-weight: 600;
  min-width: 70px;
}
.apv-exec-time {
  font-size: 11px;
  color: var(--t3);
  white-space: nowrap;
}
.apv-exec-res {
  flex: 1;
  font-size: 12px;
  color: var(--t2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.apv-badge {
  font-size: 10px;
  background: rgba(49, 104, 224, 0.12);
  color: var(--a-text);
  padding: 2px 8px;
  border-radius: 8px;
  flex-shrink: 0;
}
.apv-exec-rtry {
  font-size: 10px;
  color: var(--t3);
  flex-shrink: 0;
}
.apv-logbtn {
  font-size: 10px;
  color: var(--a);
  border: 1px solid color-mix(in srgb, var(--a) 42%, transparent);
  border-radius: 999px;
  padding: 2px 10px;
  flex-shrink: 0;
  cursor: pointer;
  white-space: nowrap;
  background: none;
}
.apv-exec-arrow {
  font-size: 10px;
  color: var(--t3);
  transition: transform 0.3s;
  flex-shrink: 0;
}
.apv-exec-arrow.open {
  transform: rotate(180deg);
}
.apv-exec-detail {
  padding: 4px 12px 14px 36px;
}
.apv-exec-lbl {
  font-size: 11px;
  color: var(--t3);
  margin-bottom: 6px;
}
.apv-exec-pre {
  white-space: pre-wrap;
  font-size: 12px;
  color: var(--t);
  background: var(--bg);
  border-radius: 8px;
  padding: 10px 12px;
  max-height: 300px;
  overflow-y: auto;
  margin-bottom: 12px;
  font-family: inherit;
}
.apv-muted {
  font-size: 11px;
  color: var(--t3);
}
.apv-findings-t {
  margin-bottom: 6px;
}
.apv-finding {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 0;
  border-bottom: 1px solid var(--b);
}
.apv-find-ic {
  font-size: 12px;
  flex-shrink: 0;
}
.apv-find-body {
  flex: 1;
  min-width: 0;
}
.apv-find-t {
  font-size: 12px;
  font-weight: 500;
  color: var(--t);
}
.apv-find-d {
  font-size: 11px;
  color: var(--t2);
  margin-top: 2px;
  white-space: pre-wrap;
}
.apv-find-sev {
  font-size: 10px;
  margin-top: 2px;
}
.apv-find-act {
  flex-shrink: 0;
  align-self: center;
}
/* 弹窗通用 */
.apv-modal-ov {
  position: fixed;
  inset: 0;
  z-index: 1001;
  background: rgba(0, 0, 0, 0.25);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
}
.apv-dialog {
  background: var(--c, #fff);
  border-radius: 12px;
  width: 900px;
  max-width: calc(100vw - 32px);
  height: min(680px, calc(100vh - 64px));
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.15);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: all 0.3s ease;
}
.apv-dialog.expanded {
  width: calc(100vw - 32px);
  height: calc(100vh - 32px);
}
.apv-dlg-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 20px;
  border-bottom: 1px solid var(--b);
  flex-shrink: 0;
}
.apv-dlg-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
}
.apv-dlg-ic {
  display: inline-flex;
  width: 20px;
  height: 20px;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  background: rgba(49, 104, 224, 0.15);
  color: var(--a-text);
  font-size: 11px;
}
.apv-dlg-tools {
  display: flex;
  align-items: center;
  gap: 2px;
}
.apv-dlg-tool {
  padding: 4px 8px;
  border: none;
  background: transparent;
  cursor: pointer;
  color: var(--t3);
  border-radius: 4px;
}
.apv-dlg-close {
  cursor: pointer;
  font-size: 18px;
  color: var(--t3);
  padding: 4px 8px;
  border-radius: 999px;
  line-height: 1;
  background: none;
  border: none;
}
.apv-dlg-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: row;
  overflow: hidden;
}
.apv-dlg-left {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--b);
  overflow-y: auto;
}
.apv-dlg-left .apv-fld {
  padding: 8px 20px;
}
.apv-dlg-left .apv-fld:first-child {
  padding-top: 16px;
}
.apv-dlg-left .apv-fld:last-child {
  padding-bottom: 16px;
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.apv-dlg-right {
  flex: 1.2;
  min-width: 340px;
  overflow-y: auto;
  padding: 16px 20px;
  background: var(--bg, rgba(0, 0, 0, 0.02));
}
.apv-fld {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.apv-fld + .apv-fld {
  margin-top: 8px;
}
.apv-hint {
  font-size: 11px;
  color: var(--t3);
}
.apv-req {
  color: var(--r, #dc3545);
}
.apv-pickerbtn {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--b);
  border-radius: 8px;
  background: var(--c);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 38px;
  font-size: 13px;
}
.apv-instr-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
}
.apv-dlg-instr textarea {
  flex: 1;
  min-height: 120px;
}
.apv-dlg-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 20px;
  border-top: 1px solid var(--b);
  flex-shrink: 0;
  background: var(--c, #fff);
}
.apv-dlg-btns {
  display: flex;
  gap: 8px;
}
/* schedule editor */
.apv-sect {
  font-size: 13px;
  font-weight: 600;
  color: var(--t2);
  margin-bottom: 8px;
}
.apv-schedbox {
  border: 1px solid var(--b);
  border-radius: 8px;
  padding: 12px;
  background: var(--c, #fff);
}
.apv-fset {
  border: none;
  padding: 0;
  margin: 0 0 16px 0;
}
.apv-seg {
  display: inline-grid;
  grid-auto-flow: column;
  grid-auto-columns: 1fr;
  gap: 2px;
  padding: 2px;
  background: var(--chip-bg);
  border-radius: 999px;
}
.apv-seg button {
  padding: 4px 14px;
  border: none;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
  background: transparent;
  color: var(--t3);
}
.apv-seg button.on {
  background: var(--a);
  color: #fff;
  
}
.apv-mt6 {
  margin-top: 6px;
}
.apv-everyrow {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  align-items: center;
}
.apv-inputgrp {
  display: flex;
  align-items: center;
  border: 1px solid var(--b);
  border-radius: 6px;
  overflow: hidden;
  min-width: 0;
  flex: 1;
  background: var(--c);
}
.apv-igrp-lbl {
  padding: 6px 8px;
  font-size: 11px;
  color: var(--t3);
  white-space: nowrap;
}
.apv-num {
  width: 44px;
  text-align: center;
  padding: 6px 4px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 13px;
  background: var(--c);
  color: var(--t);
  outline: none;
}
.apv-num.sm {
  width: 36px;
}
.apv-inputgrp .apv-num {
  border: none;
  border-radius: 0;
  flex: 1;
  width: auto;
}
.apv-igrp-sel {
  border: none;
  border-left: 1px solid var(--b);
  padding: 6px 8px;
  font-size: 11px;
  background: var(--c);
  color: var(--t);
  outline: none;
  cursor: pointer;
  min-width: 0;
}
.apv-igrp-sel.flex {
  flex: 1;
}
.apv-tilde {
  padding: 0 2px;
  font-size: 10px;
  color: var(--t3);
}
.apv-minrow {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
}
.apv-dayrow {
  display: grid;
  grid-template-columns: 7fr 4fr;
  gap: 6px;
  align-items: center;
}
.apv-input.flex {
  flex: 1;
}
.apv-weekdays {
  display: flex;
  gap: 2px;
  margin-top: 6px;
}
.apv-weekdays button {
  flex: 1;
  min-width: 0;
  height: 26px;
  border: none;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 500;
  cursor: pointer;
  background: var(--bg);
  color: var(--t3);
}
.apv-weekdays button.on {
  background: var(--t);
  color: var(--c);
}
.apv-advlink {
  font-size: 10px;
  margin-top: 2px;
}
.apv-advlink span {
  cursor: pointer;
  color: var(--a);
}
.apv-cronpreview {
  font-size: 12px;
  color: var(--t2);
  margin-top: 6px;
  padding: 8px 10px;
  background: var(--c, #fff);
  border-radius: 6px;
}
.apv-cronstr {
  font-size: 11px;
  color: var(--t);
}
.apv-cron-desc {
  font-size: 12px;
  color: var(--t2);
  margin-top: 2px;
}
.apv-nextrun-l {
  margin-top: 4px;
}
.apv-retryrow {
  display: flex;
  align-items: center;
  gap: 8px;
}
/* 选择器弹窗 */
.apv-pickmodal {
  background: var(--c, #fff);
  border-radius: 12px;
  padding: 20px 24px;
  width: 460px;
  height: 600px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.12);
  display: flex;
  flex-direction: column;
}
.apv-pickmodal.short {
  height: 560px;
}
.apv-pickmodal h3 {
  margin: 0 0 4px;
  font-size: 17px;
  font-weight: 600;
  color: var(--t, #1d1d1f);
}
.apv-pick-sub {
  font-size: 12px;
  color: var(--t3);
  margin-bottom: 12px;
}
.apv-pick-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
  flex-shrink: 0;
  align-items: center;
}
.apv-pick-tags > span {
  cursor: pointer;
  font-size: 11px;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--b, #e8e8ed);
  color: var(--t, #1d1d1f);
  background: var(--bg, #f5f5f7);
  font-weight: 500;
  transition: all 0.15s;
}
.apv-pick-tags > span.on {
  border-color: var(--a, #0071e3);
  color: var(--on-accent);
  background: var(--a, #0071e3);
}
.apv-tag-sep {
  width: 1px;
  height: 14px;
  background: var(--b, #e8e8ed);
  margin: 0 4px;
  padding: 0 !important;
  cursor: default !important;
  border: none !important;
}
.apv-tag-clear {
  color: var(--t3, #86868b) !important;
}
.apv-pick-search {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--b);
  border-radius: 8px;
  font-size: 13px;
  background: var(--c);
  color: var(--t);
  outline: none;
  margin-bottom: 12px;
  flex-shrink: 0;
}
.apv-pick-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}
.apv-pick-empty {
  padding: 20px 0;
  text-align: center;
  color: var(--t3);
  font-size: 13px;
}
.apv-pick-group {
  margin-top: 12px;
  margin-bottom: 4px;
  font-size: 11px;
  font-weight: 600;
  color: var(--t3);
  text-transform: uppercase;
}
.apv-pick-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  border: none;
  border-radius: 14px;
  cursor: pointer;
  background: var(--bg, #f5f5f7);
  transition: all 0.2s ease;
}
.apv-pick-item.on {
  background: rgba(0, 113, 227, 0.06);
}
.apv-pick-item input {
  width: 18px;
  height: 18px;
  cursor: pointer;
  accent-color: var(--a-text);
}
.apv-pick-body {
  flex: 1;
  min-width: 0;
}
.apv-pick-name {
  font-weight: 600;
  font-size: 13px;
  color: var(--t, #1d1d1f);
  display: flex;
  align-items: center;
  flex-wrap: wrap;
}
.apv-pick-alias {
  font-size: 10px;
  color: var(--t3);
  font-weight: 400;
  margin-left: 4px;
}
.apv-pick-meta {
  font-size: 10px;
  color: var(--t3);
  margin-top: 3px;
}
.apv-pick-online {
  font-size: 11px;
  font-weight: 500;
  color: var(--g);
  flex-shrink: 0;
}
.apv-engtag,
.apv-scopetag {
  font-size: 9px;
  padding: 2px 7px;
  border-radius: 999px;
  color: var(--on-accent);
  margin-left: 4px;
  font-weight: 500;
}
.apv-pick-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
  flex-shrink: 0;
}
/* 派发弹窗 */
.apv-dispmodal {
  background: var(--c, #fff);
  border-radius: 12px;
  width: 520px;
  max-width: calc(100vw - 32px);
  max-height: calc(100vh - 64px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.15);
}
.apv-disp-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
}
/* kebab 菜单 */
.apv-rowmenu {
  position: fixed;
  z-index: 1001;
  width: 140px;
  background: var(--c, #fff);
  border: 1px solid var(--b);
  border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
  padding: 4px;
}
.apv-rowmenu > div {
  padding: 8px 10px;
  font-size: 12px;
  cursor: pointer;
  border-radius: 4px;
  color: var(--t);
  white-space: nowrap;
}
.apv-rowmenu > div:hover {
  background: var(--bg);
}
.apv-rowmenu-del {
  color: #dc3545 !important;
}
.apv-rowmenu-del:hover {
  background: rgba(220, 53, 69, 0.08) !important;
}
/* Issue #486：超范围引擎分组 */
.apv-detached-g {
  color: var(--r);
}
.apv-detached {
  background: color-mix(in srgb, var(--r) 8%, transparent);
  opacity: 0.85;
}
.apv-detached-warn {
  color: var(--r);
}
/* Issue #697：疑似重复徽标与派发提示 */
.apv-dup-badge {
  display: inline-block;
  margin-left: 6px;
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--c-warning) 12.5%, transparent);
  color: var(--c-warning);
  vertical-align: middle;
}
.apv-dup-warn {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin-bottom: 12px;
  padding: 10px 12px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--c-warning) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--c-warning) 20%, transparent);
  font-size: 12px;
  color: var(--t2, #5b667b);
  line-height: 1.6;
}
.apv-dup-ic {
  flex-shrink: 0;
}
.apv-dup-note {
  margin-top: 2px;
  color: var(--t3, #86868b);
}

/* 执行日志弹窗（与 EventView 的 ev__tl-* 同一套样式，作用域互不影响） */
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
.ev__tl-body .apv-muted {
  color: #8b96a8;
}
</style>
