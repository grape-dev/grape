// knowledge.js — 项目知识库页面（Issue #67 Vue 3 重写）
// 统一列表 + 统计卡片 + 筛选器 + 详情页面 + 关联管理

var _knowledgeVueApp = null;

const AGENT_TYPES = [
  { value: '', label: '全部 Agent', desc: '' },
  { value: 'vod', label: 'VOD', desc: '需求分析、拆解 Issue、规划开发路径' },
  { value: 'issue', label: 'Issue', desc: '需求拆解、任务规划、Issue 管理' },
  { value: 'coding', label: 'Coding', desc: '代码实现、修复缺陷、创建 PR' },
  { value: 'qa', label: 'QA', desc: '代码审查、测试验证、质量把关' },
  { value: 'sre', label: 'SRE', desc: '部署发布、监控告警、运维排障' },
  { value: 'chat', label: 'Chat', desc: '对话交互、知识沉淀、流程控制' },
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
        editMode: false,
        createForm: null,
        sceneScrollLeft: false,
        sceneScrollRight: false,
        createScenePopover: false,
        createSceneSearch: '',
        createSceneTempSelected: [],
        list: [],
        loading: false,
        firstLoad: true,
        filter: { refStatus: '', query: '' },
        stats: { system: 0, agent: 0, manual: 0, unlinked: 0 },
        showArchived: false,
        activeTab: 'system',
        page: 1,
        pageSize: 20,
        total: 0,
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
            return { ref: r, sceneId: r.scene_id, label: r.scene_display_name || r.scene_name || String(r.scene_id || '').substring(0, 8) };
          });
      },
      availableScenes() {
        const selected = new Set(this.selectedSceneTags.map(t => t.sceneId));
        let available = (this.repoScenes || []).filter(s => !selected.has(s.id));
        if (this.sceneSearch) {
          const q = this.sceneSearch.toLowerCase();
          available = available.filter(s => (s.name || '').toLowerCase().includes(q) || (s.id || '').toLowerCase().includes(q));
        }
        return available;
      },
      availableCreateScenes() {
        const selected = new Set(this.createForm?.sceneSelected || []);
        let available = (this.repoScenes || []).filter(s => !selected.has(s.id));
        if (this.createSceneSearch) {
          const q = this.createSceneSearch.toLowerCase();
          available = available.filter(s => (s.name || '').toLowerCase().includes(q) || (s.id || '').toLowerCase().includes(q));
        }
        return available;
      },
    },
    async mounted() {
      await this.loadStats();
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
          if (this.filter.query) params.set('q', this.filter.query);
          const d = await API.get('/knowledge?' + params.toString());
          if (window._currentView !== 'knowledge') return;
          this.list = d.knowledge || [];
          this.total = d.total || 0;
          this.firstLoad = false;
        } catch (e) {
          if (e.name === 'AbortError' && e.silent) return;
          if (window._currentView !== 'knowledge') return;
        } finally {
          if (window._currentView === 'knowledge') this.loading = false;
        }
      },
      async loadStats() {
        try {
          const repo = this.currentRepo();
          if (!repo) return;
          const d = await API.get('/knowledge/stats?repo=' + encodeURIComponent(repo));
          if (window._currentView !== 'knowledge') return;
          this.stats = d || { system: 0, agent: 0, manual: 0, unlinked: 0 };
        } catch (e) {
          if (e.name === 'AbortError' && e.silent) return;
        }
      },
      switchTab(tab) {
        this.activeTab = tab;
        this.page = 1;
        this.loadList();
      },
      totalPages() {
        return Math.max(1, Math.ceil(this.total / this.pageSize));
      },
      changePage(p) {
        this.page = p;
        this.loadList();
      },
      sourceLabel(s) {
        if (s === 'system') return this.t('source_system');
        if (s === 'agent') return this.t('source_agent');
        return this.t('source_manual');
      },
      knowledgeTitle(k) {
        if (!k.file_path) return k.description || '';
        const fname = k.file_path.split('/').pop().replace(/\.\w+$/, '');
        return fname.replace(/[-_]/g, ' ');
      },
      sourceBarClass(s) {
        if (s === 'system') return 'bg-default';
        if (s === 'agent') return 'bg-skill';
        if (s === 'manual') return 'bg-agent';
        return 'bg-default';
      },
      sourceIcon(s) {
        if (s === 'system') return '⚙';
        if (s === 'agent') return '🤖';
        if (s === 'manual') return '✍';
        return '📄';
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
          this.$nextTick(() => this.updateSceneScrollBtns());
        } catch (e) {
          if (e.name === 'AbortError' && e.silent) return;
          app.showToast(e.message || I18N.t('common.load_failed'));
        }
      },
      backToList() {
        this.editMode = false;
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
        // 刷新详情以获取完整 refs（含 scene_name/pipeline 等）
        try {
          const d = await API.get('/knowledge/' + kid);
          if (this.detail && this.detail.id === kid) {
            this.detail = d;
            this.$nextTick(() => this.updateSceneScrollBtns());
          }
        } catch (e) {
          // ignore
        }
      },

      // Agent 标签操作
      isAgentSelected(agentType) {
        if (!this.detail || !this.detail.refs) return false;
        return this.detail.refs.some(r => r.agent_type === agentType && r.scene_id === '' && r.enabled);
      },
      agentLabel(agentType) {
        const at = AGENT_TYPES.find(a => a.value === agentType);
        return at ? at.label : agentType;
      },
      async toggleAgentRef(agentType) {
        const kid = this.detail.id;
        if (this.isAgentSelected(agentType)) {
          // 已选 → 删除
          const ref = this.detail.refs.find(r => r.agent_type === agentType && r.scene_id === '' && r.enabled);
          if (ref) {
            try { await API.del('/knowledge/ref/' + ref.id); } catch(e) { app.showToast(e.message); }
          }
        } else {
          // 未选 → 添加
          try { await API.post('/knowledge/' + kid + '/refs/batch', { scene_ids: [], agent_types: [agentType] }); } catch(e) { app.showToast(e.message); }
        }
        await this.loadRefs(kid);
      },
      async removeAgentRef(tag) {
        try {
          await API.del('/knowledge/ref/' + tag.ref.id);
          tag.ref.enabled = false;
        } catch (e) {
          app.showToast(e.message);
        }
      },
      async addAgentByType(agentType) {
        const kid = this.detail.id;
        try {
          await API.post('/knowledge/' + kid + '/refs/batch', { scene_ids: [], agent_types: [agentType] });
          await this.loadRefs(kid);
          app.showToast(this.t('ref_add'));
        } catch (e) {
          app.showToast(e.message || I18N.t('common.load_failed'));
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
            await API.del('/knowledge/ref/' + existing.id);
            existing.enabled = false;
          } catch (e) {
            app.showToast(e.message);
          }
        } else {
          try {
            await API.post('/knowledge/' + kid + '/refs/batch', { scene_ids: [], agent_types: [] });
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
          await API.del('/knowledge/ref/' + tag.ref.id);
          tag.ref.enabled = false;
        } catch (e) {
          app.showToast(e.message);
        }
      },
      async addSceneById(sceneId) {
        const kid = this.detail.id;
        try {
          await API.post('/knowledge/' + kid + '/refs/batch', { scene_ids: [sceneId], agent_types: [] });
          await this.loadRefs(kid);
          app.showToast(this.t('ref_add'));
        } catch (e) {
          app.showToast(e.message || I18N.t('common.load_failed'));
        }
      },
      openScenePopover() {
        this.addScenePopover = true;
        this.sceneSearch = '';
        this.sceneSelected = [];
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
        try {
          await API.post('/knowledge/' + kid + '/refs/batch', { scene_ids: this.sceneSelected, agent_types: [] });
          await this.loadRefs(kid);
          app.showToast(this.t('ref_added_count', { count: this.sceneSelected.length }));
        } catch (e) {
          app.showToast(e.message || I18N.t('common.load_failed'));
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
        try {
          await API.post('/knowledge/' + kid + '/refs/batch', { scene_ids: [], agent_types: this.agentSelected });
          await this.loadRefs(kid);
          app.showToast(this.t('ref_added_count', { count: this.agentSelected.length }));
        } catch (e) {
          app.showToast(e.message || I18N.t('common.load_failed'));
        }
        this.addAgentPopover = false;
        this.agentSelected = [];
      },

      // 编辑
      editKnowledge() {
        this.editMode = true;
      },
      cancelEdit() {
        this.editMode = false;
      },
      scrollSceneTrack(delta) {
        const el = this.$refs.sceneTrack;
        if (el) el.scrollBy({ left: delta, behavior: 'smooth' });
      },
      updateSceneScrollBtns() {
        const el = this.$refs.sceneTrack;
        if (!el) return;
        this.sceneScrollLeft = el.scrollLeft > 5;
        this.sceneScrollRight = el.scrollLeft + el.clientWidth < el.scrollWidth - 5;
      },
      async saveEdit() {
        if (!this.detail) return;
        try {
          await API.put('/knowledge/' + this.detail.id, {
            content: this.detail.content || '',
            description: this.detail.description || ''
          });
          app.showToast(this.t('knowledge_updated'));
          this.editMode = false;
          await this.viewContent({ id: this.detail.id });
        } catch (e) {
          app.showToast(e.message || I18N.t('common.load_failed'));
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
            this.editMode = true;
          }
        });
      },

      // 新增知识
      openEntryModal() {
        this.createForm = {
          title: '',
          description: '',
          content: '',
          agentSelected: [],
          sceneSelected: []
        };
        this.view = 'create';
        this.loadScenes();
        this.loadRepoScenes();
      },
      getSceneName(sid) {
        const s = (this.repoScenes || []).find(x => x.id === sid);
        return s ? s.name : String(sid || '').substring(0, 12);
      },
      openCreateScenePopover() {
        this.createScenePopover = true;
        this.createSceneSearch = '';
        this.createSceneTempSelected = [...(this.createForm?.sceneSelected || [])];
        this.loadRepoScenes();
      },
      confirmCreateScenes() {
        if (this.createForm) {
          this.createForm.sceneSelected = [...this.createSceneTempSelected];
        }
        this.createScenePopover = false;
      },
      cancelCreate() {
        this.createForm = null;
        this.view = 'list';
      },
      toggleCreateAgent(agentType) {
        const idx = this.createForm.agentSelected.indexOf(agentType);
        if (idx >= 0) this.createForm.agentSelected.splice(idx, 1);
        else this.createForm.agentSelected.push(agentType);
      },
      toggleCreateScene(sceneId) {
        // 场景列表里的 X 删除（直接删）
        const idx = this.createForm.sceneSelected.indexOf(sceneId);
        if (idx >= 0) this.createForm.sceneSelected.splice(idx, 1);
      },
      toggleCreateSceneTemp(sceneId) {
        // 弹出层里切换临时选中
        const idx = this.createSceneTempSelected.indexOf(sceneId);
        if (idx >= 0) this.createSceneTempSelected.splice(idx, 1);
        else this.createSceneTempSelected.push(sceneId);
      },
      async saveCreate() {
        if (!this.createForm.content.trim()) { app.showToast(this.t('entry_content')); return; }
        try {
          const d = await API.post('/knowledge/entry', {
            repo: this.currentRepo(),
            content: this.createForm.content,
            agent_type: '',
            description: this.createForm.description || ''
          });
          const kid = d.id || d.knowledge_id;
          // 批量关联 Agent + 场景
          if (kid && (this.createForm.agentSelected.length > 0 || this.createForm.sceneSelected.length > 0)) {
            try {
              await API.post('/knowledge/' + kid + '/refs/batch', {
                scene_ids: this.createForm.sceneSelected,
                agent_types: this.createForm.agentSelected
              });
            } catch(e) {}
          }
          app.showToast(this.t('knowledge_created'));
          this.createForm = null;
          this.view = 'list';
          await this.loadStats();
          await this.loadList();
        } catch (e) {
          app.showToast(e.message || I18N.t('common.load_failed'));
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
          <template v-if="!firstLoad">
            <!-- 统计卡片（点击切换 tab，在 apple-card 外面） -->
            <div class="stats-bar">
              <div class="stat-card" :class="{ active: activeTab === 'system' }" @click="switchTab('system')">
                <span class="stat-num">{{ statSystem() }}</span>
                <span class="stat-label">{{ t('stats_system') }}</span>
              </div>
              <div class="stat-card" :class="{ active: activeTab === 'agent' }" @click="switchTab('agent')">
                <span class="stat-num">{{ statAgent() }}</span>
                <span class="stat-label">{{ t('stats_agent') }}</span>
              </div>
              <div class="stat-card" :class="{ active: activeTab === 'user' }" @click="switchTab('user')">
                <span class="stat-num">{{ statUser() }}</span>
                <span class="stat-label">{{ t('stats_user') }}</span>
              </div>
            </div>

            <!-- Tab 栏（在 apple-card 外面） -->
            <div class="kb-tab-bar">
              <button class="kb-tab" :class="{ active: activeTab === 'system' }" @click="switchTab('system')">{{ t('stats_system') }}</button>
              <button class="kb-tab" :class="{ active: activeTab === 'agent' }" @click="switchTab('agent')">{{ t('stats_agent') }}</button>
              <button class="kb-tab" :class="{ active: activeTab === 'user' }" @click="switchTab('user')">{{ t('stats_user') }}</button>
            </div>

            <div class="apple-card" style="padding:20px">
              <!-- 筛选器 -->
            <div class="filter-bar">
              <select v-if="activeTab === 'user'" v-model="filter.refStatus" class="kb-select">
                <option value="">{{ t('filter_ref_all') }}</option>
                <option value="linked">{{ t('filter_ref_linked') }}</option>
                <option value="unlinked">{{ t('filter_ref_unlinked') }}</option>
              </select>
              <input v-model="filter.query" :placeholder="t('search_placeholder')" class="kb-input" @keyup.enter="loadList">
              <button class="bt bt-o bt-s" @click="loadList">{{ t('search_btn') }}</button>
              <label class="kb-checkbox-label">
                <input type="checkbox" v-model="showArchived"> {{ t('show_archived') }}
              </label>
              <div style="margin-left:auto;display:flex;gap:8px">
                <button v-if="activeTab === 'user'" class="bt bt-o bt-s" style="color:var(--a);border-color:var(--a)" @click="openEntryModal">{{ t('entry_create') }}</button>
              </div>
            </div>

            <div v-if="loading" class="kb-loading-overlay">
              <div class="kb-spinner"></div>
              <span style="font-size:12px;color:var(--t3);margin-top:8px">{{ t('loading') }}</span>
            </div>
            <div v-if="filteredList.length === 0 && !loading" class="ts t2" style="padding:12px">{{ t('empty') }}</div>
            <div v-show="filteredList.length > 0 && !loading" class="kb-grid">
              <div v-for="k in filteredList" :key="k.id || k.file_path" class="skill-card" :class="{ 'archived-card': k.archived }" @click="viewContent(k)">
                <div class="scn-card-bar" :class="sourceBarClass(k.source)"></div>
                <div class="scn-card-body">
                  <div class="scn-card-head">
                    <div class="scn-ic" :class="sourceBarClass(k.source)">{{ sourceIcon(k.source) }}</div>
                    <div class="scn-card-title">{{ knowledgeTitle(k) }}</div>
                    <span v-if="k.archived" class="archived-tag">{{ t('archived') }}</span>
                  </div>
                  <div class="scn-card-desc">{{ k.description || t('no_description') }}</div>
                  <div class="scn-card-foot">
                    <span class="scn-meta">{{ k.created_by || '' }} · {{ (k.created_at || '').substring(0, 16) }}</span>
                    <div class="scn-card-btns">
                      <button class="bt bt-o bt-s" @click.stop="viewContent(k)">{{ t('view') }}</button>
                      <button v-if="k.source === 'manual' && !k.archived" class="bt bt-o bt-s" style="color:var(--r);border-color:var(--r)" @click.stop="quickDelete(k)">{{ t('delete') }}</button>
                      <button v-if="k.source !== 'manual' && !k.archived" class="bt bt-o bt-s" @click.stop="quickArchive(k)">{{ t('archive') }}</button>
                      <button v-if="k.archived" class="bt bt-o bt-s" @click.stop="quickArchive(k)">{{ t('unarchive') }}</button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <!-- 分页器 -->
            <div class="kb-pager">
              <button class="kb-page-btn" :disabled="page <= 1" @click="changePage(page - 1)">‹</button>
              <span style="font-size:12px;color:var(--t2)">{{ page }} / {{ totalPages() }} ({{ total }})</span>
              <button class="kb-page-btn" :disabled="page >= totalPages()" @click="changePage(page + 1)">›</button>
              <select v-model="pageSize" class="kb-select" style="width:auto" @change="changePage(1)">
                <option :value="20">20/页</option>
                <option :value="50">50/页</option>
              </select>
            </div>
            </div>
          </template>
        </template>

        <!-- ===== 详情视图 ===== -->
        <template v-if="view === 'detail' && detail && !editMode">
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
                <h2 style="font-size:17px;font-weight:600;color:var(--t);margin-bottom:8px">{{ knowledgeTitle(detail) }}</h2>
                <div style="font-size:13px;color:var(--t2);line-height:1.5;margin-bottom:8px;font-weight:500">
                  {{ detail.description || t('no_description') }}
                </div>
                <div style="font-size:11px;color:var(--t3);line-height:1.6">
                  {{ detail.created_by || '' }} · {{ (detail.created_at || '').substring(0, 16) }}
                  <span v-if="detail.file_path"> · {{ detail.file_path }}</span>
                </div>
              </div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;padding-top:16px;border-top:1px solid var(--b);flex-wrap:wrap;justify-content:flex-end">
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
            <!-- Agent 关联卡片（标签式，点击切换） -->
            <div class="cd kb-detail-card" style="padding:24px;margin-bottom:20px;border-radius:18px">
              <div class="cd-h" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
                <h2 style="font-size:16px;font-weight:600">{{ t('ref_agent_title') }}</h2>
                <span v-if="isGlobalChecked()" class="kb-tag kb-tag-global" style="font-size:10px;cursor:pointer" @click="toggleGlobalRef">
                  {{ t('ref_global_short') }} ×
                </span>
                <button v-else class="kb-tag-add" style="font-size:10px" @click="toggleGlobalRef">+ {{ t('ref_add_global') }}</button>
              </div>
              <div style="display:flex;flex-wrap:wrap;gap:8px">
                <div v-for="at in agentTypes.filter(a => a.value)" :key="at.value"
                  class="kb-agent-tag" :class="{ 'kb-agent-selected': isAgentSelected(at.value) }"
                  @click="toggleAgentRef(at.value)">
                  <div class="kb-agent-name">{{ at.label }}</div>
                  <div v-if="at.desc" class="kb-agent-desc">{{ at.desc }}</div>
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
              <div v-else class="scn-scroll-wrap">
                <button class="scn-scroll-btn scn-scroll-prev" :class="{ show: sceneScrollLeft }" @click="scrollSceneTrack(-314)">◀</button>
                <div class="scn-scroll-track" ref="sceneTrack" @scroll="updateSceneScrollBtns">
                  <div v-for="tag in selectedSceneTags" :key="tag.sceneId" class="scn-card" style="flex-shrink:0;width:300px;position:relative">
                    <button class="kb-scene-x" style="z-index:5" @click="removeSceneRef(tag)">×</button>
                    <div class="scn-card-bar" :class="sourceBarClass(detail.source)"></div>
                    <div class="scn-card-body">
                      <div class="scn-card-head">
                        <div class="scn-ic bg-default">📦</div>
                        <div class="scn-card-title">{{ tag.ref.scene_display_name || tag.label }}</div>
                      </div>
                      <div v-if="tag.ref.scene_description" class="scn-card-desc">{{ tag.ref.scene_description }}</div>
                      <div v-if="tag.ref.scene_pipeline" class="scn-chain">
                        <template v-for="(node, i) in tag.ref.scene_pipeline.split(' → ')" :key="i">
                          <span v-if="i > 0" class="scn-arr">→</span>
                          <span class="scn-tag">{{ node }}</span>
                        </template>
                      </div>
                    </div>
                  </div>
                </div>
                <button class="scn-scroll-btn scn-scroll-next" :class="{ show: sceneScrollRight }" @click="scrollSceneTrack(314)">▶</button>
              </div>
            </div>

            <!-- 场景选择弹出层 -->
            <div v-if="addScenePopover" class="modal-overlay" @click.self="addScenePopover = false">
              <div class="modal cd" style="width:720px;max-width:92vw">
                <h3 style="font-size:14px;font-weight:600;margin-bottom:14px">{{ t('ref_select_scenes') }}</h3>

                <!-- 搜索 -->
                <input v-model="sceneSearch" :placeholder="t('ref_search_scenes')" class="kb-input" style="width:100%;margin-bottom:12px">

                <!-- 场景列表（固定高度滚动） -->
                <div class="kb-scene-pick-scroll" style="max-height:400px;overflow-y:auto;padding-right:4px">
                  <div v-if="availableScenes.length === 0" class="ts t2" style="padding:24px;text-align:center">{{ t('ref_no_available') }}</div>
                  <div v-else class="rgrid" style="gap:12px">
                    <div v-for="s in availableScenes" :key="s.id" class="scn-card"
                         :class="{ 'kb-pick-selected': sceneSelected.includes(s.id) }"
                         style="cursor:pointer;position:relative"
                         @click="toggleSceneSelect(s.id)">
                      <div v-if="sceneSelected.includes(s.id)" class="kb-pick-check">✓</div>
                      <div class="scn-card-bar bg-default"></div>
                      <div class="scn-card-body">
                        <div class="scn-card-head">
                          <div class="scn-ic bg-default">📦</div>
                          <div class="scn-card-title">{{ s.name }}</div>
                        </div>
                        <div v-if="s.desc" class="scn-card-desc">{{ s.desc }}</div>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- 底部：已选数量 + 操作 -->
                <div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px;padding-top:12px;border-top:1px solid var(--b)">
                  <span style="font-size:11px;color:var(--t3)">{{ t('ref_selected_count', { count: sceneSelected.length }) }}</span>
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

        <!-- ===== 编辑视图 ===== -->
        <template v-if="view === 'detail' && detail && editMode">
          <div style="margin-bottom:16px">
            <button class="bt bt-o bt-s" @click="cancelEdit">← {{ t('back_to_list') }}</button>
          </div>
          <div class="cd" style="padding:28px 32px;margin-bottom:20px;border-radius:20px">
            <div class="cd-h" style="margin-bottom:16px">
              <h2 style="font-size:16px;font-weight:600">{{ t('edit_title') }}</h2>
            </div>
            <input v-model="detail.description" :placeholder="t('entry_description')" class="kb-input" style="width:100%;margin-bottom:12px">
            <textarea v-model="detail.content" rows="20" class="kb-textarea"></textarea>
            <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px;padding-top:16px;border-top:1px solid var(--b)">
              <button class="bt bt-o bt-s" @click="cancelEdit">{{ t('cancel') }}</button>
              <button class="bt bt-p bt-s" @click="saveEdit">{{ t('confirm') }}</button>
            </div>
          </div>
        </template>

        <!-- ===== 新增视图 ===== -->
        <template v-if="view === 'create' && createForm">
          <div style="margin-bottom:16px">
            <button class="bt bt-o bt-s" @click="cancelCreate">← {{ t('back_to_list') }}</button>
          </div>
          <div class="cd" style="padding:28px 32px;margin-bottom:20px;border-radius:20px">
            <div class="cd-h" style="margin-bottom:16px">
              <h2 style="font-size:16px;font-weight:600">{{ t('entry_title') }}</h2>
            </div>
            <div style="margin-bottom:12px">
              <label style="font-size:11px;color:var(--t3);display:block;margin-bottom:4px">{{ t('entry_description') }}</label>
              <input v-model="createForm.description" :placeholder="t('entry_description')" class="kb-input" style="width:100%">
            </div>
            <div style="margin-bottom:16px">
              <label style="font-size:11px;color:var(--t3);display:block;margin-bottom:4px">{{ t('entry_content') }}</label>
              <textarea v-model="createForm.content" rows="20" class="kb-textarea"></textarea>
            </div>
          </div>

          <!-- Agent 关联卡片（标签式，同详情页面） -->
          <div class="cd kb-detail-card" style="padding:24px;margin-bottom:20px;border-radius:18px">
            <div class="cd-h" style="margin-bottom:14px">
              <h2 style="font-size:16px;font-weight:600">{{ t('ref_agent_title') }}</h2>
            </div>
            <div style="display:flex;flex-wrap:wrap;gap:8px">
              <div v-for="at in agentTypes.filter(a => a.value)" :key="at.value"
                class="kb-agent-tag" :class="{ 'kb-agent-selected': createForm.agentSelected.includes(at.value) }"
                @click="toggleCreateAgent(at.value)">
                <div class="kb-agent-name">{{ at.label }}</div>
                <div v-if="at.desc" class="kb-agent-desc">{{ at.desc }}</div>
              </div>
            </div>
          </div>

          <!-- 场景关联卡片（scn-card + 弹出层选择，同详情页面） -->
          <div class="cd kb-detail-card" style="padding:24px;margin-bottom:20px;border-radius:18px">
            <div class="cd-h" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
              <h2 style="font-size:16px;font-weight:600">{{ t('ref_scene_title') }}</h2>
              <button class="kb-tag-add" style="font-size:10px" @click="openCreateScenePopover">+ {{ t('ref_add_scene') }}</button>
            </div>
            <div v-if="createForm.sceneSelected.length === 0" class="ts t2" style="padding:12px 0">{{ t('ref_no_scenes_linked') }}</div>
            <div v-else class="scn-scroll-wrap">
              <div class="scn-scroll-track">
                <div v-for="sid in createForm.sceneSelected" :key="sid" class="scn-card" style="flex-shrink:0;width:300px;position:relative">
                  <button class="kb-scene-x" style="z-index:5" @click="toggleCreateScene(sid)">×</button>
                  <div class="scn-card-bar bg-default"></div>
                  <div class="scn-card-body">
                    <div class="scn-card-head">
                      <div class="scn-ic bg-default">📦</div>
                      <div class="scn-card-title">{{ getSceneName(sid) }}</div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="bt bt-o bt-s" @click="cancelCreate">{{ t('cancel') }}</button>
            <button class="bt bt-p bt-s" @click="saveCreate">{{ t('confirm') }}</button>
          </div>
        </template>

        <!-- 新增页面场景选择弹出层 -->
        <div v-if="createScenePopover" class="modal-overlay" @click.self="createScenePopover = false">
          <div class="modal cd" style="width:720px;max-width:92vw">
            <h3 style="font-size:14px;font-weight:600;margin-bottom:14px">{{ t('ref_select_scenes') }}</h3>
            <input v-model="createSceneSearch" :placeholder="t('ref_search_scenes')" class="kb-input" style="width:100%;margin-bottom:12px">
            <div style="max-height:400px;overflow-y:auto;padding-right:4px">
              <div v-if="availableCreateScenes.length === 0" class="ts t2" style="padding:24px;text-align:center">{{ t('ref_no_available') }}</div>
              <div v-else class="rgrid" style="gap:12px">
                <div v-for="s in availableCreateScenes" :key="s.id" class="scn-card"
                     :class="{ 'kb-pick-selected': createSceneTempSelected.includes(s.id) }"
                     style="cursor:pointer;position:relative"
                     @click="toggleCreateSceneTemp(s.id)">
                  <div v-if="createSceneTempSelected.includes(s.id)" class="kb-pick-check">✓</div>
                  <div class="scn-card-bar bg-default"></div>
                  <div class="scn-card-body">
                    <div class="scn-card-head">
                      <div class="scn-ic bg-default">📦</div>
                      <div class="scn-card-title">{{ s.name }}</div>
                    </div>
                    <div v-if="s.desc" class="scn-card-desc">{{ s.desc }}</div>
                  </div>
                </div>
              </div>
            </div>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px;padding-top:12px;border-top:1px solid var(--b)">
              <span style="font-size:11px;color:var(--t3)">{{ t('ref_selected_count', { count: createSceneTempSelected.length }) }}</span>
              <div style="display:flex;gap:8px">
                <button class="bt bt-o bt-s" @click="createScenePopover = false">{{ t('cancel') }}</button>
                <button class="bt bt-p bt-s" @click="confirmCreateScenes">{{ t('confirm') }}</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    `,
  });

  _knowledgeVueApp.mount(el);
}
