<template>
  <div class="kv-root">
    <div class="knowledge-page">
      <!-- ===== 列表视图 ===== -->
      <template v-if="view === 'list'">
        <template v-if="!firstLoad">
          <!-- 统计卡片（点击切换 tab，在 apple-card 外面） -->
          <div class="stats-bar">
            <div class="stat-card" @click="switchTab('system')">
              <div class="stat-info">
                <span class="stat-num">{{ stats.system }}</span>
                <span class="stat-label">{{ t('stats_system') }}</span>
              </div>
              <div class="stat-icon" style="background: rgba(0, 113, 227, 0.1); color: #0071e3">
                ⚙
              </div>
            </div>
            <div class="stat-card" @click="switchTab('agent')">
              <div class="stat-info">
                <span class="stat-num">{{ stats.agent }}</span>
                <span class="stat-label">{{ t('stats_agent') }}</span>
              </div>
              <div class="stat-icon" style="background: rgba(255, 149, 0, 0.1); color: #ff9500">
                🤖
              </div>
            </div>
            <div class="stat-card" @click="switchTab('user')">
              <div class="stat-info">
                <span class="stat-num">{{ stats.manual }}</span>
                <span class="stat-label">{{ t('stats_user') }}</span>
              </div>
              <div class="stat-icon" style="background: rgba(52, 199, 89, 0.1); color: #34c759">
                👤
              </div>
            </div>
          </div>

          <!-- Tab 栏 -->
          <div class="kb-tab-bar">
            <button
              class="kb-tab"
              :class="{ active: activeTab === 'system' }"
              @click="switchTab('system')"
            >
              {{ t('stats_system') }}
            </button>
            <button
              class="kb-tab"
              :class="{ active: activeTab === 'agent' }"
              @click="switchTab('agent')"
            >
              {{ t('stats_agent') }}
            </button>
            <button
              class="kb-tab"
              :class="{ active: activeTab === 'user' }"
              @click="switchTab('user')"
            >
              {{ t('stats_user') }}
            </button>
          </div>

          <div class="apple-card" style="padding: 20px">
            <!-- 筛选器 -->
            <div class="filter-bar">
              <select v-if="activeTab === 'user'" v-model="filter.refStatus" class="kb-select">
                <option value="">{{ t('filter_ref_all') }}</option>
                <option value="linked">{{ t('filter_ref_linked') }}</option>
                <option value="unlinked">{{ t('filter_ref_unlinked') }}</option>
              </select>
              <input
                v-model="filter.query"
                :placeholder="t('search_placeholder')"
                class="kb-input"
                @keyup.enter="loadList"
              />
              <button class="bt bt-o bt-s" @click="loadList">{{ t('search_btn') }}</button>
              <button
                class="bt bt-s"
                :class="filter.sortLatest ? 'bt-p' : 'bt-o'"
                @click="toggleSortLatest"
              >
                {{ t('latest') }}
              </button>
              <button v-if="activeTab === 'user'" class="bt bt-p bt-s" @click="openEntryModal">
                {{ t('entry_create') }}
              </button>
            </div>

            <div v-if="loading" class="kb-loading-overlay">
              <div class="kb-spinner"></div>
              <span style="font-size: 12px; color: var(--t3); margin-top: 8px">{{
                t('loading')
              }}</span>
            </div>
            <div v-if="filteredList.length === 0 && !loading" class="ts t2" style="padding: 12px">
              {{ t('empty') }}
            </div>
            <div v-show="filteredList.length > 0 && !loading" class="kb-grid">
              <div
                v-for="k in filteredList"
                :key="k.id || k.file_path"
                class="skill-card"
                :class="{ 'archived-card': k.archived }"
                @click="viewContent(k)"
              >
                <div class="scn-card-bar" :class="sourceBarClass(k.source)"></div>
                <div class="scn-card-body">
                  <div class="scn-card-head">
                    <div class="scn-ic" :class="sourceBarClass(k.source)">
                      {{ sourceIcon(k.source) }}
                    </div>
                    <div class="scn-card-title">{{ k.name || knowledgeTitle(k) }}</div>
                  </div>
                  <div class="scn-card-desc" :title="k.description || t('no_description')">
                    {{ k.description || t('no_description') }}
                  </div>
                  <div class="kv-ref-row">
                    <span
                      v-if="sceneRefCount(k) > 0"
                      class="kb-ref-pill"
                      :title="'场景：' + sceneNames(k).join('、')"
                      @click.stop="viewContent(k)"
                    >
                      {{ t('scenes_unit') }}{{ sceneRefCount(k) }}
                    </span>
                    <span
                      v-if="agentRefCount(k) > 0"
                      class="kb-ref-pill"
                      :title="'Agent：' + agentNames(k).join('、')"
                      @click.stop="viewContent(k)"
                    >
                      Agent {{ agentRefCount(k) }}
                    </span>
                  </div>
                  <div class="scn-card-foot">
                    <span
                      class="scn-meta"
                      :title="k.created_by + ' · ' + (k.created_at || '').substring(0, 16)"
                      >{{ k.created_by || '' }} · {{ (k.created_at || '').substring(0, 16) }}</span
                    >
                    <div class="scn-card-btns">
                      <button class="bt bt-p bt-s" @click.stop="viewContent(k)">
                        {{ t('view') }}
                      </button>
                      <button
                        v-if="k.source === 'manual' && !k.archived"
                        class="bt bt-o bt-s kv-del-btn"
                        :disabled="submitting"
                        @click.stop="quickDelete(k)"
                      >
                        {{ t('delete') }}
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <!-- 分页器 -->
            <div class="kb-pager">
              <button class="kb-page-btn" :disabled="page <= 1" @click="changePage(page - 1)">
                ‹
              </button>
              <span class="kb-pager-sum">{{ page }} / {{ totalPages }} ({{ total }})</span>
              <button
                class="kb-page-btn"
                :disabled="page >= totalPages"
                @click="changePage(page + 1)"
              >
                ›
              </button>
              <select
                v-model.number="pageSize"
                class="kb-select"
                style="width: auto"
                @change="changePage(1)"
              >
                <option :value="20">20/页</option>
                <option :value="50">50/页</option>
              </select>
            </div>
          </div>
        </template>
      </template>

      <!-- ===== 详情视图 ===== -->
      <template v-if="view === 'detail' && detail && !editMode">
        <div class="kv-back">
          <button class="bt bt-o bt-s" @click="backToList">← {{ t('back_to_list') }}</button>
        </div>

        <!-- 头部卡片：标签 + 元数据 + 操作按钮 -->
        <div class="cd kv-head-card">
          <div class="kv-head-main">
            <div class="kv-head-tags">
              <span class="source-tag" :class="sourceClass(detail.source)">{{
                sourceLabel(detail.source)
              }}</span>
              <span v-if="detail.issue_tag" class="issue-tag">{{ detail.issue_tag }}</span>
            </div>
            <h2 class="kv-head-title">{{ detail.name || knowledgeTitle(detail) }}</h2>
            <div class="kv-head-desc">{{ detail.description || t('no_description') }}</div>
            <div class="kv-head-meta">
              {{ detail.created_by || '' }} · {{ (detail.created_at || '').substring(0, 16) }}
            </div>
          </div>
          <div class="kv-head-actions">
            <button
              v-if="detail.source === 'manual' && !detail.archived"
              class="bt bt-p bt-s"
              @click="editKnowledge"
            >
              {{ t('edit_save') }}
            </button>
            <button
              v-if="detail.source === 'manual' && !detail.archived"
              class="bt bt-o bt-s kv-del-btn"
              @click="deleteKnowledge"
            >
              {{ t('delete') }}
            </button>
          </div>
        </div>

        <!-- 内容卡片（固定高度，超出滚动） -->
        <div class="cd kb-detail-card kv-content-card">
          <div class="cd-h" style="margin-bottom: 12px">
            <h2 class="kv-card-title">{{ t('detail_content_title') }}</h2>
          </div>
          <div class="kb-detail-scroll">
            <div class="kb-detail-content" v-html="renderMarkdown(detail.content || '')"></div>
          </div>
        </div>

        <!-- 系统知识：自动加载，不显示关联管理 -->
        <div
          v-if="detail.source === 'system'"
          class="cd kb-detail-card"
          style="padding: 20px; margin-bottom: 20px; border-radius: 18px"
        >
          <div class="ts t2" style="text-align: center">{{ t('ref_system_auto') }}</div>
        </div>

        <!-- 用户知识：可配置 Agent/场景 -->
        <template v-if="detail.source === 'manual'">
          <!-- Agent 关联卡片（标签式，点击切换） -->
          <div
            class="cd kb-detail-card"
            style="padding: 24px; margin-bottom: 20px; border-radius: 18px"
          >
            <div class="cd-h" style="margin-bottom: 14px">
              <h2 class="kv-card-title">{{ t('ref_agent_title') }}</h2>
            </div>
            <div class="kv-agent-wrap">
              <div
                v-for="at in agentTypes.filter((a) => a.value)"
                :key="at.value"
                class="kb-agent-tag"
                :class="{
                  'kb-agent-selected': isAgentSelected(at.value),
                  'kb-agent-pending': pendingAgentType === at.value,
                }"
                @click="toggleAgentRef(at.value)"
              >
                <div class="kb-agent-check"><span class="kb-check-yes">✓</span></div>
                <div class="kb-agent-name">{{ at.label }}</div>
                <div v-if="at.desc" class="kb-agent-desc">{{ at.desc }}</div>
              </div>
            </div>
          </div>

          <!-- 场景关联卡片 -->
          <div
            class="cd kb-detail-card"
            style="padding: 24px; margin-bottom: 20px; border-radius: 18px"
          >
            <div class="cd-h kv-scene-head">
              <h2 class="kv-card-title">{{ t('ref_scene_title') }}</h2>
              <button class="kb-tag-add" @click="openScenePopover()">
                + {{ t('ref_add_scene') }}
              </button>
            </div>
            <div v-if="selectedSceneTags.length === 0" class="ts t2" style="padding: 12px 0">
              {{ t('ref_no_scenes_linked') }}
            </div>
            <div v-else class="scn-scroll-wrap">
              <button
                class="scn-scroll-btn scn-scroll-prev"
                :class="{ show: sceneScrollLeft }"
                @click="scrollSceneTrack(-314)"
              >
                ◀
              </button>
              <transition-group
                ref="sceneTrack"
                name="kb-scene"
                tag="div"
                class="scn-scroll-track"
                @scroll="updateSceneScrollBtns"
              >
                <div
                  v-for="tag in selectedSceneTags"
                  :key="tag.sceneId"
                  class="scn-card"
                  :class="{ 'kb-scene-pending': pendingSceneId === tag.sceneId }"
                  style="flex-shrink: 0; width: 300px; position: relative"
                >
                  <button class="kb-scene-x" style="z-index: 5" @click="removeSceneRef(tag)">
                    ×
                  </button>
                  <div class="scn-card-bar" :class="sourceBarClass(detail.source)"></div>
                  <div class="scn-card-body">
                    <div class="scn-card-head">
                      <div class="scn-ic bg-default">📦</div>
                      <div class="scn-card-title">
                        {{ tag.ref.scene_display_name || tag.label }}
                      </div>
                    </div>
                    <div v-if="tag.ref.scene_description" class="scn-card-desc">
                      {{ tag.ref.scene_description }}
                    </div>
                    <div v-if="tag.ref.scene_pipeline" class="scn-chain">
                      <template v-for="(node, i) in tag.ref.scene_pipeline.split(' → ')" :key="i">
                        <span v-if="i > 0" class="scn-arr">→</span>
                        <span class="scn-tag">{{ node }}</span>
                      </template>
                    </div>
                  </div>
                </div>
              </transition-group>
              <button
                class="scn-scroll-btn scn-scroll-next"
                :class="{ show: sceneScrollRight }"
                @click="scrollSceneTrack(314)"
              >
                ▶
              </button>
            </div>
          </div>
        </template>
      </template>

      <!-- ===== 编辑视图 ===== -->
      <template v-if="view === 'detail' && detail && editMode">
        <div class="kv-back">
          <button class="bt bt-o bt-s" @click="cancelEdit">← {{ t('back_to_list') }}</button>
        </div>
        <div class="cd kv-head-card">
          <div class="cd-h" style="margin-bottom: 16px">
            <h2 class="kv-card-title">{{ t('edit_title') }}</h2>
          </div>
          <div class="kv-form-row">
            <label class="kv-label">{{ t('entry_name') }}<span class="kb-required">*</span></label>
            <input
              v-model="editForm.name"
              :placeholder="t('entry_name_ph')"
              class="kb-input"
              style="width: 100%"
              maxlength="50"
            />
          </div>
          <div class="kv-form-row">
            <label class="kv-label">{{ t('entry_description') }}</label>
            <input
              v-model="editForm.description"
              :placeholder="t('entry_description')"
              class="kb-input"
              style="width: 100%"
              maxlength="200"
            />
          </div>
          <div class="kv-form-row">
            <div class="kv-form-row-head">
              <label class="kv-label"
                >{{ t('entry_content') }}<span class="kb-required">*</span></label
              >
              <div class="kv-form-row-tools">
                <button
                  v-if="editForm.content !== editFormSnapshot?.content"
                  class="bt bt-o bt-s kv-tool-btn"
                  :title="t('reset_tip')"
                  @click="resetEdit"
                >
                  {{ t('reset') }}
                </button>
                <label class="bt bt-o bt-s kv-tool-btn kv-file-label">
                  {{ t('import_file') }}
                  <input
                    type="file"
                    accept=".md,.json,.txt"
                    style="display: none"
                    @change="(e) => importFile(e, 'edit')"
                  />
                </label>
              </div>
            </div>
            <textarea v-model="editForm.content" rows="20" class="kb-textarea"></textarea>
          </div>
          <div class="kv-form-actions">
            <button class="bt bt-o bt-s" @click="cancelEdit">{{ t('cancel') }}</button>
            <button
              class="bt bt-p bt-s"
              :class="{ 'kb-btn-loading': submitting }"
              :disabled="submitting"
              @click="saveEdit"
            >
              {{ t('confirm') }}
            </button>
          </div>
        </div>
      </template>

      <!-- ===== 新增视图 ===== -->
      <template v-if="view === 'create' && createForm">
        <div class="kv-back">
          <button class="bt bt-o bt-s" @click="cancelCreate">← {{ t('back_to_list') }}</button>
        </div>
        <div class="cd kv-head-card">
          <div class="cd-h" style="margin-bottom: 16px">
            <h2 class="kv-card-title">{{ t('entry_title') }}</h2>
          </div>
          <div class="kv-form-row">
            <label class="kv-label">{{ t('entry_name') }}<span class="kb-required">*</span></label>
            <input
              v-model="createForm.name"
              :placeholder="t('entry_name_ph')"
              class="kb-input"
              style="width: 100%"
              maxlength="50"
            />
          </div>
          <div class="kv-form-row">
            <label class="kv-label">{{ t('entry_description') }}</label>
            <input
              v-model="createForm.description"
              :placeholder="t('entry_description')"
              class="kb-input"
              style="width: 100%"
              maxlength="200"
            />
          </div>
          <div class="kv-form-row">
            <div class="kv-form-row-head">
              <label class="kv-label"
                >{{ t('entry_content') }}<span class="kb-required">*</span></label
              >
              <div class="kv-form-row-tools">
                <label class="bt bt-o bt-s kv-tool-btn kv-file-label">
                  {{ t('import_file') }}
                  <input
                    type="file"
                    accept=".md,.json,.txt"
                    style="display: none"
                    @change="(e) => importFile(e, 'create')"
                  />
                </label>
              </div>
            </div>
            <textarea v-model="createForm.content" rows="20" class="kb-textarea"></textarea>
          </div>
        </div>

        <!-- Agent 关联卡片（标签式，同详情页面） -->
        <div
          class="cd kb-detail-card"
          style="padding: 24px; margin-bottom: 20px; border-radius: 18px"
        >
          <div class="cd-h" style="margin-bottom: 14px">
            <h2 class="kv-card-title">{{ t('ref_agent_title') }}</h2>
          </div>
          <div class="kv-agent-wrap">
            <div
              v-for="at in agentTypes.filter((a) => a.value)"
              :key="at.value"
              class="kb-agent-tag"
              :class="{ 'kb-agent-selected': createForm.agentSelected.includes(at.value) }"
              @click="toggleCreateAgent(at.value)"
            >
              <div class="kb-agent-check"><span class="kb-check-yes">✓</span></div>
              <div class="kb-agent-name">{{ at.label }}</div>
              <div v-if="at.desc" class="kb-agent-desc">{{ at.desc }}</div>
            </div>
          </div>
        </div>

        <!-- 场景关联卡片（scn-card + 弹出层选择，同详情页面） -->
        <div
          class="cd kb-detail-card"
          style="padding: 24px; margin-bottom: 20px; border-radius: 18px"
        >
          <div class="cd-h kv-scene-head">
            <h2 class="kv-card-title">{{ t('ref_scene_title') }}</h2>
            <button class="kb-tag-add" @click="openCreateScenePopover()">
              + {{ t('ref_add_scene') }}
            </button>
          </div>
          <div v-if="createForm.sceneSelected.length === 0" class="ts t2" style="padding: 12px 0">
            {{ t('ref_no_scenes_linked') }}
          </div>
          <div v-else class="scn-scroll-wrap">
            <button
              class="scn-scroll-btn scn-scroll-prev"
              :class="{ show: sceneScrollLeft }"
              @click="scrollSceneTrack(-314)"
            >
              ◀
            </button>
            <transition-group
              ref="sceneTrack"
              name="kb-scene"
              tag="div"
              class="scn-scroll-track"
              @scroll="updateSceneScrollBtns"
            >
              <div
                v-for="sid in createForm.sceneSelected"
                :key="sid"
                class="scn-card"
                style="flex-shrink: 0; width: 300px; position: relative"
              >
                <button class="kb-scene-x" style="z-index: 5" @click="toggleCreateScene(sid)">
                  ×
                </button>
                <div class="scn-card-bar bg-default"></div>
                <div class="scn-card-body">
                  <div class="scn-card-head">
                    <div class="scn-ic bg-default">📦</div>
                    <div class="scn-card-title">{{ getSceneName(sid) }}</div>
                  </div>
                  <div v-if="getSceneDesc(sid)" class="scn-card-desc">{{ getSceneDesc(sid) }}</div>
                  <div v-if="getScenePipeline(sid)" class="scn-chain">
                    <template v-for="(node, i) in getScenePipeline(sid).split(' → ')" :key="i">
                      <span v-if="i > 0" class="scn-arr">→</span>
                      <span class="scn-tag">{{ node }}</span>
                    </template>
                  </div>
                </div>
              </div>
            </transition-group>
            <button
              class="scn-scroll-btn scn-scroll-next"
              :class="{ show: sceneScrollRight }"
              @click="scrollSceneTrack(314)"
            >
              ▶
            </button>
          </div>
        </div>

        <div class="kv-form-actions">
          <button class="bt bt-o bt-s" @click="cancelCreate">{{ t('cancel') }}</button>
          <button
            class="bt bt-p bt-s"
            :class="{ 'kb-btn-loading': submitting }"
            :disabled="submitting"
            @click="saveCreate"
          >
            {{ t('confirm') }}
          </button>
        </div>
      </template>
    </div>
    <!-- 详情页场景选择弹出层 -->
    <teleport to="body">
      <div v-if="addScenePopover" class="modal-overlay" @click.self="addScenePopover = false">
        <div class="modal kb-scene-modal">
          <div class="kb-scene-modal-title">{{ t('ref_select_scenes') }}</div>
          <div class="kb-scene-modal-search">
            <input
              v-model="sceneSearch"
              :placeholder="t('ref_search_scenes')"
              class="kb-input"
              style="flex: 1; width: auto"
            />
          </div>
          <div class="kb-scene-modal-list">
            <div
              v-if="availableScenes.length === 0"
              class="ts t2"
              style="padding: 20px; text-align: center"
            >
              {{ t('ref_no_available') }}
            </div>
            <div
              v-for="s in availableScenes"
              :key="s.id"
              class="kb-scene-row"
              :class="{ 'kb-scene-row-selected': sceneSelected.includes(s.id) }"
              @click="toggleSceneSelect(s.id)"
            >
              <div class="kb-scene-row-info">
                <div class="kb-scene-row-name">{{ s.name }}</div>
                <div v-if="s.desc" class="kb-scene-row-desc">{{ s.desc }}</div>
              </div>
              <div class="kb-scene-row-check"><span class="kb-check-yes">✓</span></div>
            </div>
          </div>
          <div class="kb-scene-modal-footer">
            <span class="kv-selected-count">{{
              t('ref_selected_count', { count: sceneSelected.length })
            }}</span>
            <div class="kv-footer-btns">
              <button class="bt bt-o bt-s" @click="addScenePopover = false">
                {{ t('cancel') }}
              </button>
              <button
                class="bt bt-p bt-s"
                :class="{ 'kb-btn-loading': submitting }"
                :disabled="submitting || sceneSelected.length === 0"
                @click="confirmAddScenes"
              >
                {{ t('confirm') }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </teleport>
    <!-- 新增页场景选择弹出层 -->
    <teleport to="body">
      <div v-if="createScenePopover" class="modal-overlay" @click.self="createScenePopover = false">
        <div class="modal kb-scene-modal">
          <div class="kb-scene-modal-title">{{ t('ref_select_scenes') }}</div>
          <div class="kb-scene-modal-search">
            <input
              v-model="createSceneSearch"
              :placeholder="t('ref_search_scenes')"
              class="kb-input"
              style="flex: 1; width: auto"
            />
          </div>
          <div class="kb-scene-modal-list">
            <div
              v-if="availableCreateScenes.length === 0"
              class="ts t2"
              style="padding: 20px; text-align: center"
            >
              {{ t('ref_no_available') }}
            </div>
            <div
              v-for="s in availableCreateScenes"
              :key="s.id"
              class="kb-scene-row"
              :class="{ 'kb-scene-row-selected': createSceneTempSelected.includes(s.id) }"
              @click="toggleCreateSceneTemp(s.id)"
            >
              <div class="kb-scene-row-info">
                <div class="kb-scene-row-name">{{ s.name }}</div>
                <div v-if="s.desc" class="kb-scene-row-desc">{{ s.desc }}</div>
              </div>
              <div class="kb-scene-row-check"><span class="kb-check-yes">✓</span></div>
            </div>
          </div>
          <div class="kb-scene-modal-footer">
            <span class="kv-selected-count">{{
              t('ref_selected_count', { count: createSceneTempSelected.length })
            }}</span>
            <div class="kv-footer-btns">
              <button class="bt bt-o bt-s" @click="createScenePopover = false">
                {{ t('cancel') }}
              </button>
              <button class="bt bt-p bt-s" @click="confirmCreateScenes">{{ t('confirm') }}</button>
            </div>
          </div>
        </div>
      </div>
    </teleport>
  </div>
</template>

<script setup>
// Batch 15 (knowledge): 收编 legacy knowledge.js (Issue #67 散装 Vue app) into the
// unified Vite chunk pipeline. Equivalent migration of the Options-API original:
// list/detail/edit/create four views + agent/scene ref management + file import.
import { computed, nextTick, onMounted, reactive, ref } from 'vue'

const G = globalThis

const AGENT_TYPES = [
  { value: '', label: '全部 Agent', desc: '' },
  { value: 'vod', label: 'VOD', desc: '需求分析、拆解 Issue、规划开发路径' },
  { value: 'issue', label: 'Issue', desc: '需求拆解、任务规划、Issue 管理' },
  { value: 'coding', label: 'Coding', desc: '代码实现、修复缺陷、创建 PR' },
  { value: 'qa', label: 'QA', desc: '代码审查、测试验证、质量把关' },
  { value: 'sre', label: 'SRE', desc: '部署发布、监控告警、运维排障' },
  { value: 'chat', label: 'Chat', desc: '对话交互、知识沉淀、流程控制' },
]

const view = ref('list')
const editMode = ref(false)
const editForm = ref(null)
const editFormSnapshot = ref(null)
const createForm = ref(null)
const submitting = ref(false)
const sceneScrollLeft = ref(false)
const sceneScrollRight = ref(false)
const createScenePopover = ref(false)
const createSceneSearch = ref('')
const createSceneTempSelected = ref([])
const list = ref([])
const loading = ref(false)
const firstLoad = ref(true)
const filter = reactive({ refStatus: '', query: '', sortLatest: false })
const stats = reactive({ system: 0, agent: 0, manual: 0, unlinked: 0 })
const contentLimitBytes = ref(5242880) // Issue #372: backend stats provides real limit, 5MB fallback
const contentLimitDisplay = ref('5MB')
const activeTab = ref('system')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const detail = ref(null)
const agentTypes = AGENT_TYPES
const addScenePopover = ref(false)
const sceneSearch = ref('')
const sceneSelected = ref([])
const repoScenes = ref([])
const pendingAgentType = ref(null)
const pendingSceneId = ref(null)
const sceneTrack = ref(null)

function t(key, params) {
  return G.I18N.t('knowledge.' + key, params || {})
}
function currentRepo() {
  try {
    const c = G.app.currentRepo()
    return c ? c.fullName || '' : ''
  } catch (e) {
    return ''
  }
}

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))

const filteredList = computed(() => {
  let result = list.value
  if (filter.refStatus === 'linked') {
    result = result.filter((k) => k.refs && k.refs.length > 0)
  } else if (filter.refStatus === 'unlinked') {
    result = result.filter((k) => !k.refs || k.refs.length === 0)
  }
  return result
})

const selectedSceneTags = computed(() => {
  if (!detail.value || !detail.value.refs) return []
  return detail.value.refs
    .filter((r) => r.scene_id !== '' && r.enabled)
    .map((r) => ({
      ref: r,
      sceneId: r.scene_id,
      label: r.scene_display_name || r.scene_name || String(r.scene_id || '').substring(0, 8),
    }))
})
const availableScenes = computed(() => {
  const selected = new Set(selectedSceneTags.value.map((x) => x.sceneId))
  let available = (repoScenes.value || []).filter((s) => !selected.has(s.id))
  if (sceneSearch.value) {
    const q = sceneSearch.value.toLowerCase()
    available = available.filter(
      (s) => (s.name || '').toLowerCase().includes(q) || (s.id || '').toLowerCase().includes(q)
    )
  }
  return available
})
const availableCreateScenes = computed(() => {
  const selected = new Set(createForm.value?.sceneSelected || [])
  let available = (repoScenes.value || []).filter((s) => !selected.has(s.id))
  if (createSceneSearch.value) {
    const q = createSceneSearch.value.toLowerCase()
    available = available.filter(
      (s) => (s.name || '').toLowerCase().includes(q) || (s.id || '').toLowerCase().includes(q)
    )
  }
  return available
})

async function loadList() {
  loading.value = true
  try {
    const repo = currentRepo()
    if (!repo) {
      loading.value = false
      return
    }
    const params = new URLSearchParams({ repo, page: page.value, page_size: pageSize.value })
    const tabSource = { system: 'system', agent: 'agent', user: 'manual' }
    if (tabSource[activeTab.value]) params.set('source', tabSource[activeTab.value])
    if (filter.query) params.set('q', filter.query)
    if (filter.sortLatest) params.set('sort', 'latest')
    const d = await G.API.get('/knowledge?' + params.toString())
    if (G.window._currentView !== 'knowledge') return
    list.value = d.knowledge || []
    total.value = d.total || 0
    firstLoad.value = false
  } catch (e) {
    if (e.name === 'AbortError' && e.silent) return
    if (G.window._currentView !== 'knowledge') return
    G.app.showToast(G.I18N.t('common.load_failed') || '加载失败')
  } finally {
    if (G.window._currentView === 'knowledge') loading.value = false
  }
}

async function loadStats() {
  try {
    const repo = currentRepo()
    if (!repo) return
    const d = await G.API.get('/knowledge/stats?repo=' + encodeURIComponent(repo))
    if (G.window._currentView !== 'knowledge') return
    Object.assign(stats, d || { system: 0, agent: 0, manual: 0, unlinked: 0 })
    // Issue #358: read backend real limit from stats
    if (d && d.content_limit_bytes) {
      contentLimitBytes.value = d.content_limit_bytes
      contentLimitDisplay.value =
        d.content_limit_display || Math.round(d.content_limit_bytes / 1024) + 'KB'
    }
  } catch (e) {
    if (e.name === 'AbortError' && e.silent) return
    G.app.showToast(G.I18N.t('common.load_failed') || '加载失败')
  }
}

function switchTab(tab) {
  activeTab.value = tab
  page.value = 1
  loadList()
}
function toggleSortLatest() {
  filter.sortLatest = !filter.sortLatest
  page.value = 1
  loadList()
}
function changePage(p) {
  page.value = p
  loadList()
}

function sourceLabel(s) {
  if (s === 'system') return t('source_system')
  if (s === 'agent') return t('source_agent')
  return t('source_manual')
}
function knowledgeTitle(k) {
  if (!k.file_path) return k.description || ''
  const fname = k.file_path
    .split('/')
    .pop()
    .replace(/\.\w+$/, '')
  return fname.replace(/[-_]/g, ' ')
}
function agentRefCount(k) {
  if (!k.refs) return 0
  return k.refs.filter((r) => r.agent_type !== '' && r.scene_id === '' && r.enabled).length
}
function sceneRefCount(k) {
  if (!k.refs) return 0
  return k.refs.filter((r) => r.scene_id !== '' && r.enabled).length
}
function agentNames(k) {
  if (!k.refs) return []
  return k.refs
    .filter((r) => r.agent_type !== '' && r.scene_id === '' && r.enabled)
    .map((r) => {
      const at = AGENT_TYPES.find((a) => a.value === r.agent_type)
      return at ? at.label : r.agent_type
    })
}
function sceneNames(k) {
  if (!k.refs) return []
  return k.refs
    .filter((r) => r.scene_id !== '' && r.enabled)
    .map((r) => r.scene_name || r.scene_id.substring(0, 8))
}
function sourceBarClass(s) {
  if (s === 'system') return 'bg-default'
  if (s === 'agent') return 'bg-skill'
  if (s === 'manual') return 'bg-agent'
  return 'bg-default'
}
function sourceIcon(s) {
  if (s === 'system') return '⚙'
  if (s === 'agent') return '🤖'
  if (s === 'manual') return '✍'
  return '📄'
}
function sourceClass(s) {
  return 'source-' + (s || 'agent')
}

// ── detail ──
async function viewContent(k) {
  if (!k.id) {
    G.app.showToast(t('error_no_id'))
    return
  }
  try {
    const d = await G.API.get('/knowledge/' + k.id)
    if (G.window._currentView !== 'knowledge') return
    detail.value = d
    view.value = 'detail'
    nextTick(() => updateSceneScrollBtns())
  } catch (e) {
    if (e.name === 'AbortError' && e.silent) return
    G.app.showToast(e.message || G.I18N.t('common.load_failed'))
  }
}
function backToList() {
  editMode.value = false
  view.value = 'list'
  detail.value = null
  loadList()
}
async function refreshDetailFull() {
  if (!detail.value) return
  try {
    const d = await G.API.get('/knowledge/' + detail.value.id)
    if (G.window._currentView !== 'knowledge') return
    detail.value = d
    nextTick(() => updateSceneScrollBtns())
  } catch (e) {
    if (e.name === 'AbortError' && e.silent) return
    G.app.showToast(t('refresh_failed') || '刷新失败，请手动刷新页面')
  }
}

// ── agent ref ops ──
function isAgentSelected(agentType) {
  if (!detail.value || !detail.value.refs) return false
  return detail.value.refs.some((r) => r.agent_type === agentType && r.scene_id === '' && r.enabled)
}
async function toggleAgentRef(agentType) {
  if (submitting.value) return
  submitting.value = true
  pendingAgentType.value = agentType
  try {
    const kid = detail.value.id
    if (isAgentSelected(agentType)) {
      const r = detail.value.refs.find(
        (x) => x.agent_type === agentType && x.scene_id === '' && x.enabled
      )
      if (r) {
        await G.API.del('/knowledge/ref/' + r.id, 15000)
        await refreshDetailFull()
        G.app.showToast(t('ref_unbind_agent_ok'))
      }
    } else {
      await G.API.post(
        '/knowledge/' + kid + '/refs/batch',
        { scene_ids: [], agent_types: [agentType] },
        15000
      )
      await refreshDetailFull()
      G.app.showToast(t('ref_bind_agent_ok'))
    }
  } catch (e) {
    if (e.name === 'AbortError' && !e.silent) {
      G.app.showToast(t('ref_timeout_refresh'))
      await refreshDetailFull()
    } else if (e.name === 'AbortError' && e.silent) {
      // nav/tab-switch cancellation — silent
    } else {
      G.app.showToast(e.message || t('ref_failed_refresh'))
      await refreshDetailFull()
    }
  } finally {
    submitting.value = false
    pendingAgentType.value = null
  }
}

// ── scene ref ops ──
async function removeSceneRef(tag) {
  if (submitting.value) return
  submitting.value = true
  pendingSceneId.value = tag.sceneId
  try {
    await G.API.del('/knowledge/ref/' + tag.ref.id, 15000)
    await refreshDetailFull()
    G.app.showToast(t('ref_unbind_scene_ok'))
  } catch (e) {
    if (e.name === 'AbortError' && !e.silent) {
      G.app.showToast(t('ref_timeout_refresh'))
      await refreshDetailFull()
    } else if (e.name === 'AbortError' && e.silent) {
      // silent
    } else {
      G.app.showToast(e.message || t('ref_failed_refresh'))
      await refreshDetailFull()
    }
  } finally {
    submitting.value = false
    pendingSceneId.value = null
  }
}
function openScenePopover() {
  addScenePopover.value = true
  sceneSearch.value = ''
  sceneSelected.value = []
  loadRepoScenes()
}
async function loadRepoScenes() {
  try {
    const repo = currentRepo()
    const d = await G.API.get('/scenes?repo=' + encodeURIComponent(repo))
    if (G.window._currentView !== 'knowledge') return
    repoScenes.value = (d.scenes || []).map((s) => ({
      id: String(s.id || s.scene_id || ''),
      name: s.display_name || s.name || '',
      desc: s.description || '',
      category: '',
      pipeline: (s.nodes || [])
        .filter((n) => n.enabled !== false)
        .map((n) => n.tag || n.id || '')
        .join(' → '),
    }))
  } catch (e) {
    repoScenes.value = []
  }
}
function toggleSceneSelect(sceneId) {
  const idx = sceneSelected.value.indexOf(sceneId)
  if (idx >= 0) sceneSelected.value.splice(idx, 1)
  else sceneSelected.value.push(sceneId)
}
async function confirmAddScenes() {
  if (submitting.value || sceneSelected.value.length === 0) return
  submitting.value = true
  const kid = detail.value.id
  try {
    await G.API.post(
      '/knowledge/' + kid + '/refs/batch',
      { scene_ids: sceneSelected.value, agent_types: [] },
      15000
    )
    await refreshDetailFull()
    G.app.showToast(t('ref_added_count', { count: sceneSelected.value.length }))
    addScenePopover.value = false
    sceneSelected.value = []
  } catch (e) {
    if (e.name === 'AbortError' && !e.silent) {
      G.app.showToast(t('ref_timeout_refresh'))
      await refreshDetailFull()
    } else if (e.name === 'AbortError' && e.silent) {
      // silent
    } else {
      G.app.showToast(e.message || t('ref_failed_refresh'))
      await refreshDetailFull()
    }
  } finally {
    submitting.value = false
  }
}

// ── edit ──
function editKnowledge() {
  const snapshot = {
    name: detail.value.name || '',
    content: detail.value.content || '',
    description: detail.value.description || '',
  }
  editForm.value = { ...snapshot }
  editFormSnapshot.value = snapshot
  editMode.value = true
}
function resetEdit() {
  if (editFormSnapshot.value) {
    editForm.value = { ...editFormSnapshot.value }
  }
}
function cancelEdit() {
  editMode.value = false
}
function importFile(event, target) {
  const file = event.target.files[0]
  if (!file) return
  if (file.size > contentLimitBytes.value) {
    G.app.showToast(t('file_too_large').replace('{limit}', contentLimitDisplay.value))
    event.target.value = ''
    return
  }
  const reader = new FileReader()
  reader.onload = (e) => {
    const text = e.target.result || ''
    if (target === 'create') {
      createForm.value.content = text
      if (!createForm.value.name) {
        createForm.value.name = file.name.replace(/\.\w+$/, '').replace(/[-_]/g, ' ')
      }
    } else {
      editForm.value.content = text
    }
    G.app.showToast(t('file_imported'))
  }
  reader.readAsText(file)
  event.target.value = ''
}
function scrollSceneTrack(delta) {
  const el = sceneTrack.value?.$el || sceneTrack.value
  if (el) el.scrollBy({ left: delta, behavior: 'smooth' })
}
function updateSceneScrollBtns() {
  const el = sceneTrack.value?.$el || sceneTrack.value
  if (!el) return
  sceneScrollLeft.value = el.scrollLeft > 5
  sceneScrollRight.value = el.scrollLeft + el.clientWidth < el.scrollWidth - 5
}
async function saveEdit() {
  if (!detail.value) return
  if (!editForm.value.name.trim()) {
    G.app.showToast(t('name_required'))
    return
  }
  if (editForm.value.name.length > 50) {
    G.app.showToast(t('name_too_long'))
    return
  }
  if (editForm.value.description.length > 200) {
    G.app.showToast(t('desc_too_long'))
    return
  }
  if (!editForm.value.content.trim()) {
    G.app.showToast(t('content_required'))
    return
  }
  if (new Blob([editForm.value.content]).size > contentLimitBytes.value) {
    G.app.showToast(t('content_too_large').replace('{limit}', contentLimitDisplay.value))
    return
  }
  if (submitting.value) return
  submitting.value = true
  try {
    await G.API.put(
      '/knowledge/' + detail.value.id,
      {
        content: editForm.value.content || '',
        description: editForm.value.description || '',
        name: editForm.value.name.trim(),
      },
      15000
    )
    G.app.showToast(t('knowledge_updated'))
    // Issue #199: refresh detail after save, exit edit mode
    await refreshDetailFull()
    editMode.value = false
  } catch (e) {
    if (e.name === 'AbortError' && !e.silent) {
      G.app.showToast(t('save_timeout'))
      await refreshDetailFull()
      editMode.value = false
    } else if (e.name === 'AbortError' && e.silent) {
      // silent
    } else {
      G.app.showToast(e.message || G.I18N.t('common.load_failed'))
    }
  } finally {
    submitting.value = false
  }
}

// ── delete ──
function deleteKnowledge() {
  const kid = detail.value.id
  G.window._showAppleConfirm(t('delete_confirm'), async function () {
    if (submitting.value) return
    submitting.value = true
    try {
      await G.API.del('/knowledge/' + kid, 15000)
      G.app.showToast(t('knowledge_deleted'))
      backToList()
    } catch (e) {
      if (e.name === 'AbortError' && !e.silent) {
        G.app.showToast(t('ref_timeout_refresh'))
        backToList()
      } else if (e.name === 'AbortError' && e.silent) {
        // silent
      } else {
        G.app.showToast(e.message || G.I18N.t('common.load_failed'))
        await refreshDetailFull()
      }
    } finally {
      submitting.value = false
    }
  })
}

async function quickDelete(k) {
  G.window._showAppleConfirm(t('delete_confirm'), async function () {
    if (submitting.value) return
    submitting.value = true
    try {
      await G.API.del('/knowledge/' + k.id, 15000)
      G.app.showToast(t('knowledge_deleted'))
      await loadList()
    } catch (e) {
      if (e.name === 'AbortError' && !e.silent) {
        G.app.showToast(t('ref_timeout_refresh'))
        await loadList()
      } else if (e.name === 'AbortError' && e.silent) {
        // silent
      } else {
        G.app.showToast(e.message || G.I18N.t('common.load_failed'))
        await loadList()
      }
    } finally {
      submitting.value = false
    }
  })
}
function quickEdit(k) {
  viewContent(k).then(() => {
    if (detail.value && detail.value.source === 'manual') {
      editMode.value = true
    }
  })
}

// ── create ──
function openEntryModal() {
  createForm.value = {
    name: '',
    title: '',
    description: '',
    content: '',
    agentSelected: [],
    sceneSelected: [],
  }
  view.value = 'create'
  loadRepoScenes()
  nextTick(() => updateSceneScrollBtns())
}
function getSceneName(sid) {
  const s = (repoScenes.value || []).find((x) => x.id === sid)
  return s ? s.name : String(sid || '').substring(0, 12)
}
function getSceneDesc(sid) {
  const s = (repoScenes.value || []).find((x) => x.id === sid)
  return s ? s.desc || '' : ''
}
function getScenePipeline(sid) {
  const s = (repoScenes.value || []).find((x) => x.id === sid)
  return s ? s.pipeline || '' : ''
}
function openCreateScenePopover() {
  createScenePopover.value = true
  createSceneSearch.value = ''
  createSceneTempSelected.value = [...(createForm.value?.sceneSelected || [])]
  loadRepoScenes()
}
function confirmCreateScenes() {
  if (createForm.value) {
    createForm.value.sceneSelected = [...createSceneTempSelected.value]
  }
  createScenePopover.value = false
  nextTick(() => updateSceneScrollBtns())
}
function cancelCreate() {
  createForm.value = null
  view.value = 'list'
}
function toggleCreateAgent(agentType) {
  const idx = createForm.value.agentSelected.indexOf(agentType)
  if (idx >= 0) createForm.value.agentSelected.splice(idx, 1)
  else createForm.value.agentSelected.push(agentType)
}
function toggleCreateScene(sceneId) {
  // X button on scene card list — direct remove
  const idx = createForm.value.sceneSelected.indexOf(sceneId)
  if (idx >= 0) createForm.value.sceneSelected.splice(idx, 1)
  nextTick(() => updateSceneScrollBtns())
}
function toggleCreateSceneTemp(sceneId) {
  // popover temp toggle
  const idx = createSceneTempSelected.value.indexOf(sceneId)
  if (idx >= 0) createSceneTempSelected.value.splice(idx, 1)
  else createSceneTempSelected.value.push(sceneId)
}
async function saveCreate() {
  if (!createForm.value.name.trim()) {
    G.app.showToast(t('name_required'))
    return
  }
  if (createForm.value.name.length > 50) {
    G.app.showToast(t('name_too_long'))
    return
  }
  if (createForm.value.description.length > 200) {
    G.app.showToast(t('desc_too_long'))
    return
  }
  if (!createForm.value.content.trim()) {
    G.app.showToast(t('content_required'))
    return
  }
  if (new Blob([createForm.value.content]).size > contentLimitBytes.value) {
    G.app.showToast(t('content_too_large').replace('{limit}', contentLimitDisplay.value))
    return
  }
  if (submitting.value) return
  submitting.value = true
  try {
    await G.API.post(
      '/knowledge/entry',
      {
        repo: currentRepo(),
        content: createForm.value.content,
        agent_type: '',
        description: createForm.value.description || '',
        name: createForm.value.name.trim(),
        scene_ids: createForm.value.sceneSelected,
        agent_types: createForm.value.agentSelected,
      },
      15000
    )
    G.app.showToast(t('knowledge_created'))
    createForm.value = null
    view.value = 'list'
    await Promise.all([loadStats(), loadList()])
  } catch (e) {
    if (e.name === 'AbortError' && !e.silent) {
      // timeout — outcome uncertain, back to list for user confirmation
      G.app.showToast(t('create_timeout'))
      createForm.value = null
      view.value = 'list'
      await loadList()
    } else if (e.name === 'AbortError' && e.silent) {
      // silent
    } else {
      // non-timeout — transaction rolled back, stay on create page for safe retry
      G.app.showToast(e.message || G.I18N.t('common.load_failed'))
    }
  } finally {
    submitting.value = false
  }
}

function renderMarkdown(content) {
  if (typeof G.marked !== 'undefined' && G.marked.parse) {
    // safe mode: escape HTML before markdown parse (XSS guard, same as legacy)
    const escaped = (content || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
    return G.marked.parse(escaped)
  }
  return '<pre>' + (content || '').replace(/</g, '&lt;') + '</pre>'
}

onMounted(async () => {
  await loadStats()
  await loadList()
})

defineExpose({ viewContent, quickEdit })
</script>

<style scoped>
.kv-back {
  margin-bottom: 16px;
}
.kv-head-card {
  padding: 28px 32px;
  margin-bottom: 20px;
  border-radius: 20px;
}
.kv-head-main {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 20px;
}
.kv-head-tags {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.kv-head-tags :deep(.source-tag) {
  font-size: 11px;
  padding: 3px 12px;
}
.kv-head-title {
  font-size: 17px;
  font-weight: 600;
  color: var(--t);
  margin-bottom: 8px;
}
.kv-head-desc {
  font-size: 13px;
  color: var(--t2);
  line-height: 1.5;
  margin-bottom: 8px;
  font-weight: 500;
  white-space: pre-line;
  word-break: break-word;
}
.kv-head-meta {
  font-size: 11px;
  color: var(--t3);
  line-height: 1.6;
}
.kv-head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-top: 16px;
  border-top: 1px solid var(--b);
  flex-wrap: wrap;
  justify-content: flex-end;
}
.kv-content-card {
  padding: 24px;
  margin-bottom: 20px;
  border-radius: 18px;
}
.kv-card-title {
  font-size: 16px;
  font-weight: 600;
}
.kv-agent-wrap {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.kv-scene-head {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 10px;
  margin-bottom: 14px;
}
.kv-ref-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 14px;
  flex-wrap: wrap;
  font-size: 10px;
  min-height: 17px;
}
.kv-del-btn {
  color: var(--r);
  border-color: var(--r);
}
.kv-del-btn:not(:disabled):hover {
  background: var(--r);
  border-color: var(--r);
  color: #fff;
}
.kv-form-row {
  margin-bottom: 12px;
}
.kv-form-row-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
}
.kv-form-row-tools {
  display: flex;
  gap: 6px;
}
.kv-tool-btn {
  cursor: pointer;
  font-size: 11px;
}
.kv-file-label {
  position: relative;
  overflow: hidden;
}
.kv-label {
  font-size: 11px;
  color: var(--t3);
  display: block;
  margin-bottom: 4px;
}
.kv-form-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--b);
}
.kv-selected-count {
  font-size: 11px;
  color: var(--t3);
}
.kv-footer-btns {
  display: flex;
  gap: 8px;
}
</style>
