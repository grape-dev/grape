<template>
  <div class="org">
    <!-- 列表视图 -->
    <template v-if="!modalOnly && !detailId">
      <div class="org__bar">
        <h2>{{ createOnly ? t('create.title') : t('title') }}</h2>
        <div class="sp"></div>
        <AGButton v-if="createOnly" variant="plain" @click="G.app._accountGoBack()">{{
          t('back_to_workspace')
        }}</AGButton>
        <template v-else>
          <!-- Issue #456：从个人中心进组织页后可一键回工作台 -->
          <AGButton variant="plain" @click="G.app._accountGoBack()">{{
            t('back_to_workspace')
          }}</AGButton>
          <AGButton @click="openCreate">{{ t('create_org') }}</AGButton>
        </template>
      </div>
      <div v-if="!createOnly" class="org__list">
        <div v-if="loading" class="org__hint">{{ t('loading') }}</div>
        <div v-else-if="error" class="org__hint org__hint--err">{{ error }}</div>
        <div v-else-if="!orgs.length" class="org__hint org__hint--center">{{ t('no_orgs') }}</div>
        <AGCard v-for="org in orgs" :key="org.id" class="org__card" @click="openDetail(org.id)">
          <div class="org__icon">🏢</div>
          <div class="org__info">
            <div class="org__name">
              {{ org.name || '' }}
              <span v-if="org.is_personal" class="org__badge">{{ t('badge_personal') }}</span>
            </div>
            <div class="org__meta">
              {{ org.slug || '' }} · {{ org.member_count || 0 }} {{ t('members') }}
            </div>
          </div>
          <div :class="['org__role', `role-${org.role}`]">{{ org.role || 'member' }}</div>
        </AGCard>
      </div>
    </template>

    <!-- 详情视图 -->
    <template v-else-if="!modalOnly">
      <div class="org__back">
        <AGButton variant="plain" size="sm" @click="closeDetail">{{
          t('back_to_workspace')
        }}</AGButton>
      </div>
      <div v-if="detailLoading" class="org__hint">{{ t('loading') }}</div>
      <div v-else-if="detailError" class="org__hint org__hint--err">{{ detailError }}</div>
      <template v-else-if="detail">
        <AGCard class="org__dcard">
          <div class="org__dhead">
            <div class="org__dicon">🏢</div>
            <div>
              <div class="org__dname">
                {{ detail.name || '' }}
                <span v-if="detail.is_personal" class="org__badge">{{ t('badge_personal') }}</span>
              </div>
              <div class="org__meta">{{ detail.slug || '' }}</div>
            </div>
          </div>
          <div class="org__desc-block">
            <div class="org__desc-label">{{ t('detail.description_label') }}</div>
            <div class="org__desc">{{ detail.description || t('detail.no_description') }}</div>
          </div>
          <div class="org__dmeta">
            {{ t('detail.role_label') }}: {{ detail.role || '' }}
            <template v-if="!detail.is_personal">
              · {{ t('detail.member_limit') }}: {{ detail.max_members || 50 }} ·
              {{ t('detail.worker_limit') }}: {{ detail.max_workers || 10 }}
            </template>
          </div>
        </AGCard>

        <!-- 仓库地图 + 关联仓库：一个卡片内完成查看能力、进入工作台、跳转平台仓库 -->
        <AGCard v-if="!detail.is_personal" class="org__dcard">
          <div class="org__mhead">
            <h3>
              {{ t('detail.repo_map_title') }}
              <span class="org__meta"
                >({{ repoMapSummary.ready || 0 }}/{{ repoMapSummary.total || 0 }})</span
              >
            </h3>
            <div class="org__mhead-actions">
              <AGButton size="sm" variant="plain" @click="openRepoMapSummary">
                {{ t('detail.repo_map_summary_view') }}
              </AGButton>
              <AGButton size="sm" variant="plain" :disabled="mapBusy" @click="analyzeAllMap">
                {{ mapBusy ? t('detail.repo_map_analyzing') : t('detail.repo_map_generate') }}
              </AGButton>
            </div>
          </div>
          <p class="org-repo-map__hint">{{ t('detail.repo_map_subtitle') }}</p>
          <div v-if="!repos.length" class="org__hint">{{ t('detail.repos_empty') }}</div>
          <template v-else>
            <!-- 组织仓库：编排/镜像用，不参与地图分析 -->
            <div v-if="orgRepo" class="org-repo-section org-repo-section--org">
              <div class="org-repo-section__head">
                <span class="org-repo-section__title">{{ t('detail.org_repo_section') }}</span>
                <span class="org__badge org__badge--accent">{{
                  t('workbench.repos.org_repo')
                }}</span>
              </div>
              <div
                class="org-repo org-repo--org"
                role="button"
                tabindex="0"
                @click="openRepoWorkspace(orgRepo)"
                @keydown.enter="openRepoWorkspace(orgRepo)"
              >
                <span class="org-repo__icon" v-html="repoIcon(orgRepo)"></span>
                <span class="org-repo__info">
                  <span class="org-repo__name">{{ repoName(orgRepo) }}</span>
                  <span class="org-repo__meta">{{
                    orgRepo.full_name || orgRepo.repo_key
                  }}</span>
                </span>
                <span class="org-repo__actions" @click.stop>
                  <a
                    class="org-repo__link"
                    :href="repoPageUrl(orgRepo)"
                    target="_blank"
                    rel="noopener"
                  >
                    {{ t('detail.open_repo_page') }} ↗
                  </a>
                </span>
              </div>
            </div>

            <!-- 关联仓库：即使一条都没有也要显示标题与「添加关联仓库」入口 -->
            <div class="org-repo-section">
              <div class="org-repo-section__head">
                <span class="org-repo-section__title"
                  >{{ t('detail.linked_repos_section') }} ({{ linkedRepos.length }})</span
                >
                <AGButton
                  v-if="canManageRepos"
                  size="sm"
                  variant="plain"
                  @click="openBindRepo"
                >
                  ＋ {{ ct('organizations.workbench.repos.picker_add') }}
                </AGButton>
              </div>
              <div v-if="linkedRepos.length" class="org__repolist">
                <div
                  v-for="repo in linkedRepos"
                  :key="repo.repo_key"
                  class="org-repo org-repo--map"
                  role="button"
                  tabindex="0"
                  @click="openRepoWorkspace(repo)"
                  @keydown.enter="openRepoWorkspace(repo)"
                >
                  <span class="org-repo__icon" v-html="repoIcon(repo)"></span>
                  <span class="org-repo__info">
                    <span class="org-repo__name">
                      {{ repoName(repo) }}
                      <span
                        class="org-repo-map__status"
                        :class="'org-repo-map__status--' + ((repoMapItem(repo) || {}).status || 'none')"
                        >{{ mapStatusLabel((repoMapItem(repo) || {}).status) }}</span
                      >
                    </span>
                    <span
                      v-if="(repoMapItem(repo) || {}).purpose"
                      class="org-repo__purpose"
                      >{{ (repoMapItem(repo) || {}).purpose }}</span
                    >
                    <span class="org-repo__meta">{{ repo.full_name || repo.repo_key }}</span>
                    <span
                      v-if="((repoMapItem(repo) || {}).domains || []).length"
                      class="org-repo-map__tags"
                    >
                      <span
                        v-for="d in ((repoMapItem(repo) || {}).domains || []).slice(0, 6)"
                        :key="'tag-' + repo.repo_key + '-' + d"
                        class="org-repo-map__tag"
                        >{{ d }}</span
                      >
                    </span>
                    <span v-if="(repoMapItem(repo) || {}).error" class="org-repo-map__error">{{
                      (repoMapItem(repo) || {}).error
                    }}</span>
                  </span>
                  <span class="org-repo__actions" @click.stop>
                    <a
                      class="org-repo__link"
                      :href="repoPageUrl(repo)"
                      target="_blank"
                      rel="noopener"
                    >
                      {{ t('detail.open_repo_page') }} ↗
                    </a>
                    <AGButton
                      v-if="(repoMapItem(repo) || {}).has_summary"
                      size="sm"
                      variant="plain"
                      @click="openMapSummary(repoMapItem(repo))"
                    >
                      {{ t('detail.repo_map_view') }}
                    </AGButton>
                    <AGButton
                      size="sm"
                      variant="plain"
                      :disabled="mapBusy || (repoMapItem(repo) || {}).status === 'analyzing'"
                      @click="analyzeOneMap(repoMapItem(repo) || { repo_key: repo.repo_key })"
                    >
                      {{
                        (repoMapItem(repo) || {}).has_summary
                          ? t('detail.repo_map_reanalyze')
                          : t('detail.repo_map_analyze')
                      }}
                    </AGButton>
                    <AGButton
                      v-if="canManageRepos"
                      size="sm"
                      variant="plain"
                      @click="openTransfer(repo)"
                    >
                      {{ ct('organizations.workbench.repos.transfer') }}
                    </AGButton>
                    <AGButton
                      v-if="canManageRepos"
                      size="sm"
                      variant="danger"
                      @click="unbindRepo(repo)"
                    >
                      {{ ct('organizations.workbench.repos.unbind') }}
                    </AGButton>
                  </span>
                </div>
              </div>
              <div v-else class="org__repolist-empty">
                {{ ct('organizations.workbench.repos.empty') }}
              </div>
            </div>
          </template>
        </AGCard>

        <AGCard class="org__dcard">
          <div class="org__mhead">
            <h3>{{ t('detail.members_title') }} ({{ members.length }})</h3>
            <AGButton
              v-if="(detail.role === 'owner' || detail.role === 'admin') && !detail.is_personal"
              size="sm"
              @click="showAddMember = true"
            >
              {{ t('detail.add_member') }}
            </AGButton>
          </div>
          <div v-for="m in members" :key="m.user_id" class="org__mrow">
            <div class="org__mavatar">{{ (m.username || '?').charAt(0).toUpperCase() }}</div>
            <div class="org__info">
              <div class="org__mname">{{ m.nickname || m.username || '' }}</div>
              <div class="org__meta">{{ m.username || '' }}</div>
            </div>
            <span :class="['org__role', `role-${m.role}`]">{{ m.role || '' }}</span>
            <AGButton
              v-if="detail.role === 'owner' && m.role !== 'owner'"
              variant="danger"
              size="sm"
              @click="removeMember(m)"
            >
              {{ ct('common.remove') }}
            </AGButton>
          </div>
        </AGCard>

        <AGCard class="org__dcard org__dactions">
          <AGButton
            v-if="detail.role === 'owner' || detail.role === 'admin'"
            variant="plain"
            @click="openEdit"
          >
            {{ t('detail.edit_org') || '编辑组织' }}
          </AGButton>
          <AGButton
            v-if="!detail.is_personal && detail.role === 'owner'"
            variant="danger"
            @click="deleteOrg"
          >
            {{ t('detail.delete_org') }}
          </AGButton>
          <AGButton :disabled="isCurrentOrg" @click="switchOrg">{{
            isCurrentOrg ? t('detail.current_org') : t('detail.switch_to')
          }}</AGButton>
        </AGCard>
      </template>
    </template>

    <!-- 创建组织弹窗 -->
    <AGModal
      :open="showCreate"
      :title="t('create.title')"
      :close-on-mask="false"
      @close="closeCreate"
    >
      <div class="org__form org__form--create">
        <AGInput
          v-model="createForm.name"
          :label="t('create.name_label')"
          :placeholder="t('create.name_placeholder')"
        />
        <AGInput
          v-model="createForm.slug"
          :label="t('create.slug_label')"
          :placeholder="t('create.slug_placeholder')"
        />
        <AGInput
          v-model="createForm.desc"
          :label="t('create.desc_label')"
          :placeholder="t('create.desc_placeholder')"
        />

        <!-- 组织仓库：平台 / 所属空间 / 名称 / 路径 / 可见性（对齐 GitCode/GitHub 建仓页） -->
        <div class="repo-create">
          <div class="repo-create__head">
            <div>
              <div class="repo-create__title">{{ t('create.repo_title') }}</div>
              <div class="repo-create__sub">{{ t('create.repo_subtitle') }}</div>
            </div>
            <span v-if="namespacesLoading" class="org__meta">{{ t('create.repo_loading') }}</span>
          </div>

          <div v-if="namespaceProviders.length" class="repo-create__providers">
            <button
              v-for="p in namespaceProviders"
              :key="p.provider"
              type="button"
              class="repo-plat"
              :class="{
                'repo-plat--active': createForm.repo_provider === p.provider,
                'repo-plat--off': !p.connected,
              }"
              :title="p.connected ? '' : p.error || t('create.repo_unbound')"
              @click="selectProvider(p)"
            >
              <span class="repo-plat__icon" v-html="platformIcon(p.provider)"></span>
              <span class="repo-plat__name">{{ p.name }}</span>
              <span v-if="p.connected" class="repo-plat__acct">
                <img
                  v-if="p.account && p.account.avatar_url"
                  class="repo-plat__avatar"
                  :src="p.account.avatar_url"
                  alt=""
                  referrerpolicy="no-referrer"
                />
                <span v-else class="repo-plat__avatar repo-plat__avatar--txt">{{
                  accountInitial(p.account)
                }}</span>
                <span class="repo-plat__login">{{ p.account && p.account.login }}</span>
              </span>
              <span v-else class="repo-plat__off"
                >{{ t('create.repo_unbound') }} · {{ t('create.repo_bind_action') }}</span
              >
            </button>
          </div>
          <div v-else-if="!namespacesLoading" class="repo-create__empty">
            {{ t('create.repo_no_platform') }}
          </div>

          <template v-if="currentNamespaceProvider">
            <div class="repo-create__field">
              <label>{{ t('create.repo_name_label') }}</label>
              <input
                v-model="createForm.repo_name"
                class="repo-name__input"
                type="text"
                maxlength="100"
                :placeholder="autoRepoName || t('create.repo_name_placeholder')"
                @input="onRepoNameInput"
              />
              <p class="repo-create__hint">{{ t('create.repo_name_hint') }}</p>
            </div>

            <div class="repo-create__field">
              <label>{{ t('create.repo_path_label') }}</label>
              <div class="repo-path">
                <img
                  v-if="currentOwner && currentOwner.avatar_url"
                  class="repo-path__avatar"
                  :src="currentOwner.avatar_url"
                  alt=""
                  referrerpolicy="no-referrer"
                />
                <span v-else class="repo-path__avatar repo-path__avatar--txt">{{
                  accountInitial(currentOwner)
                }}</span>
                <select v-model="createForm.repo_owner" class="repo-path__owner">
                  <optgroup :label="t('create.repo_owner_personal')">
                    <option
                      v-for="ns in personalNamespaces"
                      :key="'u-' + ns.login"
                      :value="ns.login"
                    >
                      {{ ns.login }}（{{ t('create.repo_owner_personal_tag') }}）
                    </option>
                  </optgroup>
                  <optgroup
                    v-if="orgNamespaces.length"
                    :label="t('create.repo_owner_orgs')"
                  >
                    <option v-for="ns in orgNamespaces" :key="'o-' + ns.login" :value="ns.login">
                      {{ ns.name || ns.login }}（{{ ns.login }}）
                    </option>
                  </optgroup>
                </select>
                <span class="repo-path__sep">/</span>
                <input
                  v-if="createForm.repo_provider === 'gitcode'"
                  v-model="createForm.repo_path"
                  class="repo-path__input"
                  type="text"
                  maxlength="100"
                  :placeholder="createForm.repo_name || autoRepoName"
                  @input="repoPathTouched = true"
                />
                <span v-else class="repo-path__fixed">{{
                  createForm.repo_name || autoRepoName || 'repo-name'
                }}</span>
              </div>
              <p class="repo-create__hint">
                {{
                  createForm.repo_provider === 'gitcode'
                    ? t('create.repo_path_hint')
                    : t('create.repo_path_github_hint')
                }}
              </p>
            </div>

            <div class="repo-create__field">
              <label>{{ t('create.repo_visibility_label') }}</label>
              <div class="repo-vis">
                <button
                  type="button"
                  class="repo-vis__opt"
                  :class="{ 'repo-vis__opt--active': createForm.repo_private }"
                  @click="createForm.repo_private = true"
                >
                  <span class="repo-vis__ic">🔒</span>
                  <span class="repo-vis__txt">
                    <b>{{ t('create.repo_private') }}</b>
                    <em>{{ t('create.repo_private_desc') }}</em>
                  </span>
                </button>
                <button
                  type="button"
                  class="repo-vis__opt"
                  :class="{ 'repo-vis__opt--active': !createForm.repo_private }"
                  @click="createForm.repo_private = false"
                >
                  <span class="repo-vis__ic">🌐</span>
                  <span class="repo-vis__txt">
                    <b>{{ t('create.repo_public') }}</b>
                    <em>{{ t('create.repo_public_desc') }}</em>
                  </span>
                </button>
              </div>
            </div>

          </template>

          <p v-if="!createForm.repo_provider" class="repo-create__hint repo-create__hint--warn">
            {{ t('create.repo_auto_hint') }}
          </p>
        </div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="closeCreate">{{
          ct('common.cancel')
        }}</AGButton>
        <AGButton size="sm" :disabled="creating" @click="createOrg">
          {{ creating ? t('create.creating') : t('create.confirm') }}
        </AGButton>
      </template>
    </AGModal>

    <!-- 添加成员弹窗 -->
    <AGModal :open="showAddMember" :title="t('add_member.title')" @close="showAddMember = false">
      <div class="org__form">
        <div class="org__field">
          <label>{{ t('add_member.username_label') }}</label>
          <div class="org__suggest-wrap">
            <input
              v-model="addMemberForm.username"
              :placeholder="t('add_member.username_placeholder')"
              @input="searchUsers"
            />
            <div v-if="suggestions.length" class="org__suggest">
              <div
                v-for="u in suggestions"
                :key="u.id"
                class="org__suggest-item"
                @click="pickUser(u)"
              >
                <span class="org__suggest-name">{{ u.username || '' }}</span>
                <span v-if="u.nickname" class="org__meta">{{ u.nickname }}</span>
              </div>
            </div>
          </div>
        </div>
        <div class="org__field">
          <label>{{ t('add_member.role_label') }}</label>
          <AGSelect v-model="addMemberForm.role" :options="roleOpts" />
        </div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="showAddMember = false">{{
          ct('common.cancel')
        }}</AGButton>
        <AGButton size="sm" :disabled="addingMember" @click="addMember">
          {{ addingMember ? t('add_member.adding') : t('add_member.confirm') }}
        </AGButton>
      </template>
    </AGModal>

    <!-- 编辑组织弹窗 -->
    <AGModal :open="showEdit" :title="t('detail.edit_org') || '编辑组织'" @close="showEdit = false">
      <div class="org__form">
        <AGInput v-model="editForm.name" :label="t('create.name_label') || '名称'" />
        <div class="org__field">
          <label>{{ t('create.desc_label') || '描述' }}</label>
          <textarea v-model="editForm.description" rows="2"></textarea>
        </div>
        <div v-if="!editForm.isPersonal" class="org__form-row">
          <AGInput
            v-model="editForm.max_members"
            :label="t('detail.member_limit') || '成员上限'"
            type="number"
          />
          <AGInput
            v-model="editForm.max_workers"
            :label="t('detail.worker_limit') || 'Worker 上限'"
            type="number"
          />
        </div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="showEdit = false">{{
          ct('common.cancel') || '取消'
        }}</AGButton>
        <AGButton size="sm" @click="saveEdit">{{ ct('common.save') || '保存' }}</AGButton>
      </template>
    </AGModal>

    <AGModal
      :open="showBindRepo"
      :title="ct('organizations.workbench.repos.picker_title')"
      @close="showBindRepo = false"
    >
      <div class="org__form">
        <input
          v-model="bindRepoSearch"
          class="org-bind__search"
          type="text"
          :placeholder="ct('organizations.workbench.repos.picker_search')"
        />
        <div class="org-bind__list">
          <div v-if="bindRepoLoading" class="org__hint">
            {{ t('loading') }}
          </div>
          <div v-else-if="!filteredBindRepos.length" class="org__hint">
            {{ ct('organizations.workbench.repos.picker_empty') }}
          </div>
          <label
            v-for="r in filteredBindRepos"
            :key="r.repo_key"
            class="org-bind__item"
          >
            <input
              type="checkbox"
              :checked="bindRepoSelected.includes(r.repo_key)"
              @change="toggleBindRepo(r.repo_key)"
            />
            <span class="org-bind__info">
              <span class="org-bind__name">{{ r.full_name || r.repo_key }}</span>
              <span class="org-bind__meta">{{ r.repo_key }}</span>
            </span>
            <span v-if="r.from_personal" class="org__badge">{{
              ct('organizations.workbench.repos.picker_from_personal')
            }}</span>
          </label>
        </div>
        <div class="org__meta">
          {{ ct('organizations.workbench.repos.picker_count', { n: bindRepoSelected.length }) }}
        </div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="showBindRepo = false">{{
          ct('common.cancel')
        }}</AGButton>
        <AGButton size="sm" :disabled="bindRepoSubmitting" @click="submitBindRepos">
          {{
            bindRepoSubmitting
              ? ct('organizations.workbench.repos.picker_binding')
              : ct('organizations.workbench.repos.picker_confirm')
          }}
        </AGButton>
      </template>
    </AGModal>

    <AGModal
      :open="showTransfer"
      :title="ct('organizations.workbench.repos.transfer_title')"
      @close="showTransfer = false"
    >
      <div class="org__form">
        <p class="org__hint">
          {{
            ct('organizations.workbench.repos.transfer_confirm', {
              name:
                (transferRepoRow && (transferRepoRow.full_name || transferRepoRow.repo_key)) || '',
            })
          }}
        </p>
        <div class="org__field">
          <label>{{ ct('organizations.workbench.repos.transfer') }}</label>
          <AGSelect
            v-model="transferTargetId"
            :options="transferOrgOptions"
            :placeholder="ct('organizations.workbench.repos.transfer_target_required')"
          />
        </div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="showTransfer = false">{{
          ct('common.cancel')
        }}</AGButton>
        <AGButton size="sm" :disabled="transferring" @click="submitTransfer">
          {{ ct('organizations.workbench.repos.transfer') }}
        </AGButton>
      </template>
    </AGModal>

    <BindDialog
      :open="showBindProvider"
      :provider="bindProviderTarget"
      :allow-pat="true"
      :already-bound="false"
      :providers="authProviders"
      @close="showBindProvider = false"
      @bound="onProviderBound"
    />

    <AGModal
      :open="showMapSummary"
      :title="mapSummaryTitle"
      @close="showMapSummary = false"
    >
      <div class="org-cap">
        <div class="org-cap__intro">
          <div class="org-cap__purpose">{{ mapSummary.purpose || ct('organizations.workbench.map_summary.purpose_empty') }}</div>
          <div class="org-cap__meta">
            <span v-if="mapConfidencePct">{{ ct('organizations.workbench.map_summary.confidence', { pct: mapConfidencePct }) }}</span>
            <span v-if="mapAnalyzedText">{{ ct('organizations.workbench.map_summary.updated_at', { time: mapAnalyzedText }) }}</span>
          </div>
        </div>

        <section v-if="mapSummaryCapabilities.length" class="org-cap__section">
          <div class="org-cap__label">{{ ct('organizations.workbench.map_summary.capabilities') }}</div>
          <ul class="org-cap__list">
            <li v-for="item in mapSummaryCapabilities" :key="'cap-' + item">{{ item }}</li>
          </ul>
        </section>

        <section v-if="mapSummaryInterfaces.length" class="org-cap__section">
          <div class="org-cap__label">{{ ct('organizations.workbench.map_summary.interfaces') }}</div>
          <div class="org-cap__chips">
            <code v-for="item in mapSummaryInterfaces" :key="'if-' + item">{{ item }}</code>
          </div>
        </section>

        <section v-if="mapSummaryDepends.length" class="org-cap__section">
          <div class="org-cap__label">{{ ct('organizations.workbench.map_summary.depends') }}</div>
          <div class="org-cap__chips">
            <span v-for="item in mapSummaryDepends" :key="'dep-' + item">{{
              shortRepo(item)
            }}</span>
          </div>
        </section>

        <section v-if="mapSummaryNotOwns.length" class="org-cap__section">
          <div class="org-cap__label">{{ ct('organizations.workbench.map_summary.not_owns') }}</div>
          <ul class="org-cap__list org-cap__list--muted">
            <li v-for="item in mapSummaryNotOwns" :key="'no-' + item">{{ item }}</li>
          </ul>
        </section>

        <section v-if="mapSummaryTags.length" class="org-cap__section">
          <div class="org-cap__label">{{ ct('organizations.workbench.map_summary.keywords') }}</div>
          <div class="org-cap__chips">
            <span v-for="item in mapSummaryTags" :key="'tag-' + item">{{ item }}</span>
          </div>
        </section>

        <div
          v-if="
            !mapSummaryCapabilities.length &&
            !mapSummaryInterfaces.length &&
            !mapSummaryNotOwns.length
          "
          class="org-cap__empty"
        >
          {{ ct('organizations.workbench.map_summary.empty') }}
        </div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="showMapSummary = false">
          {{ ct('common.close') }}
        </AGButton>
        <a
          v-if="mapSummaryUrl"
          class="org-repo__link org-repo__link--btn"
          :href="mapSummaryUrl"
          target="_blank"
          rel="noopener"
        >
          {{ t('detail.repo_map_open_file') }} ↗
        </a>
      </template>
    </AGModal>

    <AGModal
      :open="showRepoMapSummary"
      :title="t('detail.repo_map_summary_title')"
      @close="showRepoMapSummary = false"
    >
      <div class="org-cap">
        <div class="org-cap__intro">
          <div class="org-cap__purpose">{{ t('detail.repo_map_summary_intro') }}</div>
          <div class="org-cap__meta">
            <span>{{ t('detail.repo_map_summary_coverage', {
              ready: repoMapSummaryData.summary?.ready || 0,
              total: repoMapSummaryData.summary?.total || 0,
            }) }}</span>
            <span v-if="repoMapSummaryData.branch">
              {{ t('detail.repo_map_summary_branch', { branch: repoMapSummaryData.branch }) }}
            </span>
          </div>
        </div>
        <div class="org-summary__repos">
          <div
            v-for="item in repoMapSummaryData.repos || []"
            :key="'summary-' + item.repo_key"
            class="org-summary__repo"
          >
            <div class="org-summary__repo-head">
              <span class="org-summary__repo-name">{{
                item.full_name || item.repo_key
              }}</span>
              <span
                class="org-repo-map__status"
                :class="'org-repo-map__status--' + summaryRepoStatus(item)"
                >{{ mapStatusLabel(summaryRepoStatus(item)) }}</span
              >
            </div>
            <div class="org-summary__repo-purpose">
              {{ item.purpose || t('detail.repo_map_no_purpose') }}
            </div>
            <div v-if="(item.domains || []).length" class="org-cap__chips">
              <span
                v-for="d in (item.domains || []).slice(0, 8)"
                :key="'summary-tag-' + item.repo_key + '-' + d"
                >{{ d }}</span
              >
            </div>
          </div>
        </div>
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="showRepoMapSummary = false">
          {{ ct('common.close') }}
        </AGButton>
        <a
          v-if="repoMapSummaryData.url"
          class="org-repo__link org-repo__link--btn"
          :href="repoMapSummaryData.url"
          target="_blank"
          rel="noopener"
        >
          {{ t('detail.repo_map_summary_open') }} ↗
        </a>
      </template>
    </AGModal>
  </div>
</template>

<script setup>
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import AGCard from '@/components/ui/AGCard.vue'
import AGButton from '@/components/ui/AGButton.vue'
import AGModal from '@/components/ui/AGModal.vue'
import AGInput from '@/components/ui/AGInput.vue'
import AGSelect from '@/components/ui/AGSelect.vue'
import BindDialog from '@/components/BindDialog.vue'

const G = globalThis

const props = defineProps({
  // 组织管理列表页不再作为入口：从右上角「管理」直接进入某个组织的详情。
  initialDetailId: { type: [String, Number], default: null },
  // 从组织下拉「＋ 创建组织」进入时，直接打开创建弹窗，不展示列表页。
  initialCreate: { type: Boolean, default: false },
  // 全局悬浮创建弹窗模式：只渲染弹窗，不渲染组织列表/详情（供任意页面直接打开）。
  modalOnly: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'created'])
const currentOrgId = computed(() => {
  try {
    return localStorage.getItem('current_org_id') || ''
  } catch (e) {
    return ''
  }
})
const isCurrentOrg = computed(
  () => !!detailId.value && String(detailId.value) === String(currentOrgId.value)
)

const orgs = ref([])
const loading = ref(true)
const error = ref('')
const detailId = ref(props.initialDetailId ? String(props.initialDetailId) : null)
const createOnly = computed(() => !!props.initialCreate && !detailId.value)
const detail = ref(null)
const members = ref([])
const repos = ref([])
const repoMap = ref([])
const repoMapSummary = ref({ total: 0, ready: 0, coverage: 0 })
const mapBusy = ref(false)
const showBindProvider = ref(false)
const bindProviderTarget = ref('')
const authProviders = ref([])
const showTransfer = ref(false)
const transferRepoRow = ref(null)
const transferTargetId = ref('')
const transferOrgs = ref([])
const transferring = ref(false)
const transferOrgOptions = computed(() =>
  (transferOrgs.value || []).map((o) => ({
    value: String(o.id),
    label: o.name || o.slug || String(o.id),
  }))
)
const showBindRepo = ref(false)
const bindRepoLoading = ref(false)
const bindRepoOptions = ref([])
const bindRepoSearch = ref('')
const bindRepoSelected = ref([])
const bindRepoSubmitting = ref(false)
const showMapSummary = ref(false)
const mapSummaryTitle = ref('')
const mapSummary = ref({})
const mapSummaryUrl = ref('')
const showRepoMapSummary = ref(false)
const repoMapSummaryData = ref({})
const detailLoading = ref(false)
const detailError = ref('')
const showCreate = ref(false)
const showAddMember = ref(false)
const showEdit = ref(false)
const creating = ref(false)
const addingMember = ref(false)
const suggestions = ref([])
const roleOpts = [
  { value: 'member', label: t('add_member.role_member') },
  { value: 'admin', label: t('add_member.role_admin') },
]
const createForm = reactive({
  name: '',
  slug: '',
  desc: '',
  repo_provider: '',
  repo_owner: '',
  repo_name: '',
  repo_path: '',
  repo_private: true,
})
const namespaceProviders = ref([])
const namespacesLoading = ref(false)
const repoNameTouched = ref(false)
const repoPathTouched = ref(false)
const addMemberForm = reactive({ username: '', role: 'member' })
const editForm = reactive({
  name: '',
  description: '',
  max_members: 50,
  max_workers: 10,
  isPersonal: false,
})

function t(key, params) {
  return G.I18N.t('organizations.' + key, params)
}
function ct(key, params) {
  return G.I18N.t(key, params)
}
roleOpts[0].label = t('add_member.role_member')
roleOpts[1].label = t('add_member.role_admin')

// ── 组织仓库创建：平台账号 / 所属空间 ──
function platformIcon(provider) {
  return typeof G.platformIcon === 'function' ? G.platformIcon(provider, 16) : ''
}
function accountInitial(account) {
  const name = (account && (account.login || account.name)) || '?'
  return String(name).charAt(0).toUpperCase()
}
function repoProvider(repo) {
  const key = String((repo && repo.repo_key) || '')
  const i = key.indexOf(':')
  return i > 0 ? key.slice(0, i) : 'gitcode'
}
function repoIcon(repo) {
  return typeof G.platformIcon === 'function' ? G.platformIcon(repoProvider(repo), 16) : '📦'
}
function repoPageUrl(repo) {
  const key = String((repo && repo.repo_key) || '')
  const provider = repoProvider(repo)
  const host =
    (typeof G.platformHost === 'function' && G.platformHost(provider)) || 'gitcode.com'
  const fullName =
    String((repo && repo.full_name) || '').trim() || key.split(':').slice(-1)[0]
  if (!fullName) return '#'
  return 'https://' + host + '/' + fullName
}
const orgRepo = computed(() => repos.value.find((r) => !!r.is_org_repo) || null)
const linkedRepos = computed(() => repos.value.filter((r) => !r.is_org_repo))
const repoMapRows = computed(() => repoMap.value || [])
const canManageRepos = computed(
  () => !!detail.value && !detail.value.is_personal &&
    (detail.value.role === 'owner' || detail.value.role === 'admin')
)
const filteredBindRepos = computed(() => {
  const q = String(bindRepoSearch.value || '').trim().toLowerCase()
  return (bindRepoOptions.value || []).filter((r) => {
    if (!q) return true
    return (
      String(r.full_name || '').toLowerCase().includes(q) ||
      String(r.repo_key || '').toLowerCase().includes(q)
    )
  })
})

function shortRepo(key) {
  return String(key || '').replace(/^[^:]+:/, '')
}
function _summarySection(md, title) {
  const text = String(md || '')
  const head = text.search(new RegExp('^##[ \\t]*' + title + '[ \\t]*$', 'm'))
  if (head < 0) return ''
  const after = text.slice(head).replace(/^[^\n]*\n?/, '')
  const next = after.search(/^##[ \t]+/m)
  return next >= 0 ? after.slice(0, next) : after
}
const mapSummaryCapabilities = computed(() => {
  const body = _summarySection(mapSummary.value.summary_md, '负责的功能')
  const items = String(body || '')
    .split('\n')
    .map((line) => line.replace(/^[-*+]\s*/, '').trim())
    .filter((line) => line && !/^（.*）$/.test(line))
  if (items.length) return items.slice(0, 12)
  return (mapSummary.value.domains || []).filter(Boolean).slice(0, 6)
})
const mapSummaryInterfaces = computed(() =>
  (mapSummary.value.interfaces || []).filter(Boolean).slice(0, 12)
)
const mapSummaryDepends = computed(() =>
  (mapSummary.value.depends_on || [])
    .filter((item) => item && !['n/a', 'none'].includes(String(item).toLowerCase()))
    .slice(0, 10)
)
const mapSummaryNotOwns = computed(() =>
  (mapSummary.value.not_owns || []).filter(Boolean).slice(0, 10)
)
const mapSummaryTags = computed(() => {
  const out = []
  const seen = new Set()
  for (const item of [
    ...(mapSummary.value.domains || []),
    ...(mapSummary.value.keywords || []),
  ]) {
    const text = String(item || '').trim()
    if (!text || text.length > 14 || seen.has(text)) continue
    seen.add(text)
    out.push(text)
    if (out.length >= 12) break
  }
  return out
})
const mapConfidencePct = computed(() =>
  Math.round(Number(mapSummary.value.confidence || 0) * 100)
)
const mapAnalyzedText = computed(() => {
  const text = String(mapSummary.value.analyzed_at || '')
  return text ? text.slice(0, 16).replace('T', ' ') : ''
})
function repoName(repo) {
  return (repo && (repo.repo_name || repo.name)) ||
    String((repo && (repo.full_name || '')) || '').split('/')[1] ||
    String((repo && repo.repo_key) || '')
}
function repoMapItem(repo) {
  const key = String((repo && repo.repo_key) || '')
  const item = repoMap.value.find((x) => x.repo_key === key) || null
  if (!item) return null
  // AI 台账优先：链接/生成地图现在都走 AI，地图 status 可能仍是上一次的 ready。
  const ai = String(item.ai_status || '')
  const aiToMap = { queued: 'queued', running: 'analyzing', succeeded: 'ready', failed: 'failed' }
  return {
    ...item,
    status: aiToMap[ai] || item.status,
    error: ai === 'failed' ? (item.ai_error || item.error) : item.error,
    analyzed_at: item.ai_analyzed_at || item.analyzed_at,
  }
}
function openRepoWorkspace(repo) {
  const fullName = String((repo && repo.full_name) || '').trim()
  const repoKey = String((repo && repo.repo_key) || '').trim()
  const parts = fullName.split('/')
  if (parts.length < 2) {
    G.app.showToast(t('detail.open_workspace_failed'))
    return
  }
  // 进入仓库工作台前先把当前组织切到该仓库所属组织，避免请求带着旧 X-Org-ID。
  try {
    localStorage.setItem('current_org_id', String(detailId.value || ''))
  } catch (e) {
    /* ignore */
  }
  if (typeof G.selectRepo === 'function') {
    G.selectRepo(fullName, parts[0], parts[1], repoKey)
  } else if (G.app && typeof G.app.switchRepo === 'function') {
    G.app.switchRepo(repoKey)
  }
}
function autoNameFromSlug() {
  const raw = String(createForm.slug || createForm.name || '').trim().toLowerCase()
  const slug = raw.replace(/[^a-z0-9_.-]+/g, '-').replace(/^-+|-+$/g, '')
  // 新组织仓库名默认使用组织 slug，不再自动加 org- 前缀（显式输入仍优先）。
  return slug ? slug.slice(0, 60).replace(/-+$/g, '') : ''
}
const autoRepoName = computed(() => autoNameFromSlug())
const currentNamespaceProvider = computed(() => {
  return (
    namespaceProviders.value.find(
      (p) => p.provider === createForm.repo_provider && p.connected
    ) || null
  )
})
const personalNamespaces = computed(() => {
  const p = currentNamespaceProvider.value
  return ((p && p.namespaces) || []).filter((ns) => ns.type !== 'org')
})
const orgNamespaces = computed(() => {
  const p = currentNamespaceProvider.value
  return ((p && p.namespaces) || []).filter((ns) => ns.type === 'org')
})
const currentOwner = computed(() => {
  const p = currentNamespaceProvider.value
  const list = (p && p.namespaces) || []
  return list.find((ns) => ns.login === createForm.repo_owner) || list[0] || null
})
function openCreate() {
  createForm.name = ''
  createForm.slug = ''
  createForm.desc = ''
  createForm.repo_provider = ''
  createForm.repo_owner = ''
  createForm.repo_name = ''
  createForm.repo_path = ''
  createForm.repo_private = true
  repoNameTouched.value = false
  repoPathTouched.value = false
  showCreate.value = true
  loadNamespaces()
}

function selectProvider(p) {
  if (!p) return
  if (!p.connected) {
    openProviderBind(p.provider)
    return
  }
  createForm.repo_provider = p.provider
  const namespaces = p.namespaces || []
  const personal = namespaces.find((ns) => ns.type !== 'org')
  createForm.repo_owner = (personal && personal.login) || (namespaces[0] && namespaces[0].login) || ''
  if (!repoNameTouched.value) {
    createForm.repo_name = autoRepoName.value
    repoPathTouched.value = false
  }
  if (!repoPathTouched.value) createForm.repo_path = createForm.repo_name
}

function openProviderBind(provider) {
  bindProviderTarget.value = provider
  G.API.get('/auth/providers')
    .then(function (pd) {
      authProviders.value = (pd && pd.providers) || []
    })
    .catch(function () {
      authProviders.value = []
    })
    .finally(function () {
      showBindProvider.value = true
    })
}
function onProviderBound(provider) {
  showBindProvider.value = false
  const links =
    G.app && typeof G.app.refreshRepoLinks === 'function'
      ? G.app.refreshRepoLinks()
      : Promise.resolve(null)
  Promise.resolve(links)
    .then(function () {
      return G.API.get('/auth/profile').catch(function () { return null })
    })
    .then(function () {
      loadNamespaces(true)
    })
}

// 创建弹窗的空间数据预热：gitcode+github 两次外部 VCS 调用约 1.5~2.5s，
// 页面一进来就后台预热并配合后端 60s 缓存，打开创建弹窗时通常已就绪。
// 用 window 挂载（而不是模块变量）：<script setup> 顶层按实例执行，
// 「组织下拉 → 创建」会新起一个 modalOnly 实例，必须跨实例复用同一个请求。
function fetchNamespaces(force) {
  var now = Date.now()
  if (force || !G.__orgNsPromise || now - (G.__orgNsAt || 0) > 60000) {
    G.__orgNsAt = now
    G.__orgNsPromise = G.API.get('/orgs/repo-namespaces').catch(function (e) {
      G.__orgNsPromise = null
      throw e
    })
  }
  return G.__orgNsPromise
}

function loadNamespaces(force) {
  namespacesLoading.value = true
  namespaceProviders.value = []
  fetchNamespaces(force)
    .then(function (d) {
      namespacesLoading.value = false
      namespaceProviders.value = (d && d.providers) || []
      const connected = namespaceProviders.value.filter((p) => p.connected)
      const selected = connected.find((p) => p.provider === createForm.repo_provider)
      if (selected) {
        selectProvider(selected)
      } else if (connected.length) {
        selectProvider(connected[0])
      } else {
        createForm.repo_provider = ''
        createForm.repo_owner = ''
      }
    })
    .catch(function (e) {
      namespacesLoading.value = false
      namespaceProviders.value = []
      createForm.repo_provider = ''
      createForm.repo_owner = ''
      G.app.showToast(t('create.repo_load_failed', { msg: (e && e.message) || e }))
    })
}

function onRepoNameInput(ev) {
  repoNameTouched.value = true
  const value = ev && ev.target ? ev.target.value : createForm.repo_name
  if (!repoPathTouched.value) createForm.repo_path = value
}

watch(
  () => [createForm.slug, createForm.name],
  function () {
    if (!repoNameTouched.value) {
      createForm.repo_name = autoRepoName.value
      if (!repoPathTouched.value) createForm.repo_path = createForm.repo_name
    }
  }
)

function load() {
  loading.value = true
  error.value = ''
  G.API.get('/orgs')
    .then(function (d) {
      orgs.value = d || []
      loading.value = false
    })
    .catch(function (e) {
      error.value = t('load_failed', { msg: e.message || e })
      loading.value = false
    })
}

function openDetail(id) {
  detailId.value = id
  loadDetail(id)
}
function closeDetail() {
  // 详情页返回统一回工作台主页面（有当前仓库→dashboard，无仓库→仓库大厅），
  // 不再回到“组织列表”页。
  detailId.value = null
  detail.value = null
  G.app._accountGoBack()
}
function closeCreate() {
  showCreate.value = false
  if (props.modalOnly) emit('close')
}
// provisioning 是异步的：轮询到 ready/failed 再刷新详情，避免页面先加载空仓库地图
let _provTimer = null
function _stopProvisionPoll() {
  if (_provTimer) {
    clearTimeout(_provTimer)
    _provTimer = null
  }
}
function _waitProvision(orgId, cb) {
  _stopProvisionPoll()
  var tries = 0
  function tick() {
    G.API.get('/orgs/' + orgId + '/provision')
      .then(function (p) {
        var st = String((p && p.provision_status) || '')
        if (st === 'ready' || st === 'failed' || st === 'none') {
          _provTimer = null
          cb(p || {})
          return
        }
        if (++tries >= 60) {
          _provTimer = null
          cb(p || {})
          return
        }
        _provTimer = setTimeout(tick, 1000)
      })
      .catch(function () {
        if (++tries >= 60) {
          _provTimer = null
          cb({})
          return
        }
        _provTimer = setTimeout(tick, 1500)
      })
  }
  tick()
}

function loadDetail(id) {
  _stopMapPoll()
  _stopProvisionPoll()
  mapBusy.value = false
  detailLoading.value = true
  detailError.value = ''
  repos.value = []
  repoMap.value = []
  repoMapSummary.value = { total: 0, ready: 0, coverage: 0 }
  Promise.all([
    G.API.get('/orgs/' + id),
    G.API.get('/orgs/' + id + '/members'),
    // 个人组织没有关联仓库管理，接口会 400；这里统一降级为空列表。
    G.API.get('/orgs/' + id + '/repos').catch(function () {
      return { repos: [] }
    }),
    G.API.get('/orgs/' + id + '/repo-map').catch(function () {
      return { repos: [], summary: { total: 0, ready: 0, coverage: 0 } }
    }),
  ])
    .then(function (results) {
      detail.value = results[0]
      members.value = results[1] || []
      var mapData = results[3] || {}
      repoMap.value = mapData.repos || []
      repoMapSummary.value = mapData.summary || { total: 0, ready: 0, coverage: 0 }
      var list = (results[2] && results[2].repos) || []
      list.sort(function (a, b) {
        var ao = a.is_org_repo ? 0 : 1
        var bo = b.is_org_repo ? 0 : 1
        if (ao !== bo) return ao - bo
        return String(a.full_name || a.repo_key).localeCompare(
          String(b.full_name || b.repo_key)
        )
      })
      repos.value = list
      detailLoading.value = false
      var _st = String((results[0] && results[0].provision_status) || '')
      if (_st && _st !== 'ready' && _st !== 'none' && _st !== 'failed') {
        _waitProvision(String(id), function () {
          loadDetail(id)
        })
      }
    })
    .catch(function (e) {
      detailError.value = t('load_failed', { msg: e.message || e })
      detailLoading.value = false
    })
}

function mapStatusLabel(status) {
  var key = 'detail.repo_map_status_' + (status || 'none')
  var label = t(key)
  return label === 'organizations.' + key ? status || 'none' : label
}
function summaryRepoStatus(item) {
  var raw = String((item && item.ai_status) || '')
  var mapped = {
    queued: 'queued',
    running: 'analyzing',
    succeeded: 'ready',
    failed: 'failed',
  }
  return mapped[raw] || String((item && item.status) || 'none')
}
let _mapPollTimer = null
function _stopMapPoll() {
  if (_mapPollTimer) {
    clearInterval(_mapPollTimer)
    _mapPollTimer = null
  }
}
function loadRepoMap() {
  if (!detailId.value) return Promise.resolve()
  return G.API.get('/orgs/' + detailId.value + '/repo-map')
    .then(function (d) {
      repoMap.value = (d && d.repos) || []
      repoMapSummary.value = (d && d.summary) || { total: 0, ready: 0, coverage: 0 }
    })
    .catch(function () {
      /* ignore */
    })
}
function openBindRepo() {
  showBindRepo.value = true
  bindRepoSearch.value = ''
  bindRepoSelected.value = []
  bindRepoOptions.value = []
  bindRepoLoading.value = true
  G.API.get('/orgs/' + detailId.value + '/repos/available')
    .then(function (d) {
      const rows = (d && d.repos) || []
      // 已关联的、组织自动仓库不进入可关联列表。
      bindRepoOptions.value = rows.filter(
        (r) => !r.bound_here && !r.is_org_repo
      )
      bindRepoLoading.value = false
    })
    .catch(function (e) {
      bindRepoLoading.value = false
      bindRepoOptions.value = []
      G.app.showToast((e && e.message) || String(e))
    })
}
function toggleBindRepo(key) {
  const list = bindRepoSelected.value.slice()
  const idx = list.indexOf(key)
  if (idx >= 0) list.splice(idx, 1)
  else list.push(key)
  bindRepoSelected.value = list
}
function submitBindRepos() {
  const keys = bindRepoSelected.value.slice()
  if (!keys.length) return G.app.showToast(ct('organizations.workbench.repos.picker_none'))
  bindRepoSubmitting.value = true
  G.API.post('/orgs/' + detailId.value + '/repos/batch', { repo_keys: keys }, 120000)
    .then(function (res) {
      bindRepoSubmitting.value = false
      showBindRepo.value = false
      const results = (res && res.results) || []
      const ok = results.filter((x) => x.ok).length
      const failed = results.length - ok
      if (failed) {
        const first = results.find((x) => !x.ok) || {}
        G.app.showToast(
          ct('organizations.workbench.repos.picker_partial', {
            ok: ok,
            failed: failed,
            msg: first.message || first.error_code || '',
          })
        )
      } else {
        G.app.showToast(ct('organizations.workbench.repos.picker_success', { n: ok }))
      }
      loadDetail(detailId.value)
      mapBusy.value = true
      setTimeout(loadRepoMap, 1200)
      _startMapPoll()
    })
    .catch(function (e) {
      bindRepoSubmitting.value = false
      G.app.showToast((e && e.message) || String(e))
    })
}
function openTransfer(repo) {
  if (!repo || !repo.repo_key) return
  transferRepoRow.value = repo
  transferTargetId.value = ''
  transferring.value = false
  G.API.get('/orgs')
    .then(function (list) {
      transferOrgs.value = (list || []).filter(function (o) {
        return (
          o &&
          !o.is_personal &&
          String(o.id) !== String(detailId.value) &&
          (o.role === 'owner' || o.role === 'admin')
        )
      })
      if (!transferOrgs.value.length) {
        G.app.showToast(ct('organizations.workbench.repos.no_transfer_target'))
        return
      }
      showTransfer.value = true
    })
    .catch(function (e) {
      G.app.showToast((e && e.message) || String(e))
    })
}

function submitTransfer() {
  if (!transferTargetId.value) {
    G.app.showToast(ct('organizations.workbench.repos.transfer_target_required'))
    return
  }
  if (!transferRepoRow.value) return
  transferring.value = true
  G.API.post(
    '/orgs/' +
      detailId.value +
      '/repos/' +
      encodeURIComponent(transferRepoRow.value.repo_key) +
      '/transfer',
    { target_org_id: transferTargetId.value }
  )
    .then(function () {
      transferring.value = false
      showTransfer.value = false
      G.app.showToast(ct('organizations.workbench.repos.transfer_success'))
      loadDetail(detailId.value)
    })
    .catch(function (e) {
      transferring.value = false
      G.app.showToast((e && e.message) || String(e))
    })
}

function unbindRepo(repo) {
  if (!repo || !repo.repo_key) return
  const name = repo.full_name || repo.repo_key
  G._grapeConfirm({
    title: ct('organizations.workbench.repos.unbind'),
    message: ct('organizations.workbench.repos.unbind_confirm', { name: name }),
    confirmText: ct('organizations.workbench.repos.unbind'),
    danger: true,
    onConfirm: function () {
      G.API.del(
        '/orgs/' + detailId.value + '/repos/' +
        encodeURIComponent(repo.repo_key) + '?confirm=true'
      )
        .then(function () {
          G.app.showToast(ct('organizations.workbench.repos.unbind_success'))
          loadDetail(detailId.value)
        })
        .catch(function (e) {
          G.app.showToast((e && e.message) || String(e))
        })
    },
  })
}

function _startMapPoll() {
  _stopMapPoll()
  var started = Date.now()
  _mapPollTimer = setInterval(function () {
    loadRepoMap().then(function () {
      var pending = repoMap.value.some(function (x) {
        return x.status === 'analyzing' || x.status === 'queued'
      })
      var elapsed = Date.now() - started
      if (!pending && elapsed > 4000) {
        _stopMapPoll()
        mapBusy.value = false
      }
      if (elapsed > 60000) {
        _stopMapPoll()
        mapBusy.value = false
      }
    })
  }, 2000)
}
function analyzeAllMap() {
  if (!detailId.value || mapBusy.value) return
  mapBusy.value = true
  G.API.post('/orgs/' + detailId.value + '/repo-analysis/run', { mode: 'all' })
    .then(function (d) {
      if (d && d.skipped) {
        G.app.showToast(
          d.reason === 'running'
            ? t('detail.repo_map_running')
            : t('detail.repo_map_no_repos')
        )
        setTimeout(loadRepoMap, 500)
        if (d.reason === 'running') _startMapPoll()
        else mapBusy.value = false
        return
      }
      if (d && d.ok === false) {
        mapBusy.value = false
        G.app.showToast(
          d.reason === 'running'
            ? t('detail.repo_map_running')
            : t('detail.repo_map_failed', { msg: d.reason || '' })
        )
        return
      }
      G.app.showToast(t('detail.repo_map_started', { n: (d && d.queued) || 0 }))
      setTimeout(loadRepoMap, 800)
      _startMapPoll()
    })
    .catch(function (e) {
      mapBusy.value = false
      G.app.showToast(t('detail.repo_map_failed', { msg: (e && e.message) || e }))
    })
}
function analyzeOneMap(item) {
  if (!detailId.value || !item || !item.repo_key) return
  mapBusy.value = true
  G.API.post('/orgs/' + detailId.value + '/repo-analysis/run', {
    mode: 'single',
    repo_key: item.repo_key,
  })
    .then(function (d) {
      if (d && d.ok === false) {
        mapBusy.value = false
        G.app.showToast(
          d.reason === 'running'
            ? t('detail.repo_map_running')
            : t('detail.repo_map_failed', { msg: d.reason || '' })
        )
        return
      }
      G.app.showToast(t('detail.repo_map_started', { n: 1 }))
      setTimeout(loadRepoMap, 500)
      _startMapPoll()
    })
    .catch(function (e) {
      mapBusy.value = false
      G.app.showToast(t('detail.repo_map_failed', { msg: (e && e.message) || e }))
    })
}
function openMapSummary(item) {
  if (!detailId.value || !item) return
  mapSummaryUrl.value = String(item.summary_url || '')
  G.API.get('/orgs/' + detailId.value + '/repo-map/' + encodeURIComponent(item.repo_key))
    .then(function (d) {
      mapSummaryTitle.value = (d && (d.full_name || d.repo_key)) || item.full_name || ''
      mapSummary.value = d || {}
      showMapSummary.value = true
    })
    .catch(function (e) {
      G.app.showToast(t('detail.repo_map_failed', { msg: (e && e.message) || e }))
    })
}

function openRepoMapSummary() {
  if (!detailId.value) return
  G.API.get('/orgs/' + detailId.value + '/repo-map/summary')
    .then(function (d) {
      repoMapSummaryData.value = d || {}
      showRepoMapSummary.value = true
    })
    .catch(function (e) {
      G.app.showToast(t('detail.repo_map_failed', { msg: (e && e.message) || e }))
    })
}

function createOrg() {
  if (!createForm.name.trim()) return G.app.showToast(t('create.name_required'))
  const provider = createForm.repo_provider || ''
  if (provider) {
    const owner = (createForm.repo_owner || '').trim()
    if (!owner) return G.app.showToast(t('create.repo_owner_required'))
    const name = (createForm.repo_name || '').trim()
    const path = provider === 'gitcode' ? (createForm.repo_path || '').trim() : ''
    if (name && !/^[\w\-.]+$/.test(name)) return G.app.showToast(t('create.repo_name_invalid'))
    if (path && !/^[\w\-.]+$/.test(path)) return G.app.showToast(t('create.repo_path_invalid'))
  }
  creating.value = true
  const payload = {
    name: createForm.name.trim(),
    slug: createForm.slug.trim(),
    description: createForm.desc.trim(),
  }
  if (provider) {
    payload.repo_provider = provider
    payload.repo_owner = (createForm.repo_owner || '').trim()
    payload.repo_name = (createForm.repo_name || '').trim()
    payload.repo_path =
      provider === 'gitcode' ? (createForm.repo_path || '').trim() : ''
    payload.repo_private = !!createForm.repo_private
  }
  G.API.post('/orgs', payload)
    .then(function (org) {
      var newId = org && (org.id || org.org_id)
      var st = String((org && org.provision_status) || '')
      var finish = function (p) {
        creating.value = false
        var failed = String((p && p.provision_status) || '') === 'failed'
        if (failed) {
          G.app.showToast(t('create.repo_provision_failed', {
            msg: (p && p.provision_error) || '',
          }))
        } else {
          G.app.showToast(t('create.success'))
        }
        if (props.modalOnly) {
          // 悬浮弹窗由宿主（entries/organizations.js）在收到 created 后负责卸载
          emit('created', org)
          return
        }
        showCreate.value = false
        if (newId) {
          // 创建成功后直接切到新组织并进入管理详情，不再回到列表页
          G.app.switchOrg(String(newId))
        } else {
          load()
        }
      }
      // provisioning（建仓/webhook/场景）是异步的：等它收敛再进入，
      // 否则首屏仓库地图与侧边栏还没有组织仓库，用户需要手动刷新。
      if (newId && st && st !== 'ready' && st !== 'none' && st !== 'failed') {
        _waitProvision(String(newId), finish)
        return
      }
      finish(org)
    })
    .catch(function (e) {
      creating.value = false
      G.app.showToast(t('create.failed', { msg: e.message || e }))
    })
}

let searchTimer = null
function searchUsers() {
  clearTimeout(searchTimer)
  const q = addMemberForm.username.trim()
  if (q.length < 1) {
    suggestions.value = []
    return
  }
  searchTimer = setTimeout(function () {
    G.API.get('/orgs/search/users?q=' + encodeURIComponent(q))
      .then(function (d) {
        suggestions.value = d || []
      })
      .catch(function () {
        suggestions.value = []
      })
  }, 200)
}
function pickUser(u) {
  addMemberForm.username = u.username || ''
  suggestions.value = []
}

function addMember() {
  const username = addMemberForm.username.trim()
  if (!username) return G.app.showToast(t('add_member.username_required'))
  addingMember.value = true
  G.API.post('/orgs/' + detailId.value + '/members', {
    username,
    role: addMemberForm.role,
  })
    .then(function () {
      showAddMember.value = false
      addingMember.value = false
      G.app.showToast(t('add_member.success'))
      loadDetail(detailId.value)
    })
    .catch(function (e) {
      addingMember.value = false
      G.app.showToast(t('add_member.failed', { msg: e.message || e }))
    })
}

function removeMember(m) {
  G._grapeConfirm(
    {
      title: t('remove_member.title'),
      message: t('remove_member.confirm_msg', { name: m.username || m.user_id }),
      confirmText: ct('common.remove'),
      danger: true,
      onConfirm: function () {
        G.API.del('/orgs/' + detailId.value + '/members/' + m.user_id)
          .then(function () {
            G.app.showToast(t('remove_member.success'))
            loadDetail(detailId.value)
          })
          .catch(function (e) {
            G.app.showToast(t('remove_member.failed', { msg: e.message || e }))
          })
      },
    },
    null
  )
}

function switchOrg() {
  G.app.switchOrg(detailId.value)
}

// 删除当前组织后清掉 localStorage 里的组织上下文：否则后续请求继续带
// X-Org-ID=<已删除组织>，整站 API 全部 403，页面停在报错页（2026-11-19 实锤）。
// 先清空让请求自动回落个人组织，再异步把当前组织切成个人空间。
function _resetOrgContextAfterDelete(orgId) {
  var cur = ''
  try { cur = localStorage.getItem('current_org_id') || '' } catch (e) { return }
  if (!cur || String(cur) !== String(orgId)) return
  try { localStorage.removeItem('current_org_id') } catch (e) {}
  try { localStorage.removeItem('repo_member_cache') } catch (e) {}
  try { sessionStorage.removeItem('currentRepo') } catch (e) {}
  var refresh = function () {
    if (G.app && typeof G.app._updateTbOrgName === 'function') G.app._updateTbOrgName()
    if (G.app && typeof G.app.initSidebarRepos === 'function') G.app.initSidebarRepos()
  }
  G.API.get('/orgs')
    .then(function (orgs) {
      var personal = (orgs || []).find(function (o) { return o.is_personal })
      if (personal) {
        try { localStorage.setItem('current_org_id', String(personal.id)) } catch (e) {}
      }
      refresh()
    })
    .catch(refresh)
}

function deleteOrg() {
  G._grapeConfirm(
    {
      title: t('delete_org.title'),
      message: t('delete_org.confirm_msg', { name: detail.value.name || detailId.value }),
      confirmText: t('delete_org.confirm'),
      danger: true,
      onConfirm: function () {
        var deletedId = String(detailId.value || '')
        G.API.del('/orgs/' + detailId.value)
          .then(function () {
            G.app.showToast(t('delete_org.success'))
            _resetOrgContextAfterDelete(deletedId)
            closeDetail()
          })
          .catch(function (e) {
            G.app.showToast(t('delete_org.failed', { msg: e.message || e }))
          })
      },
    },
    null
  )
}

function openEdit() {
  editForm.name = detail.value.name || ''
  editForm.description = detail.value.description || ''
  editForm.max_members = detail.value.max_members || 50
  editForm.max_workers = detail.value.max_workers || 10
  editForm.isPersonal = !!detail.value.is_personal
  showEdit.value = true
}

function saveEdit() {
  if (!editForm.name.trim()) return G.app.showToast(t('create.name_required') || '名称不能为空')
  const payload = { name: editForm.name.trim(), description: editForm.description.trim() }
  if (!editForm.isPersonal) {
    payload.max_members = parseInt(editForm.max_members) || 0
    payload.max_workers = parseInt(editForm.max_workers) || 0
  }
  G.API.put('/orgs/' + detailId.value, payload)
    .then(function () {
      G.app.showToast(t('detail.edit_success') || '组织信息已更新')
      showEdit.value = false
      loadDetail(detailId.value)
    })
    .catch(function (e) {
      G.app.showToast((t('detail.edit_failed') || '更新失败') + ': ' + (e.message || e))
    })
}

load()
// 预热创建组织弹窗的数据源（失败静默，打开弹窗时 loadNamespaces 会再试）
fetchNamespaces(false).catch(function () {})
onUnmounted(_stopMapPoll)
onUnmounted(_stopProvisionPoll)

if (!props.modalOnly) {
  load()
}
if (props.initialDetailId) {
  openDetail(String(props.initialDetailId))
}
if (props.initialCreate && !props.initialDetailId) {
  openCreate()
}
</script>

<style scoped>
.org {
  padding: 20px;
  color: var(--c-text);
  font-family: var(--f-family);
}
.sp {
  flex: 1;
}
.org__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--s-4);
}
.org__bar h2 {
  margin: 0;
  font-size: var(--f-head-2);
}
.org__list {
  display: flex;
  flex-direction: column;
  gap: var(--s-3);
}
.org__hint {
  color: var(--c-text-3);
  padding: 20px;
}
.org__hint--center {
  text-align: center;
}
.org__hint--err {
  color: var(--c-danger);
}
.org__card {
  display: flex;
  align-items: center;
  gap: var(--s-3);
  cursor: pointer;
  transition: all 0.15s;
}
.org__card:hover {
  transform: translateY(-1px);
  box-shadow: var(--shadow-2);
}
.org__icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  background: var(--c-bg-elevated);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  flex-shrink: 0;
}
.org__info {
  flex: 1;
  min-width: 0;
}
.org__name {
  font-weight: 600;
  font-size: var(--f-body);
}
.org__badge {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--c-bg-elevated);
  color: var(--c-text-2);
  margin-left: 6px;
}
.org__meta {
  font-size: var(--f-caption);
  color: var(--c-text-3);
}
.org__role {
  font-size: var(--f-caption);
  font-weight: 500;
}
.org__role.role-owner {
  color: var(--c-success);
}
.org__role.role-admin {
  color: var(--c-accent);
}
.org__role.role-member {
  color: var(--c-text-3);
}
.org__back {
  margin-bottom: var(--s-3);
}
.org__dcard {
  margin-bottom: var(--s-4);
}
.org__dhead {
  display: flex;
  align-items: center;
  gap: var(--s-3);
  margin-bottom: var(--s-3);
}
.org__dicon {
  width: 48px;
  height: 48px;
  border-radius: 12px;
  background: var(--c-bg-elevated);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 22px;
}
.org__dname {
  font-weight: 600;
  font-size: var(--f-head-3);
}
.org__desc {
  font-size: var(--f-body);
  color: var(--c-text-2);
  margin-bottom: var(--s-2);
}
.org__dmeta {
  font-size: var(--f-caption);
  color: var(--c-text-3);
}
.org__mhead {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--s-3);
}
.org__mhead h3 {
  margin: 0;
  font-size: var(--f-body);
}
.org__mhead-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.org__mrow {
  display: flex;
  align-items: center;
  gap: var(--s-2);
  padding: var(--s-2) 0;
  border-bottom: 1px solid var(--glass-border);
}
.org__mrow:last-child {
  border-bottom: none;
}
.org__mavatar {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: var(--c-bg-elevated);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  flex-shrink: 0;
}
.org__mname {
  font-size: var(--f-body);
  font-weight: 500;
}
.org__dactions {
  display: flex;
  gap: var(--s-2);
  justify-content: center;
  align-items: center;
  flex-wrap: wrap;
}
.org__form {
  display: flex;
  flex-direction: column;
  gap: var(--s-3);
}
.org__form--create {
  max-height: min(68vh, 640px);
  overflow-y: auto;
  padding-right: 4px;
}
.org__form-row {
  display: flex;
  gap: var(--s-3);
}
.org__field {
  display: flex;
  flex-direction: column;
  gap: var(--s-1);
}
.org__field label {
  font-size: var(--f-caption);
  color: var(--c-text-2);
  font-weight: 600;
}
.org__field input,
.org__field textarea {
  width: 100%;
  padding: var(--s-2) var(--s-3);
  border: 1px solid var(--glass-border);
  border-radius: var(--r-control);
  background: var(--c-bg-elevated);
  color: var(--c-text);
  font-size: 13px;
  box-sizing: border-box;
  outline: none;
}
.org__field input:focus,
.org__field textarea:focus {
  border-color: var(--c-accent);
}
.org__suggest-wrap {
  position: relative;
}
.org__suggest {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  max-height: 160px;
  overflow-y: auto;
  border: 1px solid var(--glass-border);
  border-radius: var(--r-control);
  background: var(--c-bg-elevated);
  z-index: 100;
  box-shadow: var(--shadow-2);
}
.org__suggest-item {
  padding: var(--s-2) var(--s-3);
  cursor: pointer;
  border-bottom: 1px solid var(--glass-border);
  font-size: var(--f-caption);
}
.org__suggest-item:hover {
  background: var(--c-bg);
}
.org__suggest-name {
  font-weight: 500;
}

/* ── 组织仓库创建（对齐 GitCode/GitHub 建仓页）── */
.repo-create {
  display: flex;
  flex-direction: column;
  gap: var(--s-3);
  padding-top: var(--s-3);
  border-top: 1px solid var(--glass-border);
}
.repo-create__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--s-2);
}
.repo-create__title {
  font-size: var(--f-body);
  font-weight: 600;
}
.repo-create__sub {
  margin-top: 2px;
  font-size: var(--f-caption);
  color: var(--c-text-3);
}
.repo-create__providers {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s-2);
}
.repo-plat {
  flex: 1 1 210px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--glass-border);
  border-radius: var(--r-control);
  background: var(--c-bg-elevated);
  color: var(--c-text);
  font-family: var(--f-family);
  font-size: 13px;
  cursor: pointer;
  text-align: left;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.repo-plat:hover:not(:disabled) {
  border-color: var(--c-accent);
}
.repo-plat--active {
  border-color: var(--c-accent);
  box-shadow: inset 0 0 0 1px var(--c-accent);
}
.repo-plat--off,
.repo-plat:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.repo-plat__icon {
  display: inline-flex;
  align-items: center;
}
.repo-plat__name {
  font-weight: 600;
}
.repo-plat__acct {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  color: var(--c-text-2);
  font-size: 12px;
}
.repo-plat__avatar {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  object-fit: cover;
  flex-shrink: 0;
}
.repo-plat__avatar--txt {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--c-bg);
  font-size: 10px;
  font-weight: 600;
}
.repo-plat__login {
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.repo-plat__off {
  margin-left: auto;
  font-size: 11px;
  color: var(--c-text-3);
}
.repo-create__empty {
  padding: 10px 12px;
  border: 1px dashed var(--glass-border);
  border-radius: var(--r-control);
  color: var(--c-text-3);
  font-size: var(--f-caption);
}
.repo-create__field {
  display: flex;
  flex-direction: column;
  gap: var(--s-1);
}
.repo-create__field > label {
  font-size: var(--f-caption);
  color: var(--c-text-2);
  font-weight: 600;
}
.repo-create__hint {
  margin: 0;
  font-size: var(--f-caption);
  color: var(--c-text-3);
}
.repo-create__hint--warn {
  color: var(--c-warn, var(--c-text-3));
}
.repo-owner {
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--glass-border);
  border-radius: var(--r-control);
  padding: 4px 8px;
  background: var(--c-bg-elevated);
}
.repo-owner__avatar {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  object-fit: cover;
  flex-shrink: 0;
}
.repo-owner__avatar--txt {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--c-bg);
  font-size: 11px;
  font-weight: 600;
}
.repo-owner__select {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  color: var(--c-text);
  font-family: var(--f-family);
  font-size: 13px;
  padding: 4px 0;
}
.repo-name {
  display: flex;
  align-items: center;
  border: 1px solid var(--glass-border);
  border-radius: var(--r-control);
  background: var(--c-bg-elevated);
  overflow: hidden;
}
.repo-name:focus-within {
  border-color: var(--c-accent);
}
.repo-name__prefix {
  padding: 0 0 0 10px;
  color: var(--c-text-3);
  font-size: 13px;
  white-space: nowrap;
}
.repo-name input {
  flex: 1;
  min-width: 0;
  padding: 8px 10px 8px 4px;
  border: none;
  outline: none;
  background: transparent;
  color: var(--c-text);
  font-family: var(--f-family);
  font-size: 13px;
}
.repo-vis {
  display: flex;
  gap: var(--s-2);
}
.repo-vis__opt {
  flex: 1;
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--glass-border);
  border-radius: var(--r-control);
  background: var(--c-bg-elevated);
  color: var(--c-text);
  font-family: var(--f-family);
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.repo-vis__opt--active {
  border-color: var(--c-accent);
  box-shadow: inset 0 0 0 1px var(--c-accent);
}
.repo-vis__ic {
  font-size: 14px;
  line-height: 1.4;
}
.repo-vis__txt {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.repo-vis__txt b {
  font-size: 12px;
  font-weight: 600;
}
.repo-vis__txt em {
  font-style: normal;
  font-size: 11px;
  color: var(--c-text-3);
  line-height: 1.35;
}
/* ── 组织详情：描述 / 关联仓库 ── */
.org__desc-block {
  margin: var(--s-2) 0 var(--s-3);
  padding: 10px 12px;
  border-radius: var(--r-control);
  background: var(--c-bg);
}
.org__desc-label {
  margin-bottom: 4px;
  font-size: var(--f-caption);
  color: var(--c-text-3);
  font-weight: 600;
}
.org__desc {
  margin: 0;
  font-size: var(--f-body);
  color: var(--c-text-2);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}
.org-repo-section {
  margin-bottom: var(--s-3);
}
.org-repo-section:last-child {
  margin-bottom: 0;
}
.org-repo-section--org {
  padding: 10px 12px 12px;
  border: 1px solid color-mix(in srgb, var(--c-accent) 38%, var(--glass-border));
  border-radius: var(--r-card);
  background: linear-gradient(
    135deg,
    color-mix(in srgb, var(--c-accent) 10%, transparent),
    color-mix(in srgb, var(--c-accent) 3%, transparent)
  );
}
.org-repo-section__head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.org-repo-section__title {
  font-size: 13px;
  font-weight: 700;
  color: var(--c-text);
}
.org__repolist {
  display: flex;
  flex-direction: column;
  gap: var(--s-2);
}
.org__repolist-empty {
  padding: 14px;
  border: 1px dashed var(--glass-border);
  border-radius: var(--r-control);
  text-align: center;
  color: var(--c-text-3);
  font-size: 12px;
}
.org-repo {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 12px 14px;
  border: 1px solid color-mix(in srgb, var(--c-text) 9%, var(--glass-border));
  border-radius: var(--r-control);
  background: linear-gradient(
    180deg,
    var(--c-bg-elevated) 0%,
    color-mix(in srgb, var(--c-bg) 38%, var(--c-bg-elevated)) 100%
  );
  color: var(--c-text);
  font-family: var(--f-family);
  text-align: left;
  cursor: pointer;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
  transition: border-color 0.15s ease, transform 0.15s ease, box-shadow 0.15s ease;
}
.org-repo:hover {
  border-color: color-mix(in srgb, var(--c-accent) 55%, var(--glass-border));
  transform: translateY(-1px);
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.08);
}
.org-repo__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--c-accent) 10%, var(--c-bg-elevated));
  color: var(--c-accent);
  border: 1px solid color-mix(in srgb, var(--c-accent) 16%, transparent);
  flex-shrink: 0;
}
.org-repo__info {
  display: flex;
  flex-direction: column;
  gap: 3px;
  flex: 1;
  min-width: 0;
}
.org-repo__name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.01em;
}
.org-repo__meta {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: var(--c-text-3);
}
.org-repo__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.org-repo__link {
  padding: 4px 10px;
  border: 1px solid color-mix(in srgb, var(--c-accent) 30%, var(--glass-border));
  border-radius: 999px;
  background: color-mix(in srgb, var(--c-accent) 7%, var(--c-bg-elevated));
  color: var(--c-accent);
  font-size: 11px;
  font-weight: 500;
  text-decoration: none;
  white-space: nowrap;
  transition: background 0.15s ease, border-color 0.15s ease;
}
.org-repo__link:hover {
  background: color-mix(in srgb, var(--c-accent) 13%, var(--c-bg-elevated));
  border-color: var(--c-accent);
  color: var(--c-accent);
}
.org-repo--org {
  border-color: color-mix(in srgb, var(--c-accent) 32%, var(--glass-border));
  background: linear-gradient(
    180deg,
    color-mix(in srgb, var(--c-accent) 6%, var(--c-bg-elevated)) 0%,
    var(--c-bg-elevated) 100%
  );
  box-shadow: 0 4px 16px color-mix(in srgb, var(--c-accent) 8%, transparent);
}
.org-repo:focus-visible {
  outline: 2px solid var(--c-accent);
  outline-offset: 2px;
}
.org__badge--accent {
  color: var(--c-accent);
  font-weight: 600;
}

/* ── 仓库地图 ── */
.org-repo-map__hint {
  margin: 0 0 var(--s-3);
  font-size: var(--f-caption);
  color: var(--c-text-3);
  line-height: 1.45;
}
.org-repo-map {
  display: flex;
  flex-direction: column;
  gap: var(--s-2);
}
.org-repo-map__row {
  display: flex;
  align-items: flex-start;
  gap: var(--s-3);
  padding: 10px 12px;
  border: 1px solid var(--glass-border);
  border-radius: var(--r-control);
  background: var(--c-bg-elevated);
}
.org-repo-map__row--org {
  border-color: color-mix(in srgb, var(--c-accent) 34%, var(--glass-border));
  background: color-mix(in srgb, var(--c-accent) 5%, var(--c-bg-elevated));
}
.org-repo-map__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.org-repo-map__name {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
}
.org-repo-map__purpose {
  font-size: 12px;
  color: var(--c-text-2);
  line-height: 1.45;
}
.org-repo-map__tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.org-repo-map__tag {
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--c-bg);
  color: var(--c-text-3);
  font-size: 10px;
}
.org-repo-map__error {
  color: var(--c-danger);
  font-size: 11px;
}
.org-repo-map__status {
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 600;
  background: var(--c-bg);
  color: var(--c-text-3);
}
.org-repo-map__status--ready {
  color: var(--c-success);
}
.org-repo-map__status--analyzing,
.org-repo-map__status--queued {
  color: var(--c-accent);
}
.org-repo-map__status--failed {
  color: var(--c-danger);
}
.org-repo-map__actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}
/* ── 合并后的关联仓库地图行 ── */
.org-repo--map {
  align-items: flex-start;
}
.org-repo__purpose {
  font-size: 12px;
  color: var(--c-text-2);
  line-height: 1.45;
}
.org-repo--map .org-repo__actions {
  padding-top: 2px;
}

/* ── 能力详情弹窗：结构化、可扫读 ── */
.org-cap {
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 2px 2px 4px;
}
.org-cap__intro {
  padding: 14px 16px;
  border: 1px solid color-mix(in srgb, var(--c-accent) 22%, var(--glass-border));
  border-radius: var(--r-control);
  background: linear-gradient(
    135deg,
    color-mix(in srgb, var(--c-accent) 7%, var(--c-bg-elevated)),
    var(--c-bg-elevated)
  );
}
.org-cap__purpose {
  font-size: 14px;
  font-weight: 500;
  color: var(--c-text);
  line-height: 1.65;
}
.org-cap__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 8px;
  font-size: 11px;
  color: var(--c-text-3);
}
.org-cap__section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.org-cap__label {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  color: var(--c-text-3);
  text-transform: uppercase;
}
.org-cap__list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.org-cap__list li {
  position: relative;
  padding-left: 14px;
  font-size: 13px;
  color: var(--c-text);
  line-height: 1.55;
}
.org-cap__list li::before {
  content: '';
  position: absolute;
  left: 2px;
  top: 7px;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--c-accent);
  opacity: 0.7;
}
.org-cap__list--muted li {
  color: var(--c-text-2);
}
.org-cap__list--muted li::before {
  background: var(--c-text-3);
}
.org-cap__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.org-cap__chips span,
.org-cap__chips code {
  display: inline-flex;
  align-items: center;
  padding: 3px 9px;
  border: 1px solid color-mix(in srgb, var(--c-text) 8%, var(--glass-border));
  border-radius: 6px;
  background: var(--c-bg);
  color: var(--c-text-2);
  font-family: var(--f-mono, ui-monospace, monospace);
  font-size: 11px;
  line-height: 1.5;
}
.org-cap__chips span {
  font-family: var(--f-family);
}
.org-cap__empty {
  padding: 24px 16px;
  border: 1px dashed var(--glass-border);
  border-radius: var(--r-control);
  color: var(--c-text-3);
  font-size: 12px;
  text-align: center;
}
.org-repo__link--btn {
  display: inline-flex;
  align-items: center;
  text-decoration: none;
}
.org-summary__repos {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.org-summary__repo {
  padding: 12px 14px;
  border: 1px solid var(--glass-border);
  border-radius: var(--r-control);
  background: var(--c-bg-elevated);
}
.org-summary__repo-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.org-summary__repo-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--c-text);
  word-break: break-all;
}
.org-summary__repo-purpose {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--c-text-2);
}

/* ── 关联仓库选择弹窗 ── */
.org-bind__search {
  width: 100%;
  padding: var(--s-2) var(--s-3);
  border: 1px solid var(--glass-border);
  border-radius: var(--r-control);
  background: var(--c-bg-elevated);
  color: var(--c-text);
  font-family: var(--f-family);
  font-size: 13px;
  outline: none;
  box-sizing: border-box;
}
.org-bind__search:focus {
  border-color: var(--c-accent);
}
.org-bind__list {
  max-height: min(48vh, 360px);
  overflow-y: auto;
  border: 1px solid var(--glass-border);
  border-radius: var(--r-control);
  padding: 4px;
}
.org-bind__item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: var(--r-control);
  cursor: pointer;
  transition: background 0.15s ease;
}
.org-bind__item:hover {
  background: var(--c-bg);
}
.org-bind__item input[type='checkbox'] {
  width: 15px;
  height: 15px;
  accent-color: var(--c-accent);
  flex-shrink: 0;
}
.org-bind__info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}
.org-bind__name {
  font-size: 13px;
  font-weight: 600;
}
.org-bind__meta {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: var(--c-text-3);
}

/* ── 项目路径：所有者下拉 + 路径输入（对齐 GitCode 新建项目） ── */
.repo-name__input {
  width: 100%;
  padding: var(--s-2) var(--s-3);
  border: 1px solid var(--glass-border);
  border-radius: var(--r-control);
  background: var(--c-bg-elevated);
  color: var(--c-text);
  font-family: var(--f-family);
  font-size: 13px;
  box-sizing: border-box;
  outline: none;
}
.repo-name__input:focus {
  border-color: var(--c-accent);
}
.repo-path {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border: 1px solid var(--glass-border);
  border-radius: var(--r-control);
  background: var(--c-bg-elevated);
}
.repo-path:focus-within {
  border-color: var(--c-accent);
}
.repo-path__avatar {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  object-fit: cover;
  flex-shrink: 0;
}
.repo-path__avatar--txt {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--c-bg);
  font-size: 10px;
  font-weight: 600;
}
.repo-path__owner {
  max-width: 45%;
  border: none;
  outline: none;
  background: transparent;
  color: var(--c-text);
  font-family: var(--f-family);
  font-size: 13px;
  font-weight: 600;
  padding: 4px 0;
}
.repo-path__sep {
  color: var(--c-text-3);
  flex-shrink: 0;
}
.repo-path__input {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  color: var(--c-text);
  font-family: var(--f-family);
  font-size: 13px;
  padding: 4px 0;
}
.repo-path__fixed {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--c-text);
  font-size: 13px;
}
</style>
