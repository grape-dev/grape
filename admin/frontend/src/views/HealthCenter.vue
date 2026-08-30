<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>健康中心</h1>
      <span class="badge">{{ taskKpi.total + deviceKpi.total }} 项监控</span>
      <span style="margin-left:auto;display:flex;align-items:center;gap:10px">
        <span class="t3" style="font-size:12px">{{ lastUpdate || '—' }}</span>
        <button class="btn btn-sm" @click="load">🔄 刷新</button>
        <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:var(--text-secondary)">
          <input type="checkbox" v-model="autoRefresh" @change="toggleAuto" style="margin:0" />自动(15s)
        </label>
      </span>
    </div>

    <div style="padding:20px 28px">
      <!-- 告警中心横幅 -->
      <div class="alert-banner" v-if="alerts.length > 0" :class="{ expanded: alertExpanded }">
        <div class="alert-banner-head" @click="alertExpanded = !alertExpanded">
          <span class="alert-icon">🚨</span>
          <span class="alert-title">告警中心</span>
          <span class="alert-count">{{ alerts.length }} 条异常</span>
          <div class="alert-summary">
            <span v-for="(s, k) in alertSummary" :key="k" class="alert-chip" :style="{ color: s.color, background: s.bg }">
              {{ s.label }} {{ s.count }}
            </span>
          </div>
          <span class="alert-expand-icon">{{ alertExpanded ? '▲' : '▼' }}</span>
        </div>
        <div class="alert-banner-body" v-if="alertExpanded">
          <div class="alert-item" v-for="(a, i) in alerts" :key="i">
            <span class="alert-item-severity" :class="a.severity">{{ severityText(a.severity) }}</span>
            <span class="alert-item-source" :class="'src-' + a.dimension">{{ dimensionText(a.dimension) }}</span>
            <span class="alert-item-title">{{ a.title }}</span>
            <span class="alert-item-detail">{{ a.detail }}</span>
            <span class="alert-item-time t3">{{ a.time || '—' }}</span>
          </div>
        </div>
      </div>

      <!-- 三维度 KPI 卡片行 -->
      <div class="hc-dimensions">
        <!-- 任务维度 -->
        <div class="hc-dim-card" :class="{ active: activeTab === 'tasks' }" @click="switchTab('tasks')">
          <div class="hc-dim-head">
            <span class="hc-dim-icon">📋</span>
            <span class="hc-dim-name">任务</span>
            <span class="hc-dim-health" :style="{ color: taskHealth.color }">● {{ taskHealth.text }}</span>
          </div>
          <div class="hc-kpi-row">
            <div class="hc-kpi"><span class="hc-kpi-val">{{ taskKpi.total }}</span><span class="hc-kpi-label">总任务</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" style="color:var(--success)">{{ taskKpi.completed }}</span><span class="hc-kpi-label">已完成</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" style="color:var(--warning)">{{ taskKpi.running }}</span><span class="hc-kpi-label">运行中</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" style="color:var(--danger)">{{ taskKpi.failed }}</span><span class="hc-kpi-label">失败</span></div>
          </div>
          <div class="hc-dim-bar" v-if="taskKpi.total > 0">
            <div class="hc-dim-bar-seg success" :style="{ width: barWidth(taskKpi.completed, taskKpi.total) }"></div>
            <div class="hc-dim-bar-seg warning" :style="{ width: barWidth(taskKpi.running, taskKpi.total) }"></div>
            <div class="hc-dim-bar-seg danger" :style="{ width: barWidth(taskKpi.failed, taskKpi.total) }"></div>
          </div>
        </div>

        <!-- 设备维度 -->
        <div class="hc-dim-card" :class="{ active: activeTab === 'devices' }" @click="switchTab('devices')">
          <div class="hc-dim-head">
            <span class="hc-dim-icon">🖥️</span>
            <span class="hc-dim-name">设备</span>
            <span class="hc-dim-health" :style="{ color: deviceHealth.color }">● {{ deviceHealth.text }}</span>
          </div>
          <div class="hc-kpi-row">
            <div class="hc-kpi"><span class="hc-kpi-val">{{ deviceKpi.total }}</span><span class="hc-kpi-label">总设备</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" style="color:var(--success)">{{ deviceKpi.online }}</span><span class="hc-kpi-label">在线</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" style="color:var(--warning)">{{ deviceKpi.offline }}</span><span class="hc-kpi-label">离线</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" style="color:var(--danger)">{{ deviceKpi.unhealthy }}</span><span class="hc-kpi-label">异常</span></div>
          </div>
          <div class="hc-dim-bar" v-if="deviceKpi.total > 0">
            <div class="hc-dim-bar-seg success" :style="{ width: barWidth(deviceKpi.online, deviceKpi.total) }"></div>
            <div class="hc-dim-bar-seg warning" :style="{ width: barWidth(deviceKpi.offline, deviceKpi.total) }"></div>
            <div class="hc-dim-bar-seg danger" :style="{ width: barWidth(deviceKpi.unhealthy, deviceKpi.total) }"></div>
          </div>
        </div>

        <!-- 基础设施维度 -->
        <div class="hc-dim-card" :class="{ active: activeTab === 'infra' }" @click="switchTab('infra')">
          <div class="hc-dim-head">
            <span class="hc-dim-icon">🏗️</span>
            <span class="hc-dim-name">基础设施</span>
            <span class="hc-dim-health" :style="{ color: infraHealth.color }">● {{ infraHealth.text }}</span>
          </div>
          <div class="hc-kpi-row">
            <div class="hc-kpi"><span class="hc-kpi-val">{{ infraKpi.dbCount }}</span><span class="hc-kpi-label">数据库</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" :style="{ color: infraKpi.dbHealthy ? 'var(--success)' : 'var(--danger)' }">{{ infraKpi.dbHealthy ? '正常' : '异常' }}</span><span class="hc-kpi-label">DB 状态</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val">{{ infraKpi.elbCount }}</span><span class="hc-kpi-label">ELB</span></div>
            <div class="hc-kpi"><span class="hc-kpi-val" style="color:var(--text-tertiary)">—</span><span class="hc-kpi-label">预留</span></div>
          </div>
          <div class="hc-dim-bar" v-if="infraKpi.dbCount > 0">
            <div class="hc-dim-bar-seg" :class="infraKpi.dbHealthy ? 'success' : 'danger'" :style="{ width: '100%' }"></div>
          </div>
        </div>
      </div>

      <!-- Tab 内容区 -->
      <div class="card hc-detail-card" style="margin-top:16px">
        <!-- 任务列表（按 Issue 分组，点击展开事件） -->
        <div v-if="activeTab === 'tasks'">
          <div class="hc-detail-head">
            <h3>任务列表</h3>
            <span class="t3" style="font-size:12px">{{ timelineIssues.length }} 个 Issue — 点击展开</span>
            <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
              <input v-model="timelineRepo" placeholder="仓库名筛选" style="width:160px;padding:5px 8px;border:1px solid var(--border-color);border-radius:6px;font-size:12px" @keyup.enter="loadTimeline" />
              <button class="btn btn-sm" @click="loadTimeline">刷新</button>
            </div>
          </div>
          <div style="padding:12px 20px">
            <div v-if="timelineLoading" style="text-align:center;padding:32px;color:var(--text-tertiary)">加载中...</div>
            <div v-else-if="timelineIssues.length === 0" style="text-align:center;padding:32px;color:var(--text-tertiary)">暂无任务事件</div>
            <!-- Issue 行 -->
            <div v-for="iss in timelineIssues" :key="iss.issueId" class="tl-issue">
              <div class="tl-issue-head" @click="toggleIssue(iss.issueId)">
                <span class="tl-arrow">{{ expandedIssues.has(iss.issueId) ? '▾' : '▸' }}</span>
                <b class="tl-issue-id">
                  <a v-if="iss.repoName" :href="issueUrl(iss)" target="_blank" @click.stop class="tl-issue-link">{{ iss.repoName }} {{ iss.issueId }}</a>
                  <template v-else>{{ iss.issueId }}</template>
                </b>
                <span class="tl-issue-title t3" v-if="iss.title">{{ iss.title }}</span>
                <span class="tl-issue-count t3">{{ iss.eventCount }} 事件</span>
                <span class="tl-issue-status" :class="issueHasFailed(iss) ? 'fail' : 'ok'">{{ issueHasFailed(iss) ? '✗ 有失败' : '✓ 正常' }}</span>
                <span class="t3 tl-issue-time">{{ (iss.lastAt || '').substring(0, 16) }}</span>
              </div>
              <!-- 展开的详情：时序图 + 事件列表 -->
              <div v-if="expandedIssues.has(iss.issueId)" class="tl-issue-body">
                <!-- 切换按钮 -->
                <div class="tl-view-tabs">
                  <button class="tl-view-btn" :class="{ active: issueViewMode(iss.issueId) !== 'events' }" @click="setIssueView(iss.issueId, 'diagram')">📊 时序图</button>
                  <button class="tl-view-btn" :class="{ active: issueViewMode(iss.issueId) === 'events' }" @click="setIssueView(iss.issueId, 'events')">📋 事件列表</button>
                </div>
                <!-- 时序图容器（v-once 避免弹窗开关导致的 re-render 重新 diff SVG） -->
                <div v-show="issueViewMode(iss.issueId) !== 'events'" class="tl-diagram-container" :ref="el => diagramRefs[iss.issueId] = el">
                  <div v-if="!issueRendered(iss.issueId)" class="tl-diagram-loading">渲染中...</div>
                  <div v-else v-html="issueSvg(iss.issueId)" class="tl-diagram-svg"></div>
                </div>
                <!-- 事件列表 -->
                <div v-show="issueViewMode(iss.issueId) === 'events'">
                  <table class="vod-table">
                    <thead>
                      <tr><th>Seq</th><th>事件</th><th>Agent</th><th>Engine</th><th>Skill</th><th>状态</th><th>耗时</th><th>时间</th></tr>
                    </thead>
                    <tbody>
                      <tr v-for="ev in iss.events" :key="ev.seq" :class="{ 'tl-row-fail': ev.status === 'failed', 'tl-row-ok': ev.status === 'completed', 'tl-row-clickable': ev.taskId }" @click="ev.taskId ? openLogModal(ev.taskId, iss) : null">
                        <td class="t3">#{{ ev.seq }}</td>
                        <td><span :class="['status-tag', eventTypeClass(ev.eventType)]" style="font-size:11px">{{ eventTypeText(ev.eventType) }}</span></td>
                        <td><span class="status-tag info" style="font-size:11px">{{ ev.agentType || '—' }}</span></td>
                        <td class="t3" style="font-size:11px">{{ ev.engine || '—' }}</td>
                        <td class="t3">{{ ev.skill || '—' }}</td>
                        <td><span :class="['status-tag', ev.status === 'completed' ? 'success' : ev.status === 'failed' ? 'danger' : ev.status === 'running' ? 'warning' : 'default']">{{ ev.status }}</span></td>
                        <td class="t3">{{ formatDuration2(ev.duration) }}</td>
                        <td class="t3">{{ formatTime(ev.createdAt) }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 设备概览（Worker 卡片 + Runtime 引擎表格，对齐主服务概览 tab） -->
        <div v-if="activeTab === 'devices'">
          <div class="hc-detail-head">
            <h3>设备监控</h3>
            <span class="t3" style="font-size:12px">Worker {{ workers.online }}/{{ workers.total }} 在线 · 引擎 {{ runtimes.online }}/{{ runtimes.total }} 在线</span>
            <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
              <button class="btn btn-sm" @click="loadOverview">🔄 刷新</button>
            </div>
          </div>

          <!-- Worker KPI 行 -->
          <div class="hc-overview-kpi">
            <div class="hc-kpi-card"><span class="hc-kpi-val">{{ workers.total }}</span><span class="hc-kpi-label">Worker 总数</span></div>
            <div class="hc-kpi-card"><span class="hc-kpi-val" style="color:var(--success)">{{ workers.online }}</span><span class="hc-kpi-label">在线</span></div>
            <div class="hc-kpi-card"><span class="hc-kpi-val" style="color:var(--text-tertiary)">{{ workers.offline }}</span><span class="hc-kpi-label">离线</span></div>
            <div class="hc-kpi-card"><span class="hc-kpi-val" style="color:var(--primary)">{{ runtimes.total }}</span><span class="hc-kpi-label">引擎总数</span></div>
          </div>

          <!-- Worker 卡片网格 -->
          <div class="hc-section-title">Worker 监控</div>
          <div class="hc-worker-grid" v-if="workersList.length > 0">
            <div class="hc-worker-card" v-for="wk in workersList" :key="wk.id">
              <div class="hc-worker-head">
                <span class="hc-health-dot" :style="{ background: healthColor(wk.health) }"></span>
                <b class="hc-worker-name">{{ wk.name || wk.deviceName || 'Worker #' + wk.id }}</b>
                <span v-if="wk.deviceName && wk.deviceName !== wk.name" class="hc-worker-subname">{{ wk.deviceName }}</span>
                <span class="hc-worker-health" :style="{ color: healthColor(wk.health) }">{{ wk.health }}</span>
              </div>
              <div class="hc-worker-meta">
                <span>IP: {{ wk.lastIp || '—' }}</span>
                <span>心跳: {{ formatTime(wk.lastSeenAt).slice(11, 19) || '—' }}</span>
              </div>
              <div class="hc-resource-bar">
                <span class="hc-resource-label">CPU</span>
                <div class="hc-resource-track"><div class="hc-resource-fill" :style="{ width: pct(wk.cpuPercent), background: resourceColor(wk.cpuPercent) }"></div></div>
                <span class="hc-resource-val" :style="{ color: resourceColor(wk.cpuPercent) }">{{ Math.round(wk.cpuPercent || 0) }}%</span>
              </div>
              <div class="hc-resource-bar">
                <span class="hc-resource-label">MEM</span>
                <div class="hc-resource-track"><div class="hc-resource-fill" :style="{ width: pct(wk.memoryPercent), background: resourceColor(wk.memoryPercent) }"></div></div>
                <span class="hc-resource-val" :style="{ color: resourceColor(wk.memoryPercent) }">{{ Math.round(wk.memoryPercent || 0) }}%</span>
              </div>
              <div class="hc-resource-bar">
                <span class="hc-resource-label">TMP</span>
                <div class="hc-resource-track"><div class="hc-resource-fill" :style="{ width: pct(wk.tmpPercent), background: resourceColor(wk.tmpPercent) }"></div></div>
                <span class="hc-resource-val" :style="{ color: resourceColor(wk.tmpPercent) }">{{ Math.round(wk.tmpPercent || 0) }}%</span>
              </div>
              <div class="hc-resource-bar">
                <span class="hc-resource-label">DISK</span>
                <div class="hc-resource-track"><div class="hc-resource-fill" :style="{ width: pct(wk.diskPercent), background: resourceColor(wk.diskPercent) }"></div></div>
                <span class="hc-resource-val" :style="{ color: resourceColor(wk.diskPercent) }">{{ Math.round(wk.diskPercent || 0) }}%</span>
              </div>
            </div>
          </div>
          <div v-else class="hc-empty">无 Worker 数据</div>

          <!-- Runtime 引擎表格 -->
          <div class="hc-section-title">Runtime 引擎</div>
          <table class="vod-table">
            <thead>
              <tr><th>引擎</th><th>版本</th><th>设备</th><th>归属人</th><th>IP</th><th>健康度</th><th>繁忙</th><th>平均耗时</th><th>模式</th><th>范围</th><th>最后心跳</th></tr>
            </thead>
            <tbody>
              <tr v-for="rt in runtimesList" :key="rt.id" class="tl-row-clickable" @click="openRtTasksModal(rt)">
                <td><span class="hc-health-dot" :style="{ background: healthColor(rt.health) }"></span> <b>{{ rt.name }}</b></td>
                <td class="t3">{{ rt.version || '—' }}</td>
                <td class="t3">{{ rt.device || '—' }}</td>
                <td class="t3">{{ rt.ownerName || '—' }}</td>
                <td class="t3">{{ rt.lastIp || '—' }}</td>
                <td><span :style="{ color: healthColor(rt.health), fontWeight: 600 }">{{ rt.health }}</span></td>
                <td><span :style="{ color: loadColor(rt.running, rt.maxConcurrent) }">{{ rt.running }}/{{ rt.maxConcurrent }}</span></td>
                <td><span :style="{ color: avgDurColor(rt.avgDuration) }">{{ formatAvgDur(rt.avgDuration) }}</span></td>
                <td class="t3">{{ rt.workerName || '—' }}<span v-if="rt.workerIp" style="color:var(--text-tertiary);font-size:10px"> ({{ rt.workerIp }})</span></td>
                <td><span :class="['status-tag', rt.mode === 'local' ? 'info' : 'default']" style="font-size:11px">{{ rt.mode === 'local' ? '本机' : '远端' }}</span></td>
                <td><span class="status-tag default" style="font-size:11px">{{ rt.scope || 'global' }}</span></td>
                <td class="t3">{{ formatTime(rt.lastSeenAt) }}</td>
              </tr>
              <tr v-if="runtimesList.length === 0">
                <td colspan="11" style="text-align:center;padding:32px;color:var(--text-tertiary)">暂无引擎数据</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 基础设施详情 -->
        <div v-if="activeTab === 'infra'">
          <div class="hc-detail-head">
            <h3>基础设施监控</h3>
            <span class="t3" style="font-size:12px">数据库 / ELB / 缓存</span>
          </div>
          <div class="hc-infra-grid">
            <div class="hc-infra-card" v-for="db in infraDetails" :key="db.name">
              <div class="hc-infra-head">
                <span class="hc-infra-icon">{{ db.type === 'mysql' ? '🗄️' : '⚖️' }}</span>
                <span class="hc-infra-name">{{ db.name }}</span>
                <span class="status-tag" :class="db.healthy ? 'success' : 'danger'">{{ db.healthy ? '正常' : '异常' }}</span>
              </div>
              <div class="hc-infra-body">
                <div class="hc-infra-row"><span>类型</span><span>{{ db.type }}</span></div>
                <div class="hc-infra-row"><span>地址</span><span class="t3">{{ db.host }}:{{ db.port }}</span></div>
                <div class="hc-infra-row"><span>延迟</span><span :style="{ color: db.latency > 100 ? 'var(--warning)' : 'var(--success)' }">{{ db.latency }}ms</span></div>
                <div class="hc-infra-row"><span>连接数</span><span>{{ db.connections || '—' }}</span></div>
                <div class="hc-infra-row" v-if="db.error"><span>错误</span><span style="color:var(--danger);font-size:12px">{{ db.error }}</span></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- 任务日志弹窗（z-index 高于 Runtime 任务弹窗，后打开的在上层） -->
  <div v-if="logModal.show" class="log-modal-overlay log-layer" @click.self="closeLogModal">
    <div class="log-modal-dialog">
      <div class="log-modal-head">
        <h3 class="log-modal-title">
          <span class="t3" v-if="logModal.info.repoKey">{{ logModal.info.repoKey.replace('gitcode:', '') }}</span>
          <b v-if="logModal.info.issueId">{{ logModal.info.issueId }}</b>
          <span class="t3" v-if="logModal.info.eventSeq">#{{ logModal.info.eventSeq }}</span>
          <span class="t3" v-if="logModal.info.engine" style="color:var(--primary)">🔧 {{ logModal.info.engine }}</span>
          <span class="t3" v-if="logModal.info.createdAt">{{ formatTime(logModal.info.createdAt) }}</span>
        </h3>
        <button class="btn btn-sm" @click="closeLogModal">✕ 关闭</button>
      </div>
      <!-- 时间线 -->
      <div class="log-timeline" v-if="logModal.info.status">
        <span class="log-tl-dot" :class="logModal.info.status === 'completed' ? 'ok' : logModal.info.status === 'failed' ? 'fail' : 'run'"></span>
        <span class="log-tl-text">入队 {{ formatTime(logModal.info.createdAt).slice(11, 19) }}</span>
        <span class="log-tl-arrow" v-if="logModal.info.startedAt">→</span>
        <span class="log-tl-text" v-if="logModal.info.startedAt">开始 {{ formatTime(logModal.info.startedAt).slice(11, 19) }}</span>
        <span class="log-tl-arrow" v-if="logModal.info.completedAt">→</span>
        <span class="log-tl-text" v-if="logModal.info.completedAt">完成 {{ formatTime(logModal.info.completedAt).slice(11, 19) }}</span>
        <span class="log-tl-dur" v-if="logModal.info.startedAt && logModal.info.completedAt">⏱ {{ formatTaskDur(logModal.info) }}</span>
        <span class="log-tl-status" :class="logModal.info.status === 'completed' ? 'ok' : logModal.info.status === 'failed' ? 'fail' : 'run'">{{ logModal.info.status.toUpperCase() }}</span>
      </div>
      <!-- 日志列表 -->
      <div class="log-modal-body">
        <div v-if="logModal.loading" class="tl-diagram-loading">加载日志...</div>
        <div v-else-if="logModal.messages.length === 0" class="log-empty">
          <div v-if="logModal.info.resultText || logModal.info.errors" class="log-error-info">
            <div v-if="logModal.info.resultText" class="log-error-row">结果: {{ logModal.info.resultText }}</div>
            <div v-if="logModal.info.exitCode != null" class="log-error-row" :style="{ color: logModal.info.exitCode !== 0 ? 'var(--danger)' : 'var(--success)' }">Exit Code: {{ logModal.info.exitCode }}</div>
            <div v-if="logModal.info.errors" class="log-error-row" style="color:var(--danger)">{{ logModal.info.errors }}</div>
          </div>
          <div v-else>暂无日志记录</div>
        </div>
        <div v-else class="log-terminal">
          <div v-for="msg in logModal.messages" :key="msg.seq" :class="['log-line', 'log-type-' + msg.type]">
            <span class="log-time t3">{{ (msg.createdAt || '').slice(11, 19) }}</span>
            <span class="log-icon">{{ logTypeIcon(msg.type) }}</span>
            <span class="log-content">{{ msg.content || msg.tool || '' }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Runtime 任务弹窗（z-index 低于日志弹窗） -->
  <div v-if="rtTasksModal.show" class="log-modal-overlay rt-layer" @click.self="closeRtTasksModal">
    <div class="log-modal-dialog">
      <div class="log-modal-head">
        <h3 class="log-modal-title"><b>{{ rtTasksModal.rtName }}</b> · 任务列表</h3>
        <button class="btn btn-sm" @click="closeRtTasksModal">✕ 关闭</button>
      </div>
      <div class="log-modal-body">
        <div v-if="rtTasksModal.loading" class="tl-diagram-loading">加载任务...</div>
        <template v-else>
          <!-- 正在运行 -->
          <div class="hc-section-title" style="padding-left:0">🔄 正在运行 ({{ rtTasksModal.running.length }})</div>
          <table class="vod-table" v-if="rtTasksModal.running.length > 0">
            <thead><tr><th>Issue</th><th>Agent</th><th>Skill</th><th>状态</th><th>开始时间</th></tr></thead>
            <tbody>
              <tr v-for="t in rtTasksModal.running" :key="t.id" class="tl-row-clickable" @click="openLogModal(t.id, { issueId: t.issueId, repoName: (t.repoKey||'').replace('gitcode:','') })">
                <td><span class="t3" style="font-size:11px">{{ (t.repoKey||'').replace('gitcode:','') }}</span> <b>{{ t.issueId || '—' }}</b> <span class="t3">#{{ t.eventSeq }}</span></td>
                <td><span class="status-tag info" style="font-size:11px">{{ t.agentType }}</span></td>
                <td class="t3">{{ t.skill || '—' }}</td>
                <td><span :class="['status-tag', t.status === 'running' ? 'warning' : 'default']">{{ t.status }}</span></td>
                <td class="t3">{{ formatTime(t.startedAt) }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="hc-empty" style="padding:8px 0">无运行中任务</div>

          <!-- 历史任务 -->
          <div class="hc-section-title" style="padding-left:0;padding-top:16px">📋 历史任务 ({{ rtTasksModal.history.length }})</div>
          <table class="vod-table" v-if="rtTasksModal.history.length > 0">
            <thead><tr><th>Issue</th><th>Agent</th><th>Skill</th><th>状态</th><th>耗时</th><th>时间</th></tr></thead>
            <tbody>
              <tr v-for="t in rtTasksModal.history" :key="t.id" class="tl-row-clickable" @click="openLogModal(t.id, { issueId: t.issueId, repoName: (t.repoKey||'').replace('gitcode:','') })">
                <td><span class="t3" style="font-size:11px">{{ (t.repoKey||'').replace('gitcode:','') }}</span> <b>{{ t.issueId || '—' }}</b> <span class="t3">#{{ t.eventSeq }}</span></td>
                <td><span class="status-tag info" style="font-size:11px">{{ t.agentType }}</span></td>
                <td class="t3">{{ t.skill || '—' }}</td>
                <td><span :class="['status-tag', t.status === 'completed' ? 'success' : t.status === 'failed' ? 'danger' : 'default']">{{ t.status }}</span></td>
                <td class="t3">{{ formatTaskDur(t) }}</td>
                <td class="t3">{{ formatTime(t.createdAt) }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="hc-empty" style="padding:8px 0">无历史任务</div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref, computed, nextTick } from 'vue'
import http from '../api'

const activeTab = ref('tasks')
const autoRefresh = ref(false)
let timer = null

const lastUpdate = ref('')
const alerts = ref([])
const alertExpanded = ref(false)

// KPI
const taskKpi = ref({ total: 0, completed: 0, running: 0, failed: 0, queued: 0, stuck: 0 })
const deviceKpi = ref({ total: 0, online: 0, offline: 0, unhealthy: 0 })
const infraKpi = ref({ dbCount: 0, dbHealthy: true, elbCount: 0 })
const infraDetails = ref([])

// 任务 timeline（按 Issue 分组）
const timelineIssues = ref([])
const timelineLoading = ref(false)
const timelineRepo = ref('')
const expandedIssues = ref(new Set())
const issueViewModes = ref({})  // {issueId: 'diagram' | 'events'}
const renderedIssues = ref(new Set())  // 已渲染 mermaid 的 issue
const issueSvgs = ref({})  // {issueId: svgHtml}
const renderingIssues = ref(new Set())  // 正在渲染中的 issue
const diagramRefs = {}  // DOM 元素引用（用于绑定 mermaid 点击事件）

function issueSvg(issueId) {
  return issueSvgs.value[issueId] || ''
}

// 设备概览（Worker + Runtime）
const workers = ref({ total: 0, online: 0, offline: 0 })
const workersList = ref([])
const runtimes = ref({ total: 0, online: 0, offline: 0 })
const runtimesList = ref([])

const taskHealth = computed(() => {
  const f = taskKpi.value.failed
  if (f > 5) return { text: '异常', color: 'var(--danger)' }
  if (f > 0 || taskKpi.value.stuck > 0) return { text: '警告', color: 'var(--warning)' }
  return { text: '正常', color: 'var(--success)' }
})
const deviceHealth = computed(() => {
  const u = deviceKpi.value.unhealthy + deviceKpi.value.offline
  if (deviceKpi.value.total > 0 && u === deviceKpi.value.total) return { text: '异常', color: 'var(--danger)' }
  if (u > 0) return { text: '警告', color: 'var(--warning)' }
  return { text: '正常', color: 'var(--success)' }
})
const infraHealth = computed(() => {
  if (!infraKpi.value.dbHealthy) return { text: '异常', color: 'var(--danger)' }
  return { text: '正常', color: 'var(--success)' }
})

const alertSummary = computed(() => {
  const map = { critical: { label: '严重', color: '#f5222d', bg: '#fff1f0', count: 0 }, warning: { label: '警告', color: '#faad14', bg: '#fffbe6', count: 0 }, info: { label: '提示', color: '#006eff', bg: '#e8f0fe', count: 0 } }
  alerts.value.forEach(a => { if (map[a.severity]) map[a.severity].count++ })
  return Object.fromEntries(Object.entries(map).filter(([, v]) => v.count > 0))
})

function barWidth(val, total) { return total > 0 ? ((val / total) * 100).toFixed(1) + '%' : '0%' }
function taskStatusClass(s) {
  if (s === 'completed') return 'success'
  if (s === 'running' || s === 'dispatched' || s === 'queued') return 'warning'
  if (s === 'failed') return 'danger'
  return 'default'
}
function healthClass(h) {
  if (h === 'online') return 'success'
  if (h === 'offline') return 'default'
  if (h === 'recently_lost' || h === 'about_to_gc') return 'warning'
  return 'danger'
}
function severityText(s) { return { critical: '严重', warning: '警告', info: '提示' }[s] || s }
function dimensionText(d) { return { tasks: '任务', devices: '设备', infra: '基础设施' }[d] || d }
function formatTime(ts) {
  if (!ts) return '—'
  const s = String(ts)
  if (s.startsWith('20') && s.length >= 16) return s.slice(0, 19).replace('T', ' ')
  return s
}

function switchTab(tab) {
  activeTab.value = tab
  if (tab === 'tasks' && timelineIssues.value.length === 0) loadTimeline()
  if (tab === 'devices' && workersList.value.length === 0) loadOverview()
}

async function load() {
  try {
    const { data } = await http.get('/health-center')
    taskKpi.value = data.taskKpi || taskKpi.value
    deviceKpi.value = data.deviceKpi || deviceKpi.value
    infraKpi.value = data.infraKpi || infraKpi.value
    infraDetails.value = data.infraDetails || []
    alerts.value = data.alerts || []
    lastUpdate.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
    if (timelineIssues.value.length === 0) loadTimeline()
    // 自动刷新时也刷新设备概览数据
    loadOverview()
  } catch (e) {
    console.error('[HealthCenter] load failed', e)
    lastUpdate.value = '加载失败'
  }
}

async function loadOverview() {
  try {
    const { data } = await http.get('/health-center/overview')
    const w = data.workers || {}
    workers.value = { total: w.total || 0, online: w.online || 0, offline: w.offline || 0 }
    workersList.value = w.list || []
    const r = data.runtimes || {}
    runtimes.value = { total: r.total || 0, online: r.online || 0, offline: r.offline || 0 }
    runtimesList.value = r.list || []
  } catch (e) {
    console.error('[HealthCenter] loadOverview failed', e)
  }
}

async function loadTimeline() {
  timelineLoading.value = true
  try {
    const { data } = await http.get('/health-center/timeline', { params: { repo: timelineRepo.value } })
    timelineIssues.value = data.issues || []
  } catch (e) {
    console.error('[HealthCenter] loadTimeline failed', e)
    timelineIssues.value = []
  } finally {
    timelineLoading.value = false
  }
}

function toggleIssue(issueId) {
  const s = new Set(expandedIssues.value)
  if (s.has(issueId)) {
    s.delete(issueId)
  } else {
    s.add(issueId)
    if (!issueViewModes.value[issueId]) {
      issueViewModes.value = { ...issueViewModes.value, [issueId]: 'diagram' }
    }
  }
  expandedIssues.value = s
  // 展开时：渲染或重新绑定点击事件
  if (s.has(issueId) && issueViewModes.value[issueId] !== 'events') {
    if (renderedIssues.value.has(issueId)) {
      // 已渲染过：Vue v-html 重新渲染会丢失 data-task-id 和 onclick，
      // 需要在 nextTick 后重新绑定
      const iss = timelineIssues.value.find(i => i.issueId === issueId)
      if (iss) {
        const { evTypes } = buildSequence(iss)
        nextTick(() => bindMermaidClicks(issueId, iss, evTypes))
      }
    } else {
      // 第一次展开：渲染 mermaid
      setTimeout(() => renderMermaidForIssue(issueId), 50)
    }
  }
}

function issueViewMode(issueId) {
  return issueViewModes.value[issueId] || 'diagram'
}

function issueRendered(issueId) {
  return renderedIssues.value.has(issueId)
}

function setIssueView(issueId, mode) {
  issueViewModes.value = { ...issueViewModes.value, [issueId]: mode }
  // 切到时序图时：渲染或重新绑定
  if (mode === 'diagram') {
    if (renderedIssues.value.has(issueId)) {
      // 已渲染过：Vue 重新渲染 v-html 丢失了事件绑定，重新绑定
      const iss = timelineIssues.value.find(i => i.issueId === issueId)
      if (iss) {
        const { evTypes } = buildSequence(iss)
        nextTick(() => bindMermaidClicks(issueId, iss, evTypes))
      }
    } else {
      // 第一次：渲染 mermaid
      setTimeout(() => renderMermaidForIssue(issueId), 50)
    }
  }
}

// ===== Mermaid 时序图渲染 =====
let mermaidEngine = null

async function getMermaid() {
  if (!mermaidEngine) {
    const mod = await import('mermaid')
    mermaidEngine = mod.default
    mermaidEngine.initialize({
      startOnLoad: false,
      securityLevel: 'loose',
      theme: 'base',
      fontFamily: 'inherit',
      sequence: { mirrorActors: false, actorMargin: 60, messageMargin: 40, noteMargin: 10, boxMargin: 8, wrap: true, width: 160 }
    })
  }
  return mermaidEngine
}

function mermaidId(agent) {
  return (agent || 'agent').replace(/[^a-zA-Z0-9_-]/g, '_')
}

function actionText(t) {
  const map = { issue_opened: 'Issue创建', issue_comment: '评论', issue_updated: '更新', a2a_event: 'A2A交办', pull_request_opened: 'PR创建', pull_request_merged: 'PR合并', push: '推送', vod_signal: 'VOD信号', issue_closed: 'Issue关闭' }
  return map[t] || t || '事件'
}

function buildSequence(iss) {
  const agents = []
  for (const ev of iss.events) {
    const a = ev.agentType || '—'
    if (!agents.includes(a)) agents.push(a)
  }
  const lines = ['sequenceDiagram', 'autonumber']
  for (const a of agents) lines.push(`    participant ${mermaidId(a)} as ${a}`)

  const statuses = []
  const evTypes = []  // 'msg' or 'note'
  let prev = null
  for (const ev of iss.events) {
    const a = mermaidId(ev.agentType || '—')
    const t = ev.createdAt ? ev.createdAt.slice(11, 19) : ''
    const label = (t ? t + ' ' : '') + actionText(ev.eventType)
    if (prev && a !== prev) {
      lines.push(`    ${prev}->>${a}: ${label}`)
      statuses.push(ev.status || '')
      evTypes.push('msg')
    } else {
      lines.push(`    Note over ${a}: ${label}`)
      statuses.push(ev.status || '')
      evTypes.push('note')
    }
    prev = a
  }
  return { text: lines.join('\n'), statuses, evTypes }
}

function applySequenceColors(svg, statuses) {
  try {
    const doc = new DOMParser().parseFromString(svg, 'image/svg+xml')
    const root = doc.documentElement
    const defs = root.querySelector('defs')
    if (!defs) return svg
    const colorOf = s => (s === 'failed' ? '#e5484d' : s === 'completed' ? '#2e9e5b' : '#94a3b8')
    const lines = Array.from(root.querySelectorAll('line[marker-start*="sequencenumber"]'))
    lines.forEach((line, i) => {
      const markerId = root.id + '-seqnum-' + i
      const marker = doc.createElementNS('http://www.w3.org/2000/svg', 'marker')
      marker.setAttribute('id', markerId)
      marker.setAttribute('refX', '15'); marker.setAttribute('refY', '15')
      marker.setAttribute('markerWidth', '60'); marker.setAttribute('markerHeight', '40')
      marker.setAttribute('orient', 'auto')
      const circle = doc.createElementNS('http://www.w3.org/2000/svg', 'circle')
      circle.setAttribute('cx', '15'); circle.setAttribute('cy', '15'); circle.setAttribute('r', '6')
      circle.setAttribute('fill', colorOf(statuses[i] || ''))
      marker.appendChild(circle)
      defs.appendChild(marker)
      line.setAttribute('marker-start', 'url(#' + markerId + ')')
    })
    return new XMLSerializer().serializeToString(doc)
  } catch (e) {
    return svg
  }
}

function fixLifelineHeight(svgStr, eventCount) {
  // mermaid 默认生命线高度固定（约 200-400px），事件多时竖线不够长
  // 修复：解析 SVG，找到所有 actor 生命线（stereotype line），延长到 SVG 底部
  try {
    const doc = new DOMParser().parseFromString(svgStr, 'image/svg+xml')
    const root = doc.documentElement
    // 获取 SVG 高度
    const heightAttr = root.getAttribute('height')
    const viewBox = root.getAttribute('viewBox')
    let svgHeight = 0
    if (viewBox) {
      const parts = viewBox.split(' ')
      svgHeight = parseFloat(parts[3]) || 0
    }
    if (!svgHeight && heightAttr) {
      svgHeight = parseFloat(heightAttr) || 0
    }
    if (svgHeight <= 0) return svgStr
    // 事件多时按比例增加高度；事件少时用 mermaid 默认高度（不强制拉长）
    const minHeight = eventCount > 8 ? eventCount * 45 + 100 : 0;
    if (minHeight <= 0 || svgHeight >= minHeight) return svgStr  // 已经够高或不需调整
    // 延长生命线：找到所有竖线（actor 下方的 line）
    const lines = Array.from(root.querySelectorAll('line.actor-line, line.activation0, line.activation1, line.activation2, line.activation3'))
    // mermaid 用 class 不固定，改用筛选：x1==x2 的竖线
    const allLines = Array.from(root.querySelectorAll('line'))
    const vLines = allLines.filter(l => {
      const x1 = parseFloat(l.getAttribute('x1') || 0)
      const x2 = parseFloat(l.getAttribute('x2') || 0)
      return Math.abs(x1 - x2) < 2  // 竖线
    })
    vLines.forEach(l => {
      const y2 = parseFloat(l.getAttribute('y2') || 0)
      if (y2 < minHeight - 20) {
        l.setAttribute('y2', String(minHeight - 10))
      }
    })
    // 更新 SVG 高度
    root.setAttribute('height', String(minHeight))
    if (viewBox) {
      const parts = viewBox.split(' ')
      parts[3] = String(minHeight)
      root.setAttribute('viewBox', parts.join(' '))
    }
    return new XMLSerializer().serializeToString(doc)
  } catch (e) {
    return svgStr
  }
}

async function renderMermaidForIssue(issueId) {
  if (renderingIssues.value.has(issueId)) return
  if (renderedIssues.value.has(issueId)) return

  const iss = timelineIssues.value.find(i => i.issueId === issueId)
  if (!iss) return
  const el = diagramRefs[issueId]
  if (!el) return

  renderingIssues.value = new Set([...renderingIssues.value, issueId])

  try {
    const mmd = await getMermaid()
    const { text, statuses, evTypes } = buildSequence(iss)
    const uid = 'mmd-' + Math.random().toString(36).slice(2, 10)
    try { await mmd.parse(text) } catch (e) { /* parse 失败也尝试 render */ }
    const result = await mmd.render(uid, text)
    const svg = result.svg || result
    const coloredSvg = applySequenceColors(svg, statuses)
    // 修复生命线高度：mermaid 默认生命线高度固定，事件多时竖线不够长
    const fixedSvg = fixLifelineHeight(coloredSvg, iss.events.length)
    issueSvgs.value = { ...issueSvgs.value, [issueId]: fixedSvg }
    renderedIssues.value = new Set([...renderedIssues.value, issueId])
    // 渲染后绑定点击事件（对齐主服务 _bindMermaidClicks）
    nextTick(() => bindMermaidClicks(issueId, iss, evTypes))
  } catch (err) {
    const { text } = buildSequence(iss)
    issueSvgs.value = { ...issueSvgs.value, [issueId]: `<div style="padding:12px;color:var(--danger);font-size:12px">时序渲染失败: ${err.message || err}</div><pre style="padding:8px;background:#0c1222;color:#e0e6f0;font-size:10px;border-radius:6px;overflow-x:auto;white-space:pre-wrap">${text.replace(/</g, '&lt;')}</pre>` }
    renderedIssues.value = new Set([...renderedIssues.value, issueId])
  } finally {
    const s = new Set(renderingIssues.value)
    s.delete(issueId)
    renderingIssues.value = s
  }
}

// 给 mermaid SVG 中的 messageText/noteText 绑定点击 → 打开日志弹窗（对齐主服务 _bindMermaidClicks）
function bindMermaidClicks(issueId, iss, evTypes) {
  const el = diagramRefs[issueId]
  if (!el) return
  const svg = el.querySelector('svg')
  if (!svg) return
  const events = iss.events || []
  if (!events.length) return
  const msgTexts = Array.from(svg.querySelectorAll('text.messageText'))
  const noteTexts = Array.from(svg.querySelectorAll('text.noteText'))
  // 构建 taskId → event 映射（用于事件委托）
  const taskEventMap = {}
  let msgIdx = 0, noteIdx = 0
  events.forEach((ev, i) => {
    const type = evTypes[i] || 'msg'
    let txt = null
    if (type === 'msg') {
      txt = msgTexts[msgIdx]
      msgIdx++
    } else {
      txt = noteTexts[noteIdx]
      noteIdx++
    }
    if (!txt) return
    // 给有 taskId 的 text 元素打标记 + 样式
    if (ev.taskId) {
      txt.setAttribute('data-task-id', ev.taskId)
      txt.setAttribute('data-issue-id', iss.issueId)
      txt.style.cursor = 'pointer'
      txt.style.textDecoration = 'underline'
      txt.style.fill = 'var(--primary)'
      taskEventMap[ev.taskId] = ev
    }
    // tooltip：引擎名 + IP
    const _eng = ev.engine || ''
    if (_eng) {
      const _t = document.createElementNS('http://www.w3.org/2000/svg', 'title')
      _t.textContent = _eng
      txt.appendChild(_t)
    }
  })
  // 用事件委托：在 SVG 上监听 click，通过 data-task-id 找到对应事件
  // 避免 Vue v-html 重新渲染导致事件绑定丢失
  svg.onclick = function(e) {
    const target = e.target.closest('[data-task-id]')
    if (!target) return
    const taskId = target.getAttribute('data-task-id')
    if (taskId && taskEventMap[taskId]) {
      e.stopPropagation()
      openLogModal(taskId, iss)
    }
  }
}
function issueHasFailed(iss) { return iss.events.some(e => e.status === 'failed') }

function issueUrl(iss) {
  const num = (iss.issueId || '').replace('#', '')
  return `https://gitcode.com/${iss.repoName}/issues/${num}`
}
function eventTypeText(t) {
  const map = { issue_opened: 'Issue创建', issue_comment: '评论', issue_updated: '更新', a2a_event: 'A2A交办', pull_request_opened: 'PR创建', pull_request_merged: 'PR合并', push: '推送', vod_signal: 'VOD信号' }
  return map[t] || t || '—'
}
function eventTypeClass(t) {
  if (t === 'a2a_event') return 'info'
  if (t === 'pull_request_merged') return 'success'
  if (t === 'issue_opened') return 'info'
  return 'default'
}
function formatDuration2(d) {
  if (!d) return '—'
  const s = Math.round(Number(d))
  if (isNaN(s) || s <= 0) return '—'
  if (s < 60) return s + 's'
  if (s < 3600) return Math.floor(s / 60) + 'm' + (s % 60) + 's'
  return Math.floor(s / 3600) + 'h' + Math.floor((s % 3600) / 60) + 'm'
}

function pct(v) { return Math.min(v || 0, 100) + '%' }
function resourceColor(v) {
  v = v || 0
  if (v > 80) return 'var(--danger)'
  if (v > 60) return 'var(--warning)'
  return 'var(--success)'
}
function healthColor(h) {
  if (h === 'online') return 'var(--success)'
  if (h === 'recently_lost') return 'var(--warning)'
  if (h === 'about_to_gc') return 'var(--danger)'
  return 'var(--text-tertiary)'
}
function loadColor(running, max) {
  if (running >= max * 2) return 'var(--danger)'
  if (running >= max) return 'var(--warning)'
  if (running > 0) return 'var(--success)'
  return 'var(--text-tertiary)'
}

function toggleAuto() {
  if (autoRefresh.value) { timer = setInterval(load, 15000) }
  else { clearInterval(timer); timer = null }
}

// ===== 任务日志弹窗 =====
const logModal = ref({ show: false, loading: false, messages: [], info: {} })

async function openLogModal(taskId, issue) {
  logModal.value = { show: true, loading: true, messages: [], info: { issueId: issue.issueId, repoKey: issue.repoName ? 'gitcode:' + issue.repoName : '' } }
  try {
    const [msgsResp, infoResp] = await Promise.all([
      http.get(`/health-center/tasks/${taskId}/messages`),
      http.get(`/health-center/tasks/${taskId}/info`),
    ])
    const info = infoResp.data || {}
    if (!info.repoKey && issue.repoName) info.repoKey = 'gitcode:' + issue.repoName
    logModal.value = { show: true, loading: false, messages: msgsResp.data || [], info }
  } catch (e) {
    console.error('[HealthCenter] loadLog failed', e)
    logModal.value = { show: true, loading: false, messages: [], info: { error: e.message } }
  }
}

function closeLogModal() {
  logModal.value = { show: false, loading: false, messages: [], info: {} }
}

function logTypeIcon(t) {
  const map = { step_start: '▶', step_finish: '✓', text: '💬', reasoning: '🤔', tool_use: '🔧', error: '❌', done: '✅' }
  return map[t] || '•'
}

// ===== Runtime 任务弹窗 =====
const rtTasksModal = ref({ show: false, loading: false, rtName: '', running: [], history: [] })

async function openRtTasksModal(rt) {
  rtTasksModal.value = { show: true, loading: true, rtName: rt.name, running: [], history: [] }
  try {
    const { data } = await http.get(`/health-center/runtimes/${rt.id}/tasks`, { params: { limit: 50 } })
    rtTasksModal.value = { show: true, loading: false, rtName: rt.name, running: data.running || [], history: data.history || [] }
  } catch (e) {
    console.error('[HealthCenter] loadRtTasks failed', e)
    rtTasksModal.value = { show: true, loading: false, rtName: rt.name, running: [], history: [] }
  }
}

function closeRtTasksModal() {
  rtTasksModal.value = { show: false, loading: false, rtName: '', running: [], history: [] }
}

function formatAvgDur(d) {
  if (!d || d <= 0) return '—'
  if (d < 60) return Math.round(d) + 's'
  return Math.floor(d / 60) + 'm' + Math.round(d % 60) + 's'
}

function avgDurColor(d) {
  if (!d || d <= 0) return 'var(--text-tertiary)'
  if (d > 300) return 'var(--danger)'
  if (d > 60) return 'var(--warning)'
  return 'var(--success)'
}

function formatTaskDur(t) {
  if (!t.startedAt || !t.completedAt) return '—'
  const s = (new Date(t.completedAt) - new Date(t.startedAt)) / 1000
  if (isNaN(s) || s <= 0) return '—'
  return formatDuration2(s)
}
onMounted(load)
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
/* 告警中心横幅 */
.alert-banner { border: 1px solid #ffa39e; border-radius: var(--radius); background: #fff1f0; margin-bottom: 16px; overflow: hidden; }
.alert-banner-head { display: flex; align-items: center; gap: 10px; padding: 10px 16px; cursor: pointer; user-select: none; }
.alert-icon { font-size: 16px; }
.alert-title { font-weight: 600; font-size: 14px; color: var(--danger); }
.alert-count { font-size: 12px; font-weight: 600; color: #fff; background: var(--danger); padding: 2px 8px; border-radius: 10px; }
.alert-summary { display: flex; gap: 6px; margin-left: 8px; }
.alert-chip { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 8px; }
.alert-expand-icon { margin-left: auto; font-size: 10px; color: var(--text-secondary); }
.alert-banner-body { border-top: 1px solid #ffccc7; background: #fff; max-height: 320px; overflow-y: auto; }
.alert-item { display: flex; align-items: center; gap: 10px; padding: 8px 16px; border-bottom: 1px solid var(--border-soft); font-size: 13px; }
.alert-item:last-child { border-bottom: none; }
.alert-item-severity { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 4px; white-space: nowrap; }
.alert-item-severity.critical { background: #fff1f0; color: #f5222d; }
.alert-item-severity.warning { background: #fffbe6; color: #faad14; }
.alert-item-severity.info { background: #e8f0fe; color: #006eff; }
.alert-item-source { font-size: 11px; padding: 2px 6px; border-radius: 4px; white-space: nowrap; }
.alert-item-source.src-tasks { background: #e8f0fe; color: #006eff; }
.alert-item-source.src-devices { background: #f6ffed; color: #52c41a; }
.alert-item-source.src-infra { background: #fff7e6; color: #d48806; }
.alert-item-title { font-weight: 500; min-width: 80px; }
.alert-item-detail { color: var(--text-secondary); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.alert-item-time { font-size: 11px; white-space: nowrap; }

/* 三维度卡片 */
.hc-dimensions { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
.hc-dim-card { background: var(--bg-white); border: 2px solid transparent; border-radius: var(--radius-lg); padding: 16px; cursor: pointer; transition: all 0.2s; box-shadow: var(--card-shadow); }
.hc-dim-card:hover { box-shadow: var(--card-shadow-hover); }
.hc-dim-card.active { border-color: var(--primary); background: var(--primary-bg); }
.hc-dim-head { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
.hc-dim-icon { font-size: 20px; }
.hc-dim-name { font-size: 15px; font-weight: 600; }
.hc-dim-health { margin-left: auto; font-size: 12px; font-weight: 600; }
.hc-kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 10px; }
.hc-kpi { display: flex; flex-direction: column; align-items: center; gap: 2px; }
.hc-kpi-val { font-size: 22px; font-weight: 700; line-height: 1.2; }
.hc-kpi-label { font-size: 11px; color: var(--text-tertiary); }
.hc-dim-bar { display: flex; height: 4px; border-radius: 2px; overflow: hidden; background: #f0f0f0; }
.hc-dim-bar-seg { height: 100%; transition: width 0.3s; }
.hc-dim-bar-seg.success { background: var(--success); }
.hc-dim-bar-seg.warning { background: var(--warning); }
.hc-dim-bar-seg.danger { background: var(--danger); }

/* 详情卡片 */
.hc-detail-card { padding: 0; overflow: hidden; }
.hc-detail-head { display: flex; align-items: center; gap: 10px; padding: 14px 20px; border-bottom: 1px solid var(--border-soft); }
.hc-detail-head h3 { font-size: 14px; font-weight: 600; }

/* 基础设施网格 */
.hc-infra-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; padding: 20px; }
.hc-infra-card { border: 1px solid var(--border-color); border-radius: var(--radius); overflow: hidden; }
.hc-infra-head { display: flex; align-items: center; gap: 8px; padding: 10px 14px; background: #fafafa; border-bottom: 1px solid var(--border-soft); }
.hc-infra-icon { font-size: 18px; }
.hc-infra-name { font-weight: 600; font-size: 14px; flex: 1; }
.hc-infra-body { padding: 10px 14px; }
.hc-infra-row { display: flex; justify-content: space-between; font-size: 13px; padding: 4px 0; }
.hc-infra-row span:first-child { color: var(--text-tertiary); }
.hc-infra-row span:last-child { font-weight: 500; }

@media (max-width: 900px) {
  .hc-dimensions { grid-template-columns: 1fr; }
}

/* Issue 分组列表 */
.tl-issue { border: 1px solid var(--border-color); border-radius: 8px; overflow: hidden; margin-bottom: 8px; }
.tl-issue-head { display: flex; align-items: center; gap: 10px; padding: 10px 14px; cursor: pointer; background: #fafbfc; }
.tl-issue-head:hover { background: #f5f9ff; }
.tl-arrow { font-size: 12px; color: var(--text-tertiary); width: 14px; }
.tl-issue-id { font-size: 13px; min-width: 40px; }
.tl-issue-link { color: var(--primary); text-decoration: none; }
.tl-issue-link:hover { text-decoration: underline; }
.tl-issue-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.tl-issue-count { font-size: 11px; white-space: nowrap; }
.tl-issue-status { font-size: 10px; font-weight: 600; white-space: nowrap; }
.tl-issue-status.fail { color: var(--danger); }
.tl-issue-status.ok { color: var(--success); }
.tl-issue-time { font-size: 11px; white-space: nowrap; }
.tl-issue-body { border-top: 1px solid var(--border-color); padding: 10px 14px; }
.tl-view-tabs { display: flex; gap: 0; margin-bottom: 10px; }
.tl-view-btn { padding: 5px 12px; font-size: 12px; border: 1px solid var(--border-color); background: #fff; color: var(--text-secondary); cursor: pointer; }
.tl-view-btn:first-child { border-radius: 6px 0 0 6px; }
.tl-view-btn:last-child { border-radius: 0 6px 6px 0; border-left: none; }
.tl-view-btn.active { background: var(--primary); color: #fff; border-color: var(--primary); }
.tl-diagram-container { overflow-x: auto; padding: 8px 0; min-height: 60px; }
.tl-diagram-loading { padding: 20px; text-align: center; color: var(--text-tertiary); font-size: 12px; }
.tl-row-fail td { background: #fff1f0 !important; }
.tl-row-ok td { background: #f6ffed !important; }

/* 设备概览 */
.hc-overview-kpi { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0; border: 1px solid var(--border-color); border-radius: var(--radius); overflow: hidden; margin: 12px 20px; }
.hc-kpi-card { display: flex; flex-direction: column; align-items: center; gap: 2px; padding: 14px; border-right: 1px solid var(--border-soft); }
.hc-kpi-card:last-child { border-right: none; }
.hc-section-title { font-size: 13px; font-weight: 600; padding: 12px 20px 6px; }
.hc-empty { text-align: center; padding: 24px; color: var(--text-tertiary); font-size: 13px; }

.hc-worker-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; padding: 0 20px 12px; }
.hc-worker-card { background: #fff; border: 1px solid var(--border-color); border-radius: var(--radius); padding: 12px 14px; box-shadow: var(--card-shadow); }
.hc-worker-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.hc-health-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
.hc-worker-name { font-size: 13px; }
.hc-worker-subname { font-size: 10px; color: var(--text-tertiary); max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hc-worker-health { margin-left: auto; font-size: 10px; font-weight: 600; }
.hc-worker-meta { display: grid; grid-template-columns: 1fr 1fr; gap: 2px 12px; font-size: 10px; color: var(--text-tertiary); margin-bottom: 8px; }
.hc-resource-bar { display: flex; align-items: center; gap: 6px; font-size: 10px; margin-bottom: 4px; }
.hc-resource-label { color: var(--text-tertiary); min-width: 30px; }
.hc-resource-track { flex: 1; height: 8px; background: #f0f0f0; border-radius: 4px; overflow: hidden; min-width: 60px; }
.hc-resource-fill { height: 100%; border-radius: 4px; transition: width 0.3s; }
.hc-resource-val { font-weight: 600; min-width: 30px; text-align: right; }

/* 任务日志弹窗 */
.log-modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.5); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.log-modal-overlay.rt-layer { z-index: 1100; }
.log-modal-overlay.log-layer { z-index: 1200; }
.log-modal-dialog { width: 860px; max-width: 94vw; max-height: 85vh; background: #fff; border-radius: var(--radius-lg); display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 8px 32px rgba(0,0,0,0.2); }
.log-modal-head { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; border-bottom: 1px solid var(--border-color); }
.log-modal-title { font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.log-timeline { display: flex; align-items: center; gap: 6px; padding: 8px 18px; border-bottom: 1px solid var(--border-soft); font-size: 11px; }
.log-tl-dot { width: 8px; height: 8px; border-radius: 50%; }
.log-tl-dot.ok { background: var(--success); } .log-tl-dot.fail { background: var(--danger); } .log-tl-dot.run { background: var(--warning); }
.log-tl-arrow { color: var(--text-tertiary); }
.log-tl-status { margin-left: auto; font-weight: 600; }
.log-tl-dur { color: var(--primary); font-weight: 600; }
.log-tl-status.ok { color: var(--success); } .log-tl-status.fail { color: var(--danger); } .log-tl-status.run { color: var(--warning); }
.log-modal-body { flex: 1; overflow-y: auto; padding: 14px 18px; }
.log-empty { text-align: center; padding: 32px; color: var(--text-tertiary); }
.log-error-info { text-align: left; background: #fff1f0; border: 1px solid #ffa39e; border-radius: 8px; padding: 12px; }
.log-error-row { font-size: 12px; margin-bottom: 4px; font-family: monospace; }
.log-terminal { background: #0c1222; border-radius: 8px; padding: 12px; max-height: 60vh; overflow-y: auto; }
.log-line { display: flex; align-items: flex-start; gap: 8px; padding: 3px 0; font-size: 12px; line-height: 1.6; border-bottom: 1px solid rgba(255,255,255,0.04); font-family: Menlo, Monaco, Consolas, monospace; }
.log-time { color: #5b667b; font-size: 10px; min-width: 55px; flex-shrink: 0; }
.log-icon { min-width: 16px; flex-shrink: 0; }
.log-content { color: #e0e6f0; word-break: break-all; white-space: pre-wrap; }
.log-type-step_start .log-content { color: #5b9eff; } .log-type-step_finish .log-content { color: #1ca44c; }
.log-type-text .log-content { color: #e0e6f0; } .log-type-reasoning .log-content { color: #b388ff; }
.log-type-tool_use .log-content { color: #ea5a0c; } .log-type-error .log-content { color: #dc2828; }
.log-type-done .log-content { color: #1ca44c; }

/* 事件行可点击 */
.tl-row-clickable { cursor: pointer; }
.tl-row-clickable:hover td { background: #f0f5ff !important; }
</style>
