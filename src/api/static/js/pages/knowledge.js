// knowledge.js — 项目知识库页面（Issue #67 Vue 3 重写）
// 统一列表 + 统计卡片 + 筛选器 + CRUD + 关联管理

var _knowledgeVueApp = null;

function _currentRepo() {
  try {
    const c = JSON.parse(localStorage.getItem('currentRepo') || 'null');
    return c ? (c.fullName || '') : '';
  } catch { return ''; }
}

function _repoQS() {
  const repo = _currentRepo();
  return repo ? '?repo=' + encodeURIComponent(repo) : '';
}

function renderKnowledge() {
  const el = document.getElementById('pageContent');
  if (!el) return;

  // 卸载旧的 Vue 应用
  if (_knowledgeVueApp) {
    _knowledgeVueApp.unmount();
    _knowledgeVueApp = null;
  }

  const repo = _currentRepo();
  if (!repo) {
    el.innerHTML = '<div class="ts t2" style="padding:12px">' + I18N.t('knowledge.no_repo_selected') + '</div>';
    return;
  }

  _knowledgeVueApp = Vue.createApp({
    data() {
      return {
        list: [],
        loading: true,
        filter: { source: '', refStatus: '', query: '' },
        stats: { system: 0, agent: 0, manual: 0, unlinked: 0 },
        showArchived: false,
        editModal: null,
        refModal: null,
        entryModal: null,
        detailView: null,
      };
    },
    computed: {
      filteredList() {
        let result = this.list;
        if (!this.showArchived) {
          result = result.filter(k => !k.archived);
        }
        if (this.filter.source === 'system') {
          result = result.filter(k => k.source === 'system' || k.source === 'agent');
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
    },
    async mounted() {
      await this.loadList();
    },
    methods: {
      t(key, params) {
        return I18N.t('knowledge.' + key, params || {});
      },
      async loadList() {
        this.loading = true;
        try {
          const d = await API.get('/knowledge' + _repoQS());
          if (window._currentView !== 'knowledge') return;
          this.list = d.knowledge || [];
          this.stats = d.stats || { system: 0, agent: 0, manual: 0, unlinked: 0 };
        } catch (e) {
          if (e.name === 'AbortError' && e.silent) return;
          if (window._currentView !== 'knowledge') return;
        } finally {
          if (window._currentView === 'knowledge') this.loading = false;
        }
      },
      filterByStats(type) {
        if (type === 'system') this.filter.source = 'system';
        else if (type === 'user') this.filter.source = 'manual';
        else if (type === 'unlinked') this.filter.refStatus = 'unlinked';
      },
      sourceLabel(s) {
        if (s === 'system') return this.t('source_system');
        if (s === 'agent') return this.t('source_agent');
        return this.t('source_manual');
      },
      sourceClass(s) {
        return 'source-' + (s || 'agent');
      },
      statSystem() { return this.stats.system + this.stats.agent; },
      statUser() { return this.stats.manual; },
      statUnlinked() { return this.stats.unlinked; },

      // 查看全文
      async viewContent(k) {
        try {
          const key = k.obs_key || k.file_path || k.path;
          const d = await API.get('/knowledge/content?repo=' + encodeURIComponent(_currentRepo()) + '&obs_key=' + encodeURIComponent(key));
          if (window._currentView !== 'knowledge') return;
          this.detailView = d;
        } catch (e) {
          if (e.name === 'AbortError' && e.silent) return;
          app.showToast(e.message || I18N.t('common.load_failed'));
        }
      },

      // 编辑
      editKnowledge(k) {
        this.editModal = { id: k.id, content: k.content || '', loading: false };
        // 加载全文
        API.get('/knowledge/' + k.id).then(d => {
          if (this.editModal && this.editModal.id === k.id) {
            this.editModal.content = d.content || '';
          }
        }).catch(() => {});
      },
      async saveEdit() {
        if (!this.editModal) return;
        this.editModal.loading = true;
        try {
          await API.put('/knowledge/' + this.editModal.id, { content: this.editModal.content });
          app.showToast(this.t('knowledge_updated'));
          this.editModal = null;
          await this.loadList();
        } catch (e) {
          app.showToast(e.message || I18N.t('common.load_failed'));
        } finally {
          if (this.editModal) this.editModal.loading = false;
        }
      },

      // 删除
      async deleteKnowledge(k) {
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

      // 归档/取消归档
      async toggleArchive(k) {
        try {
          await API.put('/knowledge/' + k.id + '/archive', { archived: !k.archived });
          app.showToast(k.archived ? this.t('unarchived') : this.t('archived'));
          await this.loadList();
        } catch (e) {
          app.showToast(e.message || I18N.t('common.load_failed'));
        }
      },

      // 关联设置
      async openRefModal(k) {
        try {
          const d = await API.get('/knowledge/' + k.id + '/refs');
          this.refModal = { knowledge: k, refs: d.refs || [], newScene: '', newAgent: '' };
        } catch (e) {
          app.showToast(e.message);
        }
      },
      async addRef() {
        const sceneId = this.refModal.newScene || '';
        const agentType = this.refModal.newAgent || '';
        try {
          await API.post('/knowledge/' + this.refModal.knowledge.id + '/refs', {
            scene_id: sceneId, agent_type: agentType
          });
          const d = await API.get('/knowledge/' + this.refModal.knowledge.id + '/refs');
          this.refModal.refs = d.refs || [];
          this.refModal.newScene = '';
          this.refModal.newAgent = '';
          app.showToast(this.t('ref_add'));
        } catch (e) {
          if (e.detail && e.detail.message && e.detail.message.includes('已绑定')) {
            app.showToast(this.t('ref_already_bound'));
          } else if (e.detail && e.detail.message && e.detail.message.includes('20')) {
            app.showToast(e.detail.message);
          } else {
            app.showToast(e.message || I18N.t('common.load_failed'));
          }
        }
      },
      async toggleRef(ref) {
        try {
          await API.put('/knowledge/ref/' + ref.id, { enabled: !ref.enabled });
          ref.enabled = !ref.enabled;
        } catch (e) {
          app.showToast(e.message);
        }
      },
      deleteRef(ref) {
        const self = this;
        const affected = this.refModal.refs.filter(r => r.enabled && r.id !== ref.id).length;
        const msg = this.t('ref_delete_confirm') + (affected > 0 ? '（会影响 ' + affected + ' 个场景）' : '');
        _showAppleConfirm(msg, async function() {
          try {
            await API.del('/knowledge/ref/' + ref.id);
            self.refModal.refs = self.refModal.refs.filter(r => r.id !== ref.id);
            app.showToast(self.t('ref_delete'));
          } catch (e) {
            app.showToast(e.message);
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
            repo: _currentRepo(),
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
    },
    template: `
      <div class="knowledge-page">
        <div v-if="loading" class="loading">{{ t('loading') }}</div>
        <template v-else>
          <div class="stats-bar">
            <div class="stat-card" @click="filterByStats('system')">
              <span class="stat-num">{{ statSystem() }}</span>
              <span class="stat-label">{{ t('stats_system') }}</span>
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
            <select v-model="filter.source" style="padding:7px 10px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;background:var(--c)">
              <option value="">{{ t('filter_source_all') }}</option>
              <option value="system">{{ t('source_system') }}</option>
              <option value="manual">{{ t('source_manual') }}</option>
            </select>
            <select v-model="filter.refStatus" style="padding:7px 10px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;background:var(--c)">
              <option value="">{{ t('filter_ref_all') }}</option>
              <option value="linked">{{ t('filter_ref_linked') }}</option>
              <option value="unlinked">{{ t('filter_ref_unlinked') }}</option>
            </select>
            <input v-model="filter.query" :placeholder="t('search_placeholder')"
              style="padding:7px 14px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;outline:none;background:var(--c);width:200px">
            <label style="font-size:11px;color:var(--t3);display:flex;align-items:center;gap:4px;cursor:pointer">
              <input type="checkbox" v-model="showArchived"> {{ t('show_archived') }}
            </label>
            <button @click="openEntryModal" style="padding:7px 14px;border:1px solid var(--a);border-radius:var(--rd);font-size:12px;cursor:pointer;color:var(--a);margin-left:auto">{{ t('entry_create') }}</button>
          </div>

          <div v-if="filteredList.length === 0" class="ts t2" style="padding:12px">{{ t('empty') }}</div>
          <div v-else style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px">
            <div v-for="k in filteredList" :key="k.id || k.file_path" class="cd skill-card" style="padding:14px" :class="{ 'archived-card': k.archived }">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap">
                <span class="source-tag" :class="sourceClass(k.source)">{{ sourceLabel(k.source) }}</span>
                <span v-if="k.ref_summary && k.ref_summary !== '未关联'" class="ref-tag">{{ k.ref_summary }}</span>
                <span v-else class="ref-tag unlinked">{{ t('ref_unlinked') }}</span>
                <span v-if="k.issue_tag" class="issue-tag">{{ k.issue_tag }}</span>
                <span v-if="k.archived" class="archived-tag">{{ t('archived') }}</span>
              </div>
              <div v-if="isFileTooLarge(k)" class="size-warning">⚠ {{ t('file_too_large', { size: fileSizeKB(k) }) }}</div>
              <div style="font-size:11px;color:var(--t2);line-height:1.4;margin-bottom:6px">{{ (k.content || '').substring(0, 200) }}</div>
              <div style="font-size:10px;color:var(--t3);margin-bottom:8px">{{ k.created_by || '' }} · {{ (k.created_at || '').substring(0, 16) }}</div>
              <div style="display:flex;gap:6px;flex-wrap:wrap">
                <button class="bt bt-o bt-s" @click="viewContent(k)">{{ t('view') }}</button>
                <button v-if="k.source === 'manual' && !k.archived" class="bt bt-o bt-s" @click="editKnowledge(k)">{{ t('edit_save') }}</button>
                <button v-if="k.source === 'manual' && !k.archived" class="bt bt-o bt-s" style="color:var(--r);border-color:var(--r)" @click="deleteKnowledge(k)">{{ t('delete') }}</button>
                <button v-if="!k.archived" class="bt bt-o bt-s" @click="openRefModal(k)">{{ t('ref_settings') }}</button>
                <button v-if="k.source !== 'manual' && !k.archived" class="bt bt-o bt-s" @click="toggleArchive(k)">{{ t('archive') }}</button>
                <button v-if="k.archived" class="bt bt-o bt-s" @click="toggleArchive(k)">{{ t('unarchive') }}</button>
              </div>
            </div>
          </div>
        </template>

        <div v-if="editModal" class="modal-overlay" @click.self="editModal = null">
          <div class="modal cd">
            <h3 style="font-size:14px;font-weight:600;margin-bottom:14px">{{ t('edit_title') }}</h3>
            <textarea v-model="editModal.content" rows="12" style="width:100%;min-height:200px;padding:12px;border:1px solid var(--b);border-radius:8px;font-size:13px;font-family:monospace;resize:vertical"></textarea>
            <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
              <button class="bt bt-o bt-s" @click="editModal = null">{{ t('cancel') }}</button>
              <button class="bt bt-p bt-s" :disabled="editModal.loading" @click="saveEdit">{{ t('edit_save') }}</button>
            </div>
          </div>
        </div>

        <div v-if="refModal" class="modal-overlay" @click.self="refModal = null">
          <div class="modal cd" style="width:520px">
            <h3 style="font-size:14px;font-weight:600;margin-bottom:14px">{{ t('ref_modal_title') }}</h3>
            <div style="display:flex;gap:8px;margin-bottom:14px">
              <input v-model="refModal.newScene" :placeholder="t('ref_scene')" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px">
              <select v-model="refModal.newAgent" style="padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c)">
                <option value="">{{ t('ref_agent_all') }}</option>
                <option value="vod">VOD</option>
                <option value="coding">Coding</option>
                <option value="qa">QA</option>
                <option value="sre">SRE</option>
                <option value="chat">Chat</option>
              </select>
              <button class="bt bt-p bt-s" @click="addRef">{{ t('ref_add') }}</button>
            </div>
            <div v-if="refModal.refs.length === 0" class="ts t2" style="padding:8px">{{ t('ref_unlinked') }}</div>
            <div v-else style="max-height:300px;overflow-y:auto">
              <div v-for="ref in refModal.refs" :key="ref.id" style="display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--bg)">
                <span style="font-size:12px;flex:1">{{ ref.scene_id || t('ref_scene_all') }} · {{ ref.agent_type || t('ref_agent_all') }}</span>
                <label style="cursor:pointer">
                  <input type="checkbox" :checked="ref.enabled" @change="toggleRef(ref)">
                </label>
                <button class="bt bt-o bt-s" style="color:var(--r);border-color:var(--r)" @click="deleteRef(ref)">{{ t('ref_delete') }}</button>
              </div>
            </div>
            <div style="display:flex;justify-content:flex-end;margin-top:12px">
              <button class="bt bt-o bt-s" @click="refModal = null">{{ t('cancel') }}</button>
            </div>
          </div>
        </div>

        <div v-if="entryModal" class="modal-overlay" @click.self="entryModal = null">
          <div class="modal cd">
            <h3 style="font-size:14px;font-weight:600;margin-bottom:14px">{{ t('entry_title') }}</h3>
            <textarea v-model="entryModal.content" rows="10" :placeholder="t('entry_content')" style="width:100%;min-height:180px;padding:12px;border:1px solid var(--b);border-radius:8px;font-size:13px;font-family:monospace;resize:vertical"></textarea>
            <select v-model="entryModal.agentType" style="width:100%;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c);margin-top:8px">
              <option value="">{{ t('ref_agent_all') }}</option>
              <option value="vod">VOD</option>
              <option value="coding">Coding</option>
              <option value="qa">QA</option>
              <option value="sre">SRE</option>
              <option value="chat">Chat</option>
            </select>
            <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
              <button class="bt bt-o bt-s" @click="entryModal = null">{{ t('cancel') }}</button>
              <button class="bt bt-p bt-s" :disabled="entryModal.loading" @click="createEntry">{{ t('entry_create') }}</button>
            </div>
          </div>
        </div>

        <div v-if="detailView" class="modal-overlay" @click.self="detailView = null">
          <div class="modal cd" style="width:700px;max-width:90vw;max-height:80vh;overflow-y:auto">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
              <h3 style="font-size:14px;font-weight:600">{{ detailView.file || detailView.obs_key || '' }}</h3>
              <button class="bt bt-o bt-s" @click="detailView = null">{{ t('cancel') }}</button>
            </div>
            <div v-html="renderMarkdown(detailView.content || '')" style="font-size:13px;line-height:1.6"></div>
          </div>
        </div>
      </div>
    `,
  });

  _knowledgeVueApp.mount(el);
}

function renderMarkdown(content) {
  if (typeof marked !== 'undefined' && marked.parse) {
    return marked.parse(content);
  }
  return '<pre>' + content.replace(/</g, '&lt;') + '</pre>';
}

// 保留旧函数兼容（loadKnowledgeContent 可能被其他地方调用）
function loadKnowledgeContent(key) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  // 如果 Vue 应用还在，用 detailView 模式
  if (_knowledgeVueApp && _knowledgeVueApp._instance) {
    const vm = _knowledgeVueApp._instance.proxy;
    API.get('/knowledge/content?repo=' + encodeURIComponent(_currentRepo()) + '&obs_key=' + encodeURIComponent(key)).then(d => {
      vm.detailView = d;
    }).catch(e => {
      if (e.name === 'AbortError' && e.silent) return;
      app.showToast(e.message || I18N.t('common.load_failed'));
    });
    return;
  }
  el.innerHTML = '<div class="loading">' + I18N.t('knowledge.loading') + '</div>';
  API.get('/knowledge/content?repo=' + encodeURIComponent(_currentRepo()) + '&obs_key=' + encodeURIComponent(key)).then(d => {
    if (window._currentView !== 'knowledge') return;
    const fileLabel = d.file || d.obs_key || '';
    const isJson = fileLabel.toLowerCase().endsWith('.json');
    const content = d.content || '';
    const rendered = isJson
      ? '<pre style="background:var(--bg);padding:14px;border-radius:var(--rd);font-size:11px;font-family:monospace;line-height:1.6;overflow-x:auto;white-space:pre-wrap">' + content.replace(/</g, '&lt;') + '</pre>'
      : renderMarkdown(content);
    el.innerHTML = '<button class="bt bt-o bt-s" onclick="app.nav(\'knowledge\')" style="margin-bottom:12px">' + I18N.t('knowledge.back_to_knowledge') + '</button><div class="cd" style="padding:16px 20px"><h3 style="font-size:15px;font-weight:600;margin-bottom:12px">📄 ' + fileLabel + '</h3>' + (rendered || '<div class="ts t2">' + I18N.t('knowledge.empty') + '</div>') + '</div>';
  }).catch(e => {
    if (e.name === 'AbortError' && e.silent) return;
    if (window._currentView !== 'knowledge') return;
    el.innerHTML = '<div class="error">' + I18N.t('knowledge.load_detail_failed', { msg: e.message }) + '</div>';
  });
}
