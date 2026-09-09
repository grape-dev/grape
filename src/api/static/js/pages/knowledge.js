// knowledge.js — 项目知识库页面（Issue #67 Vue 3 重写）
// 统一列表 + 统计卡片 + 筛选器 + 详情页面 + 关联管理

var _knowledgeVueApp = null;

const AGENT_TYPES = [
  { value: '', label: '全部 Agent' },
  { value: 'vod', label: 'VOD' },
  { value: 'coding', label: 'Coding' },
  { value: 'qa', label: 'QA' },
  { value: 'sre', label: 'SRE' },
  { value: 'chat', label: 'Chat' },
];

function renderKnowledge() {
  const el = document.getElementById('pageContent');
  if (!el) return;

  if (_knowledgeVueApp) {
    _knowledgeVueApp.unmount();
    _knowledgeVueApp = null;
  }

  _knowledgeVueApp = Vue.createApp({
    data() {
      return {
        view: 'list',
        list: [],
        loading: true,
        filter: { source: '', refStatus: '', query: '' },
        stats: { system: 0, agent: 0, manual: 0, unlinked: 0 },
        showArchived: false,
        page: 1,
        pageSize: 50,
        total: 0,
        editModal: null,
        entryModal: null,
        detail: null,
        scenes: [],
        agentTypes: AGENT_TYPES,
      };
    },
    computed: {
      filteredList() {
        let result = this.list;
        if (!this.showArchived) {
          result = result.filter(k => !k.archived);
        }
        if (this.filter.source === 'system') {
          result = result.filter(k => k.source === 'system');
        } else if (this.filter.source === 'agent') {
          result = result.filter(k => k.source === 'agent');
        } else if (this.filter.source === 'manual') {
          result = result.filter(k => k.source === 'manual');
        }
        if (this.filter.refStatus === 'linked') {
          result = result.filter(k => k.refs && k.refs.length > 0);
        } else if (this.filter.refStatus === 'unlinked') {
          result = result.filter(k => !k.refs || k.refs.length === 0);
        }
        if (this.filter.query) {
          const q = this.filter.query.toLowerCase();
          result = result.filter(k =>
            (k.content || '').toLowerCase().includes(q) ||
            (k.file_path || '').toLowerCase().includes(q) ||
            (k.created_by || '').toLowerCase().includes(q)
          );
        }
        return result;
      },
      detailAgentRefs() {
        if (!this.detail || !this.detail.refs) return [];
        const agentRefs = this.detail.refs.filter(r => r.scene_id === '' && r.agent_type !== '');
        return agentRefs;
      },
      detailSceneRefs() {
        if (!this.detail || !this.detail.refs) return [];
        return this.detail.refs.filter(r => r.scene_id !== '');
      },
      detailGlobalRef() {
        if (!this.detail || !this.detail.refs) return null;
        return this.detail.refs.find(r => r.scene_id === '' && r.agent_type === '');
      },
    },
    async mounted() {
      await this.loadList();
    },
    methods: {
      t(key, params) {
        return I18N.t('knowledge.' + key, params || {});
      },
      currentRepo() {
        try {
          const c = JSON.parse(localStorage.getItem('currentRepo') || 'null');
          return c ? (c.fullName || '') : '';
        } catch { return ''; }
      },
      async loadList() {
        this.loading = true;
        try {
          const repo = this.currentRepo();
          if (!repo) { this.loading = false; return; }
          const params = new URLSearchParams({ repo: repo, page: this.page, page_size: this.pageSize });
          if (this.filter.source) params.set('source', this.filter.source);
          const d = await API.get('/knowledge?' + params.toString());
          if (window._currentView !== 'knowledge') return;
          this.list = d.knowledge || [];
          this.stats = d.stats || { system: 0, agent: 0, manual: 0, unlinked: 0 };
          this.total = d.total || 0;
        } catch (e) {
          if (e.name === 'AbortError' && e.silent) return;
          if (window._currentView !== 'knowledge') return;
        } finally {
          if (window._currentView === 'knowledge') this.loading = false;
        }
      },
      filterByStats(type) {
        if (type === 'system') this.filter.source = 'system';
        else if (type === 'agent') this.filter.source = 'agent';
        else if (type === 'user') this.filter.source = 'manual';
        else if (type === 'unlinked') this.filter.refStatus = 'unlinked';
        this.page = 1;
        this.loadList();
      },
      sourceLabel(s) {
        if (s === 'system') return this.t('source_system');
        if (s === 'agent') return this.t('source_agent');
        return this.t('source_manual');
      },
      sourceClass(s) {
        return 'source-' + (s || 'agent');
      },
      statSystem() { return this.stats.system; },
      statAgent() { return this.stats.agent; },
      statUser() { return this.stats.manual; },
      statUnlinked() { return this.stats.unlinked; },

      // 详情页面
      async viewContent(k) {
        if (!k.id) {
          app.showToast(this.t('error_no_id'));
          return;
        }
        try {
          const d = await API.get('/knowledge/' + k.id);
          if (window._currentView !== 'knowledge') return;
          this.detail = d;
          this.view = 'detail';
          await this.loadScenes();
          await this.loadRefs(k.id);
        } catch (e) {
          if (e.name === 'AbortError' && e.silent) return;
          app.showToast(e.message || I18N.t('common.load_failed'));
        }
      },
      backToList() {
        this.view = 'list';
        this.detail = null;
        this.loadList();
      },
      async loadScenes() {
        try {
          const repo = this.currentRepo();
          const d = await API.get('/scenes?repo=' + encodeURIComponent(repo));
          if (window._currentView !== 'knowledge') return;
          this.scenes = (d.scenes || []).map(s => ({
            id: s.id || s.scene_id || '',
            name: s.display_name || s.name || s.id || '',
          }));
        } catch (e) {
          this.scenes = [];
        }
      },
      async loadRefs(kid) {
        try {
          const d = await API.get('/knowledge/' + kid + '/refs');
          if (this.detail && this.detail.id === kid) {
            this.detail.refs = d.refs || [];
          }
        } catch (e) {
          // ignore
        }
      },

      // Agent 多选关联
      isAgentChecked(agentType) {
        if (!this.detail || !this.detail.refs) return false;
        return this.detail.refs.some(r => r.scene_id === '' && r.agent_type === agentType && r.enabled);
      },
      async toggleAgentRef(agentType) {
        const kid = this.detail.id;
        const existing = (this.detail.refs || []).find(r => r.scene_id === '' && r.agent_type === agentType);
        if (existing) {
          try {
            await API.put('/knowledge/ref/' + existing.id, { enabled: !existing.enabled });
            existing.enabled = !existing.enabled;
          } catch (e) {
            app.showToast(e.message);
          }
        } else {
          try {
            await API.post('/knowledge/' + kid + '/refs', { scene_id: '', agent_type: agentType });
            await this.loadRefs(kid);
            app.showToast(this.t('ref_add'));
          } catch (e) {
            if (e.detail && e.detail.message && (e.detail.message.includes('已绑定') || e.detail.message.includes('Duplicate'))) {
              app.showToast(this.t('ref_already_bound'));
            } else {
              app.showToast(e.message || I18N.t('common.load_failed'));
            }
          }
        }
      },

      // 全局关联（所有场景 + 所有 Agent）
      isGlobalChecked() {
        return this.detailGlobalRef && this.detailGlobalRef.enabled;
      },
      async toggleGlobalRef() {
        const kid = this.detail.id;
        const existing = this.detailGlobalRef;
        if (existing) {
          try {
            await API.put('/knowledge/ref/' + existing.id, { enabled: !existing.enabled });
            existing.enabled = !existing.enabled;
          } catch (e) {
            app.showToast(e.message);
          }
        } else {
          try {
            await API.post('/knowledge/' + kid + '/refs', { scene_id: '', agent_type: '' });
            await this.loadRefs(kid);
            app.showToast(this.t('ref_add'));
          } catch (e) {
            app.showToast(e.message || I18N.t('common.load_failed'));
          }
        }
      },

      // 场景多选关联
      isSceneChecked(sceneId) {
        if (!this.detail || !this.detail.refs) return false;
        return this.detail.refs.some(r => r.scene_id === sceneId && r.enabled);
      },
      async toggleSceneRef(sceneId) {
        const kid = this.detail.id;
        const existing = (this.detail.refs || []).find(r => r.scene_id === sceneId && r.agent_type === '');
        if (existing) {
          try {
            await API.put('/knowledge/ref/' + existing.id, { enabled: !existing.enabled });
            existing.enabled = !existing.enabled;
          } catch (e) {
            app.showToast(e.message);
          }
        } else {
          try {
            await API.post('/knowledge/' + kid + '/refs', { scene_id: sceneId, agent_type: '' });
            await this.loadRefs(kid);
            app.showToast(this.t('ref_add'));
          } catch (e) {
            if (e.detail && e.detail.message && (e.detail.message.includes('已绑定') || e.detail.message.includes('Duplicate'))) {
              app.showToast(this.t('ref_already_bound'));
            } else {
              app.showToast(e.message || I18N.t('common.load_failed'));
            }
          }
        }
      },

      // 编辑
      editKnowledge() {
        this.editModal = { id: this.detail.id, content: this.detail.content || '', loading: false };
      },
      async saveEdit() {
        if (!this.editModal) return;
        this.editModal.loading = true;
        try {
          await API.put('/knowledge/' + this.editModal.id, { content: this.editModal.content });
          app.showToast(this.t('knowledge_updated'));
          this.editModal = null;
          await this.viewContent({ id: this.detail.id });
        } catch (e) {
          app.showToast(e.message || I18N.t('common.load_failed'));
        } finally {
          if (this.editModal) this.editModal.loading = false;
        }
      },

      // 删除
      deleteKnowledge() {
        const self = this;
        const kid = this.detail.id;
        _showAppleConfirm(this.t('delete_confirm'), async function() {
          try {
            await API.del('/knowledge/' + kid);
            app.showToast(self.t('knowledge_deleted'));
            self.backToList();
          } catch (e) {
            app.showToast(e.message || I18N.t('common.load_failed'));
          }
        });
      },

      // 归档
      async toggleArchive() {
        try {
          await API.put('/knowledge/' + this.detail.id + '/archive', { archived: !this.detail.archived });
          app.showToast(this.detail.archived ? this.t('unarchived') : this.t('archived'));
          this.detail.archived = !this.detail.archived;
        } catch (e) {
          app.showToast(e.message || I18N.t('common.load_failed'));
        }
      },

      // 卡片上的快速操作
      async quickDelete(k) {
        const self = this;
        _showAppleConfirm(this.t('delete_confirm'), async function() {
          try {
            await API.del('/knowledge/' + k.id);
            app.showToast(self.t('knowledge_deleted'));
            await self.loadList();
          } catch (e) {
            app.showToast(e.message || I18N.t('common.load_failed'));
          }
        });
      },
      async quickArchive(k) {
        try {
          await API.put('/knowledge/' + k.id + '/archive', { archived: !k.archived });
          app.showToast(k.archived ? this.t('unarchived') : this.t('archived'));
          await this.loadList();
        } catch (e) {
          app.showToast(e.message || I18N.t('common.load_failed'));
        }
      },
      quickEdit(k) {
        this.viewContent(k).then(() => {
          if (this.detail && this.detail.source === 'manual') {
            this.editModal = { id: this.detail.id, content: this.detail.content || '', loading: false };
          }
        });
      },

      // 新增知识
      openEntryModal() {
        this.entryModal = { content: '', agentType: '', loading: false };
      },
      async createEntry() {
        if (!this.entryModal.content.trim()) { app.showToast(this.t('entry_content')); return; }
        this.entryModal.loading = true;
        try {
          await API.post('/knowledge/entry', {
            repo: this.currentRepo(),
            content: this.entryModal.content,
            agent_type: this.entryModal.agentType || ''
          });
          app.showToast(this.t('knowledge_created'));
          this.entryModal = null;
          await this.loadList();
        } catch (e) {
          app.showToast(e.message || I18N.t('common.load_failed'));
        } finally {
          if (this.entryModal) this.entryModal.loading = false;
        }
      },

      // 文件过大提示
      isFileTooLarge(k) {
        return k.scope === 'project' && k.content && k.content.length > 10240;
      },
      fileSizeKB(k) {
        return ((k.content || '').length / 1024).toFixed(1);
      },
      renderMarkdown(content) {
        if (typeof marked !== 'undefined' && marked.parse) {
          return marked.parse(content);
        }
        return '<pre>' + (content || '').replace(/</g, '&lt;') + '</pre>';
      },
    },
    template: `
      <div class="knowledge-page">
        <!-- ===== 列表视图 ===== -->
        <template v-if="view === 'list'">
          <div v-if="loading" class="loading">{{ t('loading') }}</div>
          <template v-else>
            <div class="stats-bar">
              <div class="stat-card" @click="filterByStats('system')">
                <span class="stat-num">{{ statSystem() }}</span>
                <span class="stat-label">{{ t('stats_system') }}</span>
              </div>
              <div class="stat-card" @click="filterByStats('agent')">
                <span class="stat-num">{{ statAgent() }}</span>
                <span class="stat-label">{{ t('stats_agent') }}</span>
              </div>
              <div class="stat-card" @click="filterByStats('user')">
                <span class="stat-num">{{ statUser() }}</span>
                <span class="stat-label">{{ t('stats_user') }}</span>
              </div>
              <div class="stat-card" @click="filterByStats('unlinked')">
                <span class="stat-num">{{ statUnlinked() }}</span>
                <span class="stat-label">{{ t('stats_unlinked') }}</span>
              </div>
            </div>

            <div class="filter-bar">
              <select v-model="filter.source" class="kb-select">
                <option value="">{{ t('filter_source_all') }}</option>
                <option value="system">{{ t('source_system') }}</option>
                <option value="agent">{{ t('source_agent') }}</option>
                <option value="manual">{{ t('source_manual') }}</option>
              </select>
              <select v-model="filter.refStatus" class="kb-select">
                <option value="">{{ t('filter_ref_all') }}</option>
                <option value="linked">{{ t('filter_ref_linked') }}</option>
                <option value="unlinked">{{ t('filter_ref_unlinked') }}</option>
              </select>
              <input v-model="filter.query" :placeholder="t('search_placeholder')" class="kb-input">
              <label class="kb-checkbox-label">
                <input type="checkbox" v-model="showArchived"> {{ t('show_archived') }}
              </label>
              <button class="bt bt-o bt-s" style="color:var(--a);border-color:var(--a);margin-left:auto" @click="openEntryModal">{{ t('entry_create') }}</button>
            </div>

            <div v-if="filteredList.length === 0" class="ts t2" style="padding:12px">{{ t('empty') }}</div>
            <div v-else class="kb-grid">
              <div v-for="k in filteredList" :key="k.id || k.file_path" class="cd skill-card" style="padding:14px" :class="{ 'archived-card': k.archived }">
                <div class="card-header">
                  <span class="source-tag" :class="sourceClass(k.source)">{{ sourceLabel(k.source) }}</span>
                  <span v-if="k.ref_summary && k.ref_summary !== '未关联'" class="ref-tag">{{ k.ref_summary }}</span>
                  <span v-else class="ref-tag unlinked">{{ t('ref_unlinked') }}</span>
                  <span v-if="k.issue_tag" class="issue-tag">{{ k.issue_tag }}</span>
                  <span v-if="k.archived" class="archived-tag">{{ t('archived') }}</span>
                </div>
                <div v-if="isFileTooLarge(k)" class="size-warning">⚠ {{ t('file_too_large', { size: fileSizeKB(k) }) }}</div>
                <div class="card-content">{{ k.content || '' }}</div>
                <div class="card-footer">
                  <span>{{ k.created_by || '' }} · {{ (k.created_at || '').substring(0, 16) }}</span>
                </div>
                <div class="card-actions">
                  <button class="bt bt-o bt-s" @click="viewContent(k)">{{ t('view') }}</button>
                  <button v-if="k.source === 'manual' && !k.archived" class="bt bt-o bt-s" @click="quickEdit(k)">{{ t('edit_save') }}</button>
                  <button v-if="k.source === 'manual' && !k.archived" class="bt bt-o bt-s" style="color:var(--r);border-color:var(--r)" @click="quickDelete(k)">{{ t('delete') }}</button>
                  <button v-if="k.source !== 'manual' && !k.archived" class="bt bt-o bt-s" @click="quickArchive(k)">{{ t('archive') }}</button>
                  <button v-if="k.archived" class="bt bt-o bt-s" @click="quickArchive(k)">{{ t('unarchive') }}</button>
                </div>
              </div>
            </div>
          </template>
        </template>

        <!-- ===== 详情视图 ===== -->
        <template v-if="view === 'detail' && detail">
          <div style="margin-bottom:16px">
            <button class="bt bt-o bt-s" @click="backToList">← {{ t('back_to_list') }}</button>
          </div>

          <div class="cd" style="padding:20px;margin-bottom:16px">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap">
              <span class="source-tag" :class="sourceClass(detail.source)">{{ sourceLabel(detail.source) }}</span>
              <span class="ref-tag">{{ detail.scope }}</span>
              <span v-if="detail.issue_tag" class="issue-tag">{{ detail.issue_tag }}</span>
              <span v-if="detail.archived" class="archived-tag">{{ t('archived') }}</span>
              <span style="font-size:10px;color:var(--t3);margin-left:auto">{{ detail.created_by || '' }} · {{ (detail.created_at || '').substring(0, 16) }}</span>
            </div>
            <div v-html="renderMarkdown(detail.content || '')" class="kb-detail-content"></div>
            <div style="display:flex;gap:8px;margin-top:16px;flex-wrap:wrap">
              <button v-if="detail.source === 'manual' && !detail.archived" class="bt bt-o bt-s" @click="editKnowledge">{{ t('edit_save') }}</button>
              <button v-if="detail.source === 'manual' && !detail.archived" class="bt bt-o bt-s" style="color:var(--r);border-color:var(--r)" @click="deleteKnowledge">{{ t('delete') }}</button>
              <button v-if="detail.source !== 'manual' && !detail.archived" class="bt bt-o bt-s" @click="toggleArchive">{{ t('archive') }}</button>
              <button v-if="detail.archived" class="bt bt-o bt-s" @click="toggleArchive">{{ t('unarchive') }}</button>
            </div>
          </div>

          <!-- 关联管理 -->
          <div class="cd" style="padding:20px">
            <h3 style="font-size:14px;font-weight:600;margin-bottom:16px">{{ t('ref_modal_title') }}</h3>

            <!-- 全局关联 -->
            <div class="kb-ref-section">
              <label class="kb-ref-item">
                <input type="checkbox" :checked="isGlobalChecked()" @change="toggleGlobalRef">
                <span>{{ t('ref_global') }}</span>
              </label>
            </div>

            <!-- Agent 多选 -->
            <div class="kb-ref-section">
              <div class="kb-ref-section-title">{{ t('ref_agent_title') }}</div>
              <div class="kb-ref-grid">
                <label v-for="at in agentTypes.filter(a => a.value)" :key="at.value" class="kb-ref-item">
                  <input type="checkbox" :checked="isAgentChecked(at.value)" @change="toggleAgentRef(at.value)">
                  <span>{{ at.label }}</span>
                </label>
              </div>
            </div>

            <!-- 场景多选 -->
            <div class="kb-ref-section">
              <div class="kb-ref-section-title">{{ t('ref_scene_title') }}</div>
              <div v-if="scenes.length === 0" class="ts t2" style="padding:8px">{{ t('ref_no_scenes') }}</div>
              <div v-else class="kb-ref-grid">
                <label v-for="s in scenes" :key="s.id" class="kb-ref-item">
                  <input type="checkbox" :checked="isSceneChecked(s.id)" @change="toggleSceneRef(s.id)">
                  <span>{{ s.name }}</span>
                </label>
              </div>
            </div>
          </div>
        </template>

        <!-- 编辑 modal -->
        <div v-if="editModal" class="modal-overlay" @click.self="editModal = null">
          <div class="modal cd">
            <h3 style="font-size:14px;font-weight:600;margin-bottom:14px">{{ t('edit_title') }}</h3>
            <textarea v-model="editModal.content" rows="12" class="kb-textarea"></textarea>
            <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
              <button class="bt bt-o bt-s" @click="editModal = null">{{ t('cancel') }}</button>
              <button class="bt bt-p bt-s" :disabled="editModal.loading" @click="saveEdit">{{ t('edit_save') }}</button>
            </div>
          </div>
        </div>

        <!-- 新增 modal -->
        <div v-if="entryModal" class="modal-overlay" @click.self="entryModal = null">
          <div class="modal cd">
            <h3 style="font-size:14px;font-weight:600;margin-bottom:14px">{{ t('entry_title') }}</h3>
            <textarea v-model="entryModal.content" rows="10" :placeholder="t('entry_content')" class="kb-textarea"></textarea>
            <select v-model="entryModal.agentType" class="kb-select" style="width:100%;margin-top:8px">
              <option value="">{{ t('ref_agent_all') }}</option>
              <option v-for="at in agentTypes.filter(a => a.value)" :key="at.value" :value="at.value">{{ at.label }}</option>
            </select>
            <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
              <button class="bt bt-o bt-s" @click="entryModal = null">{{ t('cancel') }}</button>
              <button class="bt bt-p bt-s" :disabled="entryModal.loading" @click="createEntry">{{ t('entry_create') }}</button>
            </div>
          </div>
        </div>
      </div>
    `,
  });

  _knowledgeVueApp.mount(el);
}
