<template>
  <div class="pm">
    <!-- ═══ 插件市场主页（列表常驻；详情走右侧抽屉，不整页替换）═══ -->
    <!-- scope 子页签（official / repo / user） -->
    <div class="pm__subtab">
      <div class="seg-bar pm__seg">
        <a :class="{ ac: tab === 'official' }" @click="switchTab('official')">{{ tp('scope_official') }}</a>
        <a :class="{ ac: tab === 'repo' }" @click="switchTab('repo')">{{ tp('scope_repo') }}</a>
        <a :class="{ ac: tab === 'user' }" @click="switchTab('user')">{{ tp('scope_user') }}</a>
      </div>
    </div>

    <!-- 全部插件 -->
    <div class="apple-card">
      <div class="cd-h apple-search pm__search">
        <h2>
          {{ tp('all_plugins') }}
          <span class="pm__count">{{ total }}</span>
        </h2>
        <div class="pm__tools">
          <button
            :class="['bt bt-s', sort === 'latest' ? 'bt-tg' : 'bt-o']"
            @click="setSort('latest')"
          >
            {{ tp('sort_latest') }}
          </button>
          <button
            :class="['bt bt-s', sort === 'hot' ? 'bt-tg' : 'bt-o']"
            @click="setSort('hot')"
          >
            {{ tp('sort_hot') }}
          </button>
          <input
            v-model="qDraft"
            class="pm__q"
            :placeholder="tp('search_placeholder')"
            @input="onSearchInput"
            @keyup.enter="doSearch"
          />
          <button class="bt bt-o bt-s" @click="doSearch">{{ tp('find_plugin') }}</button>
          <button class="bt bt-o bt-s" @click="resetFilters">{{ tp('reset_filters') }}</button>
          <button
            v-if="tab !== 'official'"
            class="bt bt-p bt-s pm__upload-btn"
            @click="openUpload"
          >
            {{ tp('upload_plugin') }}
          </button>
        </div>
      </div>

      <!-- repo 无当前仓库提示 -->
      <div v-if="tab === 'repo' && !currentRepo" class="ts t2 pm__norepo">
        {{ tp('no_repo_selected') }}
      </div>

      <!-- 左右分栏：分类筛选 + 右侧内容 -->
      <div v-else class="pm__cols">
        <div class="pm__main">
          <div class="pm__tags">
            <a
              :class="['skill-tag-filter', { ac: activeTagId === '' }]"
              href="javascript:void(0)"
              :style="tagStyle(activeTagId === '')"
              @click="switchTag('')"
              >{{ tp('all_categories') }}</a
            >
            <a
              v-for="c in categories"
              :key="c.id"
              :class="['skill-tag-filter', { ac: activeTagId === String(c.id) }]"
              href="javascript:void(0)"
              :style="tagStyle(activeTagId === String(c.id))"
              @click="switchTag(String(c.id))"
            >
              <span v-if="c.icon" class="pm__tag-ic">{{ c.icon }}</span
              >{{ c.name }}
            </a>
          </div>
          <div class="pm__gridwrap">
            <div v-if="listLoading" class="scn-loading"><span class="scn-spinner"></span></div>
            <!-- 搜索结果分支（/plugins/search 全局命中，不 paginate；行内展示命中原因） -->
            <div v-else-if="searchMode" class="rgrid">
              <div v-for="p in searchHits" :key="p.id" class="rc" @click="openDetail(p.id)">
                <div class="rh">
                  <span style="font-size: 14px">🧩</span>
                  <span class="rn" :title="fullName(p)">{{ fullName(p) }}</span>
                </div>
                <div class="rd" style="margin-bottom: 8px" :title="p.description || ''">
                  {{ p.description || '' }}
                </div>
                <div v-if="p.match_reason" class="ro pm__matchreason"
                  >🎯 {{ tp('search_match') }}：{{ p.match_reason }}</div
                >
                <div class="ro">v{{ p.version || '—' }} · {{ p.author || tp('author_unknown') }}</div>
                <div class="rf">
                  <button class="bt bt-o bt-s" @click.stop="openDetail(p.id)">
                    {{ tp('view') }}
                  </button>
                </div>
              </div>
            </div>
            <div v-else-if="!plugins.length" class="ts t3 pm__empty">
              {{ tp('no_plugins') }}
            </div>
            <div v-else class="rgrid">
              <div
                v-for="p in plugins"
                :key="p.id"
                class="rc"
                @click="openDetail(p.id)"
              >
                <div class="rh">
                  <span style="font-size: 14px">🧩</span>
                  <span :class="['tg', scopeTagClass(p)]" style="font-size: 9px">{{
                    scopeLabel(p.scope)
                  }}</span>
                  <span v-if="p.category_name" class="tg pm__cat-tag">{{
                    p.category_name
                  }}</span>
                  <span v-if="p.certified" class="tg pm__cert-tag" title="official">✓</span>
                  <span class="rn" :title="fullName(p)">{{ fullName(p) }}</span>
                </div>
                <div class="rd" style="margin-bottom: 8px" :title="p.description || ''">
                  {{ p.description || '' }}
                </div>
                <div class="ro">
                  v{{ p.version || '—' }} · {{ p.author || tp('author_unknown')
                  }}<template v-if="p.updated_at"> · {{ p.updated_at }}</template>
                </div>
                <div v-if="p.usage_count > 0" class="ro">
                  {{ p.usage_count }} {{ tp('scenes_in_use') }}
                </div>
                <div class="rf">
                  <span class="s pm__like" @click.stop="onLike(p)">
                    {{ p.liked_by_me ? '❤️' : '🤍' }} {{ p.like_count || 0 }}
                  </span>
                  <button class="bt bt-o bt-s" @click.stop="openDetail(p.id)">
                    {{ tp('view') }}
                  </button>
                </div>
              </div>
            </div>
            <!-- 分页器（搜索态不 paginate，隐藏） -->
            <div v-if="!searchMode" class="pm__pager">
              <button class="bt bt-o bt-s" :disabled="page <= 1" @click="goPage(page - 1)">
                ‹
              </button>
              <span class="pm__pginfo"
                >{{ page }} / {{ totalPages }} ({{ total
                }}{{ tp('items_count_suffix') }})</span
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
                class="pm__pgsize"
                @change="setPageSize($event.target.value)"
              >
                <option value="20">20{{ tp('per_page_suffix') }}</option>
                <option value="50">50{{ tp('per_page_suffix') }}</option>
                <option value="100">100{{ tp('per_page_suffix') }}</option>
              </select>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══ 插件详情抽屉（右侧 overlay；就地概览/凭据/参数配置）═══ -->
    <Teleport to="body">
      <Transition name="pm-drawer">
        <div v-if="detailOpen" class="pm__drawer-mask" @click.self="closeDetail">
          <div class="pm__drawer" role="dialog" aria-modal="true">
            <button class="pm__drawer-close" :title="td('close')" @click="closeDetail">✕</button>
            <div v-if="detailLoading" class="pm__loading">{{ tp('loading') }}</div>
            <div v-else-if="!detail" class="pm__loading">—</div>
            <div v-else class="det">
        <div class="det-head">
          <span class="det-ico">🧩</span>
          <h1 class="det-name">{{ detail.display_name || detail.name }}</h1>
          <span :class="['tg', scopeTagClass(detail)]" style="font-size: 10px">{{
            scopeLabel(detail.scope)
          }}</span>
          <span v-if="detail.certified" class="tg pm__cert-tag" :title="tp('certified_short')">{{
            tp('certified_short')
          }}</span>
          <div class="det-actions">
            <span
              :class="['det-like', { liked: detail.liked_by_me }]"
              title="like"
              @click="onLike(detail)"
            >
              {{ detail.liked_by_me ? '❤️' : '🤍' }} {{ detail.like_count || 0 }}
            </span>
            <button
              v-if="detail.zip_url || detail.is_obs"
              class="bt bt-o bt-s"
              @click="downloadPlugin(detail)"
            >
              {{ tp('download') }}
            </button>
            <button
              v-if="!installedNames.length || !installedNames.includes(detail.name)"
              class="bt bt-p bt-s"
              :disabled="installing"
              @click="doInstall(detail)"
            >
              {{ installing ? tp('installing_short') : tp('install') }}
            </button>
            <button
              v-else
              class="bt bt-o bt-s pm__del-btn"
              :disabled="uninstalling"
              @click="doUninstall(detail)"
            >
              {{ tp('uninstall') }}
            </button>
          </div>
        </div>
        <p class="det-sum">{{ detail.description || '' }}</p>
        <div class="info-grid">
          <div class="info-cell">
            <div class="il">{{ td('author') }}</div>
            <div class="iv">{{ detail.author || '—' }}</div>
          </div>
          <div class="info-cell">
            <div class="il">{{ td('version') }}</div>
            <div class="iv">v{{ detail.version || '—' }}</div>
          </div>
          <div class="info-cell">
            <div class="il">{{ td('category') }}</div>
            <div class="iv">{{ detail.category_name || '—' }}</div>
          </div>
          <div class="info-cell">
            <div class="il">{{ td('namespace') }}</div>
            <div class="iv">{{ detail.namespace_owner || '—' }}</div>
          </div>
          <div class="info-cell">
            <div class="il">{{ td('updated') }}</div>
            <div class="iv">{{ detail.updated_at || '—' }}</div>
          </div>
          <div class="info-cell">
            <div class="il">{{ td('fingerprint') }}</div>
            <div class="iv pm__mono" :title="detail.content_hash || ''">{{
              shortHash(detail.content_hash)
            }}</div>
          </div>
        </div>
        <div class="fn-card">
          <div class="fn-label">{{ tp('manifest') }}</div>
          <pre class="pm__manifest">{{ prettyManifest }}</pre>
        </div>

        <!-- ═══ 凭据配置段（就地新增/修改：声明键 + ➕ 自由增行）═══ -->
        <div class="fn-card pm__cfg-card">
          <div class="fn-label">
            {{ tp('cred_title') }}
            <span class="pm__cfg-sub">{{ tp('cred_hint') }}</span>
          </div>
          <div v-if="credState.loading" class="pm__cfg-loading">{{ tp('loading') }}</div>
          <template v-else>
            <div v-for="f in credState.declared" :key="f.key" class="pm__credrow">
              <label class="pm__credkey" :title="f.key"
                >{{ f.key }}<b v-if="f.required" class="pm__credreq">*</b>
                <span v-if="f.label && f.label !== f.key" class="pm__credinj">{{ f.label }}</span>
              </label>
              <div class="pm__credval">
                <input
                  v-model="f.value"
                  :type="f.show ? 'text' : 'password'"
                  class="pm__credinput"
                  autocomplete="new-password"
                  :placeholder="isCredConfigured(f.key) ? tp('cred_placeholder_configured') : tp('cred_placeholder')"
                  @input="f.dirty = true"
                />
                <button
                  class="pm__credeye"
                  :title="f.show ? tp('cred_hide') : tp('cred_show')"
                  @click="toggleCredEye(f)"
                >
                  {{ f.show ? '🙈' : '👁' }}
                </button>
                <span v-if="isCredConfigured(f.key)" class="pm__credok">✓ {{ tp('cred_configured') }}</span>
              </div>
            </div>
            <div
              v-if="!credState.declared.length && !credState.savedFreeRows.length"
              class="pm__credempty"
            >
              {{ tp('cred_none_declared') }}
            </div>
            <!-- 复审返工：已配置但未在 manifest 声明的自由行——保存后仍在同屏，可改可删 -->
            <div
              v-for="r in credState.savedFreeRows"
              :key="'sf' + r.key"
              class="pm__credrow pm__credrow-saved"
            >
              <label class="pm__credkey" :title="r.key"
                >{{ r.key }}
                <span class="pm__credok">✓ {{ tp('cred_configured') }}</span>
              </label>
              <div class="pm__credval">
                <input
                  v-model="r.value"
                  :type="r.show ? 'text' : 'password'"
                  class="pm__credinput"
                  autocomplete="new-password"
                  :placeholder="tp('cred_placeholder_configured')"
                  @input="r.dirty = true"
                />
                <button
                  class="pm__credeye"
                  :title="r.show ? tp('cred_hide') : tp('cred_show')"
                  @click="r.show = !r.show"
                >
                  {{ r.show ? '🙈' : '👁' }}
                </button>
                <button class="pm__creddel" :title="tp('cred_free_remove')" @click="removeSavedCredRow(r.key)">✕</button>
              </div>
            </div>
            <!-- ➕ 自由增行（provider=plugin:{name} 泛化凭据库） -->
            <div v-for="(r, ri) in credState.freeRows" :key="'fr' + ri" class="pm__credrow">
              <input
                v-model="r.key"
                class="pm__credinput pm__credkeyin"
                :placeholder="tp('cred_free_key_ph')"
              />
              <div class="pm__credval">
                <input
                  v-model="r.value"
                  :type="r.show ? 'text' : 'password'"
                  class="pm__credinput"
                  autocomplete="new-password"
                  :placeholder="tp('cred_placeholder')"
                />
                <button
                  class="pm__credeye"
                  :title="r.show ? tp('cred_hide') : tp('cred_show')"
                  @click="r.show = !r.show"
                >
                  {{ r.show ? '🙈' : '👁' }}
                </button>
                <button class="pm__creddel" :title="tp('cred_free_remove')" @click="removeCredRow(ri)">✕</button>
              </div>
            </div>
            <button class="bt bt-o bt-s pm__credadd" @click="addCredRow">➕ {{ tp('cred_free_add') }}</button>
            <div class="pm__cfgnote">{{ tp('cred_free_pending') }}</div>
            <div class="pm__cfg-actions">
              <button class="bt bt-p bt-s" :disabled="credState.saving" @click="saveCreds">
                {{ credState.saving ? tp('cred_saving') : tp('cred_save') }}
              </button>
            </div>
          </template>
        </div>

        <!-- ═══ 参数配置段（A4 personal overlay：GET/PUT /api/plugins/{name}/config）═══ -->
        <div class="fn-card pm__cfg-card">
          <div class="fn-label">
            {{ tp('param_title') }}
            <span class="pm__cfg-sub">{{ tp('param_hint') }}</span>
          </div>
          <div v-if="paramState.loading" class="pm__cfg-loading">{{ tp('loading') }}</div>
          <div v-else-if="!paramState.rows.length" class="pm__credempty">{{ tp('param_none') }}</div>
          <table v-else class="pm__ptable">
            <thead>
              <tr>
                <th>{{ tp('param_col_key') }}</th>
                <th>{{ tp('param_col_type') }}</th>
                <th>{{ tp('param_col_desc') }}</th>
                <th>{{ tp('param_col_value') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in paramState.rows" :key="r.pk">
                <td class="pm__pkey"
                  >{{ r.pk }}<b v-if="r.required" class="pm__credreq">*</b></td
                >
                <td>{{ r.type }}</td>
                <td class="pm__pdesc">{{ r.description }}</td>
                <td class="pm__pval">
                  <input
                    v-if="r.type === 'boolean'"
                    type="checkbox"
                    class="pm__pchk"
                    :checked="r.value === 'true' || r.value === true"
                    @change="onParamBool(r, $event)"
                  />
                  <select
                    v-else-if="r.enumOpts && r.enumOpts.length"
                    v-model="r.value"
                    class="pm__psel"
                    @change="r.dirty = true"
                  >
                    <option v-for="o in r.enumOpts" :key="String(o)" :value="String(o)">{{ o }}</option>
                  </select>
                  <input
                    v-else
                    v-model="r.value"
                    class="pm__credinput"
                    :type="r.type === 'number' ? 'number' : 'text'"
                    @input="r.dirty = true"
                  />
                  <span v-if="r.personal" class="pm__psrc" :title="tp('param_src_personal')">{{
                    tp('param_src_personal_tag')
                  }}</span>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-if="paramState.rows.length" class="pm__cfg-actions">
            <button class="bt bt-p bt-s" :disabled="paramState.saving" @click="saveParams">
              {{ paramState.saving ? tp('param_saving') : tp('param_save') }}
            </button>
          </div>
        </div>

        <!-- 版本族 -->
        <div class="apple-card">
          <div class="cd-h">
            <h2>
              {{ tp('versions') }}
              <span class="pm__count">{{ versions.length }}</span>
            </h2>
          </div>
          <div v-if="!versions.length" class="ts t3 pm__empty">
            {{ tp('versions_empty') }}
          </div>
          <div v-else class="tw">
            <table>
              <thead>
                <tr>
                  <th>{{ td('version') }}</th>
                  <th>{{ td('status') }}</th>
                  <th>{{ td('updated') }}</th>
                  <th>{{ td('action') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="v in versions" :key="v.id">
                  <td>v{{ v.version }}</td>
                  <td>
                    <span v-if="v.effective" class="tg tg-c">{{ td('effective') }}</span>
                    <span v-else-if="v.status === 'revoked'" class="tg tg-i">{{
                      td('revoked')
                    }}</span>
                    <span v-else class="tg tg-d">{{ td('history') }}</span>
                  </td>
                  <td>{{ v.updated_at || '—' }}</td>
                  <td>
                    <button
                      v-if="!v.effective && v.status !== 'revoked'"
                      class="bt bt-o bt-s"
                      @click="doRollback(v)"
                    >
                      {{ tp('rollback') }}
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- ═══ 上传插件弹窗（L0 门禁先行 + 结构化错误清单）═══ -->
    <AGModal :open="showUpload" :title="tp('upload.title')" @close="showUpload = false">
      <div class="pm__form">
        <div class="srow pm__srow">
          <label>{{ tp('upload.scope_label') }}</label>
          <AGSelect
            v-model="uploadForm.scope"
            :options="[
              { value: 'user', label: tp('scope_user') },
              { value: 'repo', label: tp('scope_repo') },
            ]"
            class="pm__f1"
          />
        </div>
        <div v-if="uploadForm.scope === 'repo'" class="srow pm__srow">
          <label>{{ tp('upload.repo_label') }}</label>
          <span>{{ currentRepo || tp('no_repo_selected') }}</span>
        </div>
        <div class="srow pm__srow">
          <label>{{ tp('upload.category_label') }}</label>
          <AGSelect v-model="uploadForm.categoryId" :options="categoryOpts" class="pm__f1" />
        </div>
        <div class="pm__file-fld">
          <label>{{ tp('upload.file_label') }}</label>
          <input
            type="file"
            accept=".zip"
            class="pm__file"
            @change="uploadForm.file = $event.target.files[0] || null"
          />
        </div>
        <div class="srow pm__srow">
          <label>{{ tp('upload.source_repo_label') }}</label>
          <input v-model="uploadForm.sourceRepo" class="pm__srcinput" />
        </div>
        <div class="srow pm__srow">
          <label>{{ tp('upload.source_ref_label') }}</label>
          <input v-model="uploadForm.sourceRef" class="pm__srcinput" />
        </div>
        <div class="srow pm__srow">
          <label>{{ tp('upload.source_branch_label') }}</label>
          <input v-model="uploadForm.sourceBranch" class="pm__srcinput" />
        </div>
        <div v-if="uploadPct > 0 && !uploadDone" class="pm__upload-prog">
          <div class="pm__upload-txt">{{ tp('upload.uploading') }} {{ uploadPct }}%</div>
          <div class="pm__upload-bar"><div :style="{ width: uploadPct + '%' }"></div></div>
        </div>
        <!-- L0 门禁结构化错误清单：check/code/path/reason/hint 逐条展示，不合并成一句报错 -->
        <div v-if="uploadErrors.length" class="pm__l0err">
          <div class="pm__l0err-title">{{ tp('upload.l0_errors_title', { count: uploadErrors.length }) }}</div>
          <div class="pm__l0err-list">
            <div v-for="(err, i) in uploadErrors" :key="i" class="pm__l0err-item">
              <div class="pm__l0err-row">
                <span class="tg tg-i pm__l0check">{{ err.check || '—' }}</span>
                <span class="pm__l0code">{{ err.code || '—' }}</span>
                <span class="pm__l0path">{{ err.path || '(root)' }}</span>
              </div>
              <!-- 坏 zip 等后端分支仅 {code,message} 时降级展示 message（不白屏） -->
              <div class="pm__l0reason">{{ err.reason || err.message || '' }}</div>
              <div v-if="err.hint" class="pm__l0hint">💡 {{ err.hint }}</div>
            </div>
          </div>
        </div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="showUpload = false">{{
          tp('cancel')
        }}</AGButton>
        <AGButton size="sm" :disabled="uploading || validating" @click="doUpload">
          {{ validating ? tp('upload.validating') : uploading ? tp('upload.uploading') : tp('upload.submit') }}
        </AGButton>
      </template>
    </AGModal>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import AGModal from '@/components/ui/AGModal.vue'
import AGButton from '@/components/ui/AGButton.vue'
import AGSelect from '@/components/ui/AGSelect.vue'

const G = globalThis

// ── I18N（plugins.* 命名空间；对齐 skills 视图模式：缺失键回退内置文案）──
function tp(key, params) {
  const full = 'plugins.' + key
  const v = G.I18N.t(full, params)
  return v && v !== full ? v : KEY_DEFAULTS[key] || key
}
const KEY_DEFAULTS = {
  scope_official: '官方插件',
  scope_repo: '仓库插件',
  scope_user: '个人插件',
  all_plugins: '全部插件',
  sort_latest: '最新',
  sort_hot: '最热',
  search_placeholder: '搜索插件名称 / 描述 / 作者',
  find_plugin: '查找插件',
  reset_filters: '重置',
  upload_plugin: '上传插件',
  no_repo_selected: '请先选择仓库',
  no_plugins: '暂无插件',
  all_categories: '全部分类',
  view: '详情',
  loading: '加载中...',
  author_unknown: '未知作者',
  scenes_in_use: '场景在用',
  items_count_suffix: '条',
  per_page_suffix: '/页',
  like_failed: '操作失败',
  install: '安装',
  installed: '已安装',
  install_failed: '安装失败',
  install_success: '安装成功',
  uninstall: '卸载',
  uninstall_confirm: '确认卸载此插件？卸载后随插件创建的捆绑技能将一并移除',
  uninstall_success: '已卸载',
  uninstall_failed: '卸载失败',
  installing_short: '安装中...',
  versions: '版本族',
  versions_empty: '暂无版本记录',
  rollback: '回滚到此版本',
  rollback_success: '已切换生效版本',
  rollback_failed: '回滚失败',
  download: '下载插件包',
  download_failed: '下载失败',
  manifest: 'manifest（插件清单）',
  certified_short: '官方认证',
  cancel: '取消',
  cred_title: '凭据',
  cred_hint: '填入后密文保存，不回显明文',
  cred_configured: '已配置',
  cred_placeholder: '输入凭据值...',
  cred_placeholder_configured: '已密文保存，留空保持不变，输入新值可修改',
  cred_none_declared: 'manifest 未声明凭据，可自由添加',
  cred_free_add: '添加凭据',
  cred_free_key_ph: '自定义键名（如 OPENAI_API_KEY）',
  cred_show: '明文显示',
  cred_hide: '隐藏',
  cred_save: '保存凭据',
  cred_saving: '保存中...',
  cred_saved: '凭据已保存',
  cred_save_failed: '凭据保存失败: {{msg}}',
  cred_nothing: '没有要保存的凭据',
  search_match: '命中',
  param_title: '运行参数',
  param_hint: '个人默认值，保存后调用未传参时生效',
  param_none: 'manifest 未声明运行参数',
  param_col_key: '参数',
  param_col_type: '类型',
  param_col_desc: '说明',
  param_col_value: '默认值（可编辑）',
  param_save: '保存参数',
  param_saving: '保存中...',
  param_saved: '参数默认值已保存',
  param_save_failed: '参数保存失败: {{msg}}',
  param_nothing: '没有要保存的参数值',
  param_src_personal: '个人覆盖（A4 继承链）',
  param_src_personal_tag: '个人',
  'upload.submit': '上传',
  'upload.title': '上传插件',
  'upload.file_label': '插件 ZIP 包（根目录须含 plugin.json / manifest.json）',
  'upload.scope_label': '作用域',
  'upload.repo_label': '仓库',
  'upload.category_label': '分类',
  'upload.category_uncategorized': '未分类',
  'upload.uploading': '上传中',
  'upload.validating': 'L0 门禁校验中',
  'upload.source_repo_label': '来源仓库（可选）',
  'upload.source_ref_label': '来源 commit（可选，40 位 sha）',
  'upload.source_branch_label': '来源分支（可选，缺省默认分支）',
  'upload.toast_select_zip': '请选择插件 ZIP 文件',
  'upload.toast_scope_repo_required': '仓库插件必须先在侧边栏选择仓库',
  'upload.toast_upload_success': '上传成功',
  'upload.toast_upload_failed': '上传失败',
  'upload.l0_errors_title': 'L0 校验未通过（{count} 项）',
  // 复审返工（QA 202045541 P1-2/P1-3/P3-7）
  cred_free_remove: '删除',
  cred_free_pending: '自由行凭据待后端迁移，本次会话生效',
  cred_saved_session: '已保存；自由行凭据待后端迁移，本次会话生效',
  detail_forbidden: '无权访问该插件（仅作者/所属仓库可见）',
}

// 详情/表格硬编码文案（plugins.detail.* 走 I18N，缺失回退中文）
const D = {
  close: '关闭',
  load_failed: '加载失败',
  author: '作者',
  version: '版本',
  category: '分类',
  updated: '更新时间',
  namespace: '命名空间',
  fingerprint: '包指纹',
  status: '状态',
  action: '操作',
  effective: '生效中',
  history: '历史',
  revoked: '已下架',
}
function td(key, params) {
  const full = 'plugins.detail.' + key
  const v = G.I18N.t(full, params)
  return v && v !== full ? v : D[key]
}

// ── 列表状态 ──
const tab = ref('official')
const sort = ref('latest')
const qDraft = ref('')
const q = ref('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const plugins = ref([])
const categories = ref([])
const activeTagId = ref('')
const listLoading = ref(false)
const currentRepo = ref('')
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))

// 搜索态（P3-2：GET /api/plugins/search 全局命中；空 q/空结果/异常回落列表接口）
const searchMode = ref(false)
const searchHits = ref([])
let searchTimer = null

function stale() {
  return G.window._currentView !== 'plugins' && G.window._currentView !== 'plugins:' + tab.value
}

function initRepo() {
  try {
    const c = G.app.currentRepo()
    currentRepo.value = c ? c.fullName || '' : ''
  } catch (e) {
    currentRepo.value = ''
  }
}

// 分类契约：GET /api/plugins/categories
function loadCategories() {
  if (categories.value.length) return
  G.API.get('/plugins/categories')
    .then(function (d) {
      categories.value = d.categories || []
    })
    .catch(function () {})
}

// 列表契约：GET /api/plugins（q/category_id/sort/page/page_size/scope/repo）
function loadPlugins() {
  listLoading.value = true
  G.window._currentView = 'plugins:' + tab.value
  const params = ['scope=' + tab.value]
  if (currentRepo.value) params.push('repo=' + encodeURIComponent(currentRepo.value))
  if (sort.value) params.push('sort=' + sort.value)
  if (q.value) params.push('q=' + encodeURIComponent(q.value))
  if (activeTagId.value) params.push('category_id=' + encodeURIComponent(activeTagId.value))
  params.push('page=' + page.value)
  params.push('page_size=' + pageSize.value)
  G.API.get('/plugins?' + params.join('&'))
    .then(function (d) {
      if (stale()) return
      plugins.value = d.plugins || []
      total.value = d.total || plugins.value.length
      listLoading.value = false
    })
    .catch(function () {
      if (stale()) return
      plugins.value = []
      total.value = 0
      listLoading.value = false
    })
}

function loadAll() {
  initRepo()
  loadCategories()
  loadPlugins()
}

function switchTab(t) {
  tab.value = t
  page.value = 1
  activeTagId.value = ''
  searchMode.value = false
  searchHits.value = []
  loadAll()
}
function switchTag(id) {
  activeTagId.value = id || ''
  page.value = 1
  searchMode.value = false
  searchHits.value = []
  loadPlugins()
}
function setSort(s) {
  sort.value = s
  page.value = 1
  searchMode.value = false
  searchHits.value = []
  loadPlugins()
}
// 搜索输入防抖 300ms（连续输入重置计时器，静默后统一走搜索契约）
function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(doSearch, 300)
}
// 搜索契约（P3-2）：非空 q 走 GET /api/plugins/search（名称精确 + 关键词命中，
// 行内展示 match_reason）；空 q / 空结果 / 接口异常回落列表接口 ?q=
function doSearch() {
  q.value = qDraft.value
  page.value = 1
  if (!q.value.trim()) {
    searchMode.value = false
    searchHits.value = []
    loadPlugins()
    return
  }
  G.API.get('/plugins/search?q=' + encodeURIComponent(q.value))
    .then(function (d) {
      const rows = (d && d.plugins) || []
      if (!rows.length) {
        searchMode.value = false
        searchHits.value = []
        loadPlugins()
        return
      }
      searchMode.value = true
      searchHits.value = rows.map(function (p) {
        return {
          id: p.plugin_id || p.id,
          name: p.name,
          display_name: p.display_name || p.name,
          description: p.description || '',
          version: p.version,
          author: p.author || '',
          category_name: p.category_name || '',
          match_reason: p.match_reason || '',
        }
      })
    })
    .catch(function () {
      searchMode.value = false
      searchHits.value = []
      loadPlugins()
    })
}
function resetFilters() {
  activeTagId.value = ''
  q.value = ''
  qDraft.value = ''
  page.value = 1
  searchMode.value = false
  searchHits.value = []
  loadPlugins()
}
function goPage(p) {
  page.value = Math.min(totalPages.value, Math.max(1, p))
  loadPlugins()
}
function setPageSize(v) {
  pageSize.value = parseInt(v, 10)
  page.value = 1
  loadPlugins()
}

function tagStyle(on) {
  return on
    ? 'padding:5px 12px;font-size:11px;color:var(--a);border:1px solid var(--a);background:rgba(49,104,224,0.06);cursor:pointer;border-radius:8px;transition:all .15s;white-space:nowrap;text-decoration:none'
    : 'padding:5px 12px;font-size:11px;color:var(--t2);border:1px solid var(--b);background:var(--c);cursor:pointer;border-radius:8px;transition:all .15s;white-space:nowrap;text-decoration:none'
}

function fullName(p) {
  const ns = p.namespace_owner || ''
  const nm = p.display_name || p.name
  return ns ? nm + '@' + ns : nm
}
function scopeLabel(scope) {
  if (scope === 'official') return tp('scope_official')
  if (scope === 'repo') return tp('scope_repo')
  return tp('scope_user')
}
function scopeTagClass(p) {
  return p.scope === 'official' ? 'tg-c' : p.scope === 'repo' ? 'tg-i' : 'tg-d'
}

// 点赞契约（实查无 by-id）：POST /api/plugins/{plugin_id}/like
function onLike(p) {
  G.API.post('/plugins/' + p.id + '/like', {})
    .then(function (r) {
      p.liked_by_me = !!r.liked
      p.like_count = r.like_count || 0
    })
    .catch(function () {
      G.app.showToast(tp('like_failed'))
    })
}

// ── 详情抽屉（契约：GET /api/plugins/by-id/{id}，含 manifest 与 zip_url）──
const detail = ref(null)
const detailOpen = ref(false)
const detailLoading = ref(false)
const versions = ref([])
const installedNames = ref([])
const installing = ref(false)
const uninstalling = ref(false)

const prettyManifest = computed(function () {
  const raw = (detail.value && detail.value.manifest) || ''
  if (!raw) return ''
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch (e) {
    return raw
  }
})

function shortHash(h) {
  return h ? String(h).slice(0, 16) + '…' : '—'
}

// ═══ 凭据段（就地配置：声明键走插件端点；自由增行走 settings 泛化端点）═══
const credState = ref({
  declared: [], // manifest 声明凭据：[{key,label,required,value,dirty,show}]
  freeRows: [], // ➕ 自由增行（未保存）：[{key,value,show}]
  savedFreeRows: [], // 已保存的自由行（未声明已配置）：[{key,value,dirty,show}]
  configuredKeys: [], // GET credentials 回显的存在性（无明文）
  freeConfiguredKeys: [], // 自由行保存后的同屏标记（后端 provider 透传就绪前客户端态）
  loading: false,
  saving: false,
})

function manifestObj(p) {
  try {
    return JSON.parse((p && p.manifest) || '{}') || {}
  } catch (e) {
    return {}
  }
}

// manifest 凭据声明双格式兼容：
// 数组形态（registry plugin.json）[{key,label,required}]；
// 映射形态（市场 manifest.json）{KEY: {required, inject, ...}}
function parseManifestCreds(mf) {
  const raw = (mf && mf.credentials) || []
  const out = []
  if (Array.isArray(raw)) {
    raw.forEach(function (f) {
      if (!f) return
      const key = f.key || f.name || ''
      if (key) {
        out.push({
          key: String(key),
          label: f.label || String(key),
          required: !!f.required,
          inject: f.inject || '',
        })
      }
    })
  } else if (raw && typeof raw === 'object') {
    Object.keys(raw).forEach(function (k) {
      const v = raw[k] || {}
      out.push({
        key: k,
        label: v.label || k,
        required: !!v.required,
        inject: v.inject || '',
      })
    })
  }
  return out
}

// 抽屉仍停留在同一插件（切插件/关抽屉后的迟到响应一律丢弃）
function sameDetail(name) {
  return (
    G.window._currentView === 'plugins:detail' &&
    !!detail.value &&
    detail.value.name === name
  )
}

// 凭据存在性回显：GET /api/plugins/{name}/credentials → configured_keys（掩码语义，无明文）
function loadCreds(name) {
  credState.value.configuredKeys = []
  credState.value.loading = true
  G.API.get('/plugins/' + encodeURIComponent(name) + '/credentials')
    .then(function (d) {
      if (!sameDetail(name)) return
      credState.value.configuredKeys = (d && d.configured_keys) || []
      credState.value.loading = false
      syncSavedFreeRows()
    })
    .catch(function () {
      if (!sameDetail(name)) return
      credState.value.loading = false
    })
}

// 复审返工：configured_keys ∪ freeConfiguredKeys 中「未在 manifest 声明」的键
// 渲染为可继续编辑/删除的自由行（保存后同屏可见，不再立即消失）
function syncSavedFreeRows() {
  const declaredKeys = credState.value.declared.map(function (f) {
    return f.key
  })
  const seen = {}
  const rows = []
  credState.value.configuredKeys.concat(credState.value.freeConfiguredKeys).forEach(function (k) {
    const key = String(k || '')
    if (!key || seen[key] || declaredKeys.indexOf(key) >= 0) return
    seen[key] = true
    const prev = credState.value.savedFreeRows.find(function (r) {
      return r.key === key
    })
    rows.push(prev || { key: key, value: '', dirty: false, show: false })
  })
  credState.value.savedFreeRows = rows
}

function removeSavedCredRow(key) {
  credState.value.configuredKeys = credState.value.configuredKeys.filter(function (k) {
    return k !== key
  })
  credState.value.freeConfiguredKeys = credState.value.freeConfiguredKeys.filter(function (k) {
    return k !== key
  })
  syncSavedFreeRows()
}

function isCredConfigured(key) {
  return (
    credState.value.configuredKeys.indexOf(key) >= 0 ||
    credState.value.freeConfiguredKeys.indexOf(key) >= 0
  )
}

// 👁 仅切换输入框 type（明文回显不走任何端点）
function toggleCredEye(f) {
  f.show = !f.show
}

function addCredRow() {
  credState.value.freeRows.push({ key: '', value: '', show: false })
}

function removeCredRow(ri) {
  credState.value.freeRows.splice(ri, 1)
}

// 关闭/切换抽屉时复位凭据表单（脏态不泄漏到下一个插件）
function resetCredState() {
  credState.value.declared = []
  credState.value.freeRows = []
  credState.value.savedFreeRows = []
  credState.value.configuredKeys = []
  credState.value.freeConfiguredKeys = []
  credState.value.loading = false
  credState.value.saving = false
}

// 复审返工：保存后广播跨视图刷新事件——Runtime 视图已打开的插件面板即时重拉
// （无 CustomEvent 的旧环境/静态测试环境静默降级）
function notifyPluginConfigSaved(name) {
  try {
    if (typeof G.window.dispatchEvent === 'function' && typeof G.window.CustomEvent === 'function') {
      G.window.dispatchEvent(new G.window.CustomEvent('grape:plugin-config-saved', { detail: { name: name } }))
    }
  } catch (e) {
    /* 事件广播失败不影响保存主链路 */
  }
}

// 保存凭据：manifest 声明键 → POST 插件端点（仅提交 dirty 非空值，掩码占位不回写）；
// 自由增行 → PUT settings 泛化端点（provider=plugin:{name} 冒号格式）
function saveCreds() {
  const p = detail.value
  if (!p) return
  const declared = {}
  credState.value.declared.forEach(function (f) {
    const v = (f.value || '').trim()
    if (f.key && v !== '' && f.dirty) declared[f.key] = v
  })
  const free = []
  credState.value.freeRows.forEach(function (r) {
    const k = (r.key || '').trim()
    const v = (r.value || '').trim()
    if (k && v) free.push({ key: k, value: v })
  })
  // 复审返工：已配置但未声明的自由行支持原地改值（仅 dirty 且非空才提交）
  credState.value.savedFreeRows.forEach(function (r) {
    const v = (r.value || '').trim()
    if (r.key && v !== '' && r.dirty) free.push({ key: r.key, value: v })
  })
  if (!Object.keys(declared).length && !free.length) {
    G.app.showToast(tp('cred_nothing'))
    return
  }
  credState.value.saving = true
  const jobs = []
  if (Object.keys(declared).length) {
    jobs.push(
      G.API.post('/plugins/' + encodeURIComponent(p.name) + '/credentials', {
        config: declared,
      })
    )
  }
  free.forEach(function (c) {
    jobs.push(
      G.API.put('/settings/credentials', {
        provider: 'plugin:' + p.name,
        config: { [c.key]: c.value },
      }).then(function () {
        if (credState.value.freeConfiguredKeys.indexOf(c.key) < 0) {
          credState.value.freeConfiguredKeys.push(c.key)
        }
      })
    )
  })
  Promise.all(jobs)
    .then(function () {
      credState.value.saving = false
      credState.value.declared.forEach(function (f) {
        f.value = ''
        f.dirty = false
        f.show = false
      })
      credState.value.freeRows = []
      credState.value.savedFreeRows.forEach(function (r) {
        r.value = ''
        r.dirty = false
        r.show = false
      })
      syncSavedFreeRows()
      // 自由行当前仅客户端会话标记（后端 provider 透传未迁移）——提示不得谎称已持久化
      if (free.length) G.app.showToast(tp('cred_saved_session'))
      else G.app.showToast(tp('cred_saved'))
      notifyPluginConfigSaved(p.name)
      loadCreds(p.name)
    })
    .catch(function (e) {
      credState.value.saving = false
      G.app.showToast(tp('cred_save_failed', { msg: (e && e.message) || '' }))
    })
}

// ═══ 参数段（A4 配置继承链·个人档：GET/PUT /api/plugins/{name}/config）═══
const paramState = ref({
  rows: [], // [{pk, cmd, name, type, required, description, enumOpts, value, original, personal, dirty}]
  loading: false,
  saving: false,
})

// commands 双格式兼容：数组 [{name, params: [...]}]（A1 schema）与映射表 {"cmd": {params: [...]}}（早期 demo 包）
function normalizeCommands(cmds) {
  let arr = cmds || []
  if (!Array.isArray(arr) && arr && typeof arr === 'object') {
    arr = Object.keys(arr).map(function (k) {
      const c = typeof arr[k] === 'object' && arr[k] ? arr[k] : {}
      c.name = c.name || k
      return c
    })
  }
  return Array.isArray(arr) ? arr : []
}

// params 双格式兼容：数组 [{name, type, required, description}]；映射表简写
// {"address": "深圳市南山区"}（字符串值即描述）或 {"address": {...}}
function normalizeParams(ps) {
  let arr = ps || []
  if (!Array.isArray(arr) && arr && typeof arr === 'object') {
    arr = Object.keys(arr).map(function (k) {
      const v = arr[k]
      const x = typeof v === 'object' && v ? v : {}
      x.name = x.name || k
      if (typeof v === 'string' && v && !x.description) x.description = v
      return x
    })
  }
  return Array.isArray(arr) ? arr : []
}

function parseManifestParams(mf) {
  const rows = []
  normalizeCommands((mf && mf.commands) || []).forEach(function (cmd) {
    const cn = cmd.name || ''
    if (!cn) return
    normalizeParams(cmd.params).forEach(function (pa) {
      if (!pa.name) return
      rows.push({
        pk: cn + '.' + pa.name,
        cmd: cn,
        name: pa.name,
        type: pa.type || 'string',
        required: !!pa.required,
        description: pa.description || '',
        enumOpts: pa.enum || pa.options || [],
        value: '',
        original: '',
        personal: false,
        dirty: false,
      })
    })
  })
  return rows
}

// 个人覆盖值回填：GET /api/plugins/{name}/config（未配置返回空对象 → 全默认）
function loadParams(name) {
  paramState.value.loading = true
  G.API.get('/plugins/' + encodeURIComponent(name) + '/config')
    .then(function (d) {
      if (!sameDetail(name)) return
      const cfg = (d && d.config) || {}
      paramState.value.rows.forEach(function (r) {
        if (Object.prototype.hasOwnProperty.call(cfg, r.pk)) {
          r.original = cfg[r.pk]
          r.value = cfg[r.pk]
          r.personal = true
        }
      })
      paramState.value.loading = false
    })
    .catch(function () {
      if (!sameDetail(name)) return
      paramState.value.loading = false
    })
}

function onParamBool(r, ev) {
  r.value = ev && ev.target && ev.target.checked ? 'true' : 'false'
  r.dirty = true
}

function resetParamState() {
  paramState.value.rows = []
  paramState.value.loading = false
  paramState.value.saving = false
}

// 保存参数：PUT /api/plugins/{name}/config——保存前 diff 原值，仅提交被改动
// 且非空的键（不全量写回，避免 personal overlay 覆盖默认值/写空）
function saveParams() {
  const p = detail.value
  if (!p) return
  const config = {}
  paramState.value.rows.forEach(function (r) {
    const v = String(r.value === null || r.value === undefined ? '' : r.value)
    if (r.pk && r.dirty && v !== '' && v !== String(r.original)) config[r.pk] = v
  })
  if (!Object.keys(config).length) {
    G.app.showToast(tp('param_nothing'))
    return
  }
  paramState.value.saving = true
  G.API.put('/plugins/' + encodeURIComponent(p.name) + '/config', { config: config })
    .then(function () {
      paramState.value.saving = false
      paramState.value.rows.forEach(function (r) {
        if (Object.prototype.hasOwnProperty.call(config, r.pk)) {
          r.original = config[r.pk]
          r.personal = true
        }
        r.dirty = false
      })
      G.app.showToast(tp('param_saved'))
      notifyPluginConfigSaved(p.name)
    })
    .catch(function (e) {
      paramState.value.saving = false
      G.app.showToast(tp('param_save_failed', { msg: (e && e.message) || '' }))
    })
}

let detailReqId = null
function loadDetail(id) {
  detailLoading.value = true
  detailReqId = id
  G.window._currentView = 'plugins:detail'
  G.API.get('/plugins/by-id/' + encodeURIComponent(id))
    .then(function (d) {
      if (detailReqId !== id || G.window._currentView !== 'plugins:detail') return
      detail.value = d
      detailLoading.value = false
      loadVersions(d.id)
      loadInstalled()
      credState.value.declared = parseManifestCreds(manifestObj(d)).map(function (f) {
        return { key: f.key, label: f.label, required: f.required, inject: f.inject, value: '', dirty: false, show: false }
      })
      loadCreds(d.name)
      paramState.value.rows = parseManifestParams(manifestObj(d))
      loadParams(d.name)
    })
    .catch(function (e) {
      if (detailReqId !== id || G.window._currentView !== 'plugins:detail') return
      detailLoading.value = false
      // 复审返工：搜索命中他人 user/repo 插件时 by-id 返回 403——给出明确无权限文案
      if (e && e.status === 403) {
        G.app.showToast(tp('detail_forbidden'))
        return
      }
      G.app.showToast(td('load_failed', { msg: (e && e.message) || '' }))
    })
}

function loadVersions(id) {
  versions.value = []
  G.API.get('/plugins/by-id/' + encodeURIComponent(id) + '/versions')
    .then(function (d) {
      versions.value = d.versions || []
    })
    .catch(function () {
      versions.value = []
    })
}

// 版本族内切换生效版本（契约：POST /api/plugins/by-id/{id}/rollback，body.version）
function doRollback(v) {
  if (!detail.value || !v) return
  G.API.post('/plugins/by-id/' + encodeURIComponent(detail.value.id) + '/rollback', {
    version: v.version,
  })
    .then(function () {
      G.app.showToast(tp('rollback_success'))
      loadDetail(detail.value.id)
    })
    .catch(function (e) {
      G.app.showToast(tp('rollback_failed', { msg: (e && e.message) || '' }))
    })
}

// C1 已安装清单（契约：GET /api/plugins/installed）——驱动安装/卸载按钮切换
function loadInstalled() {
  G.API.get('/plugins/installed')
    .then(function (d) {
      installedNames.value = (d.plugins || []).map(function (p) {
        return p.name
      })
    })
    .catch(function () {
      installedNames.value = []
    })
}

// 安装（契约：POST /api/plugins/install，body = manifest 清单透传）
function doInstall(p) {
  if (!p) return
  let manifest
  try {
    manifest = JSON.parse(p.manifest || '{}') || {}
  } catch (e) {
    manifest = {}
  }
  if (!manifest.name) manifest.name = p.name
  if (!manifest.version) manifest.version = p.version
  installing.value = true
  G.API.post('/plugins/install', manifest)
    .then(function () {
      installing.value = false
      G.app.showToast(tp('install_success'))
      loadInstalled()
    })
    .catch(function (e) {
      installing.value = false
      G.app.showToast(tp('install_failed', { msg: (e && e.message) || '' }))
    })
}

// 卸载（契约：DELETE /api/plugins/{name}；随插件创建的捆绑技能联动移除）
function doUninstall(p) {
  if (!p) return
  G.app.showConfirm(tp('uninstall_confirm'), function () {
    uninstalling.value = true
    G.API.del('/plugins/' + encodeURIComponent(p.name))
      .then(function () {
        uninstalling.value = false
        G.app.showToast(tp('uninstall_success'))
        loadInstalled()
      })
      .catch(function (e) {
        uninstalling.value = false
        G.app.showToast(tp('uninstall_failed', { msg: (e && e.message) || '' }))
      })
  })
}

// 下载插件包（详情优先 zip_url 签名直链；缺签名直链时走 /download 校验出口）
function downloadPlugin(p) {
  if (p.zip_url) {
    try {
      G.window.open(p.zip_url, '_blank')
      return
    } catch (e) {
      /* 兜底走 /download */
    }
  }
  G.API.getRaw('/plugins/by-id/' + encodeURIComponent(p.id) + '/download')
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status)
      return r.blob().then(function (b) {
        const a = document.createElement('a')
        a.href = URL.createObjectURL(b)
        a.download = 'plugin-' + p.name + '.zip'
        document.body.appendChild(a)
        a.click()
        a.remove()
        setTimeout(function () {
          URL.revokeObjectURL(a.href)
        }, 5000)
      })
    })
    .catch(function () {
      G.app.showToast(tp('download_failed'))
    })
}

function openDetail(id) {
  detail.value = null
  versions.value = []
  resetCredState()
  resetParamState()
  detailOpen.value = true
  loadDetail(id)
}

// 关闭抽屉：仅复位抽屉态——列表不卸载、不重拉，筛选/滚动位置原样保持
function closeDetail() {
  detailOpen.value = false
  detail.value = null
  versions.value = []
  resetCredState()
  resetParamState()
  G.window._currentView = 'plugins:' + tab.value
}

// ── 上传（契约：L0 门禁 POST /api/plugin-market/validate-package 先行 → POST /api/plugins/upload）──
const showUpload = ref(false)
const uploading = ref(false)
const validating = ref(false)
const uploadDone = ref(false)
const uploadPct = ref(0)
const uploadErrors = ref([])
const uploadForm = ref({
  scope: 'user',
  categoryId: '0',
  file: null,
  sourceRepo: '',
  sourceRef: '',
  sourceBranch: '',
})

const categoryOpts = computed(function () {
  const opts = [{ value: '0', label: tp('upload.category_uncategorized') }]
  categories.value.forEach(function (c) {
    opts.push({ value: String(c.id), label: (c.icon || '') + ' ' + c.name })
  })
  return opts
})

function defaultUploadForm(scope) {
  return {
    scope: scope,
    categoryId: '0',
    file: null,
    sourceRepo: '',
    sourceRef: '',
    sourceBranch: '',
  }
}

function openUpload() {
  const scope = tab.value === 'repo' ? 'repo' : 'user'
  uploadForm.value = defaultUploadForm(scope)
  uploadErrors.value = []
  uploadPct.value = 0
  uploadDone.value = false
  showUpload.value = true
}

// 复审返工：坏 zip 等后端分支只回 {code,message}（无 errors）——收敛成单条错误进
// pm__l0err 面板，不再只弹 toast（AC『缺字段时降级展示 message』由此可达）
function pushUploadError(e) {
  if (e && e.errors && Array.isArray(e.errors) && e.errors.length) {
    uploadErrors.value = e.errors
    return true
  }
  const d = (e && e.detail) || {}
  const msg = d.message || (e && e.message) || ''
  if (!msg) return false
  uploadErrors.value = [
    {
      code: d.code || (e && e.status) || '',
      path: '',
      reason: msg,
    },
  ]
  return true
}

function doUpload() {
  if (!uploadForm.value.file) {
    G.app.showToast(tp('upload.toast_select_zip'))
    return
  }
  if (uploadForm.value.scope === 'repo' && !currentRepo.value) {
    G.app.showToast(tp('upload.toast_scope_repo_required'))
    return
  }
  uploadErrors.value = []
  validating.value = true
  // ① L0 门禁先行（multipart file；拒绝即展示结构化清单，不上传）
  const vfd = new FormData()
  vfd.append('file', uploadForm.value.file)
  G.API.upload('/plugin-market/validate-package', vfd)
    .then(function (report) {
      validating.value = false
      if (report && report.valid === false) {
        uploadErrors.value = report.errors || []
        return
      }
      doUploadAfterGate()
    })
    .catch(function (e) {
      validating.value = false
      // 门禁未合入（!1666 前 404）/暂不可用 → 不阻断上传主链路，直接走 upload 由其 400 口径兜底
      if (e && (e.status === 404 || e.status === 405)) {
        doUploadAfterGate()
        return
      }
      // 结构化 errors 原样展示；坏 zip 等仅 {code,message} 时降级为单条面板错误
      if (pushUploadError(e)) return
      G.app.showToast((e && e.message) || tp('upload.toast_upload_failed'))
    })
}

function doUploadAfterGate() {
  const f = uploadForm.value
  const fd = new FormData()
  fd.append('file', f.file)
  fd.append('scope', f.scope)
  if (f.scope === 'repo' && currentRepo.value) fd.append('repo', currentRepo.value)
  fd.append('category_id', String(parseInt(f.categoryId, 10)))
  if (f.sourceRepo) fd.append('source_repo', f.sourceRepo)
  if (f.sourceRef) fd.append('source_ref', f.sourceRef)
  if (f.sourceBranch) fd.append('source_branch', f.sourceBranch)
  uploading.value = true
  uploadPct.value = 0
  uploadDone.value = false
  // ② 上架（POST /api/plugins/upload；400 PLUGIN_L0_INVALID 时 detail.errors 同构展示）
  G.API.upload('/plugins/upload', fd, function (pct) {
    uploadPct.value = pct
  })
    .then(function (r) {
      uploading.value = false
      uploadDone.value = true
      showUpload.value = false
      G.app.showToast((r && r.message) || tp('upload.toast_upload_success'))
      page.value = 1
      loadAll()
    })
    .catch(function (e) {
      uploading.value = false
      uploadPct.value = 0
      if (pushUploadError(e)) return
      G.app.showToast((e && e.message) || tp('upload.toast_upload_failed'))
    })
}

onMounted(function () {
  loadAll()
})

onBeforeUnmount(function () {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = null
})
</script>

<style scoped>
.pm {
  min-height: 100%;
}

/* ═══ 详情抽屉（右侧 overlay，自研：mask + 右缘面板 + 转场）═══ */
.pm__drawer-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.32);
  z-index: 1000;
}
.pm__drawer {
  position: absolute;
  right: 0;
  top: 0;
  bottom: 0;
  width: min(560px, 92vw);
  background: var(--c);
  border-left: 1px solid var(--b);
  box-shadow: -12px 0 40px rgba(0, 0, 0, 0.18);
  overflow-y: auto;
  padding: 18px 22px 44px;
  text-align: left;
}
.pm__drawer-close {
  position: absolute;
  top: 12px;
  right: 14px;
  width: 30px;
  height: 30px;
  border-radius: 50%;
  border: 1px solid var(--b);
  background: var(--bg);
  color: var(--t2);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
  z-index: 2;
}
.pm__drawer-close:hover {
  color: var(--t);
  border-color: var(--a);
}
.pm-drawer-enter-active,
.pm-drawer-leave-active {
  transition: opacity 0.2s ease;
}
.pm-drawer-enter-from,
.pm-drawer-leave-to {
  opacity: 0;
}
.pm-drawer-enter-active .pm__drawer,
.pm-drawer-leave-active .pm__drawer {
  transition: transform 0.22s ease;
}
.pm-drawer-enter-from .pm__drawer,
.pm-drawer-leave-to .pm__drawer {
  transform: translateX(28px);
}
.pm__seg {
  margin-top: 0;
}
.pm__subtab {
  text-align: left;
  margin-bottom: 20px;
}
.pm__search {
  justify-content: space-between;
}
.pm__tools {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.pm__count {
  font-size: 10px;
  font-weight: 400;
  color: var(--t2);
  background: var(--bg);
  padding: 2px 8px;
  border-radius: 8px;
  margin-left: 4px;
}
.pm__q {
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
.pm__q:focus {
  border-color: var(--a);
  background: var(--c);
}
.pm__upload-btn {
  color: var(--on-accent);
  background: var(--g-fill);
  border-color: var(--g-fill);
}
.pm__norepo {
  padding: 12px;
}
.pm__cols {
  margin-top: 10px;
}
.pm__main {
  flex: 1;
  min-width: 280px;
}
.pm__tags {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.pm__tag-ic {
  margin-right: 3px;
}
.pm__empty {
  padding: 20px;
  text-align: center;
}
.pm__pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  margin-top: 16px;
  padding: 12px 0;
}
.pm__pginfo {
  font-size: 12px;
  color: var(--t2);
}
.pm__pgsize {
  padding: 4px 8px;
  border: 1px solid var(--b);
  border-radius: var(--rd);
  font-size: 11px;
  background: var(--c);
  color: var(--t);
  outline: none;
}
.pm__cat-tag {
  font-size: 9px;
  background: rgba(49, 104, 224, 0.08);
  color: var(--a);
}
.pm__cert-tag {
  font-size: 9px;
  background: rgba(212, 156, 15, 0.12);
  color: var(--y);
}
.pm__like {
  cursor: pointer;
}
.pm__loading {
  text-align: center;
  padding: 40px 0;
  color: var(--t2);
  font-size: 13px;
}
.pm__mono {
  font-family: monospace;
  font-size: 11px;
}
.pm__manifest {
  background: var(--bg);
  padding: 14px;
  border-radius: var(--rd);
  font-size: 11px;
  font-family: monospace;
  line-height: 1.6;
  overflow-x: auto;
  white-space: pre-wrap;
  max-height: 400px;
  overflow-y: auto;
  margin: 0;
  text-align: left;
}
.pm__del-btn {
  color: var(--r);
  border-color: var(--r);
}

/* 搜索命中原因行 */
.pm__matchreason {
  color: var(--a);
  font-size: 11px;
  background: rgba(49, 104, 224, 0.06);
  border-radius: 8px;
  padding: 2px 8px;
  display: inline-block;
}

/* ═══ 凭据/参数配置段 ═══ */
.pm__cfg-card {
  margin-top: 14px;
}
.pm__cfg-sub {
  font-size: 11px;
  font-weight: 400;
  color: var(--t2);
  margin-left: 4px;
}
.pm__cfg-loading {
  font-size: 12px;
  color: var(--t2);
  padding: 8px 0;
}
.pm__credrow {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 0;
  flex-wrap: wrap;
}
.pm__credkey {
  flex: none;
  width: 200px;
  font-family: monospace;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pm__credreq {
  color: var(--r);
}
.pm__credinj {
  font-size: 10px;
  color: var(--t2);
  margin-left: 4px;
  font-family: inherit;
}
.pm__cfgnote {
  font-size: 10px;
  color: var(--t3);
  margin-top: 4px;
}
.pm__credrow-saved .pm__credkey {
  display: flex;
  align-items: center;
  gap: 4px;
}
.pm__credval {
  flex: 1;
  min-width: 160px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.pm__credinput {
  flex: 1;
  min-width: 120px;
  padding: 7px 10px;
  border: 1px solid var(--b);
  border-radius: 8px;
  font-size: 12px;
  background: var(--bg);
  color: var(--t);
  outline: none;
}
.pm__credinput:focus {
  border-color: var(--a);
}
.pm__credkeyin {
  flex: none;
  width: 200px;
  font-family: monospace;
}
.pm__credeye,
.pm__creddel {
  flex: none;
  border: 1px solid var(--b);
  background: var(--c);
  border-radius: 8px;
  width: 28px;
  height: 28px;
  font-size: 12px;
  cursor: pointer;
  color: var(--t2);
  line-height: 1;
}
.pm__creddel {
  color: var(--r);
}
.pm__credeye:hover,
.pm__creddel:hover {
  border-color: var(--a);
}
.pm__credok {
  flex: none;
  font-size: 11px;
  color: var(--g);
}
.pm__credempty {
  font-size: 12px;
  color: var(--t2);
  margin: 4px 0;
}
.pm__credadd {
  margin-top: 6px;
}
.pm__cfg-actions {
  margin-top: 10px;
  text-align: right;
}

/* ═══ 参数表 ═══ */
.pm__ptable {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.pm__ptable th {
  text-align: left;
  padding: 6px 8px;
  border-bottom: 1px solid var(--b);
  color: var(--t2);
  font-size: 11px;
  font-weight: 500;
}
.pm__ptable td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--b);
  vertical-align: middle;
}
.pm__pkey {
  font-family: monospace;
  font-size: 11px;
  word-break: break-all;
}
.pm__pdesc {
  color: var(--t2);
  font-size: 11px;
  max-width: 150px;
}
.pm__pval {
  min-width: 140px;
}
.pm__pchk {
  transform: translateY(1px);
}
.pm__psel {
  padding: 6px 8px;
  border: 1px solid var(--b);
  border-radius: 8px;
  font-size: 12px;
  background: var(--bg);
  color: var(--t);
  outline: none;
  max-width: 140px;
}
.pm__psrc {
  display: inline-block;
  margin-left: 6px;
  font-size: 10px;
  color: var(--a);
  background: rgba(49, 104, 224, 0.08);
  border-radius: 6px;
  padding: 1px 6px;
}

/* 上传弹窗 */
.pm__form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.pm__srow {
  margin-bottom: 0;
}
.pm__f1 {
  flex: 1;
  min-width: 0;
}
.pm__file-fld {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 4px;
}
.pm__file-fld > label {
  font-size: 11px;
  color: var(--t2);
}
.pm__file {
  font-size: 12px;
}
.pm__srcinput {
  flex: 1;
  padding: 7px 10px;
  border: 1px solid var(--b);
  border-radius: 6px;
  font-size: 12px;
  background: var(--bg);
  outline: none;
  color: var(--t);
}
.pm__srcinput:focus {
  border-color: var(--a);
}
.pm__upload-prog {
  margin-bottom: 6px;
}
.pm__upload-txt {
  font-size: 11px;
  color: var(--t2);
  margin-bottom: 4px;
}
.pm__upload-bar {
  background: var(--bg);
  border-radius: 8px;
  height: 8px;
  overflow: hidden;
}
.pm__upload-bar > div {
  background: var(--a);
  height: 100%;
  transition: width 0.2s;
}

/* L0 结构化错误清单 */
.pm__l0err {
  border: 1px solid rgba(220, 40, 40, 0.25);
  border-radius: 10px;
  padding: 10px 12px;
  background: rgba(220, 40, 40, 0.04);
}
.pm__l0err-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--r);
  margin-bottom: 8px;
}
.pm__l0err-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 260px;
  overflow-y: auto;
}
.pm__l0err-item {
  border: 1px solid var(--b);
  border-radius: 8px;
  padding: 8px 10px;
  background: var(--c);
  font-size: 11px;
}
.pm__l0err-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}
.pm__l0check {
  font-size: 9px;
}
.pm__l0code {
  font-family: monospace;
  font-size: 10px;
  color: var(--t2);
}
.pm__l0path {
  font-family: monospace;
  font-size: 10px;
  color: var(--a);
  word-break: break-all;
}
.pm__l0reason {
  color: var(--t);
  line-height: 1.5;
}
.pm__l0hint {
  color: var(--t2);
  margin-top: 2px;
  line-height: 1.5;
}
</style>
