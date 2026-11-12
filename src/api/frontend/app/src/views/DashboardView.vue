<template>
  <div class="dbv">
    <!-- ═══ 加载/错误态 ═══ -->
    <div v-if="loadError" class="error">{{ loadError }}</div>
    <div v-else-if="!loaded" class="loading">{{ t('loading') }}</div>

    <!-- ═══ 创建新场景模式 ═══ -->
    <template v-else-if="mode === 'create'">
      <button class="ap-btn" style="margin-bottom: 12px" @click="exitCreateMode()">
        ← {{ t('scene_center_title') }}
      </button>
      <div class="cd dbv-headcard">
        <div class="dbv-headrow">
          <div
            class="dbv-scene-icon"
            style="cursor: pointer"
            @click="iconPickerOpen = !iconPickerOpen"
          >
            {{ createIcon }}
          </div>
          <div class="dbv-headfields">
            <input
              v-model="createName"
              class="dbv-name-input"
              :placeholder="t('scene_name_placeholder')"
            />
            <input v-model="createDesc" class="dbv-desc-input" :placeholder="t('desc_optional')" />
            <div v-if="iconPickerOpen" class="dbv-iconpicker">
              <span
                v-for="ic in CUSTOM_ICONS"
                :key="ic"
                :class="['dbv-iconpick', { on: ic === createIcon }]"
                @click="createIcon = ic"
                >{{ ic }}</span
              >
            </div>
          </div>
          <button class="ap-btn" style="margin-top: 4px" @click="saveCreateScene()">
            {{ t('save') }}
          </button>
        </div>
      </div>
      <div class="det-card-wrap">
        <div class="dbv-sechead">
          <h2>{{ t('execution_flow') }}</h2>
        </div>
        <div class="ap-grid">
          <template v-for="(node, i) in editNodes" :key="node.id">
            <div :class="['ap-nd', { off: !node.enabled }]">
              <div class="ap-nd-top" :style="{ background: agentColor(node.id) }"></div>
              <div class="ap-nd-head">
                <span
                  class="ap-nd-num"
                  :style="{ background: agentColor(node.id) + '22', color: agentColor(node.id) }"
                  >{{ i + 1 }}</span
                >
                <span class="ap-nd-name">{{ agentIcon(node.id) }} {{ agentLabel(node.id) }}</span>
                <span
                  v-if="canToggle(node.id)"
                  class="ap-nd-toggle"
                  :style="{ color: node.enabled ? '#ff3b30' : '#34c759' }"
                  @click.stop="toggleAgent(node.id)"
                  >{{ node.enabled ? '✕' : '✓' }}</span
                >
              </div>
              <div v-if="node.enabled" class="ap-nd-desc">{{ agentDesc(node.id) }}</div>
              <div v-else class="ap-nd-off-lbl">{{ t('not_needed_in_scene') }}</div>
              <div class="ap-nd-hr"></div>
              <div class="ap-nd-skillbox">
                <div class="ap-nd-lbl">{{ t('label_skill') }}</div>
                <template v-if="node.enabled">
                  <div class="dbv-skillpick" @click.stop="openSkillDropdown(node.id, $event)">
                    {{ node.skill_display_name || node.skill || t('select_skill') }}
                  </div>
                </template>
                <span v-else class="ap-nd-none">{{ t('agent_disabled') }}</span>
              </div>
              <div class="ap-nd-engbox">
                <div class="ap-nd-lbl">{{ t('label_engine') }}</div>
                <template v-if="node.enabled">
                  <div v-html="engineBadgeHtml(node)"></div>
                  <button class="ap-nd-engbtn" @click.stop="openEngineModal(node.id)">
                    {{ t('select_engine') }}
                  </button>
                </template>
                <span v-else class="ap-nd-none">none</span>
              </div>
            </div>
            <span v-if="i < editNodes.length - 1" class="ap-arr">→</span>
          </template>
        </div>
      </div>
      <!-- PR 订阅 -->
      <div class="det-card-wrap dbv-prsub">
        <div style="flex: 1; min-width: 0">
          <div class="dbv-prsub-t">PR 订阅</div>
          <div class="dbv-prsub-d">
            开启后，QA Agent 将自动审查所有目标分支匹配本场景工作分支的 Pull
            Request，审查结果以评论形式回帖到对应 PR
          </div>
        </div>
        <span class="dbv-switch" :class="{ on: createPrSub }" @click="createPrSub = !createPrSub"
          ><span class="dbv-knob"></span
        ></span>
      </div>
      <!-- 高级选项 -->
      <div class="det-card-wrap">
        <div class="dbv-sechead">
          <h2>{{ t('advanced_options') }}</h2>
        </div>
        <div class="adv-row">
          <span class="adv-label">{{ t('work_branch') }}</span>
          <select v-model="advWorkBranch" class="adv-select">
            <option v-if="!branches.length" value="main">main</option>
            <option v-for="br in branches" :key="br.name" :value="br.name">
              {{ br.name }}{{ br.protected ? ' 🔒' : '' }}
            </option>
          </select>
          <button class="ap-btn" @click="newBranchOpen = !newBranchOpen">
            {{ t('create_branch') }}
          </button>
        </div>
        <div v-if="newBranchOpen" class="dbv-newbranch">
          <input v-model="newBranchName" :placeholder="t('branch_name')" class="dbv-nb-input" />
          <select v-model="newBranchRef" class="dbv-nb-sel">
            <option v-if="!branches.length" value="main">main</option>
            <option v-for="br in branches" :key="br.name" :value="br.name">{{ br.name }}</option>
          </select>
          <button class="ap-btn" @click="createNewBranch()">{{ t('create') }}</button>
          <button class="ap-btn" @click="newBranchOpen = false">{{ t('cancel') }}</button>
        </div>
        <div class="dbv-advsec">
          <div class="dbv-advsec-t">{{ t('custom_config') }}</div>
          <div class="dbv-cflist">
            <div v-for="(f, i) in advFields" :key="i" class="dbv-cfrow">
              <input
                v-model="f.name"
                class="dbv-cfkey"
                placeholder="name"
                :class="{ 'cf-invalid': nameError(f.name) }"
              />
              <input
                v-model="f.value"
                :type="f.show ? 'text' : 'password'"
                class="dbv-cfval"
                :placeholder="f.keepValue ? '********' : 'value'"
                @input="onFieldInput(f)"
              />
              <button class="dbv-cfeye" @click="f.keepValue ? openReveal(f) : (f.show = !f.show)">
                {{ f.show ? '🙈' : '👁' }}
              </button>
              <button class="dbv-cfdel" @click="advFields.splice(i, 1)">×</button>
            </div>
          </div>
          <button
            class="ap-btn dbv-addbtn-sm"
            @click="advFields.push({ name: '', value: '', show: false })"
          >
            + {{ t('add_config') }}
          </button>
        </div>
        <div class="dbv-advsec dbv-advsep">
          <div class="dbv-advsec-t">关联仓库</div>
          <div class="dbv-advsec-d">
            配置关联仓库后，一次需求开发可同时操作多个仓库，开发完成后向每个有改动的仓库分别提交 PR
          </div>
          <div class="dbv-cflist">
            <div v-for="(lr, i) in linkedRepos" :key="i" class="dbv-lrrow">
              <select v-model="lr.repoKey" class="dbv-lrrepo" @change="loadLinkedBranches(i)">
                <option value="">选择仓库</option>
                <option
                  v-for="r in linkedRepoOptions"
                  :key="r.repo_key || r.full_name"
                  :value="r.repo_key || r.full_name"
                >
                  {{ r.full_name }}
                </option>
              </select>
              <select v-model="lr.branch" class="dbv-lrbranch">
                <option value="">分支（默认 main）</option>
                <option v-for="br in lr.branches" :key="br.name" :value="br.name">
                  {{ br.name }}{{ br.protected ? ' 🔒' : '' }}
                </option>
              </select>
              <button class="dbv-cfdel" @click="linkedRepos.splice(i, 1)">×</button>
            </div>
          </div>
          <button class="ap-btn dbv-addbtn-sm" @click="addLinkedRepo()">+ 添加关联仓库</button>
        </div>
      </div>
    </template>

    <!-- ═══ 场景详情页 ═══ -->
    <template v-else-if="mode === 'detail'">
      <div v-if="detailError" class="error">{{ detailError }}</div>
      <div v-else-if="!detail" class="loading">{{ t('loading') }}</div>
      <template v-else>
        <button class="ap-btn" style="margin-bottom: 12px" @click="G.app.nav('dash')">
          ← {{ t('scene_center_title') }}
        </button>
        <!-- 名称/描述卡片（内联编辑） -->
        <div class="cd dbv-headcard">
          <div class="dbv-headrow">
            <div class="dbv-scene-icon">{{ detailIcon }}</div>
            <div class="dbv-headfields">
              <div v-if="!inlineEdit" class="dbv-name-display" @click="enterInlineEdit('name')">
                {{ detail.display_name || detail.name }}
              </div>
              <input
                v-else
                v-model="inlineName"
                class="dbv-name-input"
                @keyup.enter="saveInlineEdit('name')"
              />
              <div v-if="!inlineEditDesc" class="dbv-desc-display" @click="enterInlineEdit('desc')">
                <template v-if="detail.description">{{ detail.description }}</template>
                <span v-else class="dbv-desc-ph">{{ t('desc_optional') }}</span>
              </div>
              <input
                v-else
                v-model="inlineDesc"
                class="dbv-desc-input"
                @keyup.enter="saveInlineEdit('desc')"
              />
            </div>
            <div class="dbv-deployglobe">
              <template v-if="!deployUrlEdit">
                <a
                  v-if="deployUrl"
                  :href="deployUrl"
                  target="_blank"
                  rel="noopener"
                  class="dbv-globe-link"
                  :title="deployUrl"
                  @contextmenu.prevent="startDeployUrlEdit()"
                  @touchstart.passive="deployUrlPressStart()"
                  @touchend="deployUrlPressEnd()"
                  @touchmove="deployUrlPressEnd()"
                  >🔗</a
                >
                <button
                  v-else
                  class="dbv-globe-empty"
                  title="登记场景访问地址"
                  @click="startDeployUrlEdit()"
                >
                  🌐
                </button>
              </template>
              <template v-else>
                <input
                  v-model="deployUrlDraft"
                  class="dbv-deployurl-input"
                  placeholder="https://..."
                  @keyup.enter="saveDeployUrl()"
                />
                <button class="ap-btn bt-mini" @click="saveDeployUrl()">{{ t('save') }}</button>
                <button class="ap-btn bt-mini" @click="cancelDeployUrlEdit()">
                  {{ t('cancel') }}
                </button>
              </template>
            </div>
            <button class="ap-btn" style="margin-top: 4px" @click="copySceneFromDetail()">
              {{ t('copy_scene') }}
            </button>
            <button
              v-if="detail.scope !== 'official'"
              class="ap-btn ap-btn-d"
              style="margin-top: 4px"
              @click="confirmDeleteScene(detail.id, detail.display_name || detail.name)"
            >
              {{ t('delete') }}
            </button>
          </div>
        </div>
        <!-- 执行流程 -->
        <div class="det-card-wrap">
          <div class="dbv-sechead">
            <h2>{{ t('execution_flow') }}</h2>
            <div style="display: flex; gap: 8px">
              <template v-if="flowEditing">
                <button class="ap-btn" @click="saveSceneEdit()">{{ t('save') }}</button>
                <button class="ap-btn" @click="cancelSceneEdit()">{{ t('cancel') }}</button>
              </template>
              <button
                v-else-if="detail.scope !== 'official'"
                class="ap-btn"
                @click="enterFlowEdit()"
              >
                {{ t('edit') }}
              </button>
            </div>
          </div>
          <div class="ap-grid">
            <template
              v-for="(node, i) in flowEditing ? editNodes : viewNodes"
              :key="node.id + '-' + i"
            >
              <!-- off 判定用 === false：老场景 nodes 无 enabled 字段（null=启用），!node.enabled 会误判成全灰 -->
              <div
                :class="[
                  'ap-nd',
                  { off: node.enabled === false, 'scope-bad': !!scopeViolationOf(node) },
                ]"
              >
                <div
                  class="ap-nd-top"
                  :style="{ background: agentColor(node.tag || node.id) }"
                ></div>
                <div class="ap-nd-head">
                  <span
                    class="ap-nd-num"
                    :style="{
                      background: agentColor(node.tag || node.id) + '22',
                      color: agentColor(node.tag || node.id),
                    }"
                    >{{ i + 1 }}</span
                  >
                  <span class="ap-nd-name"
                    >{{ agentIcon(node.tag || node.id) }}
                    {{ agentLabel(node.tag || node.id) }}</span
                  >
                  <span
                    v-if="flowEditing && canToggle(node.id)"
                    class="ap-nd-toggle"
                    :style="{ color: node.enabled ? '#ff3b30' : '#34c759' }"
                    @click.stop="toggleAgent(node.id)"
                    >{{ node.enabled ? '✕' : '✓' }}</span
                  >
                </div>
                <div v-if="node.enabled" class="ap-nd-desc">
                  {{ agentDesc(node.tag || node.id) }}
                </div>
                <div v-else class="ap-nd-off-lbl">{{ t('not_needed_in_scene') }}</div>
                <div v-if="scopeViolationOf(node)" class="ap-nd-violation">
                  {{ scopeViolationHint(scopeViolationOf(node)) }}
                </div>
                <div class="ap-nd-hr"></div>
                <div class="ap-nd-skillbox">
                  <div class="ap-nd-lbl">{{ t('label_skill') }}</div>
                  <!-- enabled=null（老场景未存该字段）视为启用，与 off 判定一致 -->
                  <template v-if="node.enabled !== false">
                    <div
                      v-if="flowEditing"
                      class="dbv-skillpick"
                      @click.stop="openSkillDropdown(node.id, $event)"
                    >
                      {{ node.skill_display_name || node.skill || t('select_skill') }}
                    </div>
                    <span v-else-if="nodeSkill(node)" class="dbv-skillname">{{
                      nodeSkill(node)
                    }}</span>
                    <span v-else class="ap-nd-none">none</span>
                  </template>
                  <span v-else class="ap-nd-none">{{
                    flowEditing ? t('agent_disabled') : 'none'
                  }}</span>
                </div>
                <div class="ap-nd-engbox">
                  <div class="ap-nd-lbl">{{ t('label_engine') }}</div>
                  <template v-if="node.enabled !== false">
                    <div v-html="engineBadgeHtml(node)"></div>
                    <button
                      v-if="flowEditing"
                      class="ap-nd-engbtn"
                      @click.stop="openEngineModal(node.id)"
                    >
                      {{ t('select_engine') }}
                    </button>
                  </template>
                  <span v-else class="ap-nd-none">none</span>
                </div>
              </div>
              <span v-if="i < (flowEditing ? editNodes : viewNodes).length - 1" class="ap-arr"
                >→</span
              >
            </template>
            <div
              v-if="!(flowEditing ? editNodes : viewNodes).length"
              style="padding: 20px; text-align: center; color: var(--t3)"
            >
              无 Agent 节点
            </div>
          </div>
        </div>
        <!-- PR 订阅 -->
        <div class="det-card-wrap dbv-prsub">
          <div style="flex: 1; min-width: 0">
            <div class="dbv-prsub-t">PR 订阅</div>
            <div class="dbv-prsub-d">
              开启后，QA Agent 将自动审查所有目标分支匹配本场景工作分支的 Pull
              Request，审查结果以评论形式回帖到对应 PR
            </div>
          </div>
          <span class="dbv-switch" :class="{ on: detailPrSub }" @click="togglePrSubscription()"
            ><span class="dbv-knob"></span
          ></span>
        </div>
        <!-- 高级选项 -->
        <div class="det-card-wrap">
          <div class="dbv-sechead">
            <h2>{{ t('advanced_options') }}</h2>
            <button class="ap-btn" @click="saveAdvancedOptions()">{{ t('save') }}</button>
          </div>
          <div class="adv-row">
            <span class="adv-label">{{ t('work_branch') }}</span>
            <select v-model="advWorkBranch" class="adv-select">
              <option v-if="!branches.length" :value="detailWorkBranch || 'main'">
                {{ detailWorkBranch || 'main' }}
              </option>
              <option
                v-for="br in branches"
                :key="br.name"
                :value="br.name"
                :selected="br.name === detailWorkBranch"
              >
                {{ br.name }}{{ br.protected ? ' 🔒' : '' }}
              </option>
            </select>
            <button class="ap-btn" @click="newBranchOpen = !newBranchOpen">
              {{ t('create_branch') }}
            </button>
          </div>
          <div v-if="newBranchOpen" class="dbv-newbranch">
            <input v-model="newBranchName" :placeholder="t('branch_name')" class="dbv-nb-input" />
            <select v-model="newBranchRef" class="dbv-nb-sel">
              <option v-if="!branches.length" value="main">main</option>
              <option v-for="br in branches" :key="br.name" :value="br.name">{{ br.name }}</option>
            </select>
            <button class="ap-btn" @click="createNewBranch()">{{ t('create') }}</button>
            <button class="ap-btn" @click="newBranchOpen = false">{{ t('cancel') }}</button>
          </div>
          <div class="dbv-advsec">
            <div class="dbv-advsec-t">{{ t('custom_config') }}</div>
            <div class="dbv-cflist">
              <div v-for="(f, i) in advFields" :key="i" class="dbv-cfrow">
                <template v-if="!f.preset">
                  <input
                    v-model="f.name"
                    class="dbv-cfkey"
                    placeholder="name"
                    :class="{ 'cf-invalid': nameError(f.name) }"
                  />
                </template>
                <template v-else>
                  <span class="dbv-cfkey-static">{{ f.name }}</span>
                </template>
                <input
                  v-model="f.value"
                  :type="f.show ? 'text' : 'password'"
                  class="dbv-cfval"
                  :placeholder="f.keepValue ? '********' : 'value'"
                  @input="onFieldInput(f)"
                />
                <button class="dbv-cfeye" @click="f.keepValue ? openReveal(f) : (f.show = !f.show)">
                  {{ f.show ? '🙈' : '👁' }}
                </button>
                <span v-if="!f.preset" class="dbv-cfscope">
                  <label
                    v-for="sc in ['all', 'vod', 'issue', 'coding', 'qa', 'sre']"
                    :key="sc"
                    class="dbv-scope-lbl"
                  >
                    <input
                      v-model="f.scope"
                      type="checkbox"
                      :value="sc"
                      @change="onScopeToggle(f, sc)"
                    />{{ sc }}
                  </label>
                </span>
                <button v-if="!f.preset" class="dbv-cfdel" @click="advFields.splice(i, 1)">
                  ×
                </button>
              </div>
            </div>
            <button
              class="ap-btn dbv-addbtn-sm"
              @click="advFields.push({ name: '', value: '', show: false, scope: ['all'] })"
            >
              + {{ t('add_config') }}
            </button>
          </div>
          <div class="dbv-advsec dbv-advsep">
            <div class="dbv-advsec-t">关联仓库</div>
            <div class="dbv-advsec-d">
              配置关联仓库后，一次需求开发可同时操作多个仓库，开发完成后向每个有改动的仓库分别提交
              PR
            </div>
            <div class="dbv-cflist">
              <div v-for="(lr, i) in linkedRepos" :key="i" class="dbv-lrrow">
                <select v-model="lr.repoKey" class="dbv-lrrepo" @change="loadLinkedBranches(i)">
                  <option value="">选择仓库</option>
                  <option
                    v-for="r in linkedRepoOptions"
                    :key="r.repo_key || r.full_name"
                    :value="r.repo_key || r.full_name"
                  >
                    {{ r.full_name }}
                  </option>
                </select>
                <select v-model="lr.branch" class="dbv-lrbranch">
                  <option value="">分支（默认 main）</option>
                  <option v-for="br in lr.branches" :key="br.name" :value="br.name">
                    {{ br.name }}{{ br.protected ? ' 🔒' : '' }}
                  </option>
                </select>
                <button class="dbv-cfdel" @click="linkedRepos.splice(i, 1)">×</button>
              </div>
            </div>
            <button class="ap-btn dbv-addbtn-sm" @click="addLinkedRepo()">+ 添加关联仓库</button>
          </div>
        </div>
        <!-- 相关流水线 -->
        <div class="det-card-wrap">
          <div class="dbv-sechead">
            <h2>{{ t('related_pipeline') }}</h2>
            <div style="display: flex; gap: 8px">
              <div class="pl-stat">
                <div class="v" style="color: #34c759">{{ scenePipeActive }}</div>
                <div class="l">{{ t('stat_active') }}</div>
              </div>
              <div class="pl-stat">
                <div class="v" style="color: #ff3b30">{{ scenePipeFailed }}</div>
                <div class="l">{{ t('stat_failed') }}</div>
              </div>
              <div class="pl-stat">
                <div class="v" style="color: #0071e3">{{ scenePipeTotal }}</div>
                <div class="l">{{ t('stat_total') }}</div>
              </div>
            </div>
          </div>
          <div
            v-if="pipesLoading"
            style="text-align: center; padding: 24px; color: var(--t3); font-size: 13px"
          >
            {{ t('loading') }}
          </div>
          <div
            v-else-if="!scenePipes.length"
            style="text-align: center; padding: 24px; color: var(--t3); font-size: 13px"
          >
            {{ t('no_issues') }}
          </div>
          <template v-else>
            <table class="pl-table">
              <tr>
                <th>{{ t('th_issue') }}</th>
                <th>{{ t('th_title') }}</th>
                <th>{{ t('th_duration') }}</th>
                <th>{{ t('th_agents') }}</th>
                <th>{{ t('th_stage') }}</th>
                <th>{{ t('th_owner') }}</th>
              </tr>
              <tr
                v-for="p in scenePipePage"
                :key="p.issue_id"
                class="dbv-piperow"
                @click="jumpFromScenePipeline(p)"
              >
                <td>
                  <b>{{ p.issue_id || '?' }}</b>
                  <span
                    v-if="p.executing"
                    class="tg tg-c"
                    style="color: var(--on-accent); background: var(--g-fill)"
                    >⚡</span
                  >
                </td>
                <td>
                  <a
                    :href="pipeIssueUrl(p)"
                    target="_blank"
                    rel="noopener"
                    style="color: var(--a); text-decoration: underline; font-size: 11px"
                    >{{ (p.title || '—').substring(0, 40) }}</a
                  >
                </td>
                <td>{{ pipeDuration(p) }}</td>
                <td>
                  <span
                    v-for="(at, j) in ['vod', 'issue', 'coding', 'qa', 'sre']"
                    :key="at"
                    :class="['tg', ['tg-v', 'tg-i', 'tg-c', 'tg-q', 'tg-s'][j]]"
                    :style="pipeAgentStyle(p, j)"
                    @click.stop="pipeAgentClick(p, j)"
                    >{{ at.charAt(0).toUpperCase() }}</span
                  >
                </td>
                <td>
                  <span
                    :style="{
                      fontSize: '11px',
                      color: p.stage === 'done' ? '#34c759' : 'var(--t3)',
                    }"
                    >{{ stageLabel(p.stage) }}</span
                  >
                </td>
                <td>{{ p.current_owner || '—' }}</td>
              </tr>
            </table>
            <div v-if="pipeTotalPages > 1" class="dbv-pipepager">
              <span>{{ t('page_info', { cur: pipePage + 1, total: pipeTotalPages }) }}</span>
              <button
                class="bt bt-o bt-s"
                :disabled="pipePage <= 0"
                @click="loadScenePipePage(detail && detail.id, pipePage - 1)"
              >
                {{ t('prev_page') }}
              </button>
              <button
                class="bt bt-o bt-s"
                :disabled="pipePage >= pipeTotalPages - 1"
                @click="loadScenePipePage(detail && detail.id, pipePage + 1)"
              >
                {{ t('next_page') }}
              </button>
            </div>
          </template>
        </div>
      </template>
    </template>

    <!-- ═══ 场景中心主页 ═══ -->
    <template v-else>
      <!-- host banner -->
      <div v-if="hostBanner" :class="['dbv-hostbanner', { error: hostBanner.error }]">
        ⚠️ {{ hostBanner.text }}
        <button @click="repairHost()">{{ hostBanner.btn }}</button>
      </div>
      <!-- Hero -->
      <div class="ap-hero">
        <div class="ap-hero-row">
          <div class="ap-hero-icon">■</div>
          <div style="flex: 1">
            <div class="ap-hero-title">
              {{ dashOwner }}/{{ dashName }}
              <span class="dbv-rolepill">{{
                role === 'Maintainer' ? t('hero_maintainer') : t('hero_member')
              }}</span>
            </div>
            <div class="ap-hero-sub">
              {{ role === 'Maintainer' ? t('hero_maintainer') : t('hero_member') }} ·
              {{ t('webhook_ok') }}
            </div>
          </div>
          <div class="ap-stats">
            <div class="ap-stat">
              <div class="v" style="color: #34c759">{{ metrics.active_agents || 0 }}</div>
              <div class="l">{{ t('stat_active') }}</div>
            </div>
            <div class="ap-stat">
              <div class="v" style="color: #ff3b30">{{ metrics.failed_events || 0 }}</div>
              <div class="l">{{ t('stat_failed') }}</div>
            </div>
            <div class="ap-stat">
              <div class="v" style="color: #0071e3">{{ metrics.total_issues || 0 }}</div>
              <div class="l">{{ t('stat_total') }}</div>
            </div>
          </div>
        </div>
      </div>
      <!-- 场景中心介绍 -->
      <div class="apple-card dbv-marketcard">
        <div class="dbv-marketrow">
          <div style="font-size: 32px; flex-shrink: 0">🏪</div>
          <div style="flex: 1; min-width: 0">
            <div class="dbv-market-t">{{ t('market_intro_title') }}</div>
            <div class="dbv-market-d">{{ t('market_intro_desc') }}</div>
          </div>
          <button class="ap-btn" @click="G.app.nav('scene-market')">
            {{ t('goto_market') }} →
          </button>
        </div>
      </div>
      <!-- 场景列表 -->
      <template v-if="scenes.length">
        <div class="apple-card dbv-scenecard">
          <div class="cd-h dbv-scenehead">
            <h2>
              {{ t('scene_center_title') }}
              <span class="dbv-scene-count">{{ sceneTotal }}</span>
            </h2>
            <div class="dbv-scenehead-right">
              <div class="dbv-scenesearch">
                <input
                  v-model="sceneQuery"
                  class="dbv-scenesearch-input"
                  :placeholder="t('scene_search_placeholder')"
                  @input="onSceneSearchInput"
                  @keyup.enter="commitSceneSearch()"
                />
                <button v-if="sceneQuery" class="dbv-scenesearch-clear" @click="clearSceneSearch()">
                  ✕
                </button>
              </div>
              <button class="ap-btn" @click="openSceneRecommendModal()">
                {{ t('scene_recommend_rules') }}
              </button>
              <button class="ap-btn" @click="enterCreateMode()">＋ {{ t('add_scene') }}</button>
            </div>
          </div>
          <div class="scn-list">
            <div
              v-for="(s, idx) in scenes"
              :key="s.id"
              class="scn-row"
              @click="G.app.nav('scene-detail-local', s.id)"
            >
              <div
                class="scn-row-icon"
                :style="{ background: morandiColors[idx % morandiColors.length] }"
              >
                {{ sceneIcon(s) }}
              </div>
              <div class="scn-row-mid">
                <div style="display: flex; align-items: center; gap: 8px">
                  <span class="scn-row-name">{{ s.display_name || s.name }}</span>
                  <span v-if="s.created_by" class="dbv-scene-owner">@{{ s.created_by }}</span>
                </div>
                <div class="dbv-scene-descrow">
                  <span class="scn-row-desc" :title="s.description">{{
                    s.description || '—'
                  }}</span>
                </div>
              </div>
              <div class="scn-row-chain">
                <template v-for="(n, i) in sceneChain(s)" :key="i">
                  <span v-if="i > 0" class="scn-arr">→</span>
                  <span class="scn-tag" :style="chainTagStyle(n)">{{ chainTagLabel(n) }}</span>
                </template>
              </div>
              <div class="scn-row-right">
                <span v-if="s.pr_subscription_enabled" class="dbv-pr-badge">已开启 PR 订阅</span>
                <span class="scn-branch-tag">🌿 {{ s.work_branch || 'main' }}</span>
              </div>
            </div>
          </div>
          <!-- Issue #501：服务端分页 -->
          <div v-if="sceneTotalPages > 1" class="dbv-scenepager">
            <span>{{ t('page_info', { cur: scenePage, total: sceneTotalPages }) }}</span>
            <button
              class="bt bt-o bt-s"
              :disabled="scenePage <= 1"
              @click="sceneGotoPage(scenePage - 1)"
            >
              {{ t('prev_page') }}
            </button>
            <button
              class="bt bt-o bt-s"
              :disabled="scenePage >= sceneTotalPages"
              @click="sceneGotoPage(scenePage + 1)"
            >
              {{ t('next_page') }}
            </button>
          </div>
        </div>
      </template>
      <!-- 空场景 -->
      <template v-else>
        <div v-if="sceneQuery.trim()" class="dbv-emptycard">
          <div class="dbv-empty-ic">🔍</div>
          <div class="dbv-empty-t">{{ t('scene_search_no_match') }}</div>
          <div class="dbv-empty-d">{{ sceneQuery }}</div>
          <button class="ap-btn" @click="clearSceneSearch()">{{ t('scene_search_clear') }}</button>
        </div>
        <template v-else>
          <div class="apple-card dbv-marketcard">
            <div class="dbv-marketrow">
              <div style="font-size: 32px; flex-shrink: 0">🏪</div>
              <div style="flex: 1; min-width: 0">
                <div class="dbv-market-t">{{ t('market_intro_title') }}</div>
                <div class="dbv-market-d">{{ t('market_intro_desc') }}</div>
              </div>
              <button class="ap-btn" @click="G.app.nav('scene-market')">
                {{ t('goto_market') }} →
              </button>
            </div>
          </div>
          <div class="dbv-emptycard">
            <div class="dbv-empty-ic">📋</div>
            <div class="dbv-empty-t">{{ t('no_scene_title') }}</div>
            <div class="dbv-empty-d">{{ t('no_scene_desc') }}</div>
            <button class="ap-btn" @click="enterCreateMode()">➕ {{ t('no_scene_create') }}</button>
          </div>
        </template>
      </template>
    </template>

    <!-- ═══ 技能下拉面板 ═══ -->
    <Teleport to="body">
      <div
        v-if="skillPanel.open"
        class="dbv-skillpanel"
        :style="{ left: skillPanel.x + 'px', top: skillPanel.y + 'px', width: skillPanel.w + 'px' }"
      >
        <input
          v-model="skillPanel.q"
          class="dbv-skillsearch"
          :placeholder="t('skill_search_placeholder')"
        />
        <div class="dbv-skilllist">
          <div class="dbv-skillopt clear" @click="pickSkill('')">✕ {{ t('skill_clear') }}</div>
          <template v-if="skillGroups.length">
            <template v-for="g in skillGroups" :key="g.key">
              <div class="dbv-skillgroup">{{ g.label }} ({{ g.items.length }})</div>
              <div
                v-for="s in g.items"
                :key="skillKeyOf(s)"
                :class="['dbv-skillopt', { sel: skillIsSelected(s) }]"
                :title="s.description || ''"
                @click="pickSkill(s.skill_name, skillKeyOf(s))"
              >
                <span v-if="skillIsSelected(s)" class="dbv-skillcheck">✓</span>
                <span class="dbv-skillname-t">{{ s.display_name || s.skill_name }}</span>
              </div>
            </template>
          </template>
          <div v-else class="dbv-skillnomatch">{{ t('skill_no_match') }}</div>
        </div>
      </div>
    </Teleport>

    <!-- ═══ 引擎选择弹窗 ═══ -->
    <div v-if="engineModal.open" class="rtv-modal-ov" @click.self="engineModal.open = false">
      <div class="dbv-engmodal">
        <h3>{{ t('select_engine') }} — {{ agentLabel(engineModal.agentType) }}</h3>
        <div class="dbv-eng-sub">
          {{
            t('engines_available', { count: (agentEngines[engineModal.agentType] || []).length })
          }}
        </div>
        <div v-if="engineDetachHint" v-html="engineDetachHint"></div>
        <!-- 筛选 -->
        <div class="dbv-engfilters">
          <span
            v-for="ty in engModalAvailTypes"
            :key="'et' + ty"
            :class="{ on: engineModal.filterType === ty }"
            @click="engineModal.filterType = engineModal.filterType === ty ? '' : ty"
            >{{ ty }}</span
          >
          <span v-if="engModalAvailTypes.length" class="dbv-engsep"></span>
          <span
            v-for="s in engModalAvailScopes"
            :key="'es' + s"
            :class="{ on: engineModal.filterScope === s }"
            @click="engineModal.filterScope = engineModal.filterScope === s ? '' : s"
            >{{ SCOPE_LABELS_ALL[s] || s }}</span
          >
          <span style="flex: 1"></span>
          <span class="dbv-engclear" @click="engCurIds = []">全不选</span>
        </div>
        <input v-model="engineModal.q" class="dbv-engsearch" placeholder="搜索引擎名称/标签..." />
        <div class="dbv-englist">
          <div
            v-if="!engModalFiltered.length"
            style="padding: 20px 0; text-align: center; color: var(--t3); font-size: 13px"
          >
            {{
              engineModal.agentType === 'sre'
                ? t('sre_engine_empty_hint')
                : t('no_engine_broadcast')
            }}
          </div>
          <template v-for="s in SCOPE_ORDER2" :key="'eg' + s">
            <template v-if="engModalByScope[s]">
              <div class="dbv-enggroup">
                {{ SCOPE_LABELS_ALL[s] }} ({{ engModalByScope[s].length }})<span
                  v-if="s === 'user'"
                  class="dbv-engpersonalhint"
                  >{{ t('engine_personal_scope_hint') }}</span
                >
              </div>
              <label
                v-for="e in engModalByScope[s]"
                :key="e.id"
                :class="['dbv-engitem', { on: engCurIds.includes(e.id) }]"
              >
                <input
                  type="checkbox"
                  :checked="engCurIds.includes(e.id)"
                  @change="toggleEngineId(e.id)"
                />
                <div class="dbv-engbody">
                  <div class="dbv-engname">
                    {{ e.alias || engineDisplayName(e) }}
                    <span v-if="e.alias" class="dbv-engalias">({{ engineDisplayName(e) }})</span>
                    <span
                      v-for="tg in e.tags || []"
                      :key="tg"
                      class="dbv-engtag"
                      :style="{ background: ENG_TAG_COLORS[tg] || '#999' }"
                      >{{ tg === 'all' ? 'All' : tg.charAt(0).toUpperCase() + tg.slice(1) }}</span
                    >
                    <span
                      v-for="st in e.scene_tags || []"
                      :key="st"
                      class="dbv-engtag"
                      :style="{ background: ENG_SCENE_COLORS[st] || '#10b981' }"
                      >{{ engSceneDisplayName(st) }}</span
                    >
                    <span
                      class="dbv-engtag"
                      :style="{ background: SCOPE_COLORS_ALL[e.scope] || '#999' }"
                      >{{ SCOPE_LABELS_ALL[e.scope] || e.scope }}</span
                    >
                  </div>
                  <div class="dbv-engmeta">
                    {{ e.device || '' }}{{ e.cred_name ? ' · ' + e.cred_name : '' }}
                  </div>
                </div>
                <span class="dbv-engstatus" :style="{ color: engStatusColor(e.status) }">{{
                  engStatusLabel(e.status)
                }}</span>
              </label>
            </template>
          </template>
        </div>
        <div class="dbv-engfoot">
          <button class="ap-btn" @click="engineModal.open = false">{{ t('cancel') }}</button>
          <button class="ap-btn" @click="confirmEnginePick()">{{ t('confirm') }}</button>
        </div>
      </div>
    </div>

    <!-- ═══ 复制场景弹窗 ═══ -->
    <div v-if="copyDlg.open" class="rtv-modal-ov" @click.self="copyDlg.open = false">
      <div class="dbv-copymodal">
        <h3>{{ t('confirm_copy_scene') }}</h3>
        <p class="dbv-copy-hint">{{ t('copy_scene_hint') }}</p>
        <label class="dbv-copy-lbl">{{ t('copy_scene_name_label') }}</label>
        <input v-model="copyDlg.name" maxlength="40" class="dbv-copy-input" />
        <div class="dbv-copy-btns">
          <button class="ap-btn" @click="copyDlg.open = false">{{ t('cancel') }}</button>
          <button class="ap-btn" @click="doCopyScene()">{{ t('copy_scene') }}</button>
        </div>
      </div>
    </div>

    <!-- ═══ 删除场景确认 ═══ -->
    <div v-if="deleteDlg.open" class="rtv-modal-ov" @click.self="deleteDlg.open = false">
      <div class="dbv-copymodal">
        <h3>{{ t('confirm_delete_scene') }}</h3>
        <p class="dbv-copy-hint">{{ deleteDlg.name }}</p>
        <div class="dbv-copy-btns">
          <button class="ap-btn" @click="deleteDlg.open = false">{{ t('cancel') }}</button>
          <button class="ap-btn ap-btn-d" @click="doDeleteScene()">{{ t('delete') }}</button>
        </div>
      </div>
    </div>

    <!-- ═══ 密钥明文取回（#885，密码二次确认） ═══ -->
    <div v-if="revealDlg.open" class="rtv-modal-ov" @click.self="closeReveal()">
      <div class="dbv-copymodal">
        <h3>查看明文：{{ revealDlg.name }}</h3>
        <p class="dbv-copy-hint">查看密钥明文需重新输入 Grape 密码确认，读取动作将记入审计日志。</p>
        <input
          v-model="revealDlg.password"
          type="password"
          class="dbv-copy-input"
          placeholder="Grape 密码"
          @keyup.enter="submitReveal()"
        />
        <div v-if="revealDlg.error" class="dbv-reveal-err">{{ revealDlg.error }}</div>
        <div class="dbv-copy-btns">
          <button class="ap-btn" @click="closeReveal()">{{ t('cancel') }}</button>
          <button class="ap-btn" :disabled="revealDlg.busy" @click="submitReveal()">
            {{ t('confirm') }}
          </button>
        </div>
      </div>
    </div>

    <!-- ═══ 智能推荐规则弹窗 ═══ -->
    <div v-if="recDlg.open" class="rtv-modal-ov" @click.self="recDlg.open = false">
      <div class="dbv-recmodal">
        <div class="dbv-rec-head">
          <h2>{{ t('scene_recommend_rules') }}</h2>
          <button
            class="ap-btn"
            style="padding: 2px 8px; font-size: 16px"
            @click="recDlg.open = false"
          >
            ×
          </button>
        </div>
        <div class="dbv-rec-sec">
          <div class="dbv-rec-t">{{ t('recommend_fallback') }}</div>
          <select v-model="recDlg.fallback" class="dbv-rec-sel">
            <option v-if="!recDlg.scenes.length" value="">{{ t('loading') }}</option>
            <option v-for="s in recDlg.scenes" :key="s.id" :value="s.id">
              {{ s.display_name || s.name || s.id }}
            </option>
          </select>
          <div class="dbv-rec-hint">{{ t('recommend_fallback_hint') }}</div>
        </div>
        <div class="dbv-rec-sec dbv-rec-sep">
          <div class="dbv-rec-t">{{ t('recommend_rules') }}</div>
          <div class="dbv-rec-rules">
            <div v-for="(r, i) in recDlg.rules" :key="i" class="dbv-rec-row">
              <select v-model="r.scene_id" class="dbv-rec-rowsel">
                <option v-for="s in recDlg.scenes" :key="s.id" :value="s.id">
                  {{ s.display_name || s.name || s.id }}
                </option>
              </select>
              <input
                v-model="r.condition"
                :placeholder="t('recommend_condition_value')"
                class="dbv-rec-rowinput"
              />
              <button
                class="ap-btn ap-btn-d"
                style="padding: 4px 8px; font-size: 14px"
                @click="recDlg.rules.splice(i, 1)"
              >
                ×
              </button>
            </div>
          </div>
          <button
            class="ap-btn dbv-addbtn-sm"
            :disabled="!recDlg.loaded"
            @click="recDlg.rules.push({ scene_id: '', condition: '' })"
          >
            + {{ t('recommend_add_rule') }}
          </button>
          <div class="dbv-rec-hint">{{ t('recommend_rules_hint') }}</div>
        </div>
        <div class="dbv-copy-btns" style="margin-top: 20px">
          <button class="ap-btn" @click="recDlg.open = false">{{ t('cancel') }}</button>
          <button class="ap-btn" :disabled="!recDlg.loaded" @click="saveSceneRecommend()">
            {{ t('save') }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'

const G = globalThis

// ═══ 常量 ═══
const AGENT_META = {
  vod: { icon: '🔍', label: 'VOD', color: '#ff2d92' },
  issue: { icon: '📋', label: 'Issue', color: '#ff3b30' },
  coding: { icon: '💻', label: 'Coding', color: '#0071e3' },
  qa: { icon: '🧪', label: 'QA', color: '#af52de' },
  sre: { icon: '⚙️', label: 'SRE', color: '#ff9f0a' },
  chat: { icon: '💬', label: 'Chat', color: '#34c759' },
}
const SCENE_ICONS = {
  default: '⚙',
  harmony: '🍃',
  iac: '☁',
  'cloud-native': '☁',
  'skill-dev': '🔧',
  'api-skill-dev': '🔧',
  'agent-dev': '🤖',
  'blog-publish': '📝',
}
const CUSTOM_ICONS = [
  '📦',
  '🚀',
  '🎨',
  '🔬',
  '🏗️',
  '🛠️',
  '📊',
  '🎮',
  '🌐',
  '💡',
  '🔧',
  '⚡',
  '🎯',
  '🌱',
  '🔥',
  '⭐',
]
const morandiColors = ['#8B9DB0', '#A8B8A0', '#C4A882', '#B0A0B8', '#9BB0C4', '#C49B9B']
const tagColors = {
  vod: '#B07A8B',
  issue: '#B8857A',
  coding: '#7A92B0',
  qa: '#9A85A8',
  sre: '#B89B6A',
}
const ENG_TAG_COLORS = {
  all: '#6366f1',
  vod: '#ec4899',
  issue: '#ef4444',
  coding: '#3b82f6',
  qa: '#a855f7',
  sre: '#f59e0b',
}
const SCOPE_COLORS_ALL = { global: '#6366f1', user: '#34c759', repo: '#f59e0b', local: 'var(--t3)' }
const SCOPE_LABELS_ALL = { global: '全局', user: '个人', repo: '仓库', local: '本机' }
const SCOPE_ORDER2 = ['user', 'repo', 'global', 'local']
const ENG_SCENE_COLORS = {
  default: '#10b981',
  'cloud-native': '#3b82f6',
  iac: '#f59e0b',
  'agent-dev': '#8b5cf6',
  'skill-dev': '#ec4899',
  'api-skill-dev': '#06b6d4',
  'blog-publish': '#ef4444',
}
const ALL_AGENTS = ['vod', 'issue', 'coding', 'qa', 'sre']

// ═══ props / 状态 ═══
const props = defineProps({
  initialData: { type: Object, default: null },
  initialDetailId: { type: String, default: '' },
})
const loaded = ref(false)
const loadError = ref('')
// 主页
const dashData = ref(null)
const scenes = ref([])
// Issue #501：场景列表分页/检索（页码与检索词以 window._sceneList* 为准，app.js loadDash 读取）
const sceneTotal = ref(0)
const scenePage = ref(1)
const sceneQuery = ref('')
let sceneSearchTimer = null
const scenePageSize = () => G.window._sceneListPageSize || 20
const sceneTotalPages = computed(() =>
  Math.max(1, Math.ceil((sceneTotal.value || 0) / scenePageSize()))
)
function onSceneSearchInput() {
  clearTimeout(sceneSearchTimer)
  sceneSearchTimer = setTimeout(commitSceneSearch, 300)
}
function commitSceneSearch() {
  clearTimeout(sceneSearchTimer)
  const q = (sceneQuery.value || '').trim()
  if (q === (G.window._sceneListQ || '')) return
  G.window._sceneListQ = q
  G.window._sceneListPage = 1
  reloadSceneList()
}
function clearSceneSearch() {
  sceneQuery.value = ''
  commitSceneSearch()
}
function sceneGotoPage(p) {
  if (p < 1 || p > sceneTotalPages.value) return
  G.window._sceneListPage = p
  reloadSceneList()
}
function reloadSceneList() {
  // 与 app.js 的局部重载契约对接：切走页面时不得把用户拉回 dash
  if (typeof G.window._sceneReload === 'function' && G.window._currentView === 'dash') {
    G.window._sceneReload()
  } else if (G.window._currentView === 'dash') {
    G.app.nav('dash')
  }
}
const scenePipelines = ref({})
const dashOwner = ref('')
const dashName = ref('')
const role = ref('Member')
const metrics = ref({})
// 模式：'list' | 'create' | 'detail'
const mode = ref('list')
// 详情
const detail = ref(null)
const detailError = ref('')
const viewNodes = ref([])
const agents = ref([])
const agentEngines = ref({})
const pipesLoading = ref(false)
const scenePipes = ref([])
const pipePage = ref(0)
const detailPrSub = ref(false)
// 编辑/创建共用
const editNodes = ref([])
const flowEditing = ref(false)
// 创建
const createName = ref('')
const createDesc = ref('')
const createIcon = ref('📦')
const createPrSub = ref(false)
const iconPickerOpen = ref(false)
// 内联编辑
const inlineEdit = ref(false)
const inlineEditDesc = ref(false)
const inlineName = ref('')
const inlineDesc = ref('')
// 高级选项
const branches = ref([])
const advWorkBranch = ref('main')
const detailWorkBranch = ref('')
const newBranchOpen = ref(false)
const newBranchName = ref('')
const newBranchRef = ref('main')
const advFields = ref([])
const linkedRepos = ref([])
const linkedRepoOptions = ref([])
// 技能下拉
const skillPanel = reactive({
  open: false,
  agentType: '',
  current: '',
  currentKey: '',
  nameCounts: {},
  q: '',
  x: 0,
  y: 0,
  w: 240,
  skills: [],
})
const skillPickCache = {}
// Issue #978：下拉选择以唯一键（skill_id 优先，回退 scope:skill_name）为标识，
// 官方/仓库/个人三档可同名，裸 skill_name 会互相覆盖显示名与勾选态
function skillKeyOf(s) {
  return s.skill_id || (s.scope || 'official') + ':' + s.skill_name
}
function skillIsSelected(s) {
  if (skillPanel.currentKey && skillKeyOf(s) === skillPanel.currentKey) return true
  return (
    !!skillPanel.current &&
    skillPanel.current === s.skill_name &&
    (skillPanel.nameCounts[s.skill_name] || 0) === 1
  )
}
// 引擎弹窗
const engineModal = reactive({ open: false, agentType: '', q: '', filterType: '', filterScope: '' })
const engCurIds = ref([])
const engineDetachHint = ref('')
// 场景标签缓存（显示名）
const sceneTagsCache = ref(null)
// 复制/删除/推荐弹窗
const copyDlg = reactive({ open: false, name: '' })
const deleteDlg = reactive({ open: false, id: '', name: '' })
const recDlg = reactive({ open: false, fallback: '', scenes: [], rules: [], loaded: false })
// Issue #885: 密钥字段明文按需取回（密码二次确认）
const revealDlg = reactive({
  open: false,
  field: null,
  name: '',
  password: '',
  busy: false,
  error: '',
})
// Issue #461: 场景级访问地址（详情页复制场景按钮前的地球仪，三态）
const deployUrl = ref('')
const deployUrlEdit = ref(false)
const deployUrlDraft = ref('')
let deployUrlPressTimer = null

// ═══ 工具 ═══
const API = () => G.API
const app = () => G.app
function t(key, vars) {
  const v = G.I18N.t('dashboard.' + key, vars)
  if (v && v !== 'dashboard.' + key) return v
  const pv = G.I18N.t('pipeline.' + key, vars)
  if (pv && pv !== 'pipeline.' + key) return pv
  const cv = G.I18N.t('common.' + key, vars)
  if (cv && cv !== 'common.' + key) return cv
  const map = {
    loading: '加载中...',
    save: '保存',
    cancel: '取消',
    confirm: '确认',
    delete: '删除',
    edit: '编辑',
    create: '创建',
    scene_center_title: '项目场景',
    execution_flow: '执行流程',
    advanced_options: '高级选项',
    stat_active: '活跃',
    stat_failed: '失败',
    stat_total: '总数',
    th_issue: 'Issue',
    th_title: '标题',
    th_duration: '耗时',
    th_agents: 'Agent',
    th_stage: '阶段',
    th_owner: '负责人',
    page_info: '第 {cur}/{total} 页',
    prev_page: '上一页',
    next_page: '下一页',
    related_pipeline: '相关流水线',
    no_issues: '暂无 Issue',
    work_branch: '工作分支',
    create_branch: '新建分支',
    branch_name: '分支名',
    custom_config: '自定义配置',
    add_config: '添加配置项',
    label_skill: '技能',
    label_engine: '引擎',
    select_skill: '选择技能',
    select_engine: '选择引擎',
    skill_clear: '清除选择',
    skill_no_match: '无匹配技能',
    skill_search_placeholder: '搜索技能名称/描述',
    not_needed_in_scene: '此场景下不需要',
    agent_disabled: '已停用',
    hero_maintainer: '管理员',
    hero_member: '成员',
    webhook_ok: 'Webhook 配置正常',
    market_intro_title: '探索场景中心',
    market_intro_desc:
      '场景中心提供官方场景模板，涵盖云原生、IaC、Agent 开发等多种场景，一键应用到你的项目',
    goto_market: '前往场景中心',
    no_scene_title: '暂无场景',
    no_scene_desc: '还没有配置任何场景',
    no_scene_create: '创建新场景',
    scene_search_placeholder: '搜索场景名称/描述',
    scene_search_no_match: '没有匹配的场景',
    scene_search_clear: '清除搜索',
    add_scene: '添加场景',
    scene_name_placeholder: '请输入场景名称',
    desc_optional: '描述（可选）',
    scene_recommend_rules: '智能推荐规则',
    copy_scene: '复制场景',
    confirm_copy_scene: '复制场景',
    copy_scene_hint: '将复制该场景的完整配置为可编辑副本，源场景不受影响。',
    copy_scene_name_label: '副本名称',
    confirm_delete_scene: '确认删除场景',
    recommend_fallback: '兜底场景',
    recommend_fallback_hint: '当任务无法精确匹配任何规则时，走此场景',
    recommend_rules: '场景功能',
    recommend_add_rule: '添加规则',
    recommend_rules_hint: 'key 从当前仓库已应用场景中选择，value 描述什么情况下推荐使用该场景',
    recommend_condition_value: '推荐条件',
    recommend_fallback_required: '兜底场景不能为空',
    recommend_rule_dup: '同一场景不能有多条规则',
    sre_engine_empty_hint: '暂无可用引擎',
    no_engine_broadcast: '暂无可用引擎，将使用广播模式',
    engines_available: '已注册引擎: {count}',
    engine_personal_scope_hint: '个人级引擎仅创建者可用',
    online: '在线',
  }
  let out = map[key] || key
  if (vars)
    Object.keys(vars).forEach((k) => {
      out = String(out).replace('{' + k + '}', vars[k])
    })
  return out
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}
// Issue #1310: 引擎显示名统一「引擎名（凭据名）」，worker-name.js 提供唯一实现
function engineDisplayName(e) {
  const fn = G.window.engineDisplayName
  if (typeof fn === 'function') return fn(e)
  const en = String(
    (e && (e.display_name || e.engine_name || e.name || e.instance_name)) || ''
  ).trim()
  const wn = String((e && e.worker_name) || '').trim() || '未命名设备'
  return en ? en + '（' + wn + '）' : wn
}
function currentRepo() {
  return app().currentRepo ? app().currentRepo() : null
}
function stripRepoPrefix(rk) {
  return String(rk || '').replace(/^(gitcode|github):/, '')
}
// Issue #393fc76e9：分支接口显式携带平台（repo_key），避免同名仓被 user_repos 的
// github: 记录带偏（托管仓被误判 → 分支只剩 main）。仅在当前仓库与查询 owner/repo
// 一致时携带，跨仓页面不会注入错误平台。
function branchQS(owner, name) {
  try {
    const rk = (currentRepo() || {}).repoKey || ''
    if (rk && rk.indexOf(':') > 0) {
      const bare = stripRepoPrefix(rk)
      if (!owner || !name || bare === owner + '/' + name) {
        return '?repo_key=' + encodeURIComponent(rk)
      }
    }
  } catch (e) {
    /* ignore */
  }
  return ''
}
function resolveOwnerName(data) {
  let owner = ''
  let name = ''
  try {
    const cr = currentRepo()
    if (cr) {
      owner = cr.owner
      name = cr.name
    }
  } catch (e) {
    /* ignore */
  }
  if (!owner || !name) {
    const parts = stripRepoPrefix((data || {}).repo || '').split('/')
    owner = parts[0] || ''
    name = parts[1] || ''
  }
  return [owner, name]
}
function agentColor(tag) {
  return (AGENT_META[tag] || {}).color || tagColors[tag] || '#0071e3'
}
function agentIcon(tag) {
  return (AGENT_META[tag] || {}).icon || '◈'
}
function agentLabel(tag) {
  return (AGENT_META[tag] || {}).label || tag
}
function agentDesc(tag) {
  return t('desc_' + tag)
}
function canToggle(tag) {
  return tag !== 'vod' && tag !== 'issue'
}
function sceneIcon(s) {
  return SCENE_ICONS[s.id] || SCENE_ICONS[s.name] || s.icon || (s.scope === 'official' ? '⚙' : '📦')
}
const detailIcon = computed(() => {
  const s = detail.value || {}
  return SCENE_ICONS[s.id] || SCENE_ICONS[s.name] || (s.scope === 'official' ? '⚙' : '📦')
})
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
    if (f.preset) continue
    const err = nameError(f.name)
    if (err) return { error: err }
  }
  return {
    fields: fields
      .filter((f) => (f.name || '').trim())
      .map((f) => {
        const sec = computeIsSecret(f.name, f.show === false)
        const out = {
          name: f.name.trim(),
          value: (f.value || '').trim(),
          is_secret: sec,
          scope: f.scope && f.scope.length ? f.scope : ['all'],
          preset: !!f.preset,
        }
        // Issue #885: 已配置未改动的密钥 → 服务端保留旧值，不回传明文
        if (sec && f.keepValue) out.keep_value = true
        return out
      }),
  }
}
function onScopeToggle(f, sc) {
  // all 与具体 agent 互斥
  if (sc === 'all' && f.scope.includes('all')) {
    f.scope = ['all']
  } else if (sc !== 'all') {
    const i = f.scope.indexOf('all')
    if (i >= 0) f.scope.splice(i, 1)
  }
}
// Issue #885: 用户改动密钥输入框 → 取消 keep_value 占位语义
function onFieldInput(f) {
  f.keepValue = false
}
function openReveal(f) {
  revealDlg.field = f
  revealDlg.name = f.name
  revealDlg.password = ''
  revealDlg.error = ''
  revealDlg.busy = false
  revealDlg.open = true
}
function closeReveal() {
  revealDlg.open = false
  revealDlg.field = null
  revealDlg.password = ''
}
function submitReveal() {
  const f = revealDlg.field
  if (!f) return
  const sceneId = detail.value ? detail.value.id : ''
  if (!sceneId) {
    revealDlg.error = '当前上下文不支持取回明文'
    return
  }
  revealDlg.busy = true
  revealDlg.error = ''
  API()
    .post(
      '/scenes/' +
        encodeURIComponent(sceneId) +
        '/advanced-options/reveal?repo=' +
        encodeURIComponent(dashOwner.value + '/' + dashName.value),
      { name: revealDlg.name, password: revealDlg.password }
    )
    .then(function (d) {
      f.value = (d && d.value) || ''
      f.show = true
      f.keepValue = false
      revealDlg.busy = false
      revealDlg.open = false
    })
    .catch(function (e) {
      revealDlg.busy = false
      const dd = (e && e.detail) || {}
      if (dd.need_set_password) {
        revealDlg.error = '请先在账户设置中设置登录密码后再取回明文'
        return
      }
      revealDlg.error = dd.message || dd.detail || (e && e.message) || '取回明文失败'
    })
}
// 场景标签显示名
function ensureSceneTags(cb) {
  if (sceneTagsCache.value) {
    if (cb) cb()
    return
  }
  API()
    .get('/runtime/scene-tags')
    .then(function (d) {
      sceneTagsCache.value = d.scenes || []
      if (cb) cb()
    })
    .catch(function () {
      sceneTagsCache.value = []
      if (cb) cb()
    })
}
function engSceneDisplayName(name) {
  if (sceneTagsCache.value) {
    const s = sceneTagsCache.value.find((x) => x.name === name)
    if (s) return s.display_name
  }
  return name
}

// ═══ 引擎三态徽章（Issue #249 等价：如实呈现绑定状态） ═══
function engineBadgeState(engineIds, agentType) {
  const ids = engineIds || []
  if (!ids.length) return { total: 0, offline: 0 }
  const ae = agentEngines.value || {}
  if (!ae[agentType] || !ae[agentType].length) {
    // SRE 专属：引擎行不存在（凭据吊销后被清理）= 绑定失效，全部计入 offline
    if (agentType === 'sre') return { total: ids.length, offline: ids.length }
    return { total: ids.length, offline: 0 }
  }
  const availMap = {}
  ae[agentType].forEach((e) => {
    if (e.status === 'online' || e.status === 'recently_lost') availMap[e.id] = true
  })
  const offline = ids.filter((id) => !availMap[id]).length
  return { total: ids.length, offline }
}
function engineBadgeHtml(node) {
  const tag = node.tag || node.id
  const ids = node.engine_runtime_ids || []
  const st = engineBadgeState(ids, tag)
  if (!st.total) {
    if (tag === 'sre') {
      return `<span class="dbv-engbadge gray">${esc(t('sre_engine_required'))}</span>`
    }
    return `<span class="dbv-engbadge green">${esc(t('broadcast') || '广播')}</span>`
  }
  if (st.offline > 0) {
    const label =
      st.offline < st.total
        ? t('engine_offline_partial', { total: st.total, offline: st.offline }) ||
          `${st.total} 台引擎 · ${st.offline} 离线`
        : t('engine_offline_all') || '绑定失效 · 全部离线'
    return `<span class="dbv-engbadge red">${esc(label)}</span>`
  }
  return `<span class="dbv-engbadge blue">${esc(t('engine_count', { count: st.total }))}</span>`
}

// ═══ 场景链（卡片上的技能链展示） ═══
function sceneChain(s) {
  const pipeline = scenePipelines.value[s.id] || {}
  return pipeline.nodes || []
}
function chainTagLabel(n) {
  const tag = n.tag || n.id || ''
  return (AGENT_META[tag] || {}).label || tag
}
function chainTagStyle(n) {
  const tag = n.tag || n.id || ''
  const c = tagColors[tag] || '#B0B0B0'
  return { background: c + '1A', color: c }
}
function nodeSkill(node) {
  const tag = node.tag || node.id
  const ag = agents.value.find((a) => a.agent_type === tag) || {}
  return node.skill_display_name || node.skill || ag.skill_display_name || ag.skill || ''
}

// ═══ host banner（托管自愈） ═══
const hostBanner = computed(() => {
  const hs = (dashData.value || {}).host_status || ''
  if (!hs) return null
  if (hs === 'bot_missing')
    return {
      error: true,
      text: '该项目 grape_bot 未成功加入项目，请手动添加 bot 至项目，或点击「重检修复」自动处理',
      btn: '重检修复',
    }
  if (hs === 'webhook_missing')
    return { error: false, text: '项目 webhook 缺失，Grape 无法接收仓库事件', btn: '补建 webhook' }
  return { error: false, text: '该项目托管流程未完成，Grape 部分功能不可用', btn: '重检修复' }
})
function repairHost() {
  let owner = ''
  let name = ''
  try {
    const cr = currentRepo()
    if (cr) {
      owner = cr.owner
      name = cr.name
    }
  } catch (e) {
    /* ignore */
  }
  if (!owner || !name) return
  API()
    .post('/repos/' + owner + '/' + name + '/repair-host', {})
    .then(function (d) {
      if (d && d.status === 'ok') {
        app().showToast('托管已修复')
        G.window.location.reload()
      } else {
        app().showToast((d && d.message) || '修复失败，请稍后手动重试')
      }
    })
    .catch(function (e) {
      app().showToast('修复请求失败：' + (e.detail?.message || e.message || '未知错误'))
    })
}

// ═══ 主入口：renderDashboard(data) ═══
function renderDash(data) {
  dashData.value = data
  scenes.value = data._scenes || []
  // Issue #501：分页总数（老后端无 total 时回退当前页条数）与页码/检索词回填
  sceneTotal.value = typeof data._scenesTotal === 'number' ? data._scenesTotal : scenes.value.length
  scenePage.value = G.window._sceneListPage || 1
  sceneQuery.value = G.window._sceneListQ || ''
  role.value = data.role || 'Member'
  metrics.value = data.metrics || {}
  const [owner, name] = resolveOwnerName(data)
  dashOwner.value = owner
  dashName.value = name
  // 同步 window 全局（app.js 编辑守卫引用 _dashboardEditing）
  G.window._dashboardData = data
  G.window._dashOwner = owner
  G.window._dashName = name
  mode.value = 'list'
  loaded.value = true
  loadError.value = ''
  // Issue #f5c6b25b9：进仓库后无引擎 → 引导添加设备（app.js 时序链）
  try {
    if (app()._checkEngineBanner) app()._checkEngineBanner()
  } catch (e) {
    /* ignore */
  }
  // 卡片技能链：app.js 的 with-pipeline 单一来源已带 _pipelines（Issue #501 A 方案），
  // 缺失时才回退单独拉取，避免同表数据双请求
  if (data._pipelines) scenePipelines.value = data._pipelines
  else loadScenePipelines(owner, name)
  // Issue #407: 创建模式自动恢复
  const autoEdit = getDashEditMode()
  if (autoEdit && autoEdit.mode === 'create' && owner && name) {
    clearDashEditMode()
    enterCreateMode()
  }
}
function loadScenePipelines(owner, name) {
  if (!owner || !name) return
  API()
    .get('/scenes/with-pipeline?repo=' + encodeURIComponent(owner + '/' + name))
    .then(function (d) {
      const m = {}
      ;(d.scenes || []).forEach(function (s) {
        m[s.id] = s.pipeline || {}
      })
      scenePipelines.value = m
    })
    .catch(function () {
      scenePipelines.value = {}
    })
}

// ═══ Issue #407: 草稿持久化（1小时过期） ═══
function draftKey(owner, name, mode, sceneId) {
  return 'dashDraft_' + owner + '/' + name + '_' + mode + (sceneId ? '_' + sceneId : '')
}
function saveDashDraft(owner, name, mode, sceneId, data) {
  try {
    if (data && typeof data === 'object' && data.ts === undefined) data.ts = Date.now()
    G.sessionStorage.setItem(draftKey(owner, name, mode, sceneId), JSON.stringify(data))
  } catch (e) {
    /* ignore */
  }
}
function restoreDashDraft(owner, name, mode, sceneId) {
  try {
    const raw = G.sessionStorage.getItem(draftKey(owner, name, mode, sceneId))
    if (!raw) return null
    const o = JSON.parse(raw)
    if (!o || typeof o !== 'object') {
      G.sessionStorage.removeItem(draftKey(owner, name, mode, sceneId))
      return null
    }
    if (o.ts && Date.now() - o.ts > 3600000) {
      G.sessionStorage.removeItem(draftKey(owner, name, mode, sceneId))
      return null
    }
    return o
  } catch (e) {
    return null
  }
}
function clearDashDraft(owner, name, mode, sceneId) {
  try {
    G.sessionStorage.removeItem(draftKey(owner, name, mode, sceneId))
  } catch (e) {
    /* ignore */
  }
}
function saveDashEditMode(mode, sceneId) {
  try {
    G.sessionStorage.setItem(
      'dashEditMode',
      JSON.stringify({ mode: mode, sceneId: sceneId || null, ts: Date.now() })
    )
  } catch (e) {
    /* ignore */
  }
}
function getDashEditMode() {
  try {
    const raw = G.sessionStorage.getItem('dashEditMode')
    if (!raw) return null
    const o = JSON.parse(raw)
    if (o.ts && Date.now() - o.ts > 3600000) {
      G.sessionStorage.removeItem('dashEditMode')
      return null
    }
    return o
  } catch (e) {
    return null
  }
}
function clearDashEditMode() {
  try {
    G.sessionStorage.removeItem('dashEditMode')
  } catch (e) {
    /* ignore */
  }
}
// 节点精简序列化（草稿用）
function serializeNodes(nodes) {
  return nodes.map(function (n) {
    return {
      id: n.id,
      tag: n.tag,
      skill: n.skill,
      skill_display_name: n.skill_display_name,
      mode: n.mode,
      next: n.next,
      handoff: n.handoff,
      skip: n.skip,
      enabled: n.enabled,
      engine_runtime_ids: n.engine_runtime_ids,
    }
  })
}
function applyDraftNodes(draft) {
  if (!draft || !draft.nodes) return false
  editNodes.value.forEach(function (en) {
    const dn = draft.nodes.find(function (n) {
      return n.id === en.id
    })
    if (dn) {
      en.enabled = dn.enabled
      en.skill = dn.skill || ''
      en.skill_display_name = dn.skill_display_name || ''
      en.engine_runtime_ids = dn.engine_runtime_ids || []
    }
  })
  return true
}
function saveEditDraft(sceneId) {
  saveDashDraft(dashOwner.value, dashName.value, 'edit', sceneId, {
    nodes: serializeNodes(editNodes.value),
  })
}
function saveCreateDraft() {
  saveDashDraft(dashOwner.value, dashName.value, 'create', null, {
    sceneName: createName.value,
    sceneDesc: createDesc.value,
    icon: createIcon.value,
    nodes: serializeNodes(editNodes.value),
    prSubEnabled: createPrSub.value,
  })
}

// ═══ 创建模式 ═══
function enterCreateMode() {
  const data = dashData.value || {}
  const [owner, name] = resolveOwnerName(data)
  dashOwner.value = owner
  dashName.value = name
  agents.value = data.agents || []
  agentEngines.value = data.agent_engines || {}
  // 默认全部启用，skill 为空
  editNodes.value = ALL_AGENTS.map((at) => ({
    id: at,
    tag: at,
    skill: '',
    skill_display_name: '',
    mode: 'emit',
    next: 'done',
    handoff: 'a2a',
    skip: [],
    enabled: true,
    engine_runtime_ids: [],
  }))
  createName.value = ''
  createDesc.value = ''
  createIcon.value = '📦'
  createPrSub.value = false
  iconPickerOpen.value = false
  flowEditing.value = false
  mode.value = 'create'
  G.window._dashboardEditing = true
  saveDashEditMode('create')
  // Issue #407: 恢复草稿
  const draft = restoreDashDraft(owner, name, 'create')
  if (draft) {
    if (draft.sceneName) createName.value = draft.sceneName
    if (draft.sceneDesc) createDesc.value = draft.sceneDesc
    if (draft.icon) createIcon.value = draft.icon
    if (draft.prSubEnabled) createPrSub.value = true
    applyDraftNodes(draft)
  }
  // Issue #307: 加载托管仓库列表
  API()
    .get('/repos/managed')
    .then(function (d) {
      linkedRepoOptions.value = (d && d.repos) || []
    })
    .catch(function () {
      linkedRepoOptions.value = []
    })
  // 分支列表
  loadBranches(owner, name)
  // 高级选项默认
  advFields.value = []
  linkedRepos.value = []
  advWorkBranch.value = 'main'
}
function exitCreateMode() {
  G.window._dashboardEditing = false
  clearDashEditMode()
  clearDashDraft(dashOwner.value, dashName.value, 'create')
  app().nav('dash')
}
function saveCreateScene() {
  if (!createName.value.trim()) {
    app().showToast(t('please_input_scene_name'))
    return
  }
  // 校验：启用的 agent 必须有技能
  const emptyAgents = editNodes.value.filter((n) => n.enabled && !n.skill)
  if (emptyAgents.length) {
    const names = emptyAgents.map((n) => agentLabel(n.id)).join('、')
    app().showToast(t('skill_empty_warning', { agents: names }))
    return
  }
  // SRE 必选引擎前端拦截
  const sreNoEngine = editNodes.value.find(
    (n) => n.id === 'sre' && n.enabled && !(n.engine_runtime_ids || []).length
  )
  if (sreNoEngine) {
    app().showToast(t('sre_engine_personal_only'))
    return
  }
  const nodesPayload = buildNodesPayload(editNodes.value)
  API()
    .post('/scenes?repo=' + encodeURIComponent(dashOwner.value + '/' + dashName.value), {
      name: createName.value.trim(),
      description: createDesc.value.trim(),
      scope: 'repo',
      nodes: nodesPayload,
      icon: createIcon.value,
      pr_subscription_enabled: createPrSub.value,
    })
    .then(function (r) {
      G.window._dashboardEditing = false
      clearDashEditMode()
      clearDashDraft(dashOwner.value, dashName.value, 'create')
      // 收集高级选项并保存
      const wb = advWorkBranch.value || 'main'
      const cf = collectFields(advFields.value)
      if (cf.error) {
        app().showToast(cf.error)
        return
      }
      const lr = collectLinkedRepos()
      const advPromise =
        wb || cf.fields.length || lr.length
          ? API().put(
              '/scenes/' +
                encodeURIComponent(r.scene_id) +
                '/advanced-options?repo=' +
                encodeURIComponent(dashOwner.value + '/' + dashName.value),
              { work_branch: wb, custom_fields: cf.fields, linked_repos: lr }
            )
          : Promise.resolve()
      advPromise
        .then(function () {
          app().showToast(t('scene_created'))
          app().nav('dash')
        })
        .catch(function () {
          app().showToast(t('scene_created'))
          app().nav('dash')
        })
    })
    .catch((e) => {
      // Issue #1564：技能 scope 冲突 → 按后端 400 violations[] 对节点标红，就地修正
      const viols = (e && e.detail && e.detail.violations) || []
      if (viols.length) {
        resetScopeViolations(viols)
        app().showToast(
          (e.detail && e.detail.message) ||
            t('skill_scope_mismatch') ||
            '技能 scope 不匹配，请修正标红节点'
        )
        return
      }
      app().showToast(t('save_failed', { msg: e.detail?.message || e.message }))
    })
}
// Issue #1564：violations[] → 节点索引（node_id 优先，回退 node_tag）
const scopeViolations = reactive({})
function resetScopeViolations(list) {
  Object.keys(scopeViolations).forEach(function (k) {
    delete scopeViolations[k]
  })
  ;(list || []).forEach(function (v) {
    const k = (v && (v.node_id || v.node_tag)) || ''
    if (k) scopeViolations[k] = v
  })
}
function scopeViolationOf(node) {
  if (!node) return null
  return scopeViolations[node.id || ''] || scopeViolations[node.tag || ''] || null
}
function scopeViolationHint(v) {
  if (!v) return ''
  const skill = v.skill || ''
  const actual = v.actual_scope || ''
  const expected = v.expected_scope || 'repo'
  return (
    (t('skill_scope_mismatch') || '技能 scope 不匹配') +
    '：' +
    skill +
    '（' +
    actual +
    ' → ' +
    expected +
    '）'
  )
}
function buildNodesPayload(nodes) {
  return nodes
    .filter((n) => n.enabled)
    .map((n, i, arr) => ({
      id: n.id,
      tag: n.tag,
      skill: n.skill || '',
      mode: n.mode || 'emit',
      next: i < arr.length - 1 ? arr[i + 1].id : 'done',
      handoff: i < arr.length - 1 ? 'a2a' : 'complete',
      skip: n.skip || [],
      enabled: true,
      engine_runtime_ids: n.engine_runtime_ids || [],
    }))
}
function collectLinkedRepos() {
  return linkedRepos.value
    .filter((lr) => lr.repoKey)
    .map((lr) => ({ repo_key: lr.repoKey, branch: lr.branch || 'main' }))
}
function loadBranches(owner, name) {
  if (!owner || !name) return
  API()
    .get('/scenes/branches/' + owner + '/' + name + branchQS(owner, name))
    .then(function (b) {
      branches.value = (b && b.branches) || []
    })
    .catch(function () {
      branches.value = []
    })
}
function createNewBranch() {
  if (!newBranchName.value.trim()) {
    app().showToast(t('branch_name_empty'))
    return
  }
  API()
    .post(
      '/scenes/branches/' +
        dashOwner.value +
        '/' +
        dashName.value +
        branchQS(dashOwner.value, dashName.value),
      {
        branch_name: newBranchName.value.trim(),
        refs: newBranchRef.value || 'main',
      }
    )
    .then(function () {
      app().showToast(t('branch_created'))
      return API().get(
        '/scenes/branches/' +
          dashOwner.value +
          '/' +
          dashName.value +
          branchQS(dashOwner.value, dashName.value)
      )
    })
    .then(function (b) {
      branches.value = (b && b.branches) || []
      advWorkBranch.value = newBranchName.value.trim()
      newBranchName.value = ''
      newBranchOpen.value = false
    })
    .catch(function (e) {
      app().showToast(e.detail?.message || e.message || '创建失败')
    })
}
function addLinkedRepo() {
  linkedRepos.value.push({ repoKey: '', branch: '', branches: [] })
}
function loadLinkedBranches(i) {
  const lr = linkedRepos.value[i]
  if (!lr || !lr.repoKey) return
  const parts = stripRepoPrefix(lr.repoKey).split('/')
  if (parts.length < 2) return
  API()
    .get(
      '/scenes/branches/' +
        encodeURIComponent(parts[0]) +
        '/' +
        encodeURIComponent(parts[1]) +
        branchQS(parts[0], parts[1])
    )
    .then(function (b) {
      lr.branches = (b && b.branches) || []
    })
    .catch(function () {
      lr.branches = []
    })
}

// ═══ 编辑操作（创建/编辑共用） ═══
function toggleAgent(agentType) {
  const node = editNodes.value.find((n) => n.id === agentType)
  if (!node) return
  node.enabled = !node.enabled
  // Issue #406：禁用时 skill/display 同清
  if (!node.enabled) {
    node.skill = ''
    node.skill_display_name = ''
    node.engine_runtime_ids = []
  }
  if (mode.value === 'create') saveCreateDraft()
  else if (flowEditing.value) saveEditDraft(detail.value ? detail.value.id : '')
}

// ═══ 技能下拉（Issue #292 分组+搜索） ═══
function openSkillDropdown(agentType, e) {
  const rect = e.currentTarget.getBoundingClientRect()
  skillPanel.agentType = agentType
  const node = editNodes.value.find((n) => n.id === agentType)
  skillPanel.current = node ? node.skill || '' : ''
  skillPanel.currentKey = ''
  skillPanel.q = ''
  skillPanel.x = rect.left
  skillPanel.w = Math.max(rect.width, 240)
  const estHeight = 320
  let top = rect.bottom + 4
  if (top + estHeight > G.window.innerHeight) top = Math.max(8, rect.top - estHeight - 4)
  skillPanel.y = top
  skillPanel.open = true
  // 加载技能
  const repo = currentRepo()
  const repoParam =
    '?page_size=100' + (repo && repo.fullName ? '&repo=' + encodeURIComponent(repo.fullName) : '')
  API()
    .get('/scenes/skills/' + agentType + repoParam)
    .then(function (r) {
      skillPanel.skills = r.skills || []
      // Issue #978：缓存唯一键 → {name, display}；同名计数用于旧裸名回显兜底
      const cache = (skillPickCache[agentType] = {})
      const counts = {}
      skillPanel.skills.forEach(function (s) {
        cache[skillKeyOf(s)] = { name: s.skill_name, display: s.display_name || s.skill_name }
        counts[s.skill_name] = (counts[s.skill_name] || 0) + 1
      })
      skillPanel.nameCounts = counts
      // 已选技能若在列表中唯一 → 用唯一键精确回显；同名多档不勾选
      const hits = skillPanel.skills.filter(function (s) {
        return s.skill_name === (skillPanel.current || '')
      })
      skillPanel.currentKey = hits.length === 1 ? skillKeyOf(hits[0]) : ''
    })
    .catch(function () {
      skillPanel.skills = []
      app().showToast(t('skill_load_failed') || '技能列表加载失败')
    })
}
const skillGroups = computed(() => {
  const q = skillPanel.q.toLowerCase().trim()
  const list = q
    ? skillPanel.skills.filter((s) =>
        [s.skill_name, s.display_name, s.description].join(' ').toLowerCase().includes(q)
      )
    : skillPanel.skills.slice()
  const groups = [
    { key: 'official', label: '官方技能' },
    { key: 'repo', label: '仓库技能' },
    { key: 'user', label: '个人技能' },
  ]
  return groups
    .map(function (g) {
      return {
        key: g.key,
        label: g.label,
        items: list.filter((s) => (s.scope || 'official') === g.key),
      }
    })
    .filter((g) => g.items.length)
})
function pickSkill(skillName, skillKey) {
  const node = editNodes.value.find((n) => n.id === skillPanel.agentType)
  if (node) {
    node.skill = skillName || ''
    if (scopeViolations[node.id || '']) delete scopeViolations[node.id]
    const cache = skillPickCache[skillPanel.agentType] || {}
    // Issue #978：优先按唯一键命中缓存；无 key 时按 name 兜底（同名三档不再覆盖）
    let entry = skillKey ? cache[skillKey] : null
    if (!entry && skillName) {
      const hit = Object.keys(cache).find(function (k) {
        return cache[k] && cache[k].name === skillName
      })
      entry = hit ? cache[hit] : null
    }
    node.skill_display_name = skillName ? (entry && entry.display) || skillName : ''
  }
  skillPanel.open = false
  if (mode.value === 'create') saveCreateDraft()
  else if (flowEditing.value) saveEditDraft(detail.value ? detail.value.id : '')
}
function onDocClick(e) {
  // 关闭技能面板
  if (
    skillPanel.open &&
    !e.target.closest('.dbv-skillpanel') &&
    !e.target.closest('.dbv-skillpick')
  ) {
    skillPanel.open = false
  }
}

// ═══ 引擎选择弹窗 ═══
function openEngineModal(agentType) {
  engineModal.open = true
  engineModal.agentType = agentType
  engineModal.q = ''
  engineModal.filterType = ''
  engineModal.filterScope = ''
  engineDetachHint.value = ''
  const node = editNodes.value.find((n) => n.id === agentType)
  const all = (agentEngines.value[agentType] || []).slice()
  // SRE 专属：只显示个人+仓库
  const visible =
    agentType === 'sre' ? all.filter((e) => e.scope === 'user' || e.scope === 'repo') : all
  const visibleIds = new Set(visible.map((e) => e.id))
  // 打开即剔除不可见引擎勾选
  engCurIds.value = ((node && node.engine_runtime_ids) || []).filter((id) => visibleIds.has(id))
  // 异步拉 detached 引擎提示
  if (dashOwner.value && dashName.value) {
    API()
      .get('/agents/' + agentType + '/' + dashOwner.value + '/' + dashName.value)
      .then(function (d) {
        const hints = d.detached_engines || []
        const mine = hints.filter(function (h) {
          return !h.node_tag || h.node_tag === agentType
        })
        if (!mine.length) return
        const names = mine.map(function (h) {
          return (h.runtime_name || 'engine') + '#' + h.runtime_id
        })
        const uniq = names.filter(function (v, i) {
          return names.indexOf(v) === i
        })
        engineDetachHint.value =
          '<div class="dbv-detach">' +
          '以下引擎因离线超过 8 小时被系统移除：' +
          '<b>' +
          esc(uniq.join('、')) +
          '</b>' +
          '，如需使用请在下方列表重新勾选绑定。' +
          '</div>'
      })
      .catch(function () {})
  }
  ensureSceneTags()
}
const engModalAll = computed(() => (agentEngines.value[engineModal.agentType] || []).slice())
const engModalVisible = computed(() => {
  const all = engModalAll.value
  return engineModal.agentType === 'sre'
    ? all.filter((e) => e.scope === 'user' || e.scope === 'repo')
    : all
})
const engModalAvailTypes = computed(() => {
  const types = []
  engModalVisible.value.forEach(function (e) {
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
const engModalAvailScopes = computed(() => [
  ...new Set(engModalVisible.value.map((e) => e.scope || 'global')),
])
const engModalFiltered = computed(() => {
  const q = engineModal.q.toLowerCase().trim()
  return engModalVisible.value.filter(function (e) {
    if (q) {
      // Issue #1310：搜索引擎名/凭据名/设备名，与列表显示口径一致
      const hay = [
        engineDisplayName(e),
        e.name,
        e.alias,
        e.worker_name || '',
        e.engine_name || '',
        e.scope_label,
        (e.tags || []).join(' '),
      ]
        .join(' ')
        .toLowerCase()
      if (!hay.includes(q)) return false
    }
    if (engineModal.filterType) {
      const ename = (e.name || '').toLowerCase()
      if (!ename.includes(engineModal.filterType)) return false
    }
    if (engineModal.filterScope && (e.scope || 'global') !== engineModal.filterScope) return false
    return true
  })
})
const engModalByScope = computed(() => {
  const groups = {}
  engModalFiltered.value.forEach(function (e) {
    const s = e.scope || 'global'
    if (!groups[s]) groups[s] = []
    groups[s].push(e)
  })
  return groups
})
function toggleEngineId(id) {
  const i = engCurIds.value.indexOf(id)
  if (i >= 0) engCurIds.value.splice(i, 1)
  else engCurIds.value.push(id)
}
function engStatusColor(st) {
  return (
    { online: '#34c759', recently_lost: '#f59e0b', offline: 'var(--t3)', about_to_gc: '#ff3b30' }[
      st
    ] || 'var(--t3)'
  )
}
function engStatusLabel(st) {
  return (
    { online: t('online'), recently_lost: '失联', offline: '离线', about_to_gc: '待回收' }[st] ||
    '离线'
  )
}
function confirmEnginePick() {
  const node = editNodes.value.find((n) => n.id === engineModal.agentType)
  if (node) node.engine_runtime_ids = engCurIds.value.slice()
  engineModal.open = false
  // Issue #407 R1: 引擎确认后同步保存草稿
  if (mode.value === 'create') saveCreateDraft()
  else if (flowEditing.value) saveEditDraft(detail.value ? detail.value.id : '')
}

// ═══ 场景详情页 ═══
function loadSceneDetail(sceneId) {
  mode.value = 'detail'
  loaded.value = true // 组件重新 mount 后 loading 态复位（否则 !loaded 抢先渲染）
  detail.value = null
  detailError.value = ''
  flowEditing.value = false
  const data = dashData.value || {}
  const [owner, name] = resolveOwnerName(data)
  dashOwner.value = owner
  dashName.value = name
  // 恒请求权威 dashboard（不复用快照——陈旧快照会误判场景不存在）
  const dashPromise = API().get('/dashboard/' + owner + '/' + name)
  const pipeCfgPromise = API().get(
    '/scenes/with-pipeline?repo=' + encodeURIComponent(owner + '/' + name)
  )
  Promise.all([dashPromise, pipeCfgPromise])
    .then(function (results) {
      const [dashData2, scenesData] = results
      // 场景判定「新鲜优先」
      const scenesList =
        scenesData.scenes && scenesData.scenes.length
          ? scenesData.scenes.map(function (s) {
              return {
                id: s.id,
                name: s.name,
                display_name: s.display_name,
                scope: 'repo',
                description: s.description,
                pr_subscription_enabled: s.pr_subscription_enabled,
                created_by: s.created_by,
              }
            })
          : dashData2._scenes || []
      const scene = scenesList.find(function (s) {
        return s.id === sceneId
      })
      if (!scene) {
        detailError.value = '场景不存在'
        return
      }
      const scenePipeline = (scenesData.scenes || []).find(function (s) {
        return s.id === sceneId
      })
      const pipeline = scenePipeline ? scenePipeline.pipeline : scenePipelines.value[sceneId] || {}
      detail.value = scene
      viewNodes.value = (pipeline && pipeline.nodes) || []
      agents.value = dashData2.agents || []
      agentEngines.value = dashData2.agent_engines || {}
      detailPrSub.value = !!scene.pr_subscription_enabled
      dashData.value = dashData2
      G.window._dashboardData = dashData2
      // 高级选项加载
      loadAdvancedOptions(sceneId, owner, name)
      // Issue #461: 场景级访问地址（地球仪三态）
      loadSceneDeployUrl(sceneId)
      // 延迟加载相关流水线（Issue #1077-S：按场景独立查询 + 服务端分页，
      // 不再从整仓最近 50 条里前端过滤——仓越忙本场景越容易看不全）
      loadScenePipePage(sceneId, 0)
      // Issue #407: 编辑模式自动恢复
      const autoEdit = getDashEditMode()
      if (autoEdit && autoEdit.mode === 'edit' && autoEdit.sceneId === sceneId) {
        clearDashEditMode()
        enterFlowEdit()
      } else if (autoEdit && autoEdit.mode === 'edit') {
        clearDashEditMode()
      }
    })
    .catch(function (e) {
      detailError.value = t('load_failed') + ': ' + (e.message || '')
    })
}
function loadAdvancedOptions(sceneId, owner, name) {
  // 托管仓库列表
  API()
    .get('/repos/managed')
    .then(function (d) {
      linkedRepoOptions.value = (d && d.repos) || []
    })
    .catch(function () {
      linkedRepoOptions.value = []
    })
  // 分支
  loadBranches(owner, name)
  // 高级选项
  API()
    .get('/scenes/' + sceneId + '/advanced-options?repo=' + encodeURIComponent(owner + '/' + name))
    .then(function (d) {
      detailWorkBranch.value = d.work_branch || 'main'
      advWorkBranch.value = d.work_branch || 'main'
      advFields.value = (d.custom_fields || []).map(function (f) {
        const sec = computeIsSecret(f.name, f.is_secret)
        return {
          name: f.name || '',
          value: f.value || '',
          show: !sec,
          scope: f.scope || ['all'],
          preset: !!f.preset,
          description: f.description || '',
          // Issue #885: 服务端对密钥只给占位（value:"" + has_value），标记 keepValue 后
          // 保存时提交 keep_value 由服务端保留旧值；用户输入即清除
          hasValue: !!f.has_value,
          keepValue: !!(sec && f.has_value && !f.value),
        }
      })
      linkedRepos.value = (d.linked_repos || []).map(function (lr) {
        return { repoKey: lr.repo_key || '', branch: lr.branch || '', branches: [] }
      })
      // 预填关联仓库分支
      linkedRepos.value.forEach(function (lr, i) {
        if (lr.repoKey) loadLinkedBranches(i)
      })
    })
    .catch(function () {})
}
function saveAdvancedOptions() {
  if (!advWorkBranch.value) {
    app().showToast(t('branch_required'))
    return
  }
  const cf = collectFields(advFields.value)
  if (cf.error) {
    app().showToast(cf.error)
    return
  }
  API()
    .put(
      '/scenes/' +
        encodeURIComponent(detail.value.id) +
        '/advanced-options?repo=' +
        encodeURIComponent(dashOwner.value + '/' + dashName.value),
      {
        work_branch: advWorkBranch.value,
        custom_fields: cf.fields,
        linked_repos: collectLinkedRepos(),
      }
    )
    .then(function () {
      app().showToast(t('adv_options_saved'))
    })
    .catch(function (e) {
      app().showToast(e.detail?.message || e.message || '保存失败')
    })
}
// 内联编辑（名称/描述）
function enterInlineEdit(field) {
  if (field === 'name') {
    inlineName.value = detail.value.display_name || detail.value.name || ''
    inlineEdit.value = true
  } else {
    inlineDesc.value = detail.value.description || ''
    inlineEditDesc.value = true
  }
}
function saveInlineEdit(field) {
  const trimmed = (field === 'name' ? inlineName.value : inlineDesc.value).trim()
  if (field === 'name' && !trimmed) {
    app().showToast(t('please_input_scene_name'))
    return
  }
  if (field === 'name') inlineEdit.value = false
  else inlineEditDesc.value = false
  // 先取权威 pipeline nodes
  API()
    .get('/scenes/with-pipeline?repo=' + encodeURIComponent(dashOwner.value + '/' + dashName.value))
    .then(function (d) {
      const sc = (d.scenes || []).find(function (s) {
        return s.id === detail.value.id
      })
      const pipeNodes = ((sc && sc.pipeline && sc.pipeline.nodes) || []).filter(function (n) {
        return n.enabled !== false
      })
      const payload = { nodes: pipeNodes }
      if (field === 'name') payload.name = trimmed
      else payload.description = trimmed
      API()
        .put(
          '/scenes/' +
            encodeURIComponent(detail.value.id) +
            '?repo=' +
            encodeURIComponent(dashOwner.value + '/' + dashName.value),
          payload
        )
        .then(function () {
          app().showToast(t('scene_saved'))
          if (field === 'name') detail.value.display_name = trimmed
          else detail.value.description = trimmed
        })
        .catch(function (err) {
          app().showToast(t('save_failed', { msg: err.detail?.message || err.message }))
        })
    })
    .catch(function (err) {
      app().showToast('加载场景数据失败: ' + (err.message || ''))
    })
}
// PR 订阅
function togglePrSubscription() {
  const newOn = !detailPrSub.value
  detailPrSub.value = newOn
  API()
    .put(
      '/scenes/' +
        encodeURIComponent(detail.value.id) +
        '?repo=' +
        encodeURIComponent(dashOwner.value + '/' + dashName.value),
      { pr_subscription_enabled: newOn }
    )
    .then(function () {
      app().showToast(newOn ? 'PR 订阅已开启' : 'PR 订阅已关闭')
      if (detail.value) detail.value.pr_subscription_enabled = newOn
    })
    .catch(function (e) {
      detailPrSub.value = !newOn
      app().showToast('保存失败: ' + (e.detail?.message || e.message))
    })
}
// 流程编辑
function enterFlowEdit() {
  // 获取权威 dashData + pipeline nodes
  API()
    .get('/dashboard/' + dashOwner.value + '/' + dashName.value)
    .then(function (dashData2) {
      G.window._dashboardData = dashData2
      agents.value = dashData2.agents || []
      agentEngines.value = dashData2.agent_engines || {}
      API()
        .get(
          '/scenes/with-pipeline?repo=' + encodeURIComponent(dashOwner.value + '/' + dashName.value)
        )
        .then(function (pipeData) {
          const sc = (pipeData.scenes || []).find(function (s) {
            return s.id === detail.value.id
          })
          const originalNodes = (sc && sc.pipeline && sc.pipeline.nodes) || []
          buildEditNodes(originalNodes, agents.value)
          flowEditing.value = true
          G.window._dashboardEditing = true
          saveDashEditMode('edit', detail.value.id)
          // Issue #407: 恢复编辑草稿
          const draft = restoreDashDraft(dashOwner.value, dashName.value, 'edit', detail.value.id)
          if (draft) applyDraftNodes(draft)
        })
        .catch(function () {
          buildEditNodes(null, agents.value)
          flowEditing.value = true
          G.window._dashboardEditing = true
        })
    })
    .catch(function () {})
}
function buildEditNodes(originalNodes, ags) {
  const sceneNodes = originalNodes || (dashData.value || {}).scene_nodes || []
  editNodes.value = ALL_AGENTS.map(function (at) {
    const node = sceneNodes.find(function (n) {
      return (n.tag || n.id) === at
    })
    const a =
      ags.find(function (x) {
        return x.agent_type === at
      }) || {}
    if (node) {
      return {
        id: at,
        tag: at,
        skill: node.skill || a.skill || a.skill_display_name || '',
        skill_display_name: node.skill_display_name || a.skill_display_name || '',
        mode: node.mode || 'emit',
        next: node.next || 'done',
        handoff: node.handoff || 'a2a',
        skip: node.skip || [],
        enabled: node.enabled !== false,
        engine_runtime_ids: node.engine_runtime_ids || a.engine_runtime_ids || [],
      }
    }
    return {
      id: at,
      tag: at,
      skill: '',
      skill_display_name: '',
      mode: 'emit',
      next: 'done',
      handoff: 'a2a',
      skip: [],
      enabled: false,
      engine_runtime_ids: [],
    }
  })
}
function saveSceneEdit() {
  const emptyAgents = editNodes.value.filter(function (n) {
    return n.enabled && !n.skill
  })
  if (emptyAgents.length) {
    const names = emptyAgents
      .map(function (n) {
        return agentLabel(n.id)
      })
      .join('、')
    app().showToast(t('skill_empty_warning', { agents: names }))
    return
  }
  const sreNoEngine = editNodes.value.find(function (n) {
    return n.id === 'sre' && n.enabled && !(n.engine_runtime_ids || []).length
  })
  if (sreNoEngine) {
    app().showToast(t('sre_engine_personal_only'))
    return
  }
  API()
    .put(
      '/scenes/' +
        encodeURIComponent(detail.value.id) +
        '?repo=' +
        encodeURIComponent(dashOwner.value + '/' + dashName.value),
      { nodes: buildNodesPayload(editNodes.value) }
    )
    .then(function () {
      G.window._dashboardEditing = false
      flowEditing.value = false
      clearDashEditMode()
      clearDashDraft(dashOwner.value, dashName.value, 'edit', detail.value.id)
      app().showToast(t('scene_saved'))
      // 重新加载详情（查看模式）
      loadSceneDetail(detail.value.id)
    })
    .catch(function (e) {
      // Issue #1564：技能 scope 冲突 → 按后端 400 violations[] 对节点标红，就地修正
      const viols = (e && e.detail && e.detail.violations) || []
      if (viols.length) {
        resetScopeViolations(viols)
        app().showToast(
          (e.detail && e.detail.message) ||
            t('skill_scope_mismatch') ||
            '技能 scope 不匹配，请修正标红节点'
        )
        return
      }
      app().showToast(t('save_failed', { msg: e.detail?.message || e.message }))
    })
}
function cancelSceneEdit() {
  G.window._dashboardEditing = false
  flowEditing.value = false
  clearDashEditMode()
  clearDashDraft(dashOwner.value, dashName.value, 'edit', detail.value.id)
}
// 复制场景（Issue #402）
function copySceneFromDetail() {
  const sn = detail.value.display_name || detail.value.name || ''
  copyDlg.name = (sn + '-copy').slice(0, 40)
  copyDlg.open = true
}
function generateCopySceneName(want, existing) {
  want = String(want || '')
    .trim()
    .slice(0, 40)
  let finalName = want
  if (existing[finalName]) {
    for (let i = 2; i < 100; i++) {
      const suf = String(i)
      finalName = want.slice(0, 40 - suf.length) + suf
      if (!existing[finalName]) break
    }
  }
  return finalName
}
function doCopyScene() {
  const scene = detail.value
  const srcList = (dashData.value && dashData.value._scenes) || []
  let src = srcList.find(function (s) {
    return s && s.id === scene.id
  })
  if (!src) {
    src = {
      description: scene.description || '',
      icon: '',
      nodes: viewNodes.value || [],
      pr_subscription_enabled: scene.pr_subscription_enabled ? 1 : 0,
      linked_repos: [],
    }
  }
  // 引擎清洗
  const okEng = {}
  Object.keys(agentEngines.value || {}).forEach(function (ty) {
    okEng[ty] = (agentEngines.value[ty] || []).map(function (e) {
      return e && e.id
    })
  })
  const copyNodes = JSON.parse(JSON.stringify(src.nodes || []))
  let dropped = false
  copyNodes.forEach(function (n) {
    const tag = n.tag || n.id || ''
    const ok = okEng[tag] || []
    const eids = (n.engine_runtime_ids || []).filter(function (eid) {
      return ok.some(function (oid) {
        return oid == eid
      })
    })
    if (eids.length !== (n.engine_runtime_ids || []).length) dropped = true
    n.engine_runtime_ids = eids
    if (tag === 'sre' && n.enabled !== false && !eids.length) n.enabled = false
  })
  // 名称冲突处理
  const existing = {}
  srcList.forEach(function (s) {
    existing[(s.name || '').trim()] = 1
    existing[(s.display_name || s.name || '').trim()] = 1
  })
  const want = (copyDlg.name || '').trim() || (scene.display_name || scene.name) + '-copy'
  const finalName = generateCopySceneName(want.slice(0, 40), existing)
  API()
    .post('/scenes?repo=' + encodeURIComponent(dashOwner.value + '/' + dashName.value), {
      name: finalName,
      description: src.description || '',
      scope: 'repo',
      nodes: copyNodes,
      icon: src.icon || '',
      pr_subscription_enabled: !!src.pr_subscription_enabled,
      linked_repos: src.linked_repos || [],
    })
    .then(function (r) {
      copyDlg.open = false
      let msg = t('scene_copied')
      if (dropped) msg += '（不可用引擎已剔除）'
      app().showToast(msg)
      app().nav('scene-detail-local', r.scene_id)
    })
    .catch(function (e) {
      app().showToast(t('copy_failed', { msg: e.detail?.message || e.message }))
    })
}
// 删除场景
function confirmDeleteScene(sceneId, sceneName) {
  deleteDlg.id = sceneId
  deleteDlg.name = sceneName
  deleteDlg.open = true
}
function doDeleteScene() {
  API()
    .del(
      '/scenes/' +
        encodeURIComponent(deleteDlg.id) +
        '?repo=' +
        encodeURIComponent(dashOwner.value + '/' + dashName.value)
    )
    .then(function () {
      G.window._dashboardEditing = false
      deleteDlg.open = false
      app().showToast(t('scene_deleted'))
      app().nav('dash')
    })
    .catch(function (e) {
      deleteDlg.open = false
      app().showToast(t('delete_failed', { msg: e.detail?.message || e.message }))
    })
}
// ═══ Issue #461: 场景级访问地址（地球仪三态） ═══
function loadSceneDeployUrl(sceneId) {
  if (!sceneId) return
  API()
    .get(
      '/scenes/' +
        encodeURIComponent(sceneId) +
        '/deploy-url?repo=' +
        encodeURIComponent(dashOwner.value + '/' + dashName.value)
    )
    .then(function (d) {
      deployUrl.value = (d && d.deploy_url) || ''
    })
    .catch(function () {
      deployUrl.value = ''
    })
}
function startDeployUrlEdit() {
  deployUrlDraft.value = deployUrl.value || ''
  deployUrlEdit.value = true
}
function cancelDeployUrlEdit() {
  deployUrlEdit.value = false
}
function saveDeployUrl() {
  const v = (deployUrlDraft.value || '').trim()
  if (v && !/^https?:\/\//i.test(v)) {
    app().showToast('访问地址需以 http:// 或 https:// 开头')
    return
  }
  const sceneId = detail.value ? detail.value.id : ''
  if (!sceneId) return
  API()
    .put(
      '/scenes/' +
        encodeURIComponent(sceneId) +
        '/deploy-url?repo=' +
        encodeURIComponent(dashOwner.value + '/' + dashName.value),
      { deploy_url: v }
    )
    .then(function () {
      deployUrl.value = v
      deployUrlEdit.value = false
      app().showToast(t('save'))
    })
    .catch(function (e) {
      app().showToast((e && e.detail && e.detail.message) || (e && e.message) || '保存失败')
    })
}
// 长按 600ms 进入编辑（桌面右键同样触发）
function deployUrlPressStart() {
  clearTimeout(deployUrlPressTimer)
  deployUrlPressTimer = setTimeout(startDeployUrlEdit, 600)
}
function deployUrlPressEnd() {
  clearTimeout(deployUrlPressTimer)
}
// ═══ 相关流水线（服务端按场景分页，Issue #1077-S） ═══
const PIPE_SIZE = 10
const scenePipeTotal = ref(0)
const scenePipeActiveN = ref(0)
const scenePipeFailedN = ref(0)
const scenePipeHasMore = ref(false)
const scenePipeErr = ref('')
const scenePipeSeq = ref(0)
const pipeTotalPages = computed(() =>
  Math.max(1, Math.ceil((scenePipeTotal.value || 0) / PIPE_SIZE))
)
const scenePipePage = computed(() => scenePipes.value)
const scenePipeActive = computed(
  () => scenePipeActiveN.value || scenePipes.value.filter((p) => p.executing).length
)
const scenePipeFailed = computed(
  () =>
    scenePipeFailedN.value ||
    scenePipes.value.filter((p) => p.statuses && p.statuses.some((s) => s === 3)).length
)
function loadScenePipePage(sceneId, page) {
  if (!sceneId) return
  const repo = (dashData.value && dashData.value.repo) || ''
  const bare = String(repo).replace(/^(gitcode|github):/, '')
  if (bare.indexOf('/') < 0) return
  const p = Math.max(0, parseInt(page, 10) || 0)
  pipePage.value = p
  pipesLoading.value = true
  scenePipeErr.value = ''
  const seq = ++scenePipeSeq.value
  API()
    .get(
      '/pipeline/' +
        bare +
        '?scene_id=' +
        encodeURIComponent(sceneId) +
        '&history_limit=' +
        PIPE_SIZE +
        '&history_offset=' +
        p * PIPE_SIZE
    )
    .then(function (d) {
      if (seq !== scenePipeSeq.value) return // 场景已切换
      pipesLoading.value = false
      let rows = (d.history || []).slice()
      if (d.active && p === 0) rows = [d.active].concat(rows)
      scenePipes.value = rows
      scenePipeTotal.value = (d.history_total || 0) + (d.active ? 1 : 0)
      scenePipeHasMore.value = !!d.history_has_more
      scenePipeActiveN.value = d.active_count || 0
      scenePipeFailedN.value = d.failed_count || 0
    })
    .catch(function (e) {
      if (seq !== scenePipeSeq.value) return
      pipesLoading.value = false
      if (e && e.name === 'AbortError' && e.silent) return
      scenePipes.value = []
      scenePipeTotal.value = 0
      scenePipeErr.value = (e && e.message) || ''
    })
}
function stageLabel(stage) {
  const map = { vod: 'VOD', issue: 'Issue', coding: 'Coding', qa: 'QA', sre: 'SRE', done: '完成' }
  return map[stage] || stage
}
function pipeDuration(p) {
  const d = p.duration
  return d ? (d >= 60 ? Math.floor(d / 60) + 'm ' + Math.round(d % 60) + 's' : d + 's') : '—'
}
function pipeIssueUrl(p) {
  const num = String(p.issue_id || '').replace(/^#/, '')
  const repo = (dashData.value || {}).repo || ''
  const host = repo.startsWith('github:') ? 'github.com' : 'gitcode.com'
  const parts = stripRepoPrefix(repo).split('/')
  return num && parts.length === 2
    ? 'https://' + host + '/' + parts[0] + '/' + parts[1] + '/issues/' + num
    : '#'
}
function pipeAgentStyle(p, j) {
  const hasAgent = p.statuses && p.statuses[j] > 0
  const evSeq =
    (p.stages && p.stages[j] && p.stages[j].latest_event && p.stages[j].latest_event.seq) || ''
  return hasAgent && evSeq ? { cursor: 'pointer' } : { opacity: '0.3', cursor: 'default' }
}
function pipeAgentClick(p, j) {
  const hasAgent = p.statuses && p.statuses[j] > 0
  const evSeq =
    (p.stages && p.stages[j] && p.stages[j].latest_event && p.stages[j].latest_event.seq) || ''
  if (hasAgent && evSeq) app().nav('event', evSeq)
}
// Issue #166: 场景流水线 → 跳转任务中心对应需求详情
function jumpFromScenePipeline(p) {
  const issueNum = String(p.issue_id || '').replace(/^#/, '')
  let latestSeq = ''
  if (p.stages) {
    for (let si = p.stages.length - 1; si >= 0; si--) {
      if (p.statuses && p.statuses[si] > 0) {
        const ev = p.stages[si] && p.stages[si].latest_event
        if (ev && ev.seq) {
          latestSeq = ev.seq
          break
        }
      }
    }
  }
  if (!issueNum) {
    if (latestSeq) app().nav('event', latestSeq)
    return
  }
  G.window._pendingIssueNumber = String(issueNum).replace(/^#/, '')
  G.window._taskDetailFromScene = {
    sceneId: detail.value ? detail.value.id : '',
    latestSeq: latestSeq || '',
  }
  app().nav('tasks')
}
// ═══ 智能推荐规则 ═══
function openSceneRecommendModal() {
  recDlg.open = true
  recDlg.loaded = false
  recDlg.fallback = ''
  recDlg.rules = []
  recDlg.scenes = []
  API()
    .get('/scenes/with-pipeline?repo=' + encodeURIComponent(dashOwner.value + '/' + dashName.value))
    .then(function (d) {
      const scenes = (d && d.scenes) || []
      recDlg.scenes = scenes
      return API().get(
        '/repos/' + dashOwner.value + '/' + dashName.value + '/scene-recommend-config'
      )
    })
    .then(function (cfg) {
      if (!cfg) cfg = { fallback_scene: '', rules: [] }
      if (cfg.fallback_scene) recDlg.fallback = cfg.fallback_scene
      else if (recDlg.scenes.length) recDlg.fallback = recDlg.scenes[0].id
      recDlg.rules = (cfg.rules || []).map(function (r) {
        return { scene_id: r.scene_id, condition: r.condition }
      })
      recDlg.loaded = true // Issue #487：场景列表就绪前禁用添加/保存，避免空选项静默丢规则
    })
    .catch(function (e) {
      app().showToast('加载推荐配置失败: ' + (e.message || ''))
    })
}
function saveSceneRecommend() {
  if (!recDlg.fallback) {
    app().showToast(t('recommend_fallback_required'))
    return
  }
  const rules = []
  const seen = {}
  for (const r of recDlg.rules) {
    if (r.scene_id && (r.condition || '').trim()) {
      if (seen[r.scene_id]) {
        app().showToast(t('recommend_rule_dup'))
        return
      }
      seen[r.scene_id] = true
      rules.push({ scene_id: r.scene_id, condition: r.condition.trim() })
    }
  }
  API()
    .put('/repos/' + dashOwner.value + '/' + dashName.value + '/scene-recommend-config', {
      fallback_scene: recDlg.fallback,
      rules: rules,
    })
    .then(function () {
      app().showToast('智能推荐规则已保存')
      recDlg.open = false
    })
    .catch(function (e) {
      app().showToast(e.detail?.message || e.message || '保存失败')
    })
}

// ═══ 生命周期 ═══
onMounted(function () {
  document.addEventListener('click', onDocClick)
  if (props.initialData) {
    // renderDashboard(data) 契约：app.js 加载数据后注入
    renderDash(props.initialData)
  } else if (props.initialDetailId) {
    // renderSceneDetailLocal(id) 契约：自加载
    G.window._currentView = 'scene-detail-local'
    loadSceneDetail(props.initialDetailId)
  } else {
    // 兜底：自加载 dashboard（通常 app.js 已注入 data）
    loadError.value = ''
    loaded.value = false
    const repo = currentRepo()
    if (repo) {
      Promise.all([
        API().get('/dashboard/' + repo.owner + '/' + repo.name),
        API().get('/scenes?repo=' + encodeURIComponent(repo.owner + '/' + repo.name)),
      ])
        .then(function (results) {
          const [dashData2, scenesData] = results
          dashData2._scenes = scenesData.scenes || []
          renderDash(dashData2)
        })
        .catch(function (e) {
          loadError.value = t('load_failed') + ': ' + (e.message || '')
          loaded.value = true
        })
    } else {
      loadError.value = '请先选择仓库'
      loaded.value = true
    }
  }
})
onBeforeUnmount(function () {
  document.removeEventListener('click', onDocClick)
  G.window._dashboardEditing = false
})
</script>

<style scoped>
.dbv {
  min-height: 100%;
}
.dbv .loading,
.dbv .error {
  padding: 40px;
  text-align: center;
}
/* 按钮 */
.ap-btn {
  padding: 7px 18px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s ease;
  background: transparent;
  border: 1px solid rgba(0, 113, 227, 0.3);
  color: #0071e3;
  font-family: inherit;
}
.ap-btn:hover {
  background: #0071e3;
  border-color: #0071e3;
  color: var(--on-accent);
}
.ap-btn-d {
  border: 1px solid rgba(255, 59, 48, 0.3);
  color: #ff3b30;
}
.ap-btn-d:hover {
  background: #ff3b30;
  border-color: #ff3b30;
  color: var(--on-accent);
}
/* Hero */
.ap-hero {
  background: var(--glass-hero);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: none;
  border-radius: 18px;
  padding: 24px 32px;
  margin-bottom: 16px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}
.ap-hero-row {
  display: flex;
  align-items: center;
  gap: 16px;
}
.ap-hero-icon {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: linear-gradient(135deg, #5ac8fa, #0071e3);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--on-accent);
  font-size: 22px;
  flex-shrink: 0;
}
.ap-hero-title {
  font-size: 17px;
  font-weight: 600;
  color: var(--t);
  letter-spacing: -0.3px;
}
.ap-hero-sub {
  font-size: 12px;
  color: var(--t3);
  margin-top: 3px;
}
.ap-stats {
  display: flex;
  gap: 8px;
  margin-left: auto;
}
.ap-stat {
  text-align: center;
  padding: 8px 18px;
  background: var(--bg);
  border-radius: 12px;
  min-width: 64px;
}
.ap-stat .v {
  font-size: 20px;
  font-weight: 700;
  line-height: 1.1;
}
.ap-stat .l {
  font-size: 10px;
  color: var(--t3);
  margin-top: 2px;
}
.dbv-rolepill {
  font-size: 10px;
  font-weight: 500;
  color: #0071e3;
  background: #e3edfd;
  padding: 2px 8px;
  border-radius: 6px;
  vertical-align: middle;
  margin-left: 4px;
}
/* host banner */
.dbv-hostbanner {
  display: flex;
  align-items: center;
  gap: 10px;
  background: rgba(255, 243, 205, 0.8);
  backdrop-filter: blur(8px);
  border: none;
  border-radius: 14px;
  padding: 10px 14px;
  margin-bottom: 12px;
  font-size: 13px;
  color: #7a5b00;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}
.dbv-hostbanner.error {
  background: rgba(253, 236, 234, 0.8);
  color: #c31f1e;
}
.dbv-hostbanner button {
  margin-left: auto;
  background: rgba(255, 149, 0, 0.15);
  color: #ff9500;
  border: 1px solid rgba(255, 149, 0, 0.3);
  border-radius: 999px;
  padding: 6px 16px;
  cursor: pointer;
  font-size: 12px;
  font-weight: 500;
}
.dbv-hostbanner button:hover {
  background: #ff9500;
  color: var(--on-accent);
}
.dbv-hostbanner.error button {
  color: #ff3b30;
  border-color: rgba(255, 59, 48, 0.3);
  background: rgba(255, 59, 48, 0.1);
}
.dbv-hostbanner.error button:hover {
  background: #ff3b30;
  color: var(--on-accent);
}
/* 市场卡 */
.dbv-marketcard {
  margin-bottom: 16px;
  background: color-mix(in srgb, var(--c) 62%, transparent);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: none;
  border-radius: 18px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.05);
}
.dbv-marketrow {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 20px;
}
.dbv-market-t {
  font-size: 14px;
  font-weight: 600;
  color: var(--t);
  margin-bottom: 4px;
}
.dbv-market-d {
  font-size: 12px;
  color: var(--t3);
  line-height: 1.5;
}
/* 场景卡 */
.dbv-scenecard {
  background: color-mix(in srgb, var(--c) 62%, transparent);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: none;
  border-radius: 18px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.05);
}
.dbv-scenehead {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}
.dbv-scene-count {
  font-size: 10px;
  font-weight: 400;
  color: var(--t2);
  background: var(--bg);
  padding: 2px 8px;
  border-radius: 8px;
  margin-left: 4px;
}
.scn-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.scn-row {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 16px 20px;
  background: color-mix(in srgb, var(--c) 62%, transparent);
  backdrop-filter: blur(16px);
  border: none;
  border-radius: 18px;
  cursor: pointer;
  transition: all 0.25s ease;
  position: relative;
  overflow: hidden;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.05);
}
.scn-row:hover {
  background: color-mix(in srgb, var(--c) 82%, transparent);
  box-shadow: 0 3px 16px rgba(0, 0, 0, 0.08);
}
.scn-row-icon {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  flex-shrink: 0;
  color: var(--on-accent);
  font-weight: 600;
}
.scn-row-name {
  font-size: 15px;
  font-weight: 600;
  color: var(--t);
  white-space: nowrap;
  letter-spacing: -0.2px;
}
.dbv-scene-owner {
  font-size: 10px;
  color: var(--t3);
}
.scn-row-mid {
  flex: 0 1 30%;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.scn-row-desc {
  font-size: 12px;
  color: var(--t3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-weight: 400;
}
.dbv-scene-descrow {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.scn-row-chain {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  flex: 1 1 auto;
  min-width: 0;
  padding: 0 16px;
}
.scn-row-chain .scn-tag {
  font-size: 12px;
  font-weight: 500;
  padding: 4px 14px;
  border-radius: 999px;
  letter-spacing: 0.1px;
}
.scn-row-chain .scn-arr {
  font-size: 12px;
  color: #d2d2d7;
  font-weight: 400;
  margin: 0 1px;
}
.scn-branch-tag {
  font-size: 11px;
  font-weight: 500;
  padding: 3px 12px;
  border-radius: 999px;
  background: rgba(142, 142, 147, 0.1);
  color: #8e8e93;
  letter-spacing: 0.1px;
  flex-shrink: 0;
}
.scn-row-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}
.dbv-pr-badge {
  font-size: 10px;
  font-weight: 500;
  padding: 3px 10px;
  border-radius: 999px;
  background: rgba(175, 82, 222, 0.1);
  color: #af52de;
  border: 1px solid rgba(175, 82, 222, 0.2);
}
/* 空态 */
.dbv-emptycard {
  background: var(--c);
  border: 1px solid #d2d2d7;
  border-radius: 18px;
  padding: 48px 24px;
  margin-bottom: 16px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.06);
  text-align: center;
}
.dbv-empty-ic {
  font-size: 40px;
  margin-bottom: 12px;
}
.dbv-empty-t {
  font-size: 15px;
  font-weight: 600;
  color: var(--t);
  margin-bottom: 6px;
}
.dbv-empty-d {
  font-size: 12px;
  color: var(--t3);
  margin-bottom: 20px;
}
/* 头卡（创建/详情） */
.dbv-headcard {
  padding: 20px 24px;
  margin-bottom: 16px;
}
.dbv-headrow {
  display: flex;
  align-items: flex-start;
  gap: 16px;
}
.dbv-scene-icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  font-size: 18px;
  background: #8b9db0;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: var(--on-accent);
  font-weight: 600;
}
.dbv-headfields {
  flex: 1;
  min-width: 0;
}
.dbv-name-display {
  font-size: 20px;
  font-weight: 700;
  cursor: text;
  padding: 2px 6px;
  margin: -2px -6px 4px;
  border-radius: 6px;
  transition: background 0.15s;
}
.dbv-name-display:hover {
  background: rgba(0, 0, 0, 0.04);
}
.dbv-desc-display {
  font-size: 13px;
  color: var(--t3);
  cursor: text;
  padding: 2px 6px;
  margin: -2px -6px;
  border-radius: 6px;
  transition: background 0.15s;
}
.dbv-desc-display:hover {
  background: rgba(0, 0, 0, 0.04);
}
.dbv-desc-ph {
  color: #c7c7cc;
  font-style: italic;
}
.dbv-name-input {
  width: 100%;
  font-size: 20px;
  font-weight: 700;
  padding: 4px 8px;
  border: 1px solid #d2d2d7;
  border-radius: 6px;
  outline: none;
  background: var(--c);
  margin-bottom: 6px;
  font-family: inherit;
  color: var(--t);
}
.dbv-desc-input {
  width: 100%;
  font-size: 13px;
  padding: 4px 8px;
  border: 1px solid #d2d2d7;
  border-radius: 6px;
  outline: none;
  background: var(--c);
  color: var(--t3);
  font-family: inherit;
}
.dbv-iconpicker {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
  padding: 8px;
  background: var(--bg);
  border-radius: 10px;
}
.dbv-iconpick {
  font-size: 20px;
  width: 36px;
  height: 36px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.15s;
  background: var(--bg);
}
.dbv-iconpick.on {
  background: #0071e3;
  color: var(--on-accent);
}
/* 详情卡 */
.det-card-wrap {
  background: color-mix(in srgb, var(--c) 62%, transparent);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: none;
  border-radius: 18px;
  padding: 24px;
  margin-bottom: 16px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.05);
}
.dbv-sechead {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}
.dbv-sechead h2 {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
}
/* 流程卡 */
.ap-grid {
  display: flex;
  gap: 12px;
  padding: 0;
  overflow-x: auto;
  min-height: 280px;
}
.ap-nd.scope-bad {
  border: 1.5px solid var(--r);
  box-shadow: 0 0 0 3px rgba(255, 59, 48, 0.12);
}
.ap-nd-violation {
  font-size: 10px;
  line-height: 1.4;
  color: var(--r);
  margin-top: 6px;
  text-align: left;
  width: 100%;
}
.ap-nd {
  flex: 1 1 0;
  min-width: 0;
  max-width: 260px;
  min-height: 260px;
  background: color-mix(in srgb, var(--c) 62%, transparent);
  backdrop-filter: blur(16px);
  border: none;
  border-radius: 18px;
  padding: 18px;
  display: flex;
  flex-direction: column;
  transition: all 0.25s ease;
  position: relative;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.05);
}
.ap-nd:hover {
  background: color-mix(in srgb, var(--c) 82%, transparent);
  box-shadow: 0 3px 16px rgba(0, 0, 0, 0.08);
}
.ap-nd.off {
  opacity: 0.35;
  filter: grayscale(1);
}
.ap-nd-top {
  width: 36px;
  height: 4px;
  border-radius: 2px;
  margin-bottom: 12px;
}
.ap-nd-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.ap-nd-num {
  width: 22px;
  height: 22px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.ap-nd-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--t);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ap-nd-toggle {
  cursor: pointer;
  font-size: 14px;
  margin-left: auto;
  padding: 2px 6px;
}
.ap-nd-desc {
  font-size: 11px;
  color: var(--t3);
  line-height: 1.5;
  margin-bottom: 10px;
  font-weight: 400;
}
.ap-nd-off-lbl {
  font-size: 10px;
  color: var(--t3);
  margin-bottom: 8px;
}
.ap-nd-hr {
  height: 1px;
  background: var(--hairline);
  margin: 8px 0;
}
.ap-nd-skillbox,
.ap-nd-engbox {
  background: var(--c);
  border: 1px solid var(--hairline);
  border-radius: 10px;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  align-items: center;
}
.ap-nd-skillbox {
  margin-bottom: 8px;
}
.ap-nd-engbox {
  flex: 1;
  justify-content: center;
  gap: 6px;
}
.ap-nd-lbl {
  font-size: 8px;
  color: var(--t3);
  text-transform: uppercase;
  letter-spacing: 0.8px;
  margin-bottom: 3px;
}
.ap-nd-none {
  font-size: 11px;
  color: var(--t3);
}
.dbv-skillname {
  font-size: 11px;
  font-weight: 500;
  color: var(--t);
}
.dbv-skillpick {
  width: 100%;
  padding: 6px 10px;
  border: 1px solid var(--hairline);
  border-radius: 8px;
  font-size: 11px;
  background: var(--bg);
  color: var(--t);
  outline: none;
  cursor: pointer;
  text-align: left;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dbv-skillpick:hover {
  border-color: #0071e3;
  background: var(--c);
}
.ap-nd-engbtn {
  font-size: 10px;
  padding: 3px 10px;
  border: 1px solid var(--hairline);
  border-radius: 8px;
  background: transparent;
  color: var(--t2);
  cursor: pointer;
}
.ap-nd-engbtn:hover {
  border-color: #0071e3;
  color: #0071e3;
}
.ap-arr {
  font-size: 18px;
  color: #0071e3;
  font-weight: 700;
  align-self: center;
  margin: 0 4px;
  flex-shrink: 0;
}
/* 引擎徽章 —— :deep 必须：徽章是 v-html 注入的裸 span，无 scope 属性，
   scoped 规则 .dbv-engbadge[data-v-xxx] 匹配不上会退化为纯黑文本（胶囊丢失） */
:deep(.dbv-engbadge) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  font-weight: 600;
  padding: 4px 12px;
  border-radius: 999px;
}
:deep(.dbv-engbadge.green) {
  background: #34c75914;
  color: #34c759;
}
:deep(.dbv-engbadge.blue) {
  background: #0071e314;
  color: #0071e3;
}
:deep(.dbv-engbadge.red) {
  background: #ff3b3014;
  color: #ff3b30;
}
:deep(.dbv-engbadge.gray) {
  background: #86868b14;
  color: var(--t3);
}
/* PR 订阅 */
.dbv-prsub {
  padding: 16px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.dbv-prsub-t {
  font-size: 13px;
  font-weight: 600;
  color: var(--t);
}
.dbv-prsub-d {
  font-size: 11px;
  color: var(--t3);
  margin-top: 3px;
  line-height: 1.5;
}
.dbv-switch {
  width: 44px;
  height: 26px;
  border-radius: 999px;
  background: var(--hairline);
  cursor: pointer;
  transition: background 0.2s;
  position: relative;
  flex-shrink: 0;
  margin-left: 16px;
  display: inline-block;
}
.dbv-switch.on {
  background: #34c759;
}
.dbv-knob {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  /* 旋钮固定白：暗色下要压在浅色轨道上才看得见（不随 --c 翻转） */
  background: var(--on-accent);
  position: absolute;
  top: 2px;
  left: 2px;
  transition: left 0.2s;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
}
.dbv-switch.on .dbv-knob {
  left: 20px;
}
/* 高级选项 */
.adv-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 0;
  border-bottom: 1px solid rgba(0, 0, 0, 0.04);
}
.adv-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--t3);
  min-width: 80px;
  flex-shrink: 0;
}
.adv-select {
  flex: 1;
  max-width: 280px;
  padding: 7px 12px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  font-size: 13px;
  background: var(--bg);
  color: var(--t);
  outline: none;
  cursor: pointer;
  font-family: inherit;
}
.dbv-newbranch {
  padding: 8px 0 4px 92px;
  display: flex;
  gap: 8px;
}
.dbv-nb-input {
  flex: 1;
  padding: 7px 12px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  font-size: 13px;
  outline: none;
  background: var(--bg);
  font-family: inherit;
  color: var(--t);
}
.dbv-nb-sel {
  width: 120px;
  padding: 7px 12px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  font-size: 13px;
  background: var(--bg);
  outline: none;
  cursor: pointer;
  font-family: inherit;
}
.dbv-advsec {
  padding: 12px 0 4px;
}
.dbv-advsep {
  border-top: 1px solid rgba(0, 0, 0, 0.04);
  margin-top: 12px;
}
.dbv-advsec-t {
  font-size: 12px;
  font-weight: 600;
  color: var(--t3);
  margin-bottom: 8px;
}
.dbv-advsec-d {
  font-size: 11px;
  color: #a1a1a6;
  margin-bottom: 8px;
  line-height: 1.5;
}
.dbv-cflist {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.dbv-cfrow {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.dbv-cfkey {
  flex: 0 0 140px;
  padding: 7px 12px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  font-size: 12px;
  outline: none;
  background: var(--bg);
  font-family: inherit;
  color: var(--t);
}
.dbv-cfkey-static {
  flex: 0 0 140px;
  font-weight: 600;
  color: var(--t);
  font-size: 12px;
}
.dbv-cfval {
  flex: 1;
  padding: 7px 12px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  font-size: 12px;
  outline: none;
  background: var(--bg);
  font-family: inherit;
  color: var(--t);
}
.cf-invalid {
  border-color: var(--r, #dc3545) !important;
}
.dbv-cfeye {
  border: 1px solid var(--hairline);
  background: var(--bg);
  border-radius: 8px;
  width: 28px;
  height: 28px;
  padding: 0;
  font-size: 14px;
  cursor: pointer;
  color: var(--t3);
  flex-shrink: 0;
}
.dbv-cfdel {
  width: 28px;
  height: 28px;
  border-radius: 999px;
  border: none;
  background: rgba(255, 59, 48, 0.08);
  color: #ff3b30;
  cursor: pointer;
  font-size: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.dbv-cfdel:hover {
  background: rgba(255, 59, 48, 0.15);
}
.dbv-cfscope {
  display: inline-flex;
  gap: 2px 8px;
  align-items: center;
  flex-wrap: wrap;
}
.dbv-scope-lbl {
  font-size: 11px;
  color: var(--t3);
  display: inline-flex;
  align-items: center;
  gap: 2px;
  white-space: nowrap;
  cursor: pointer;
}
.dbv-addbtn-sm {
  margin-top: 8px;
  font-size: 11px;
}
/* 关联仓库 */
.dbv-lrrow {
  display: flex;
  gap: 8px;
  align-items: center;
}
.dbv-lrrepo {
  flex: 0 0 200px;
  padding: 8px 12px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  font-size: 12px;
  background: var(--bg);
  color: var(--t);
  outline: none;
  cursor: pointer;
  font-family: inherit;
}
.dbv-lrbranch {
  flex: 1;
  padding: 8px 12px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  font-size: 12px;
  background: var(--bg);
  color: var(--t);
  outline: none;
  cursor: pointer;
  font-family: inherit;
}
/* 流水线表 */
.pl-stat {
  text-align: center;
  padding: 8px 16px;
  background: var(--bg);
  border: none;
  border-radius: 14px;
  min-width: 64px;
}
.pl-stat .v {
  font-size: 18px;
  font-weight: 700;
  line-height: 1.1;
}
.pl-stat .l {
  font-size: 10px;
  color: var(--t3);
  margin-top: 2px;
}
.pl-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.pl-table th {
  text-align: left;
  padding: 10px 14px;
  font-weight: 500;
  color: var(--t3);
  border-bottom: 1px solid rgba(0, 0, 0, 0.04);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
}
.pl-table td {
  padding: 10px 14px;
  border-bottom: 1px solid rgba(0, 0, 0, 0.03);
}
.dbv-piperow {
  cursor: pointer;
  transition: background 0.12s;
}
.dbv-piperow:hover td {
  background: rgba(0, 0, 0, 0.02);
}
.dbv-pipepager {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
  font-size: 12px;
  color: var(--t3);
}
/* 技能下拉面板 */
.dbv-skillpanel {
  position: fixed;
  z-index: 1001;
  background: var(--c);
  border: 1px solid var(--hairline);
  border-radius: 12px;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.12);
  padding: 8px;
  box-sizing: border-box;
}
.dbv-skillsearch {
  width: 100%;
  box-sizing: border-box;
  padding: 7px 10px;
  border: 1px solid var(--hairline);
  border-radius: 8px;
  font-size: 12px;
  background: var(--bg);
  color: var(--t);
  outline: none;
  margin-bottom: 6px;
  font-family: inherit;
}
.dbv-skilllist {
  max-height: 260px;
  overflow-y: auto;
}
.dbv-skillgroup {
  margin: 6px 4px 4px;
  font-size: 10px;
  font-weight: 600;
  color: var(--t3);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.dbv-skillopt {
  padding: 7px 10px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 12px;
  color: var(--t);
  display: flex;
  align-items: center;
  gap: 6px;
}
.dbv-skillopt:hover {
  background: rgba(0, 113, 227, 0.06);
}
.dbv-skillopt.clear {
  color: var(--t3);
  border-bottom: 1px solid rgba(0, 0, 0, 0.05);
  margin-bottom: 4px;
}
.dbv-skillopt.sel {
  background: rgba(0, 113, 227, 0.08);
}
.dbv-skillopt.sel:hover {
  background: rgba(0, 113, 227, 0.08);
}
.dbv-skillcheck {
  color: #0071e3;
  font-weight: 700;
  flex-shrink: 0;
}
.dbv-skillname-t {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dbv-skillnomatch {
  padding: 14px 0;
  text-align: center;
  color: var(--t3);
  font-size: 12px;
}
/* 引擎弹窗 */
.rtv-modal-ov {
  position: fixed;
  inset: 0;
  z-index: 1001;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
}
.dbv-engmodal {
  background: var(--glass-hero-strong);
  backdrop-filter: blur(20px);
  border-radius: 18px;
  padding: 28px;
  width: 460px;
  height: 600px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.12);
  display: flex;
  flex-direction: column;
}
.dbv-engmodal h3 {
  margin: 0 0 4px;
  font-size: 17px;
  font-weight: 600;
  color: var(--t);
  letter-spacing: -0.3px;
}
.dbv-eng-sub {
  font-size: 12px;
  color: var(--t3);
  margin-bottom: 12px;
}
.dbv-detach {
  background: rgba(255, 59, 48, 0.08);
  border: 1px solid rgba(255, 59, 48, 0.18);
  border-radius: 12px;
  padding: 10px 14px;
  margin-bottom: 12px;
  font-size: 11px;
  color: #d32f2f;
  line-height: 1.6;
}
.dbv-engfilters {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
  flex-shrink: 0;
  align-items: center;
}
.dbv-engfilters > span {
  cursor: pointer;
  font-size: 11px;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--hairline);
  color: var(--t);
  background: var(--bg);
  font-weight: 500;
}
.dbv-engfilters > span.on {
  border-color: #0071e3;
  color: var(--on-accent);
  background: #0071e3;
}
.dbv-engsep {
  width: 1px;
  height: 14px;
  background: var(--hairline);
  margin: 0 4px;
  padding: 0 !important;
  border: none !important;
  cursor: default !important;
}
.dbv-engclear {
  color: var(--t3) !important;
}
.dbv-engsearch {
  width: 100%;
  padding: 8px 12px;
  border: none;
  border-radius: 12px;
  font-size: 13px;
  background: var(--bg);
  color: var(--t);
  outline: none;
  margin-bottom: 12px;
  flex-shrink: 0;
  font-family: inherit;
}
.dbv-englist {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}
.dbv-enggroup {
  margin-top: 12px;
  margin-bottom: 4px;
  font-size: 11px;
  font-weight: 600;
  color: var(--t3);
  text-transform: uppercase;
}
.dbv-engpersonalhint {
  font-size: 10px;
  color: var(--t3);
  font-weight: 400;
  margin-left: 6px;
  text-transform: none;
}
.dbv-engitem {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  border: none;
  border-radius: 14px;
  cursor: pointer;
  background: var(--bg);
  transition: all 0.2s ease;
}
.dbv-engitem.on {
  background: rgba(0, 113, 227, 0.06);
}
.dbv-engitem input {
  width: 18px;
  height: 18px;
  cursor: pointer;
  accent-color: #0071e3;
}
.dbv-engbody {
  flex: 1;
  min-width: 0;
}
.dbv-engname {
  font-weight: 600;
  font-size: 13px;
  color: var(--t);
  display: flex;
  align-items: center;
  flex-wrap: wrap;
}
.dbv-engalias {
  font-size: 10px;
  color: var(--t3);
  font-weight: 400;
  margin-left: 4px;
}
.dbv-engmeta {
  font-size: 10px;
  color: var(--t3);
  margin-top: 3px;
}
.dbv-engtag {
  font-size: 9px;
  padding: 2px 7px;
  border-radius: 999px;
  color: var(--on-accent);
  margin-left: 4px;
  font-weight: 500;
}
.dbv-engstatus {
  font-size: 11px;
  font-weight: 500;
  flex-shrink: 0;
}
.dbv-engfoot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
  flex-shrink: 0;
}
/* 复制/删除弹窗 */
.dbv-copymodal {
  background: var(--c);
  border-radius: 18px;
  padding: 28px;
  min-width: 320px;
  max-width: 400px;
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.15);
}
.dbv-copymodal h3 {
  margin: 0 0 12px;
  font-size: 17px;
  font-weight: 600;
  color: var(--t);
}
.dbv-copy-hint {
  margin: 0 0 12px;
  font-size: 13px;
  color: var(--t3);
  line-height: 1.5;
}
.dbv-copy-lbl {
  display: block;
  font-size: 12px;
  font-weight: 600;
  color: var(--t3);
  margin-bottom: 6px;
}
.dbv-copy-input {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--hairline);
  border-radius: 12px;
  font-size: 13px;
  outline: none;
  background: var(--bg);
  margin-bottom: 20px;
  box-sizing: border-box;
  font-family: inherit;
  color: var(--t);
}
.dbv-copy-btns {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.dbv-reveal-err {
  color: #ff3b30;
  font-size: 12px;
  margin: -8px 0 12px;
  line-height: 1.4;
}
/* Issue #501: 场景列表检索 + 分页 */
.dbv-scenehead-right {
  display: flex;
  gap: 8px;
  align-items: center;
}
.dbv-scenesearch {
  position: relative;
}
.dbv-scenesearch-input {
  width: 180px;
  padding: 7px 26px 7px 12px;
  border: 1px solid var(--hairline);
  border-radius: 999px;
  background: var(--bg);
  font-size: 12px;
  outline: none;
  color: var(--t);
  font-family: inherit;
}
.dbv-scenesearch-input:focus {
  border-color: #0071e3;
  background: var(--c);
}
.dbv-scenesearch-clear {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  border: none;
  background: transparent;
  cursor: pointer;
  color: var(--t3);
  font-size: 12px;
  line-height: 1;
}
.dbv-scenepager {
  display: flex;
  gap: 8px;
  align-items: center;
  justify-content: flex-end;
  padding: 10px 16px 0;
  font-size: 12px;
  color: var(--t3);
}
/* Issue #461: 场景访问地址地球仪三态 */
.dbv-deployglobe {
  margin-top: 4px;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.dbv-globe-link {
  font-size: 16px;
  line-height: 29px;
  text-decoration: none;
  cursor: pointer;
}
.dbv-globe-empty {
  border: 1px solid var(--hairline);
  background: var(--bg);
  border-radius: 999px;
  width: 29px;
  height: 29px;
  padding: 0;
  cursor: pointer;
  color: #c7c7cc;
  font-size: 14px;
  line-height: 1;
}
.dbv-globe-empty:hover {
  color: var(--t3);
  border-color: #d2d2d7;
}
.dbv-deployurl-input {
  width: 200px;
  padding: 5px 10px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  background: var(--bg);
  font-size: 12px;
  outline: none;
  font-family: inherit;
  color: var(--t);
}
.dbv-deployurl-input:focus {
  border-color: #0071e3;
  background: var(--c);
}
.bt-mini {
  padding: 2px 10px;
  font-size: 11px;
}
/* 推荐规则弹窗 */
.dbv-recmodal {
  width: 680px;
  max-height: 85vh;
  overflow-y: auto;
  background: color-mix(in srgb, var(--c) 92%, transparent);
  backdrop-filter: blur(28px) saturate(200%);
  border-radius: 18px;
  border: 1px solid var(--hairline);
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.12);
  padding: 28px;
}
.dbv-rec-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.dbv-rec-head h2 {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
}
.dbv-rec-sec {
  margin-bottom: 20px;
}
.dbv-rec-sep {
  border-top: 1px solid rgba(0, 0, 0, 0.06);
  padding-top: 16px;
}
.dbv-rec-t {
  font-size: 13px;
  font-weight: 600;
  color: var(--t);
  margin-bottom: 8px;
}
.dbv-rec-sel {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  font-size: 13px;
  background: var(--bg);
  color: var(--t);
  outline: none;
  cursor: pointer;
  font-family: inherit;
}
.dbv-rec-hint {
  font-size: 11px;
  color: var(--t3);
  margin-top: 4px;
}
.dbv-rec-rules {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.dbv-rec-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.dbv-rec-rowsel {
  flex: 0 0 200px;
  padding: 7px 12px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  font-size: 13px;
  background: var(--bg);
  outline: none;
  cursor: pointer;
  font-family: inherit;
  color: var(--t);
}
.dbv-rec-rowinput {
  flex: 1;
  padding: 7px 12px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  font-size: 12px;
  outline: none;
  background: var(--bg);
  font-family: inherit;
  color: var(--t);
}
</style>
