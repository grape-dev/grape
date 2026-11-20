<template>
  <!-- 单一根元素：fragment 根 + 根级 Teleport(AGModal) 组合会让 Vue unmount 遍历 anchor 撞 null -->
  <div class="sm">
    <!-- ═══ 市场主页 ═══ -->
    <template v-if="!detailId">
      <!-- 一级 Tab：场景 / 技能 -->
      <div class="sm__top">
        <div class="seg-bar sm__seg">
          <a class="ac">🏪 {{ tt('scenes_tab') }}</a>
          <a @click="G.app.nav('skills')">⚙ {{ tt('skills_tab') }}</a>
        </div>
        <div class="sm__intro">
          <b class="sm__intro-b">{{ tt('scenes_tab') }}</b>
          {{ tt('market_intro_scene') }} ·
          <b class="sm__intro-b">{{ tt('skills_tab') }}</b>
          {{ tt('market_intro_skill') }}
        </div>
      </div>
      <div class="sm__body">
        <!-- 子 Tab：场景市场(official) -->
        <div class="sm__subtab">
          <div class="seg-bar sm__seg">
            <a class="ac">{{ tt('scene_scope_official') }}</a>
          </div>
        </div>

        <!-- 精选场景 -->
        <div v-if="featured.length" class="apple-card">
          <div class="cd-h">
            <h2>🌟 {{ tt('featured_scenes') }}</h2>
          </div>
          <div class="feat-row">
            <SceneCard
              v-for="s in featured"
              :key="'f' + s.id"
              :s="s"
              @open="openDetail(s.id)"
              @apply="onApply(s)"
              @experience="onExperience(s)"
              @like="onLike(s)"
            />
          </div>
        </div>

        <!-- 全部场景 -->
        <div class="apple-card">
          <div class="cd-h apple-search sm__search">
            <h2>
              {{ tt('all_scenes') }}
              <span class="sm__count">{{ total }}</span>
            </h2>
            <input
              v-model="qDraft"
              class="sm__q"
              :placeholder="tt('search_placeholder')"
              @keyup.enter="doSearch"
            />
            <button class="bt bt-p bt-s" @click="doSearch">{{ tt('query') }}</button>
            <button
              :class="['bt bt-s', sort === 'latest' ? 'bt-tg' : 'bt-o']"
              @click="setSort('latest')"
            >
              {{ tt('sort_latest') }}
            </button>
            <button :class="['bt bt-s', sort === 'hot' ? 'bt-tg' : 'bt-o']" @click="setSort('hot')">
              {{ tt('sort_hot') }}
            </button>
          </div>
          <div class="sm__catfilters">
            <select class="sm__pgsize" :value="activeL1" @change="onL1Change($event.target.value)">
              <option value="0">{{ tt('filter_all_l1') }}</option>
              <option v-for="c in catTree" :key="'l1-' + c.id" :value="c.id">
                {{ c.icon ? c.icon + ' ' : '' }}{{ c.name }}
              </option>
            </select>
            <select class="sm__pgsize" :value="activeL2" @change="onL2Change($event.target.value)">
              <option value="0">{{ tt('filter_all_l2') }}</option>
              <option v-for="c in l2Options" :key="'l2-' + c.id" :value="c.id">
                {{ c.icon ? c.icon + ' ' : '' }}{{ c.name }}
              </option>
            </select>
          </div>
          <div class="stabs">
            <a
              v-for="c in catList"
              :key="c.id"
              :class="{ ac: c.id === activeCat }"
              @click="setCat(c.id)"
              >{{ c.icon ? c.icon + ' ' : '' }}{{ c.name }}</a
            >
          </div>
          <div class="sm__gridwrap">
            <div v-if="listLoading" class="scn-loading"><span class="scn-spinner"></span></div>
            <div v-else-if="!scenes.length" class="ts t3 sm__empty">{{ tt('no_scenes') }}</div>
            <div v-else class="rgrid">
              <SceneCard
                v-for="s in scenes"
                :key="s.id"
                :s="s"
                @open="openDetail(s.id)"
                @apply="onApply(s)"
                @experience="onExperience(s)"
                @like="onLike(s)"
              />
            </div>
            <!-- 分页器 -->
            <div v-if="scenes.length" class="sm__pager">
              <button class="bt bt-o bt-s" :disabled="page <= 1" @click="goPage(page - 1)">
                ‹
              </button>
              <span class="sm__pginfo"
                >{{ page }} / {{ totalPages }} ({{ total }}{{ tt('items_count_suffix') }})</span
              >
              <button class="bt bt-o bt-s" :disabled="page >= totalPages" @click="goPage(page + 1)">
                ›
              </button>
              <select
                :value="pageSize"
                class="sm__pgsize"
                @change="setPageSize($event.target.value)"
              >
                <option value="20">20{{ tt('per_page_suffix') }}</option>
                <option value="50">50{{ tt('per_page_suffix') }}</option>
                <option value="100">100{{ tt('per_page_suffix') }}</option>
              </select>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- ═══ 场景详情页 ═══ -->
    <template v-else>
      <div v-if="detailLoading" class="loading">{{ tc('loading') }}</div>
      <div v-else-if="detailError" class="error">{{ tc('load_failed') }}: {{ detailError }}</div>
      <template v-else-if="detail">
        <AGButton class="sm__back" size="sm" variant="plain" @click="backToMarket"
          >← {{ tt('back_to_scene_market') }}</AGButton
        >

        <!-- 头部卡片 -->
        <div class="sm__dhead">
          <div class="sm__dhead-top">
            <div :class="['scn-ic', catBg(detail), 'sm__dic']">
              <img v-if="iconIsUrl(detail)" :src="detail.icon" alt="" @error="onIconErr" />
              <template v-else>{{ detail.icon || '⚙' }}</template>
            </div>
            <div class="sm__dhead-info">
              <div class="sm__dtitle-row">
                <h2>{{ detail.display_name || detail.name }}</h2>
                <span
                  v-if="detail.name && detail.name !== detail.display_name"
                  class="sm__dnamepill"
                  >{{ detail.name }}</span
                >
              </div>
              <div class="sm__dmeta-row">
                <span v-if="detail.category_name" class="tg tg-d sm__tg10">{{
                  detail.category_name
                }}</span>
                <span v-if="isOfficial && detail.is_featured" class="tg sm__tgfeat"
                  >🌟 {{ tt('featured_tag') }}</span
                >
                <span v-if="detail.version" class="tg tg-d sm__tg9">v{{ detail.version }}</span>
              </div>
              <div class="sm__ddesc">
                {{ detail.market_description || detail.description || '' }}
              </div>
            </div>
          </div>
          <div class="sm__dfoot">
            <template v-if="isOfficial">
              <span class="sm__dlike" @click="onLike(detail)">
                {{ detail.liked_by_me ? '❤️' : '🤍' }} {{ detail.like_count || 0 }}
              </span>
              <span class="sm__drepos">{{ detail.repo_count || 0 }} {{ tt('repos_in_use') }}</span>
            </template>
            <div class="sm__dactions">
              <AGButton variant="plain" size="sm" @click="onApply(detail)">{{
                tt('apply_scene')
              }}</AGButton>
              <AGButton
                v-if="detail.recommended_template"
                variant="plain"
                size="sm"
                @click="onExperience(detail)"
                >{{ tt('experience') }}</AGButton
              >
            </div>
          </div>
        </div>

        <!-- 执行流程 -->
        <template v-if="detailSkills.length">
          <div class="cd-h sm__sech">
            <h2>{{ tt('execution_flow') }}</h2>
          </div>
          <div class="sm__flow-wrap">
            <div class="sm__flow-grid">
              <template v-for="(sk, i) in detailSkills" :key="'f' + i">
                <div :class="['sm__fcard', { 'sm__fcard--del': sk.skill_deleted }]">
                  <div
                    class="sm__fcard-top"
                    :style="{ background: sk.skill_deleted ? 'var(--r)' : agentMeta(sk).color }"
                  ></div>
                  <div class="sm__fcard-h">
                    <span class="sm__fnum" :style="numStyle(sk)">{{ i + 1 }}</span>
                    <span class="sm__flabel"
                      >{{ agentMeta(sk).icon }} {{ agentMeta(sk).label }}</span
                    >
                    <span v-if="sk.skill_deleted" class="sm__fdel">⚠ {{ td('deleted') }}</span>
                  </div>
                  <div class="sm__fdesc" :title="sk.role_desc || ''">{{ sk.role_desc || '' }}</div>
                  <div class="sm__fsep"></div>
                  <div class="sm__fbox">
                    <div class="sm__flbl">{{ td('label_skill') }}</div>
                    <span v-if="sk.skill_deleted" class="sm__fval sm__fval--del">{{
                      skillName(sk)
                    }}</span>
                    <span
                      v-else
                      class="sm__fval sm__fval--skill"
                      @click="G.app.nav('skill', sk.skill_id || '')"
                      >{{ skillName(sk) }}</span
                    >
                  </div>
                  <div class="sm__feng">
                    <div class="sm__flbl">{{ td('label_engine') }}</div>
                    <span class="sm__feng-pill">{{ td('broadcast') }}</span>
                  </div>
                </div>
                <span v-if="i < detailSkills.length - 1" class="sm__farrow">→</span>
              </template>
            </div>
          </div>
        </template>

        <!-- 技能套件 -->
        <template v-if="detailSkills.length">
          <div class="cd-h sm__sech2">
            <h2>{{ tt('skill_suite') }}</h2>
          </div>
          <div class="sm__flow-wrap">
            <div class="sm__suite-grid">
              <div
                v-for="(sk, i) in detailSkills"
                :key="'s' + i"
                :class="['sm__scard', { 'sm__scard--del': sk.skill_deleted }]"
                @click="!sk.skill_deleted && G.app.nav('skill', sk.skill_id || '')"
              >
                <div
                  class="sm__snum"
                  :style="{ background: sk.skill_deleted ? 'var(--r)' : agentMeta(sk).color }"
                >
                  {{ i + 1 }}
                </div>
                <div class="sm__sbody">
                  <div class="sm__sname">
                    {{ agentMeta(sk).icon }} {{ skillName(sk) }}
                    <span v-if="sk.skill_deleted" class="sm__fdel">⚠ {{ td('deleted') }}</span>
                  </div>
                  <div class="sm__srole" :title="sk.role_desc || agentMeta(sk).label">
                    {{ sk.role_desc || agentMeta(sk).label }}
                  </div>
                </div>
                <span class="sm__sbadge" :style="badgeStyle(sk)">{{ agentMeta(sk).label }}</span>
              </div>
            </div>
          </div>
        </template>

        <!-- 场景参数 -->
        <template v-if="presetFields.length">
          <div class="cd-h sm__sech2">
            <h2>{{ tt('scene_params') }}</h2>
            <span class="sm__param-hint">{{ tt('scene_params_hint') }}</span>
          </div>
          <div class="sm__flow-wrap">
            <div class="sm__param-list">
              <div v-for="p in presetFields" :key="p.name" class="sm__param-row">
                <div class="sm__param-info">
                  <div class="sm__param-name-row">
                    <code class="sm__param-name">{{ p.name }}</code>
                    <span v-if="p.is_secret" class="sm__pill sm__pill--sec"
                      >🔒 {{ td('secret') }}</span
                    >
                    <span class="sm__pill">{{ scopeText(p) }}</span>
                    <span v-if="p.default_value" class="sm__param-dv"
                      >{{ td('default') }}: {{ p.default_value }}</span
                    >
                  </div>
                  <div v-if="p.description" class="sm__param-desc">{{ p.description }}</div>
                </div>
              </div>
            </div>
          </div>
        </template>

        <!-- 样例项目模板 -->
        <template v-if="detail.recommended_template && isOfficial">
          <div class="cd-h sm__sech2">
            <h2>{{ tt('sample_template') }}</h2>
          </div>
          <div
            :class="['sm__flow-wrap', { 'sm__tpl--remote': tplIsRemote }]"
            @click="tplIsRemote && openTpl()"
          >
            <div class="sm__tpl-row">
              <span class="sm__tpl-ic">{{ tpl.icon || '📦' }}</span>
              <div class="sm__tpl-info">
                <div class="sm__tpl-name-row">
                  <span class="sm__tpl-name">{{
                    tpl.name || detail.recommended_template_name || detail.recommended_template
                  }}</span>
                  <span v-if="tplIsRemote" class="sm__pill sm__pill--ok">{{
                    tt('gitcode_remote')
                  }}</span>
                  <span v-else class="sm__pill">{{ tt('local_template') }}</span>
                  <span v-if="tpl.language" class="sm__pill sm__pill--lang">{{
                    tpl.language
                  }}</span>
                  <span v-if="tpl.stars" class="sm__tpl-stars">⭐ {{ tpl.stars }}</span>
                </div>
                <div class="sm__tpl-desc">{{ tpl.description || tt('template_desc') }}</div>
              </div>
              <span v-if="tplIsRemote" class="sm__tpl-view">{{ tt('view_project') }} ↗</span>
            </div>
          </div>
        </template>
      </template>
    </template>

    <!-- ═══ 应用场景弹窗（高级选项） ═══ -->
    <AGModal :open="applyCtx.open" :title="tt('apply_scene')" @close="applyCtx.open = false">
      <div class="sm__form">
        <!-- 场景名称（weekly-09：每次应用可编辑，预填模板名） -->
        <div class="sm__fld">
          <label class="sm__lbl">{{ tt('apply_scene_name_label') }}</label>
          <input
            v-model="applyCtx.sceneName"
            class="sm__scenename"
            maxlength="50"
            :placeholder="tt('apply_scene_name_label')"
            @input="applyCtx.sceneNameError = ''"
          />
          <div v-if="applyCtx.sceneNameError" class="sm__nameerr">
            {{ applyCtx.sceneNameError }}
          </div>
        </div>
        <!-- 华为云提示 -->
        <div v-if="applyCtx.isHwCloud" class="sm__hwhint">
          ☁ {{ td('hw_hint') }}
          <a class="sm__hwlink" @click="G.app.nav('settings')">{{ td('hw_goto_settings') }}</a>
        </div>
        <!-- 工作分支 -->
        <div class="sm__fld">
          <label class="sm__lbl">{{ td2('dashboard.work_branch') }}</label>
          <div class="sm__row">
            <AGSelect v-model="applyCtx.workBranch" :options="branchOpts" class="sm__f1" />
            <AGButton size="sm" @click="applyCtx.showNewBranch = !applyCtx.showNewBranch">{{
              td2('dashboard.create_branch')
            }}</AGButton>
          </div>
          <div v-if="applyCtx.showNewBranch" class="sm__newbranch">
            <input
              v-model="applyCtx.newBranchName"
              class="sm__nbname"
              :placeholder="td2('dashboard.branch_name')"
            />
            <AGSelect v-model="applyCtx.newBranchRef" :options="branchRefOpts" class="sm__nbref" />
            <AGButton size="sm" @click="createBranch">{{ td2('common.create') }}</AGButton>
          </div>
        </div>
        <!-- 自定义配置项 -->
        <div class="sm__fld">
          <label class="sm__lbl">{{ td2('dashboard.custom_config') }}</label>
          <div class="sm__cf-list">
            <div v-for="(f, i) in applyCtx.fields" :key="i" class="sm__cf">
              <span v-if="f.preset" class="sm__cf-key-static">
                <b>{{ f.name }}</b>
                <small v-if="f.description">{{ f.description }}</small>
              </span>
              <AGInput v-else v-model="f.name" class="sm__f1" :invalid="cfNameError(f)" />
              <div class="sm__cf-val">
                <input
                  v-model="f.value"
                  :type="f.reveal ? 'text' : fInputType(f)"
                  :placeholder="td('value_ph')"
                />
                <button
                  v-if="fInputType(f) === 'password'"
                  class="sm__cf-eye"
                  type="button"
                  @click="f.reveal = !f.reveal"
                >
                  {{ f.reveal ? '🙈' : '👁' }}
                </button>
              </div>
              <div v-if="!f.preset && !f.readOnlyKey" class="sm__cf-scope">
                <label v-for="a in scopeAgents" :key="a" class="sm__cf-scope-lbl">
                  <input
                    v-model="f.scope"
                    type="checkbox"
                    :value="a"
                    @change="scopeToggle(f, a)"
                  />{{ a }}
                </label>
              </div>
              <AGButton
                v-if="!f.preset"
                variant="danger"
                size="sm"
                @click="applyCtx.fields.splice(i, 1)"
                >×</AGButton
              >
            </div>
          </div>
          <AGButton size="sm" variant="plain" @click="addField()"
            >+ {{ td2('dashboard.add_config') }}</AGButton
          >
        </div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="applyCtx.open = false">{{
          td2('common.cancel')
        }}</AGButton>
        <AGButton size="sm" :disabled="applyCtx.busy" @click="confirmApply">
          {{ tt('apply_scene') }}
        </AGButton>
      </template>
    </AGModal>

    <!-- ═══ 场景名称冲突弹窗 ═══ -->
    <AGModal
      :open="conflict.open"
      :title="tt('scene_name_conflict_title')"
      @close="conflict.open = false"
    >
      <div class="sm__conf">
        <span class="sm__warn-ic">⚠️</span>
        <div>
          <div class="sm__conf-desc">{{ conflict.message || tt('scene_name_conflict_desc') }}</div>
        </div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="conflict.open = false">{{
          td2('common.close')
        }}</AGButton>
        <AGButton size="sm" @click="gotoRepoScenes">{{ tt('goto_repo_scenes') }}</AGButton>
      </template>
    </AGModal>

    <!-- ═══ 体验确认弹窗 ═══ -->
    <AGModal
      :open="expConfirm.open"
      :title="
        expConfirm.applied ? tt('exp_confirm_overwrite_title') : tt('exp_confirm_exists_title')
      "
      @close="expConfirm.open = false"
    >
      <div class="sm__conf">
        <span class="sm__warn-ic">⚠️</span>
        <div class="sm__conf-desc">
          {{
            expConfirm.applied ? tt('exp_confirm_overwrite_desc') : tt('exp_confirm_exists_desc')
          }}
          {{ expConfirm.fullName }}
          {{
            expConfirm.applied
              ? tt('exp_confirm_overwrite_warning')
              : tt('exp_confirm_exists_warning')
          }}
        </div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="expConfirm.open = false">{{
          td2('common.cancel')
        }}</AGButton>
        <AGButton size="sm" @click="doExperience">{{ tt('exp_confirm_btn') }}</AGButton>
      </template>
    </AGModal>

    <!-- ═══ 体验进度弹窗 ═══ -->
    <AGModal :open="expModal.open" :title="tt('experience')" @close="cancelExperience">
      <div class="sm__exp">
        <div class="sm__exp-txt">{{ expModal.text }}</div>
        <div class="sm__exp-bar">
          <div class="sm__exp-fill" :style="{ width: expModal.pct + '%' }"></div>
        </div>
        <div class="sm__exp-pct">{{ expModal.pct }}%</div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="cancelExperience">{{
          expModal.done ? td2('common.close') : td2('common.cancel')
        }}</AGButton>
      </template>
    </AGModal>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import SceneCard from './SceneCard.vue'
import AGButton from '@/components/ui/AGButton.vue'
import AGModal from '@/components/ui/AGModal.vue'
import AGInput from '@/components/ui/AGInput.vue'
import AGSelect from '@/components/ui/AGSelect.vue'

const G = globalThis

// ── I18N helpers（老壳 skills.* 命名空间为主）──
// 陷阱：t(key) 找不到返回 key 本身（truthy），|| 兜底不生效——必须与完整 key 比对
function tt(key) {
  const full = 'skills.' + key
  const v = G.I18N.t(full)
  return v && v !== full ? v : keyDefaults[key] || key
}
const keyDefaults = {
  scenes_tab: '场景',
  skills_tab: '技能',
  market_intro_scene: '是多个技能按开发流程组合的开发模式',
  market_intro_skill: '是单个 Agent 的能力包',
  scene_scope_official: '场景市场',
  featured_scenes: '精选场景',
  all_scenes: '全部场景',
  filter_all_l1: '全部一级分类',
  filter_all_l2: '全部二级分类',
  search_placeholder: '搜索场景名称或描述',
  query: '查询',
  sort_latest: '最新',
  sort_hot: '最热',
  no_scenes: '暂无场景',
  items_count_suffix: '条',
  per_page_suffix: '/页',
  all_categories: '全部',
  apply_scene: '应用',
  experience: '体验',
  back_to_scene_market: '返回场景市场',
  execution_flow: '执行流程',
  skill_suite: '技能套件',
  scene_params: '场景参数',
  scene_params_hint: '应用场景时需配置，Agent 将通过环境变量读取',
  sample_template: '样例项目模板',
  template_desc: '点击体验可基于此模板创建项目',
  gitcode_remote: 'GitCode 远程',
  local_template: '本地模板',
  view_project: '查看项目',
  featured_tag: '精选',
  repos_in_use: '仓库在用',
  skills_suffix: '技能',
  apply_confirm_title: '确认覆盖应用',
  apply_confirm_desc: '当前仓库已应用过此场景',
  apply_confirm_warning: '再次应用将覆盖更新已有的场景配置。',
  apply_confirm_btn: '确认覆盖',
  scene_name_conflict_title: '场景名称冲突',
  scene_name_conflict_desc: '仓库中已存在同名场景，请先修改已有场景名称',
  goto_repo_scenes: '去修改场景',
  exp_confirm_overwrite_title: '确认覆盖场景配置',
  exp_confirm_overwrite_desc: '项目',
  exp_confirm_overwrite_warning: '已存在且已应用过此场景，再次体验将覆盖已有的场景配置。',
  exp_confirm_exists_title: '项目已存在',
  exp_confirm_exists_desc: '项目',
  exp_confirm_exists_warning: '已存在，体验将在此项目上应用场景配置。',
  exp_confirm_btn: '继续体验',
}
// 详情页局部文案（老代码是硬编码中文，保持一致）
const D = {
  deleted: '已删除',
  label_skill: '技能',
  label_engine: '引擎',
  broadcast: '广播',
  secret: '密钥',
  default: '默认',
  hw_hint: '华为云场景需要 AK/SK 凭证。如已配置将自动填入，也可在此直接填写。',
  hw_goto_settings: '前往设置',
  value_ph: 'value',
}
function td(key) {
  return D[key]
}
// toast 类文案（skills.toast.*）——同样规避 I18N 返回 key 本身的陷阱
function tf(key, def) {
  const full = 'skills.' + key
  const v = G.I18N.t(full)
  return v && v !== full ? v : def
}
function tc(key) {
  return (
    G.I18N.t('common.' + key) ||
    (key === 'loading' ? '加载中...' : key === 'load_failed' ? '加载失败' : key)
  )
}
function td2(key) {
  const v = G.I18N.t(key)
  const map = {
    'dashboard.work_branch': '工作分支',
    'dashboard.create_branch': '新建分支',
    'dashboard.branch_name': '分支名',
    'common.create': '创建',
    'common.cancel': '取消',
    'common.close': '关闭',
    'dashboard.custom_config': '自定义配置',
    'dashboard.add_config': '添加配置项',
    'common.save': '保存',
  }
  return v && v !== key ? v : map[key] || key
}

// ── Agent 图标+颜色映射（与老 _AGENT_META 一致；分类底色用壳里 sc-bg 渐变类）──
const AGENT_META = {
  vod: { icon: '🔍', label: 'VOD', color: '#B07A8B' },
  issue: { icon: '📋', label: 'Issue', color: '#B8857A' },
  coding: { icon: '💻', label: 'Coding', color: '#7A92B0' },
  qa: { icon: '🧪', label: 'QA', color: '#9A85A8' },
  sre: { icon: '⚙️', label: 'SRE', color: '#B89B6A' },
  chat: { icon: '💬', label: 'Chat', color: 'var(--g)' },
  auto: { icon: '⏰', label: 'Auto', color: '#8E9BAA' },
}
const CAT_BG = {
  1: 'bg-default',
  2: 'bg-harmony',
  3: 'bg-iac',
  4: 'bg-skill',
  5: 'bg-agent',
  6: 'bg-blog',
}
function agentMeta(sk) {
  return AGENT_META[sk.tag] || AGENT_META[sk.agent_type] || {}
}
function catBg(s) {
  return CAT_BG[s.category_id] || 'bg-default'
}
function iconIsUrl(s) {
  const v = s.icon || ''
  return v.startsWith('http') || v.startsWith('/static/') || v.startsWith('/uploads/')
}
function onIconErr(e) {
  e.target.style.display = 'none'
  e.target.parentElement.innerText = e.target.dataset.fallback || '⚙'
}
function skillName(sk) {
  return sk.display_name || sk.name || sk.skill || ''
}
// 数字圆点/徽章配色：老代码 bgColor+'22' 十六进制拼 alpha，删除态用 var(--r)22 是非法 CSS（坑#5）
// 这里统一改 color-mix，明暗主题都安全
function numStyle(sk) {
  const bg = sk.skill_deleted ? 'var(--r)' : agentMeta(sk).color || 'var(--a)'
  return { background: 'color-mix(in srgb, ' + bg + ' 13%, transparent)', color: bg }
}
function badgeStyle(sk) {
  const bg = sk.skill_deleted ? 'var(--r)' : agentMeta(sk).color || 'var(--a)'
  return { background: 'color-mix(in srgb, ' + bg + ' 9%, transparent)', color: bg }
}

// ── 市场列表状态 ──
const cats = ref([])
const catTree = ref([])
const activeL1 = ref(0)
const activeL2 = ref(0)
const l2Options = computed(function () {
  var hit = catTree.value.filter(function (c) { return String(c.id) === String(activeL1.value) })[0]
  return (hit && hit.children) || []
})
const featured = ref([])
const scenes = ref([])
const total = ref(0)
const activeCat = ref(0)
const qDraft = ref('')
const q = ref('')
const sort = ref('latest')
const page = ref(1)
const pageSize = ref(20)
const listLoading = ref(false)
const catList = computed(() => [{ id: 0, name: tt('all_categories') }].concat(cats.value))
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))

function loadList() {
  listLoading.value = true
  let qs =
    '?sort=' +
    sort.value +
    '&page=' +
    page.value +
    '&page_size=' +
    pageSize.value +
    '&scope=official'
  if (q.value) qs += '&q=' + encodeURIComponent(q.value)
  if (activeCat.value) qs += '&category_id=' + activeCat.value
  if (activeL1.value) qs += '&cat_l1_id=' + activeL1.value
  if (activeL2.value) qs += '&cat_l2_id=' + activeL2.value
  G.API.get('/scenes/market' + qs)
    .then(function (d) {
      if (stale()) return
      scenes.value = d.scenes || []
      total.value = d.total || 0
      listLoading.value = false
    })
    .catch(function (e) {
      if (stale() || (e && e.name === 'AbortError' && e.silent)) return
      scenes.value = []
      total.value = 0
      listLoading.value = false
    })
}
function stale() {
  return G.window._currentView !== 'scene-market:official'
}

function loadMarket() {
  scenes.value = []
  total.value = 0
  featured.value = []
  listLoading.value = true
  G.window._currentView = 'scene-market:official'
  if (!cats.value.length) {
    G.API.get('/scenes/market/categories')
      .then(function (d) {
        if (!stale()) {
          cats.value = d.categories || []
          catTree.value = d.tree || []
        }
      })
      .catch(function () {})
  }
  G.API.get('/scenes/market?sort=latest&limit=4&featured_only=1&scope=official')
    .then(function (d) {
      if (!stale()) featured.value = d.scenes || []
    })
    .catch(function () {
      if (!stale()) featured.value = []
    })
  loadList()
}
function doSearch() {
  q.value = qDraft.value
  page.value = 1
  loadList()
}
function setSort(s) {
  sort.value = s
  page.value = 1
  loadList()
}
function setCat(c) {
  activeCat.value = c
  page.value = 1
  loadList()
}
function onL1Change(v) {
  activeL1.value = parseInt(v, 10) || 0
  activeL2.value = 0
  page.value = 1
  loadList()
}
function onL2Change(v) {
  activeL2.value = parseInt(v, 10) || 0
  page.value = 1
  loadList()
}
function goPage(p) {
  page.value = Math.min(totalPages.value, Math.max(1, p))
  loadList()
}
function setPageSize(v) {
  pageSize.value = parseInt(v, 10)
  page.value = 1
  loadList()
}

const props = defineProps({ initialDetailId: { type: String, default: '' } })

// ── 详情页状态 ──
const detailId = ref(props.initialDetailId || null)
const detail = ref(null)
const detailLoading = ref(false)
const detailError = ref('')
const isOfficial = computed(
  () => (detail.value && detail.value.scope) === 'official' || !detail.value
)
const detailSkills = computed(() => (detail.value && detail.value.skills) || [])
const tpl = computed(() => (detail.value && detail.value.template) || {})
const tplIsRemote = computed(() => tpl.value.source === 'git' && tpl.value.source_repo)
const presetFields = computed(function () {
  var raw = detail.value && detail.value.preset_custom_fields
  if (!raw) return []
  if (typeof raw === 'string') {
    try {
      return JSON.parse(raw || '[]') || []
    } catch (e) {
      return []
    }
  }
  return Array.isArray(raw) ? raw : []
})
const SCOPE_LABELS = {
  vod: 'VOD',
  issue: 'Issue',
  coding: 'Coding',
  qa: 'QA',
  sre: 'SRE',
  all: '全部',
}
function scopeText(p) {
  var sc = Array.isArray(p.scope) ? p.scope : [p.scope || 'all']
  return sc
    .map(function (x) {
      return SCOPE_LABELS[x] || x
    })
    .join(' / ')
}
function openTpl() {
  if (tplIsRemote.value) G.window.open('https://gitcode.com/' + tpl.value.source_repo, '_blank')
}

function openDetail(id) {
  G.window._currentView = 'scene-detail'
  detailId.value = String(id)
  detailLoading.value = true
  detailError.value = ''
  loadDetailData(detailId.value)
}
function loadDetailData(id) {
  detail.value = null
  G.API.get('/scenes/market/' + id)
    .then(function (d) {
      if (G.window._currentView !== 'scene-detail') return
      detail.value = d
      detailLoading.value = false
    })
    .catch(function (e) {
      if (G.window._currentView !== 'scene-detail') return
      detailError.value = e.message || ''
      detailLoading.value = false
    })
}
function backToMarket() {
  G.app.nav('scene-market')
}

// renderSceneDetail 全局契约由入口 entries/scene-market.js 持有（fresh mount + initialDetailId），
// 组件内不再覆盖——旧实例闭包在 unmount 后不会更新 DOM

// ── 点赞 ──
function onLike(s) {
  G.API.post('/scenes/market/' + s.id + '/like', {})
    .then(function (r) {
      s.liked_by_me = !!r.liked
      s.like_count = r.like_count || 0
    })
    .catch(function () {
      G.app.showToast(tf('toast.like_failed', '操作失败'))
    })
}

// ── 应用场景（高级选项弹窗）──
const applyCtx = reactive({
  open: false,
  sceneId: 0,
  sceneName: '',
  sceneNameError: '',
  isHwCloudScene: false,
  owner: '',
  repoName: '',
  workBranch: '',
  branches: [],
  showNewBranch: false,
  newBranchName: '',
  newBranchRef: '',
  fields: [],
  busy: false,
})
const branchOpts = computed(() =>
  applyCtx.branches.map(function (b) {
    return { value: b.name, label: b.name + (b.protected ? ' 🔒' : '') }
  })
)
const branchRefOpts = computed(() =>
  applyCtx.branches.map(function (b) {
    return { value: b.name, label: b.name }
  })
)
const scopeAgents = ['all', 'vod', 'issue', 'coding', 'qa', 'sre']

function currentRepoFullName() {
  try {
    var c = G.app.currentRepo()
    return c ? c.fullName || '' : ''
  } catch (e) {
    return ''
  }
}
function onApply(s) {
  var repo = currentRepoFullName()
  if (!repo) {
    G.app.showToast(tf('toast.select_repo_first', '请先选择仓库'))
    return
  }
  var parts = repo.split('/')
  var owner = encodeURIComponent(parts[0])
  var repoName = encodeURIComponent(parts[1])
  var skillsArr =
    s.skills && s.skills.length
      ? s.skills.map(function (x) {
          return x.name || x.skill || x
        })
      : typeof s.skills === 'string' && s.skills
        ? s.skills.split(',')
        : []
  // weekly-09 修正：按技能集判断华为云场景（_applyScene 第二参已改 display_name，不能再比 name）
  var isHw = skillsArr.indexOf('huawei-cloud') >= 0
  // weekly-09 修订：删除「已应用预检」——每次应用都是全新场景（后端 custom-{uuid}），
  // 同一用户基于同一模板建多个场景是正当需求，没有覆盖语义
  showApplyModal(s, owner, repoName, s.display_name || s.name || '', isHw)
}
function showApplyModal(s, owner, repoName, sceneName, isHwCloudScene) {
  applyCtx.sceneId = s.id
  applyCtx.owner = owner
  applyCtx.repoName = repoName
  applyCtx.sceneName = sceneName || ''
  applyCtx.sceneNameError = ''
  applyCtx.isHwCloudScene = !!isHwCloudScene
  applyCtx.workBranch = 'main' // 老代码语义：默认选中 main，分支列表回来后校正（含 catch 兜底，勿留空——select :value 空值匹配不到 option 会显示空白）
  applyCtx.branches = [{ name: 'main' }]
  applyCtx.showNewBranch = false
  applyCtx.newBranchName = ''
  applyCtx.newBranchRef = ''
  applyCtx.fields = []
  applyCtx.busy = false
  applyCtx.open = true
  // 加载分支列表
  G.API.get('/scenes/branches/' + owner + '/' + repoName)
    .then(function (b) {
      if (!applyCtx.open) return
      applyCtx.branches = b && b.branches && b.branches.length ? b.branches : [{ name: 'main' }]
      applyCtx.workBranch = applyCtx.branches.some(function (x) {
        return x.name === 'main'
      })
        ? 'main'
        : applyCtx.branches[0].name
    })
    .catch(function () {
      // 分支列表拉取失败（如仓库 bot token 失效 502）：保留 main 兜底且必须选中，否则下拉显示空白
      applyCtx.branches = [{ name: 'main' }]
      applyCtx.workBranch = 'main'
    })
  // 华为云场景：注入 AK/SK 只读配置项
  if (isHwCloudScene) {
    G.API.get('/settings/credentials')
      .then(function (credData) {
        var hw = (credData && credData.huawei_cloud) || {}
        addField('HUAWEICLOUD_SDK_AK', hw.ak || '', { readOnlyKey: true })
        addField('HUAWEICLOUD_SDK_SK', hw.sk || '', { readOnlyKey: true })
      })
      .catch(function () {
        addField('HUAWEICLOUD_SDK_AK', '', { readOnlyKey: true })
        addField('HUAWEICLOUD_SDK_SK', '', { readOnlyKey: true })
      })
  }
  // weekly-09：无覆盖语义——每次应用都是全新场景，不再回填旧高级选项
}
function addField(name, value, opts) {
  opts = opts || {}
  var isSec = cfComputeIsSecret(name, opts.isSecret)
  applyCtx.fields.push({
    name: name || '',
    value: value || '',
    readOnlyKey: !!opts.readOnlyKey,
    preset: !!opts.preset,
    isSecret: isSec,
    reveal: false,
    scope: Array.isArray(opts.scope) ? opts.scope.slice() : [opts.scope || 'all'],
    description: opts.description || '',
  })
}
function fInputType(f) {
  return f.readOnlyKey || f.isSecret ? 'password' : 'text'
}
function cfNameError(f) {
  return !f.preset && G._cfNameError ? G._cfNameError(f.name) : ''
}
function scopeToggle(f, a) {
  if (a === 'all') {
    if (f.scope.indexOf('all') >= 0) f.scope = ['all']
  } else {
    var i = f.scope.indexOf('all')
    if (i >= 0) f.scope.splice(i, 1)
  }
}
function cfComputeIsSecret(name, isSecret) {
  if (isSecret === true) return true
  if (isSecret === false) return false
  var up = (name || '').trim().toUpperCase()
  return ['_KEY', '_SK', '_SECRET', '_TOKEN', '_PASSWORD', '_PWD', '_CREDENTIALS'].some(
    function (s) {
      return up.endsWith(s)
    }
  )
}
function createBranch() {
  var branchName = (applyCtx.newBranchName || '').trim()
  var refs = applyCtx.newBranchRef || 'main'
  if (!branchName) {
    G.app.showToast(td2('dashboard.branch_name_empty') || '分支名不能为空')
    return
  }
  G.API.post('/scenes/branches/' + applyCtx.owner + '/' + applyCtx.repoName, {
    branch_name: branchName,
    refs: refs,
  })
    .then(function () {
      G.app.showToast(td2('dashboard.branch_created') || '分支已创建')
      return G.API.get('/scenes/branches/' + applyCtx.owner + '/' + applyCtx.repoName)
    })
    .then(function (b) {
      applyCtx.branches = (b && b.branches) || []
      applyCtx.workBranch = branchName
      applyCtx.newBranchName = ''
      applyCtx.showNewBranch = false
    })
    .catch(function (e) {
      G.app.showToast((e && e.detail && e.detail.message) || (e && e.message) || '创建失败')
    })
}
function confirmApply() {
  if (!applyCtx.workBranch) {
    G.app.showToast(td2('dashboard.branch_required') || '工作分支不能为空')
    return
  }
  // 收集校验 custom fields（复用老壳共享校验器）
  for (var i = 0; i < applyCtx.fields.length; i++) {
    var f = applyCtx.fields[i]
    if (!f.name) continue
    if (!f.preset && G._cfNameError) {
      var err = G._cfNameError(f.name)
      if (err) {
        G.app.showToast(err + '（第 ' + (i + 1) + ' 行）')
        return
      }
    }
  }
  var fields = applyCtx.fields
    .filter(function (f) {
      return (f.name || '').trim()
    })
    .map(function (f) {
      return {
        name: f.name.trim(),
        value: (f.value || '').trim(),
        is_secret: f.readOnlyKey || f.isSecret ? true : fInputType(f) === 'password',
        scope: f.scope.length ? f.scope : ['all'],
        preset: !!f.preset,
      }
    })
  // weekly-09：场景名称必填（空名/撞名时弹窗保留，改名重试）
  var sceneDisplayName = (applyCtx.sceneName || '').trim()
  if (!sceneDisplayName) {
    applyCtx.sceneNameError = tt('apply_scene_name_empty')
    return
  }
  applyCtx.busy = true
  applyCtx.open = false
  doApplyScene(
    applyCtx.sceneId,
    applyCtx.owner,
    applyCtx.repoName,
    applyCtx.workBranch,
    fields,
    sceneDisplayName
  )
}
function doApplyScene(sceneId, owner, repoName, workBranch, customFields, sceneDisplayName) {
  var repoFull = decodeURIComponent(owner) + '/' + decodeURIComponent(repoName)
  var reqBody = sceneDisplayName ? { display_name: sceneDisplayName } : {}
  G.API.post('/scenes/market/' + sceneId + '/apply?owner=' + owner + '&repo=' + repoName, reqBody)
    .then(function (r) {
      var sceneIdLocal = r.scene_id
      var navAfter = function (extraMsg) {
        G.app.showToast((r.message || tf('toast.apply_success', '场景已应用')) + (extraMsg || ''))
        var curFull = currentRepoFullName()
        if (curFull !== repoFull) {
          selectRepoAndNav(repoFull).then(function () {
            G.app.nav('scene-detail-local', sceneIdLocal)
          })
        } else {
          G.app.nav('scene-detail-local', sceneIdLocal)
        }
      }
      if (!sceneIdLocal) {
        G.app.showToast(r.message || tf('toast.apply_success', '场景已应用'))
        G.app.nav('dash')
        return null
      }
      return G.API.put(
        '/scenes/' +
          encodeURIComponent(sceneIdLocal) +
          '/advanced-options?repo=' +
          encodeURIComponent(repoFull),
        {
          work_branch: workBranch,
          custom_fields: customFields,
        }
      )
        .then(function () {
          navAfter('')
        })
        .catch(function (e) {
          console.warn('Advanced options save failed:', e)
          navAfter('（高级选项保存失败）')
        })
    })
    .catch(function (e) {
      // weekly-09：失败时弹窗保留，用户改名重试（409 撞名直接落在名称输入框下方）
      applyCtx.busy = false
      applyCtx.open = true
      var msg = (e && (e.message || (e.detail && e.detail.message))) || ''
      if (e && e.status === 409) {
        applyCtx.sceneNameError = msg || tt('apply_scene_name_conflict')
        return
      }
      G.app.showToast(tf('toast.apply_failed', '应用失败') + ': ' + msg)
    })
}
function selectRepoAndNav(fullName) {
  return G.API.get('/repos/managed')
    .then(function (d) {
      var repos = d.repos || []
      // 统一走 shell 的仓库下拉渲染：组织仓库只显示「组织仓库」，普通仓库按平台分组。
      // 这里不能再手工拼 full_name，否则会把组织仓库的标签覆盖成真实仓库名。
      if (typeof G.app._updateSidebarRepos === 'function') {
        G.app._updateSidebarRepos(repos)
      }
      G.app.switchRepo(fullName)
    })
    .catch(function () {
      G.app.switchRepo(fullName)
    })
}

// ── 场景名称冲突 ──
const conflict = reactive({ open: false, message: '', fullName: '' })
function showConflict(message, fullName) {
  conflict.message = message || ''
  conflict.fullName = fullName || ''
  conflict.open = true
}
function gotoRepoScenes() {
  conflict.open = false
  if (conflict.fullName) selectRepoAndNav(conflict.fullName)
  else G.app.nav('dash')
}

// ── 场景体验 ──
const expConfirm = reactive({ open: false, applied: false, fullName: '', sceneId: 0 })
const expModal = reactive({ open: false, pct: 0, text: '', done: false })
let expTimer = null
const EXP_STEPS = [
  { pct: 10, text: 'templates.progress_checking', def: '检查环境...' },
  { pct: 25, text: 'templates.progress_creating_repo', def: '正在创建 GitCode 仓库...' },
  { pct: 50, text: 'templates.progress_downloading', def: '下载模板代码...' },
  { pct: 70, text: 'templates.progress_pushing', def: '推送代码到仓库...' },
  { pct: 85, text: 'templates.progress_hosting', def: '托管仓库...' },
  { pct: 95, text: 'templates.progress_applying_scene', def: '应用场景配置...' },
]
function stepText(st) {
  var v = G.I18N.t(st.text)
  return v && v !== st.text ? v : st.def
}
function onExperience(s) {
  G.API.get('/scenes/market/' + s.id + '/experience-check')
    .then(function (check) {
      if (check.repo_exists && check.scene_applied) showExpConfirm(s.id, check.full_name, true)
      else if (check.repo_exists) showExpConfirm(s.id, check.full_name, false)
      else doExperience(s.id)
    })
    .catch(function () {
      doExperience(s.id)
    })
}
function showExpConfirm(sceneId, fullName, applied) {
  expConfirm.sceneId = sceneId
  expConfirm.fullName = fullName || ''
  expConfirm.applied = !!applied
  expConfirm.open = true
}
function doExperience(sceneId) {
  expConfirm.open = false
  expModal.open = true
  expModal.done = false
  var step = 0
  function setProgress(pct, text) {
    expModal.pct = pct
    expModal.text = text
  }
  setProgress(EXP_STEPS[0].pct, stepText(EXP_STEPS[0]))
  step = 1
  clearInterval(expTimer)
  expTimer = setInterval(function () {
    if (step < EXP_STEPS.length) {
      setProgress(EXP_STEPS[step].pct, stepText(EXP_STEPS[step]))
      step++
    }
  }, 5000)
  G.API.post('/scenes/market/' + sceneId + '/experience', {}, 300000)
    .then(function (r) {
      clearInterval(expTimer)
      setProgress(100, G.I18N.t('templates.progress_done') || '完成')
      expModal.done = true
      if (r.message) G.app.showToast(r.message)
      var fullName = r.full_name || ''
      setTimeout(function () {
        expModal.open = false
        if (fullName) selectRepoAndNav(fullName)
      }, 800)
    })
    .catch(function (e) {
      clearInterval(expTimer)
      if (e && e.status === 409) {
        expModal.open = false
        showConflict(e.message, e.detail && e.detail.full_name)
      } else {
        setProgress(
          0,
          (G.I18N.t('templates.progress_failed') || '失败') + ': ' + ((e && e.message) || '')
        )
        expModal.done = true
        G.app.showToast(tf('toast.experience_failed', '体验失败') + ': ' + ((e && e.message) || ''))
      }
    })
}
function cancelExperience() {
  if (!expModal.done) clearInterval(expTimer)
  expModal.open = false
}
onBeforeUnmount(function () {
  clearInterval(expTimer)
})

onMounted(function () {
  if (detailId.value) {
    // renderSceneDetail 入口路径：直接进详情（须先设 _currentView，否则 stale 守卫会丢弃加载结果）
    G.window._currentView = 'scene-detail'
    detailLoading.value = true
    detailError.value = ''
    loadDetailData(detailId.value)
  } else {
    loadMarket()
  }
  // popstate 回放 scene-detail（#scene-detail/xx）时重进详情
  var onPop = function (e) {
    if (e.state && e.state.view === 'scene-detail' && e.state.param != null) {
      G.window._currentView = 'scene-detail'
      detailId.value = String(e.state.param)
      detailLoading.value = true
      loadDetailData(detailId.value)
    }
  }
  G.window.addEventListener('popstate', onPop)
  // app.js 每次 nav 都重挂载组件，无需自卸载 listener（壳整体换内容）
})
</script>

<style scoped>
.sm__top {
  margin-bottom: 20px;
  /* 2026-11-16：72% 玻璃叠在 --bg 页面上与背景同色，区块里的卡片没有轮廓；
     改实心亮面 + 发丝描边（与 dashboard 场景详情同口径）。 */
  background: var(--c);
  border: 1px solid var(--hairline);
  border-radius: 18px;
  padding: 24px;
  text-align: center;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}
.sm__seg {
  margin-top: 0;
}
.sm__intro {
  padding: 14px 0 0;
  font-size: 11px;
  color: var(--t3);
  line-height: 1.6;
}
.sm__intro-b {
  color: var(--a);
  font-weight: 600;
}
.sm__body {
  text-align: left;
}
.sm__subtab {
  margin-bottom: 20px;
}
.sm__count {
  font-size: 10px;
  font-weight: 400;
  color: var(--t2);
  background: var(--bg);
  padding: 2px 8px;
  border-radius: 8px;
  margin-left: 4px;
}
.sm__q {
  padding: 8px 14px;
  border: 1px solid var(--b);
  border-radius: 10px;
  font-size: 12px;
  outline: none;
  background: var(--bg);
  color: var(--t);
  transition: border-color 0.2s;
  width: 200px;
}
.sm__q:focus {
  border-color: var(--a);
  background: var(--c);
}
.sm__search {
  justify-content: flex-start;
}
.sm__empty {
  padding: 20px;
  text-align: center;
}
.sm__pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  margin-top: 16px;
  padding: 12px 0;
}
.sm__pginfo {
  font-size: 12px;
  color: var(--t2);
}
.sm__pgsize {
  padding: 4px 8px;
  border-radius: 999px;
  font-size: 11px;
  background: var(--bg);
  color: var(--t);
  outline: none;
}

/* ── 详情页 ── */
.sm__back {
  margin-bottom: 16px;
}
.sm__dhead {
  padding: 28px 32px;
  margin-bottom: 20px;
  border-radius: 18px;
  /* 2026-11-16：72% 玻璃叠在 --bg 页面上与背景同色，区块里的卡片没有轮廓；
     改实心亮面 + 发丝描边（与 dashboard 场景详情同口径）。 */
  background: var(--c);
  border: 1px solid var(--hairline);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}
.sm__dhead-top {
  display: flex;
  align-items: flex-start;
  gap: 20px;
  margin-bottom: 20px;
}
.sm__dic {
  width: 80px;
  height: 80px;
  border-radius: 18px;
  font-size: 36px;
  overflow: hidden;
}
.sm__dic img {
  width: 100%;
  height: 100%;
  border-radius: inherit;
  object-fit: cover;
}
.sm__dhead-info {
  flex: 1;
  min-width: 0;
}
.sm__dtitle-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.sm__dtitle-row h2 {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.02em;
}
.sm__dnamepill {
  font-size: 11px;
  color: var(--t3);
  background: var(--bg);
  padding: 3px 10px;
  border-radius: 999px;
  font-weight: 400;
}
.sm__dmeta-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.sm__tg10 {
  font-size: 10px;
}
.sm__tg9 {
  font-size: 9px;
}
.sm__tgfeat {
  background: rgba(184, 155, 106, 0.12);
  color: var(--c-sre);
  font-size: 10px;
}
.sm__ddesc {
  font-size: 13px;
  color: var(--t2);
  line-height: 1.6;
}
.sm__dfoot {
  display: flex;
  align-items: center;
  gap: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--bg);
}
.sm__dlike {
  cursor: pointer;
  font-size: 13px;
  color: var(--t2);
  display: flex;
  align-items: center;
  gap: 4px;
}
.sm__drepos {
  font-size: 11px;
  color: var(--t3);
}
.sm__dactions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}

.sm__sech {
  margin-bottom: 12px;
}
.sm__sech2 {
  margin: 24px 0 12px;
}
.sm__flow-wrap {
  /* 2026-11-16：72% 玻璃叠在 --bg 页面上与背景同色，区块里的卡片没有轮廓；
     改实心亮面 + 发丝描边（与 dashboard 场景详情同口径）。 */
  background: var(--c);
  border: 1px solid var(--hairline);
  border-radius: 18px;
  padding: 24px;
  margin-bottom: 16px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}
.sm__flow-grid {
  display: flex;
  gap: 12px;
  overflow-x: auto;
  min-height: 280px;
  scrollbar-width: none;
}
.sm__flow-grid::-webkit-scrollbar {
  display: none;
}
.sm__fcard {
  flex: 1 1 0;
  min-width: 0;
  max-width: 260px;
  min-height: 260px;
  background: color-mix(in srgb, var(--c) 62%, var(--bg));
  border: none;
  border-radius: 18px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  transition: all 0.2s;
  position: relative;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}
.sm__fcard:hover {
  background: color-mix(in srgb, var(--c) 80%, var(--bg));
  box-shadow:
    0 14px 32px rgba(0, 0, 0, 0.14),
    0 3px 8px rgba(0, 0, 0, 0.08);
  transform: translateY(-3px);
}
.sm__fcard--del {
  border: 1px solid var(--r);
  opacity: 0.7;
}
.sm__fcard-top {
  width: 36px;
  height: 4px;
  border-radius: 2px;
  margin-bottom: 12px;
}
.sm__fcard-h {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.sm__fnum {
  width: 22px;
  height: 22px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.sm__flabel {
  font-size: 14px;
  font-weight: 600;
  color: var(--t);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sm__fdel {
  font-size: 9px;
  color: var(--r);
  font-weight: 600;
  flex-shrink: 0;
}
.sm__fdesc {
  font-size: 10px;
  color: var(--t3);
  line-height: 1.5;
  margin-bottom: 10px;
  flex: 1;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
}
.sm__fsep {
  height: 1px;
  background: var(--bg);
  margin: 8px 0;
}
.sm__fbox {
  background: var(--bg);
  border-radius: 12px;
  padding: 8px 10px;
  margin-bottom: 8px;
  display: flex;
  flex-direction: column;
  align-items: center;
}
.sm__flbl {
  font-size: 8px;
  color: var(--t3);
  text-transform: uppercase;
  letter-spacing: 0.8px;
  margin-bottom: 3px;
}
.sm__fval {
  font-size: 11px;
  font-weight: 500;
  color: var(--t);
}
.sm__fval--del {
  color: var(--r);
}
.sm__fval--skill {
  color: var(--c-coding);
  cursor: pointer;
}
.sm__feng {
  background: var(--bg);
  border-radius: 12px;
  padding: 8px 10px;
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}
.sm__feng-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: rgba(52, 199, 89, 0.1);
  color: var(--g);
  font-size: 11px;
  font-weight: 600;
  padding: 4px 12px;
  border-radius: 999px;
}
.sm__farrow {
  font-size: 18px;
  color: var(--c-coding);
  font-weight: 700;
  align-self: center;
  margin: 0 4px;
  flex-shrink: 0;
}

.sm__suite-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 12px;
}
.sm__scard {
  background: color-mix(in srgb, var(--c) 62%, var(--bg));
  border: none;
  border-radius: 18px;
  padding: 16px;
  display: flex;
  align-items: flex-start;
  gap: 12px;
  transition: all 0.2s;
  cursor: pointer;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}
.sm__scard:hover {
  background: color-mix(in srgb, var(--c) 80%, var(--bg));
  box-shadow:
    0 14px 32px rgba(0, 0, 0, 0.14),
    0 3px 8px rgba(0, 0, 0, 0.08);
  transform: translateY(-3px);
}
.sm__scard--del {
  border: 1px solid var(--r);
  opacity: 0.7;
}
.sm__snum {
  width: 28px;
  height: 28px;
  border-radius: 999px;
  color: var(--on-accent);
  font-size: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  flex-shrink: 0;
}
.sm__sbody {
  flex: 1;
  min-width: 0;
}
.sm__sname {
  font-size: 14px;
  font-weight: 600;
  color: var(--t);
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sm__srole {
  font-size: 11px;
  color: var(--t3);
  line-height: 1.5;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sm__sbadge {
  font-size: 10px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 999px;
  flex-shrink: 0;
}

.sm__param-hint {
  font-size: 11px;
  color: var(--t3);
  font-weight: 400;
  margin-left: 8px;
}
.sm__param-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.sm__param-row {
  background: var(--bg);
  border-radius: 14px;
  padding: 12px 16px;
  display: flex;
  align-items: center;
  gap: 12px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}
.sm__param-info {
  flex: 1;
  min-width: 0;
}
.sm__param-name-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.sm__param-name {
  background: var(--c-accent-soft);
  color: var(--a);
  font-size: 12px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 6px;
}
.sm__pill {
  font-size: 9px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--bg);
  color: var(--t3);
}
.sm__pill--sec {
  background: var(--c-danger-soft);
  color: var(--r);
}
.sm__pill--ok {
  background: rgba(52, 199, 89, 0.12);
  color: var(--g);
}
.sm__pill--lang {
  background: color-mix(in srgb, var(--c-coding) 14%, transparent);
  color: var(--c-coding);
}
.sm__param-dv {
  font-size: 9px;
  color: var(--t3);
}
.sm__param-desc {
  font-size: 11px;
  color: var(--t3);
  margin-top: 4px;
  line-height: 1.5;
}

.sm__tpl--remote {
  cursor: pointer;
}
.sm__tpl-row {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 8px 0;
}
.sm__tpl-ic {
  font-size: 32px;
  flex-shrink: 0;
}
.sm__tpl-info {
  flex: 1;
  min-width: 0;
}
.sm__tpl-name-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
  flex-wrap: wrap;
}
.sm__tpl-name {
  font-size: 15px;
  font-weight: 600;
  color: var(--t);
}
.sm__tpl-stars {
  font-size: 11px;
  color: var(--t3);
}
.sm__tpl-desc {
  font-size: 12px;
  color: var(--t3);
  line-height: 1.5;
}
.sm__tpl-view {
  font-size: 12px;
  color: var(--a);
  font-weight: 500;
  flex-shrink: 0;
  padding: 6px 16px;
  border-radius: 999px;
}

/* ── 弹窗内部 ── */
.sm__form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.sm__warn {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 14px;
  background: var(--c-danger-soft);
  border-radius: 12px;
}
.sm__warn-ic {
  font-size: 18px;
  flex-shrink: 0;
  line-height: 1;
}
.sm__warn-txt {
  font-size: 12px;
  color: var(--t2);
  line-height: 1.6;
}
.sm__hwhint {
  padding: 10px 14px;
  background: var(--c-accent-soft);
  border-radius: 10px;
  font-size: 11px;
  color: var(--t3);
  line-height: 1.6;
}
.sm__hwlink {
  color: var(--a);
  cursor: pointer;
  text-decoration: none;
}
.sm__fld {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.sm__lbl {
  font-size: 12px;
  font-weight: 600;
  color: var(--t3);
}
.sm__row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.sm__f1 {
  flex: 1;
  min-width: 0;
}
.sm__newbranch {
  padding: 8px 0 0;
}
.sm__nbname {
  flex: 1;
  min-width: 0;
  padding: 7px 12px;
  border: 1px solid var(--b);
  border-radius: 10px;
  font-size: 13px;
  outline: none;
  background: var(--bg);
  color: var(--t);
}
.sm__nbref {
  width: 130px;
}
.sm__cf-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.sm__cf {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.sm__cf-key-static {
  flex: 1;
  min-width: 0;
  display: inline-flex;
  flex-direction: column;
  align-items: stretch;
  justify-content: center;
  padding: 7px 12px;
  font-size: 13px;
}
.sm__cf-key-static b {
  font-weight: 600;
  color: var(--t);
  line-height: 1.3;
}
.sm__cf-key-static small {
  font-size: 10px;
  color: var(--t3);
  line-height: 1.3;
  margin-top: 2px;
}
.sm__cf-val {
  flex: 1;
  min-width: 0;
  display: flex;
  gap: 6px;
  align-items: center;
}
.sm__cf-val input {
  flex: 1;
  min-width: 0;
  padding: 7px 12px;
  border: 1px solid var(--b);
  border-radius: 10px;
  font-size: 13px;
  outline: none;
  background: var(--bg);
  color: var(--t);
}
.sm__cf-eye {
  border: 1px solid var(--b);
  background: var(--bg);
  border-radius: 8px;
  width: 30px;
  height: 30px;
  padding: 0;
  font-size: 14px;
  cursor: pointer;
  color: var(--t3);
  flex-shrink: 0;
}
.sm__cf-scope {
  display: inline-flex;
  gap: 2px 8px;
  align-items: center;
  flex-wrap: wrap;
}
.sm__cf-scope-lbl {
  font-size: 11px;
  color: var(--t3);
  display: inline-flex;
  align-items: center;
  gap: 2px;
  white-space: nowrap;
  cursor: pointer;
}
.sm__conf {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 4px;
}
.sm__conf-desc {
  font-size: 12px;
  color: var(--t2);
  line-height: 1.6;
}
.sm__exp-txt {
  font-size: 12px;
  color: var(--t2);
  margin-bottom: 6px;
}
.sm__exp-bar {
  height: 6px;
  background: var(--bg);
  border-radius: 999px;
  overflow: hidden;
}
.sm__exp-fill {
  height: 100%;
  background: var(--a);
  border-radius: 999px;
  transition: width 0.4s ease;
}
.sm__exp-pct {
  text-align: right;
  font-size: 10px;
  color: var(--t3);
  margin-top: 3px;
}
.sm {
  min-height: 100%;
}
/* weekly-09：应用弹窗的场景名称输入 */
.sm__scenename {
  width: 100%;
  box-sizing: border-box;
  padding: 8px 12px;
  border: 1px solid var(--b, #e8e8ed);
  border-radius: 10px;
  font-size: 13px;
  outline: none;
  background: var(--bg, #f5f5f7);
}
.sm__scenename:focus {
  border-color: var(--a-text);
  background: var(--c);
}
.sm__nameerr {
  margin-top: 6px;
  font-size: 11px;
  color: var(--r);
  line-height: 1.5;
}
</style>
