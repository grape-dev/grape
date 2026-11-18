<template>
  <div class="sk">
    <!-- ═══ 技能市场主页 ═══ -->
    <!-- 一级 Tab：场景 / 技能 -->
    <div class="sk__top">
      <div class="seg-bar sk__seg">
        <a @click="G.app.nav('scene-market')">🏪 {{ tt('scenes_tab') }}</a>
        <a class="ac">⚙ {{ tt('skills_tab') }}</a>
      </div>
      <div class="sk__intro">
        <b class="sk__intro-b">{{ tt('scenes_tab') }}</b>
        {{ tt('market_intro_scene') }} ·
        <b class="sk__intro-b">{{ tt('skills_tab') }}</b>
        {{ tt('market_intro_skill') }}
      </div>
    </div>

    <template v-if="!detail && !pkg && !subSkill">
      <!-- 二级子页签 -->
      <div class="sk__subtab">
        <div class="seg-bar sk__seg">
          <a :class="{ ac: tab === 'official' }" @click="switchTab('official')">{{
            scopeName('official')
          }}</a>
          <a :class="{ ac: tab === 'repo' }" @click="switchTab('repo')">{{ scopeName('repo') }}</a>
          <a :class="{ ac: tab === 'user' }" @click="switchTab('user')">{{ scopeName('user') }}</a>
        </div>
      </div>

      <!-- 精选技能（仅 official） -->
      <div v-if="tab === 'official' && featured.length" class="apple-card">
        <div class="cd-h">
          <h2>🌟 {{ tt('featured_skills') }}</h2>
        </div>
        <div class="feat-row">
          <div
            v-for="s in featured"
            :key="'f' + s.id"
            class="feat-card"
            :style="{ '--fg1': gradient(s).g1, '--fg2': gradient(s).g2 }"
            @click="openDetail(s.id)"
          >
            <span class="feat-badge">★</span>
            <div class="feat-ico">{{ agentMeta(s).icon || '◈' }}</div>
            <div class="feat-name">
              {{ s.display_name || s.name }}<span class="feat-agent">{{ agentMeta(s).label }}</span>
            </div>
            <div class="feat-desc">{{ s.description || '' }}</div>
            <div class="feat-scenes">{{ scenesShort(s) }}</div>
            <div class="feat-scenes sk__featcount">
              {{ sceneCount(s) }} {{ td('scenes_in_use') }} · {{ s.repo_count || 0 }}
              {{ td('repos_in_use') }}
            </div>
          </div>
        </div>
      </div>

      <!-- 全部技能 -->
      <div class="apple-card">
        <div class="cd-h apple-search sk__search">
          <h2>
            {{ tt('all_skills') }}
            <span class="sk__count">{{ total }}</span>
          </h2>
          <div class="sk__tools">
            <button
              :class="['bt bt-s', sort === 'latest' ? 'bt-tg' : 'bt-o']"
              @click="setSort('latest')"
            >
              {{ tt('sort_latest') }}
            </button>
            <button
              v-if="tab !== 'user'"
              :class="['bt bt-s', sort === 'hot' ? 'bt-tg' : 'bt-o']"
              @click="setSort('hot')"
            >
              {{ tt('sort_hot') }}
            </button>
            <input
              v-model="qDraft"
              class="sk__q"
              :placeholder="tt('skill_search_placeholder')"
              @keyup.enter="doSearch"
            />
            <button class="bt bt-o bt-s" @click="doSearch">{{ tt('find_skill') }}</button>
            <button class="bt bt-o bt-s" @click="resetFilters">{{ tt('reset_filters') }}</button>
            <button
              v-if="tab === 'repo' || tab === 'user'"
              class="bt bt-p bt-s sk__upload-btn"
              @click="showUpload = true"
            >
              {{ tt2('action.add_skill') }}
            </button>
          </div>
        </div>

        <!-- repo 无当前仓库提示 -->
        <div v-if="tab === 'repo' && !currentRepo" class="ts t2 sk__norepo">
          {{ tt2('empty.no_repo_selected') }}
        </div>

        <!-- 左右分栏：Agent 筛选 + 右侧内容 -->
        <div v-else class="sk__cols">
          <!-- 左侧 Agent 筛选栏 -->
          <div class="sk__agents">
            <a
              v-for="(at, i) in agentList"
              :key="at"
              :class="['skill-agent-item', { ac: activeAgent === i }]"
              href="javascript:void(0)"
              @click="switchAgent(i)"
            >
              <span v-if="agentMetaOf(at).icon" class="sk__agent-ic">{{
                agentMetaOf(at).icon
              }}</span>
              {{ (agentMetaOf(at).label || at) + ' Agent' }}
              <span v-if="(agentStats[at] || 0) > 0" class="sk__agent-count">{{
                agentStats[at]
              }}</span>
            </a>
          </div>
          <!-- 右侧：标签筛选 + 技能网格 + 分页 -->
          <div class="sk__main">
            <div class="sk__tags">
              <a
                :class="['skill-tag-filter', { ac: activeL1 === 0 }]"
                href="javascript:void(0)"
                :style="tagStyle(activeL1 === 0)"
                @click="onL1Change(0)"
                >{{ tt('filter_all_l1') }}</a
              >
              <a
                v-for="c in catTree"
                :key="'l1-' + c.id"
                :class="['skill-tag-filter', { ac: activeL1 === c.id }]"
                href="javascript:void(0)"
                :style="tagStyle(activeL1 === c.id)"
                @click="onL1Change(c.id)"
              >
                <span v-if="c.icon" class="sk__tag-ic">{{ c.icon }}</span
                >{{ c.name }}
              </a>
            </div>
            <div v-if="l2Options.length" class="sk__tags">
              <a
                :class="['skill-tag-filter', { ac: activeL2 === 0 }]"
                href="javascript:void(0)"
                :style="tagStyle(activeL2 === 0)"
                @click="onL2Change(0)"
                >{{ tt('filter_all_l2') }}</a
              >
              <a
                v-for="c in l2Options"
                :key="'l2-' + c.id"
                :class="['skill-tag-filter', { ac: activeL2 === c.id }]"
                href="javascript:void(0)"
                :style="tagStyle(activeL2 === c.id)"
                @click="onL2Change(c.id)"
              >
                <span v-if="c.icon" class="sk__tag-ic">{{ c.icon }}</span
                >{{ c.name }}
              </a>
            </div>
            <div class="sk__tags">
              <a
                :class="['skill-tag-filter', { ac: activeTag === 'all' }]"
                href="javascript:void(0)"
                :style="tagStyle(activeTag === 'all')"
                @click="switchTag('all', '')"
                >{{ tt('all_categories') }}</a
              >
              <a
                v-for="c in categories"
                :key="c.id"
                :class="['skill-tag-filter', { ac: activeTagId === String(c.id) }]"
                href="javascript:void(0)"
                :style="tagStyle(activeTagId === String(c.id))"
                @click="switchTag(c.name, String(c.id))"
              >
                <span v-if="c.icon" class="sk__tag-ic">{{ c.icon }}</span
                >{{ c.name }}
              </a>
            </div>
            <div class="sk__gridwrap">
              <div v-if="listLoading" class="scn-loading"><span class="scn-spinner"></span></div>
              <div v-else-if="!skills.length" class="ts t3 sk__empty">
                {{ tt2('empty.no_skills') }}
              </div>
              <div v-else class="rgrid">
                <div
                  v-for="s in skills"
                  :key="s.id"
                  :class="['rc', 'skill-card', { sk__hwcard: s.name === 'huawei-cloud' }]"
                  @click="openDetail(s.id)"
                >
                  <!-- 华为云技能包 -->
                  <template v-if="s.name === 'huawei-cloud'">
                    <div class="rh">
                      <span class="sk__hw-ic">📁</span>
                      <span :class="['tg', tagClass(s)]" style="font-size: 9px">{{
                        agentMeta(s).label
                      }}</span>
                      <span class="rn">{{ s.display_name || s.name }}</span>
                    </div>
                    <div class="rd">{{ s.description || '' }}</div>
                    <div class="rf">
                      <span class="s">{{ tt2('action.view') }} →</span>
                    </div>
                  </template>
                  <!-- 普通技能卡 -->
                  <template v-else>
                    <div class="rh">
                      <span style="font-size: 14px">{{ agentMeta(s).icon || '' }}</span>
                      <span :class="['tg', tagClass(s)]" style="font-size: 9px">{{
                        agentMeta(s).label
                      }}</span>
                      <span v-if="s.category_name" class="tg sk__cat-tag">{{
                        s.category_name
                      }}</span>
                      <span class="rn" :title="s.display_name || s.name">{{
                        s.display_name || s.name
                      }}</span>
                      <span v-if="s.is_featured && s.scope !== 'user'" class="tg sk__feat-tag"
                        >🌟</span
                      >
                    </div>
                    <div class="rd" style="margin-bottom: 8px" :title="s.description || ''">
                      {{ s.description || '' }}
                    </div>
                    <div v-if="catPath(s)" class="sk__catpath">{{ catPath(s) }}</div>
                    <div v-if="(s.tags || []).length" class="sk__chips">
                      <span v-for="(t, i) in (s.tags || []).slice(0, 3)" :key="'st' + i" class="sk__chip">{{
                        t.name
                      }}</span>
                    </div>
                    <div v-if="scenesShort(s)" class="ro sk__scenes" :title="scenesFull(s)">
                      {{ tt('info_scenes') }}：{{ scenesShort(s) }}
                    </div>
                    <div v-if="sceneCount(s) > 0 || (s.repo_count || 0) > 0" class="ro">
                      {{ sceneCount(s) }} {{ td('scenes_in_use') }}，{{ s.repo_count || 0 }}
                      {{ td('repos_in_use') }}
                    </div>
                    <div class="rf">
                      <span v-if="s.scope !== 'user'" class="s sk__like" @click.stop="onLike(s)">
                        {{ s.liked_by_me ? '❤️' : '🤍' }} {{ s.like_count || 0 }}
                      </span>
                      <span v-else class="s"></span>
                      <div class="sk-card-btns">
                        <!-- 个人/仓库技能卡整卡可点，去掉重复的「查看」按钮（只留官方技能） -->
                        <button
                          v-if="s.scope === 'official'"
                          class="bt bt-o bt-s"
                          @click.stop="openDetail(s.id)"
                        >
                          {{ tt2('action.view') }}
                        </button>
                        <!-- Issue #561：直接覆盖更新（免删了重传；ID/场景引用/点赞保留） -->
                        <button
                          v-if="s.scope !== 'official'"
                          class="bt bt-o bt-s"
                          @click.stop="askUpdate(s)"
                        >
                          {{ tt2('action.update_skill') }}
                        </button>
                        <button
                          v-if="s.scope !== 'official'"
                          class="bt bt-o bt-s sk__del-btn"
                          @click.stop="askDelete(s)"
                        >
                          {{ tt2('action.delete') }}
                        </button>
                        <button
                          v-if="s.scope === 'user' && currentRepo"
                          class="bt bt-o bt-s"
                          @click.stop="askConvert(s, 'user')"
                        >
                          {{ td('to_repo') }}
                        </button>
                        <button
                          v-else-if="s.scope === 'repo' && currentRepo"
                          class="bt bt-o bt-s"
                          @click.stop="askConvert(s, 'repo')"
                        >
                          {{ td('to_user') }}
                        </button>
                      </div>
                    </div>
                  </template>
                </div>
              </div>
              <!-- 分页器 -->
              <div class="sk__pager">
                <button class="bt bt-o bt-s" :disabled="page <= 1" @click="goPage(page - 1)">
                  ‹
                </button>
                <span class="sk__pginfo"
                  >{{ page }} / {{ totalPages }} ({{ pageTotal
                  }}{{ tt('items_count_suffix') }})</span
                >
                <button
                  class="bt bt-o bt-s"
                  :disabled="page >= totalPages"
                  @click="goPage(page + 1)"
                >
                  ›
                </button>
                <select
                  :value="pageSize"
                  class="sk__pgsize"
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
      </div>
    </template>

    <!-- ═══ 技能详情页（Issue #3168 五 Tab，D12 skd-* 隔离） ═══ -->
    <template v-else-if="detail">
      <button class="skd-back" @click="backFromDetail">← {{ tc('back') }}</button>
      <div class="skd-page">
        <div class="skd-card skd-head">
          <span
            class="skd-ico"
            :style="{ '--fg1': gradient(detail).g1, '--fg2': gradient(detail).g2 }"
            >{{ agentMeta(detail).icon || '◈' }}</span
          >
          <div class="skd-head-main">
            <div class="skd-name-row">
              <h1 class="skd-name">{{ detail.display_name || detail.name }}</h1>
              <span class="agent-badge">{{ agentMeta(detail).label }}</span>
              <span v-if="detail.is_featured" class="skd-badge">★ {{ tt('featured_badge') }}</span>
            </div>
            <div v-if="detailTags.length" class="skd-tags">
              <span v-for="t in detailTags" :key="'dt-' + t" class="skd-chip">{{ t }}</span>
            </div>
            <div v-else class="skd-meta"><span>{{ tt('no_tags') }} —</span></div>
            <div class="skd-meta">
              <span class="skd-chip">v{{ detailVersion }}</span>
              <span>{{ scopeLabel }}</span>
              <span>{{ tt('updated_at') }} {{ formatDate(detail.updated_at) }}</span>
              <span>⬇ {{ detail.download_count || 0 }} {{ tt('downloads') }}</span>
              <span>{{ tt('orchestrated_in') }} {{ sceneCount(detail) }} {{ tt('scenes_unit') }}</span>
              <span v-if="detail.created_by" class="skd-contributor">{{ detail.created_by }}</span>
            </div>
          </div>
          <div class="skd-head-actions">
            <button
              v-if="detail.scope !== 'user'"
              :class="['skd-btn', 'skd-like', { liked: detail.liked_by_me }]"
              @click="onLike(detail)"
            >
              {{ detail.liked_by_me ? '❤️' : '🤍' }} {{ detail.like_count || 0 }}
            </button>
            <button
              v-if="detail.is_obs || detail.zip_url"
              class="skd-btn skd-dl"
              @click="downloadZip(detail.id)"
            >
              {{ tt('download_skill') }}
            </button>
          </div>
        </div>

        <div class="skd-tabs">
          <button
            v-for="t in skdTabs"
            :key="'skdtab-' + t.key"
            :class="['skd-tab', { on: skdTab === t.key }]"
            @click="setSkdTab(t.key)"
          >
            {{ t.label }}<span v-if="t.count !== null" class="skd-count">{{ t.count }}</span>
          </button>
        </div>

        <!-- 概览 -->
        <div v-if="skdTab === 'overview'" class="skd-panel">
          <div class="skd-card skd-summary">{{ detailSummary }}</div>
          <div class="skd-card skd-metatable">
            <div class="skd-row"><span>{{ tt('info_scenes') }}</span><div class="skd-scene-chips">
              <a v-for="sc in sceneChips" :key="'sc-' + sc.id" class="skd-chip skd-chip-link" @click="openScene(sc)">{{ sc.name }}</a>
              <span v-if="!sceneChips.length">—</span>
            </div></div>
            <div class="skd-row"><span>{{ tt('info_category') }}</span><b>{{ catPath(detail) || detail.category_name || '—' }}</b></div>
            <div class="skd-row"><span>{{ tt('info_version') }}</span><b>v{{ detailVersion }}</b></div>
            <div class="skd-row"><span>{{ tt('info_author') }}</span><b>{{ detail.created_by || '—' }}</b></div>
            <div class="skd-row"><span>{{ tt('updated_at') }}</span><b>{{ formatDate(detail.updated_at) }}</b></div>
          </div>
          <div v-if="hasOverview" class="skd-card skd-overview">
            <h2 class="skd-h2">{{ tt('structured_overview') }}</h2>
            <div v-for="slot in overviewSlots" :key="'ov-' + slot.key" class="skd-ov-slot">
              <h3 class="skd-h3">{{ slot.title }}</h3>
              <ol v-if="slot.kind === 'steps'">
                <li v-for="(line, i) in slot.items" :key="'ovl-' + i">{{ line }}</li>
              </ol>
              <ul v-else>
                <li v-for="(line, i) in slot.items" :key="'ovl-' + i">{{ line }}</li>
              </ul>
            </div>
          </div>
          <div class="skd-card">
            <h2 class="skd-h2">{{ tt('description_original') }}</h2>
            <p class="skd-desc">{{ detail.description || '—' }}</p>
          </div>
          <div class="skd-card skd-comments">
            <h2 class="skd-h2">
              {{ tt('user_comments') }}<span class="skd-count">{{ skdComments.total }}</span>
            </h2>
            <div class="skd-comment-form">
              <textarea
                v-model="commentDraft"
                class="skd-textarea"
                rows="3"
                maxlength="1000"
                :placeholder="tt('comment_placeholder')"
              ></textarea>
              <div class="skd-comment-actions">
                <span class="skd-hint">{{ commentDraft.length }}/1000</span>
                <button class="skd-btn skd-btn-primary" :disabled="commentBusy" @click="submitComment">
                  {{ commentBusy ? tt('comment_submitting') : tt('comment_submit') }}
                </button>
              </div>
            </div>
            <div v-if="!skdComments.comments.length" class="skd-empty">{{ tt('comment_empty') }}</div>
            <div v-for="c in skdComments.comments" :key="'cm-' + c.id" class="skd-comment">
              <div class="skd-comment-head">
                <b>{{ c.user_name || '用户' }}</b><span>{{ formatDate(c.created_at) }}</span>
              </div>
              <div class="skd-comment-body">{{ c.content }}</div>
            </div>
            <div v-if="skdComments.total > skdComments.comments.length" class="skd-more">
              <button class="skd-btn" @click="loadMoreComments">{{ tt('load_more') }}</button>
            </div>
          </div>
        </div>

        <!-- SKILL.md -->
        <div v-else-if="skdTab === 'skillmd'" class="skd-panel">
          <div class="skd-card skd-md-layout">
            <aside v-if="skillToc.length" class="skd-toc">
              <div class="skd-toc-title">{{ tt('toc_title') }}</div>
              <a v-for="h in skillToc" :key="'toc-' + h.id" @click="jumpToHeading(h.id)">{{ h.text }}</a>
            </aside>
            <div class="skd-md-body">
              <!-- eslint-disable-next-line vue/no-v-html — 复用既有 renderSkillContent 渲染链路 -->
              <div v-html="detailHtml"></div>
            </div>
          </div>
        </div>

        <!-- 资源 -->
        <div v-else-if="skdTab === 'resources'" class="skd-panel">
          <div class="skd-card skd-actions">
            <a class="skd-btn" :href="sourceUrl" target="_blank" rel="noopener">{{ tt('view_source') }}</a>
            <a class="skd-btn" :href="issueUrl" target="_blank" rel="noopener">{{ tt('report_issue') }}</a>
            <button v-if="detail.is_obs || detail.zip_url" class="skd-btn" @click="downloadZip(detail.id)">
              {{ tt('download_skill') }}
            </button>
          </div>
          <div class="skd-card skd-filetree">
            <h2 class="skd-h2">{{ tt('file_tree') }}<span class="skd-count">{{ detail.file_count || 0 }}</span></h2>
            <div v-if="!skdFiles.length" class="skd-empty">{{ tt('no_files') }}</div>
            <div v-for="f in skdFiles" :key="'f-' + f.path" class="skd-file">
              <span class="skd-file-name">{{ f.path }}</span>
              <span class="skd-file-size">{{ formatSize(f.size) }}</span>
            </div>
          </div>
        </div>

        <!-- 相关技能 -->
        <div v-else-if="skdTab === 'related'" class="skd-panel">
          <div v-if="!skdRelated.length" class="skd-card skd-empty">{{ tt('no_related') }}</div>
          <div v-else class="skd-related-grid">
            <div v-for="r in skdRelated" :key="'rel-' + r.id" class="skd-card skd-related" @click="openDetail(r.id)">
              <div class="skd-related-name">{{ r.display_name || r.name }}</div>
              <div class="skd-related-desc">{{ r.summary || r.description || '—' }}</div>
              <div class="skd-related-meta">
                <span class="skd-chip">{{ agentMetaOf(r.agent_type).label || r.agent_type }}</span>
                <span>❤️ {{ r.like_count || 0 }}</span>
              </div>
            </div>
          </div>
        </div>

        <!-- 版本历史 -->
        <div v-else-if="skdTab === 'versions'" class="skd-panel">
          <div class="skd-card">
            <table class="skd-table">
              <thead>
                <tr><th>{{ tt('version') }}</th><th>{{ tt('published_at') }}</th><th>{{ tt('change_note') }}</th></tr>
              </thead>
              <tbody>
                <tr v-for="v in skdVersions" :key="'ver-' + v.version + '-' + v.uploaded_at">
                  <td>
                    {{ v.version }}
                    <span v-if="v.is_latest" class="skd-badge">Latest</span>
                  </td>
                  <td>{{ formatDate(v.uploaded_at) }}</td>
                  <td>{{ v.change_note || '—' }}</td>
                </tr>
                <tr v-if="!skdVersions.length"><td colspan="3" class="skd-empty">{{ tt('no_versions') }}</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </template>

    <!-- ═══ 华为云技能包子技能列表 ═══（分支顺序：subSkill 优先于 pkg——查看子技能时 pkg 不卸载） -->
    <template v-else-if="subSkill">
      <button class="bt bt-o bt-s sk__back-btn" @click="backToPkg">← {{ th('back') }}</button>
      <div class="sk__subdet">
        <div class="cd sk__subdet-main">
          <div class="sk__subdet-h">
            <span class="sk__subdet-ic">📄</span>
            <h3>{{ subSkill.display_name || subSkill.name }}</h3>
          </div>
          <div class="sk__subdet-desc">{{ subSkill.description || '' }}</div>
          <h3 class="sk__subdet-mdtitle">SKILL.md</h3>
          <pre class="sk__subdet-pre">{{ subSkill.content || th('no_detail') }}</pre>
        </div>
        <div class="cd sk__subdet-side">
          <h3>{{ tt('info_title') }}</h3>
          <div class="srow">
            <label>{{ tt('info_name') }}</label
            ><span>{{ subSkill.name }}</span>
          </div>
          <div class="srow">
            <label>{{ tt('info_description') }}</label
            ><span class="t2">{{ subSkill.description || '—' }}</span>
          </div>
        </div>
      </div>
    </template>

    <!-- ═══ 华为云包列表（subSkill 已在上面优先处理） ═══ -->
    <template v-else-if="pkg">
      <button class="bt bt-o bt-s sk__back-btn" @click="backFromDetail">← {{ th('back') }}</button>
      <div class="cd sk__pkg-head">
        <div class="sk__pkg-row">
          <span class="sk__pkg-ic">📁</span>
          <div>
            <h3>{{ pkg.display_name || pkg.name }}</h3>
            <div class="sk__pkg-desc">{{ pkg.description || '' }}</div>
          </div>
          <button class="bt bt-o bt-s sk__pkg-viewmd" @click="viewPkgMd">
            {{ th('view_skill_md') }}
          </button>
        </div>
      </div>
      <div class="sk__subgrid">
        <div v-if="subLoading" class="sk__subhint">{{ th('loading_sub') }}</div>
        <div v-else-if="!subSkills.length" class="sk__subhint">{{ th('no_sub_skill') }}</div>
        <div
          v-for="sub in subSkills"
          :key="sub.name"
          class="cd sk__subcard"
          @click="openSubSkill(sub.name)"
        >
          <div class="sk__subcard-h">
            <span class="sk__subcard-ic">📄</span
            ><span class="sk__subcard-name">{{ sub.display_name || sub.name }}</span>
          </div>
          <div class="sk__subcard-sname">{{ sub.name }}</div>
          <div class="sk__subcard-desc">{{ (sub.description || '').substring(0, 200) }}</div>
        </div>
      </div>
    </template>
    <!-- ═══ 上传技能弹窗 ═══ -->
    <AGModal :open="showUpload" :title="tt2('upload.title')" @close="showUpload = false">
      <div class="sk__form">
        <div class="srow sk__srow">
          <label>{{ tt2('upload.type') }}</label
          ><span>{{ tab === 'repo' ? td('scope_repo') : td('scope_user') }}</span>
        </div>
        <div v-if="tab === 'repo'" class="srow sk__srow">
          <label>{{ tt2('upload.repo') }}</label
          ><span>{{ currentRepo }}</span>
        </div>
        <div class="srow sk__srow">
          <label>{{ tt2('upload.agent_type') }}</label>
          <AGSelect
            v-model="uploadForm.agentType"
            :options="agentOpts"
            :placeholder="tt2('upload.select_agent')"
            class="sk__f1"
          />
        </div>
        <div class="srow sk__srow">
          <label>{{ tt2('upload.category') }}</label>
          <AGSelect v-model="uploadForm.categoryId" :options="categoryOpts" class="sk__f1" />
        </div>
        <div class="srow sk__srow">
          <label>{{ tt('upload_cat_l1') }}</label>
          <AGSelect v-model="uploadForm.catL1" :options="l1Opts" class="sk__f1" />
        </div>
        <div class="srow sk__srow">
          <label>{{ tt('upload_cat_l2') }}</label>
          <AGSelect v-model="uploadForm.catL2" :options="uploadL2Opts" class="sk__f1" />
        </div>
        <div class="srow sk__srow">
          <label>{{ tt('upload_tags') }}</label>
          <input v-model="uploadForm.tags" class="sk__f1" :placeholder="tt('upload_tags_ph')" />
        </div>
        <div class="srow sk__srow">
          <label>{{ tt('upload_summary') }}</label>
          <input v-model="uploadForm.summary" class="sk__f1" :placeholder="tt('upload_summary_ph')" />
        </div>
        <div class="sk__file-fld">
          <label>{{ tt2('upload.file_label') }}</label>
          <input
            type="file"
            accept=".zip"
            class="sk__file"
            @change="uploadForm.file = $event.target.files[0] || null"
          />
        </div>
        <div class="sk__fmt-hint">
          {{ tt2('upload.format_req') }}
          <a class="sk__tpl-link" @click="downloadTemplate">{{
            tt2('action.download_template')
          }}</a>
        </div>
        <div v-if="uploadPct > 0 && !uploadDone" class="sk__upload-prog">
          <div class="sk__upload-txt">{{ tt2('upload.uploading') }} {{ uploadPct }}%</div>
          <div class="sk__upload-bar"><div :style="{ width: uploadPct + '%' }"></div></div>
        </div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="showUpload = false">{{
          tt2('action.cancel')
        }}</AGButton>
        <AGButton size="sm" :disabled="uploading" @click="doUpload">{{
          uploading ? tt2('upload.uploading') : tt2('action.upload')
        }}</AGButton>
      </template>
    </AGModal>

    <!-- ═══ Issue #561：上传更新技能（覆盖当前内容） ═══ -->
    <AGModal
      :open="updateCtx.open"
      :title="tt2('update.title') + (updateCtx.name ? ' — ' + updateCtx.name : '')"
      @close="updateCtx.open = false"
    >
      <div class="sk__form">
        <div class="sk__fmt-hint">{{ tt2('update.tip') }}</div>
        <div class="srow sk__srow">
          <label>{{ tt('upload_summary') }}</label>
          <input v-model="updateCtx.summary" class="sk__f1" :placeholder="tt('upload_summary_ph')" />
        </div>
        <div class="srow sk__srow">
          <label>{{ tt('change_note') }}</label>
          <input v-model="updateCtx.changeNote" class="sk__f1" :placeholder="tt('change_note_ph')" />
        </div>
        <div class="sk__file-fld">
          <label>{{ tt2('upload.file_label') }}</label>
          <input
            type="file"
            accept=".zip"
            class="sk__file"
            @change="updateCtx.file = $event.target.files[0] || null"
          />
        </div>
        <div v-if="updateCtx.pct > 0 && !updateCtx.done" class="sk__upload-prog">
          <div class="sk__upload-txt">{{ tt2('upload.uploading') }} {{ updateCtx.pct }}%</div>
          <div class="sk__upload-bar"><div :style="{ width: updateCtx.pct + '%' }"></div></div>
        </div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="updateCtx.open = false">{{
          tt2('action.cancel')
        }}</AGButton>
        <AGButton size="sm" :disabled="updateCtx.busy" @click="doUpdate">
          {{ updateCtx.busy ? tt2('upload.uploading') : tt2('action.confirm_update') }}
        </AGButton>
      </template>
    </AGModal>

    <!-- ═══ 删除确认弹窗 ═══ -->
    <AGModal
      :open="deleteCtx.open"
      :title="tc2('skills.confirm.delete_skill')"
      @close="deleteCtx.open = false"
    >
      <div class="sk__conf">
        <div class="sk__conf-ic">🗑</div>
        <h3>{{ tc2('skills.confirm.delete_skill') }}</h3>
        <div class="sk__conf-desc">{{ tt('delete_skill_tip') }}</div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="deleteCtx.open = false">{{
          tt2('action.cancel')
        }}</AGButton>
        <AGButton size="sm" variant="danger" :disabled="deleteCtx.busy" @click="confirmDelete">
          {{ deleteCtx.busy ? tt('deleting') : tt('confirm_delete') }}
        </AGButton>
      </template>
    </AGModal>

    <!-- ═══ 转换（复制）确认弹窗 ═══ -->
    <AGModal :open="convertCtx.open" :title="td('convert_title')" @close="convertCtx.open = false">
      <div class="sk__conf">
        <div class="sk__conf-ic sk__conf-ic--blue">🔄</div>
        <h3>
          {{ td('convert_name').replace('{name}', convertCtx.skill ? convertCtx.skill.name : '') }}
        </h3>
        <div class="sk__conf-desc">{{ convertDesc }}</div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="convertCtx.open = false">{{
          td('cancel')
        }}</AGButton>
        <AGButton size="sm" :disabled="convertCtx.busy" @click="confirmConvert">
          {{ convertCtx.busy ? td('converting') : td('confirm_convert') }}
        </AGButton>
      </template>
    </AGModal>

    <!-- ═══ 包 SKILL.md 查看弹窗 ═══ -->
    <AGModal :open="mdModal.open" title="📄 SKILL.md" @close="mdModal.open = false">
      <pre class="sk__md-pre">{{ mdModal.content }}</pre>
    </AGModal>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import AGButton from '@/components/ui/AGButton.vue'
import AGModal from '@/components/ui/AGModal.vue'
import AGSelect from '@/components/ui/AGSelect.vue'

const G = globalThis

// app.js 契约：renderSkillDetail(d) 入口注入完整数据对象（含 huawei-cloud 分包）
const props = defineProps({ initialDetail: { type: Object, default: null } })

// ── I18N helpers（skills.* 命名空间；规避 t(key) 返回 key 本身的陷阱）──
function tt(key) {
  const full = 'skills.' + key
  const v = G.I18N.t(full)
  return v && v !== full ? v : KEY_DEFAULTS[key] || key
}
function tt2(key) {
  const full = 'skills.' + key
  const v = G.I18N.t(full)
  return v && v !== full ? v : KEY2_DEFAULTS[key] || key
}
function tc(key) {
  const full = 'common.' + key
  const v = G.I18N.t(full)
  return v && v !== full ? v : key === 'back' ? '返回' : key
}
function tc2(key) {
  const v = G.I18N.t(key)
  return v && v !== key ? v : '确认删除此技能？'
}
function th(key) {
  const full = 'skills.huawei.' + key
  const v = G.I18N.t(full)
  return v && v !== full ? v : HUAWEI_DEFAULTS[key] || key
}
const KEY_DEFAULTS = {
  scenes_tab: '场景',
  skills_tab: '技能',
  market_intro_scene: '是多个技能按开发流程组合的开发模式',
  market_intro_skill: '是单个 Agent 的能力包',
  all_skills: '全部技能',
  featured_skills: '精选技能',
  all_categories: '全部',
  filter_all_l1: '全部一级分类',
  filter_all_l2: '全部二级分类',
  upload_cat_l1: '一级分类',
  upload_cat_l2: '二级分类',
  upload_tags: '标签（≤3，逗号分隔）',
  upload_tags_ph: '如 AI,自动化',
  upload_summary: '摘要（≤50 字）',
  upload_summary_ph: '一句话说明用途，不要使用 → 或 -> 流程链',
  change_note_ph: '本次更新说明（可选）',
  sort_latest: '最新',
  sort_hot: '最热',
  skill_search_placeholder: '搜索技能名称或描述',
  find_skill: '查找技能',
  reset_filters: '重置',
  items_count_suffix: '条',
  per_page_suffix: '/页',
  info_scenes: '所属场景',
  info_tags: '分类',
  info_category: '分类',
  info_version: '版本',
  info_author: '作者',
  info_function: '内容说明',
  featured_badge: '精选',
  no_tags: '标签',
  updated_at: '更新于',
  downloads: '次下载',
  orchestrated_in: '已编排进',
  scenes_unit: '个场景',
  structured_overview: '结构化概述',
  description_original: '概述原文',
  user_comments: '用户评论',
  comment_placeholder: '写下你的使用体验…',
  comment_submit: '发表评论',
  comment_submitting: '发表中...',
  comment_empty: '暂无评论，来抢沙发',
  load_more: '加载更多',
  toc_title: '章节目录',
  view_source: '查看源码',
  report_issue: '提 Issue',
  file_tree: '文件树',
  no_files: '暂无文件清单',
  no_related: '暂无相关技能',
  version: '版本号',
  published_at: '发布时间',
  change_note: '变更说明',
  no_versions: '暂无版本历史',
  ov_summary: '总体说明',
  ov_steps: '关键步骤链',
  ov_constraints: '约束简述',
  ov_products: '产物说明',
  ov_scope: '适用范围',
  tab_overview: '概览',
  tab_skillmd: 'SKILL.md',
  tab_resources: '资源',
  tab_related: '相关技能',
  tab_versions: '版本历史',
  login_required: '请先登录后再发表评论',
  info_title: '信息',
  info_name: '名称',
  info_description: '描述',
  download_skill: '下载技能包',
  delete_skill_tip: '删除后不可恢复，技能数据将被永久移除',
  deleting: '删除中...',
  confirm_delete: '确认删除',
}
const KEY2_DEFAULTS = {
  'action.add_skill': '上传技能',
  'action.view': '详情',
  'action.delete': '删除',
  'action.cancel': '取消',
  'action.upload': '上传',
  'action.download_template': '下载模板',
  'empty.no_repo_selected': '请先选择仓库',
  'empty.no_skills': '暂无技能',
  'upload.title': '上传技能',
  'upload.type': '类型',
  'upload.repo': '仓库',
  'upload.agent_type': 'Agent 类型',
  'upload.select_agent': '请选择',
  'upload.category': '分类',
  'upload.uncategorized': '未分类',
  'upload.file_label': '技能 ZIP 包',
  'upload.format_req': 'ZIP 内含 SKILL.md',
  'upload.uploading': '上传中',
  // Issue #561：覆盖更新
  'action.update_skill': '上传更新',
  'action.confirm_update': '确认覆盖更新',
  'update.title': '上传更新技能',
  'update.tip':
    '上传新 ZIP 覆盖当前技能内容，技能 ID、场景引用与点赞保留；ZIP 内目录名需与技能名一致',
  'toast.select_zip': '请选择技能 ZIP 文件',
  'toast.update_success': '更新成功',
  'toast.update_failed': '更新失败',
}
const HUAWEI_DEFAULTS = {
  back: '返回',
  loading_sub: '加载中...',
  no_sub_skill: '暂无子技能',
  no_detail: '无详情',
  view_skill_md: '查看 SKILL.md',
}
// 详情/卡片硬编码文案（老代码同款中文）
const D = {
  scenes_in_use: '场景在用',
  repos_in_use: '仓库在用',
  to_repo: '转仓库',
  to_user: '转个人',
  scope_repo: '仓库技能',
  scope_user: '个人技能',
  convert_title: '确认复制',
  convert_name: '确认复制「{name}」',
  cancel: '取消',
  converting: '复制中...',
  confirm_convert: '确认复制',
}
function td(key) {
  return D[key]
}

// ── Agent meta / 渐变（与老 _AGENT_META / _FEAT_GRADIENT 一致）──
const AGENT_META = {
  vod: { icon: '🔍', label: 'VOD' },
  issue: { icon: '📋', label: 'Issue' },
  coding: { icon: '💻', label: 'Coding' },
  qa: { icon: '🧪', label: 'QA' },
  sre: { icon: '⚙️', label: 'SRE' },
  chat: { icon: '💬', label: 'Chat' },
  auto: { icon: '⏰', label: 'Auto' },
}
const FEAT_GRADIENT = {
  vod: { g1: '#8FA9C4', g2: '#53688A' },
  issue: { g1: '#A99C7C', g2: '#75684A' },
  coding: { g1: '#7EA48C', g2: '#4B7359' },
  qa: { g1: '#A49AC0', g2: '#72638F' },
  sre: { g1: '#C3917E', g2: '#97503D' },
  chat: { g1: '#45B8B8', g2: '#0A7A7A' },
}
const agentList = ['vod', 'issue', 'coding', 'qa', 'sre', 'chat', 'auto']
function agentMeta(s) {
  return AGENT_META[s.agent_type] || {}
}
function agentMetaOf(at) {
  return AGENT_META[at] || {}
}
function gradient(s) {
  return FEAT_GRADIENT[s.agent_type] || { g1: 'var(--a-text)', g2: '#2556c2' }
}
function catPath(s) {
  var c = s && s.category
  if (!c) return ''
  var l1 = c.l1 && c.l1.name
  var l2 = c.l2 && c.l2.name
  if (l1 && l2) return l1 + ' / ' + l2
  return l1 || l2 || ''
}
function onL1Change(v) {
  activeL1.value = parseInt(v, 10) || 0
  activeL2.value = 0
  page.value = 1
  loadSkills()
}
function onL2Change(v) {
  activeL2.value = parseInt(v, 10) || 0
  page.value = 1
  loadSkills()
}
function tagClass(s) {
  return s.scope === 'official' ? 'tg-c' : s.scope === 'repo' ? 'tg-i' : 'tg-d'
}
function scopeName(scope) {
  const map = {
    official: tt('scope_official') === 'scope_official' ? '技能市场' : tt('scope_official'),
    repo: td('scope_repo'),
    user: td('scope_user'),
  }
  return map[scope] || scope
}
function scenesArrOf(s) {
  return (s.scenes || []).map(function (sc) {
    return sc.display_name || sc.name || sc
  })
}
function scenesShort(s) {
  const a = scenesArrOf(s)
  if (!a.length) return ''
  return a.length <= 2 ? a.join(' · ') : a.slice(0, 2).join(' · ') + '(+' + (a.length - 2) + ')'
}
function scenesFull(s) {
  return scenesArrOf(s).join(' · ')
}
function sceneCount(s) {
  return s.scene_count || scenesArrOf(s).length || 0
}

// ── 列表状态 ──
const tab = ref('official')
const sort = ref('latest')
const qDraft = ref('')
const q = ref('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const pageTotal = ref(0)
const skills = ref([])
const featured = ref([])
const agentStats = ref({})
const categories = ref([])
const catTree = ref([])
const activeL1 = ref(0)
const activeL2 = ref(0)
const l2Options = computed(function () {
  var hit = catTree.value.filter(function (c) { return c.id === activeL1.value })[0]
  return (hit && hit.children) || []
})
const l1Opts = computed(function () {
  var opts = [{ value: '0', label: '未分类' }]
  catTree.value.forEach(function (c) {
    opts.push({ value: String(c.id), label: (c.icon || '') + ' ' + c.name })
  })
  return opts
})
const uploadL2Opts = computed(function () {
  var hit = catTree.value.filter(function (c) { return String(c.id) === String(uploadForm.catL1) })[0]
  var opts = [{ value: '0', label: '未分类' }]
  ;((hit && hit.children) || []).forEach(function (c) {
    opts.push({ value: String(c.id), label: (c.icon || '') + ' ' + c.name })
  })
  return opts
})
const activeAgent = ref(-1)
const activeTag = ref('all')
const activeTagId = ref('')
const listLoading = ref(false)
const currentRepo = ref('')
const totalPages = computed(() => Math.max(1, Math.ceil(pageTotal.value / pageSize.value)))

function stale() {
  return G.window._currentView !== 'skills' && G.window._currentView !== 'skills:' + tab.value
}

function loadCategories() {
  if (categories.value.length) return
  G.API.get('/skills/categories')
    .then(function (d) {
      categories.value = d.categories || []
      catTree.value = d.tree || []
    })
    .catch(function () {})
}
function loadAgentStats() {
  const params = ['scope=' + tab.value]
  if (currentRepo.value) params.push('repo=' + encodeURIComponent(currentRepo.value))
  G.API.get('/skills/agent-stats?' + params.join('&'))
    .then(function (d) {
      var m = {}
      ;(d.agents || []).forEach(function (a) {
        m[a.agent_type] = a.count
      })
      agentStats.value = m
      total.value = d.total || 0
    })
    .catch(function () {})
}
function loadSkills() {
  listLoading.value = true
  const params = []
  if (currentRepo.value) params.push('repo=' + encodeURIComponent(currentRepo.value))
  if (tab.value) params.push('scope=' + tab.value)
  if (sort.value) params.push('sort=' + sort.value)
  if (q.value) params.push('q=' + encodeURIComponent(q.value))
  if (activeAgent.value >= 0 && agentList[activeAgent.value])
    params.push('agent_type=' + agentList[activeAgent.value])
  if (activeTagId.value) params.push('category_id=' + encodeURIComponent(activeTagId.value))
  if (activeL1.value) params.push('cat_l1_id=' + activeL1.value)
  if (activeL2.value) params.push('cat_l2_id=' + activeL2.value)
  params.push('page=' + page.value)
  params.push('page_size=' + pageSize.value)
  G.API.get('/skills?' + params.join('&'))
    .then(function (d) {
      if (stale()) return
      skills.value = d.skills || []
      pageTotal.value = d.total || skills.value.length
      listLoading.value = false
    })
    .catch(function (e) {
      if (stale() || (e && e.name === 'AbortError' && e.silent)) return
      skills.value = []
      pageTotal.value = 0
      listLoading.value = false
    })
}
function loadFeatured() {
  if (tab.value !== 'official') return
  G.API.get('/skills/featured')
    .then(function (d) {
      if (!stale()) featured.value = d.skills || []
    })
    .catch(function () {
      featured.value = []
    })
}
function initRepo() {
  try {
    var c = G.app.currentRepo()
    currentRepo.value = c ? c.fullName || '' : ''
  } catch (e) {
    currentRepo.value = ''
  }
}
function loadAll() {
  G.window._currentView = 'skills:' + tab.value
  initRepo()
  loadCategories()
  loadAgentStats()
  loadFeatured()
  loadSkills()
}

function switchTab(t) {
  tab.value = t
  page.value = 1
  activeAgent.value = -1
  activeTag.value = 'all'
  activeTagId.value = ''
  loadAll()
}
function switchAgent(i) {
  activeAgent.value = activeAgent.value === i ? -1 : i
  activeTag.value = 'all'
  activeTagId.value = ''
  page.value = 1
  loadSkills()
}
function switchTag(label, id) {
  activeTag.value = label
  activeTagId.value = id || ''
  page.value = 1
  loadSkills()
}
function resetFilters() {
  activeAgent.value = -1
  activeTag.value = 'all'
  activeTagId.value = ''
  q.value = ''
  qDraft.value = ''
  page.value = 1
  loadSkills()
}
function setSort(s) {
  sort.value = s
  page.value = 1
  loadSkills()
}
function doSearch() {
  q.value = qDraft.value
  page.value = 1
  loadSkills()
}
function goPage(p) {
  page.value = Math.min(totalPages.value, Math.max(1, p))
  loadSkills()
}
function setPageSize(v) {
  pageSize.value = parseInt(v, 10)
  page.value = 1
  loadSkills()
}

function tagStyle(on) {
  return on
    ? 'padding:5px 12px;font-size:11px;color:var(--a);border:1px solid var(--a);background:rgba(49,104,224,0.06);cursor:pointer;border-radius:8px;transition:all .15s;white-space:nowrap;text-decoration:none'
    : 'padding:5px 12px;font-size:11px;color:var(--t2);border:1px solid var(--b);background:var(--c);cursor:pointer;border-radius:8px;transition:all .15s;white-space:nowrap;text-decoration:none'
}

// ── 点赞 ──
function onLike(s) {
  G.API.post('/skills/' + s.id + '/like', {})
    .then(function (r) {
      s.liked_by_me = !!r.liked
      s.like_count = r.like_count || 0
    })
    .catch(function () {
      G.app.showToast(
        tt('toast.like_failed') === 'toast.like_failed' ? '操作失败' : tt('toast.like_failed')
      )
    })
}

// ── 详情页 ──
const detail = ref(null)
const detailLoading = ref(false)
const skdTab = ref('overview')
const commentDraft = ref('')
const commentBusy = ref(false)
const skdComments = reactive({ comments: [], total: 0, page: 1, pageSize: 20 })
const skdFiles = computed(function () {
  return (detail.value && detail.value.files) || []
})
const skdVersions = computed(function () {
  return (detail.value && detail.value.versions) || []
})
const skdRelated = computed(function () {
  return (detail.value && detail.value.related) || []
})
const detailTags = computed(function () {
  return (detail.value && detail.value.tags) || []
})
const detailSummary = computed(function () {
  var d = detail.value
  if (!d) return ''
  var s = d.summary || d.description || ''
  s = String(s).replace(/\r?\n/g, ' ').split(/→|->/)[0].trim()
  return s.length > 50 ? s.slice(0, 50) : s || '—'
})
const detailVersion = computed(function () {
  var d = detail.value
  if (!d) return '—'
  if (d.version) return d.version
  var m = (d.content || '').match(/^---[\s\S]*?^version:\s*(.+?)$/m)
  return m ? m[1].trim() : '—'
})
const scopeLabel = computed(function () {
  return scopeName((detail.value && detail.value.scope) || 'official')
})
const sceneChips = computed(function () {
  if (!detail.value) return []
  return (detail.value.scenes || []).map(function (sc) {
    if (typeof sc === 'string') return { id: '', name: sc }
    return { id: sc.id || sc.scene_id || sc.name || '', name: sc.display_name || sc.name || '' }
  })
})
const skillToc = computed(function () {
  var content = (detail.value && detail.value.content) || ''
  var out = []
  var seen = {}
  content.replace(/^##\s+(.+)$/gm, function (_m, title) {
    var id = headingId(title)
    if (seen[id]) id = id + '-' + out.length
    seen[id] = true
    out.push({ id: id, text: title.trim() })
    return _m
  })
  return out
})
const detailHtml = computed(function () {
  if (!detail.value || !detail.value.content) return ''
  var html = renderSkillContent(detail.value.content)
  var toc = skillToc.value
  var i = 0
  return html.replace(/<h2([^>]*)>([\s\S]*?)<\/h2>/g, function (m, attrs, inner) {
    var id = toc[i] ? toc[i].id : 'skd-sec-' + i
    i++
    return '<h2' + attrs + ' id="' + id + '">' + inner + '</h2>'
  })
})
const overviewSlots = computed(function () {
  return extractOverview(detail.value)
})
const hasOverview = computed(function () {
  return overviewSlots.value.length > 0
})
const skdTabs = computed(function () {
  return [
    { key: 'overview', label: tt('tab_overview'), count: null },
    { key: 'skillmd', label: tt('tab_skillmd'), count: null },
    { key: 'resources', label: tt('tab_resources'), count: (detail.value && detail.value.file_count) || 0 },
    { key: 'related', label: tt('tab_related'), count: skdRelated.value.length },
    { key: 'versions', label: tt('tab_versions'), count: skdVersions.value.length },
  ]
})
const sourceUrl = computed(function () {
  var d = detail.value || {}
  if (d.scope === 'official') {
    return 'https://gitcode.com/grape-dev/grape-skills/tree/main/' + encodeURIComponent(d.name || '')
  }
  var repo = (G.app && G.app.currentRepo && G.app.currentRepo()) || null
  return repo && repo.htmlUrl ? repo.htmlUrl : 'https://gitcode.com/grape-dev/grape'
})
const issueUrl = computed(function () {
  var d = detail.value || {}
  return (
    'https://gitcode.com/grape-dev/grape/issues/new?title=' +
    encodeURIComponent('[技能] ' + (d.display_name || d.name || '') + ' 问题反馈')
  )
})
function headingId(title) {
  return 'skd-h-' + String(title || '')
    .trim()
    .toLowerCase()
    .replace(/[^\w\u4e00-\u9fa5]+/g, '-')
    .replace(/^-+|-+$/g, '')
}
function formatDate(v) {
  if (!v) return '—'
  var s = String(v).replace('T', ' ')
  return s.slice(0, 10)
}
function formatSize(n) {
  var v = Number(n) || 0
  if (v >= 1024 * 1024) return (v / (1024 * 1024)).toFixed(1) + ' MB'
  if (v >= 1024) return (v / 1024).toFixed(1) + ' KB'
  return v + ' B'
}
function sectionText(content, names) {
  var lines = String(content || '').split(/\r?\n/)
  var out = []
  var hit = false
  var level = 0
  for (var i = 0; i < lines.length; i++) {
    var m = lines[i].match(/^(#{1,4})\s+(.+)$/)
    if (m) {
      var title = m[2].trim()
      var matched = names.some(function (n) {
        return title.indexOf(n) >= 0
      })
      if (hit && matched) break
      if (matched) {
        hit = true
        level = m[1].length
        continue
      }
      if (hit && m[1].length <= level) break
      if (hit) out.push(lines[i])
      continue
    }
    if (hit) out.push(lines[i])
  }
  return out.join('\n').trim()
}
function cleanLines(text, limit, stripBullet) {
  if (!text) return []
  var out = []
  String(text)
    .split(/\r?\n/)
    .forEach(function (ln) {
      var t = ln.trim()
      if (!t) return
      t = t.replace(/^[-*+]\s+/, '')
      if (stripBullet) t = t.replace(/^\d+[.)]\s+/, '')
      t = t.replace(/^[✅❌]\s*/, '')
      if (t.length > 120) t = t.slice(0, 120)
      if (t) out.push(t)
    })
  return out.slice(0, limit || 5)
}
function extractOverview(d) {
  if (!d || !d.content) return []
  var content = d.content
  var slots = []
  var positioning = cleanLines(sectionText(content, ['定位']), 3)
  if (positioning.length) slots.push({ key: 'summary', title: tt('ov_summary'), items: positioning, kind: 'list' })
  var steps = cleanLines(sectionText(content, ['处理流程', '流程']), 6, true)
  if (steps.length) slots.push({ key: 'steps', title: tt('ov_steps'), items: steps, kind: 'steps' })
  var constraints = cleanLines(sectionText(content, ['操作边界', '约束']), 5)
  if (constraints.length)
    slots.push({ key: 'constraints', title: tt('ov_constraints'), items: constraints, kind: 'list' })
  var products = cleanLines(sectionText(content, ['任务完成流程', '输出规范']), 4)
  if (products.length) slots.push({ key: 'products', title: tt('ov_products'), items: products, kind: 'list' })
  var scopeLines = []
  var at = agentMetaOf(d.agent_type).label || d.agent_type || ''
  if (at) scopeLines.push(at + ' Agent')
  var scs = sceneChips.value.map(function (x) {
    return x.name
  })
  if (scs.length) scopeLines.push('适用场景：' + scs.join('、'))
  if (scopeLines.length)
    slots.push({ key: 'scope', title: tt('ov_scope'), items: scopeLines, kind: 'list' })
  return slots
}
function setSkdTab(key) {
  skdTab.value = key
  try {
    var id = detail.value && detail.value.id
    if (id) {
      G.window.history.replaceState(
        { view: 'skill', param: String(id) },
        '',
        '#skill/' + encodeURIComponent(id) + '/' + key
      )
    }
  } catch (e) {
    /* ignore */
  }
}
function jumpToHeading(id) {
  var el = document.getElementById(id)
  if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
function openScene(sc) {
  if (!sc || !sc.id) return
  if (G.window.renderSceneDetail) G.window.renderSceneDetail(sc.id)
  else if (G.app && G.app.nav) G.app.nav('scene-detail', sc.id)
}
function loadDetailComments(reset) {
  var d = detail.value
  if (!d || !d.id) return
  var page = reset ? 1 : skdComments.page + 1
  G.API.get(
    '/skills/by-id/' + encodeURIComponent(d.id) + '/comments?page=' + page + '&page_size=' + skdComments.pageSize
  )
    .then(function (r) {
      var list = r.comments || []
      skdComments.comments = reset ? list : skdComments.comments.concat(list)
      skdComments.total = r.total || 0
      skdComments.page = page
    })
    .catch(function () {})
}
function loadMoreComments() {
  loadDetailComments(false)
}
function submitComment() {
  var d = detail.value
  if (!d || !d.id) return
  var u = null
  try {
    u = JSON.parse(G.window.localStorage.getItem('user') || 'null')
  } catch (e) {
    u = null
  }
  if (!u) {
    G.app.showToast(tt('login_required'))
    if (G.app.showLogin) G.app.showLogin()
    return
  }
  var content = (commentDraft.value || '').trim()
  if (!content) return
  commentBusy.value = true
  G.API.post('/skills/by-id/' + encodeURIComponent(d.id) + '/comments', { content: content })
    .then(function (r) {
      commentBusy.value = false
      commentDraft.value = ''
      if (r && r.comment) {
        skdComments.comments.unshift(r.comment)
        skdComments.total += 1
        if (d) d.comment_count = (d.comment_count || 0) + 1
      }
    })
    .catch(function (e) {
      commentBusy.value = false
      G.app.showToast((e && e.message) || '评论发表失败')
    })
}
function resetDetailPanels() {
  skdTab.value = 'overview'
  commentDraft.value = ''
  skdComments.comments = []
  skdComments.total = 0
  skdComments.page = 1
}
function openDetail(id, tabKey) {
  G.window._currentView = 'skill'
  detail.value = null
  pkg.value = null
  subSkill.value = null
  resetDetailPanels()
  detailLoading.value = true
  G.API.get('/skills/by-id/' + encodeURIComponent(id))
    .then(function (d) {
      if (G.window._currentView !== 'skill') return
      if (d.name === 'huawei-cloud') {
        // 华为云技能包 → 子技能页面
        pkg.value = d
        loadSubSkills(d.id)
      } else {
        detail.value = d
        if (tabKey) skdTab.value = tabKey
        loadDetailComments(true)
      }
      detailLoading.value = false
    })
    .catch(function (e) {
      if (G.window._currentView !== 'skill') return
      detailLoading.value = false
      G.app.showToast(
        (tc('load_failed') === 'load_failed' ? '加载失败' : tc('load_failed')) +
          ': ' +
          ((e && e.message) || '')
      )
    })
}
// renderSkillDetail 全局契约由入口 entries/skills-view.js 持有（fresh mount + initialDetail 注入），
// 组件内不覆盖——旧实例闭包在 unmount 后不会更新 DOM（批4同款坑）
function backFromDetail() {
  const from = G.window._skillDetailFrom
  if (!from || !from.view) {
    G.app.nav('scene-market')
    return
  }
  if (from.view === 'scene-detail' && from.param) {
    G.window.renderSceneDetail && G.window.renderSceneDetail(from.param)
    try {
      G.window.history.replaceState(
        { view: 'scene-detail', param: from.param },
        '',
        '#scene-detail/' + encodeURIComponent(from.param)
      )
    } catch (e) {
      /* replaceState 在隐私模式等场景可能抛错，忽略（与老代码一致） */
    }
  } else {
    // 回技能列表
    detail.value = null
    pkg.value = null
    subSkill.value = null
    G.window._currentView = 'skills:' + tab.value
    try {
      G.window.history.replaceState({ view: 'skills' }, '', '#skills')
    } catch (e) {
      /* ignore */
    }
    loadAll()
  }
}

// ── 华为云包 ──
const pkg = ref(null)
const subSkills = ref([])
const subLoading = ref(false)
const subSkill = ref(null)
function loadSubSkills(pkgId) {
  subLoading.value = true
  subSkills.value = []
  G.API.get('/skills/' + pkgId + '/sub')
    .then(function (d) {
      subSkills.value = d.sub_skills || []
      subLoading.value = false
    })
    .catch(function () {
      subSkills.value = []
      subLoading.value = false
    })
}
function openSubSkill(subName) {
  subSkill.value = null
  G.API.get('/skills/package-sub/' + pkg.value.id + '/' + subName)
    .then(function (d) {
      if (G.window._currentView !== 'skill' && G.window._currentView !== 'skills') return
      subSkill.value = d
    })
    .catch(function (e) {
      if (e && e.name === 'AbortError' && e.silent) return
      G.app.showToast(
        (tc('load_failed') === 'load_failed' ? '加载失败' : tc('load_failed')) +
          ': ' +
          ((e && e.message) || '')
      )
    })
}
function backToPkg() {
  subSkill.value = null
}
const mdModal = reactive({ open: false, content: '' })
function viewPkgMd() {
  G.API.get('/skills/by-id/' + pkg.value.id)
    .then(function (d) {
      mdModal.content = d.content || ''
      mdModal.open = true
    })
    .catch(function () {})
}

// ── YAML front matter 解析（Issue #232，与老 _parseYAMLFrontMatter 等价）──
function parseYAMLFrontMatter(text) {
  var lines = text.split(/\r?\n/)
  var entries = []
  var i = 0
  while (i < lines.length) {
    var line = lines[i]
    if (!line.trim() || line.trim().indexOf('#') === 0) {
      i++
      continue
    }
    var kvMatch = line.match(/^(\w[\w.-]*)\s*:\s*(.*)$/)
    if (!kvMatch) {
      i++
      continue
    }
    var key = kvMatch[1]
    var valPart = kvMatch[2].trim()
    var value
    if (valPart === '|' || valPart === '>' || valPart === '|-' || valPart === '>-') {
      i++
      var multiline = []
      while (i < lines.length) {
        var ml = lines[i]
        if (ml.trim() === '') {
          multiline.push('')
          i++
          continue
        }
        if (/^\s/.test(ml)) {
          multiline.push(ml.replace(/^\s{2}/, ''))
          i++
        } else break
      }
      value = multiline.join('\n').trim()
    } else if (valPart === '') {
      i++
      var listItems = []
      while (i < lines.length) {
        var li = lines[i]
        if (/^\s+-\s+/.test(li)) {
          listItems.push(li.replace(/^\s+-\s+/, '').trim())
          i++
        } else if (/^\s{2,}\w/.test(li) && !/^\s+-\s/.test(li)) {
          listItems.push(li.trim())
          i++
        } else break
      }
      value = listItems.length > 0 ? listItems.join(', ') : ''
    } else {
      value = valPart
      i++
    }
    entries.push({ key: key, value: value })
  }
  return entries
}
function escapeHtml(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}
function renderSkillContent(rawContent) {
  if (!rawContent) return ''
  var fmMatch = rawContent.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/)
  if (!fmMatch) return G.window.renderMarkdown(rawContent)
  var entries = parseYAMLFrontMatter(fmMatch[1])
  if (entries.length === 0) return G.window.renderMarkdown(rawContent)
  var fmHtml = '<table class="fm-table"><tbody>'
  for (var i = 0; i < entries.length; i++) {
    fmHtml +=
      '<tr><th>' +
      escapeHtml(entries[i].key) +
      '</th><td>' +
      escapeHtml(entries[i].value) +
      '</td></tr>'
  }
  fmHtml += '</tbody></table>'
  var bodyText = rawContent.slice(fmMatch[0].length)
  var bodyHtml = bodyText.trim() ? G.window.renderMarkdown(bodyText) : ''
  return fmHtml + bodyHtml
}

// ── 上传 ──
const showUpload = ref(false)
const uploading = ref(false)
const uploadDone = ref(false)
const uploadPct = ref(0)
const uploadForm = reactive({ agentType: '', categoryId: '0', catL1: '0', catL2: '0', tags: '', summary: '', file: null })
// 一级分类变更时重置二级，避免提交旧一级下的二级形成跨父级组合
watch(
  () => uploadForm.catL1,
  function (next, prev) {
    if (next !== prev) uploadForm.catL2 = '0'
  }
)
const agentOpts = agentList.map(function (at) {
  return { value: at, label: (AGENT_META[at] ? AGENT_META[at].label : at) + ' Agent' }
})
const categoryOpts = computed(function () {
  var opts = [{ value: '0', label: KEY2_DEFAULTS['upload.uncategorized'] }]
  categories.value.forEach(function (c) {
    opts.push({ value: String(c.id), label: (c.icon || '') + ' ' + c.name })
  })
  return opts
})
function downloadTemplate() {
  var token = encodeURIComponent(G.window.localStorage.getItem('token') || '')
  G.window.open('/api/skills/template?token=' + token, '_blank')
}
function doUpload() {
  if (!uploadForm.file) {
    G.app.showToast(
      KEY2_DEFAULTS['upload.file_label'] === 'upload.file_label'
        ? '请选择 ZIP 文件'
        : '请选择 ZIP 文件'
    )
    return
  }
  if (!uploadForm.agentType) {
    G.app.showToast('请选择 Agent 类型')
    return
  }
  var fd = new FormData()
  fd.append('file', uploadForm.file)
  fd.append('scope', tab.value)
  fd.append('agent_type', uploadForm.agentType)
  fd.append('category_id', String(parseInt(uploadForm.categoryId, 10)))
  fd.append('cat_l1_id', String(parseInt(uploadForm.catL1, 10) || 0))
  fd.append('cat_l2_id', String(parseInt(uploadForm.catL2, 10) || 0))
  fd.append('tags', uploadForm.tags || '')
  fd.append('summary', uploadForm.summary || '')
  if (tab.value === 'repo' && currentRepo.value) fd.append('repo', currentRepo.value)
  uploading.value = true
  uploadPct.value = 0
  uploadDone.value = false
  G.API.upload('/skills/upload', fd, function (pct) {
    uploadPct.value = pct
  })
    .then(function (r) {
      uploading.value = false
      uploadDone.value = true
      showUpload.value = false
      G.app.showToast(r.message || '上传成功')
      loadAll()
    })
    .catch(function (e) {
      uploading.value = false
      uploadPct.value = 0
      var msg = (e && e.message) || '上传失败'
      if (e && e.errors && Array.isArray(e.errors) && e.errors.length > 0) msg = e.errors.join('；')
      G.app.showToast(msg)
    })
}

// ── Issue #561：上传更新技能（覆盖当前内容，ID/场景引用/点赞保留） ──
const updateCtx = reactive({
  open: false,
  id: 0,
  name: '',
  file: null,
  summary: '',
  changeNote: '',
  busy: false,
  pct: 0,
  done: false,
})
function askUpdate(s) {
  updateCtx.open = true
  updateCtx.id = s && s.id
  updateCtx.name = (s && (s.display_name || s.name)) || ''
  updateCtx.file = null
  updateCtx.summary = ''
  updateCtx.changeNote = ''
  updateCtx.busy = false
  updateCtx.pct = 0
  updateCtx.done = false
}
function doUpdate() {
  if (!updateCtx.file) {
    G.app.showToast(tt2('toast.select_zip'))
    return
  }
  const fd = new FormData()
  fd.append('file', updateCtx.file)
  fd.append('summary', updateCtx.summary || '')
  fd.append('change_note', updateCtx.changeNote || '')
  updateCtx.busy = true
  updateCtx.pct = 0
  updateCtx.done = false
  G.API.upload('/skills/by-id/' + encodeURIComponent(updateCtx.id) + '/update', fd, function (pct) {
    updateCtx.pct = pct
  })
    .then(function (r) {
      updateCtx.busy = false
      updateCtx.done = true
      updateCtx.open = false
      G.app.showToast((r && r.message) || tt2('toast.update_success'))
      loadAll()
    })
    .catch(function (e) {
      updateCtx.busy = false
      updateCtx.pct = 0
      let msg = (e && e.message) || tt2('toast.update_failed')
      if (e && e.errors && Array.isArray(e.errors) && e.errors.length > 0) msg = e.errors.join('；')
      G.app.showToast(msg)
    })
}

// ── 删除 ──
const deleteCtx = reactive({ open: false, skill: null, busy: false })
function askDelete(s) {
  deleteCtx.skill = s
  deleteCtx.busy = false
  deleteCtx.open = true
}
function confirmDelete() {
  if (!deleteCtx.skill) return
  deleteCtx.busy = true
  G.API.del('/skills/by-id/' + encodeURIComponent(deleteCtx.skill.id))
    .then(function (r) {
      deleteCtx.open = false
      G.app.showToast(r.message || '已删除')
      loadAll()
    })
    .catch(function (e) {
      deleteCtx.busy = false
      G.app.showToast('删除失败' + (e && e.message ? '：' + e.message : ''))
    })
}

// ── 转换（复制） ──
const convertCtx = reactive({ open: false, skill: null, sourceScope: 'user', busy: false })
const convertDesc = computed(function () {
  if (!convertCtx.skill) return ''
  var isUserToRepo = convertCtx.sourceScope === 'user'
  var sourceLabel = isUserToRepo ? '个人' : '仓库'
  var targetDesc = isUserToRepo ? '此仓库的仓库' : '个人'
  return '将' + sourceLabel + '技能复制为' + targetDesc + '技能，原' + sourceLabel + '技能保留'
})
function askConvert(s, sourceScope) {
  convertCtx.skill = s
  convertCtx.sourceScope = sourceScope || 'user'
  convertCtx.busy = false
  convertCtx.open = true
}
function confirmConvert() {
  if (!convertCtx.skill || !currentRepo.value) {
    G.app.showToast('请先选择仓库')
    return
  }
  convertCtx.busy = true
  G.API.post('/skills/convert', {
    skill_name: convertCtx.skill.name,
    repo: currentRepo.value,
    source_scope: convertCtx.sourceScope,
  })
    .then(function (r) {
      convertCtx.open = false
      G.app.showToast(r.message || '复制成功')
      loadAll()
    })
    .catch(function (e) {
      convertCtx.busy = false
      G.app.showToast('复制失败：' + ((e && e.message) || ''))
    })
}

// ── 下载技能包 ──
function downloadZip(id) {
  G.API.getRaw('/skills/by-id/' + encodeURIComponent(id) + '/download')
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status)
      var ct = r.headers.get('content-type') || ''
      if (ct.includes('application/json')) {
        return r.json().then(function (d) {
          if (d && d.url) {
            G.window.open(d.url, '_blank')
            return
          }
          throw new Error('no url')
        })
      }
      return r.blob().then(function (b) {
        var a = document.createElement('a')
        a.href = URL.createObjectURL(b)
        a.download = 'skill-' + id + '.zip'
        document.body.appendChild(a)
        a.click()
        a.remove()
        setTimeout(function () {
          URL.revokeObjectURL(a.href)
        }, 5000)
      })
    })
    .catch(function (e) {
      if (e && e.name === 'AbortError' && e.silent) return
      G.app.showToast('下载失败')
    })
}

onMounted(function () {
  // renderSkillDetail 入口：mount 即持详情数据（含 huawei-cloud 分包）
  if (props.initialDetail) {
    G.window._currentView = 'skill'
    if (props.initialDetail.name === 'huawei-cloud') {
      pkg.value = props.initialDetail
      loadSubSkills(props.initialDetail.id)
    } else {
      detail.value = props.initialDetail
      var m = (G.window.location.hash || '').match(/^#skill\/[^/]+\/([a-z]+)/)
      if (m) skdTab.value = m[1]
      loadDetailComments(true)
    }
    return
  }
  // 场景详情跳转过来带的预填搜索词（老 _skillMarketSearchInit 契约）
  if (G.window._skillMarketSearchInit) {
    q.value = G.window._skillMarketSearchInit
    qDraft.value = G.window._skillMarketSearchInit
    G.window._skillMarketSearchInit = null
  }
  loadAll()
})
</script>

<style scoped>
.sk {
  min-height: 100%;
}
.sk__top {
  margin-bottom: 20px;
  background: var(--c);
  border-radius: 14px;
  padding: 20px;
  text-align: center;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
}
.sk__seg {
  margin-top: 0;
}
.sk__intro {
  padding: 14px 0 0;
  font-size: 11px;
  color: var(--t3);
  line-height: 1.6;
}
.sk__intro-b {
  color: var(--a);
  font-weight: 600;
}
.sk__subtab {
  text-align: left;
  margin-bottom: 20px;
}
.sk__search {
  justify-content: space-between;
}
.sk__tools {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.sk__count {
  font-size: 10px;
  font-weight: 400;
  color: var(--t2);
  background: var(--bg);
  padding: 2px 8px;
  border-radius: 8px;
  margin-left: 4px;
}
.sk__q {
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
.sk__q:focus {
  border-color: var(--a);
  background: var(--c);
}
.sk__upload-btn {
  color: var(--on-accent);
  background: var(--g-fill);
  border-color: var(--g-fill);
}
.sk__norepo {
  padding: 12px;
}
.sk__cols {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  flex-wrap: wrap;
  margin-top: 10px;
}
.sk__agents {
  width: 160px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.sk__agent-ic {
  margin-right: 2px;
}
.sk__agent-count {
  font-size: 10px;
  font-weight: 400;
  color: var(--t2);
  background: var(--bg);
  padding: 1px 6px;
  border-radius: 8px;
  margin-left: 2px;
}
.sk__main {
  flex: 1;
  min-width: 280px;
}
.sk__tags {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.sk__tag-ic {
  margin-right: 3px;
}
.sk__empty {
  padding: 20px;
  text-align: center;
}
.sk__pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  margin-top: 16px;
  padding: 12px 0;
}
.sk__pginfo {
  font-size: 12px;
  color: var(--t2);
}
.sk__pgsize {
  padding: 4px 8px;
  border: 1px solid var(--b);
  border-radius: var(--rd);
  font-size: 11px;
  background: var(--c);
  color: var(--t);
  outline: none;
}
.sk__featcount {
  margin-top: auto;
  padding-top: 8px;
}
.sk__catpath {
  font-size: 10px;
  color: var(--t3);
  margin-bottom: 4px;
}
.sk__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 6px;
}
.sk__chip {
  font-size: 10px;
  line-height: 1;
  padding: 2px 6px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--a) 12%, transparent);
  color: var(--a);
}
.sk__cat-tag {
  font-size: 9px;
  background: rgba(49, 104, 224, 0.08);
  color: var(--a);
}
.sk__feat-tag {
  font-size: 9px;
  background: rgba(212, 156, 15, 0.12);
  color: var(--y);
}
.sk__scenes {
  color: var(--a);
}
.sk__like {
  cursor: pointer;
}
.sk__del-btn {
  color: var(--r);
  border-color: var(--r);
}

/* 华为云包 & 子技能 */
.sk__back-btn {
  margin-bottom: 12px;
}
.sk__pkg-head {
  padding: 16px 20px;
  margin-bottom: 14px;
}
.sk__pkg-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.sk__pkg-ic {
  font-size: 28px;
}
.sk__pkg-row h3 {
  font-size: 16px;
  font-weight: 600;
}
.sk__pkg-desc {
  font-size: 12px;
  color: var(--t2);
  margin-top: 4px;
}
.sk__pkg-viewmd {
  margin-left: auto;
}
.sk__subgrid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
}
.sk__subhint {
  padding: 20px;
  text-align: center;
  color: var(--t3);
  grid-column: 1 / -1;
}
.sk__subcard {
  padding: 14px;
  cursor: pointer;
}
.sk__subcard-h {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
.sk__subcard-ic {
  font-size: 16px;
}
.sk__subcard-name {
  font-size: 13px;
  font-weight: 600;
}
.sk__subcard-sname {
  font-size: 10px;
  color: var(--t3);
  margin-bottom: 6px;
}
.sk__subcard-desc {
  font-size: 11px;
  color: var(--t2);
  line-height: 1.5;
  flex: 1;
}
.sk__subdet {
  display: grid;
  grid-template-columns: 1fr 300px;
  gap: 14px;
  min-height: 500px;
}
.sk__subdet-main {
  padding: 16px 20px;
  overflow-y: auto;
}
.sk__subdet-h {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.sk__subdet-ic {
  font-size: 22px;
}
.sk__subdet-h h3 {
  font-size: 15px;
  font-weight: 600;
}
.sk__subdet-desc {
  font-size: 12px;
  color: var(--t2);
  margin-bottom: 14px;
  line-height: 1.5;
}
.sk__subdet-mdtitle {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 6px;
}
.sk__subdet-pre {
  background: var(--bg);
  padding: 14px;
  border-radius: var(--rd);
  font-size: 11px;
  font-family: monospace;
  line-height: 1.6;
  overflow-x: auto;
  white-space: pre-wrap;
}
.sk__subdet-side {
  padding: 16px 20px;
  font-size: 12px;
}
.sk__subdet-side h3 {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 10px;
}

/* 弹窗内部 */
.sk__form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.sk__srow {
  margin-bottom: 0;
}
.sk__f1 {
  flex: 1;
  min-width: 0;
}
.sk__file-fld {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 4px;
}
.sk__file-fld > label {
  font-size: 11px;
  color: var(--t2);
}
.sk__file {
  font-size: 12px;
}
.sk__fmt-hint {
  font-size: 11px;
  color: var(--t2);
  margin-bottom: 4px;
}
.sk__tpl-link {
  color: var(--a);
  cursor: pointer;
}
.sk__upload-prog {
  margin-bottom: 6px;
}
.sk__upload-txt {
  font-size: 11px;
  color: var(--t2);
  margin-bottom: 4px;
}
.sk__upload-bar {
  background: var(--bg);
  border-radius: 8px;
  height: 8px;
  overflow: hidden;
}
.sk__upload-bar > div {
  background: var(--a);
  height: 100%;
  transition: width 0.2s;
}
.sk__conf {
  text-align: center;
}
.sk__conf-ic {
  width: 48px;
  height: 48px;
  border-radius: 50%;
  background: var(--c-danger-soft, rgba(255, 59, 48, 0.1));
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto 12px;
  font-size: 22px;
}
.sk__conf-ic--blue {
  background: var(--c-accent-soft, rgba(0, 113, 227, 0.1));
}
.sk__conf h3 {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 6px;
}
.sk__conf-desc {
  font-size: 12px;
  color: var(--t3);
}
.sk__md-pre {
  max-height: 70vh;
  overflow-y: auto;
  font-family: monospace;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
}

/* ===== Issue #3168 技能详情页（skd-* 隔离，D12 规范） ===== */
.skd-back {
  margin-bottom: 12px;
  border: 1px solid var(--hairline);
  background: var(--c);
  border-radius: var(--rd);
  padding: 6px 14px;
  font-size: 12px;
  color: var(--t2);
  cursor: pointer;
}
.skd-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.skd-card {
  background: var(--c);
  border: 1px solid var(--hairline);
  border-radius: var(--r2);
  box-shadow: var(--sh);
  padding: 20px;
}
@media (max-width: 760px) {
  .skd-card {
    padding: 16px;
  }
}
.skd-head {
  display: flex;
  align-items: flex-start;
  gap: 16px;
}
.skd-ico {
  width: 52px;
  height: 52px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  color: #fff;
  border-radius: var(--rd);
  background: linear-gradient(145deg, var(--fg1, var(--a)), var(--fg2, var(--a-text)));
}
.skd-head-main {
  flex: 1;
  min-width: 0;
}
.skd-name-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.skd-name {
  margin: 0;
  font-size: 20px;
  line-height: 1.3;
}
.skd-badge {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  background: var(--chip-bg);
  color: var(--t2);
  padding: 2px 10px;
  font-size: 11px;
  margin-left: 4px;
}
.skd-tags {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin: 8px 0;
}
.skd-chip {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  background: var(--chip-bg);
  color: var(--t2);
  padding: 3px 10px;
  font-size: 12px;
}
.skd-chip-link {
  color: var(--a);
  cursor: pointer;
}
.skd-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--t3);
  margin-top: 8px;
}
.skd-contributor {
  color: var(--t2);
}
.skd-head-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
.skd-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid var(--hairline);
  background: var(--c);
  color: var(--t);
  border-radius: var(--rd);
  padding: 6px 14px;
  font-size: 12px;
  cursor: pointer;
  text-decoration: none;
}
.skd-btn:disabled {
  opacity: 0.6;
  cursor: default;
}
.skd-btn-primary {
  background: var(--a);
  border-color: var(--a);
  color: #fff;
}
.skd-like.liked {
  color: var(--a);
}
.skd-tabs {
  position: sticky;
  top: 0;
  z-index: 5;
  display: flex;
  gap: 4px;
  overflow-x: auto;
  background: var(--c);
  border: 1px solid var(--hairline);
  border-radius: var(--r2);
  box-shadow: var(--sh);
  padding: 0 8px;
}
.skd-tab {
  border: 0;
  background: transparent;
  padding: 12px 16px;
  font-size: 13px;
  color: var(--t2);
  border-bottom: 2px solid transparent;
  cursor: pointer;
  white-space: nowrap;
}
.skd-tab.on {
  color: var(--a);
  border-bottom-color: var(--a);
  font-weight: 600;
}
.skd-count {
  margin-left: 6px;
  font-size: 11px;
  color: var(--t3);
}
.skd-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.skd-h2 {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 12px;
  font-size: 15px;
}
.skd-h3 {
  margin: 0 0 6px;
  font-size: 13px;
  color: var(--t2);
}
.skd-summary {
  font-size: 15px;
  line-height: 1.6;
}
.skd-metatable .skd-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--hairline);
  font-size: 13px;
}
.skd-metatable .skd-row:last-child {
  border-bottom: 0;
  padding-bottom: 0;
}
.skd-metatable .skd-row > span {
  width: 76px;
  flex-shrink: 0;
  color: var(--t3);
  font-size: 12px;
}
.skd-scene-chips {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.skd-ov-slot {
  padding: 12px 0;
  border-bottom: 1px solid var(--hairline);
}
.skd-ov-slot:last-child {
  border-bottom: 0;
  padding-bottom: 0;
}
.skd-ov-slot ul,
.skd-ov-slot ol {
  margin: 0;
  padding-left: 20px;
}
.skd-ov-slot li {
  font-size: 13px;
  line-height: 1.7;
  color: var(--t2);
  margin: 2px 0;
}
.skd-desc {
  margin: 0;
  white-space: pre-wrap;
  line-height: 1.7;
  font-size: 13px;
  color: var(--t2);
}
.skd-comment-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.skd-textarea {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--hairline);
  border-radius: var(--rd);
  padding: 10px;
  background: var(--bg);
  color: var(--t);
  font: inherit;
  font-size: 13px;
  resize: vertical;
}
.skd-comment-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.skd-hint {
  font-size: 11px;
  color: var(--t3);
}
.skd-empty {
  padding: 16px;
  text-align: center;
  color: var(--t3);
  font-size: 13px;
}
.skd-comment {
  padding: 12px 0;
  border-bottom: 1px solid var(--hairline);
}
.skd-comment:last-child {
  border-bottom: 0;
}
.skd-comment-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--t3);
}
.skd-comment-head b {
  color: var(--t2);
}
.skd-comment-body {
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  color: var(--t);
}
.skd-more {
  padding-top: 12px;
  text-align: center;
}
.skd-md-layout {
  display: grid;
  grid-template-columns: 200px minmax(0, 1fr);
  gap: 16px;
}
@media (max-width: 760px) {
  .skd-md-layout {
    grid-template-columns: 1fr;
  }
  .skd-toc {
    display: none;
  }
}
.skd-toc {
  position: sticky;
  top: 56px;
  align-self: start;
  max-height: 70vh;
  overflow: auto;
  border-right: 1px solid var(--hairline);
  padding-right: 12px;
}
.skd-toc-title {
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--t3);
}
.skd-toc a {
  display: block;
  padding: 6px 0;
  font-size: 12px;
  color: var(--t2);
  cursor: pointer;
}
.skd-md-body {
  min-width: 0;
  overflow-x: auto;
}
.skd-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.skd-file {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--hairline);
  font-size: 13px;
}
.skd-file:last-child {
  border-bottom: 0;
}
.skd-file-name {
  word-break: break-all;
}
.skd-file-size {
  flex-shrink: 0;
  color: var(--t3);
}
.skd-related-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 16px;
}
.skd-related {
  display: flex;
  flex-direction: column;
  gap: 8px;
  cursor: pointer;
}
.skd-related-name {
  font-size: 14px;
  font-weight: 600;
}
.skd-related-desc {
  display: -webkit-box;
  overflow: hidden;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  font-size: 12px;
  line-height: 1.5;
  color: var(--t2);
}
.skd-related-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: var(--t3);
}
.skd-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.skd-table th {
  padding: 10px 8px;
  border-bottom: 1px solid var(--hairline);
  text-align: left;
  font-weight: 600;
  color: var(--t2);
}
.skd-table td {
  padding: 10px 8px;
  border-bottom: 1px solid var(--hairline);
  vertical-align: top;
}
</style>
