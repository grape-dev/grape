<template>
  <div class="org">
    <!-- 列表视图 -->
    <template v-if="!detailId">
      <div class="org__bar">
        <h2>{{ t('title') }}</h2>
        <div class="sp"></div>
        <!-- Issue #456：从个人中心进组织页后可一键回工作台 -->
        <AGButton variant="plain" @click="G.app._accountGoBack()">{{
          t('back_to_workspace')
        }}</AGButton>
        <AGButton @click="showCreate = true">{{ t('create_org') }}</AGButton>
      </div>
      <div class="org__list">
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
    <template v-else>
      <div class="org__back">
        <AGButton variant="plain" size="sm" @click="closeDetail">{{ t('detail.back') }}</AGButton>
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
          <div class="org__desc">{{ detail.description || t('detail.no_description') }}</div>
          <div class="org__dmeta">
            {{ t('detail.role_label') }}: {{ detail.role || '' }}
            <template v-if="!detail.is_personal">
              · {{ t('detail.member_limit') }}: {{ detail.max_members || 50 }} ·
              {{ t('detail.worker_limit') }}: {{ detail.max_workers || 10 }}
            </template>
          </div>
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
          <AGButton @click="switchOrg">{{ t('detail.switch_to') }}</AGButton>
        </AGCard>
      </template>
    </template>

    <!-- 创建组织弹窗 -->
    <AGModal :open="showCreate" :title="t('create.title')" @close="showCreate = false">
      <div class="org__form">
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
      </div>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="showCreate = false">{{
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
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
import AGCard from '@/components/ui/AGCard.vue'
import AGButton from '@/components/ui/AGButton.vue'
import AGModal from '@/components/ui/AGModal.vue'
import AGInput from '@/components/ui/AGInput.vue'
import AGSelect from '@/components/ui/AGSelect.vue'

const G = globalThis

const orgs = ref([])
const loading = ref(true)
const error = ref('')
const detailId = ref(null)
const detail = ref(null)
const members = ref([])
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
const createForm = reactive({ name: '', slug: '', desc: '' })
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
  detailId.value = null
  detail.value = null
  load()
}
function loadDetail(id) {
  detailLoading.value = true
  detailError.value = ''
  Promise.all([G.API.get('/orgs/' + id), G.API.get('/orgs/' + id + '/members')])
    .then(function (results) {
      detail.value = results[0]
      members.value = results[1] || []
      detailLoading.value = false
    })
    .catch(function (e) {
      detailError.value = t('load_failed', { msg: e.message || e })
      detailLoading.value = false
    })
}

function createOrg() {
  if (!createForm.name.trim()) return G.app.showToast(t('create.name_required'))
  creating.value = true
  G.API.post('/orgs', {
    name: createForm.name.trim(),
    slug: createForm.slug.trim(),
    description: createForm.desc.trim(),
  })
    .then(function () {
      showCreate.value = false
      creating.value = false
      G.app.showToast(t('create.success'))
      load()
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

function deleteOrg() {
  G._grapeConfirm(
    {
      title: t('delete_org.title'),
      message: t('delete_org.confirm_msg', { name: detail.value.name || detailId.value }),
      confirmText: t('delete_org.confirm'),
      danger: true,
      onConfirm: function () {
        G.API.del('/orgs/' + detailId.value)
          .then(function () {
            G.app.showToast(t('delete_org.success'))
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
</style>
