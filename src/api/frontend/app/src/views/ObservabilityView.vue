<template>
  <div class="obv">
    <!-- ═══ 头部：标题 + 刷新控制 ═══ -->
    <div class="cd obv-shell">
      <div class="obv-head">
        <h2 class="obv-title">
          {{ t('title') }}
          <span class="ts t3">/ {{ t('section.' + section) }}</span>
        </h2>
        <div v-if="section === 'overview'" class="obv-headtools">
          <span class="ts t3">{{ lastUpdate }}</span>
          <span class="stabs obv-rangbar">
            <a
              v-for="r in ['1h', '6h', '24h', '7d']"
              :key="r"
              :class="{ ac: ovRange === r }"
              @click="switchOvRange(r)"
              >{{ r }}</a
            >
          </span>
          <button class="bt bt-o bt-s" @click="loadOverview()">{{ t('refresh') }}</button>
        </div>
        <div v-else class="obv-headtools">
          <span class="ts t3">{{ lastUpdate }}</span>
          <button class="bt bt-o bt-s" @click="refreshObs()">{{ t('refresh') }}</button>
          <label class="ts t3 obv-autolbl"
            ><input v-model="autoOn" type="checkbox" style="margin: 0" @change="toggleAuto" />{{
              t('auto_refresh')
            }}</label
          >
        </div>
      </div>

      <!-- ═══ 页内大 Tab（三视图） ═══ -->
      <div class="obv-bigtabs">
        <a
          v-for="v in ['overview', 'devices', 'inspect']"
          :key="v"
          :class="{ on: section === v }"
          @click="switchSection(v)"
        >
          <div class="obv-tab-ic">{{ { overview: '📊', devices: '💻', inspect: '▶' }[v] }}</div>
          <div class="obv-tab-t">{{ t('section.' + v) }}</div>
          <div class="obv-tab-h">{{ t('view_hint.' + v) }}</div>
        </a>
      </div>

      <!-- ═══ 子 Tab（仅 inspect 有） ═══ -->
      <div v-if="subTabs.length > 1" class="stabs obv-subtabs">
        <a v-for="tb in subTabs" :key="tb" :class="{ ac: tab === tb }" @click="switchTab(tb)">{{
          t('tab.' + tb)
        }}</a>
      </div>

      <!-- ═══ 内容区 ═══ -->
      <div class="obv-content">
        <!-- 总览 -->
        <template v-if="section === 'overview'">
          <div v-if="ovLoading" class="loading">{{ t('loading') }}</div>
          <div v-else-if="ovError" class="error">{{ ovError }}</div>
          <template v-else>
            <!-- SLO 卡 -->
            <div v-html="sloCardHtml"></div>
            <!-- 告警横幅 -->
            <div v-html="bannerHtml"></div>
            <!-- KPI -->
            <div class="obv-kpigrid">
              <div class="cd obv-kpi">
                <div class="ts t3">{{ t('ov.kpi_queue') }}</div>
                <div class="obv-kpi-v">{{ ovKpis.queue_depth || 0 }}</div>
              </div>
              <div class="cd obv-kpi">
                <div class="ts t3">{{ t('ov.kpi_success') }}</div>
                <div class="obv-kpi-v">{{ ovKpiSuccess }}</div>
              </div>
              <div class="cd obv-kpi">
                <div class="ts t3">{{ t('ov.kpi_p95') }}</div>
                <div class="obv-kpi-v">{{ ovKpiP95 }}</div>
              </div>
              <div class="cd obv-kpi">
                <div class="ts t3">{{ t('ov.kpi_engines') }}</div>
                <div class="obv-kpi-v">{{ ovKpiEngines }}</div>
              </div>
              <div class="cd obv-kpi">
                <div class="ts t3">{{ t('ov.kpi_tokens') }}</div>
                <div class="obv-kpi-v">{{ (ovKpis.tokens || 0).toLocaleString() }}</div>
                <div class="ts t3">{{ t('ov.scope') }}{{ (ovScope && ovScope.repos) || 0 }}</div>
              </div>
            </div>
            <!-- 图表 -->
            <div class="obv-chartgrid">
              <div class="cd obv-chartbox">
                <div class="ts t3 obv-chart-lbl">{{ t('ov.chart_outcome') }}</div>
                <div :ref="(el) => setChartEl('chartOutcome', el)" class="obv-chart240"></div>
              </div>
              <div class="cd obv-chartbox">
                <div class="ts t3 obv-chart-lbl">{{ t('ov.chart_model') }}</div>
                <div :ref="(el) => setChartEl('chartModel', el)" class="obv-chart240"></div>
              </div>
            </div>
            <div class="cd obv-chartbox">
              <div class="ts t3 obv-chart-lbl">{{ t('ov.chart_token') }}</div>
              <div :ref="(el) => setChartEl('chartToken', el)" class="obv-chart220"></div>
            </div>
          </template>
        </template>

        <!-- 设备与引擎 -->
        <template v-else-if="section === 'devices'">
          <div v-if="!opsData && !rtData" class="loading">{{ t('loading') }}</div>
          <template v-else>
            <div class="obv-sec">
              <h2>{{ t('worker_monitor') }}</h2>
              <span class="obv-sec-sub">{{
                t('online_count', {
                  count: (opsWorkers.online || 0) + '/' + (opsWorkers.total || 0),
                })
              }}</span>
            </div>
            <div class="obv-kpirow">
              <div class="obv-kpicell">
                <div class="obv-kpil">{{ t('worker_total') }}</div>
                <div class="obv-kpiv" style="color: var(--a)">{{ opsWorkers.total || 0 }}</div>
              </div>
              <div class="obv-kpicell">
                <div class="obv-kpil">{{ t('online') }}</div>
                <div class="obv-kpiv" style="color: var(--g)">{{ opsWorkers.online || 0 }}</div>
              </div>
              <div class="obv-kpicell">
                <div class="obv-kpil">{{ t('offline') }}</div>
                <div class="obv-kpiv" style="color: var(--t3)">{{ opsWorkers.offline || 0 }}</div>
              </div>
              <div class="obv-kpicell">
                <div class="obv-kpil">{{ t('engine_total') }}</div>
                <div class="obv-kpiv" style="color: var(--pu)">{{ opsRuntimes.total || 0 }}</div>
              </div>
            </div>
            <div class="obv-wkgrid">
              <div v-if="!sortedWorkers.length" class="ts t2" style="padding: 12px">
                {{ t('no_remote_worker') }}
              </div>
              <div v-for="wk in pageWorkers" :key="wkKey(wk)" class="obv-wkcard">
                <div class="obv-wk-head">
                  <span class="obv-wk-dot">{{ healthDot(wk.health) }}</span>
                  <b class="obv-wk-name">{{ workerDisplayName(wk) }}</b>
                  <span class="obv-wk-health" :style="{ color: healthColor(wk.health) }">{{
                    t(wk.health || 'offline')
                  }}</span>
                </div>
                <div class="obv-wk-meta">
                  <div>IP: {{ wk.last_ip || '-' }}</div>
                  <div>
                    {{ t('heartbeat') }}: {{ fmtLocal(wk.last_seen_at || '').substring(11, 19) }}
                  </div>
                </div>
                <div v-for="b in workerBars(wk)" :key="b.label" class="obv-bar-row">
                  <span class="obv-bar-lbl">{{ b.label }}</span>
                  <div class="obv-bar-track">
                    <div
                      class="obv-bar-fill"
                      :style="{ width: Math.min(b.pct, 100) + '%', background: barColor(b.pct) }"
                    ></div>
                  </div>
                  <span class="obv-bar-val" :style="{ color: barColor(b.pct) }">{{ b.text }}</span>
                </div>
              </div>
            </div>
            <div v-if="sortedWorkers.length" class="obv-pager">
              <button class="bt bt-o bt-s" :disabled="workerPage === 0" @click="workerPage--">
                {{ t('prev_page') }}
              </button>
              <span class="ts t3">{{ workerPage + 1 }} / {{ workerTotalPages }}</span>
              <button
                class="bt bt-o bt-s"
                :disabled="workerPage >= workerTotalPages - 1"
                @click="workerPage++"
              >
                {{ t('next_page') }}
              </button>
              <label class="ts t3 obv-pagesz"
                >{{ t('rows_per_page') }}
                <select :value="workerPageSize" @change="setWorkerPageSize">
                  <option
                    v-for="n in [10, 20, 50]"
                    :key="n"
                    :value="n"
                    :selected="workerPageSize === n"
                  >
                    {{ n }}
                  </option>
                </select>
              </label>
            </div>
            <!-- Runtime 引擎表 -->
            <div class="obv-sec">
              <h2>{{ t('runtime_engine') }}</h2>
              <span class="obv-sec-sub">{{
                t('runtime_subtitle', {
                  online: opsRuntimes.online || 0,
                  total: opsRuntimes.total || 0,
                })
              }}</span>
            </div>
            <template v-if="sortedRuntimes.length">
              <div class="obv-rtfilter">
                <input
                  v-model="rtSearch"
                  type="text"
                  class="obv-rtsearch"
                  :placeholder="t('search_engine_placeholder')"
                  @input="runtimePage = 0"
                />
                <select v-model="rtDeviceFilter" class="obv-rtsel" @change="runtimePage = 0">
                  <option value="">{{ t('all_devices') }}</option>
                  <option v-for="d in availDevices" :key="d" :value="d">{{ d }}</option>
                </select>
                <span class="ts t3">{{ filteredRuntimes.length }}/{{ sortedRuntimes.length }}</span>
              </div>
              <div v-if="pageRuntimes.length" class="tw">
                <table>
                  <thead>
                    <tr>
                      <th>{{ t('col_engine') }}</th>
                      <th>{{ t('col_version') }}</th>
                      <th>{{ t('col_health') }}</th>
                      <th>健康分</th>
                      <th>{{ t('col_busy') }}</th>
                      <th>{{ t('col_avg_duration') }}</th>
                      <th>{{ t('col_token') }}</th>
                      <th>{{ t('col_probe') }}</th>
                      <th>{{ t('col_mode') }}</th>
                      <th>{{ t('col_device') }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="rti in pageRuntimes"
                      :key="rti.id"
                      class="obv-rtrow"
                      @click="openSessionModal(rti)"
                    >
                      <td>
                        {{ healthDot(rti.health) }} <b>{{ rti.name }}</b>
                      </td>
                      <td class="ts t3">{{ rti.version || '-' }}</td>
                      <td :style="{ color: healthColor(rti.health), fontWeight: 600 }">
                        {{ t(rti.health || 'offline') }}
                      </td>
                      <td class="ts t3" v-html="healthScoreHtml(rti)"></td>
                      <td :style="{ color: loadColor(rti) }">
                        {{ rti.running || 0 }}/{{ rti.max_concurrent || 0 }}
                      </td>
                      <td :style="{ color: avgDurColor(rti) }">{{ avgDurStr(rti) }}</td>
                      <td class="ts t2">{{ tokForEngine(rti.id) }}</td>
                      <td
                        :style="{ color: probeColor(rti.probe_status) }"
                        :title="rti.probe_error || ''"
                      >
                        {{ probeIcon(rti.probe_status) }}
                        {{ probeStr(rti.probe_status, rti.probe_latency) }}
                      </td>
                      <td>
                        <span :class="rti.mode === 'local' ? 'tg tg-ch' : 'tg tg-c'">{{
                          rti.mode === 'local' ? t('mode_local') : t('mode_remote')
                        }}</span>
                      </td>
                      <td class="ts t3">
                        {{ rti.device_name ? workerDisplayName({ name: rti.device_name }) : '-' }}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div v-else class="ts t2" style="padding: 12px">{{ t('no_matching_engine') }}</div>
              <div class="obv-pager">
                <button class="bt bt-o bt-s" :disabled="runtimePage === 0" @click="runtimePage--">
                  {{ t('prev_page') }}
                </button>
                <span class="ts t3">{{ runtimePage + 1 }} / {{ runtimeTotalPages }}</span>
                <button
                  class="bt bt-o bt-s"
                  :disabled="runtimePage >= runtimeTotalPages - 1"
                  @click="runtimePage++"
                >
                  {{ t('next_page') }}
                </button>
                <label class="ts t3 obv-pagesz"
                  >{{ t('rows_per_page') }}
                  <select :value="runtimePageSize" @change="setRuntimePageSize">
                    <option
                      v-for="n in [10, 20, 50]"
                      :key="n"
                      :value="n"
                      :selected="runtimePageSize === n"
                    >
                      {{ n }}
                    </option>
                  </select>
                </label>
              </div>
            </template>
            <div v-else class="ts t2" style="padding: 12px">{{ t('no_engine') }}</div>
          </template>
        </template>

        <!-- 任务与日志 -->
        <template v-else>
          <!-- tasks 子 Tab -->
          <template v-if="tab === 'tasks'">
            <div class="obv-anomalybar">
              <button class="bt bt-o bt-s" @click="openAnomaly">
                {{ t('tab.anomaly') }}
              </button>
              <span class="ts t3">{{ t('anomaly_open_hint') }}</span>
            </div>
            <div class="obv-sec">
              <h2>{{ t('task_status_dist') }}</h2>
            </div>
            <div class="obv-kpirow">
              <div class="obv-kpicell">
                <div class="obv-kpil">{{ t('task_total') }}</div>
                <div class="obv-kpiv" style="color: var(--a)">{{ taskStats.total || 0 }}</div>
              </div>
              <div class="obv-kpicell">
                <div class="obv-kpil">{{ t('completed') }}</div>
                <div class="obv-kpiv" style="color: var(--g)">{{ taskStats.completed || 0 }}</div>
              </div>
              <div class="obv-kpicell">
                <div class="obv-kpil">{{ t('in_progress') }}</div>
                <div class="obv-kpiv" style="color: var(--pu)">
                  {{ taskStats.in_progress || 0 }}
                </div>
              </div>
              <div class="obv-kpicell">
                <div class="obv-kpil">{{ t('failed') }}</div>
                <div
                  class="obv-kpiv"
                  :style="{ color: (taskStats.failed || 0) > 0 ? 'var(--r)' : 'var(--t3)' }"
                >
                  {{ taskStats.failed || 0 }}
                </div>
              </div>
            </div>
            <!-- Token 卡 -->
            <div v-html="tokCardHtml"></div>
            <!-- Issue 任务列表 -->
            <div v-if="timelineLoading" class="loading">{{ t('loading_tasks') }}</div>
            <div v-else-if="timelineError" class="error">{{ timelineError }}</div>
            <template v-else>
              <div class="obv-sec">
                <h2>{{ t('task_list') }}</h2>
                <span class="obv-sec-sub">{{
                  t('task_list_subtitle', { count: timelineIssues.length })
                }}</span>
              </div>
              <input
                v-model="timelineSearch"
                type="text"
                class="obv-tlsearch"
                :placeholder="t('search_tasks_placeholder')"
              />
              <div
                v-if="!filteredIssues.length"
                class="ts t2"
                style="padding: 20px; text-align: center"
              >
                {{ t('no_matching_tasks') }}
              </div>
              <template v-else>
                <div v-for="iss in pageIssues" :key="iss.issueId" class="obv-issuecard">
                  <div class="obv-issue-head" @click="toggleIssue(iss.issueId)">
                    <span class="obv-issue-arrow">{{
                      expandedIssues.has(iss.issueId) ? '▾' : '▸'
                    }}</span>
                    <a
                      v-if="issueUrl(iss)"
                      :href="issueUrl(iss)"
                      target="_blank"
                      class="obv-issue-link"
                      @click.stop
                      >{{ iss.issueId }} ↗</a
                    >
                    <b v-else class="obv-issue-id">{{ iss.issueId }}</b>
                    <span v-if="iss.title" class="obv-issue-title">{{ iss.title }}</span>
                    <span v-else style="flex: 1"></span>
                    <span class="ts t3">{{ t('events_count', { count: iss.eventCount }) }}</span>
                    <span
                      :style="{
                        color: issueHasFailed(iss) ? 'var(--r)' : 'var(--g)',
                        fontSize: '10px',
                        fontWeight: 600,
                      }"
                      >{{ issueHasFailed(iss) ? t('has_failure') : t('normal') }}</span
                    >
                    <span class="ts t3" style="color: var(--a)"
                      >{{ t('total_duration') }}: {{ totalDurStr(iss) }}</span
                    >
                    <span class="ts t3">{{ fmtLocal(iss.lastAt || '').substring(0, 16) }}</span>
                  </div>
                  <div v-if="expandedIssues.has(iss.issueId)" class="obv-issue-body">
                    <div class="obv-viewmode">
                      <button
                        :class="[
                          'bt bt-s',
                          issueViewMode(iss.issueId) !== 'events' ? 'bt-p' : 'bt-o',
                        ]"
                        @click="setIssueView(iss.issueId, 'diagram')"
                      >
                        {{ t('sequence_diagram') }}
                      </button>
                      <button
                        :class="[
                          'bt bt-s',
                          issueViewMode(iss.issueId) === 'events' ? 'bt-p' : 'bt-o',
                        ]"
                        @click="setIssueView(iss.issueId, 'events')"
                      >
                        {{ t('event_list') }}
                      </button>
                    </div>
                    <div
                      v-show="issueViewMode(iss.issueId) !== 'events'"
                      :ref="(el) => setDiagramRef(iss.issueId, el)"
                      class="obv-mmdbox"
                    ></div>
                    <div v-show="issueViewMode(iss.issueId) === 'events'" class="obv-evtable-wrap">
                      <table class="obv-evtable">
                        <thead>
                          <tr>
                            <th>{{ t('col_seq') }}</th>
                            <th>{{ t('col_event') }}</th>
                            <th>{{ t('col_agent') }}</th>
                            <th>{{ t('col_engine') }}</th>
                            <th>{{ t('col_skill') }}</th>
                            <th>{{ t('col_status') }}</th>
                            <th>{{ t('col_duration') }}</th>
                            <th>{{ t('col_token') }}</th>
                            <th>{{ t('col_time') }}</th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr
                            v-for="ev in iss.events"
                            :key="ev.seq"
                            :style="ev.taskId ? { cursor: 'pointer' } : {}"
                            @click="onEvRowClick(ev, iss)"
                          >
                            <td class="ts">{{ ev.seq }}</td>
                            <td>{{ actionText(ev.eventType) }}</td>
                            <td>
                              <b>{{ ev.agentType || '-' }}</b>
                            </td>
                            <td class="ts t3">{{ engineWithIp(ev.engine) }}</td>
                            <td class="ts t3">{{ ev.skill || '-' }}</td>
                            <td
                              :style="{
                                color:
                                  ev.status === 'failed'
                                    ? 'var(--r)'
                                    : ev.status === 'completed'
                                      ? 'var(--g)'
                                      : 'var(--t3)',
                              }"
                            >
                              {{ ev.status || '-' }}
                            </td>
                            <td class="ts t3">{{ ev.duration || '-' }}s</td>
                            <td class="ts t2">{{ tokForTask(ev.taskId) }}</td>
                            <td class="ts t3">
                              {{ fmtLocal(ev.createdAt || '').substring(0, 19) }}
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  </div>
                </div>
                <div class="obv-pager">
                  <button
                    class="bt bt-o bt-s"
                    :disabled="timelinePage === 0"
                    @click="timelinePage--"
                  >
                    {{ t('prev_page') }}
                  </button>
                  <span class="ts t3">{{ timelinePage + 1 }} / {{ timelineTotalPages }}</span>
                  <button
                    class="bt bt-o bt-s"
                    :disabled="timelinePage >= timelineTotalPages - 1"
                    @click="timelinePage++"
                  >
                    {{ t('next_page') }}
                  </button>
                </div>
              </template>
            </template>
          </template>

          <!-- events 子 Tab（统一事件视图） -->
          <template v-else-if="tab === 'events'">
            <!-- Issue #476：跨仓「failed 且 result 为空」占比摘要（异步、失败静默不阻塞列表） -->
            <div v-if="evGap && evGap.total_failed" class="obv-gap">
              <span class="obv-gap-t">{{ t('ev_failed_gap_title') }}</span>
              <span>Failed {{ evGap.total_failed }}</span>
              <span :style="{ color: evGap.empty_ratio > 0 ? 'var(--r)' : 'var(--g)' }">
                {{ (evGap.empty_ratio * 100).toFixed(1) }}% {{ t('ev_failed_empty') }}
              </span>
              <span class="ts t3">{{ evGap.empty_result }} {{ t('ev_failed_empty_unit') }}</span>
              <span class="ts t3">legacy {{ evGap.legacy }}</span>
              <span class="ts t3">{{ evGap.with_diag }} {{ t('ev_failed_with_diag') }}</span>
            </div>
            <div class="cd obv-evfilter">
              <div class="obv-evfilter-row">
                <span style="font-weight: 600">{{ t('events_title') }}</span>
                <a
                  v-for="sc in eventSources"
                  :key="'s' + sc.k"
                  :class="{ on: evSource === sc.k }"
                  class="obv-chip"
                  @click="pickEvSource(sc.k)"
                  >{{ sc.l }}</a
                >
                <span class="ts t3">|</span>
                <a
                  v-for="st in eventStates"
                  :key="'t' + st.k"
                  :class="{ on: evState === st.k }"
                  class="obv-chip"
                  @click="pickEvState(st.k)"
                  >{{ st.l }}</a
                >
                <input
                  v-model="evQ"
                  type="text"
                  class="obv-evq"
                  :placeholder="t('ev_filter_keyword')"
                  @keydown.enter.exact.prevent="eventReload()"
                />
                <button class="bt bt-o bt-s obv-evsearchbtn" @click="eventReload()">
                  {{ t('ev_search') }}
                </button>
                <span class="ts t3">{{
                  t('ev_count', { n: (eventsData.events || []).length })
                }}</span>
              </div>
              <div v-if="evNew.length" class="obv-evnew">
                <a @click="applyNewEvents()">{{ t('ev_new_events', { n: evNew.length }) }}</a>
              </div>
              <div
                v-if="(eventsData.truncated_sources || []).length"
                class="ts t3"
                style="margin-top: 6px"
              >
                {{
                  t('ev_truncated', {
                    sources: eventsData.truncated_sources.map((s) => t('source.' + s)).join('、'),
                  })
                }}
              </div>
              <div class="obv-evbody">
                <div v-if="eventsLoading" class="loading">{{ t('loading') }}</div>
                <div
                  v-else-if="!(eventsData.events || []).length"
                  class="ts t2"
                  style="padding: 20px; text-align: center"
                >
                  {{ t('no_events') }}
                </div>
                <template v-else>
                  <div
                    v-for="e in eventsData.events"
                    :key="e.key"
                    class="cd obv-evrow"
                    @click="toggleEventDetail(e.key)"
                  >
                    <div class="obv-evrow-main">
                      <span class="ts t3 obv-evtime">{{ alertTime(e.started_at) }}</span>
                      <span class="ts t3">{{ sourceLabel(e) }}</span>
                      <span v-html="eventBadge(e)"></span>
                      <span style="flex: 1">{{ eventTitle(e) }}</span>
                      <span class="ts t2">{{ eventStateLabel(e) }}</span>
                      <span class="ts t3 obv-evdur">{{
                        e.ts_kind === 'point'
                          ? ''
                          : durHuman(e.duration_sec) + (e.ongoing ? ' …' : '')
                      }}</span>
                    </div>
                    <div class="obv-evrow-sub">
                      <span class="ts t2">{{ eventTargetLabel(e) }}</span>
                      <span style="flex: 1"></span>
                      <span class="ts t2" v-html="eventActions(e)"></span>
                    </div>
                    <div v-if="openEventKeys.has(e.key)" class="obv-evdetail">
                      <div class="obv-evdetail-grid"><span v-html="eventDetailHtml(e)"></span></div>
                    </div>
                  </div>
                </template>
              </div>
              <div v-if="evMore" class="obv-evmore">
                <button class="bt bt-o bt-s" @click="loadEvents(true)">
                  {{ t('ev_load_more') }}
                </button>
              </div>
            </div>
          </template>

          <!-- errorlogs 子 Tab -->
          <template v-else-if="tab === 'errorlogs'">
            <div class="obv-logfilter">
              <div class="obv-logbtns">
                <button
                  :class="['bt bt-s', logLevel === '' ? 'bt-p' : 'bt-o']"
                  @click="setLogLevel('')"
                >
                  {{ t('log_all') }}
                </button>
                <button
                  :class="['bt bt-s', logLevel === 'ERROR' ? 'bt-p' : 'bt-o']"
                  @click="setLogLevel('ERROR')"
                >
                  {{ t('log_only_error') }}
                </button>
                <button
                  :class="['bt bt-s', logLevel === 'WARNING' ? 'bt-p' : 'bt-o']"
                  @click="setLogLevel('WARNING')"
                >
                  {{ t('log_only_warning') }}
                </button>
              </div>
              <select v-model="logSource" class="obv-logsel" @change="loadLogs()">
                <option value="">{{ t('log_all_sources') }}</option>
                <option v-for="s in logSources" :key="s" :value="s">{{ s }}</option>
              </select>
              <input
                v-model="logQ"
                type="text"
                class="obv-logq"
                :placeholder="t('log_search_placeholder')"
                @keydown.enter.exact.prevent="loadLogs()"
              />
              <button class="bt bt-o bt-s" @click="loadLogs()">{{ t('search') }}</button>
              <span class="ts t3">{{ logCountText }}</span>
              <span v-if="logTask" class="ts t2 obv-logtask">
                {{ t('log_task_filter') }}: {{ logTask }}
                <a style="cursor: pointer; color: var(--a)" @click="clearLogTask">✕</a>
              </span>
            </div>
            <div class="ts t3" style="margin-bottom: 10px">{{ t('log_scope_hint') }}</div>
            <div v-if="logsLoading" class="loading">{{ t('loading') }}</div>
            <div
              v-else-if="!logItems.length"
              class="ts t2"
              style="padding: 20px; text-align: center"
            >
              {{ t('no_matching_log') }}
            </div>
            <div v-else class="obv-logbox">
              <div v-for="(e, i) in logItems" :key="i" class="obv-logline">
                <span class="obv-logtime">{{
                  String(e.ts || '')
                    .replace('T', ' ')
                    .substring(0, 19)
                }}</span>
                <span v-if="e.source" class="obv-logsrc">[{{ e.source }}]</span>
                <span v-if="e.component" class="obv-logcomp">{{ e.component }}</span>
                <span v-if="shortRepo(e.repo_key)" class="obv-logctx" :title="e.repo_key">{{
                  shortRepo(e.repo_key)
                }}</span>
                <span v-if="e.task_id" class="obv-logctx">{{ e.task_id }}</span>
                <span :style="{ color: logLevelColor(e.level), fontWeight: 'bold' }">{{
                  e.level
                }}</span>
                <span class="obv-logmsg">{{ e.msg || e.message || '' }}</span>
              </div>
            </div>
          </template>
        </template>
      </div>

      <!-- ═══ 设备分区底部：资源曲线 ═══ -->
      <div v-if="section === 'devices'" class="obv-ressection">
        <div class="obv-reshead">
          <div class="ts t3">{{ t('res.title') }}</div>
          <div class="obv-restools">
            <select v-model="resRt" class="ip ip-s obv-ressel">
              <option value="">{{ t('res.all') }}</option>
              <option v-for="r in resRuntimes" :key="r.id" :value="String(r.id)">
                {{ workerDisplayName(r) }}
              </option>
            </select>
            <span class="stabs obv-rangbar">
              <a
                v-for="r in ['1h', '6h', '24h', '7d']"
                :key="r"
                :class="{ ac: resRange === r }"
                @click="pickResRange(r)"
                >{{ r }}</a
              >
            </span>
          </div>
        </div>
        <div :ref="(el) => setChartEl('chartRes', el)" class="obv-reschart"></div>
      </div>
    </div>

    <!-- ═══ 任务日志弹窗 ═══ -->
    <div v-if="taskLog.open" class="rtv-modal-ov" @click.self="closeTaskLog()">
      <div class="obv-logmodal">
        <div class="obv-lm-head">
          <h3 class="obv-lm-title">
            <template v-if="taskLog.info"
              ><span v-if="taskLog.info.repo" class="ts t3">{{ taskLog.info.repo }}</span>
              <b v-if="taskLog.info.issue">{{ taskLog.info.issue }}</b>
              <span v-if="taskLog.info.seq" class="ts">#{{ taskLog.info.seq }}</span></template
            >
            <template v-else>{{ t('task_log') }}</template>
          </h3>
          <div class="obv-lm-tools">
            <span v-if="taskLog.live" class="obv-lm-live">{{ t('live') }}</span>
            <button class="bt bt-o bt-s" @click="openTimeline(taskLog.taskId)">
              {{ t('timeline') }}
            </button>
            <button class="bt bt-o bt-s" @click="closeTaskLog()">{{ t('close') }}</button>
          </div>
        </div>
        <div
          v-if="taskLog.timelineHtml"
          class="obv-lm-timeline"
          v-html="taskLog.timelineHtml"
        ></div>
        <div class="obv-lm-body">
          <div v-if="taskLog.loading" class="loading">{{ t('loading_logs') }}</div>
          <div v-else class="obv-logbox">
            <div v-for="(m, i) in taskLog.msgs" :key="i" class="obv-logline">
              <span class="obv-logtime">{{ fmtLocal(m.created_at || '').substring(11, 19) }}</span>
              <span :style="{ color: msgTypeColor(m.type) }">{{ msgTypeIcon(m.type) }}</span>
              <span v-if="m.type === 'tool_use'"
                ><b :style="{ color: msgTypeColor(m.type) }">{{ m.tool || 'tool' }}</b>
                <span class="obv-logsub">{{ (m.content || '').substring(0, 300) }}</span></span
              >
              <span v-else :style="{ color: msgTypeColor(m.type) }">{{
                (m.content || '').substring(0, 800)
              }}</span>
            </div>
            <div v-if="!taskLog.msgs.length" class="obv-logempty">{{ t('no_exec_log') }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══ 追踪时间线弹窗 ═══ -->
    <div v-if="traceModal.open" class="rtv-modal-ov" @click.self="traceModal.open = false">
      <div class="obv-tracemodal">
        <div class="obv-lm-head">
          <h3 class="obv-lm-title">{{ t('timeline_title') }}</h3>
          <button class="bt bt-o bt-s" @click="traceModal.open = false">{{ t('close') }}</button>
        </div>
        <div class="obv-lm-body">
          <div v-if="traceModal.loading" class="loading">{{ t('loading') }}</div>
          <div v-else-if="traceModal.error" class="error">{{ traceModal.error }}</div>
          <template v-else>
            <div class="ts t3 obv-tracemeta">{{ traceMeta }}</div>
            <div
              v-if="!traceModal.tasks.length"
              class="ts t2"
              style="padding: 24px; text-align: center"
            >
              {{ t('timeline_empty') }}
            </div>
            <div
              v-for="(tk, i) in traceModal.tasks"
              :key="i"
              class="cd obv-tracecard"
              :style="{ borderLeftColor: traceColor(tk) }"
            >
              <div class="obv-trace-head">
                <b>{{ tk.agent_type || '-' }}</b>
                <span class="ts t3">{{ tk.engine || '#' + tk.runtime_id }}</span>
                <span class="ts" :style="{ color: traceColor(tk) }">{{ tk.status }}</span>
                <span class="ts t3"
                  >{{ t('timeline_queue_wait') }} {{ fmtDur(tk.queue_wait_s) }} ·
                  {{ t('timeline_duration') }} {{ fmtDur(tk.duration_s) }}</span
                >
                <span v-if="tk.tokens && tk.tokens.total" class="ts t3"
                  >tokens {{ tk.tokens.total.toLocaleString() }}</span
                >
                <a class="obv-trace-loglink" @click="openTaskLog(tk.id, {})">{{
                  t('view_logs')
                }}</a>
              </div>
              <div class="ts t3 obv-trace-track">
                {{ (tk.enqueue_at || '').replace('T', ' ').substring(0, 19) }} →
                {{ (tk.dispatch_at || '').replace('T', ' ').substring(0, 19) }} →
                {{ (tk.terminal_at || '…').replace('T', ' ').substring(0, 19) }}
              </div>
              <div v-if="isTraceBad(tk) && tk.failure_reason" class="obv-trace-fail">
                {{ t('timeline_failed') }}: {{ tk.failure_reason }}
              </div>
              <details v-if="(tk.steps || []).length" class="obv-trace-steps">
                <summary class="ts t2">{{ t('timeline_steps') }} ({{ tk.steps.length }})</summary>
                <div class="obv-logbox obv-trace-stepbox">
                  <div v-for="(st, j) in tk.steps" :key="j" class="obv-logline">
                    <span class="obv-logtime">#{{ st.seq }}</span>
                    <span :style="{ color: stepColor(st.type) }"
                      >{{ st.type }}{{ st.tool ? ' ' + st.tool : '' }}</span
                    >
                    <span class="obv-logmsg">{{ st.content || st.output || '' }}</span>
                  </div>
                </div>
              </details>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- ═══ 异常任务弹窗 ═══ -->
    <div v-if="anomalyModal.open" class="rtv-modal-ov" @click.self="anomalyModal.open = false">
      <div class="obv-anomalymodal">
        <div class="obv-lm-head">
          <h3 class="obv-lm-title">{{ t('tab.anomaly') }}</h3>
          <button class="bt bt-o bt-s" @click="anomalyModal.open = false">{{ t('close') }}</button>
        </div>
        <div class="obv-lm-body">
          <div v-if="anomalyModal.loading" class="loading">{{ t('loading_task') }}</div>
          <div
            v-else-if="!anomalyModal.stuck.length && !anomalyModal.failed.length"
            class="ts t2"
            style="padding: 40px; text-align: center"
          >
            {{ t('no_anomaly') }}
          </div>
          <template v-else>
            <div v-if="anomalyModal.stuck.length" class="obv-sec">
              <h2>{{ t('stuck_tasks') }}</h2>
              <span class="obv-sec-sub">{{
                t('stuck_tasks_subtitle', { count: anomalyModal.stuck.length })
              }}</span>
            </div>
            <div v-if="anomalyModal.stuck.length" class="tw">
              <table>
                <thead>
                  <tr>
                    <th>Issue/Seq</th>
                    <th>Agent</th>
                    <th>Repo</th>
                    <th>Started</th>
                    <th>{{ t('col_status') }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="s in anomalyModal.stuck"
                    :key="s.id"
                    class="obv-rtrow"
                    @click="
                      openTaskLog(s.id, {
                        issue: s.issue_id,
                        seq: s.event_seq,
                        created: s.started_at || '',
                      })
                    "
                  >
                    <td>
                      <b>{{ anomalyIdStr(s) }}</b>
                    </td>
                    <td>{{ s.agent_type || '-' }}</td>
                    <td class="ts t3">{{ shortRepo(s.repo_key).substring(0, 20) }}</td>
                    <td class="ts t3">{{ fmtLocal(s.started_at || '').substring(0, 19) }}</td>
                    <td><span style="color: var(--r)">🔴 STUCK</span></td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="obv-sec">
              <h2>{{ t('failed_tasks') }}</h2>
              <span class="obv-sec-sub">{{
                t('failed_tasks_subtitle', {
                  total: anomalyModal.failed.length,
                  page: anomalyPage + 1,
                  pages: anomalyTotalPages,
                })
              }}</span>
            </div>
            <div v-if="anomalyPageItems.length" class="tw">
              <table>
                <thead>
                  <tr>
                    <th>{{ t('col_issue_seq') }}</th>
                    <th>{{ t('col_agent') }}</th>
                    <th>{{ t('col_repo') }}</th>
                    <th>{{ t('col_failed_short') }}</th>
                    <th>{{ t('col_result') }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="s in anomalyPageItems"
                    :key="s.id"
                    class="obv-rtrow"
                    @click="
                      openTaskLog(s.id, {
                        issue: s.issue_id,
                        seq: s.event_seq,
                        created: s.completed_at || s.started_at || '',
                      })
                    "
                  >
                    <td>
                      <b>{{ anomalyIdStr(s) }}</b>
                    </td>
                    <td>{{ s.agent_type || '-' }}</td>
                    <td class="ts t3">{{ shortRepo(s.repo_key).substring(0, 20) }}</td>
                    <td class="ts t3">
                      {{ fmtLocal(s.completed_at || s.started_at || '').substring(0, 19) }}
                    </td>
                    <td>
                      <span class="ts t3" style="color: var(--r)"
                        >{{ (s.result_text || (s.errors || [])[0] || '' || '').substring(0, 80)
                        }}{{ s.exit_code != null ? ' [exit:' + s.exit_code + ']' : '' }}</span
                      >
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="obv-pager">
              <button class="bt bt-o bt-s" :disabled="anomalyPage === 0" @click="anomalyPage--">
                {{ t('prev_page') }}
              </button>
              <span class="ts t3">{{ anomalyPage + 1 }} / {{ anomalyTotalPages }}</span>
              <button
                class="bt bt-o bt-s"
                :disabled="anomalyPage >= anomalyTotalPages - 1"
                @click="anomalyPage++"
              >
                {{ t('next_page') }}
              </button>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- ═══ Session 列表弹窗 ═══ -->
    <div v-if="sessionModal.open" class="rtv-modal-ov" @click.self="sessionModal.open = false">
      <div class="obv-sessionmodal">
        <div class="obv-lm-head">
          <h3 class="obv-lm-title">{{ sessionModal.name }} · {{ t('session_list') }}</h3>
          <button class="bt bt-o bt-s" @click="sessionModal.open = false">{{ t('close') }}</button>
        </div>
        <div class="obv-lm-body">
          <div v-if="sessionModal.loading" class="loading">{{ t('loading_session') }}</div>
          <div
            v-else-if="!sessionModal.sessions.length"
            class="ts t2"
            style="padding: 20px; text-align: center"
          >
            {{ t('no_session') }}
          </div>
          <template v-else>
            <div class="obv-sessiongrp">
              <div class="obv-sessiongrp-head" @click="sessionRunningOpen = !sessionRunningOpen">
                <span class="obv-issue-arrow">{{ sessionRunningOpen ? '▾' : '▸' }}</span>
                <b>{{ t('running') }}</b>
                <span class="ts t3" style="margin-left: auto">{{
                  t('session_count', { count: sessionRunning.length })
                }}</span>
              </div>
              <div v-if="sessionRunningOpen" class="obv-sessiongrp-body">
                <div v-if="!sessionRunning.length" class="ts t3" style="padding: 8px">
                  {{ t('no_running_session') }}
                </div>
                <div v-else class="tw">
                  <table>
                    <thead>
                      <tr>
                        <th>Agent</th>
                        <th>Issue / Seq</th>
                        <th>Skill</th>
                        <th>{{ t('col_status') }}</th>
                        <th>{{ t('col_start_time') }}</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr
                        v-for="s in sessionRunning"
                        :key="s.id"
                        class="obv-rtrow"
                        @click="
                          openTaskLog(s.current_task.id, {
                            issue: s.current_task.issue_id || '',
                            seq: s.current_task.event_seq || 0,
                          })
                        "
                      >
                        <td>
                          <b>{{ s.agent_type || '-' }}</b>
                        </td>
                        <td class="ts">
                          <b>{{ sessionTaskIdStr(s.current_task) }}</b>
                        </td>
                        <td class="ts t3">{{ s.skill || '-' }}</td>
                        <td>
                          <span :style="{ color: sessionStatusColor(s.current_task.status) }"
                            >{{ sessionStatusIcon(s.current_task.status) }}
                            {{ s.current_task.status }}</span
                          >
                        </td>
                        <td class="ts t3">
                          {{ fmtLocal(s.current_task.started_at || '').substring(0, 16) }}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
            <div class="obv-sessiongrp">
              <div class="obv-sessiongrp-head" @click="sessionIdleOpen = !sessionIdleOpen">
                <span class="obv-issue-arrow">{{ sessionIdleOpen ? '▾' : '▸' }}</span>
                <b>{{ t('completed_tasks') }}</b>
                <span class="ts t3" style="margin-left: auto">{{
                  t('task_count', { count: sessionCompletedCount })
                }}</span>
              </div>
              <div v-if="sessionIdleOpen" class="obv-sessiongrp-body">
                <div v-if="!sessionCompletedTasks.length" class="ts t3" style="padding: 8px">
                  {{ t('no_completed_tasks') }}
                </div>
                <div v-else class="tw">
                  <table>
                    <thead>
                      <tr>
                        <th>{{ t('col_issue_seq') }}</th>
                        <th>{{ t('col_agent') }}</th>
                        <th>{{ t('col_status') }}</th>
                        <th>{{ t('col_token') }}</th>
                        <th>{{ t('col_complete_time') }}</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr
                        v-for="tk in sessionCompletedTasks"
                        :key="tk.id"
                        class="obv-rtrow"
                        @click="
                          openTaskLog(tk.id, { issue: tk.issue_id || '', seq: tk.event_seq || 0 })
                        "
                      >
                        <td class="ts">
                          <b>{{ sessionTaskIdStr(tk) }}</b>
                        </td>
                        <td>
                          <b>{{ tk.agent_type || '-' }}</b>
                        </td>
                        <td>
                          <span :style="{ color: sessionStatusColor(tk.status) }"
                            >{{ sessionStatusIcon(tk.status) }} {{ tk.status }}</span
                          >
                        </td>
                        <td class="ts t2">{{ tokForTask(tk.id) }}</td>
                        <td class="ts t3">
                          {{ fmtLocal(tk.completed_at || '').substring(0, 16) }}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- ═══ 告警面板弹窗 ═══ -->
    <div v-if="alertPanel.open" class="rtv-modal-ov" @click.self="alertPanel.open = false">
      <div class="obv-alertmodal">
        <div class="obv-lm-head">
          <h3 class="obv-lm-title">{{ t('alert_panel') }}</h3>
          <div class="obv-lm-tools">
            <span class="ts t3">{{ alertNotifyResult }}</span>
            <button class="bt bt-o bt-s" @click="testNotify()">{{ t('test_notify') }}</button>
            <button class="bt bt-o bt-s" @click="alertPanel.open = false">{{ t('close') }}</button>
          </div>
        </div>
        <div class="stabs obv-subtabs">
          <a
            v-for="v in ['events', 'rules', 'silences']"
            :key="v"
            :class="{ ac: alertPanelView === v }"
            @click="alertPanelView = v"
            >{{ t('alert_tab.' + v) }}</a
          >
        </div>
        <div class="obv-lm-body">
          <div v-if="alertPanelView === 'rules'">
            <div v-if="!alertRules.length" class="ts t2" style="padding: 20px; text-align: center">
              {{ t('no_data') }}
            </div>
            <div v-else class="tw">
              <table>
                <thead>
                  <tr>
                    <th>{{ t('rule_col.name') }}</th>
                    <th>{{ t('rule_col.metric') }}</th>
                    <th>{{ t('rule_col.threshold') }}</th>
                    <th>{{ t('rule_col.for_sec') }}</th>
                    <th>{{ t('rule_col.severity') }}</th>
                    <th>{{ t('rule_col.enabled') }}</th>
                    <th>{{ t('rule_col.notify') }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="r in alertRules" :key="r.id">
                    <td>{{ t('rule.' + r.name) }}</td>
                    <td class="ts t2">{{ r.metric }}</td>
                    <td>
                      {{ r.threshold }}
                      <a class="obv-linka" @click="setThreshold(r)">{{ t('edit') }}</a>
                    </td>
                    <td class="ts t2">{{ r.for_sec }}s</td>
                    <td class="ts t2">{{ severityText(r) }}</td>
                    <td>
                      <a class="obv-linka" @click="toggleRule(r)">{{
                        Number(r.enabled) ? t('on') : t('off')
                      }}</a>
                    </td>
                    <td>
                      {{ chanLabel(r.notify_channels) }}
                      <a class="obv-linka" @click="setNotify(r)">{{ t('edit') }}</a>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="ts t3" style="margin-top: 10px">{{ t('alert_rules_hint') }}</div>
          </div>
          <div v-else-if="alertPanelView === 'silences'">
            <div
              v-if="!alertSilences.length"
              class="ts t2"
              style="padding: 20px; text-align: center"
            >
              {{ t('no_silence') }}
            </div>
            <div v-else class="tw">
              <table>
                <thead>
                  <tr>
                    <th>{{ t('silence_col.rule') }}</th>
                    <th>{{ t('silence_col.repo') }}</th>
                    <th>{{ t('silence_col.until') }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="e in alertSilences" :key="e.id">
                    <td>{{ t('rule.' + e.rule_name) }}</td>
                    <td class="ts t2">{{ shortRepo(e.repo_key) }}</td>
                    <td class="ts t2">{{ alertTime(e.silenced_until) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
          <div v-else>
            <div v-if="!alertEvents.length" class="ts t2" style="padding: 20px; text-align: center">
              {{ t('no_alerts') }}
            </div>
            <div v-else class="tw">
              <table>
                <thead>
                  <tr>
                    <th>{{ t('event_col.rule') }}</th>
                    <th>{{ t('event_col.severity') }}</th>
                    <th>{{ t('event_col.source') }}</th>
                    <th>{{ t('event_col.repo') }}</th>
                    <th>{{ t('event_col.value') }}</th>
                    <th>{{ t('event_col.status') }}</th>
                    <th>{{ t('event_col.last_seen') }}</th>
                    <th>{{ t('event_col.op') }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="e in alertEvents" :key="e.id">
                    <td>
                      {{ t('rule.' + e.rule_name)
                      }}<span v-if="isSilenced(e)" class="ts t3">({{ t('silenced') }})</span>
                    </td>
                    <td class="ts t2">{{ severityText(e) }}</td>
                    <td class="ts t3">{{ sourceLabel(e) }}</td>
                    <td class="ts t2">{{ shortRepo(e.repo_key) || eventDimTarget(e) }}</td>
                    <td>
                      {{ metricVal(e)
                      }}<span v-if="!String(e.metric || '').startsWith('gap_')" class="ts t3">
                        / {{ e.threshold }}</span
                      >
                    </td>
                    <td class="ts t2">{{ t('status.' + (e.status || '')) }}</td>
                    <td class="ts t2">{{ alertTime(e.last_seen_at) }}</td>
                    <td>
                      <a v-if="e.status === 'firing'" class="obv-linka" @click="silence(e)">{{
                        t('silence')
                      }}</a
                      ><span v-else>—</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'

const G = globalThis

// ═══ 常量 ═══
const HEALTH_DOT = {
  online: '🟢',
  idle: '🟢',
  normal: '🟢',
  busy: '🟡',
  overload: '🔴',
  recently_lost: '🟡',
  offline: '⚫',
  about_to_gc: '🔴',
}
const HEALTH_COLOR = {
  online: 'var(--g)',
  idle: 'var(--g)',
  normal: 'var(--g)',
  busy: 'var(--y)',
  overload: 'var(--r)',
  recently_lost: 'var(--y)',
  offline: 'var(--t3)',
  about_to_gc: 'var(--r)',
}
const SESSION_ICON = {
  completed: '✅',
  failed: '❌',
  cancelled: '⛔',
  running: '🔄',
  queued: '⏳',
  dispatched: '🔄',
}
const SESSION_COLOR = {
  completed: 'var(--g)',
  failed: 'var(--r)',
  cancelled: 'var(--t3)',
  running: 'var(--a)',
  queued: 'var(--y)',
  dispatched: 'var(--a)',
}
const MSG_ICON = {
  step_start: '▶',
  step_finish: '✓',
  text: '💬',
  reasoning: '🤔',
  tool_use: '🔧',
  error: '❌',
  done: '✅',
}
const MSG_COLOR = {
  step_start: '#5b9eff',
  step_finish: '#1ca44c',
  text: '#e0e6f0',
  reasoning: '#b388ff',
  tool_use: '#ea5a0c',
  error: '#dc2828',
  done: '#1ca44c',
}
const EVENT_SOURCES = [
  ['', 'ev_all'],
  ['monitor', 'source.monitor'],
  ['gap', 'source.gap'],
  ['mutation', 'source.mutation'],
  ['task', 'source.task'],
  ['change', 'source.change'],
]
const EVENT_STATES = [
  ['', 'ev_all_state'],
  ['firing', 'state.firing'],
  ['resolved', 'state.resolved'],
  ['failed', 'state.failed'],
  ['stuck', 'state.stuck'],
  ['done', 'state.done'],
]

// ═══ props / 状态 ═══
const props = defineProps({ initialSection: { type: String, default: 'overview' } })
const section = ref('overview')
const tab = ref('')
const lastUpdate = ref('-')
const autoOn = ref(true)
let obsTimer = null
// 总览
const ovRange = ref('24h')
const ovLoading = ref(false)
const ovError = ref('')
const ovKpis = ref({})
const ovScope = ref(null)
const ovCharts = ref({})
const ovData = ref(null)
let chartRefs = {}
// 设备
const opsData = ref(null)
const rtData = ref(null)
const workerPage = ref(0)
const workerPageSize = ref(10)
const runtimePage = ref(0)
const runtimePageSize = ref(10)
const rtSearch = ref('')
const rtDeviceFilter = ref('')
// 资源曲线
const resRange = ref('6h')
const resRt = ref('')
const resData = ref(null)
// Token 列
const tokEngines = ref({})
const tokTasks = ref({})
let tokColAt = 0
// 任务列表
const timelineLoading = ref(false)
const timelineError = ref('')
const timelineIssues = ref([])
const timelineSearch = ref('')
const timelinePage = ref(0)
const timelinePageSize = ref(10)
const expandedIssues = reactive(new Set())
const issueViewModes = reactive({})
let timelineCache = null
let diagramRefs = {}
const mermaidRendered = reactive({})
// 事件视图
const eventsData = ref({})
const eventsLoading = ref(false)
const evSource = ref('')
const evState = ref('')
const evQ = ref('')
const evCursor = ref(null)
const evMore = ref(false)
const evNew = ref([])
const openEventKeys = reactive(new Set())
let evLastSoft = 0
// 错误日志
const logLevel = ref('')
const logSource = ref('')
const logQ = ref('')
const logTask = ref('')
const logItems = ref([])
const logSources = ref([])
const logsLoading = ref(false)
const logCountText = ref('')
// 弹窗
const taskLog = reactive({
  open: false,
  taskId: '',
  info: null,
  msgs: [],
  loading: false,
  live: false,
  timelineHtml: '',
})
let taskLogTimer = null
const traceModal = reactive({ open: false, loading: false, error: '', tasks: [], meta: '' })
const anomalyModal = reactive({ open: false, loading: false, stuck: [], failed: [] })
const anomalyPage = ref(0)
const sessionModal = reactive({ open: false, loading: false, name: '', rtId: 0, sessions: [] })
const sessionRunningOpen = ref(true)
const sessionIdleOpen = ref(false)
const alertPanel = reactive({ open: false })
const alertPanelView = ref('events')
const alertNotifyResult = ref('')
// SLO
const sloData = ref(null)

// ═══ 工具 ═══
const API = () => G.API
const app = () => G.app
function t(key, vars) {
  const v = G.I18N.t('observability.' + key, vars)
  return v && v !== 'observability.' + key ? v : key
}
// Issue #701：worker/设备显示名唯一来源 runtime_credentials.name，空名统一占位「未命名设备」
function workerDisplayName(wk) {
  const n = String((wk && wk.name) || '').trim()
  if (n) return n
  const v = G.I18N.t('common.unnamed_worker')
  return v && v !== 'common.unnamed_worker' ? v : '未命名设备'
}
// t() 缺键回退（老代码 _obsTOr 同款语义：值===key 视为未命中）
function tOr(key, fallback) {
  const v = G.I18N.t(key, {})
  return typeof v === 'string' && v !== key ? v : fallback
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}
function fmtLocal(s) {
  return G.fmtLocalTime ? G.fmtLocalTime(s) : String(s || '')
}
function shortRepo(rk) {
  const s = String(rk || '')
  const i = s.indexOf(':')
  return i >= 0 ? s.slice(i + 1) : s
}
function currentRepo() {
  return app().currentRepo ? app().currentRepo() : null
}
function repoFull() {
  const r = currentRepo()
  return r && r.fullName ? String(r.fullName) : ''
}
function alertTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d.getTime()) ? String(iso) : d.toLocaleString('zh-CN', { hour12: false })
}
function durHuman(sec) {
  const s = Number(sec)
  if (!isFinite(s) || s <= 0) return '—'
  if (s < 60) return s + 's'
  if (s < 3600) return Math.round(s / 60) + t('dur_unit_min')
  if (s < 86400) return Math.round(s / 360) / 10 + t('dur_unit_hour')
  return Math.round(s / 8640) / 10 + t('dur_unit_day')
}
function fmtDur(v) {
  if (v === null || v === undefined) return '—'
  return v >= 60
    ? Math.floor(v / 60) + 'm' + Math.round(v % 60) + 's'
    : Math.round(v * 10) / 10 + 's'
}
function tokNum(v) {
  const n = Number(v || 0)
  if (!isFinite(n) || n <= 0) return '—'
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k'
  return String(Math.round(n))
}
function healthDot(h) {
  return HEALTH_DOT[h] || '⚫'
}
function healthColor(h) {
  return HEALTH_COLOR[h] || 'var(--t3)'
}
function barColor(pct) {
  return pct > 80 ? 'var(--r)' : pct > 60 ? 'var(--y)' : 'var(--g)'
}

// ═══ Tab 结构 ═══
const subTabs = computed(() =>
  section.value === 'inspect'
    ? ['tasks', 'events', 'errorlogs']
    : section.value === 'devices'
      ? ['overview']
      : []
)
function switchSection(v) {
  if (v === section.value) return
  section.value = v
  tab.value = subTabs.value[0] || ''
  workerPage.value = 0
  runtimePage.value = 0
  timelinePage.value = 0
  stopAuto()
  if (v === 'overview') {
    loadOverview()
  } else {
    refreshObs()
    if (autoOn.value) startAuto()
    if (v === 'devices') loadResHistory()
  }
}
function switchTab(tb) {
  tab.value = tb
  timelinePage.value = 0
  // 用缓存数据渲染（不重新 fetch）——Vue 里 computed 自动联动，只需触发 tab 专属异步加载
  if (tb === 'errorlogs') loadLogs()
  else if (tb === 'events') {
    eventReload()
    loadEventGap()
  } else if (tb === 'tasks' && !timelineIssues.value.length) loadTimeline()
}
// ── Issue #476：跨仓失败空结果占比（异步摘要卡，失败静默） ──
const evGap = ref(null)
function loadEventGap() {
  G.API.get('/obs/events/failed-result-gap?limit=5')
    .then((d) => {
      evGap.value = d && d.total_failed ? d : null
    })
    .catch(() => {
      evGap.value = null
    })
}
function switchOvRange(r) {
  ovRange.value = r
  loadOverview()
}
function toggleAuto() {
  if (autoOn.value) startAuto()
  else stopAuto()
}
function startAuto() {
  stopAuto()
  obsTimer = setInterval(() => refreshObs(true), 10000)
}
function setWorkerPageSize(e) {
  workerPageSize.value = Number(e.target.value) || 10
  workerPage.value = 0
}
function setRuntimePageSize(e) {
  runtimePageSize.value = Number(e.target.value) || 10
  runtimePage.value = 0
}
function stopAuto() {
  if (obsTimer) {
    clearInterval(obsTimer)
    obsTimer = null
  }
  G.window._stopObsAutoRan = true
}
// app.js 离开 observability 时调（typeof 守卫契约）
G.window.stopObsAuto = stopAuto

// ═══ 总览 ═══
function loadOverview() {
  ovLoading.value = true
  ovError.value = ''
  const full = repoFull()
  const qs =
    (full ? 'repo=' + encodeURIComponent(full) + '&' : '') +
    'range=' +
    encodeURIComponent(ovRange.value)
  Promise.all([
    API()
      .get('/obs/overview?' + qs)
      .catch(() => null),
    API()
      .get('/obs/alerts?' + qs)
      .catch(() => null),
  ])
    .then(function (results) {
      if (G.window._currentView !== 'observability' || section.value !== 'overview') return
      const d = results[0] || {}
      ovData.value = d
      ovKpis.value = d.kpis || {}
      ovScope.value = d.scope || null
      ovCharts.value = d.charts || {}
      ovLoading.value = false
      lastUpdate.value = new Date().toLocaleTimeString('zh-CN')
      loadSlo()
      // 图表渲染（nextTick 等 DOM 就位）
      nextTick(function () {
        renderOverviewCharts()
      })
    })
    .catch(function (e) {
      ovLoading.value = false
      ovError.value = t('load_failed', { msg: e.message || e })
    })
}
function renderOverviewCharts() {
  const C = ovCharts.value || {}
  const echarts = G.echarts
  const mkChart = function (el, option) {
    if (!el) return
    if (!echarts) {
      el.innerHTML =
        '<div class="ts t3" style="padding: 20px 0">' + t('ov.charts_unavailable') + '</div>'
      return
    }
    try {
      const ch = echarts.init(el)
      ch.setOption(option)
      chartRefs[el.className + Math.random()] = ch
    } catch (e) {
      el.innerHTML = '<div class="ts t3" style="padding: 20px 0">' + t('ov.chart_failed') + '</div>'
    }
  }
  const fmtAxis = function (iso) {
    const d = new Date(iso)
    if (isNaN(d.getTime())) return ''
    const hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0')
    return ovRange.value === '7d' ? d.getMonth() + 1 + '/' + d.getDate() + ' ' + hm : hm
  }
  const to = C.task_outcome || {}
  const labels = (to.buckets || []).map(fmtAxis)
  const axis = { axisLabel: { fontSize: 10 }, axisLine: { lineStyle: { color: '#888' } } }
  const grid = { left: 42, right: 46, top: 28, bottom: 24 }
  const legend = { top: 0, right: 0, itemWidth: 10, itemHeight: 8, textStyle: { fontSize: 11 } }
  mkChart(chartRefsEl('chartOutcome'), {
    tooltip: { trigger: 'axis' },
    legend: Object.assign(
      { data: [t('ov.s_completed'), t('ov.s_failed'), t('ov.s_cancelled'), t('ov.s_rate')] },
      legend
    ),
    grid: grid,
    xAxis: Object.assign({ type: 'category', data: labels }, axis),
    yAxis: [
      { type: 'value', minInterval: 1, axisLabel: { fontSize: 10 } },
      {
        type: 'value',
        min: 0,
        max: 100,
        splitLine: { show: false },
        axisLabel: { fontSize: 10, formatter: '{value}%' },
      },
    ],
    series: [
      {
        name: t('ov.s_completed'),
        type: 'bar',
        stack: 'o',
        barMaxWidth: 18,
        itemStyle: { color: '#22a06b' },
        data: to.completed || [],
      },
      {
        name: t('ov.s_failed'),
        type: 'bar',
        stack: 'o',
        barMaxWidth: 18,
        itemStyle: { color: '#d64545' },
        data: to.failed || [],
      },
      {
        name: t('ov.s_cancelled'),
        type: 'bar',
        stack: 'o',
        barMaxWidth: 18,
        itemStyle: { color: '#e0a02a' },
        data: to.cancelled || [],
      },
      {
        name: t('ov.s_rate'),
        type: 'line',
        yAxisIndex: 1,
        connectNulls: true,
        symbolSize: 5,
        itemStyle: { color: '#4a7fd1' },
        lineStyle: { width: 2 },
        data: to.rate || [],
      },
    ],
  })
  const tt = C.token_trend || {}
  mkChart(chartRefsEl('chartToken'), {
    tooltip: { trigger: 'axis' },
    legend: Object.assign({ data: [t('ov.s_input'), t('ov.s_output'), t('ov.s_cache')] }, legend),
    grid: Object.assign({}, grid, { top: 26 }),
    xAxis: Object.assign({ type: 'category', data: (tt.buckets || []).map(fmtAxis) }, axis),
    yAxis: [{ type: 'value', axisLabel: { fontSize: 10 } }],
    series: [
      {
        name: t('ov.s_input'),
        type: 'bar',
        stack: 't',
        barMaxWidth: 18,
        itemStyle: { color: '#4a7fd1' },
        data: tt.input || [],
      },
      {
        name: t('ov.s_output'),
        type: 'bar',
        stack: 't',
        barMaxWidth: 18,
        itemStyle: { color: '#22a06b' },
        data: tt.output || [],
      },
      {
        name: t('ov.s_cache'),
        type: 'bar',
        stack: 't',
        barMaxWidth: 18,
        itemStyle: { color: '#8b8b8b' },
        data: tt.cache || [],
      },
    ],
  })
  const ms = (C.model_share || []).slice().reverse()
  mkChart(chartRefsEl('chartModel'), {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: function (p) {
        const it = p[0]
        const m = ms[it.dataIndex] || {}
        return (
          (m.model || '') +
          '<br/>' +
          (m.tokens || 0).toLocaleString() +
          ' tokens · ' +
          (m.share || 0) +
          '%'
        )
      },
    },
    grid: { left: 90, right: 24, top: 10, bottom: 20 },
    xAxis: { type: 'value', axisLabel: { fontSize: 10 } },
    yAxis: {
      type: 'category',
      data: ms.map((m) => m.model),
      axisLabel: { fontSize: 10, width: 84, overflow: 'truncate' },
    },
    series: [
      {
        type: 'bar',
        barMaxWidth: 16,
        itemStyle: { color: '#4a7fd1' },
        data: ms.map((m) => m.tokens),
      },
    ],
  })
}
// 模板 ref 收集（图表容器）
const chartEls = {}
function chartRefsEl(name) {
  return chartEls[name]
}
function setChartEl(name, el) {
  if (el) chartEls[name] = el
}
// SLO 卡
function loadSlo() {
  const full = repoFull()
  API()
    .get('/obs/slo' + (full ? '?repo=' + encodeURIComponent(full) : ''))
    .then(function (d) {
      sloData.value = d || { slos: [] }
    })
    .catch(function () {})
}
const sloCardHtml = computed(() => {
  const list = (sloData.value || {}).slos || []
  if (!list.length) return ''
  const cards = list
    .map(function (s) {
      const st = String(s.state || '')
      const color =
        st === 'breached'
          ? 'var(--r)'
          : st === 'warning'
            ? '#d97706'
            : st === 'no_data'
              ? 'var(--t3)'
              : 'var(--g)'
      const show = function (v, suffix) {
        return v === null || v === undefined ? '—' : Math.round(Number(v) * 10) / 10 + suffix
      }
      return (
        '<div class="obv-slocard" style="border-left:3px solid ' +
        color +
        '">' +
        '<div class="ts t3">' +
        esc(tOr('observability.slo_name.' + s.name, s.name)) +
        '</div>' +
        '<div class="obv-slov" style="color:' +
        color +
        '">' +
        show(s.value === null || s.value === undefined ? null : s.value * 100, '%') +
        ' <span class="ts t3 obv-slotgt">/ ' +
        t('slo_target') +
        ' ' +
        show((s.target || 0) * 100, '%') +
        '</span></div>' +
        '<div class="ts t2">' +
        tOr('observability.slo_state.' + st, st || '—') +
        ' · ' +
        t('slo_budget_left') +
        ' ' +
        show(s.budget_remaining_pct, '%') +
        '</div>' +
        '</div>'
      )
    })
    .join('')
  return (
    '<div class="cd obv-slowrap"><div class="obv-slohead"><span style="font-weight:600">' +
    t('slo_title') +
    '</span><span class="ts t3">' +
    t('slo_hint') +
    '</span></div><div class="obv-slogrid">' +
    cards +
    '</div></div>'
  )
})
// 告警横幅
const bannerHtml = computed(() => {
  const a = (ovData.value || {}).alerts || alertsCache
  const s = (a && a.summary) || {}
  const n = s.firing || 0
  if (!n) {
    return (
      '<div class="cd obv-banner-ok"><span style="color:#22a06b;font-weight:600">✔</span><span class="ts t2">' +
      t('banner.ok') +
      '</span></div>'
    )
  }
  const crit = s.critical || 0
  const color = crit ? '#d64545' : '#e0a02a'
  const lt = s.latest || {}
  const detail = lt.rule_name
    ? t('rule.' + lt.rule_name) + ' · ' + shortRepo(lt.repo_key) + ' · ' + metricVal(lt)
    : ''
  return (
    '<div class="cd obv-banner-warn" style="border-left:3px solid ' +
    color +
    '"><span style="color:' +
    color +
    ';font-weight:600;white-space:nowrap">⚠ ' +
    t('banner.firing', { n: n }) +
    '</span><span class="ts t2 obv-banner-detail">' +
    esc(detail) +
    '</span><button class="bt bt-o bt-s obv-banner-btn" onclick="window.__obvOpenAlerts()">' +
    t('banner.view') +
    '</button></div>'
  )
})
let alertsCache = null
G.window.__obvOpenAlerts = function () {
  alertPanel.value_open = true
  openAlertPanel()
}

// ═══ 设备分区数据 ═══
function refreshObs(isAuto) {
  const repo = currentRepo()
  const repoParam = repo && repo.fullName ? '?repo=' + encodeURIComponent(repo.fullName) : ''
  if (!isAuto) {
    API()
      .get('/dashboard/ops/probe-now')
      .catch(() => null)
  }
  Promise.all([
    API()
      .get('/dashboard/ops/overview' + repoParam)
      .catch(() => null),
    G.fetch('/health/realtime', {
      signal: G.AbortSignal ? G.AbortSignal.timeout(10000) : undefined,
    })
      .then((r) => r.json())
      .catch(() => null),
  ]).then(function (results) {
    const [ops, realtime] = results
    opsData.value = ops
    rtData.value = realtime
    lastUpdate.value = new Date().toLocaleTimeString('zh-CN')
    if (section.value === 'devices' || tab.value === 'tasks') loadTokCols()
    if (isAuto && section.value === 'inspect' && tab.value === 'events') eventSoftRefresh()
  })
}
const realOps = computed(() => {
  const ops = opsData.value
  if (ops && !ops.error) return ops
  return fallbackOps(rtData.value)
})
function fallbackOps(realtime) {
  const rt = realtime || {}
  const tasks = rt.tasks || {}
  return {
    tasks: {
      status_counts: tasks,
      in_progress: rt.in_progress || 0,
      stuck_count: 0,
      stuck_tasks: [],
      by_source: {},
      today: {},
    },
    runtimes: {
      total: rt.runtimes ? rt.runtimes.total : 0,
      online: rt.runtimes ? rt.runtimes.online : 0,
      offline: rt.runtimes ? rt.runtimes.total - rt.runtimes.online : 0,
      by_mode: {},
      list: [],
    },
    workers: { total: 0, online: 0, offline: 0, list: [] },
  }
}
const opsWorkers = computed(() => (realOps.value || {}).workers || {})
const opsRuntimes = computed(() => (realOps.value || {}).runtimes || {})
const taskStats = computed(() => ((realOps.value || {}).tasks || {}).issue_stats || {})
const sortedWorkers = computed(() =>
  (opsWorkers.value.list || [] || []).slice().sort((a, b) => {
    const na = workerDisplayName(a).toLowerCase()
    const nb = workerDisplayName(b).toLowerCase()
    return na < nb ? -1 : na > nb ? 1 : 0
  })
)
const workerTotalPages = computed(() =>
  Math.max(1, Math.ceil(sortedWorkers.value.length / workerPageSize.value))
)
const pageWorkers = computed(() =>
  sortedWorkers.value.slice(
    workerPage.value * workerPageSize.value,
    (workerPage.value + 1) * workerPageSize.value
  )
)
function wkKey(wk) {
  return wk.id != null ? wk.id : (wk.device_name || wk.name || '').replace(/[^a-zA-Z0-9_-]/g, '_')
}
function workerBars(wk) {
  return [
    { label: t('cpu'), pct: wk.cpu_percent },
    { label: t('mem'), pct: wk.memory_percent },
    { label: t('tmp'), pct: wk.tmp_percent },
    { label: t('disk'), pct: wk.disk_percent },
    {
      label: t('bandwidth'),
      pct: wk.bandwidth_percent,
      text: (wk.bandwidth_mbps || 0).toFixed(1) + ' Mbps',
    },
  ]
}
const sortedRuntimes = computed(() =>
  (opsRuntimes.value.list || [] || [])
    .slice()
    .sort((a, b) => (a.device_name || '').localeCompare(b.device_name || ''))
)
const availDevices = computed(() => [
  ...new Set(sortedRuntimes.value.map((r) => r.device_name || '-').filter((d) => d && d !== '-')),
])
const filteredRuntimes = computed(() => {
  const devSet = rtDeviceFilter.value
    ? new Set(sortedRuntimes.value.map((rt) => rt.device_name || ''))
    : null
  return sortedRuntimes.value.filter(function (rt) {
    const dev = rt.device_name || ''
    if (rtDeviceFilter.value && devSet.has(rtDeviceFilter.value) && dev !== rtDeviceFilter.value)
      return false
    if (rtSearch.value) {
      const q = rtSearch.value.trim().toLowerCase()
      if (
        q &&
        !(rt.name || '').toLowerCase().includes(q) &&
        !(rt.device_name || '').toLowerCase().includes(q) &&
        !(rt.version || '').toLowerCase().includes(q) &&
        !(rt.mode || '').toLowerCase().includes(q)
      )
        return false
    }
    return true
  })
})
const runtimeTotalPages = computed(() =>
  Math.max(1, Math.ceil(filteredRuntimes.value.length / runtimePageSize.value))
)
const pageRuntimes = computed(() =>
  filteredRuntimes.value.slice(
    runtimePage.value * runtimePageSize.value,
    (runtimePage.value + 1) * runtimePageSize.value
  )
)
function loadColor(rti) {
  const running = rti.running || 0
  const maxC = rti.max_concurrent || 0
  return running >= maxC * 2
    ? 'var(--r)'
    : running >= maxC
      ? 'var(--y)'
      : running > 0
        ? 'var(--g)'
        : 'var(--t3)'
}
function avgDurStr(rti) {
  const d = rti.avg_duration || 0
  return d ? (d > 60 ? Math.round(d / 60) + 'm' + Math.round(d % 60) + 's' : d + 's') : '-'
}
function avgDurColor(rti) {
  const d = rti.avg_duration || 0
  return d > 300 ? 'var(--r)' : d > 60 ? 'var(--y)' : 'var(--g)'
}
function healthScoreHtml(rti) {
  const s = rti.health_score
  if (s === null || s === undefined) return '-'
  const c = s === 0 ? 'var(--r)' : s < 50 ? 'var(--y)' : 'var(--g)'
  const cf =
    rti.health_consecutive_failures > 0
      ? ' <span style="font-size:9px;opacity:.7">连失' + rti.health_consecutive_failures + '</span>'
      : ''
  const hb = rti.health_heartbeat_alive === false ? ' · 失联' : ''
  return (
    '<span style="color:' +
    c +
    ';font-weight:600">' +
    s +
    '</span>' +
    cf +
    (hb ? '<span style="font-size:9px;opacity:.7">' + hb + '</span>' : '')
  )
}
function probeIcon(ps) {
  return { ok: '✅', fail: '❌', skip: '⏭', pending: '⏳' }[ps || 'pending'] || '⏳'
}
function probeColor(ps) {
  return ps === 'ok' ? 'var(--g)' : ps === 'fail' ? 'var(--r)' : 'var(--t3)'
}
function probeStr(ps, pl) {
  return ps === 'ok'
    ? pl + 's'
    : ps === 'pending'
      ? t('probe_pending')
      : ps === 'skip'
        ? t('probe_skip')
        : t('probe_fail')
}
// Token 列
function loadTokCols(force) {
  const now = Date.now()
  if (!force && now - tokColAt < 60000) return
  tokColAt = now
  const full = repoFull()
  const repoQ = full ? '&repo=' + encodeURIComponent(full) : ''
  API()
    .get('/obs/tokens/engines?range=24h' + repoQ)
    .then(function (d) {
      const m = {}
      ;((d || {}).engines || []).forEach((e) => {
        m[String(e.engine_id)] = e.total
      })
      tokEngines.value = m
    })
    .catch(() => {})
  API()
    .get('/obs/tokens/tasks?range=7d&limit=500' + repoQ)
    .then(function (d) {
      const m = {}
      ;((d || {}).tasks || []).forEach((tk) => {
        m[String(tk.task_id)] = tk.total
      })
      tokTasks.value = m
    })
    .catch(() => {})
}
function tokForEngine(id) {
  return tokNum(tokEngines.value[String(id)])
}
function tokForTask(id) {
  return id ? tokNum(tokTasks.value[String(id)]) : '—'
}
// 资源曲线
function loadResHistory() {
  const repo = currentRepo()
  const qp = ['range=' + encodeURIComponent(resRange.value)]
  if (repo && repo.fullName) qp.push('repo=' + encodeURIComponent(repo.fullName))
  API()
    .get('/obs/runtimes/history?' + qp.join('&'))
    .then(function (d) {
      if (G.window._currentView !== 'observability' || section.value !== 'devices') return
      resData.value = d || null
      nextTick(renderResChart)
    })
    .catch(function (e) {
      const el = chartEls.chartRes
      if (el)
        el.innerHTML =
          '<div class="ts t3" style="padding:14px 0">' +
          t('load_failed', { msg: (e && e.message) || e }) +
          '</div>'
    })
}
const resRuntimes = computed(() => (resData.value || {}).runtimes || [])
function resSeries(key) {
  const d = resData.value || {}
  const series = d.series || {}
  const ids = Object.keys(series)
  if (!ids.length) return []
  if (resRt.value) return (series[resRt.value] || {})[key] || []
  const n = ((series[ids[0]] || {})[key] || []).length
  const out = new Array(n).fill(null)
  for (let i = 0; i < n; i++) {
    let sum = 0
    let cnt = 0
    for (const id of ids) {
      const v = ((series[id] || {})[key] || [])[i]
      if (v !== null && v !== undefined) {
        sum += Number(v)
        cnt++
      }
    }
    out[i] = cnt ? Math.round((sum / cnt) * 10) / 10 : null
  }
  return out
}
function renderResChart() {
  const el = chartEls.chartRes
  if (!el) return
  const d = resData.value || {}
  const labels = (d.buckets || []).map(function (iso) {
    const dt = new Date(iso)
    if (isNaN(dt.getTime())) return ''
    const hm =
      String(dt.getHours()).padStart(2, '0') + ':' + String(dt.getMinutes()).padStart(2, '0')
    return resRange.value === '7d' ? dt.getMonth() + 1 + '/' + dt.getDate() + ' ' + hm : hm
  })
  if (!resSeries('cpu').some((v) => v !== null && v !== undefined)) {
    el.innerHTML =
      '<div class="ts t3" style="padding: 20px 0; text-align: center">' + t('res.empty') + '</div>'
    return
  }
  const echarts = G.echarts
  if (!echarts) {
    el.innerHTML =
      '<div class="ts t3" style="padding: 20px 0">' + t('ov.charts_unavailable') + '</div>'
    return
  }
  const mk = function (name, key, color) {
    return {
      name: name,
      type: 'line',
      smooth: true,
      showSymbol: false,
      connectNulls: true,
      itemStyle: { color: color },
      lineStyle: { width: 2 },
      data: resSeries(key),
    }
  }
  try {
    const ch = echarts.init(el)
    ch.setOption({
      tooltip: { trigger: 'axis' },
      legend: { top: 0, right: 0, itemWidth: 10, itemHeight: 8, textStyle: { fontSize: 11 } },
      grid: { left: 40, right: 16, top: 26, bottom: 22 },
      xAxis: { type: 'category', boundaryGap: false, data: labels, axisLabel: { fontSize: 10 } },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        axisLabel: { fontSize: 10, formatter: '{value}%' },
      },
      series: [
        mk(t('res.cpu'), 'cpu', '#4a7fd1'),
        mk(t('res.mem'), 'mem', '#22a06b'),
        mk(t('res.disk'), 'disk', '#8b8b8b'),
      ],
    })
  } catch (e) {
    el.innerHTML = '<div class="ts t3" style="padding: 20px 0">' + t('ov.chart_failed') + '</div>'
  }
}
// watch resRt 变化重绘
watch(resRt, function () {
  nextTick(renderResChart)
})

// ═══ 任务列表（inspect/tasks） ═══
function loadTimeline() {
  timelineLoading.value = true
  timelineError.value = ''
  const full = repoFull()
  if (timelineCache && timelineCache.repo === full && Date.now() - timelineCache.ts < 30000) {
    timelineIssues.value = timelineCache.issues
    timelineLoading.value = false
    return
  }
  const repoParam = full ? '?repo=' + encodeURIComponent(full) : ''
  G.fetch('/api/dashboard/ops/timeline' + repoParam, {
    signal: G.AbortSignal ? G.AbortSignal.timeout(10000) : undefined,
  })
    .then((r) => r.json())
    .then(function (d) {
      timelineCache = { repo: full, ts: Date.now(), issues: d.issues || [] }
      timelineIssues.value = d.issues || []
      timelineLoading.value = false
    })
    .catch(function (e) {
      timelineLoading.value = false
      timelineError.value = t('load_failed', { msg: e.message })
    })
}
const sortedIssues = computed(() =>
  timelineIssues.value.slice().sort((a, b) => {
    const na = parseInt((a.issueId || '').replace(/^#/, '')) || 0
    const nb = parseInt((b.issueId || '').replace(/^#/, '')) || 0
    return nb - na
  })
)
const filteredIssues = computed(() => {
  const q = timelineSearch.value.trim()
  if (!q) return sortedIssues.value
  if (q.startsWith('#')) {
    const num = q.substring(1)
    if (num)
      return sortedIssues.value.filter((iss) => (iss.issueId || '').replace(/^#/, '').includes(num))
    return sortedIssues.value
  }
  const ql = q.toLowerCase()
  return sortedIssues.value.filter(function (iss) {
    return (
      (iss.issueId || '').toLowerCase().includes(ql) || (iss.title || '').toLowerCase().includes(ql)
    )
  })
})
const timelineTotalPages = computed(() =>
  Math.max(1, Math.ceil(filteredIssues.value.length / timelinePageSize.value))
)
const pageIssues = computed(() =>
  filteredIssues.value.slice(
    timelinePage.value * timelinePageSize.value,
    (timelinePage.value + 1) * timelinePageSize.value
  )
)
function issueHasFailed(iss) {
  return (iss.events || []).some((e) => e.status === 'failed')
}
function totalDurStr(iss) {
  const s = (iss.events || []).reduce((sum, e) => sum + (e.duration || 0), 0)
  if (!s || s <= 0) return '0s'
  if (s < 60) return Math.round(s) + 's'
  if (s < 3600) return Math.floor(s / 60) + 'm' + (s % 60 > 0 ? Math.round(s % 60) + 's' : '')
  return Math.floor(s / 3600) + 'h' + Math.floor((s % 3600) / 60) + 'm'
}
function issueUrl(iss) {
  const num = (iss.issueId || '').replace(/^#/, '')
  const repoPath = shortRepo(iss.repoKey || '')
  return repoPath && num ? 'https://gitcode.com/' + repoPath + '/issues/' + num : ''
}
function engineWithIp(engine) {
  if (!engine) return '-'
  const devName = engine.split('-').slice(1).join('-')
  const workers = ((realOps.value || {}).workers || {}).list || []
  const w = workers.find((x) => x.device_name === devName)
  return w && w.last_ip ? engine + ' (' + w.last_ip + ')' : engine
}
function actionText(at) {
  if (at === 'a2a_event') return t('action_a2a_event')
  const map = {
    issue_opened: 'action_issue_opened',
    issue_comment: 'action_issue_comment',
    issue_updated: 'action_issue_updated',
    pull_request_opened: 'action_pull_request_opened',
    note_hook_pr: 'action_note_hook_pr',
    issue_closed: 'action_issue_closed',
    push: 'action_push',
  }
  return map[at] ? t(map[at]) : at
}
function toggleIssue(issueId) {
  if (expandedIssues.has(issueId)) expandedIssues.delete(issueId)
  else {
    expandedIssues.add(issueId)
    if (!issueViewModes[issueId]) issueViewModes[issueId] = 'diagram'
  }
}
function issueViewMode(issueId) {
  return issueViewModes[issueId] || 'diagram'
}
function setIssueView(issueId, mode) {
  issueViewModes[issueId] = mode
  if (mode === 'diagram') nextTick(() => renderMermaidFor(issueId))
}
function setDiagramRef(issueId, el) {
  if (el) {
    diagramRefs[issueId] = el
    if (
      expandedIssues.has(issueId) &&
      issueViewMode(issueId) === 'diagram' &&
      !mermaidRendered[issueId]
    ) {
      nextTick(() => renderMermaidFor(issueId))
    }
  }
}
// Mermaid 动态加载 + 时序图渲染
let mermaidEngine = null
async function getMermaid() {
  if (!mermaidEngine) {
    await new Promise(function (resolve, reject) {
      const s = document.createElement('script')
      s.src = 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js'
      s.onload = resolve
      s.onerror = reject
      document.head.appendChild(s)
    })
    mermaidEngine = G.window.mermaid
    mermaidEngine.initialize({
      startOnLoad: false,
      securityLevel: 'loose',
      theme: 'base',
      fontFamily: 'inherit',
      sequence: {
        mirrorActors: false,
        actorMargin: 60,
        messageMargin: 40,
        noteMargin: 10,
        boxMargin: 8,
      },
    })
  }
  return mermaidEngine
}
function mermaidId(a) {
  return (a || 'agent').replace(/[^a-zA-Z0-9_-]/g, '_')
}
function buildSequence(iss) {
  const agents = []
  for (const ev of iss.events) {
    const a = ev.agentType || '—'
    if (!agents.includes(a)) agents.push(a)
  }
  const lines = ['sequenceDiagram', 'autonumber']
  for (const a of agents) lines.push('    participant ' + mermaidId(a) + ' as ' + a)
  let prev = null
  for (const ev of iss.events) {
    const a = mermaidId(ev.agentType || '—')
    const tm = ev.createdAt ? fmtLocal(ev.createdAt).slice(11, 19) : ''
    const label = tm ? tm + ' ' + actionText(ev.eventType) : actionText(ev.eventType)
    if (prev && a !== prev) lines.push('    ' + prev + '->>' + a + ': ' + label)
    else lines.push('    Note over ' + a + ': ' + label)
    prev = a
  }
  return lines.join('\n')
}
async function renderMermaidFor(issueId) {
  const box = diagramRefs[issueId]
  if (!box || mermaidRendered[issueId]) return
  mermaidRendered[issueId] = true
  box.innerHTML = '<span class="ts t3">' + t('rendering') + '</span>'
  const iss = timelineIssues.value.find((i) => i.issueId === issueId)
  if (!iss) return
  let text = ''
  try {
    const mmd = await getMermaid()
    const uid = 'mmd-' + Math.random().toString(36).slice(2, 10)
    text = buildSequence(iss)
    try {
      await mmd.parse(text)
    } catch (pe) {
      /* parse 失败也尝试 render */
    }
    const result = await mmd.render(uid, text)
    box.innerHTML = result.svg || result
  } catch (err) {
    box.innerHTML =
      '<div style="padding:12px;color:var(--r);font-size:12px">' +
      t('sequence_render_failed', { msg: err.message || err }) +
      '</div>'
    box.innerHTML += '<pre class="obv-mmderr">' + text.replace(/</g, '&lt;') + '</pre>'
  }
}
// ═══ 事件视图（inspect/events） ═══
const eventSources = EVENT_SOURCES.map(([k, l]) => ({ k, l: t(l) }))
const eventStates = EVENT_STATES.map(([k, l]) => ({ k, l: t(l) }))
function pickEvSource(k) {
  evSource.value = evSource.value === k ? '' : k
  eventReload()
}
function pickEvState(k) {
  evState.value = evState.value === k ? '' : k
  eventReload()
}
function onEvRowClick(ev, iss) {
  if (ev.taskId)
    openTaskLog(ev.taskId, { issue: iss.issueId, seq: ev.seq, created: ev.createdAt || '' })
}
function setLogLevel(l) {
  logLevel.value = l
  loadLogs()
}
function clearLogTask() {
  logTask.value = ''
  loadLogs()
}
function pickResRange(r) {
  resRange.value = r
  loadResHistory()
}
function eventQStr() {
  const full = repoFull()
  return (
    (full ? '&repo=' + encodeURIComponent(full) : '') +
    (evSource.value ? '&sources=' + encodeURIComponent(evSource.value) : '') +
    (evState.value ? '&state=' + encodeURIComponent(evState.value) : '') +
    (evQ.value ? '&q=' + encodeURIComponent(evQ.value) : '')
  )
}
function eventReload() {
  evCursor.value = null
  evMore.value = false
  evNew.value = []
  loadEvents(false)
}
function loadEvents(append) {
  eventsLoading.value = !append
  const qs = eventQStr()
  const curQ = append && evCursor.value ? '&before=' + encodeURIComponent(evCursor.value) : ''
  API()
    .get('/obs/events?limit=50' + qs + curQ)
    .then(function (d) {
      d = d || {}
      if (append) {
        const cur = eventsData.value.events || []
        eventsData.value = d
        eventsData.value.events = cur.concat(d.events || [])
      } else {
        eventsData.value = d
      }
      evCursor.value = d.next_cursor || null
      evMore.value = !!d.next_cursor
      eventsLoading.value = false
    })
    .catch(function () {
      eventsLoading.value = false
      eventsData.value = { events: [] }
    })
}
// 软刷新（30s 一次，只攒新事件不打断阅读）
function eventSoftRefresh() {
  const now = Date.now()
  if (now - evLastSoft < 30000) return
  evLastSoft = now
  API()
    .get('/obs/events?limit=50' + eventQStr())
    .then(function (d) {
      const known = eventsData.value.events || []
      const seen = {}
      known.forEach((x) => {
        seen[x.key] = 1
      })
      evNew.value = ((d || {}).events || []).filter((x) => !seen[x.key])
    })
    .catch(() => {})
}
function applyNewEvents() {
  const list = evNew.value || []
  if (!list.length) return
  eventsData.value.events = list.concat(eventsData.value.events || [])
  evNew.value = []
}
function sourceLabel(e) {
  const s = String((e && e.source) || 'monitor')
  return tOr('observability.source.' + s, s)
}
function eventDimTarget(e) {
  const dim = String((e && e.dim_key) || '')
  if (dim.indexOf('runtime:') === 0) return t('device_short') + dim.slice(8)
  return ''
}
function eventTargetLabel(e) {
  const tg = e.target || {}
  const dev =
    tg.device_id ||
    (String(e.dim_key || '').indexOf('runtime:') === 0 ? String(e.dim_key).slice(8) : '')
  if (dev) return t('device_short') + dev
  const r = shortRepo(tg.repo_key || e.repo_key) || ''
  const num = String(tg.issue_id || e.issue_id || '').replace(/^#/, '')
  return r + (num ? ' #' + num : '') || '—'
}
function eventTitle(e) {
  if (e.source === 'change') return e.title || '' || t('kind.change')
  if (e.source === 'audit') return e.title || e.kind
  if (e.source === 'task') return tOr('observability.task_state.' + e.kind, e.kind || '')
  return tOr('observability.rule.' + e.kind, e.kind || '')
}
function eventStateLabel(e) {
  const key = e.state_label_key || 'observability.state.' + e.state
  return tOr(key, tOr('observability.ev_state.' + e.state, e.state || '—'))
}
function eventBadge(e) {
  if (e.outcome) {
    const label = tOr('observability.outcome.' + e.outcome, e.outcome)
    const bad = e.outcome === 'failed' || e.outcome === 'stuck'
    return (
      '<span class="obv-outcome" style="background:' +
      (bad ? 'rgba(220,38,38,.12);color:#dc2626' : 'rgba(37,99,235,.12);color:#2563eb') +
      '">' +
      esc(label) +
      '</span>'
    )
  }
  return '<span class="ts t2">' + severityText({ severity: e.level }) + '</span>'
}
function eventActions(e) {
  const steps = Array.isArray(e.next_step) ? e.next_step : e.next_step ? [e.next_step] : []
  const tid = e.task_id || ''
  const dev =
    (e.target || {}).device_id ||
    (String(e.dim_key || '').indexOf('runtime:') === 0 ? String(e.dim_key).slice(8) : '')
  const num = String(e.issue_id || '').replace(/^#/, '')
  const path = String(e.repo_key || '')
    .split(':')
    .pop()
  const url = path && num ? 'https://gitcode.com/' + path + '/issues/' + num : ''
  const btns = []
  steps.forEach(function (s) {
    if (s === 'timeline' && tid)
      btns.push(
        '<a class="obv-linka" onclick="window.__obvTimeline(\'' +
          tid +
          '\')">' +
          t('timeline') +
          '</a>'
      )
    else if (s === 'logs' && tid)
      btns.push(
        '<a class="obv-linka" onclick="window.__obvTaskLogFor(\'' +
          tid +
          '\')">' +
          t('logs_short') +
          '</a>'
      )
    else if (s === 'issue' && url)
      btns.push(
        '<a class="obv-linka" href="' +
          url +
          '" target="_blank" rel="noopener">' +
          t('issue_short') +
          '</a>'
      )
    else if (s === 'device' && dev)
      btns.push(
        '<a class="obv-linka" onclick="window.__obvDeviceTasks(' +
          dev +
          ')">' +
          t('device_short') +
          dev +
          '</a>'
      )
  })
  if (!btns.length && url)
    btns.push(
      '<a class="obv-linka" href="' +
        url +
        '" target="_blank" rel="noopener">' +
        t('issue_short') +
        '</a>'
    )
  return btns.join(' · ') || '—'
}
G.window.__obvTimeline = function (tid) {
  openTimeline(tid)
}
G.window.__obvTaskLogFor = function (tid) {
  openTaskLogModal(tid, '')
}
G.window.__obvDeviceTasks = function (dev) {
  openSessionModal({ id: Number(dev), name: t('device_short') + dev })
}
function toggleEventDetail(key) {
  if (openEventKeys.has(key)) openEventKeys.delete(key)
  else openEventKeys.add(key)
}
function eventDetailHtml(e) {
  if (!e) return t('no_data')
  const rows = []
  const add = function (label, val) {
    if (val !== '' && val !== null && val !== undefined && val !== '—')
      rows.push(
        '<span class="ts t2 obv-evd-l">' + label + '</span><span class="ts t2">' + val + '</span>'
      )
  }
  add(t('ev_metric'), e.metric || '')
  add(t('ev_value'), e.value === null || e.value === undefined ? '' : e.value)
  add(t('ev_threshold'), e.threshold === null || e.threshold === undefined ? '' : e.threshold)
  add(t('ev_detail'), e.detail ? String(e.detail) : '')
  add(t('event_view_col.started'), e.started_at ? alertTime(e.started_at) : '')
  add(t('event_view_col.duration'), durHuman(e.duration_sec))
  return rows.length ? rows.join('') : '<span class="ts t3">' + t('no_data') + '</span>'
}

// ═══ 错误日志（inspect/errorlogs） ═══
function loadLogs() {
  logsLoading.value = true
  logItems.value = []
  G.fetch('/api/obs/logs/sources', {
    signal: G.AbortSignal ? G.AbortSignal.timeout(10000) : undefined,
  })
    .then((r) => r.json())
    .then(function (d) {
      logSources.value = d.sources || []
    })
    .catch(() => {})
  const qp = ['limit=200']
  if (logLevel.value) qp.push('level=' + logLevel.value)
  if (logSource.value) qp.push('source=' + encodeURIComponent(logSource.value))
  if (logQ.value) qp.push('q=' + encodeURIComponent(logQ.value))
  if (logTask.value) qp.push('task_id=' + encodeURIComponent(logTask.value))
  const full = repoFull()
  if (full) qp.push('repo=' + encodeURIComponent(full))
  API()
    .get('/obs/logs?' + qp.join('&'))
    .then(function (d) {
      const items = d.items || []
      logItems.value = items
      logCountText.value = t('log_count_db', { count: items.length, total: d.total || 0 })
      logsLoading.value = false
    })
    .catch(function () {
      G.fetch(
        '/api/dashboard/ops/error-logs?limit=100' +
          (logLevel.value ? '&level=' + logLevel.value : '') +
          (logSource.value ? '&source=' + encodeURIComponent(logSource.value) : ''),
        { signal: G.AbortSignal ? G.AbortSignal.timeout(15000) : undefined }
      )
        .then((r) => r.json())
        .then(function (d) {
          const entries = d.entries || []
          logItems.value = entries.map(function (e) {
            return { ts: e.ts, source: e.source, msg: e.message || '', level: e.level }
          })
          logCountText.value =
            t('log_count', { count: entries.length, files: (d.files || []).length || 1 }) +
            ' ' +
            t('log_file_fallback')
          logsLoading.value = false
        })
        .catch(function (e) {
          logsLoading.value = false
          logCountText.value = t('load_failed', { msg: e.message })
        })
    })
}
function logLevelColor(level) {
  return level === 'ERROR' || level === 'CRITICAL' ? '#dc2828' : '#d49c0f'
}

// ═══ Token 卡（tasks Tab） ═══
const tokCardHtml = computed(() => {
  const items = Object.keys(tokEngines.value).map(function (id) {
    return { id: id, total: tokEngines.value[id] }
  })
  if (!items.length) return ''
  const rows = items
    .map(function (it) {
      return (
        '<tr><td>engine #' +
        esc(it.id) +
        '</td><td class="ts t2">' +
        tokNum(it.total) +
        '</td></tr>'
      )
    })
    .join('')
  return (
    '<div class="cd obv-tokcard"><div class="obv-tokhead"><span style="font-weight:600">' +
    t('tok_title') +
    '</span><span class="ts t3">' +
    t('tok_hint', { n: tokNum(items.reduce((s, i) => s + Number(i.total || 0), 0)) }) +
    '</span></div><div class="tw"><table><tbody>' +
    rows +
    '</tbody></table></div></div>'
  )
})

// ═══ 弹窗：任务日志 ═══
function openTaskLogModal(taskId, info) {
  closeTaskLog()
  taskLog.open = true
  taskLog.taskId = taskId
  taskLog.info = typeof info === 'string' ? safeParseInfo(info) : info || null
  taskLog.msgs = []
  taskLog.loading = true
  taskLog.live = false
  taskLog.timelineHtml = ''
  let lastSeq = 0
  API()
    .get('/task-logs/' + taskId + '/messages?since=0')
    .then(function (msgs) {
      taskLog.msgs = msgs || []
      lastSeq = taskLog.msgs.length ? taskLog.msgs[taskLog.msgs.length - 1].seq : 0
      taskLog.loading = false
      API()
        .get('/task-logs/' + taskId + '/info')
        .then(function (infoData) {
          taskLog.timelineHtml = taskTimelineHtml(infoData)
          if (infoData && ['running', 'queued', 'dispatched'].includes(infoData.status)) {
            taskLog.live = true
            startTaskLogPoll(taskId, lastSeq)
          }
        })
        .catch(() => {})
    })
    .catch(function (e) {
      taskLog.loading = false
      taskLog.msgs = [{ type: 'error', content: t('load_failed', { msg: e.message }) }]
    })
}
function openTaskLog(taskId, info) {
  openTaskLogModal(taskId, info)
}
function safeParseInfo(enc) {
  try {
    return JSON.parse(decodeURIComponent(enc))
  } catch (e) {
    return null
  }
}
function startTaskLogPoll(taskId, lastSeq) {
  stopTaskLogPoll()
  taskLogTimer = setInterval(function () {
    if (!taskLog.open) {
      stopTaskLogPoll()
      return
    }
    API()
      .get('/task-logs/' + taskId + '/messages?since=' + lastSeq)
      .then(function (newMsgs) {
        if (newMsgs && newMsgs.length) {
          taskLog.msgs = taskLog.msgs.concat(newMsgs)
          lastSeq = newMsgs[newMsgs.length - 1].seq
        }
      })
      .catch(() => {})
    API()
      .get('/task-logs/' + taskId + '/info')
      .then(function (infoData) {
        if (
          infoData &&
          infoData.status &&
          !['running', 'queued', 'dispatched'].includes(infoData.status)
        ) {
          stopTaskLogPoll()
          taskLog.live = false
          taskLog.timelineHtml = taskTimelineHtml(infoData)
        }
      })
      .catch(() => {})
  }, 3000)
}
function stopTaskLogPoll() {
  if (taskLogTimer) {
    clearInterval(taskLogTimer)
    taskLogTimer = null
  }
}
function closeTaskLog() {
  stopTaskLogPoll()
  taskLog.open = false
}
function taskTimelineHtml(info) {
  if (!info) return ''
  const stages = []
  if (info.created_at)
    stages.push({ label: t('stage_queued'), time: info.created_at, color: 'var(--y)' })
  if (info.started_at)
    stages.push({ label: t('stage_start'), time: info.started_at, color: 'var(--a)' })
  if (info.completed_at)
    stages.push({
      label: t('stage_complete'),
      time: info.completed_at,
      color: info.status === 'failed' ? 'var(--r)' : 'var(--g)',
    })
  if (!stages.length) return ''
  const statusColor = {
    queued: 'var(--y)',
    dispatched: 'var(--a)',
    running: 'var(--a)',
    completed: 'var(--g)',
    failed: 'var(--r)',
    cancelled: 'var(--t3)',
  }
  let html = '<div class="obv-tlstages">'
  stages.forEach(function (s, i) {
    html +=
      '<div class="obv-tlstage"><span class="obv-tldot" style="background:' +
      s.color +
      '"></span><span class="ts t2">' +
      s.label +
      '</span><span class="ts t3">' +
      String(s.time).substring(11, 19) +
      '</span></div>'
    if (i < stages.length - 1) html += '<span class="obv-tlarrow">→</span>'
  })
  if (info.status)
    html +=
      '<span class="obv-tlstatus" style="background:' +
      (statusColor[info.status] || 'var(--t3)') +
      '">' +
      info.status.toUpperCase() +
      '</span>'
  html += '</div>'
  return html
}
function msgTypeIcon(ty) {
  return MSG_ICON[ty] || '•'
}
function msgTypeColor(ty) {
  return MSG_COLOR[ty] || '#c2cad6'
}

// ═══ 弹窗：追踪时间线 ═══
function openTimeline(traceOrTaskId) {
  traceModal.open = true
  traceModal.loading = true
  traceModal.error = ''
  traceModal.tasks = []
  traceModal.meta = ''
  API()
    .get('/obs/trace/' + encodeURIComponent(traceOrTaskId))
    .then(function (d) {
      d = d || {}
      traceModal.loading = false
      traceModal.tasks = d.tasks || []
      const rk = d.repo_key ? String(d.repo_key).split(':').slice(1).join(':') : ''
      traceModal.meta = [rk, d.issue_id, d.kind === 'pipeline' ? t('timeline_pipeline') : '']
        .filter(Boolean)
        .join(' · ')
    })
    .catch(function (e) {
      traceModal.loading = false
      traceModal.error = t('load_failed', { msg: (e && e.message) || e })
    })
}
const traceMeta = computed(() => traceModal.meta)
function traceColor(tk) {
  const bad = tk.status === 'failed' || tk.status === 'cancelled'
  return bad ? '#d64545' : tk.status === 'completed' ? '#22a06b' : '#e0a02a'
}
function isTraceBad(tk) {
  return tk.status === 'failed' || tk.status === 'cancelled'
}
function stepColor(ty) {
  return ty === 'error' ? '#dc2828' : ty === 'tool_use' ? '#5b9eff' : '#8d97a8'
}

// ═══ 弹窗：异常任务 ═══
function openAnomaly() {
  anomalyModal.open = true
  loadAnomaly()
}
function loadAnomaly() {
  anomalyModal.loading = true
  const repo = currentRepo()
  const repoParam = repo && repo.fullName ? '?repo=' + encodeURIComponent(repo.fullName) : ''
  API()
    .get('/dashboard/ops/overview' + repoParam)
    .then(function (ops) {
      anomalyModal.loading = false
      const tasks = (ops || {}).tasks || {}
      anomalyModal.stuck = tasks.stuck_tasks || []
      anomalyModal.failed = tasks.failed_tasks || []
    })
    .catch(function () {
      anomalyModal.loading = false
      anomalyModal.stuck = []
      anomalyModal.failed = []
    })
}
const anomalyTotalPages = computed(() => Math.max(1, Math.ceil(anomalyModal.failed.length / 10)))
const anomalyPageItems = computed(() =>
  anomalyModal.failed.slice(anomalyPage.value * 10, (anomalyPage.value + 1) * 10)
)
function anomalyIdStr(s) {
  return s.issue_id && s.event_seq
    ? s.issue_id + ' #' + s.event_seq
    : String(s.id || '').substring(0, 12)
}

// ═══ 弹窗：Session 列表 ═══
function openSessionModal(rti) {
  sessionModal.open = true
  sessionModal.loading = true
  sessionModal.name = rti.name || '#' + rti.id
  sessionModal.rtId = rti.id
  sessionModal.sessions = []
  const repo = currentRepo()
  const repoParam = repo && repo.fullName ? '?repo=' + encodeURIComponent(repo.fullName) : ''
  API()
    .get('/runtime/runtimes/' + rti.id + '/sessions' + repoParam)
    .then(function (d) {
      sessionModal.loading = false
      sessionModal.sessions = d.sessions || []
    })
    .catch(function () {
      sessionModal.loading = false
    })
}
const sessionRunning = computed(() => sessionModal.sessions.filter((s) => s.current_task))
const sessionCompletedTasks = computed(() => {
  const seen = new Set()
  const all = []
  sessionModal.sessions.forEach(function (s) {
    ;(s.tasks || []).forEach(function (tk) {
      if (!seen.has(tk.id)) {
        seen.add(tk.id)
        all.push(tk)
      }
    })
  })
  return all.sort((a, b) => (b.event_seq || 0) - (a.event_seq || 0))
})
const sessionCompletedCount = computed(() =>
  sessionModal.sessions.reduce((sum, s) => sum + (s.tasks ? s.tasks.length : 0), 0)
)
function sessionTaskIdStr(tk) {
  return tk.issue_id && tk.event_seq
    ? tk.issue_id + ' #' + tk.event_seq
    : tk.event_seq
      ? '#' + tk.event_seq
      : String(tk.id || '').substring(0, 12)
}
function sessionStatusIcon(st) {
  return SESSION_ICON[st] || '🔄'
}
function sessionStatusColor(st) {
  return SESSION_COLOR[st] || 'var(--a)'
}

// ═══ 弹窗：告警面板 ═══
function openAlertPanel() {
  alertPanel.open = true
  refreshAlerts()
}
const alertEvents = computed(() => ((ovData.value || {}).alerts || {}).events || [])
const alertRules = computed(() => ((ovData.value || {}).alerts || {}).rules || [])
const alertSilences = computed(() => {
  const now = Date.now()
  return alertEvents.value.filter(
    (e) => e.silenced_until && new Date(e.silenced_until).getTime() > now
  )
})
function refreshAlerts() {
  const full = repoFull()
  API()
    .get('/obs/alerts' + (full ? '?repo=' + encodeURIComponent(full) : ''))
    .then(function (a) {
      alertsCache = a || null
      if (ovData.value) ovData.value = Object.assign({}, ovData.value, { alerts: a })
    })
    .catch(() => null)
}
function isSilenced(e) {
  return e.silenced_until && new Date(e.silenced_until).getTime() > Date.now()
}
function metricVal(ev) {
  const m = String((ev && ev.metric) || '')
  const v = ev && ev.value
  if (v === null || v === undefined) return '—'
  if (m.indexOf('gap_') === 0) {
    const s = Number(v)
    if (!isFinite(s)) return '—'
    if (s < 3600) return Math.round(s / 60) + t('dur_min')
    if (s < 86400) return Math.round(s / 360) / 10 + t('dur_hour')
    return Math.round(s / 8640) / 10 + t('dur_day')
  }
  if (m.indexOf('_mutation') >= 0) {
    const n2 = Math.round(Number(v) * 1000) / 10
    return (n2 > 0 ? '+' : '') + n2 + 'pp'
  }
  if (m.indexOf('rate') >= 0 || m.indexOf('usage') >= 0)
    return Math.round(Number(v) * 1000) / 10 + '%'
  return Number(v).toLocaleString()
}
function severityText(r) {
  const sev = String((r && r.severity) || 'warning')
  const label = tOr('observability.sev.' + sev, sev)
  const tags = []
  if (r && r.aggregate_mode && r.aggregate_mode !== 'none') tags.push(t('tag_aggregate'))
  if (r && r.escalate_json) tags.push(t('tag_escalate'))
  if (r && r.escalated_at) tags.push(t('tag_escalated'))
  if (r && Number(r.notify_deferred) === 1) tags.push(t('tag_deferred'))
  return label + (tags.length ? ' ' + tags.map((x) => '·' + x).join(' ') : '')
}
function chanLabel(chans) {
  const s = String(chans || '').trim()
  if (!s) return t('notify_none')
  return s
    .split(',')
    .map((c) =>
      c.trim() === 'feishu'
        ? t('notify_feishu')
        : c.trim() === 'webhook'
          ? t('notify_webhook')
          : c.trim()
    )
    .join(' + ')
}
function silence(e) {
  app().showPrompt({
    title: t('silence_prompt'),
    value: '60',
    onConfirm: function (m) {
      if (m === null) return
      const minutes = parseInt(m, 10)
      if (!minutes || minutes < 1) return
      API()
        .post('/obs/alerts/' + e.id + '/silence?minutes=' + minutes, {})
        .then(() => refreshAlerts())
        .catch(() => null)
    },
  })
}
function toggleRule(r) {
  const enabled = !Number(r.enabled)
  API()
    .post('/obs/alerts/rules/' + r.id + '/toggle?enabled=' + enabled, {})
    .then(() => refreshAlerts())
    .catch(() => null)
}
function setThreshold(r) {
  app().showPrompt({
    title: t('threshold_prompt'),
    value: String(r.threshold),
    onConfirm: function (v) {
      if (v === null) return
      const f = parseFloat(v)
      if (isNaN(f)) return
      API()
        .post(
          '/obs/alerts/rules/' + r.id + '/threshold?threshold=' + encodeURIComponent(String(f)),
          {}
        )
        .then(() => refreshAlerts())
        .catch(() => null)
    },
  })
}
function setNotify(r) {
  const now = String(r.notify_channels || '').trim()
  const curIdx = now === 'feishu' ? '1' : now.indexOf('webhook') >= 0 ? '2' : '0'
  app().showPrompt({
    title: t('notify_prompt'),
    value: curIdx,
    onConfirm: function (v) {
      if (v === null) return
      const map = { 0: '', 1: 'feishu', 2: 'feishu,webhook' }
      if (map[v] === undefined) return
      API()
        .post('/obs/alerts/rules/' + r.id + '/notify?channels=' + encodeURIComponent(map[v]), {})
        .then(() => refreshAlerts())
        .catch(() => null)
    },
  })
}
function testNotify() {
  alertNotifyResult.value = t('test_notify_sending')
  API()
    .post('/obs/alerts/test-notify', {})
    .then(function (r) {
      const ok = r && r.ok
      alertNotifyResult.value = ok
        ? t('test_notify_ok', { id: (r && r.message_id) || '' })
        : (r && r.message) || t('test_notify_fail')
    })
    .catch(function (e) {
      alertNotifyResult.value = t('test_notify_fail') + ': ' + ((e && e.message) || e)
    })
}

// ═══ 总览 KPI 格式化 ═══
const ovKpiSuccess = computed(() => {
  const k = ovKpis.value
  return k.success_rate === null || k.success_rate === undefined ? '—' : k.success_rate + '%'
})
const ovKpiP95 = computed(() => {
  const k = ovKpis.value
  if (k.p95_duration_s === null || k.p95_duration_s === undefined) return '—'
  return k.p95_duration_s >= 60
    ? Math.floor(k.p95_duration_s / 60) + 'm' + Math.round(k.p95_duration_s % 60) + 's'
    : Math.round(k.p95_duration_s) + 's'
})
const ovKpiEngines = computed(() => {
  const k = ovKpis.value
  return k.engines_total ? k.engines_online + ' / ' + k.engines_total : '—'
})

// ═══ 生命周期 ═══
onMounted(function () {
  G.window._currentView = 'observability'
  const sec =
    props.initialSection && ['overview', 'devices', 'inspect'].includes(props.initialSection)
      ? props.initialSection
      : 'overview'
  section.value = sec
  tab.value = subTabs.value[0] || ''
  if (sec === 'overview') {
    loadOverview()
  } else {
    refreshObs()
    if (autoOn.value) startAuto()
    if (sec === 'devices') loadResHistory()
    if (tab.value === 'errorlogs') loadLogs()
    else if (tab.value === 'events') eventReload()
    else if (tab.value === 'tasks') loadTimeline()
  }
})
onBeforeUnmount(function () {
  stopAuto()
  stopTaskLogPoll()
})
</script>
<style scoped>
.obv {
  min-height: 100%;
}
.obv .loading,
.obv .error {
  padding: 40px;
  text-align: center;
}
.obv-shell {
  padding: 0;
  overflow: hidden;
}
.obv-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid var(--b);
}
.obv-title {
  font-size: 14px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 0;
}
.obv-headtools {
  display: flex;
  align-items: center;
  gap: 10px;
}
.obv-autolbl {
  display: flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
}
.obv-rangbar {
  border: none;
  padding: 0;
}
/* 大 Tab */
.obv-bigtabs {
  display: flex;
  gap: 10px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--b);
}
.obv-bigtabs a {
  flex: 1;
  cursor: pointer;
  text-align: center;
  padding: 12px 10px;
  border-radius: 10px;
  border: 1px solid var(--b);
  background: transparent;
  color: inherit;
  transition: opacity 0.15s;
}
.obv-bigtabs a:hover {
  opacity: 0.88;
}
.obv-bigtabs a.on {
  border-color: var(--a);
  background: var(--a);
  color: #fff;
}
.obv-tab-ic {
  font-size: 20px;
  line-height: 1.1;
}
.obv-tab-t {
  font-size: 15px;
  font-weight: 600;
  margin-top: 5px;
}
.obv-tab-h {
  font-size: 11px;
  opacity: 0.75;
  margin-top: 3px;
}
/* 子 Tab */
.obv-subtabs {
  padding: 0 20px;
  border-bottom: 1px solid var(--b);
}
.obv-content {
  padding: 20px;
}
/* 总览 */
.obv-kpigrid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
}
.obv-kpi {
  padding: 12px 14px;
}
.obv-kpi-v {
  font-size: 22px;
  font-weight: 600;
  line-height: 1.3;
}
.obv-chartgrid {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 12px;
  margin-top: 12px;
}
.obv-chartbox {
  padding: 12px 14px;
  margin-top: 12px;
}
.obv-chartgrid .obv-chartbox {
  margin-top: 0;
}
.obv-chart-lbl {
  margin-bottom: 4px;
}
.obv-chart240 {
  height: 240px;
}
.obv-chart220 {
  height: 220px;
}
/* SLO */
.obv-slowrap {
  padding: 10px 14px;
  margin-bottom: 12px;
}
.obv-slohead {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.obv-slogrid {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.obv-slocard {
  cursor: pointer;
  min-width: 210px;
  flex: 1;
  padding: 4px 10px;
}
.obv-slov {
  font-size: 18px;
  font-weight: 600;
}
.obv-slotgt {
  font-size: 11px;
}
/* 告警横幅 */
.obv-banner-ok {
  padding: 10px 14px;
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  border-left: 3px solid #22a06b;
}
.obv-banner-warn {
  padding: 10px 14px;
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.obv-banner-detail {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.obv-banner-btn {
  margin-left: auto;
  white-space: nowrap;
  padding: 4px 12px;
}
/* 设备分区 */
.obv-sec {
  margin-bottom: 8px;
  margin-top: 16px;
}
.obv-sec h2 {
  font-size: 13px;
  font-weight: 600;
  display: inline;
  margin: 0;
}
.obv-sec-sub {
  font-size: 10px;
  color: var(--t3);
  margin-left: 8px;
}
.obv-kpirow {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 0;
  border: 1px solid var(--b);
  border-radius: 10px;
  overflow: hidden;
  margin-bottom: 16px;
  background: var(--c);
}
.obv-kpicell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 16px 18px;
  border-right: 1px solid var(--b);
  border-bottom: 1px solid var(--b);
}
.obv-kpil {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--t3);
}
.obv-kpiv {
  font-size: 22px;
  font-weight: 700;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
}
.obv-wkgrid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 10px;
  margin-bottom: 16px;
}
.obv-wkcard {
  background: var(--c);
  border: 1px solid var(--b);
  border-radius: var(--r2);
  padding: 12px 14px;
  box-shadow: var(--sh);
}
.obv-wk-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.obv-wk-dot {
  font-size: 14px;
}
.obv-wk-name {
  font-size: 13px;
}
.obv-wk-health {
  margin-left: auto;
  font-size: 10px;
  font-weight: 600;
}
.obv-wk-meta {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 2px 12px;
  font-size: 10px;
  color: var(--t3);
  margin-bottom: 8px;
}
.obv-bar-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
  margin-bottom: 3px;
}
.obv-bar-lbl {
  color: var(--t3);
  min-width: 32px;
}
.obv-bar-track {
  flex: 1;
  height: 8px;
  background: var(--bg);
  border-radius: 4px;
  overflow: hidden;
  min-width: 60px;
}
.obv-bar-fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.3s;
}
.obv-bar-val {
  font-weight: 600;
  min-width: 50px;
  text-align: right;
}
/* Runtime 表 */
.obv-rtfilter {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.obv-rtsearch {
  flex: 1;
  min-width: 200px;
  padding: 6px 10px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 12px;
  background: var(--c);
  color: var(--t);
  font-family: inherit;
}
.obv-rtsel {
  padding: 6px 10px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 12px;
  background: var(--c);
  color: var(--t);
  font-family: inherit;
}
.obv-rtrow {
  cursor: pointer;
}
/* 分页 */
.obv-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 12px 0;
  flex-wrap: wrap;
}
.obv-pagesz {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-left: 8px;
}
.obv-pagesz select {
  padding: 4px 8px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 12px;
  background: var(--c);
}
/* 资源曲线 */
.obv-ressection {
  padding: 0 20px 20px;
}
.obv-reshead {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  border-top: 1px solid var(--b);
  padding-top: 14px;
}
.obv-restools {
  display: flex;
  align-items: center;
  gap: 10px;
}
.obv-ressel {
  max-width: 220px;
}
.obv-reschart {
  height: 200px;
  margin-top: 6px;
}
/* 任务列表 */
.obv-anomalybar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.obv-tlsearch {
  width: 100%;
  margin-bottom: 10px;
  padding: 6px 10px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 12px;
  background: var(--c);
  font-family: inherit;
}
.obv-issuecard {
  border: 1px solid var(--b);
  border-radius: 8px;
  overflow: hidden;
  margin-bottom: 8px;
}
.obv-issue-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  cursor: pointer;
  background: var(--c);
  flex-wrap: wrap;
}
.obv-issue-arrow {
  font-size: 12px;
}
.obv-issue-link {
  font-size: 13px;
  font-weight: 700;
  color: var(--a);
  text-decoration: none;
}
.obv-issue-id {
  font-size: 13px;
}
.obv-issue-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--t3);
  min-width: 0;
}
.obv-issue-body {
  padding: 10px 14px;
  border-top: 1px solid var(--b);
}
.obv-viewmode {
  display: flex;
  gap: 0;
  margin-bottom: 10px;
}
.obv-mmdbox {
  overflow-x: auto;
}
.obv-mmderr {
  padding: 8px;
  background: #0c1222;
  color: #e0e6f0;
  font-size: 10px;
  border-radius: 6px;
  overflow-x: auto;
  white-space: pre-wrap;
}
.obv-evtable-wrap {
  overflow-x: auto;
}
.obv-evtable {
  width: 100%;
  font-size: 11px;
  border-collapse: collapse;
}
.obv-evtable th {
  text-align: left;
  padding: 6px 8px;
  font-weight: 500;
  color: var(--t3);
  border-bottom: 1px solid var(--b);
}
.obv-evtable td {
  padding: 6px 8px;
  border-bottom: 1px solid rgba(0, 0, 0, 0.03);
}
/* Token 卡 */
.obv-tokcard {
  padding: 10px 14px;
  margin-bottom: 12px;
}
.obv-tokhead {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
/* 事件视图 */
.obv-evfilter {
  padding: 12px 14px;
}
/* Issue #476：失败空结果占比摘要条 */
.obv-gap {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 11px;
  background: rgba(255, 59, 48, 0.05);
  border: 1px solid rgba(255, 59, 48, 0.12);
  border-radius: 6px;
  padding: 6px 10px;
  margin-bottom: 8px;
}
.obv-gap-t {
  font-weight: 600;
}
.obv-evfilter-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.obv-chip {
  cursor: pointer;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  color: var(--a);
}
.obv-chip.on {
  background: var(--a);
  color: #fff;
}
.obv-evq {
  flex: 1;
  min-width: 180px;
  padding: 4px 8px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 12px;
  background: var(--c);
  font-family: inherit;
  color: var(--t);
}
.obv-evsearchbtn {
  white-space: nowrap;
  padding: 4px 12px;
}
.obv-evnew {
  margin-top: 8px;
}
.obv-evnew a {
  cursor: pointer;
  color: var(--a);
  font-size: 12px;
}
.obv-evbody {
  margin-top: 8px;
}
.obv-evrow {
  padding: 8px 10px;
  margin-bottom: 6px;
  border: 1px solid var(--b);
  border-radius: 6px;
  cursor: pointer;
}
.obv-evrow-main {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.obv-evtime {
  min-width: 92px;
}
.obv-evdur {
  min-width: 60px;
  text-align: right;
}
.obv-evrow-sub {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 4px;
}
.obv-evdetail {
  margin-top: 6px;
  border-top: 1px dashed var(--b);
  padding-top: 6px;
}
.obv-evdetail-grid {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 2px 10px;
}
.obv-evd-l {
  display: inline-block;
  min-width: 80px;
}
.obv-evmore {
  text-align: center;
  margin-top: 10px;
}
.obv-outcome {
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
}
.obv-linka {
  cursor: pointer;
  color: var(--a);
}
/* 错误日志 */
.obv-logfilter {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.obv-logbtns {
  display: flex;
  gap: 0;
}
.obv-logsel {
  padding: 4px 8px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 12px;
  background: var(--c);
  font-family: inherit;
}
.obv-logq {
  max-width: 200px;
  padding: 4px 8px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 12px;
  background: var(--c);
  font-family: inherit;
}
.obv-logtask {
  background: rgba(74, 127, 209, 0.12);
  padding: 2px 8px;
  border-radius: 10px;
}
.obv-logbox {
  background: #0c1222;
  color: #e0e6f0;
  font-family: Menlo, Monaco, Consolas, monospace;
  font-size: 11px;
  line-height: 1.5;
  border-radius: 8px;
  overflow: hidden;
  max-height: 70vh;
  overflow-y: auto;
}
.obv-logline {
  padding: 4px 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}
.obv-logtime {
  color: #5b667b;
}
.obv-logsrc {
  color: #5b9eff;
}
.obv-logcomp {
  color: #8d97a8;
}
.obv-logctx {
  color: #7f8ea3;
}
.obv-logmsg {
  color: #e0e6f0;
}
.obv-logsub {
  color: #8d97a8;
}
.obv-logempty {
  padding: 12px 14px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  color: #8d97a8;
}
/* 弹窗通用 */
.rtv-modal-ov {
  position: fixed;
  inset: 0;
  z-index: 1001;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
}
.obv-logmodal,
.obv-tracemodal,
.obv-anomalymodal,
.obv-sessionmodal,
.obv-alertmodal {
  background: var(--c);
  width: 860px;
  max-width: 94vw;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
  border-radius: var(--r2);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.15);
}
.obv-anomalymodal {
  width: 960px;
  max-height: 84vh;
}
.obv-sessionmodal {
  width: 760px;
  max-width: 92vw;
  max-height: 80vh;
}
.obv-alertmodal {
  width: 820px;
  max-height: 82vh;
}
.obv-lm-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  border-bottom: 1px solid var(--b);
}
.obv-lm-title {
  font-size: 13px;
  font-weight: 600;
  margin: 0;
}
.obv-lm-tools {
  display: flex;
  align-items: center;
  gap: 8px;
}
.obv-lm-live {
  font-size: 10px;
  color: var(--g);
}
.obv-lm-timeline {
  padding: 10px 18px;
  border-bottom: 1px solid var(--b);
}
.obv-lm-body {
  overflow-y: auto;
  flex: 1;
  padding: 14px 18px;
}
/* 任务时间线（弹窗内） */
.obv-tlstages {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 11px;
}
.obv-tlstage {
  display: flex;
  align-items: center;
  gap: 4px;
}
.obv-tldot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.obv-tlarrow {
  color: var(--t3);
  font-size: 10px;
}
.obv-tlstatus {
  margin-left: auto;
  padding: 2px 8px;
  border-radius: 4px;
  color: #fff;
  font-size: 10px;
  font-weight: 600;
}
/* 追踪时间线弹窗 */
.obv-tracemeta {
  margin-bottom: 10px;
}
.obv-tracecard {
  padding: 12px 14px;
  margin-bottom: 12px;
  border-left: 3px solid var(--t3);
}
.obv-trace-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.obv-trace-loglink {
  cursor: pointer;
  color: var(--a);
  margin-left: auto;
  white-space: nowrap;
}
.obv-trace-track {
  margin: 6px 0;
}
.obv-trace-fail {
  color: #d64545;
  font-size: 12px;
  margin-bottom: 6px;
}
.obv-trace-steps {
  margin-top: 4px;
}
.obv-trace-steps summary {
  cursor: pointer;
  font-size: 12px;
}
.obv-trace-stepbox {
  margin-top: 6px;
  border-radius: 6px;
  padding: 6px 0;
  max-height: 260px;
}
/* Session 弹窗 */
.obv-sessiongrp {
  border: 1px solid var(--b);
  border-radius: 8px;
  overflow: hidden;
  margin-bottom: 10px;
}
.obv-sessiongrp-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  background: var(--c);
}
.obv-sessiongrp-body {
  padding: 8px 12px;
  border-top: 1px solid var(--b);
}
</style>
