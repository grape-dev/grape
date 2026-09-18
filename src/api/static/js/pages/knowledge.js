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
        editForm: null,
        editFormSnapshot: null,
        createForm: null,
        submitting: false,
        sceneScrollLeft: false,
        sceneScrollRight: false,
        createScenePopover: false,
        createSceneSearch: '',
        createSceneTempSelected: [],
        list: [],
        loading: false,
        firstLoad: true,
        filter: { refStatus: '', query: '', sortLatest: false },
        stats: { system: 0, agent: 0, manual: 0, unlinked: 0 },
        activeTab: 'system',
        page: 1,
        pageSize: 20,
        total: 0,
        detail: null,
        agentTypes: AGENT_TYPES,
        addScenePopover: false,
        sceneSearch: '',
        sceneSelected: [],
        sceneTab: 'market',
        marketScenes: [],
        marketPage: 1,
        marketTotal: 0,
        marketPageSize: 12,
        repoScenes: [],
        pendingAgentType: null,
        pendingSceneId: null,
      };
    },
    computed: {
      filteredList() {
        let result = this.list;
        if (this.filter.refStatus === 'linked') {
          result = result.filter(k => k.refs && k.refs.length > 0);
        } else if (this.filter.refStatus === 'unlinked') {
          result = result.filter(k => !k.refs || k.refs.length === 0);
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
          if (this.filter.sortLatest) params.set('sort', 'latest');
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
      toggleSortLatest() {
        this.filter.sortLatest = !this.filter.sortLatest;
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
      agentRefCount(k) {
        if (!k.refs) return 0;
        return k.refs.filter(r => r.agent_type !== '' && r.scene_id === '' && r.enabled).length;
      },
      sceneRefCount(k) {
        if (!k.refs) return 0;
        return k.refs.filter(r => r.scene_id !== '' && r.enabled).length;
      },
      agentNames(k) {
        if (!k.refs) return [];
        return k.refs.filter(r => r.agent_type !== '' && r.scene_id === '' && r.enabled).map(r => {
          const at = AGENT_TYPES.find(a => a.value === r.agent_type);
          return at ? at.label : r.agent_type;
        });
      },
      sceneNames(k) {
        if (!k.refs) return [];
        return k.refs.filter(r => r.scene_id !== '' && r.enabled).map(r => r.scene_name || r.scene_id.substring(0, 8));
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
      async _refreshDetailFull() {
        if (!this.detail) return;
        try {
          const d = await API.get('/knowledge/' + this.detail.id);
          if (window._currentView !== 'knowledge') return;
          this.detail = d;
          this.$nextTick(() => this.updateSceneScrollBtns());
        } catch (e) {
          if (e.name === 'AbortError' && e.silent) return;
          app.showToast(this.t('refresh_failed') || '刷新失败，请手动刷新页面');
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
        if (this.submitting) return;
        this.submitting = true;
        this.pendingAgentType = agentType;
        try {
          const kid = this.detail.id;
          if (this.isAgentSelected(agentType)) {
            const ref = this.detail.refs.find(r => r.agent_type === agentType && r.scene_id === '' && r.enabled);
            if (ref) {
              await API.del('/knowledge/ref/' + ref.id, 15000);
              await this._refreshDetailFull();
              app.showToast(this.t('ref_unbind_agent_ok'));
            }
          } else {
            await API.post('/knowledge/' + kid + '/refs/batch', { scene_ids: [], agent_types: [agentType] }, 15000);
            await this._refreshDetailFull();
            app.showToast(this.t('ref_bind_agent_ok'));
          }
        } catch (e) {
          if (e.name === 'AbortError' && !e.silent) {
            app.showToast(this.t('ref_timeout_refresh'));
            await this._refreshDetailFull();
          } else if (e.name === 'AbortError' && e.silent) {
            // 导航/切 tab 导致的取消，静默
          } else {
            app.showToast(e.message || this.t('ref_failed_refresh'));
            await this._refreshDetailFull();
          }
        } finally {
          this.submitting = false;
          this.pendingAgentType = null;
        }
      },

      // 场景标签操作
      async removeSceneRef(tag) {
        if (this.submitting) return;
        this.submitting = true;
        this.pendingSceneId = tag.sceneId;
        try {
          await API.del('/knowledge/ref/' + tag.ref.id, 15000);
          await this._refreshDetailFull();
          app.showToast(this.t('ref_unbind_scene_ok'));
        } catch (e) {
          if (e.name === 'AbortError' && !e.silent) {
            app.showToast(this.t('ref_timeout_refresh'));
            await this._refreshDetailFull();
          } else if (e.name === 'AbortError' && e.silent) {
            // 导航/切 tab 导致的取消，静默
          } else {
            app.showToast(e.message || this.t('ref_failed_refresh'));
            await this._refreshDetailFull();
          }
        } finally {
          this.submitting = false;
          this.pendingSceneId = null;
        }
      },
      openScenePopover(e) {
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
            pipeline: (s.nodes || []).filter(n => n.enabled !== false).map(n => n.tag || n.id || '').join(' → '),
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
        if (this.submitting || this.sceneSelected.length === 0) return;
        this.submitting = true;
        const kid = this.detail.id;
        try {
          await API.post('/knowledge/' + kid + '/refs/batch', { scene_ids: this.sceneSelected, agent_types: [] }, 15000);
          await this._refreshDetailFull();
          app.showToast(this.t('ref_added_count', { count: this.sceneSelected.length }));
          this.addScenePopover = false;
          this.sceneSelected = [];
        } catch (e) {
          if (e.name === 'AbortError' && !e.silent) {
            app.showToast(this.t('ref_timeout_refresh'));
            await this._refreshDetailFull();
          } else if (e.name === 'AbortError' && e.silent) {
            // 导航/切 tab 导致的取消，静默
          } else {
            app.showToast(e.message || this.t('ref_failed_refresh'));
            await this._refreshDetailFull();
          }
        } finally {
          this.submitting = false;
        }
      },

      // 编辑
      editKnowledge() {
        const snapshot = {
          name: this.detail.name || '',
          content: this.detail.content || '',
          description: this.detail.description || ''
        };
        this.editForm = { ...snapshot };
        this.editFormSnapshot = snapshot;
        this.editMode = true;
      },
      resetEdit() {
        if (this.editFormSnapshot) {
          this.editForm = { ...this.editFormSnapshot };
        }
      },
      cancelEdit() {
        this.editMode = false;
      },
      importFile(event, target) {
        const file = event.target.files[0];
        if (!file) return;
        if (file.size > 10240) {
          app.showToast(this.t('file_too_large'));
          event.target.value = '';
          return;
        }
        const reader = new FileReader();
        reader.onload = (e) => {
          const text = e.target.result || '';
          if (target === 'create') {
            this.createForm.content = text;
            if (!this.createForm.name) {
              this.createForm.name = file.name.replace(/\.\w+$/, '').replace(/[-_]/g, ' ');
            }
          } else {
            this.editForm.content = text;
          }
          app.showToast(this.t('file_imported'));
        };
        reader.readAsText(file);
        event.target.value = '';
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
        if (!this.editForm.name.trim()) { app.showToast(this.t('name_required')); return; }
        if (this.editForm.name.length > 50) { app.showToast(this.t('name_too_long')); return; }
        if (this.editForm.description.length > 200) { app.showToast(this.t('desc_too_long')); return; }
        if (!this.editForm.content.trim()) { app.showToast(this.t('content_required')); return; }
        if (new Blob([this.editForm.content]).size > 10240) { app.showToast(this.t('content_too_large')); return; }
        if (this.submitting) return;
        this.submitting = true;
        try {
          await API.put('/knowledge/' + this.detail.id, {
            content: this.editForm.content || '',
            description: this.editForm.description || '',
            name: this.editForm.name.trim()
          }, 15000);
          app.showToast(this.t('knowledge_updated'));
          // Issue #199: 保存成功后返回列表
          this.backToList();
        } catch (e) {
          if (e.name === 'AbortError' && !e.silent) {
            app.showToast(this.t('save_timeout'));
          } else if (e.name === 'AbortError' && e.silent) {
            // 导航/切 tab 导致的取消，静默
          } else {
            app.showToast(e.message || I18N.t('common.load_failed'));
          }
        } finally {
          this.submitting = false;
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

      // 卡片上的快速操作
      async quickDelete(k) {
        const self = this;
        _showAppleConfirm(this.t('delete_confirm'), async function() {
          if (self.submitting) return; self.submitting = true;
          const _t = setTimeout(() => self.submitting = false, 10000);
          try {
            await API.del('/knowledge/' + k.id);
            app.showToast(self.t('knowledge_deleted'));
            await self.loadList();
          } catch (e) {
            app.showToast(e.message || I18N.t('common.load_failed'));
          } finally { clearTimeout(_t); self.submitting = false; }
        });
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
          name: '',
          title: '',
          description: '',
          content: '',
          agentSelected: [],
          sceneSelected: []
        };
        this.view = 'create';
        this.loadRepoScenes();
      },
      getSceneName(sid) {
        const s = (this.repoScenes || []).find(x => x.id === sid);
        return s ? s.name : String(sid || '').substring(0, 12);
      },
      getSceneDesc(sid) {
        const s = (this.repoScenes || []).find(x => x.id === sid);
        return s ? (s.desc || '') : '';
      },
      getScenePipeline(sid) {
        const s = (this.repoScenes || []).find(x => x.id === sid);
        return s ? (s.pipeline || '') : '';
      },
      openCreateScenePopover(e) {
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
        if (!this.createForm.name.trim()) { app.showToast(this.t('name_required')); return; }
        if (this.createForm.name.length > 50) { app.showToast(this.t('name_too_long')); return; }
        if (this.createForm.description.length > 200) { app.showToast(this.t('desc_too_long')); return; }
        if (!this.createForm.content.trim()) { app.showToast(this.t('content_required')); return; }
        if (new Blob([this.createForm.content]).size > 10240) { app.showToast(this.t('content_too_large')); return; }
        if (this.submitting) return;
        this.submitting = true;
        try {
          const d = await API.post('/knowledge/entry', {
            repo: this.currentRepo(),
            content: this.createForm.content,
            agent_type: '',
            description: this.createForm.description || '',
            name: this.createForm.name.trim()
          }, 15000);
          const kid = d.id || d.knowledge_id;
          if (kid && (this.createForm.agentSelected.length > 0 || this.createForm.sceneSelected.length > 0)) {
            await API.post('/knowledge/' + kid + '/refs/batch', {
              scene_ids: this.createForm.sceneSelected,
              agent_types: this.createForm.agentSelected
            }, 15000);
          }
          app.showToast(this.t('knowledge_created'));
          this.createForm = null;
          this.view = 'list';
          // Issue #199: loadStats + loadList 并行
          await Promise.all([this.loadStats(), this.loadList()]);
        } catch (e) {
          if (e.name === 'AbortError' && !e.silent) {
            app.showToast(this.t('create_timeout'));
            this.createForm = null;
            this.view = 'list';
            await this.loadList();
          } else if (e.name === 'AbortError' && e.silent) {
            // 导航/切 tab 导致的取消，静默
          } else {
            app.showToast(e.message || I18N.t('common.load_failed'));
          }
        } finally {
          this.submitting = false;
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
          // 安全模式：marked 默认不转义 HTML，需手动转义防止 XSS
          // 转义后 marked 仍可解析 Markdown 语法（#/-/* 等不依赖 <>）
          const escaped = (content || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
          return marked.parse(escaped);
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
              <div class="stat-card">
                <div class="stat-info">
                  <span class="stat-num">{{ statSystem() }}</span>
                  <span class="stat-label">{{ t('stats_system') }}</span>
                </div>
                <div class="stat-icon" style="background:rgba(0,113,227,0.1);color:#0071e3">⚙</div>
              </div>
              <div class="stat-card">
                <div class="stat-info">
                  <span class="stat-num">{{ statAgent() }}</span>
                  <span class="stat-label">{{ t('stats_agent') }}</span>
                </div>
                <div class="stat-icon" style="background:rgba(255,149,0,0.1);color:#ff9500">🤖</div>
              </div>
              <div class="stat-card">
                <div class="stat-info">
                  <span class="stat-num">{{ statUser() }}</span>
                  <span class="stat-label">{{ t('stats_user') }}</span>
                </div>
                <div class="stat-icon" style="background:rgba(52,199,89,0.1);color:#34c759">👤</div>
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
              <button class="bt bt-s" :class="filter.sortLatest ? 'bt-p' : 'bt-o'" @click="toggleSortLatest">{{ t('latest') }}</button>
              <button v-if="activeTab === 'user'" class="bt bt-p bt-s" @click="openEntryModal">{{ t('entry_create') }}</button>
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
                    <div class="scn-card-title">{{ k.name || knowledgeTitle(k) }}</div>
                  </div>
                  <div class="scn-card-desc" :title="k.description || t('no_description')">{{ k.description || t('no_description') }}</div>
                  <div style="display:flex;align-items:center;gap:8px;margin-bottom:14px;flex-wrap:wrap;font-size:10px;min-height:17px">
                    <span v-if="sceneRefCount(k) > 0" class="kb-ref-pill" :title="'场景：' + sceneNames(k).join('、')" @click.stop="viewContent(k)">
                      {{ t('scenes_unit') }}{{ sceneRefCount(k) }}
                    </span>
                    <span v-if="agentRefCount(k) > 0" class="kb-ref-pill" :title="'Agent：' + agentNames(k).join('、')" @click.stop="viewContent(k)">
                      Agent {{ agentRefCount(k) }}
                    </span>
                  </div>
                  <div class="scn-card-foot">
                    <span class="scn-meta" :title="k.created_by + ' · ' + (k.created_at || '').substring(0, 16)">{{ k.created_by || '' }} · {{ (k.created_at || '').substring(0, 16) }}</span>
                    <div class="scn-card-btns">
                      <button class="bt bt-p bt-s" @click.stop="viewContent(k)">{{ t('view') }}</button>
                      <button v-if="k.source === 'manual' && !k.archived" class="bt bt-o bt-s" style="color:var(--r);border-color:var(--r)" :disabled="submitting" @click.stop="quickDelete(k)">{{ t('delete') }}</button>
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
                  <span v-if="detail.issue_tag" class="issue-tag">{{ detail.issue_tag }}</span>
                </div>
                <h2 style="font-size:17px;font-weight:600;color:var(--t);margin-bottom:8px">{{ detail.name || knowledgeTitle(detail) }}</h2>
                <div style="font-size:13px;color:var(--t2);line-height:1.5;margin-bottom:8px;font-weight:500;white-space:pre-line;word-break:break-word">
                  {{ detail.description || t('no_description') }}
                </div>
                <div style="font-size:11px;color:var(--t3);line-height:1.6">
                  {{ detail.created_by || '' }} · {{ (detail.created_at || '').substring(0, 16) }}
                </div>
              </div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;padding-top:16px;border-top:1px solid var(--b);flex-wrap:wrap;justify-content:flex-end">
              <button v-if="detail.source === 'manual' && !detail.archived" class="bt bt-p bt-s" @click="editKnowledge">{{ t('edit_save') }}</button>
              <button v-if="detail.source === 'manual' && !detail.archived" class="bt bt-o bt-s" style="color:var(--r);border-color:var(--r)" @click="deleteKnowledge">{{ t('delete') }}</button>
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
              <div class="cd-h" style="display:flex;align-items:center;justify-content:flex-start;gap:10px;margin-bottom:14px">
                <h2 style="font-size:16px;font-weight:600">{{ t('ref_agent_title') }}</h2>
              </div>
              <div style="display:flex;flex-wrap:wrap;gap:8px">
                <div v-for="at in agentTypes.filter(a => a.value)" :key="at.value"
                  class="kb-agent-tag" :class="{ 'kb-agent-selected': isAgentSelected(at.value), 'kb-agent-pending': pendingAgentType === at.value }"
                  @click="toggleAgentRef(at.value)">
                  <div class="kb-agent-check"><span class="kb-check-yes">✓</span></div>
                  <div class="kb-agent-name">{{ at.label }}</div>
                  <div v-if="at.desc" class="kb-agent-desc">{{ at.desc }}</div>
                </div>
              </div>
            </div>

            <!-- 场景关联卡片 -->
            <div class="cd kb-detail-card" style="padding:24px;margin-bottom:20px;border-radius:18px">
              <div class="cd-h" style="display:flex;align-items:center;justify-content:flex-start;gap:10px;margin-bottom:14px">
                <h2 style="font-size:16px;font-weight:600">{{ t('ref_scene_title') }}</h2>
                <button class="kb-tag-add" style="font-size:10px" @click="openScenePopover()">+ {{ t('ref_add_scene') }}</button>
              </div>
              <div v-if="selectedSceneTags.length === 0" class="ts t2" style="padding:12px 0">{{ t('ref_no_scenes_linked') }}</div>
              <div v-else class="scn-scroll-wrap">
                <button class="scn-scroll-btn scn-scroll-prev" :class="{ show: sceneScrollLeft }" @click="scrollSceneTrack(-314)">◀</button>
                <transition-group name="kb-scene" tag="div" class="scn-scroll-track" ref="sceneTrack" @scroll="updateSceneScrollBtns">
                  <div v-for="tag in selectedSceneTags" :key="tag.sceneId" class="scn-card" :class="{ 'kb-scene-pending': pendingSceneId === tag.sceneId }" style="flex-shrink:0;width:300px;position:relative">
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
                </transition-group>
                <button class="scn-scroll-btn scn-scroll-next" :class="{ show: sceneScrollRight }" @click="scrollSceneTrack(314)">▶</button>
              </div>
            </div>

            <!-- 场景选择下拉面板（Teleport 在根层） -->
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
            <div style="margin-bottom:12px">
              <label style="font-size:11px;color:var(--t3);display:block;margin-bottom:4px">{{ t('entry_name') }}<span class="kb-required">*</span></label>
              <input v-model="editForm.name" :placeholder="t('entry_name_ph')" class="kb-input" style="width:100%" maxlength="50">
            </div>
            <div style="margin-bottom:12px">
              <label style="font-size:11px;color:var(--t3);display:block;margin-bottom:4px">{{ t('entry_description') }}</label>
              <input v-model="editForm.description" :placeholder="t('entry_description')" class="kb-input" style="width:100%" maxlength="200">
            </div>
            <div style="margin-bottom:12px">
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
                <label style="font-size:11px;color:var(--t3)">{{ t('entry_content') }}<span class="kb-required">*</span></label>
                <div style="display:flex;gap:6px">
                  <button v-if="editForm.content !== editFormSnapshot?.content" class="bt bt-o bt-s" style="cursor:pointer;font-size:11px" :title="t('reset_tip')" @click="resetEdit">{{ t('reset') }}</button>
                  <label class="bt bt-o bt-s" style="cursor:pointer;font-size:11px">
                    {{ t('import_file') }}
                    <input type="file" accept=".md,.json,.txt" style="display:none" @change="e => importFile(e, 'edit')">
                  </label>
                </div>
              </div>
              <textarea v-model="editForm.content" rows="20" class="kb-textarea"></textarea>
            </div>
            <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px;padding-top:16px;border-top:1px solid var(--b)">
              <button class="bt bt-o bt-s" @click="cancelEdit">{{ t('cancel') }}</button>
              <button class="bt bt-p bt-s" :class="{ 'kb-btn-loading': submitting }" :disabled="submitting" @click="saveEdit">{{ t('confirm') }}</button>
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
              <label style="font-size:11px;color:var(--t3);display:block;margin-bottom:4px">{{ t('entry_name') }}<span class="kb-required">*</span></label>
              <input v-model="createForm.name" :placeholder="t('entry_name_ph')" class="kb-input" style="width:100%" maxlength="50">
            </div>
            <div style="margin-bottom:12px">
              <label style="font-size:11px;color:var(--t3);display:block;margin-bottom:4px">{{ t('entry_description') }}</label>
              <input v-model="createForm.description" :placeholder="t('entry_description')" class="kb-input" style="width:100%" maxlength="200">
            </div>
            <div style="margin-bottom:16px">
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
                <label style="font-size:11px;color:var(--t3)">{{ t('entry_content') }}<span class="kb-required">*</span></label>
                <label class="bt bt-o bt-s" style="cursor:pointer;font-size:11px">
                  {{ t('import_file') }}
                  <input type="file" accept=".md,.json,.txt" style="display:none" @change="e => importFile(e, 'create')">
                </label>
              </div>
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
                <div class="kb-agent-check"><span class="kb-check-yes">✓</span></div>
                <div class="kb-agent-name">{{ at.label }}</div>
                <div v-if="at.desc" class="kb-agent-desc">{{ at.desc }}</div>
              </div>
            </div>
          </div>

          <!-- 场景关联卡片（scn-card + 弹出层选择，同详情页面） -->
          <div class="cd kb-detail-card" style="padding:24px;margin-bottom:20px;border-radius:18px">
            <div class="cd-h" style="display:flex;align-items:center;justify-content:flex-start;gap:10px;margin-bottom:14px">
              <h2 style="font-size:16px;font-weight:600">{{ t('ref_scene_title') }}</h2>
              <button class="kb-tag-add" style="font-size:10px" @click="openCreateScenePopover()">+ {{ t('ref_add_scene') }}</button>
            </div>
            <div v-if="createForm.sceneSelected.length === 0" class="ts t2" style="padding:12px 0">{{ t('ref_no_scenes_linked') }}</div>
            <div v-else class="scn-scroll-wrap">
              <transition-group name="kb-scene" tag="div" class="scn-scroll-track">
                <div v-for="sid in createForm.sceneSelected" :key="sid" class="scn-card" style="flex-shrink:0;width:300px;position:relative">
                  <button class="kb-scene-x" style="z-index:5" @click="toggleCreateScene(sid)">×</button>
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
            </div>
          </div>

          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="bt bt-o bt-s" @click="cancelCreate">{{ t('cancel') }}</button>
            <button class="bt bt-p bt-s" :class="{ 'kb-btn-loading': submitting }" :disabled="submitting" @click="saveCreate">{{ t('confirm') }}</button>
          </div>
        </template>

        <!-- 新增页面场景选择下拉面板（Teleport 在根层） -->
      </div>
      <!-- 详情页场景选择弹出层 -->
      <teleport to="body">
        <div v-if="addScenePopover" class="modal-overlay" @click.self="addScenePopover = false">
          <div class="modal kb-scene-modal">
            <div class="kb-scene-modal-title">{{ t('ref_select_scenes') }}</div>
            <div class="kb-scene-modal-search">
              <input v-model="sceneSearch" :placeholder="t('ref_search_scenes')" class="kb-input" style="flex:1;width:auto">
            </div>
            <div class="kb-scene-modal-list">
              <div v-if="availableScenes.length === 0" class="ts t2" style="padding:20px;text-align:center">{{ t('ref_no_available') }}</div>
              <div v-for="s in availableScenes" :key="s.id" class="kb-scene-row"
                   :class="{ 'kb-scene-row-selected': sceneSelected.includes(s.id) }"
                   @click="toggleSceneSelect(s.id)">
                <div class="kb-scene-row-info">
                  <div class="kb-scene-row-name">{{ s.name }}</div>
                  <div v-if="s.desc" class="kb-scene-row-desc">{{ s.desc }}</div>
                </div>
                <div class="kb-scene-row-check"><span class="kb-check-yes">✓</span></div>
              </div>
            </div>
            <div class="kb-scene-modal-footer">
              <span style="font-size:11px;color:var(--t3)">{{ t('ref_selected_count', { count: sceneSelected.length }) }}</span>
              <div style="display:flex;gap:8px">
                <button class="bt bt-o bt-s" @click="addScenePopover = false">{{ t('cancel') }}</button>
                <button class="bt bt-p bt-s" :class="{ 'kb-btn-loading': submitting }" :disabled="submitting || sceneSelected.length === 0" @click="confirmAddScenes">{{ t('confirm') }}</button>
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
              <input v-model="createSceneSearch" :placeholder="t('ref_search_scenes')" class="kb-input" style="flex:1;width:auto">
            </div>
            <div class="kb-scene-modal-list">
              <div v-if="availableCreateScenes.length === 0" class="ts t2" style="padding:20px;text-align:center">{{ t('ref_no_available') }}</div>
              <div v-for="s in availableCreateScenes" :key="s.id" class="kb-scene-row"
                   :class="{ 'kb-scene-row-selected': createSceneTempSelected.includes(s.id) }"
                   @click="toggleCreateSceneTemp(s.id)">
                <div class="kb-scene-row-info">
                  <div class="kb-scene-row-name">{{ s.name }}</div>
                  <div v-if="s.desc" class="kb-scene-row-desc">{{ s.desc }}</div>
                </div>
                <div class="kb-scene-row-check"><span class="kb-check-yes">✓</span></div>
              </div>
            </div>
            <div class="kb-scene-modal-footer">
              <span style="font-size:11px;color:var(--t3)">{{ t('ref_selected_count', { count: createSceneTempSelected.length }) }}</span>
              <div style="display:flex;gap:8px">
                <button class="bt bt-o bt-s" @click="createScenePopover = false">{{ t('cancel') }}</button>
                <button class="bt bt-p bt-s" @click="confirmCreateScenes">{{ t('confirm') }}</button>
              </div>
            </div>
          </div>
        </div>
      </teleport>
    `,
  });

  _knowledgeVueApp.mount(el);
}
