<template>
  <div class="rtv">
    <!-- ═══ 顶部工具栏：tab + 刷新 + 搜索 + 添加设备 ═══ -->
    <div class="rtv-toolbar">
      <button :class="['bt', tab === 'engines' ? 'bt-p' : 'bt-o']" @click="switchTab('engines')">
        {{ t('tab.engines') }}
      </button>
      <button :class="['bt', tab === 'workers' ? 'bt-p' : 'bt-o']" @click="switchTab('workers')">
        {{ t('tab.workers') }}
      </button>
      <div class="sp"></div>
      <button class="bt bt-o" @click="refreshRuntimes()">⟳ {{ t('refresh') }}</button>
      <div class="rtv-searchgrp">
        <input
          v-model="searchDraft"
          class="rtv-search"
          :placeholder="t('search_placeholder')"
          @keydown.enter.exact.prevent="applySearch"
          @change="applySearch"
        />
        <button class="rtv-searchbtn" @click="applySearch">
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            style="display: block"
          >
            <circle cx="11" cy="11" r="8" />
            <path d="M21 21l-4.35-4.35" />
          </svg>
        </button>
      </div>
      <button class="rtv-addbtn" @click="openAddDevicePanel()">
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2.5"
          stroke-linecap="round"
          stroke-linejoin="round"
          style="flex-shrink: 0"
        >
          <path d="M12 5v14M5 12h14" />
        </svg>
        {{ t('action.add_device') }}
      </button>
    </div>

    <!-- ═══ 加载/错误/空态 ═══ -->
    <div v-if="loadError" class="error">{{ loadError }}</div>
    <div v-else-if="!loaded" class="loading">{{ tc('loading') }}</div>
    <div v-else-if="isEmpty" class="rtv-empty">
      <div class="rtv-empty-ic">🔧</div>
      <div class="rtv-empty-t">暂无引擎</div>
      <div class="rtv-empty-d">当前组织下暂无接入的引擎或设备</div>
    </div>

    <!-- ═══ 主内容 ═══ -->
    <template v-else>
      <!-- 筛选标签栏 -->
      <div class="rtv-filters">
        <span class="rtv-filter-lbl">筛选</span>
        <!-- Issue #473：范围筛选（全部/仓库/个人） -->
        <span
          v-for="opt in RT_RANGE_OPTIONS"
          :key="'rg' + opt.key"
          :class="['rtv-chip', { on: rtRange === opt.key }]"
          @click="setRange(opt.key)"
          >{{ t(opt.labelKey) }}</span
        >
        <span
          v-for="ty in availTypes"
          :key="'t' + ty"
          :class="{ on: filterType === ty }"
          class="rtv-chip"
          @click="toggleType(ty)"
          ><span class="rtv-chip-dot" :style="{ background: ENGINE_DOT[ty] || '#999' }"></span
          >{{ ty }}</span
        >
        <span v-if="availTypes.length" class="rtv-chip-sep"></span>
        <span
          v-for="s in availScopes"
          :key="'s' + s"
          :class="{ on: filterScope === s }"
          class="rtv-chip"
          @click="toggleScope(s)"
          >{{ SCOPE_LABELS[s] || s }}</span
        >
        <template v-if="tab === 'engines'">
          <span v-if="availScenes.length" class="rtv-chip-sep"></span>
          <div v-if="availScenes.length" class="rtv-ddwrap">
            <span
              :class="['rtv-chip', 'rtv-chip-multi', { on: filterScene.length }]"
              @click.stop="sceneDropdown = !sceneDropdown"
            >
              {{ t('label.scene') }}{{ filterScene.length ? '(' + filterScene.length + ')' : ''
              }}<span class="rtv-caret">▼</span>
            </span>
            <div v-if="sceneDropdown" class="rtv-dd">
              <label v-for="s in availScenes" :key="s" class="rtv-dd-item">
                <input
                  type="checkbox"
                  :checked="filterScene.includes(s)"
                  :style="{ accentColor: SCENE_TAG_COLORS[s] || '#10b981' }"
                  @change="toggleMulti('scene', s)"
                />
                <span
                  class="rtv-tagpill"
                  :style="{ background: SCENE_TAG_COLORS[s] || '#10b981' }"
                  >{{ sceneDisplayName(s) }}</span
                >
              </label>
            </div>
          </div>
          <div v-if="availTags.length" class="rtv-ddwrap">
            <span
              :class="['rtv-chip', 'rtv-chip-multi', { on: filterTag.length }]"
              :style="
                filterTag.length
                  ? {
                      borderColor: RT_TAG_COLORS[filterTag[0]] || '#999',
                      background: RT_TAG_COLORS[filterTag[0]] || '#999',
                      color: '#fff',
                    }
                  : {}
              "
              @click.stop="tagDropdown = !tagDropdown"
            >
              {{ t('label.role_tags') }}{{ filterTag.length ? '(' + filterTag.length + ')' : ''
              }}<span class="rtv-caret">▼</span>
            </span>
            <div v-if="tagDropdown" class="rtv-dd">
              <label v-for="tg in availTags" :key="tg" class="rtv-dd-item">
                <input
                  type="checkbox"
                  :checked="filterTag.includes(tg)"
                  :style="{ accentColor: RT_TAG_COLORS[tg] || '#999' }"
                  @change="toggleMulti('tag', tg)"
                />
                <span class="rtv-tagpill" :style="{ background: RT_TAG_COLORS[tg] || '#999' }">{{
                  rtTagLabel(tg)
                }}</span>
              </label>
            </div>
          </div>
        </template>
        <span
          v-if="
            filterType ||
            filterScope ||
            filterScene.length ||
            filterTag.length ||
            search ||
            (rtRange && rtRange !== 'all')
          "
          class="rtv-clear"
          @click="clearFilters"
          >✕ 清除</span
        >
      </div>

      <!-- 汇总统计栏 -->
      <div v-if="summaryList.length" class="rtv-summary">
        <span style="color: var(--t2); font-weight: 500">{{
          t('stats.total', { count: summaryList.length })
        }}</span>
        <template v-for="c in summaryCounts" :key="c.k">
          <span v-if="c.n" class="rtv-sum-item"
            ><span class="rtv-sum-dot" :style="{ background: HEALTH_BADGE[c.k].color }"></span
            >{{ t('health.' + c.k) }} {{ c.n }}</span
          >
        </template>
      </div>

      <!-- Engines 卡片网格 -->
      <template v-if="tab === 'engines'">
        <div v-if="!filteredEngines.length">
          <!-- Issue #1167：有筛选 → 内联「暂无引擎」（筛选栏保留）；无筛选 → 接入引导 -->
          <div v-if="hasActiveFilters" class="ts t2" style="padding: 12px">
            {{ t('empty.no_engines') }}
          </div>
          <div v-else class="rtv-empty-guide">
            <div class="rtv-empty-guide-t">{{ t('empty.no_engines_title') }}</div>
            <div class="rtv-empty-guide-s">{{ t('empty.no_engines_sub') }}</div>
          </div>
        </div>
        <div v-else class="rtv-grid">
          <div
            v-for="rt in filteredEngines"
            :key="rt.id"
            class="rtv-ecard"
            :style="cardLeftBorder(rt.health || rt.status || 'offline', rt)"
            @click="openEngineDetail(rt.id)"
          >
            <img v-if="watermark(rt.name)" :src="watermark(rt.name)" class="rtv-watermark" alt="" />
            <div class="rtv-ecard-head">
              <span :class="['rtv-hbadge', rt.health || 'offline']"
                ><span class="rtv-dot7"></span>{{ healthLabel(rt.health) }}</span
              ><span v-if="healthHintKey(rt)" class="rtv-hhelp" :title="t(healthHintKey(rt))"
                >?</span
              ><span
                v-if="workerVersionStale(rt)"
                class="rtv-vstale"
                :title="t('label.worker_version_stale_hint')"
                >⬆ {{ t('label.worker_version_stale') }}</span
              >
              <span :class="['rtv-sbadge', scopeKey(rt)]">{{ scopeBadgeLabel(rt) }}</span>
              <span class="rtv-ename">
                <span class="rtv-ename-b">{{ rt.alias || engineDisplayName(rt) || '—' }}</span>
                <span v-if="rt.alias" class="rtv-ename-s"
                  >({{ engineDisplayName(rt) || '—' }})</span
                >
                <svg
                  class="rtv-copy"
                  width="13"
                  height="13"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  @click.stop="copyEngineName(rt)"
                >
                  <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                  <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                </svg>
              </span>
            </div>
            <div class="rtv-ecard-grid">
              <div class="rtv-kv">
                <span class="rtv-k">{{ t('label.engine_type') }}</span> {{ rt.name }}
              </div>
              <div class="rtv-kv">
                <span class="rtv-k">{{ t('label.version') }}</span> v{{ rt.version || '—' }}
              </div>
              <div class="rtv-kv">
                <span class="rtv-k">{{ t('label.worker') }}</span>
                {{ rt.worker_name || (rt.is_local ? t('label.local') : '—') }}
              </div>
              <div class="rtv-kv">
                <span class="rtv-k">{{ t('label.skills_label') }}</span>
                {{ t('label.skills_count', { count: (rt.local_skills || []).length }) }}
              </div>
              <div class="rtv-kv">
                <span class="rtv-k">{{ t('label.scene') }}</span
                ><span v-html="sceneTagSummary(rt.scene_tags)"></span>
              </div>
              <div class="rtv-kv">
                <span class="rtv-k">{{ t('label.tags') }}</span
                ><span v-html="engineTagSummary(rt.agent_tags)"></span>
              </div>
              <div class="rtv-kv">
                <span class="rtv-k">{{ t('label.installed') }}</span>
                {{ rt.installed ? t('label.yes') : t('label.no') }}
              </div>
              <div class="rtv-kv">
                <span class="rtv-k">健康分</span> <span v-html="healthScoreHtml(rt)"></span>
              </div>
              <!-- Issue #865：模型（配置模型优先，回退最近使用；title 说明来源） -->
              <div class="rtv-kv rtv-kv-full" :title="engineModelTooltip(rt)">
                <span class="rtv-k">{{ t('label.model') }}</span>
                <span class="rtv-vellipsis">{{ engineModelDisplay(rt) }}</span>
              </div>
            </div>
          </div>
        </div>
        <div v-if="totalPages('engines') > 1" class="rtv-pager">
          <button :disabled="page <= 1" @click="goPage('engines', page - 1)">‹</button>
          <template v-for="pn in pageNums('engines')" :key="pn">
            <span v-if="pn === '…'" class="rtv-ellipsis">…</span>
            <button v-else :class="{ on: pn === page }" @click="goPage('engines', pn)">
              {{ pn }}
            </button>
          </template>
          <button :disabled="page >= totalPages('engines')" @click="goPage('engines', page + 1)">
            ›
          </button>
          <span class="rtv-pager-sum">共 {{ rtTotal }} 条 / {{ totalPages('engines') }} 页</span>
        </div>
      </template>

      <!-- Workers 卡片网格 -->
      <template v-else>
        <div v-if="!devices.length" class="ts t2" style="padding: 12px">
          {{ t('empty.no_workers') }}
        </div>
        <div v-else class="rtv-grid">
          <div
            v-for="d in sortedDevices"
            :key="d.credential_id + '-' + workerDisplayName(d)"
            class="rtv-wcard"
            :style="cardLeftBorder(d.health, d)"
          >
            <div class="rtv-wcard-head">
              <span :class="['rtv-hbadge', d.health || 'offline']"
                ><span class="rtv-dot7"></span>{{ healthLabel(d.health)
                }}<span v-if="healthHintKey(d)" class="rtv-hhelp" :title="t(healthHintKey(d))"
                  >?</span
                >{{
                  d.credential_id
                    ? ' · ' + relTime(d.last_seen_at)
                    : ' · ' + t('label.local_direct')
                }}</span
              >
              <span :class="['rtv-sbadge', scopeKey(d)]">{{ scopeBadgeLabel(d) }}</span>
              <span class="rtv-wname">{{ workerDisplayName(d) }}</span>
              <span class="rtv-wip">{{ d.worker_ip || '' }}</span>
              <button v-if="canManage(d)" class="bt bt-o bt-s rtv-wbtn" @click="openScopeSwitcher(d)">
                {{ scopeShort(d.scope) }}
              </button>
              <button
                v-if="d.credential_id && isOwner(d)"
                class="bt bt-o bt-s rtv-wbtn"
                @click="renameCredential(d.credential_id)"
              >
                {{ t('action.rename') }}
              </button>
            </div>
            <div class="rtv-wcard-meta">
              <div class="rtv-wcard-cols">
                <div class="rtv-kv">
                  <span class="rtv-k">{{ t('label.owner') }}</span> {{ d.owner || '—' }}
                </div>
                <div class="rtv-kv">
                  <span class="rtv-k">{{ t('label.credential') }}</span>
                  <b
                    :style="{ color: d.credential_status === 'revoked' ? 'var(--r)' : 'var(--g)' }"
                    >{{ d.credential_status || '' }}</b
                  >
                </div>
                <div v-if="(d.scope || 'global') === 'repo' && d.repo_key" class="rtv-kv">
                  <span class="rtv-k">{{ t('label.repo_key') }}</span> {{ d.repo_key }}
                </div>
              </div>
              <button
                v-if="d.credential_id && isOwner(d)"
                class="bt bt-r bt-s rtv-wbtn"
                @click="revokeCredential(d.credential_id)"
              >
                {{ t('action.revoke') }}
              </button>
            </div>
            <div class="rtv-wcard-engines">
              <span class="rtv-k">{{ t('label.hosted_engines') }}</span>
              <span class="rtv-wcard-clis">
                <template v-if="(d.runtimes || []).length">
                  <span
                    v-for="rt in d.runtimes"
                    :key="rt.id"
                    class="rtv-cli"
                    @click.stop="openEngineDetail(rt.id)"
                    >{{ rt.alias || rt.name }} v{{ rt.version || ''
                    }}{{ rt.healthy ? ' ✓' : ' ✗' }}</span
                  >
                </template>
                <template v-else>—</template>
              </span>
            </div>
          </div>
        </div>
        <div v-if="totalPages('workers') > 1" class="rtv-pager">
          <button :disabled="devPage <= 1" @click="goPage('workers', devPage - 1)">‹</button>
          <template v-for="pn in pageNums('workers')" :key="pn">
            <span v-if="pn === '…'" class="rtv-ellipsis">…</span>
            <button v-else :class="{ on: pn === devPage }" @click="goPage('workers', pn)">
              {{ pn }}
            </button>
          </template>
          <button
            :disabled="devPage >= totalPages('workers')"
            @click="goPage('workers', devPage + 1)"
          >
            ›
          </button>
          <span class="rtv-pager-sum">共 {{ devTotal }} 条 / {{ totalPages('workers') }} 页</span>
        </div>
      </template>

      <!-- 图例 -->
      <div class="rtv-legend">
        <span class="rtv-legend-lbl">{{ t('devspace.legend_label') }}</span>
        <span v-for="(v, k) in HEALTH_NAMES" :key="k" class="rtv-legend-item">
          <span
            class="rtv-sum-dot"
            :style="{ background: (HEALTH_BADGE[k] || {}).color || 'var(--t3)' }"
          ></span>
          {{ t('health.' + v)
          }}{{ k === 'recently_lost' ? '(<5min)' : k === 'about_to_gc' ? '(>6d)' : '' }}
        </span>
      </div>
    </template>

    <!-- ═══ 引擎详情弹窗 ═══ -->
    <div v-if="engineDetail" class="rtv-modal-ov" @click.self="closeEngineDetail()">
      <div class="rtv-engdetail">
        <div class="rtv-ed-head">
          <span :class="['rtv-hbadge', engineDetail.health || 'offline']"
            ><span class="rtv-dot7"></span>{{ healthLabel(engineDetail.health) }}</span
          >
          <span class="rtv-ed-name">{{
            engineDetail.alias || engineDisplayName(engineDetail)
          }}</span>
          <span v-if="engineDetail.alias" class="rtv-ename-s"
            >({{ engineDisplayName(engineDetail) }})</span
          >
          <button class="bt bt-o bt-s rtv-ed-alias" @click="editRuntimeAlias(engineDetail)">
            {{ t('action.edit_alias') }}
          </button>
          <span v-if="engineDetail.is_local" class="rtv-localpill">{{
            t('label.local_direct')
          }}</span>
          <button class="bt bt-o" style="margin-left: auto" @click="closeEngineDetail()">
            {{ t('action.close') }}
          </button>
        </div>
        <div class="rtv-ed-grid">
          <div class="rtv-kv">
            <span class="rtv-k" style="min-width: 70px">{{ t('label.engine_type') }}</span>
            {{ engineDetail.name }}
          </div>
          <div class="rtv-kv">
            <span class="rtv-k" style="min-width: 70px">{{ t('label.version') }}</span> v{{
              engineDetail.version || '—'
            }}
          </div>
          <div class="rtv-kv">
            <span class="rtv-k" style="min-width: 70px">{{ t('label.health_status') }}</span>
            {{ engineDetail.healthy ? t('label.healthy') : t('label.unhealthy') }}
          </div>
          <div class="rtv-kv">
            <span class="rtv-k" style="min-width: 70px">{{ t('label.worker') }}</span>
            {{ engineDetail.worker_name || (engineDetail.is_local ? t('label.local') : '—') }}
          </div>
          <!-- Issue #865：配置模型 + 最后使用 -->
          <div class="rtv-kv">
            <span class="rtv-k" style="min-width: 70px">{{ t('label.model_configured') }}</span>
            <span :title="engineModelTooltip(engineDetail)" class="rtv-vellipsis">{{
              engineDetail.model_name || '—'
            }}</span>
          </div>
          <div class="rtv-kv">
            <span class="rtv-k" style="min-width: 70px">{{ t('label.model_last_used') }}</span>
            <span class="rtv-ts">{{
              recentModelList(engineDetail)[0]
                ? relTime(recentModelList(engineDetail)[0].last_used_at)
                : '—'
            }}</span>
          </div>
          <div class="rtv-kv">
            <span class="rtv-k" style="min-width: 70px">IP</span>
            {{ engineDetail.worker_ip || '—' }}
          </div>
          <div class="rtv-kv">
            <span class="rtv-k" style="min-width: 70px">{{ t('label.scope_label') }}</span>
            {{ scopeLabel(engineDetail.scope) }}
          </div>
          <div class="rtv-kv">
            <span class="rtv-k" style="min-width: 70px">{{ t('label.owner') }}</span>
            {{ engineDetail.owner || '—' }}
          </div>
          <div class="rtv-kv">
            <span class="rtv-k" style="min-width: 70px">{{ t('label.installed') }}</span>
            {{ engineDetail.installed ? t('label.yes') : t('label.no') }}
          </div>
          <div class="rtv-kv">
            <span class="rtv-k" style="min-width: 70px">健康分</span>
            <span v-html="healthScoreHtml(engineDetail)"></span>
            <button
              v-if="healthCanReset(engineDetail)"
              class="bt bt-o rtv-reset-btn"
              @click.stop="resetEngineHealth(engineDetail)"
            >
              {{ t('action.reset_health') }}
            </button>
          </div>
        </div>
        <!-- Issue #865：最近使用模型（按用量 Top 3，来源 agent_tasks 上报） -->
        <div class="rtv-ed-sec">
          <div class="rtv-ed-sechead">
            <span>{{ t('label.recent_models') }}</span>
            <span class="rtv-ts">{{ t('label.recent_models_desc') }}</span>
          </div>
          <div v-if="!recentModelList(engineDetail).length" class="rtv-muted">
            {{ t('label.recent_models_empty') }}
          </div>
          <div v-else class="rtv-models">
            <div v-for="m in recentModelList(engineDetail)" :key="m.model" class="rtv-model-row">
              <span class="rtv-vellipsis">{{ m.model }}</span>
              <span class="rtv-ts">{{ t('label.model_used_count', { count: m.count }) }}</span>
              <span class="rtv-ts"
                >{{ t('label.model_last_used') }}
                {{ m.last_used_at ? relTime(m.last_used_at) : '—' }}</span
              >
            </div>
          </div>
        </div>
        <!-- 场景标签 -->
        <div class="rtv-ed-sec">
          <div class="rtv-ed-sechead">
            <span>{{ t('label.scene_tags') }}</span>
            <button class="bt bt-p" @click="saveSceneTags(engineDetail)">
              {{ t('action.save') }}
            </button>
          </div>
          <div v-if="!sceneTagsCache" class="rtv-muted">{{ t('empty.loading') }}</div>
          <div v-else-if="!sceneTagsCache.length" class="rtv-muted">{{ t('empty.no_scenes') }}</div>
          <div v-else class="rtv-ed-tags">
            <label v-for="s in sceneTagsCache" :key="s.name" class="rtv-ed-taglbl">
              <input
                v-model="edSceneTags"
                type="checkbox"
                :value="s.name"
                :style="{ accentColor: SCENE_TAG_COLORS[s.name] || '#10b981' }"
                @change="onEdSceneTagChange"
              />
              <span
                class="rtv-tagpill"
                :style="{ background: SCENE_TAG_COLORS[s.name] || '#10b981' }"
                >{{ s.display_name }}</span
              >
            </label>
          </div>
          <div class="rtv-muted rtv-ed-desc">{{ edSceneTagsDesc }}</div>
        </div>
        <!-- 角色标签 -->
        <div class="rtv-ed-sec">
          <div class="rtv-ed-sechead">
            <span>{{ t('label.role_tags') }}</span>
            <button class="bt bt-p" @click="saveEngineTags(engineDetail)">
              {{ t('action.save_tags') }}
            </button>
          </div>
          <div class="rtv-ed-tags">
            <label v-for="tg in edTagOptions" :key="tg" class="rtv-ed-taglbl">
              <input
                v-model="edAgentTags"
                type="checkbox"
                :value="tg"
                :disabled="edTagDisabled(tg)"
                :style="{ accentColor: RT_TAG_COLORS[tg] }"
                @change="onEdTagChange(tg)"
              />
              <span class="rtv-tagpill" :style="{ background: RT_TAG_COLORS[tg] }">{{
                rtTagLabel(tg)
              }}</span>
              <span class="rtv-tagdesc">{{
                tg === 'all' ? t('tag.all_desc') : t('tag.single_desc', { label: rtTagLabel(tg) })
              }}</span>
            </label>
          </div>
        </div>
        <!-- 最大并发 -->
        <div class="rtv-ed-sec">
          <div class="rtv-ed-sechead">
            <span>{{ t('label.max_concurrent') }}</span>
            <button class="bt bt-p" @click="saveMaxConcurrent(engineDetail)">
              {{ t('action.save') }}
            </button>
          </div>
          <div class="rtv-mcrow">
            <input
              v-model.number="edMaxConcurrent"
              type="number"
              min="1"
              max="100"
              class="rtv-mc-input"
            />
            <span class="rtv-muted">{{ t('label.max_concurrent_desc') }}</span>
          </div>
        </div>
        <!-- 本地技能 -->
        <div class="rtv-ed-sec rtv-ed-last">
          <div class="rtv-ed-sechead">
            <span
              >{{ t('label.local_skill_list') }}
              <span class="rtv-skcount">{{
                t('label.skill_count_paren', { count: edSkillFiltered.length })
              }}</span></span
            >
            <button
              class="bt bt-o"
              :disabled="skillScanning"
              @click="refreshEngineSkills(engineDetail)"
            >
              {{ skillScanning ? '⟳ ' + t('toast.scanning') : t('action.refresh_skills') }}
            </button>
          </div>
          <input
            v-model="edSkillQuery"
            class="rtv-sksearch"
            :placeholder="t('skill.search_placeholder')"
          />
          <div class="rtv-sklist">
            <div v-if="!(engineDetail.local_skills || []).length" class="rtv-muted">
              {{ t('empty.no_local_skill') }}
            </div>
            <div v-else-if="!edSkillFiltered.length" class="rtv-muted">
              {{ t('skill.no_match') }}
            </div>
            <div
              v-for="s in edSkillFiltered"
              :key="s"
              class="rtv-skitem"
              @click="openSkillDetail(s)"
            >
              <div class="rtv-sk-name">{{ s }}</div>
              <div class="rtv-sk-desc">{{ edSkillDesc(s) }}</div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══ 技能详情弹窗 ═══ -->
    <div v-if="skillDetail" class="rtv-modal-ov" @click.self="skillDetail = null">
      <div class="rtv-skilldetail">
        <div class="rtv-sd-head">
          <span class="rtv-sd-name">{{ skillDetail.name }}</span>
          <button class="bt bt-o" style="margin-left: auto" @click="skillDetail = null">
            {{ t('action.close') }}
          </button>
        </div>
        <div v-if="skillDetail.data" class="rtv-sd-grid">
          <div class="rtv-kv">
            <span class="rtv-k" style="min-width: 80px">{{ t('label.description') }}</span>
            {{ skillDetail.data.description || '—' }}
          </div>
          <div class="rtv-kv">
            <span class="rtv-k" style="min-width: 80px">{{ t('label.provider') }}</span>
            {{ skillDetail.data.provider || '—' }}
          </div>
          <div class="rtv-kv">
            <span class="rtv-k" style="min-width: 80px">{{ t('label.local_path') }}</span>
            <code class="rtv-sd-path">{{ skillDetail.data.path || '—' }}</code>
          </div>
          <div class="rtv-kv">
            <span class="rtv-k" style="min-width: 80px">{{ t('label.file_count') }}</span>
            {{ skillDetail.data.file_count || 0 }}
          </div>
        </div>
        <div v-else class="rtv-muted">{{ t('skill.detail_unavailable') }}</div>
      </div>
    </div>

    <!-- ═══ scope 切换弹窗 ═══ -->
    <div v-if="scopeSwitcher.open" class="rtv-modal-ov" @click.self="scopeSwitcher.open = false">
      <div class="rtv-scopemodal">
        <div class="rtv-sm-title">{{ t('label.scope_short') }}</div>
        <label
          v-for="sc in ['repo', 'user']"
          :key="sc"
          class="rtv-sm-item"
          :class="{ on: scopeSwitcher.scope === sc }"
        >
          <input
            v-model="scopeSwitcher.scope"
            type="radio"
            :value="sc"
            name="scopeRadio"
            :style="{ accentColor: 'var(--a-text)', width: '16px', height: '16px' }"
          />
          <span>{{ scopeShort(sc) }}</span>
        </label>
        <div v-if="scopeSwitcher.scope === 'repo'" class="rtv-sm-repo">
          <input
            v-model="scopeSwitcher.repoKey"
            class="rtv-sm-input"
            :placeholder="t('label.repo_key_prompt')"
          />
        </div>
        <div class="rtv-sm-btns">
          <button class="bt bt-o" @click="scopeSwitcher.open = false">
            {{ t('action.close') }}
          </button>
          <button class="bt bt-p" @click="confirmScopeSwitch()">{{ tc('confirm') }}</button>
        </div>
      </div>
    </div>

    <!-- ═══ 添加设备侧边栏 ═══ -->
    <div v-if="addPanel.open" class="rtv-side-ov" @click.self="closeAddDevicePanel()">
      <div class="rtv-sidebar">
        <div class="rtv-sb-head">
          <div class="rtv-sb-ic">
            <svg
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="#fff"
              stroke-width="2.5"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <path d="M12 5v14M5 12h14" />
            </svg>
          </div>
          <div class="rtv-sb-title">
            <h3>{{ t('action.add_device') }}</h3>
            <p>{{ t('devspace.sidebar_subtitle') }}</p>
          </div>
          <button class="rtv-sb-close" @click="closeAddDevicePanel()">✕</button>
        </div>
        <div class="rtv-sb-body">
          <!-- 选项一：生成凭据 -->
          <div class="rtv-sb-sec">
            <div class="rtv-sb-opt" @click="toggleCredSection">
              <div class="rtv-sb-opt-ic">
                <svg
                  width="22"
                  height="22"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="var(--on-accent)"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                >
                  <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
                  <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                </svg>
              </div>
              <div class="rtv-sb-opt-body">
                <div class="rtv-sb-opt-t">{{ t('action.gen_credential') }}</div>
                <div class="rtv-sb-opt-d">{{ t('devspace.cred_hint') }}</div>
              </div>
              <svg
                class="rtv-sb-chev"
                :class="{ open: credExpanded }"
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                stroke="var(--t3)"
                stroke-width="2.5"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <path d="M6 9l6 6 6-6" />
              </svg>
            </div>
            <div v-if="credExpanded" class="rtv-sb-area">
              <!-- 凭据表单 / 生成结果 -->
              <template v-if="!credResult">
                <div class="rtv-credbox">
                  <div class="rtv-cred-t">{{ t('credential.gen_title') }}</div>
                  <div class="rtv-cred-fld">
                    <label>{{ t('label.cred_name') }}</label>
                    <input
                      v-model="credForm.name"
                      class="rtv-cred-input"
                      :placeholder="t('label.cred_name_placeholder')"
                    />
                  </div>
                  <div class="rtv-cred-fld">
                    <label>{{ t('label.owner_user') }}</label>
                    <input :value="currentUser" disabled class="rtv-cred-input rtv-cred-disabled" />
                  </div>
                  <div class="rtv-cred-fld">
                    <label>{{ t('label.scope') }}</label>
                    <select v-model="credForm.scope" class="rtv-cred-input">
                      <option value="user">{{ t('scope.user') }}</option>
                      <option value="repo">{{ t('scope.repo') }}</option>
                      <option value="global">{{ t('scope.global') }}</option>
                    </select>
                  </div>
                  <div v-if="credForm.scope === 'repo'" class="rtv-cred-fld">
                    <label>{{ t('label.repo_key') }}</label>
                    <input
                      v-model="credForm.repoKey"
                      class="rtv-cred-input"
                      placeholder="owner/repo"
                    />
                  </div>
                  <div class="rtv-cred-btns">
                    <button class="bt bt-p rtv-cred-gen" @click="doCreateCredential()">
                      {{ t('action.generate') }}
                    </button>
                    <button class="bt bt-o" @click="credExpanded = false">
                      {{ t('action.cancel') }}
                    </button>
                  </div>
                </div>
              </template>
              <template v-else-if="credResult.loading">
                <div class="rtv-sb-loading">
                  <div class="rtv-spinner"></div>
                  <div>{{ t('toast.refreshing') }}</div>
                </div>
              </template>
              <template v-else>
                <div class="rtv-credbox">
                  <div class="rtv-cred-warn">
                    <svg
                      width="16"
                      height="16"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="#d97706"
                      stroke-width="2"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                      style="flex-shrink: 0; margin-top: 1px"
                    >
                      <path
                        d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"
                      />
                      <line x1="12" y1="9" x2="12" y2="13" />
                      <line x1="12" y1="17" x2="12.01" y2="17" />
                    </svg>
                    <span>{{
                      t('credential.token_warning', {
                        name: credResult.name,
                        scope: scopeLabel(credResult.scope),
                      })
                    }}</span>
                  </div>
                  <label class="rtv-cred-lbl">Token</label>
                  <code class="rtv-cred-code">{{ credResult.token }}</code>
                  <div class="rtv-cred-sub">🐧 {{ t('credential.install_hint') }}</div>
                  <code class="rtv-cred-code sm">{{ credResult.oneCmd }}</code>
                  <button class="bt bt-p rtv-cred-fullbtn" @click="copyText(credResult.oneCmd)">
                    {{ t('action.copy_one_cmd') }}
                  </button>
                  <div class="rtv-cred-desc">{{ t('credential.install_desc') }}</div>
                  <div class="rtv-cred-sub">{{ t('credential.win_install_hint') }}</div>
                  <code class="rtv-cred-code sm">{{ credResult.winCmd }}</code>
                  <button class="bt bt-p rtv-cred-fullbtn" @click="copyText(credResult.winCmd)">
                    {{ t('action.copy_win_cmd') }}
                  </button>
                  <div class="rtv-cred-desc">{{ t('credential.win_install_desc') }}</div>
                  <div class="rtv-cred-sub">{{ t('credential.manual_start') }}</div>
                  <code class="rtv-cred-code sm">{{ credResult.runCmd }}</code>
                  <button class="bt bt-o rtv-cred-fullbtn" @click="copyText(credResult.token)">
                    {{ t('action.copy_token') }}
                  </button>
                </div>
              </template>
            </div>
          </div>
          <!-- 选项二：AI Devspace -->
          <div class="rtv-sb-sec">
            <div class="rtv-sb-opt" @click="toggleDevSection">
              <div class="rtv-sb-opt-ic">
                <svg
                  width="22"
                  height="22"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="#fff"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                >
                  <path
                    d="M17.5 19a4.5 4.5 0 1 0-1.41-8.78 7 7 0 1 0-13.09 3.78A4.5 4.5 0 0 0 6.5 19z"
                  />
                </svg>
              </div>
              <div class="rtv-sb-opt-body">
                <div class="rtv-sb-opt-t">☁ AI Devspace</div>
                <div class="rtv-sb-opt-d">{{ t('devspace.devspace_hint') }}</div>
              </div>
              <svg
                class="rtv-sb-chev"
                :class="{ open: devExpanded }"
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                stroke="var(--t3)"
                stroke-width="2.5"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <path d="M6 9l6 6 6-6" />
              </svg>
            </div>
            <div v-if="devExpanded" class="rtv-sb-area">
              <div class="rtv-devbox">
                <div class="rtv-dev-head">
                  <svg
                    width="18"
                    height="18"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="#fff"
                    stroke-width="2"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                  >
                    <path
                      d="M17.5 19a4.5 4.5 0 1 0-1.41-8.78 7 7 0 1 0-13.09 3.78A4.5 4.5 0 0 0 6.5 19z"
                    />
                  </svg>
                  <span class="rtv-dev-title">{{ t('devspace.title') }}</span>
                  <span v-if="devspaceAuto" class="rtv-dev-auto">15s auto</span>
                  <button class="rtv-dev-create" @click="showCreateEnvForm()">
                    {{ t('devspace.create') }}
                  </button>
                  <button class="rtv-dev-refresh" @click="refreshDevspaceList()">↻</button>
                </div>
                <div class="rtv-dev-body">
                  <div v-if="devspaceLoading" class="rtv-sb-loading">
                    <div class="rtv-spinner purple"></div>
                    <div>{{ t('devspace.loading') }}</div>
                  </div>
                  <div v-else-if="devspaceError" class="rtv-dev-err">
                    <div class="rtv-dev-err-t">{{ devspaceError.title }}</div>
                    <div class="rtv-dev-err-d">{{ devspaceError.detail }}</div>
                  </div>
                  <div v-else-if="!devspaces.length" class="rtv-dev-empty">
                    <div class="rtv-dev-empty-t">{{ t('devspace.empty') }}</div>
                    <div class="rtv-dev-empty-d">{{ t('devspace.empty_hint') }}</div>
                  </div>
                  <template v-else>
                    <div v-for="e in devspaces" :key="e.id" class="rtv-dev-row">
                      <span
                        class="rtv-dev-dot"
                        :style="{ background: devStatusColor(e.status) }"
                      ></span>
                      <div class="rtv-dev-main">
                        <div class="rtv-dev-rowhead">
                          <span class="rtv-dev-name">{{ e.name || e.id }}</span>
                          <span
                            class="rtv-dev-status"
                            :style="{
                              color: devStatusColor(e.status),
                              background: devStatusBg(e.status),
                            }"
                            >{{ devStatusLabel(e.status) }}</span
                          >
                        </div>
                        <div class="rtv-dev-spec">{{ devSpec(e) }}</div>
                        <div
                          v-if="e.status === 'cde.0002' && !e.worker_online"
                          class="rtv-dev-progress"
                        >
                          <div class="rtv-dev-progbar"><div class="rtv-dev-progfill"></div></div>
                        </div>
                      </div>
                      <div class="rtv-dev-acts">
                        <button
                          v-if="devCanStart(e)"
                          class="bt bt-p bt-s"
                          @click="devAction('start', e.id)"
                        >
                          {{ t('devspace.start') }}
                        </button>
                        <button
                          v-if="e.status === 'cde.0002'"
                          class="bt bt-o bt-s"
                          @click="devAction('stop', e.id)"
                        >
                          {{ t('devspace.stop') }}
                        </button>
                        <button
                          v-if="!devBusy(e)"
                          class="bt bt-r bt-s"
                          @click="devAction('delete', e.id)"
                        >
                          {{ t('devspace.delete') }}
                        </button>
                      </div>
                    </div>
                  </template>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from 'vue'

const G = globalThis

// ═══ 常量（与老代码一致） ═══
const RUNTIME_COLOR = {
  online: 'var(--g)',
  recently_lost: 'var(--y)',
  offline: 'var(--t3)',
  about_to_gc: 'var(--r)',
}
const HEALTH_NAMES = {
  online: 'online',
  recently_lost: 'recently_lost',
  offline: 'offline',
  about_to_gc: 'about_to_gc',
}
const HEALTH_BADGE = {
  online: { color: 'var(--g)', bg: '#dcfce7' },
  recently_lost: { color: 'var(--y)', bg: '#fef3c7' },
  offline: { color: 'var(--t3)', bg: '#eef0f3' },
  about_to_gc: { color: 'var(--r)', bg: '#fee2e2' },
}
const SCOPE_BADGE_CFG = {
  global: { label: '全局', color: 'var(--a-text)', bg: '#e3edfd' },
  repo: { label: '仓库', color: '#8b5cf6', bg: '#ede9fe' },
  user: { label: '个人', color: '#1ca44c', bg: '#dcfce7' },
  local: { label: '本机', color: '#0e7490', bg: '#cffafe' },
}
const SCOPE_LABELS = { global: '全局', user: '个人', repo: '仓库', local: '本机' }
const ENGINE_DOT = {
  opencode: '#3b82f6',
  hermes: '#8b5cf6',
  codearts: '#f59e0b',
  codex: '#10a37f',
  aishell: '#0a8cd8',
}
const SCENE_TAG_COLORS = {
  default: '#10b981',
  'cloud-native': '#3b82f6',
  iac: '#f59e0b',
  'agent-dev': '#8b5cf6',
  'skill-dev': '#ec4899',
  'api-skill-dev': '#06b6d4',
  'blog-publish': '#ef4444',
}
const RT_TAG_COLORS = {
  all: '#6366f1',
  vod: '#ec4899',
  issue: '#ef4444',
  coding: '#3b82f6',
  qa: '#a855f7',
  sre: '#f59e0b',
}
const RT_TAG_OPTIONS = ['all', 'vod', 'issue', 'coding', 'qa', 'sre']
const HEALTH_ORDER = { online: 0, recently_lost: 1, offline: 2, about_to_gc: 3 }
const DEV_STATUS_COLORS = {
  'cde.0000': '#6b7280',
  'cde.0001': 'var(--a-text)',
  'cde.0002': '#16a34a',
  'cde.0003': 'var(--a-text)',
  'cde.0004': '#16a34a',
  'cde.0005': '#d97706',
  'cde.0006': '#dc2626',
  'cde.0007': '#d97706',
  'cde.0008': '#d97706',
  'cde.0009': '#9ca3af',
  'cde.1001': '#dc2626',
  'cde.1002': '#dc2626',
  'cde.1003': '#dc2626',
  'cde.1004': '#dc2626',
}
const BUSY_STATES = ['cde.0001', 'cde.0003', 'cde.0005', 'cde.0006', 'cde.0007', 'cde.0008']
const PAGE_SIZE = 20

// ═══ 状态 ═══
const tab = ref('engines')
const loaded = ref(false)
const loadError = ref('')
const runtimes = ref([])
const devices = ref([])
const rtTotal = ref(0)
const devTotal = ref(0)
const page = ref(1)
const devPage = ref(1)
const search = ref('')
const searchDraft = ref('')
const filterType = ref('')
const filterScope = ref('')
const filterScene = ref([])
const filterTag = ref([])
// Issue #473：范围筛选（后端 range 参数 all/repo/user）——旧 runtimes.js _RT_RANGE_OPTIONS 移植
const rtRange = ref('all')
const RT_RANGE_OPTIONS = [
  { key: 'all', labelKey: 'rt_range_all' },
  { key: 'repo', labelKey: 'rt_range_repo' },
  { key: 'user', labelKey: 'rt_range_user' },
]
// Issue #1167：是否存在用户激活的筛选（与 clearFilters 重置范围保持一致）
const hasActiveFilters = computed(function () {
  return !!(
    filterScope.value ||
    filterType.value ||
    (Array.isArray(filterScene.value) ? filterScene.value.length : 0) ||
    (Array.isArray(filterTag.value) ? filterTag.value.length : 0) ||
    search.value ||
    (rtRange.value && rtRange.value !== 'all')
  )
})
// Issue #475/#498：健康帮助提示 key —— 后端口径（health_abnormal/health_reason）优先，
// 旧缓存无字段时回退派生信号推导（与旧 runtimes.js _healthHelpHint 完全同序）
function healthHintKey(rt) {
  if (!rt) return ''
  const h = rt.health || rt.status || 'offline'
  if (h === 'about_to_gc') return 'health.hint.about_to_gc'
  if (h === 'offline') return 'health.hint.offline'
  if (h === 'recently_lost') return 'health.hint.recently_lost'
  if (rt.health_reason) return 'health.hint.' + rt.health_reason
  if (rt.health_abnormal === false) return ''
  if ((rt.health_consecutive_failures || 0) > 0) return 'health.hint.consecutive_failures'
  if (rt.health_heartbeat_alive === false) return 'health.hint.heartbeat_lost'
  if (rt.health_score !== null && rt.health_score !== undefined && rt.health_score < 50)
    return 'health.hint.low_score'
  return ''
}
function isAbnormalEngine(rt) {
  if (!rt) return false
  const h = rt.health || rt.status || 'offline'
  if (h !== 'online') return true
  if (rt.health_abnormal !== undefined && rt.health_abnormal !== null)
    return rt.health_abnormal === true
  return (
    (rt.health_consecutive_failures || 0) > 0 ||
    rt.health_heartbeat_alive === false ||
    (rt.health_score !== null && rt.health_score !== undefined && rt.health_score < 50)
  )
}
function abnormalBorderColor(rt) {
  const h = rt.health || rt.status || 'offline'
  if (h === 'about_to_gc' || h === 'offline') return 'var(--r)'
  if (rt.health_abnormal !== undefined && rt.health_abnormal !== null) {
    return rt.health_abnormal === true ? 'var(--r)' : ''
  }
  if ((rt.health_consecutive_failures || 0) > 0 || rt.health_heartbeat_alive === false)
    return 'var(--r)'
  return ''
}
// Issue #498：worker 版本落后是独立提示（琥珀标签），不参与异常描边
function workerVersionStale(rt) {
  return !!(rt && rt.worker_version_stale)
}
const sceneDropdown = ref(false)
const tagDropdown = ref(false)
const devAvailScopes = ref([])
const devAvailTypes = ref([])
// 场景标签缓存（dashboard.js typeof 引用的契约——entry 桥到 window）
const sceneTagsCache = ref(null)
// 引擎多源索引（Issue #364）
const engineIndex = reactive({})
// 引擎详情
const engineDetail = ref(null)
const edSceneTags = ref([])
const edAgentTags = ref([])
const edMaxConcurrent = ref(20)
const edSkillQuery = ref('')
const skillScanning = ref(false)
const skillDetail = ref(null)
let skillScanDirty = false
// scope 切换
const scopeSwitcher = reactive({ open: false, cid: 0, scope: 'repo', repoKey: '' })
// 添加设备侧边栏
const addPanel = reactive({ open: false })
const credExpanded = ref(false)
const devExpanded = ref(false)
const credForm = reactive({ name: '', scope: 'user', repoKey: '' })
const credResult = ref(null)
// AI Devspace
const devspaces = ref([])
const devspaceLoading = ref(false)
const devspaceError = ref(null)
const devspaceAuto = ref(false)
let devspaceTimer = null
let grapeUsername = ''

// ═══ 工具 ═══
const API = () => G.API
const app = () => G.app
function t(key, vars) {
  return G.I18N.t('runtimes.' + key, vars)
}
function tc(key) {
  const v = G.I18N.t('common.' + key)
  return v && v !== 'common.' + key
    ? v
    : key === 'loading'
      ? '加载中...'
      : key === 'confirm'
        ? '确认'
        : key
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}
const currentUser = computed(() => {
  try {
    const u = JSON.parse(G.localStorage.getItem('user') || 'null')
    return (u && (u.nickname || u.username)) || ''
  } catch (e) {
    return ''
  }
})
function currentRepo() {
  return app().currentRepo ? app().currentRepo() : null
}
function repoQs() {
  const repo = currentRepo()
  return repo && repo.fullName ? '?repo=' + encodeURIComponent(repo.fullName) : ''
}
function healthLabel(h) {
  return t('health.' + (HEALTH_NAMES[h] || 'offline'))
}
function scopeLabel(s) {
  return t('scope.' + (s || 'global'))
}
function scopeShort(s) {
  return s === 'global'
    ? t('scope.global')
    : s === 'repo'
      ? t('scope.repo')
      : s === 'user'
        ? t('scope.user')
        : s
}
function scopeKey(rt) {
  return rt.is_local || rt.credential_id === 0 ? 'local' : rt.scope || 'global'
}
function scopeBadgeLabel(rt) {
  return SCOPE_BADGE_CFG[scopeKey(rt)].label
}
function rtTagLabel(tg) {
  return tg === 'all'
    ? t('tag.all')
    : { vod: 'VOD', issue: 'Issue', coding: 'Coding', qa: 'QA', sre: 'SRE' }[tg] || tg
}
function sceneDisplayName(name) {
  if (!sceneTagsCache.value) return name
  const s = sceneTagsCache.value.find((x) => x.name === name)
  return s ? s.display_name : name
}
function relTime(iso) {
  if (!iso) return t('time.not_online')
  try {
    const diff = (Date.now() - new Date(iso).getTime()) / 1000
    if (diff < 60) return t('time.seconds_ago', { n: Math.round(diff) })
    if (diff < 3600) return t('time.minutes_ago', { n: Math.round(diff / 60) })
    return t('time.hours_ago', { n: Math.round(diff / 3600) })
  } catch (e) {
    return iso
  }
}
// Issue #701：worker/设备显示名唯一来源 runtime_credentials.name；空名统一占位「未命名设备」，
// 不回退边端自报 device_name（#465 的回退口径已废弃，避免同一机器在不同页面叫不同名字）。
function workerDisplayName(wk) {
  const n = String((wk && wk.name) || '').trim()
  return n || tc('unnamed_worker')
}
// Issue #1310：引擎显示名统一「引擎名（凭据名）」——引擎名取 display_name/engine_name/name，
// instance_name 仅作最后兼容，避免展示设备派生名。
function engineDisplayName(rt) {
  if (!rt) return ''
  return rt.display_name || rt.engine_name || rt.name || rt.instance_name || ''
}
// Issue #865：模型展示——配置模型（worker 上报/本机配置）优先 → 最近使用模型 → —
function engineModelDisplay(rt) {
  const configured = (rt && rt.model_name) || ''
  if (configured) return configured
  const recent = ((rt && rt.recent_models) || [])[0]
  if (recent && recent.model) return recent.model + ' (' + t('label.model_recent_short') + ')'
  return '—'
}
function engineModelTooltip(rt) {
  const configured = (rt && rt.model_name) || ''
  if (configured) {
    const src =
      rt.model_source === 'local_config'
        ? t('label.model_source_local')
        : t('label.model_source_report')
    return configured + ' · ' + src
  }
  return t('label.model_fallback_hint')
}
function recentModelList(rt) {
  return (rt && rt.recent_models) || []
}
function copyText(txt) {
  try {
    G.navigator.clipboard.writeText(txt).then(() => app().showToast(t('credential.toast_copied')))
  } catch (e) {
    const ta = document.createElement('textarea')
    ta.value = txt
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
    app().showToast(t('credential.toast_copied'))
  }
}
function watermark(name) {
  const n = (name || '').toLowerCase()
  if (n.includes('aishell') || n.includes('hwcloud')) return '/static/img/engines/AIShell.svg'
  if (n.includes('opencode')) return '/static/img/engines/opencode.png'
  if (n.includes('hermes')) return '/static/img/engines/Hermes.png'
  if (n.includes('codearts')) return '/static/img/engines/codearts.jpg'
  if (n.includes('codex')) return '/static/img/engines/codex.png'
  return ''
}
function healthScoreHtml(rt) {
  const s = rt.health_score
  if (s === null || s === undefined) return '—'
  const c = s === 0 ? 'var(--r)' : s < 50 ? 'var(--y)' : 'var(--g)'
  const hb = rt.health_heartbeat_alive === false ? ' · 失联' : ''
  const cf =
    rt.health_consecutive_failures > 0
      ? ` <span class="ts t3" style="font-size:10px">连续失败${rt.health_consecutive_failures}</span>`
      : ''
  return (
    `<span style="color:${c};font-weight:600">${s}</span>${cf}` +
    (hb ? `<span class="ts t3" style="font-size:10px">${hb}</span>` : '')
  )
}
function engineTagSummary(tags) {
  if (!tags || !tags.length)
    return `<span style="color:var(--t3);font-size:10px">${t('empty.unassigned')}</span>`
  return tags
    .map(
      (x) =>
        `<span style="font-size:10px;padding:1px 6px;border-radius:999px;color: var(--on-accent);background:${RT_TAG_COLORS[x] || '#999'};margin:0 2px">${esc(rtTagLabel(x)) || esc(x)}</span>`
    )
    .join('')
}
function sceneTagSummary(tags) {
  if (!tags || !tags.length)
    return `<span style="color:var(--t3);font-size:10px">${t('empty.unassigned')}</span>`
  return tags
    .map((x) => {
      const dn = sceneDisplayName(x)
      const color = SCENE_TAG_COLORS[x] || '#10b981'
      return `<span style="font-size:10px;padding:1px 6px;border-radius:999px;color: var(--on-accent);background:${color};margin:0 2px">${esc(dn)}</span>`
    })
    .join('')
}
function cardLeftBorder(h, rt) {
  // Issue #475/#498：在线但健康口径异常 → 红描边强化；其余沿用健康色
  if (rt && isAbnormalEngine(rt)) {
    const red = abnormalBorderColor(rt)
    if (red) return { borderLeft: '3px solid ' + red }
  }
  return h === 'online'
    ? { borderLeft: '1px solid var(--b)' }
    : { borderLeft: '3px solid ' + (RUNTIME_COLOR[h] || 'var(--t3)') }
}
function isOwner(d) {
  return !!(d.owner && grapeUsername && d.owner === grapeUsername)
}
function canManage(d) {
  return !!(d.credential_id && isOwner(d))
}

// ═══ 数据加载 ═══
const isEmpty = computed(
  () => loaded.value && !loadError.value && !runtimes.value.length && !devices.value.length
)
function loadRuntimesData() {
  let qs = '?page=' + page.value + '&page_size=' + PAGE_SIZE
  if (filterScope.value) qs += '&scope=' + encodeURIComponent(filterScope.value)
  if (rtRange.value && rtRange.value !== 'all') qs += '&range=' + encodeURIComponent(rtRange.value)
  const repo = currentRepo()
  if (repo && repo.fullName) qs += '&repo=' + encodeURIComponent(repo.fullName)
  API()
    .get('/settings/agent-runtimes' + qs)
    .then(function (d) {
      if (G.window._currentView !== 'runtimes') return
      rtTotal.value = d.total || 0
      runtimes.value = (d && d.runtimes) || []
      devices.value = (d && d.devices) || devices.value
      loaded.value = true
      loadError.value = ''
      rebuildEngineIndex()
      ensureSceneTags()
    })
    .catch(function (e) {
      if (e.name === 'AbortError' && e.silent) return
      if (G.window._currentView !== 'runtimes') return
      loadError.value = (t('toast.load_failed') || '加载失败') + ': ' + (e.message || '')
      loaded.value = true
    })
}
function loadRuntimeDevices() {
  let qs = '?page=' + devPage.value + '&page_size=' + PAGE_SIZE
  if (filterScope.value) qs += '&scope=' + encodeURIComponent(filterScope.value)
  if (rtRange.value && rtRange.value !== 'all') qs += '&range=' + encodeURIComponent(rtRange.value)
  if (filterType.value) qs += '&type=' + encodeURIComponent(filterType.value)
  if (search.value) qs += '&q=' + encodeURIComponent(search.value)
  const repo = currentRepo()
  if (repo && repo.fullName) qs += '&repo=' + encodeURIComponent(repo.fullName)
  API()
    .get('/settings/runtime-devices' + qs)
    .then(function (d) {
      if (G.window._currentView !== 'runtimes') return
      devTotal.value = d.total || 0
      devAvailScopes.value = d.avail_scopes || []
      devAvailTypes.value = d.avail_types || []
      devices.value = d.devices || []
      loaded.value = true
      loadError.value = ''
      rebuildEngineIndex()
    })
    .catch(function (e) {
      if (e.name === 'AbortError' && e.silent) return
      if (G.window._currentView !== 'runtimes') return
      loadError.value = (t('toast.load_failed') || '加载失败') + ': ' + (e.message || '')
      loaded.value = true
    })
}
// Issue #364: 引擎多源索引
function rebuildEngineIndex() {
  runtimes.value.forEach(function (rt) {
    if (rt && rt.id !== undefined) engineIndex[rt.id] = rt
  })
  devices.value.forEach(function (d) {
    ;(d.runtimes || []).forEach(function (rt) {
      if (!rt || rt.id === undefined || engineIndex[rt.id]) return
      engineIndex[rt.id] = Object.assign({}, rt, {
        credential_id: d.credential_id,
        worker_name: workerDisplayName(d),
        worker_ip: d.worker_ip || '',
        scope: d.scope || 'global',
        owner: d.owner || '',
        is_local: d.credential_id === 0,
        _partial: true,
      })
    })
  })
}
function ensureSceneTags(cb) {
  if (sceneTagsCache.value) {
    if (cb) cb(sceneTagsCache.value)
    return
  }
  API()
    .get('/runtime/scene-tags')
    .then(function (d) {
      sceneTagsCache.value = d.scenes || []
      if (cb) cb(sceneTagsCache.value)
    })
    .catch(function () {
      sceneTagsCache.value = []
      if (cb) cb(sceneTagsCache.value)
    })
}

// ═══ 筛选/分页 ═══
function switchTab(t) {
  if (tab.value === t) return
  tab.value = t
  sceneDropdown.value = false
  tagDropdown.value = false
  if (t === 'workers') loadRuntimeDevices()
  else loadRuntimesData()
}
function applySearch() {
  search.value = searchDraft.value
  resetPageAndReload()
}
function toggleType(ty) {
  filterType.value = filterType.value === ty ? '' : ty
  resetPageAndReload()
}
function toggleScope(s) {
  filterScope.value = filterScope.value === s ? '' : s
  resetPageAndReload()
}
function toggleMulti(kind, val) {
  const arr = kind === 'scene' ? filterScene : filterTag
  const i = arr.value.indexOf(val)
  if (i >= 0) arr.value.splice(i, 1)
  else arr.value.push(val)
  resetPageAndReload()
}
function setRange(k) {
  if (rtRange.value === k) return
  rtRange.value = k
  resetPageAndReload()
}
function clearFilters() {
  rtRange.value = 'all'
  filterType.value = ''
  filterScope.value = ''
  filterScene.value = []
  filterTag.value = ''
  filterTag.value = []
  search.value = ''
  searchDraft.value = ''
  resetPageAndReload()
}
function resetPageAndReload() {
  if (tab.value === 'engines') {
    page.value = 1
    loadRuntimesData()
  } else {
    devPage.value = 1
    loadRuntimeDevices()
  }
}
function goPage(which, p) {
  if (which === 'engines') {
    page.value = Math.max(1, p)
    loadRuntimesData()
  } else {
    devPage.value = Math.max(1, p)
    loadRuntimeDevices()
  }
}
function totalPages(which) {
  const total = which === 'engines' ? rtTotal.value : devTotal.value
  return Math.ceil(total / PAGE_SIZE)
}
function pageNums(which) {
  const cur = which === 'engines' ? page.value : devPage.value
  const tp = totalPages(which)
  if (tp <= 1) return []
  let s = Math.max(1, cur - 3)
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
}
// 筛选选项
const availTypes = computed(() => {
  if (tab.value === 'engines') {
    const types = []
    runtimes.value.forEach((e) => {
      const n = (e.display_name || e.name || e.instance_name || '').toLowerCase()
      const tn =
        n.includes('aishell') || n.includes('hwcloud')
          ? 'aishell'
          : n.includes('hermes')
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
  }
  return devAvailTypes.value || []
})
const engAvailScopes = computed(() =>
  [
    ...new Set(
      runtimes.value.map((r) =>
        r.is_local || r.credential_id === 0 ? 'local' : r.scope || 'global'
      )
    ),
  ].filter((s) => ['local', 'user', 'repo', 'global'].includes(s))
)
const availScopes = computed(() =>
  tab.value === 'engines' ? engAvailScopes.value : devAvailScopes.value || []
)
const availScenes = computed(() =>
  [...new Set(runtimes.value.flatMap((rt) => rt.scene_tags || []))].filter(Boolean)
)
const availTags = computed(() =>
  [...new Set(runtimes.value.flatMap((rt) => rt.agent_tags || []))].filter(Boolean)
)
// Issue #432: 共享过滤（统计与列表同口径）
function filterEngines(list) {
  const q = (search.value || '').toLowerCase().trim()
  return list.filter((rt) => {
    if (q) {
      const hay = [
        rt.alias,
        rt.instance_name,
        rt.name,
        rt.worker_name,
        rt.owner,
        rt.host_name,
        (rt.agent_tags || []).join(' '),
        (rt.local_skills || []).join(' '),
      ]
        .join(' ')
        .toLowerCase()
      if (!hay.includes(q)) return false
    }
    if (filterType.value) {
      const ename = (rt.display_name || rt.name || rt.instance_name || '').toLowerCase()
      if (!ename.includes(filterType.value)) return false
    }
    if (filterScene.value.length > 0) {
      const tags = rt.scene_tags || []
      if (!filterScene.value.some((s) => tags.includes(s))) return false
    }
    if (filterTag.value.length > 0) {
      const tags = rt.agent_tags || []
      if (!filterTag.value.some((x) => tags.includes(x) || tags.includes('all'))) return false
    }
    return true
  })
}
const filteredEngines = computed(() => {
  const list = filterEngines(runtimes.value)
  list.sort((a, b) => (b.is_local ? 1 : 0) - (a.is_local ? 1 : 0))
  sortByHealth(list)
  sortByName(list)
  return list
})
const sortedDevices = computed(() => {
  const list = devices.value.slice()
  sortByHealth(list)
  sortByName(list)
  return list
})
const summaryList = computed(() =>
  tab.value === 'engines' ? filteredEngines.value : sortedDevices.value
)
const summaryCounts = computed(() => {
  const counts = { online: 0, recently_lost: 0, offline: 0, about_to_gc: 0 }
  summaryList.value.forEach((e) => {
    const k = e.health || e.status || 'offline'
    counts[k] = (counts[k] || 0) + 1
  })
  return Object.keys(counts).map((k) => ({ k, n: counts[k] }))
})
function sortByHealth(list) {
  return list.sort((a, b) => {
    const ha = HEALTH_ORDER[a.health || a.status || 'offline'] ?? 9
    const hb = HEALTH_ORDER[b.health || b.status || 'offline'] ?? 9
    return ha - hb
  })
}
// Issue #1310/#701：引擎按「引擎名（凭据名）」排序、设备按显示名（workerDisplayName）排序
function sortListName(x) {
  if (x && Array.isArray(x.runtimes)) return workerDisplayName(x) || ''
  return engineDisplayName(x) || x.instance_name || x.name || ''
}
function sortByName(list) {
  return list.sort((a, b) => {
    const na = sortListName(a).toLowerCase()
    const nb = sortListName(b).toLowerCase()
    return na.localeCompare(nb)
  })
}

// ═══ 刷新 ═══
function refreshRuntimes() {
  stopDevspaceAuto()
  loaded.value = false
  runtimes.value = []
  devices.value = []
  API()
    .post('/settings/agent-runtimes/refresh')
    .then(() => {
      if (tab.value === 'workers') {
        loadRuntimeDevices()
        API()
          .get('/settings/agent-runtimes?page=1&page_size=' + PAGE_SIZE)
          .then(function (d) {
            if (G.window._currentView !== 'runtimes') return
            runtimes.value = d.runtimes || []
            rtTotal.value = d.total || 0
            rebuildEngineIndex()
          })
          .catch(function () {})
      } else {
        loadRuntimesData()
      }
      app().showToast(t('toast.refresh_success'))
    })
    .catch((e) => app().showToast(t('toast.refresh_failed', { msg: e.message })))
}

// ═══ 引擎详情 ═══
function openEngineDetail(runtimeId) {
  let rt = runtimes.value.find((r) => r.id === runtimeId)
  if (!rt) rt = engineIndex[runtimeId]
  if (rt) {
    renderEngineDetail(rt)
    if (rt._partial) upgradeEngineDetail(runtimeId)
    return
  }
  // 异步拉取（loading 弹窗）
  engineDetail.value = { loading: true }
  API()
    .get('/settings/agent-runtimes/' + runtimeId + repoQs())
    .then(function (d) {
      if (!d || d.id === undefined) throw new Error('empty detail')
      engineIndex[runtimeId] = d
      renderEngineDetail(d)
    })
    .catch(function (e) {
      engineDetail.value = null
      app().showToast(
        (t('toast.load_failed') || '加载失败') + (e && e.message ? ': ' + e.message : '')
      )
    })
}
function upgradeEngineDetail(runtimeId) {
  API()
    .get('/settings/agent-runtimes/' + runtimeId + repoQs())
    .then(function (d) {
      if (!d || d.id === undefined) return
      delete d._partial
      engineIndex[runtimeId] = d
      if (engineDetail.value && engineDetail.value.id === runtimeId) renderEngineDetail(d)
    })
    .catch(function () {})
}
function renderEngineDetail(rt) {
  engineDetail.value = rt
  edSceneTags.value = (rt.scene_tags || []).slice()
  edAgentTags.value = (rt.agent_tags || []).slice()
  edMaxConcurrent.value = rt.max_concurrent || 20
  edSkillQuery.value = ''
  ensureSceneTags()
}
function closeEngineDetail() {
  engineDetail.value = null
  if (skillScanDirty) {
    skillScanDirty = false
    refreshRuntimes()
  }
}
function copyEngineName(rt) {
  copyText(engineDisplayName(rt))
}
// 场景可用角色
function sceneAvailableRoles(sceneTags) {
  if (!sceneTagsCache.value || !sceneTags || sceneTags.length === 0)
    return ['all', 'vod', 'issue', 'coding', 'qa', 'sre']
  const roles = []
  sceneTags.forEach(function (sn) {
    const s = sceneTagsCache.value.find(function (x) {
      return x.name === sn
    })
    if (s && s.roles)
      s.roles.forEach((r) => {
        if (roles.indexOf(r) < 0) roles.push(r)
      })
  })
  return roles
}
const edSceneTagsDesc = computed(() => {
  if (!edSceneTags.value.length) return t('label.scene_tags_empty')
  const allRoles = []
  edSceneTags.value.forEach(function (sn) {
    const s = (sceneTagsCache.value || []).find((x) => x.name === sn)
    if (s && s.roles)
      s.roles.forEach((r) => {
        if (allRoles.indexOf(r) < 0) allRoles.push(r)
      })
  })
  const labels = allRoles.map((r) => rtTagLabel(r) || r).join(', ')
  return t('label.scene_tags_roles', { roles: labels })
})
const edTagOptions = computed(() => {
  const allowed = sceneAvailableRoles(edSceneTags.value)
  return edSceneTags.value.length > 0
    ? RT_TAG_OPTIONS.filter((x) => x === 'all' || allowed.indexOf(x) >= 0)
    : RT_TAG_OPTIONS
})
function edTagDisabled(tg) {
  const allowed = sceneAvailableRoles(edSceneTags.value)
  return edSceneTags.value.length > 0 && tg !== 'all' && allowed.indexOf(tg) < 0
}
function onEdSceneTagChange() {
  // 角色标签选项随场景变化（v-model 已更新，选项 computed 自动联动）
}
function onEdTagChange(tag) {
  // all 与具体角色互斥（与老 onEngineTagChange 等价）
  if (tag === 'all') {
    if (edAgentTags.value.includes('all')) {
      edAgentTags.value = ['all']
    }
  } else {
    const i = edAgentTags.value.indexOf('all')
    if (i >= 0) edAgentTags.value.splice(i, 1)
    const agents = ['vod', 'issue', 'coding', 'qa', 'sre'].filter((x) =>
      edAgentTags.value.includes(x)
    )
    if (agents.length === 5) {
      edAgentTags.value = ['all']
    }
  }
}
function saveSceneTags(rt) {
  if (!edSceneTags.value.length) {
    app().showToast(t('toast.scene_tags_empty'))
    return
  }
  API()
    .put('/runtime/runtimes/' + rt.id + '/scene-tags', { scene_tags: edSceneTags.value })
    .then(function (d) {
      const target = runtimes.value.find((r) => r.id === rt.id)
      if (target) target.scene_tags = d.scene_tags
      devices.value.forEach(function (dev) {
        const r = (dev.runtimes || []).find((x) => x.id === rt.id)
        if (r) r.scene_tags = d.scene_tags
      })
      if (engineDetail.value && engineDetail.value.id === rt.id)
        engineDetail.value.scene_tags = d.scene_tags
      app().showToast(t('toast.scene_tags_saved'))
    })
    .catch((e) => app().showToast(t('toast.save_failed', { msg: e.message })))
}
function saveEngineTags(rt) {
  API()
    .put('/runtime/runtimes/' + rt.id + '/tags', { tags: edAgentTags.value })
    .then(function (d) {
      const target = runtimes.value.find((r) => r.id === rt.id)
      if (target) target.agent_tags = d.tags
      devices.value.forEach(function (dev) {
        const r = (dev.runtimes || []).find((x) => x.id === rt.id)
        if (r) r.agent_tags = d.tags
      })
      if (engineDetail.value && engineDetail.value.id === rt.id)
        engineDetail.value.agent_tags = d.tags
      app().showToast(t('toast.tags_saved'))
    })
    .catch((e) => app().showToast(t('toast.save_failed', { msg: e.message })))
}
function saveMaxConcurrent(rt) {
  const val = parseInt(edMaxConcurrent.value, 10)
  if (!val || val < 1 || val > 100) {
    app().showToast(t('toast.max_concurrent_invalid'))
    return
  }
  API()
    .put('/runtime/runtimes/' + rt.id + '/max-concurrent', { max_concurrent: val })
    .then(function (d) {
      const target = runtimes.value.find((r) => r.id === rt.id)
      if (target) target.max_concurrent = d.max_concurrent
      app().showToast(t('toast.max_concurrent_saved'))
    })
    .catch((e) => app().showToast(t('toast.save_failed', { msg: e.message })))
}
function healthCanReset(rt) {
  return !!(rt && (rt.scope === 'user' || rt.scope === 'repo'))
}
function resetEngineHealth(rt) {
  app().showConfirm(t('health.reset_confirm'), function () {
    API()
      .post('/settings/agent-runtimes/' + rt.id + '/health-reset' + repoQs())
      .then(function () {
        app().showToast(t('health.reset_success'))
        closeEngineDetail()
        resetPageAndReload()
      })
      .catch(function (e) {
        let msg = (e && e.message) || t('health.reset_failed')
        if (msg === '仅仓库级/个人级 worker 设备的引擎支持重置健康分，全局引擎不开放') {
          msg = t('health.reset_global_denied')
        }
        app().showToast(msg)
      })
  })
}
function editRuntimeAlias(rt) {
  app().showPrompt({
    title: t('label.alias_prompt'),
    value: rt.alias || '',
    onConfirm: function (v) {
      if (v === null) return
      API()
        .put('/runtime/runtimes/' + rt.id + '/alias', { alias: v.trim() })
        .then(function (d) {
          const target = runtimes.value.find((r) => r.id === rt.id)
          if (target) target.alias = d.alias
          devices.value.forEach(function (dev) {
            const r = (dev.runtimes || []).find((x) => x.id === rt.id)
            if (r) r.alias = d.alias
          })
          if (engineDetail.value && engineDetail.value.id === rt.id)
            engineDetail.value.alias = d.alias
          app().showToast(t('toast.alias_saved'))
        })
        .catch((e) => app().showToast(t('toast.save_failed', { msg: e.message })))
    },
  })
}
// ═══ 技能列表（Issue #335 搜索过滤） ═══
const edSkillFiltered = computed(() => {
  const skills = (engineDetail.value && engineDetail.value.local_skills) || []
  const q = edSkillQuery.value.trim().toLowerCase()
  if (!q) return skills
  return skills.filter((s) => (s + ' ' + edSkillDesc(s)).toLowerCase().includes(q))
})
function edSkillDesc(s) {
  const details = (engineDetail.value && engineDetail.value.skill_scan_result) || []
  const d = details.find((x) => x.name === s)
  return d ? d.description || t('skill.no_desc') : t('skill.click_detail')
}
function openSkillDetail(s) {
  const details = (engineDetail.value && engineDetail.value.skill_scan_result) || []
  const d = details.find((x) => x.name === s)
  skillDetail.value = { name: s, data: d || null }
}
function refreshEngineSkills(rt) {
  skillScanning.value = true
  pollSkillScan(rt.id, 0, true)
}
function pollSkillScan(runtimeId, attempt, isFirst) {
  if (attempt > 30) {
    skillScanning.value = false
    app().showToast(t('toast.scan_timeout'))
    return
  }
  const url = '/runtime/runtimes/' + runtimeId + '/refresh-skills' + (isFirst ? '?force=true' : '')
  API()
    .post(url)
    .then((d) => {
      if (d.status === 'pending') {
        setTimeout(() => pollSkillScan(runtimeId, attempt + 1, false), 2000)
        return
      }
      skillScanning.value = false
      if (!engineDetail.value || engineDetail.value.id !== runtimeId) return
      const skills = d.skills || []
      skillScanDirty = true
      engineDetail.value.local_skills = skills.map((s) => s.name)
      engineDetail.value.skill_scan_result = skills.slice()
      app().showToast(t('toast.scan_complete', { count: skills.length }))
    })
    .catch((e) => {
      skillScanning.value = false
      app().showToast(t('toast.refresh_failed', { msg: e.message }))
    })
}

// ═══ scope 切换 ═══
function openScopeSwitcher(d) {
  scopeSwitcher.open = true
  scopeSwitcher.cid = d.credential_id
  scopeSwitcher.scope = d.scope || 'global'
  scopeSwitcher.repoKey = (currentRepo() || {}).fullName || ''
}
function confirmScopeSwitch() {
  const scope = scopeSwitcher.scope
  let repoKey = ''
  if (scope === 'repo') {
    repoKey = (scopeSwitcher.repoKey || '').trim()
    if (!repoKey) {
      app().showToast(t('toast.scope_repo_required'))
      return
    }
  }
  scopeSwitcher.open = false
  API()
    .put('/settings/runtime-credentials/' + scopeSwitcher.cid + '/scope', {
      scope: scope,
      repo_key: repoKey,
    })
    .then(function (d) {
      app().showToast(t('toast.scope_changed', { scope: scopeShort(d.scope) }))
      loadRuntimesData()
      if (tab.value === 'workers') loadRuntimeDevices()
    })
    .catch((e) => app().showToast(t('toast.save_failed', { msg: e.message })))
}
// ═══ 凭据操作 ═══
function renameCredential(cid) {
  app().showPrompt({
    title: t('credential.rename_prompt'),
    onConfirm: function (nn) {
      if (!nn) return
      API()
        .put('/settings/runtime-credentials/' + cid, { name: nn.trim() })
        .then(() => {
          app().showToast(t('credential.toast_renamed'))
          refreshRuntimes()
        })
        .catch((e) => app().showToast(t('credential.toast_rename_failed', { msg: e.message })))
    },
  })
}
function revokeCredential(cid) {
  app().showConfirm(
    t('credential.revoke_confirm'),
    function () {
      API()
        .post('/settings/runtime-credentials/' + cid + '/revoke')
        .then(() => {
          app().showToast(t('credential.toast_revoked'))
          refreshRuntimes()
        })
        .catch((e) => app().showToast(t('credential.toast_revoke_failed', { msg: e.message })))
    },
    { danger: true }
  )
}

// ═══ 添加设备侧边栏 ═══
function toggleCredSection() {
  credExpanded.value = !credExpanded.value
  devExpanded.value = false
}
function toggleDevSection() {
  devExpanded.value = !devExpanded.value
  credExpanded.value = false
}
function openAddDevicePanel() {
  addPanel.open = true
  stopDevspaceAuto()
  credExpanded.value = false
  devExpanded.value = false
  credResult.value = null
  credForm.name = ''
  credForm.scope = 'user'
  credForm.repoKey = (currentRepo() || {}).fullName || ''
}
function closeAddDevicePanel() {
  stopDevspaceAuto()
  addPanel.open = false
}
function doCreateCredential() {
  const name = credForm.name
  if (!name) {
    app().showToast(t('credential.toast_name_required'))
    return
  }
  const scope = credForm.scope
  const repoKey = scope === 'repo' || scope === 'user' ? (credForm.repoKey || '').trim() : ''
  if (scope === 'repo' && !repoKey) {
    app().showToast(t('credential.toast_repo_required'))
    return
  }
  credResult.value = { loading: true }
  API()
    .post('/settings/runtime-credentials', { name: name, scope: scope, repo_key: repoKey })
    .then((d) => {
      const token = d.token || ''
      const origin = G.window.location.origin
      credResult.value = {
        name: name,
        scope: scope,
        token: token,
        oneCmd:
          'curl -fsSL ' +
          origin +
          '/api/runtime/install | bash -s -- --token ' +
          token +
          ' --server ' +
          origin,
        winCmd:
          '& ([scriptblock]::Create((irm ' +
          origin +
          '/api/runtime/install.ps1))) -Token ' +
          token +
          ' -Server ' +
          origin,
        runCmd: 'RUNTIME_TOKEN=' + token + ' python -m runtime.agent',
      }
      app().showToast(t('credential.toast_generated'))
    })
    .catch((e) => {
      credResult.value = null
      app().showToast(t('credential.toast_gen_failed', { msg: e.message }))
    })
}

// ═══ AI Devspace ═══
function refreshDevspaceList() {
  devspaceLoading.value = true
  devspaceError.value = null
  API()
    .get('/devstage/environments')
    .then(function (d) {
      devspaceLoading.value = false
      devspaces.value = d.environments || []
    })
    .catch(function (e) {
      devspaceLoading.value = false
      const msg = e.message || ''
      const isAuthErr = msg.indexOf('Unauthorized') >= 0 || msg.indexOf('APIGW.0301') >= 0
      const isAgreementErr = msg.indexOf('83700031') >= 0 || msg.indexOf('not sign agreement') >= 0
      devspaceError.value = {
        title: isAuthErr
          ? t('devspace.configure_aksk')
          : isAgreementErr
            ? t('devspace.agreement_not_signed')
            : t('devspace.load_failed'),
        detail: isAuthErr ? t('devspace.configure_aksk_hint') : isAgreementErr ? '' : msg,
      }
    })
}
function startDevspaceAuto() {
  stopDevspaceAuto()
  devspaceAuto.value = true
  devspaceTimer = setInterval(function () {
    if (!devExpanded.value) {
      stopDevspaceAuto()
      return
    }
    refreshDevspaceList()
  }, 15000)
}
function stopDevspaceAuto() {
  if (devspaceTimer) {
    clearInterval(devspaceTimer)
    devspaceTimer = null
  }
  devspaceAuto.value = false
}
function devStatusColor(st) {
  return DEV_STATUS_COLORS[st] || '#8d97a8'
}
function devStatusBg(st) {
  return 'color-mix(in srgb, ' + devStatusColor(st) + ' 15%, transparent)'
}
function devStatusLabel(st) {
  return t('devspace.status.' + (st || 'unknown').replace(/\./g, '_'))
}
function devSpec(e) {
  const cs = e.compute_spec || {}
  const acc = cs.accelerator || {}
  const parts = [(cs.cpu_cores || '?') + 'vCPU', cs.ram || '?', cs.cpu_arch || '']
  if (acc.accel_type) parts.push(acc.accel_type + ' ' + acc.accel_model + 'x' + acc.card_count)
  return (
    parts.filter(Boolean).join(' . ') + ' · ' + (e.type || '') + ' · ' + ((e.image || {}).os || '')
  )
}
function devCanStart(e) {
  return (e.status === 'cde.0000' || e.status === 'cde.0004') && !devBusy(e)
}
function devBusy(e) {
  return BUSY_STATES.indexOf(e.status) >= 0 || e.status === 'cde.0009'
}
function showCreateEnvForm() {
  const repo = currentRepo()
  let repoName = repo && repo.name ? repo.name : 'grape'
  const now = new Date()
  const ts =
    String(now.getMonth() + 1).padStart(2, '0') +
    String(now.getDate()).padStart(2, '0') +
    String(now.getHours()).padStart(2, '0') +
    String(now.getMinutes()).padStart(2, '0')
  let maxRepo = 15 - 1 - ts.length
  if (maxRepo < 1) maxRepo = 1
  repoName = repoName.replace(/[^a-zA-Z0-9_]/g, '').slice(0, maxRepo)
  const baseName = repoName + '_' + ts
  devspaceLoading.value = true
  doCreateEnvWithRetry(baseName, 0)
}
function doCreateEnvWithRetry(baseName, attempt) {
  let envName = attempt === 0 ? baseName : baseName + '_' + attempt
  if (envName.length > 15) {
    const parts = baseName.split('_')
    let repoPart = parts[0]
    const tsPart = parts.slice(1).join('_')
    const suffix = '_' + attempt
    let maxRepo = 15 - tsPart.length - 1 - suffix.length
    if (maxRepo < 1) maxRepo = 1
    repoPart = repoPart.slice(0, maxRepo)
    envName = repoPart + '_' + tsPart + suffix
  }
  const repo = currentRepo()
  const repoKey = repo ? repo.fullName : ''
  const credName = envName.slice(0, 20)
  // 服务端权威地址（5min localStorage 缓存）回退 origin
  let grapeServer = G.window.location.origin
  try {
    const cached = JSON.parse(G.localStorage.getItem('grape_server_base') || 'null')
    if (cached && cached.url && Date.now() - (cached.ts || 0) < 300000) {
      grapeServer = cached.url
    }
  } catch (e) {
    /* ignore */
  }
  const startCreate = function (server) {
    doCreateEnvWithCred(envName, credName, repoKey, server, baseName, attempt)
  }
  API()
    .get('/health')
    .then(function (h) {
      const base = (h && h.server_base_url) || ''
      if (base) {
        grapeServer = base
        try {
          G.localStorage.setItem('grape_server_base', JSON.stringify({ url: base, ts: Date.now() }))
        } catch (e) {
          /* ignore */
        }
      }
      startCreate(grapeServer)
    })
    .catch(function () {
      startCreate(grapeServer)
    })
}
function doCreateEnvWithCred(envName, credName, repoKey, grapeServer, baseName, attempt) {
  // scope 默认个人级 user（2026-10-13 调整，与 SRE 个人机器语义对齐）
  API()
    .post('/settings/runtime-credentials', { name: credName, scope: 'user' })
    .then(function (d) {
      const grapeToken = d.token || ''
      const credId = d.id || 0
      if (!grapeToken) {
        app().showToast(t('devspace.cred_gen_failed'))
        refreshDevspaceList()
        return
      }
      API()
        .post('/devstage/environments', {
          name: envName,
          grape_server: grapeServer,
          grape_token: grapeToken,
        })
        .then(function () {
          app().showToast(t('devspace.create_success'))
          refreshDevspaceList()
        })
        .catch(function (e) {
          const msg = e.message || ''
          const revokeAnd = function (fn) {
            API()
              .post('/settings/runtime-credentials/' + credId + '/revoke')
              .then(fn)
              .catch(fn)
          }
          if (
            attempt < 9 &&
            (msg.indexOf('98320005') >= 0 || msg.indexOf('already been used') >= 0)
          ) {
            revokeAnd(function () {
              doCreateEnvWithRetry(baseName, attempt + 1)
            })
          } else if (msg.indexOf('98320022') >= 0 || msg.indexOf('count limit') >= 0) {
            API().post('/settings/runtime-credentials/' + credId + '/revoke')
            app().showToast(t('devspace.count_limit'))
            refreshDevspaceList()
          } else if (msg.indexOf('83700031') >= 0 || msg.indexOf('not sign agreement') >= 0) {
            API().post('/settings/runtime-credentials/' + credId + '/revoke')
            app().showToast(t('devspace.agreement_not_signed'), 8000)
            refreshDevspaceList()
          } else if (msg.indexOf('98320049') >= 0 || msg.indexOf('Template not published') >= 0) {
            API().post('/settings/runtime-credentials/' + credId + '/revoke')
            app().showToast(t('devspace.template_not_published'))
            refreshDevspaceList()
          } else {
            API().post('/settings/runtime-credentials/' + credId + '/revoke')
            app().showToast(t('devspace.create_failed_msg', { msg: msg }))
            refreshDevspaceList()
          }
        })
    })
    .catch(function (e) {
      const msg = e.message || ''
      if (attempt < 9 && (msg.indexOf('已存在') >= 0 || msg.indexOf('exist') >= 0)) {
        doCreateEnvWithRetry(baseName, attempt + 1)
      } else {
        app().showToast(t('devspace.cred_gen_failed_msg', { msg: msg }))
        refreshDevspaceList()
      }
    })
}
function devAction(action, devstageId) {
  if (action === 'delete') {
    app().showConfirm(
      t('devspace.confirm_delete'),
      function () {
        devActionDo(action, devstageId)
      },
      { danger: true }
    )
  } else {
    devActionDo(action, devstageId)
  }
}
function devActionDo(action, devstageId) {
  devspaceLoading.value = true
  if (action === 'delete') {
    API()
      .del('/devstage/environments/' + devstageId)
      .then(() => {
        app().showToast(t('devspace.deleting'))
        setTimeout(refreshDevspaceList, 1000)
      })
      .catch(function (e) {
        const msg = e.message || ''
        if (msg.indexOf('98320063') >= 0 || msg.indexOf('can not be deleted') >= 0) {
          app().showToast(t('devspace.cannot_delete_running'))
        } else {
          app().showToast(t('devspace.delete_failed', { msg: msg }))
        }
        refreshDevspaceList()
      })
  } else {
    API()
      .post('/devstage/environments/' + action, { devstage_id: devstageId })
      .then(() => {
        app().showToast(action === 'start' ? t('devspace.starting') : t('devspace.stopping'))
        setTimeout(refreshDevspaceList, 1000)
      })
      .catch(function (e) {
        app().showToast(t('devspace.op_failed', { msg: e.message }))
        refreshDevspaceList()
      })
  }
}

// devspace 展开/收起联动自动刷新
watch(devExpanded, function (v) {
  if (v) {
    refreshDevspaceList()
    startDevspaceAuto()
  } else {
    stopDevspaceAuto()
  }
})

// ═══ 下拉关闭（document 点击委托） ═══
function onDocClick(e) {
  if (sceneDropdown.value && !e.target.closest('.rtv-ddwrap')) sceneDropdown.value = false
  if (tagDropdown.value && !e.target.closest('.rtv-ddwrap')) tagDropdown.value = false
}

// ═══ 生命周期 ═══
onMounted(function () {
  G.window._currentView = 'runtimes' // app.js nav 契约（已设，防御）
  document.addEventListener('click', onDocClick)
  try {
    grapeUsername = JSON.parse(G.localStorage.getItem('user') || '{}').username || ''
  } catch (e) {
    grapeUsername = ''
  }
  // Engines 端 avail_scopes 从返回数据取
  loadRuntimesData()
  // 异步拉一次 scene-tags（与老代码一致：先渲染骨架再补 display_name）
  ensureSceneTags()
})
onBeforeUnmount(function () {
  document.removeEventListener('click', onDocClick)
  stopDevspaceAuto()
})
</script>

<style scoped>
.rtv {
  min-height: 100%;
}
.rtv .loading,
.rtv .error {
  padding: 40px;
  text-align: center;
}
/* 工具栏 */
.rtv-toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
  align-items: center;
  flex-wrap: wrap;
}
/* 搜索：整组一个药丸（输入框无边框 + 图标按钮嵌在右端） */
.rtv-searchgrp {
  display: flex;
  align-items: center;
  flex: 1;
  max-width: 300px;
  border: 1px solid var(--b);
  border-radius: 999px;
  background: var(--bg);
  overflow: hidden;
  transition:
    border-color 0.18s,
    background 0.18s;
}
.rtv-searchgrp:focus-within {
  border-color: var(--a-text);
  background: var(--c);
}
.rtv-search {
  flex: 1;
  padding: 7px 6px 7px 14px;
  border: none;
  border-radius: 0;
  font-size: 13px;
  background: transparent;
  color: var(--t);
  outline: none;
  min-width: 0;
}
.rtv-search:focus {
  background: transparent;
}
.rtv-searchbtn {
  border: none;
  background: transparent;
  color: var(--t3);
  align-self: center;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin: 0 3px;
  cursor: pointer;
  flex-shrink: 0;
  transition:
    background 0.18s,
    color 0.18s;
}
.rtv-searchbtn:hover {
  background: color-mix(in srgb, var(--a) 16%, transparent);
  color: var(--a);
}
/* 主操作：实心 accent + 白字（几何由 bridge.css 统一） */
.rtv-addbtn {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  border: none;
  background: var(--a);
  color: var(--on-accent);
  font-weight: 600;
  cursor: pointer;
  font-family: inherit;
  transition:
    background 0.18s,
    filter 0.18s;
}
.rtv-addbtn:hover {
  background: var(--ah);
  filter: brightness(1.06);
}
/* 空态 */
.rtv-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 60px 20px;
  gap: 12px;
}
.rtv-empty-ic {
  font-size: 48px;
}
.rtv-empty-t {
  font-size: 15px;
  font-weight: 600;
  color: var(--t);
}
.rtv-empty-d {
  font-size: 13px;
  color: var(--t3);
}
/* 筛选 */
.rtv-hhelp {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 15px;
  height: 15px;
  border-radius: 50%;
  background: var(--chip-bg);
  color: var(--t2);
  font-size: 10px;
  font-weight: 700;
  margin-left: 4px;
  cursor: help;
  flex-shrink: 0;
}
.rtv-vstale {
  font-size: 10px;
  font-weight: 600;
  color: var(--y);
  background: var(--chip-bg);
  padding: 1px 8px;
  border-radius: 999px;
  margin-left: 4px;
  cursor: help;
}
.rtv-empty-guide {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 56px 20px;
  text-align: center;
}
.rtv-empty-guide-t {
  font-size: 15px;
  font-weight: 600;
  color: var(--t);
}
.rtv-empty-guide-s {
  font-size: 13px;
  color: var(--t3);
}
.rtv-filters {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.rtv-filter-lbl {
  font-size: 11px;
  color: var(--t3);
  font-weight: 500;
  margin-right: 2px;
}
.rtv-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  cursor: pointer;
  font-size: 11px;
  padding: 4px 12px;
  border-radius: 999px;
  border: 1px solid var(--b);
  color: var(--t);
  background: var(--c);
  font-weight: 500;
  transition: all 0.2s;
}
.rtv-chip.on {
  border-color: var(--a-text);
  color: var(--on-accent);
  background: var(--a);
}
.rtv-chip-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}
.rtv-chip-sep {
  width: 1px;
  background: var(--b);
  margin: 2px 4px;
  height: 18px;
}
.rtv-caret {
  font-size: 8px;
  opacity: 0.6;
}
.rtv-clear {
  cursor: pointer;
  font-size: 11px;
  padding: 4px 12px;
  border-radius: 999px;
  color: var(--t3);
  font-weight: 400;
}
.rtv-clear:hover {
  color: var(--r);
}
.rtv-ddwrap {
  position: relative;
  display: inline-block;
}
.rtv-dd {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  background: var(--c);
  border: 1px solid var(--b);
  border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
  z-index: 100;
  padding: 4px;
  min-width: 140px;
}
.rtv-dd-item {
  padding: 5px 10px;
  font-size: 11px;
  cursor: pointer;
  border-radius: 4px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.rtv-dd-item:hover {
  background: var(--bg);
}
.rtv-tagpill {
  font-size: 9px;
  padding: 1px 7px;
  border-radius: 999px;
  color: var(--on-accent);
}
/* 汇总 */
.rtv-summary {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  padding: 7px 14px;
  margin-bottom: 10px;
  font-size: 12px;
  color: var(--t2);
  background: var(--bg);
  border-radius: 8px;
}
.rtv-sum-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.rtv-sum-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  display: inline-block;
}
/* 卡片网格 */
.rtv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 12px;
}
.rtv-ecard {
  background: var(--c);
  border: 1px solid var(--b);
  border-left: 3px solid var(--t3);
  border-radius: 0 var(--r2) var(--r2) 0;
  padding: 14px 16px;
  box-shadow: var(--sh);
  cursor: pointer;
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.rtv-watermark {
  position: absolute;
  right: 12px;
  bottom: 8px;
  width: 56px;
  height: 56px;
  object-fit: contain;
  opacity: 0.25;
  pointer-events: none;
}
.rtv-ecard-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  flex-wrap: wrap;
  min-height: 24px;
}
.rtv-hbadge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 999px;
  white-space: nowrap;
}
.rtv-hbadge.online {
  color: var(--g);
  background: color-mix(in srgb, var(--g) 15%, transparent);
}
.rtv-hbadge.recently_lost {
  color: var(--y);
  background: color-mix(in srgb, var(--y) 16%, transparent);
}
.rtv-hbadge.offline {
  color: var(--t3);
  background: var(--chip-bg);
}
.rtv-hbadge.about_to_gc {
  color: var(--r);
  background: color-mix(in srgb, var(--r) 15%, transparent);
}
.rtv-dot7 {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}
.rtv-sbadge {
  display: inline-flex;
  align-items: center;
  font-size: 10px;
  font-weight: 600;
  border-radius: 999px;
  padding: 1px 8px;
  white-space: nowrap;
  vertical-align: middle;
  color: var(--on-accent);
}
.rtv-sbadge.global {
  background: #2f63d6;
}
.rtv-sbadge.repo {
  background: #7152c9;
}
.rtv-sbadge.user {
  background: #157f3d;
}
.rtv-sbadge.local {
  background: #0b6276;
}
.rtv-ename {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  overflow: hidden;
  flex: 1;
  min-width: 0;
}
.rtv-ename-b {
  font-weight: 600;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rtv-ename-s {
  font-size: 10px;
  color: var(--t3);
  font-weight: 400;
}
.rtv-copy {
  flex-shrink: 0;
  cursor: pointer;
  transition: color 0.2s;
  color: var(--t3);
}
.rtv-copy:hover {
  color: var(--g);
}
.rtv-ecard-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 16px;
  font-size: 12px;
  color: var(--t2);
  flex: 1;
}
.rtv-kv {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  display: flex;
  align-items: center;
  gap: 4px;
}
/* Issue #865：模型行跨两列 + 值省略号（不与 .rtv-ellipsis 的弱化字色混用） */
.rtv-kv-full {
  grid-column: 1 / -1;
  min-width: 0;
}
.rtv-vellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.rtv-ts {
  font-size: 11px;
  color: var(--t3);
  flex-shrink: 0;
}
.rtv-models {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.rtv-model-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  color: var(--t2);
  min-width: 0;
}
.rtv-model-row > .rtv-vellipsis {
  flex: 1;
}
.rtv-k {
  color: var(--t3);
  display: inline-block;
  min-width: 46px;
  flex-shrink: 0;
}
/* Worker 卡片 */
.rtv-wcard {
  background: var(--c);
  border: 1px solid var(--b);
  border-radius: 0 var(--r2) var(--r2) 0;
  padding: 12px 14px;
  box-shadow: var(--sh);
}
.rtv-wcard-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.rtv-wname {
  font-weight: 600;
  font-size: 14px;
}
.rtv-wip {
  font-weight: 400;
  color: var(--t3);
  font-size: 12px;
}
.rtv-wbtn {
  font-size: 11px;
  padding: 4px 10px;
  border-radius: 4px;
}
.rtv-wcard-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  font-size: 12px;
  color: var(--t2);
}
.rtv-wcard-cols {
  display: flex;
  gap: 16px;
  flex: 1;
  min-width: 0;
  flex-wrap: wrap;
}
.rtv-wcard-engines {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--t2);
}
.rtv-wcard-clis {
  flex: 1;
  min-width: 0;
  word-break: break-all;
}
.rtv-cli {
  cursor: pointer;
  color: var(--a-text);
  text-decoration: underline;
}
/* 分页 */
.rtv-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 12px 0;
  flex-wrap: wrap;
}
.rtv-pager button {
  padding: 4px 11px;
  min-width: 28px;
  border: 1px solid var(--b);
  border-radius: 999px;
  background: var(--c);
  color: var(--t);
  font-size: 12px;
  cursor: pointer;
  margin: 0 2px;
}
.rtv-pager button.on {
  border-color: var(--a);
  background: var(--a);
  color: var(--on-accent);
  font-weight: 600;
}
.rtv-pager button:disabled {
  color: var(--t3);
  opacity: 0.4;
  cursor: default;
}
.rtv-ellipsis {
  color: var(--t3);
  font-size: 12px;
  padding: 0 4px;
}
.rtv-pager-sum {
  font-size: 11px;
  color: var(--t3);
  margin-left: 8px;
}
/* 图例 */
.rtv-legend {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 8px 0;
  font-size: 11px;
  color: var(--t3);
  border-top: 1px dashed var(--b);
  margin-top: 8px;
}
.rtv-legend-lbl {
  font-weight: 500;
  color: var(--t2);
}
.rtv-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
/* 弹窗通用 */
.rtv-modal-ov {
  position: fixed;
  inset: 0;
  z-index: 1001;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
}
/* 引擎详情 */
.rtv-engdetail {
  background: var(--c);
  border-radius: 12px;
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.2);
  max-width: 680px;
  width: 90%;
  max-height: 85vh;
  overflow: auto;
  padding: 24px;
}
.rtv-ed-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.rtv-ed-name {
  font-weight: 700;
  font-size: 18px;
}
.rtv-ed-alias {
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 4px;
}
.rtv-localpill {
  font-size: 11px;
  font-weight: 600;
  color: var(--t2);
  background: var(--chip-bg);
  border-radius: 999px;
  padding: 1px 8px;
}
.rtv-ed-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 20px;
  font-size: 13px;
  color: var(--t2);
  margin-bottom: 20px;
}
.rtv-reset-btn {
  font-size: 11px;
  padding: 3px 10px;
  border-radius: 4px;
  margin-left: 4px;
}
.rtv-ed-sec {
  border-top: 1px solid var(--b);
  padding-top: 16px;
  margin-bottom: 16px;
}
.rtv-ed-last {
  margin-bottom: 0;
}
.rtv-ed-sechead {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
  font-weight: 600;
  font-size: 14px;
}
.rtv-ed-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.rtv-ed-taglbl {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  padding: 2px 6px;
}
.rtv-ed-taglbl input {
  width: 14px;
  height: 14px;
}
.rtv-tagdesc {
  color: var(--t3);
  font-size: 11px;
}
.rtv-ed-desc {
  margin-top: 6px;
}
.rtv-muted {
  color: var(--t3);
  font-size: 11px;
}
.rtv-mcrow {
  display: flex;
  align-items: center;
  gap: 10px;
}
.rtv-mc-input {
  width: 80px;
  padding: 6px 10px;
  border: 1px solid var(--b);
  border-radius: 8px;
  font-size: 14px;
  background: var(--bg);
  color: var(--t);
  outline: none;
}
.rtv-skcount {
  font-weight: 400;
  color: var(--t3);
  font-size: 12px;
}
.rtv-sksearch {
  width: 100%;
  box-sizing: border-box;
  padding: 6px 10px;
  border: 1px solid var(--b);
  border-radius: 8px;
  font-size: 13px;
  background: var(--bg);
  color: var(--t);
  outline: none;
  margin-bottom: 8px;
}
.rtv-sklist {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.rtv-skitem {
  padding: 8px 12px;
  border: 1px solid var(--b);
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s;
}
.rtv-skitem:hover {
  background: var(--bg);
}
.rtv-sk-name {
  font-weight: 600;
  font-size: 13px;
  color: var(--t);
}
.rtv-sk-desc {
  font-size: 11px;
  color: var(--t3);
  margin-top: 2px;
}
/* 技能详情 */
.rtv-skilldetail {
  background: var(--c);
  border-radius: 12px;
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.25);
  max-width: 520px;
  width: 90%;
  max-height: 80vh;
  overflow: auto;
  padding: 24px;
}
.rtv-sd-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
}
.rtv-sd-name {
  font-weight: 700;
  font-size: 16px;
}
.rtv-sd-grid {
  display: grid;
  gap: 8px;
  font-size: 13px;
  color: var(--t2);
}
.rtv-sd-grid .rtv-kv {
  white-space: normal;
}
.rtv-sd-path {
  font-size: 11px;
  background: var(--bg);
  padding: 2px 6px;
  border-radius: 4px;
  word-break: break-all;
}
/* scope 切换 */
.rtv-scopemodal {
  background: var(--c);
  border-radius: 12px;
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.2);
  width: 320px;
  padding: 20px;
}
.rtv-sm-title {
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 14px;
}
.rtv-sm-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border: 1px solid var(--b);
  border-radius: 8px;
  cursor: pointer;
  background: var(--bg);
  margin-bottom: 8px;
  font-size: 13px;
  color: var(--t);
}
.rtv-sm-item.on {
  border-color: var(--a-text);
  background: rgba(0, 113, 227, 0.06);
  font-weight: 600;
}
.rtv-sm-repo {
  margin-bottom: 12px;
}
.rtv-sm-input {
  width: 100%;
  padding: 7px 10px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 12px;
  background: var(--bg);
  color: var(--t);
  outline: none;
}
.rtv-sm-btns {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 4px;
}
/* 添加设备侧边栏 */
.rtv-side-ov {
  position: fixed;
  inset: 0;
  z-index: 1001;
  display: flex;
  justify-content: flex-end;
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  background: rgba(0, 0, 0, 0.3);
}
.rtv-sidebar {
  width: 520px;
  max-width: 92vw;
  height: 100vh;
  background: var(--c);
  box-shadow: -12px 0 40px rgba(0, 0, 0, 0.15);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}
.rtv-sb-head {
  padding: 20px 24px;
  display: flex;
  align-items: center;
  gap: 12px;
  border-bottom: 1px solid var(--b);
  position: sticky;
  top: 0;
  background: var(--c);
  z-index: 2;
}
.rtv-sb-ic {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: var(--a);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.rtv-sb-title {
  flex: 1;
  min-width: 0;
}
.rtv-sb-title h3 {
  font-size: 17px;
  font-weight: 700;
  margin: 0;
  line-height: 1.2;
}
.rtv-sb-title p {
  font-size: 12px;
  color: var(--t3);
  margin: 3px 0 0;
  line-height: 1.3;
}
.rtv-sb-close {
  width: 32px;
  height: 32px;
  border-radius: 999px;
  border: 1px solid transparent;
  background: var(--chip-bg);
  cursor: pointer;
  color: var(--t2);
  font-size: 16px;
  flex-shrink: 0;
  transition:
    background 0.18s,
    color 0.18s,
    border-color 0.18s;
}
.rtv-sb-close:hover {
  background: color-mix(in srgb, var(--r) 16%, transparent);
  color: var(--r);
  border-color: color-mix(in srgb, var(--r) 40%, transparent);
}
.rtv-sb-body {
  padding: 24px;
  flex: 1;
}
.rtv-sb-sec {
  margin-bottom: 16px;
}
.rtv-sb-opt {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 16px 18px;
  background: color-mix(in srgb, var(--a) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--a) 20%, transparent);
  border-radius: 12px;
  cursor: pointer;
  transition: all 0.2s;
}
.rtv-sb-opt:hover {
  border-color: color-mix(in srgb, var(--a) 50%, transparent);
  box-shadow: 0 2px 12px color-mix(in srgb, var(--a) 12%, transparent);
}
.rtv-sb-opt-ic {
  width: 42px;
  height: 42px;
  border-radius: 10px;
  background: var(--a);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.rtv-sb-opt-body {
  flex: 1;
  min-width: 0;
}
.rtv-sb-opt-t {
  font-size: 15px;
  font-weight: 600;
  color: var(--t);
  line-height: 1.3;
}
.rtv-sb-opt-d {
  font-size: 12px;
  color: var(--t3);
  margin-top: 2px;
}
.rtv-sb-chev {
  flex-shrink: 0;
  transition: transform 0.3s;
}
.rtv-sb-chev.open {
  transform: rotate(180deg);
}
.rtv-sb-area {
  margin-top: 12px;
}
.rtv-sb-loading {
  padding: 32px;
  text-align: center;
  color: var(--t3);
  font-size: 13px;
}
.rtv-spinner {
  display: inline-block;
  width: 24px;
  height: 24px;
  border: 2.5px solid color-mix(in srgb, var(--a) 20%, transparent);
  border-top-color: var(--a);
  border-radius: 50%;
  animation: rtvSpin 0.8s linear infinite;
  margin-bottom: 12px;
}
.rtv-spinner.purple {
  width: 20px;
  height: 20px;
  border-color: color-mix(in srgb, var(--a) 20%, transparent);
  border-top-color: var(--a);
}
@keyframes rtvSpin {
  to {
    transform: rotate(360deg);
  }
}
/* 凭据表单 */
.rtv-credbox {
  background: var(--bg);
  border: 1px solid var(--b);
  border-radius: 12px;
  padding: 18px 20px;
}
.rtv-cred-t {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 14px;
  color: var(--t);
}
.rtv-cred-fld {
  margin-bottom: 12px;
}
.rtv-cred-fld label {
  display: block;
  font-size: 11px;
  font-weight: 600;
  color: var(--t3);
  margin-bottom: 5px;
  text-transform: uppercase;
}
.rtv-cred-input {
  width: 100%;
  box-sizing: border-box;
  padding: 9px 12px;
  border: 1px solid var(--b);
  border-radius: 8px;
  font-size: 13px;
  background: var(--c);
  color: var(--t);
  font-family: inherit;
}
.rtv-cred-disabled {
  background: var(--chip-bg);
  color: var(--t2);
}
.rtv-cred-btns {
  display: flex;
  gap: 8px;
  margin-top: 16px;
}
.rtv-cred-gen {
  flex: 1;
  justify-content: center;
}
.rtv-cred-warn {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  background: color-mix(in srgb, var(--y) 16%, transparent);
  border-radius: 8px;
  margin-bottom: 14px;
  font-size: 12px;
  color: var(--y);
  line-height: 1.5;
}
.rtv-cred-lbl {
  display: block;
  font-size: 11px;
  font-weight: 600;
  color: var(--t3);
  margin-bottom: 5px;
  text-transform: uppercase;
}
.rtv-cred-code {
  display: block;
  padding: 10px 12px;
  background: var(--c);
  border: 1px solid var(--b);
  border-radius: 8px;
  word-break: break-all;
  font-size: 12px;
  margin-bottom: 14px;
}
.rtv-cred-code.sm {
  font-size: 11px;
  margin-bottom: 6px;
}
.rtv-cred-sub {
  font-size: 12px;
  font-weight: 600;
  color: var(--t);
  margin-bottom: 5px;
}
.rtv-cred-fullbtn {
  width: 100%;
  justify-content: center;
  margin-bottom: 8px;
}
.rtv-cred-desc {
  font-size: 11px;
  color: var(--t2);
  margin-bottom: 14px;
  line-height: 1.5;
}
/* Devspace */
.rtv-devbox {
  border: 1px solid var(--b);
  border-radius: 12px;
  overflow: hidden;
}
.rtv-dev-head {
  padding: 14px 18px;
  display: flex;
  align-items: center;
  gap: 10px;
  border-bottom: 1px solid var(--b);
  background: color-mix(in srgb, var(--a) 14%, var(--c));
}
.rtv-dev-title {
  font-size: 14px;
  font-weight: 600;
  flex: 1;
  color: var(--t);
}
.rtv-dev-auto {
  font-size: 10px;
  color: var(--t3);
}
.rtv-dev-create {
  background: var(--a);
  color: var(--on-accent);
  border: 1px solid transparent;
  border-radius: 999px;
  padding: 6px 14px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition:
    background 0.18s,
    filter 0.18s;
}
.rtv-dev-create:hover {
  background: var(--ah);
  filter: brightness(1.06);
}
.rtv-dev-refresh {
  background: transparent;
  color: var(--a);
  border: 1px solid color-mix(in srgb, var(--a) 42%, transparent);
  border-radius: 999px;
  padding: 5px 12px;
  font-size: 14px;
  line-height: 1.2;
  cursor: pointer;
  transition:
    background 0.18s,
    color 0.18s,
    border-color 0.18s;
}
.rtv-dev-refresh:hover {
  background: var(--a);
  color: var(--on-accent);
  border-color: var(--a);
}
.rtv-dev-body {
  background: var(--bg);
}
.rtv-dev-empty {
  padding: 36px 20px;
  text-align: center;
}
.rtv-dev-empty-t {
  font-size: 13px;
  color: var(--t2);
  margin-bottom: 4px;
}
.rtv-dev-empty-d {
  font-size: 11px;
  color: var(--t3);
}
.rtv-dev-err {
  padding: 32px 20px;
  text-align: center;
}
.rtv-dev-err-t {
  font-size: 13px;
  color: var(--t2);
  margin-bottom: 4px;
}
.rtv-dev-err-d {
  font-size: 11px;
  color: var(--t3);
}
.rtv-dev-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--b);
  transition: background 0.12s;
}
.rtv-dev-row:hover {
  background: var(--c);
}
.rtv-dev-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.rtv-dev-main {
  flex: 1;
  min-width: 0;
}
.rtv-dev-rowhead {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 3px;
}
.rtv-dev-name {
  font-size: 13px;
  font-weight: 600;
}
.rtv-dev-status {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
}
.rtv-dev-spec {
  font-size: 11px;
  color: var(--t3);
}
.rtv-dev-progress {
  margin-top: 6px;
}
.rtv-dev-progbar {
  width: 160px;
  height: 6px;
  background: rgba(96, 165, 250, 0.12);
  border-radius: 3px;
  overflow: hidden;
}
.rtv-dev-progfill {
  width: 40%;
  height: 100%;
  background: var(--a);
  border-radius: 3px;
  animation: rtvDevPulse 2s ease-in-out infinite;
}
@keyframes rtvDevPulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.6;
  }
}
.rtv-dev-acts {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}
</style>
