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
    try { _knowledgeVueApp.unmount(); } catch(e) {}
    _knowledgeVueApp = null;
  }

  // 清空容器，确保 Vue mount 时没有残留 DOM
  el.innerHTML = '';

  _knowledgeVueApp = Vue.createApp({
    data() {
      return {
        view: 'list',
        list: [],
        loading: true,
        filter: { refStatus: '', query: '' },
        stats: { system: 0, agent: 0, manual: 0, unlinked: 0 },
        showArchived: false,
        activeTab: 'all',
        page: 1,
        pageSize: 50,
        total: 0,
        editModal: null,
        entryModal: null,
        detail: null,
        scenes: [],
        agentTypes: AGENT_TYPES,
        addAgentPopover: false,
        addScenePopover: false,
        sceneSearch: '',
        sceneSelected: [],
        agentSelected: [],
        sceneTab: 'market',
        marketScenes: [],
        marketPage: 1,
        marketTotal: 0,
        marketPageSize: 12,
        repoScenes: [],
      };
    },
    computed: {
      filteredList() {
        let result = this.list;
        if (!this.showArchived) {
          result = result.filter(k => !k.archived);
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
      selectedAgentTags() {
        if (!this.detail || !this.detail.refs) return [];
        return this.detail.refs
          .filter(r => r.scene_id === '' && r.agent_type !== '' && r.enabled)
          .map(r => ({ ref: r, agentType: r.agent_type, label: this.agentLabel(r.agent_type) }));
      },
      availableAgentTypes() {
        const selected = new Set(this.selectedAgentTags.map(t => t.agentType));
        return this.agentTypes.filter(a => a.value && !selected.has(a.value));
      },
      selectedSceneTags() {
        if (!this.detail || !this.detail.refs) return [];
        return this.detail.refs
          .filter(r => r.scene_id !== '' && r.enabled)
          .map(r => {
            const scene = this.scenes.find(s => s.id === r.scene_id);
            return { ref: r, sceneId: r.scene_id, label: scene ? scene.name : String(r.scene_id || '').substring(0, 8) };
          });
      },
      availableScenes() {
        const selected = new Set(this.selectedSceneTags.map(t => t.sceneId));
        let source = this.sceneTab === 'market' ? this.marketScenes : this.repoScenes;
        let available = (source || []).filter(s => !selected.has(s.id));
        if (this.sceneSearch) {
          const q = this.sceneSearch.toLowerCase();
          available = available.filter(s => (s.name || '').toLowerCase().includes(q) || (s.id || '').toLowerCase().includes(q));
        }
        return available;
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
          const tabSource = { system: 'system', agent: 'agent', user: 'manual' };
          if (tabSource[this.activeTab]) params.set('source', tabSource[this.activeTab]);
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
      switchTab(tab) {
        this.activeTab = tab;
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
            id: String(s.id || s.scene_id || ''),
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

      // Agent 标签操作
      agentLabel(agentType) {
        const at = AGENT_TYPES.find(a => a.value === agentType);
        return at ? at.label : agentType;
      },
      async removeAgentRef(tag) {
        try {
          await API.put('/knowledge/ref/' + tag.ref.id, { enabled: false });
          tag.ref.enabled = false;
        } catch (e) {
          app.showToast(e.message);
        }
      },
      async addAgentByType(agentType) {
        const kid = this.detail.id;
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
      },

      // 全局标签操作
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

      // 场景标签操作
      async removeSceneRef(tag) {
        try {
          await API.put('/knowledge/ref/' + tag.ref.id, { enabled: false });
          tag.ref.enabled = false;
        } catch (e) {
          app.showToast(e.message);
        }
      },
      async addSceneById(sceneId) {
        const kid = this.detail.id;
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
      },
      openScenePopover() {
        this.addScenePopover = true;
        this.sceneSearch = '';
        this.sceneSelected = [];
        this.sceneTab = 'market';
        this.marketPage = 1;
        this.loadMarketScenes();
        this.loadRepoScenes();
      },
      switchSceneTab(tab) {
        this.sceneTab = tab;
        this.sceneSearch = '';
      },
      async loadMarketScenes() {
        try {
          const d = await API.get('/scenes/market?scope=official&page=' + this.marketPage + '&page_size=' + this.marketPageSize);
          if (window._currentView !== 'knowledge') return;
          this.marketScenes = (d.scenes || []).map(s => ({
            id: String(s.id || s.scene_id || ''),
            name: s.display_name || s.name || '',
            desc: s.market_description || s.description || '',
            category: s.category_name || '',
          }));
          this.marketTotal = d.total || 0;
        } catch (e) {
          this.marketScenes = [];
        }
      },
      async loadRepoScenes() {
        try {
          const repo = this.currentRepo();
          const d = await API.get('/scenes?repo=' + encodeURIComponent(repo));
          if (window._currentView !== 'knowledge') return;
          this.repoScenes = (d.scenes || []).map(s => ({
            id: String(s.id || s.scene_id || ''),
            name: s.display_name || s.name || '',
            desc: s.description || '',
            category: '',
          }));
        } catch (e) {
          this.repoScenes = [];
        }
      },
      marketTotalPages() {
        return Math.ceil(this.marketTotal / this.marketPageSize) || 1;
      },
      marketPrevPage() {
        if (this.marketPage > 1) {
          this.marketPage--;
          this.loadMarketScenes();
        }
      },
      marketNextPage() {
        if (this.marketPage < this.marketTotalPages()) {
          this.marketPage++;
          this.loadMarketScenes();
        }
      },
      toggleSceneSelect(sceneId) {
        const idx = this.sceneSelected.indexOf(sceneId);
        if (idx >= 0) {
          this.sceneSelected.splice(idx, 1);
        } else {
          this.sceneSelected.push(sceneId);
        }
      },
      async confirmAddScenes() {
        const kid = this.detail.id;
        let ok = 0;
        for (const sid of this.sceneSelected) {
          try {
            await API.post('/knowledge/' + kid + '/refs', { scene_id: sid, agent_type: '' });
            ok++;
          } catch (e) { /* ignore duplicates */ }
        }
        if (ok > 0) {
          await this.loadRefs(kid);
          app.showToast(this.t('ref_added_count', { count: ok }));
        }
        this.addScenePopover = false;
        this.sceneSelected = [];
      },

      // Agent 多选
      openAgentPopover() {
        this.addAgentPopover = true;
        this.agentSelected = [];
      },
      toggleAgentSelect(agentType) {
        const idx = this.agentSelected.indexOf(agentType);
        if (idx >= 0) {
          this.agentSelected.splice(idx, 1);
        } else {
          this.agentSelected.push(agentType);
        }
      },
      async confirmAddAgents() {
        const kid = this.detail.id;
        let ok = 0;
        for (const at of this.agentSelected) {
          try {
            await API.post('/knowledge/' + kid + '/refs', { scene_id: '', agent_type: at });
            ok++;
          } catch (e) { /* ignore duplicates */ }
        }
        if (ok > 0) {
          await this.loadRefs(kid);
          app.showToast(this.t('ref_added_count', { count: ok }));
        }
        this.addAgentPopover = false;
        this.agentSelected = [];
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
            <!-- 统计卡片（纯展示，不联动） -->
            <div class="stats-bar">
              <div class="stat-card">
                <span class="stat-num">{{ stats.system + stats.agent + stats.manual }}</span>
                <span class="stat-label">{{ t('stats_all') }}</span>
              </div>
              <div class="stat-card">
                <span class="stat-num">{{ statSystem() }}</span>
                <span class="stat-label">{{ t('stats_system') }}</span>
              </div>
              <div class="stat-card">
                <span class="stat-num">{{ statAgent() }}</span>
                <span class="stat-label">{{ t('stats_agent') }}</span>
              </div>
              <div class="stat-card">
                <span class="stat-num">{{ statUser() }}</span>
                <span class="stat-label">{{ t('stats_user') }}</span>
              </div>
            </div>

            <!-- Tab 栏（切换数据源） -->
            <div class="kb-tab-bar">
              <button class="kb-tab" :class="{ active: activeTab === 'all' }" @click="switchTab('all')">{{ t('stats_all') }}</button>
              <button class="kb-tab" :class="{ active: activeTab === 'system' }" @click="switchTab('system')">{{ t('stats_system') }}</button>
              <button class="kb-tab" :class="{ active: activeTab === 'agent' }" @click="switchTab('agent')">{{ t('stats_agent') }}</button>
              <button class="kb-tab" :class="{ active: activeTab === 'user' }" @click="switchTab('user')">{{ t('stats_user') }}</button>
            </div>

            <!-- 筛选器 -->
            <div class="filter-bar">
              <select v-if="activeTab === 'user'" v-model="filter.refStatus" class="kb-select">
                <option value="">{{ t('filter_ref_all') }}</option>
                <option value="linked">{{ t('filter_ref_linked') }}</option>
                <option value="unlinked">{{ t('filter_ref_unlinked') }}</option>
              </select>
              <input v-model="filter.query" :placeholder="t('search_placeholder')" class="kb-input">
              <label class="kb-checkbox-label">
                <input type="checkbox" v-model="showArchived"> {{ t('show_archived') }}
              </label>
              <button v-if="activeTab === 'user'" class="bt bt-o bt-s" style="color:var(--a);border-color:var(--a);margin-left:auto" @click="openEntryModal">{{ t('entry_create') }}</button>
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

          <!-- 头部卡片：标签 + 元数据 + 操作按钮 -->
          <div class="cd" style="padding:28px 32px;margin-bottom:20px;border-radius:20px">
            <div style="display:flex;align-items:flex-start;gap:16px;margin-bottom:20px">
              <div style="flex:1;min-width:0">
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap">
                  <span class="source-tag" :class="sourceClass(detail.source)" style="font-size:11px;padding:3px 12px">{{ sourceLabel(detail.source) }}</span>
                  <span class="tg tg-d" style="font-size:10px">{{ detail.scope }}</span>
                  <span v-if="detail.issue_tag" class="issue-tag">{{ detail.issue_tag }}</span>
                  <span v-if="detail.archived" class="archived-tag">{{ t('archived') }}</span>
                </div>
                <div style="font-size:11px;color:var(--t3);line-height:1.6">
                  {{ detail.created_by || '' }} · {{ (detail.created_at || '').substring(0, 16) }}
                  <span v-if="detail.file_path"> · {{ detail.file_path }}</span>
                </div>
              </div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;padding-top:16px;border-top:1px solid var(--b);flex-wrap:wrap">
              <button v-if="detail.source === 'manual' && !detail.archived" class="bt bt-o bt-s" @click="editKnowledge">{{ t('edit_save') }}</button>
              <button v-if="detail.source === 'manual' && !detail.archived" class="bt bt-o bt-s" style="color:var(--r);border-color:var(--r)" @click="deleteKnowledge">{{ t('delete') }}</button>
              <button v-if="detail.source !== 'manual' && !detail.archived" class="bt bt-o bt-s" @click="toggleArchive">{{ t('archive') }}</button>
              <button v-if="detail.archived" class="bt bt-o bt-s" @click="toggleArchive">{{ t('unarchive') }}</button>
            </div>
          </div>

          <!-- 内容卡片（固定高度，超出滚动） -->
          <div class="cd kb-detail-card" style="padding:24px;margin-bottom:20px;border-radius:18px">
            <div class="cd-h" style="margin-bottom:12px"><h2 style="font-size:16px;font-weight:600">{{ t('detail_content_title') }}</h2></div>
            <div class="kb-detail-scroll">
              <div v-html="renderMarkdown(detail.content || '')" class="kb-detail-content"></div>
            </div>
          </div>

          <!-- 系统知识：自动加载，不显示关联管理 -->
          <div v-if="detail.source === 'system'" class="cd kb-detail-card" style="padding:20px;margin-bottom:20px;border-radius:18px">
            <div class="ts t2" style="text-align:center">{{ t('ref_system_auto') }}</div>
          </div>

          <!-- 用户知识：可配置 Agent/场景 -->
          <template v-if="detail.source === 'manual'">
            <!-- Agent 关联卡片 -->
            <div class="cd kb-detail-card" style="padding:24px;margin-bottom:20px;border-radius:18px">
              <div class="cd-h" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
                <h2 style="font-size:16px;font-weight:600">{{ t('ref_agent_title') }}</h2>
                <div style="display:flex;align-items:center;gap:8px">
                  <span v-if="isGlobalChecked()" class="kb-tag kb-tag-global" style="font-size:10px">
                    {{ t('ref_global_short') }}
                    <button class="kb-tag-x" @click="toggleGlobalRef">×</button>
                  </span>
                  <button v-if="!isGlobalChecked()" class="kb-tag-add" style="font-size:10px" @click="toggleGlobalRef">+ {{ t('ref_add_global') }}</button>
                  <button class="kb-tag-add" style="font-size:10px" @click="openAgentPopover">+ {{ t('ref_add_agent') }}</button>
                </div>
              </div>
              <div v-if="selectedAgentTags.length === 0" class="ts t2" style="padding:12px 0">{{ t('ref_no_agents_linked') }}</div>
              <div v-else class="kb-scene-grid">
                <div v-for="tag in selectedAgentTags" :key="tag.agentType" class="cd kb-scene-card">
                  <button class="kb-scene-x" @click="removeAgentRef(tag)">×</button>
                  <div class="kb-scene-name">{{ tag.label }}</div>
                  <div class="kb-scene-id">{{ tag.agentType }}</div>
                </div>
              </div>
            </div>

            <!-- Agent 选择弹出层（多选） -->
            <div v-if="addAgentPopover" class="modal-overlay" @click.self="addAgentPopover = false">
              <div class="modal cd" style="width:480px">
                <h3 style="font-size:14px;font-weight:600;margin-bottom:14px">{{ t('ref_select_agents') }}</h3>
                <div v-if="availableAgentTypes.length === 0" class="ts t2" style="padding:12px">{{ t('ref_no_available') }}</div>
                <div v-else class="kb-scene-pick-grid">
                  <div v-for="at in availableAgentTypes" :key="at.value" class="kb-scene-pick-item"
                       :class="{ selected: agentSelected.includes(at.value) }"
                       @click="toggleAgentSelect(at.value)">
                    <div class="kb-scene-pick-name">{{ at.label }}</div>
                    <div class="kb-scene-pick-id">{{ at.value }}</div>
                  </div>
                </div>
                <div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px">
                  <span style="font-size:11px;color:var(--t3)">{{ t('ref_selected_count', { count: agentSelected.length }) }}</span>
                  <div style="display:flex;gap:8px">
                    <button class="bt bt-o bt-s" @click="addAgentPopover = false">{{ t('cancel') }}</button>
                    <button class="bt bt-p bt-s" :disabled="agentSelected.length === 0" @click="confirmAddAgents">{{ t('confirm') }}</button>
                  </div>
                </div>
              </div>
            </div>

            <!-- 场景关联卡片 -->
            <div class="cd kb-detail-card" style="padding:24px;margin-bottom:20px;border-radius:18px">
              <div class="cd-h" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
                <h2 style="font-size:16px;font-weight:600">{{ t('ref_scene_title') }}</h2>
                <button class="kb-tag-add" style="font-size:10px" @click="openScenePopover">+ {{ t('ref_add_scene') }}</button>
              </div>
              <div v-if="selectedSceneTags.length === 0" class="ts t2" style="padding:12px 0">{{ t('ref_no_scenes_linked') }}</div>
              <div v-else class="kb-scene-grid">
                <div v-for="tag in selectedSceneTags" :key="tag.sceneId" class="cd kb-scene-card">
                  <button class="kb-scene-x" @click="removeSceneRef(tag)">×</button>
                  <div class="kb-scene-name">{{ tag.label }}</div>
                  <div class="kb-scene-id">{{ String(tag.sceneId || '').substring(0, 12) }}...</div>
                </div>
              </div>
            </div>

            <!-- 场景选择弹出层（tab + 卡片 + 搜索 + 分页） -->
            <div v-if="addScenePopover" class="modal-overlay" @click.self="addScenePopover = false">
              <div class="modal cd" style="width:680px;max-width:92vw">
                <h3 style="font-size:14px;font-weight:600;margin-bottom:14px">{{ t('ref_select_scenes') }}</h3>

                <!-- Tab 切换 -->
                <div style="display:flex;gap:0;margin-bottom:14px;border-bottom:1px solid var(--b)">
                  <button class="kb-tab" :class="{ active: sceneTab === 'market' }" @click="switchSceneTab('market')">{{ t('ref_tab_market') }}</button>
                  <button class="kb-tab" :class="{ active: sceneTab === 'repo' }" @click="switchSceneTab('repo')">{{ t('ref_tab_repo') }}</button>
                </div>

                <!-- 搜索 -->
                <input v-model="sceneSearch" :placeholder="t('ref_search_scenes')" class="kb-input" style="width:100%;margin-bottom:12px">

                <!-- 场景列表（固定高度滚动） -->
                <div class="kb-scene-pick-scroll">
                  <div v-if="availableScenes.length === 0" class="ts t2" style="padding:24px;text-align:center">{{ t('ref_no_available') }}</div>
                  <div v-else class="kb-scene-pick-grid">
                    <div v-for="s in availableScenes" :key="s.id" class="kb-scene-pick-card"
                         :class="{ selected: sceneSelected.includes(s.id) }"
                         @click="toggleSceneSelect(s.id)">
                      <div class="kb-scene-pick-bar"></div>
                      <div class="kb-scene-pick-card-body">
                        <div class="kb-scene-pick-name">{{ s.name }}</div>
                        <div v-if="s.desc" class="kb-scene-pick-desc">{{ s.desc }}</div>
                        <div class="kb-scene-pick-id">{{ String(s.id || '').substring(0, 16) }}</div>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- 底部：分页 + 操作 -->
                <div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px;padding-top:12px;border-top:1px solid var(--b)">
                  <div style="display:flex;align-items:center;gap:8px">
                    <span style="font-size:11px;color:var(--t3)">{{ t('ref_selected_count', { count: sceneSelected.length }) }}</span>
                    <template v-if="sceneTab === 'market' && marketTotal > marketPageSize">
                      <button class="kb-page-btn" :disabled="marketPage <= 1" @click="marketPrevPage">‹</button>
                      <span style="font-size:11px;color:var(--t3)">{{ marketPage }}/{{ marketTotalPages() }}</span>
                      <button class="kb-page-btn" :disabled="marketPage >= marketTotalPages()" @click="marketNextPage">›</button>
                    </template>
                  </div>
                  <div style="display:flex;gap:8px">
                    <button class="bt bt-o bt-s" @click="addScenePopover = false">{{ t('cancel') }}</button>
                    <button class="bt bt-p bt-s" :disabled="sceneSelected.length === 0" @click="confirmAddScenes">{{ t('confirm') }}</button>
                  </div>
                </div>
              </div>
            </div>
          </template>
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
