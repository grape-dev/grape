<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>角色权限配置</h1>
      <span class="badge">{{ roles.length }} 个角色</span>
    </div>
    <div style="padding:20px 28px">
      <div class="rpc-layout" v-if="loaded">
        <!-- 顶部角色列表（chip 折行，对齐 VOD） -->
        <div class="rpc-top">
          <div class="rpc-top-header">
            <span>角色列表</span>
            <button class="btn btn-sm btn-primary" @click="openCreate">+ 新增</button>
          </div>
          <div class="rpc-role-row">
            <div
              v-for="r in roles" :key="r.code"
              class="rpc-role-item" :class="{ active: selected?.id === r.id }"
              @click="select(r)">
              <span class="rpc-role-name">{{ r.name }}</span>
              <span class="rpc-role-right">
                <span class="rpc-role-code">{{ r.code }}</span>
                <button v-if="r.isProtected !== 1" class="rpc-role-del" @click.stop="deleteRole(r)" title="删除角色">×</button>
              </span>
            </div>
          </div>
        </div>

        <!-- 权限区域（选中角色后显示） -->
        <div class="rpc-body" v-if="selected">
          <div class="rpc-body-header">
            <h4>{{ selected.name }}</h4>
            <span class="rpc-right-code">{{ selected.code }}</span>
            <span v-if="isProtected" class="rpc-locked">🔒 系统保护角色，权限不可更改</span>
          </div>

          <div class="rpc-perm-row">
            <!-- 菜单权限 -->
            <div class="rpc-section">
              <h5>菜单权限</h5>
              <p class="rpc-section-desc">勾选的页面将出现在该角色的导航菜单中</p>
              <div class="rpc-menu-tree">
                <template v-for="group in menuTree" :key="'m-' + group.code">
                  <div v-if="group.children && group.children.length" class="rpc-tree-group">
                    <label class="rpc-tree-row rpc-tree-parent" @click.stop>
                      <input type="checkbox"
                        :checked="allChecked(group)"
                        :indeterminate.prop="isIndeterminate(group)"
                        :disabled="isProtected"
                        @change="toggleGroup(group, $event.target.checked)" />
                      <span class="rpc-tree-name">{{ group.name }}</span>
                    </label>
                    <label v-for="child in group.children" :key="'mc-' + child.code" class="rpc-tree-row rpc-tree-child">
                      <input type="checkbox"
                        :checked="checkedCodes.includes(child.code)"
                        :disabled="isProtected"
                        @change="toggleChild(child.code)" />
                      <span class="rpc-tree-name">{{ child.name }}</span>
                      <span class="rpc-hint" v-if="child.path">{{ child.path }}</span>
                    </label>
                  </div>
                  <label v-else class="rpc-tree-row rpc-tree-leaf">
                    <input type="checkbox"
                      :checked="checkedCodes.includes(group.code)"
                      :disabled="isProtected"
                      @change="toggleChild(group.code)" />
                    <span class="rpc-tree-name">{{ group.name }}</span>
                  </label>
                </template>
              </div>
            </div>

            <!-- 功能按钮权限（占位：admin 后端暂未声明按钮级权限） -->
            <div class="rpc-section">
              <h5>功能按钮权限</h5>
              <p class="rpc-section-desc">仅声明了功能按钮的页面出现在下方。未声明的页面默认所有按钮全开放。</p>
              <div class="rpc-func-list">
                <div class="rpc-func-empty">暂无已声明的功能按钮权限。所有页面按钮默认全开放。</div>
              </div>
            </div>
          </div>

          <div class="rpc-api-note">
            ℹ️ 接口权限由所选菜单权限自动推导，无需手动配置。
          </div>

          <div class="rpc-actions" v-if="!isProtected">
            <button class="btn btn-primary" @click="savePerms" :disabled="saving">
              {{ saving ? '保存中...' : '保存权限' }}
            </button>
            <span class="rpc-msg" v-if="saveMsg" :class="{ success: saveOk, error: !saveOk }">{{ saveMsg }}</span>
          </div>
        </div>

        <div class="rpc-body rpc-empty" v-else>
          <p>请从上方选择一个角色</p>
        </div>
      </div>
      <div v-else class="loading-spinner">加载中...</div>
    </div>

    <!-- 新增角色弹窗 -->
    <div v-if="showCreate" class="modal" @click.self="showCreate = false">
      <div class="modal-box" style="max-width:420px">
        <h4>新增角色</h4>
        <p v-if="createError" class="err">{{ createError }}</p>
        <div class="form-row">
          <label>角色代码 <span class="t3" style="font-weight:normal">（英文标识，如 CUSTOM_ROLE）</span></label>
          <input v-model="newCode" placeholder="例：CUSTOM_ROLE" />
        </div>
        <div class="form-row">
          <label>角色名称</label>
          <input v-model="newName" placeholder="例：自定义角色" />
        </div>
        <div class="modal-actions">
          <button class="btn" @click="showCreate = false">取消</button>
          <button class="btn btn-primary" @click="createRole" :disabled="creating">{{ creating ? '创建中...' : '创建' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import http from '../api'

const roles = ref([])
const menuTree = ref([])
const selected = ref(null)
const checkedCodes = ref([])
const newCode = ref('')
const newName = ref('')
const showCreate = ref(false)
const createError = ref('')
const creating = ref(false)
const saving = ref(false)
const saveMsg = ref('')
const saveOk = ref(false)
const loaded = ref(false)

const isProtected = computed(() => selected.value?.isProtected === 1)

async function loadRoles() {
  const { data } = await http.get('/roles')
  roles.value = data
  if (selected.value) {
    const s = roles.value.find(r => r.id === selected.value.id)
    if (s) { selected.value = s; syncChecked() }
  }
  loaded.value = true
}
async function loadMenus() {
  const { data } = await http.get('/menus')
  menuTree.value = data
}
function menuIdByCode(code) {
  for (const g of menuTree.value) {
    if (g.code === code) return g.id
    const c = g.children.find(x => x.code === code)
    if (c) return c.id
  }
  return null
}
function syncChecked() {
  const allCodes = []
  menuTree.value.forEach(g => allCodes.push(g.code, ...g.children.map(c => c.code)))
  checkedCodes.value = allCodes.filter(code => selected.value.menuIds.includes(menuIdByCode(code)))
}
function select(r) {
  selected.value = r
  checkedCodes.value = []
  saveMsg.value = ''
  syncChecked()
}
function allChecked(group) {
  return group.children.every(c => checkedCodes.value.includes(c.code))
}
function isIndeterminate(group) {
  const some = group.children.some(c => checkedCodes.value.includes(c.code))
  const all = group.children.every(c => checkedCodes.value.includes(c.code))
  return some && !all
}
function toggleGroup(group, checked) {
  group.children.forEach(c => {
    const i = checkedCodes.value.indexOf(c.code)
    if (checked && i < 0) checkedCodes.value.push(c.code)
    if (!checked && i >= 0) checkedCodes.value.splice(i, 1)
  })
}
function toggleChild(code) {
  const i = checkedCodes.value.indexOf(code)
  if (i >= 0) checkedCodes.value.splice(i, 1)
  else checkedCodes.value.push(code)
}
async function savePerms() {
  const ids = []
  menuTree.value.forEach(g => {
    if (checkedCodes.value.includes(g.code)) ids.push(g.id)
    g.children.forEach(c => { if (checkedCodes.value.includes(c.code)) ids.push(c.id) })
  })
  saving.value = true
  saveMsg.value = ''
  try {
    await http.put(`/roles/${selected.value.id}/permissions`, { menuIds: ids })
    saveOk.value = true
    saveMsg.value = '权限保存成功！'
    loadRoles()
  } catch (e) {
    saveOk.value = false
    saveMsg.value = '保存失败: ' + (e.response?.data?.message || e.message)
  } finally {
    saving.value = false
  }
}
function openCreate() {
  newCode.value = ''; newName.value = ''
  createError.value = ''
  showCreate.value = true
}
async function createRole() {
  if (!newCode.value || !newName.value) {
    createError.value = '角色代码和名称不能为空'
    return
  }
  creating.value = true
  createError.value = ''
  try {
    await http.post('/roles', { code: newCode.value, name: newName.value })
    showCreate.value = false
    await loadRoles()
  } catch (e) {
    createError.value = e.response?.data?.message || '创建失败'
  } finally {
    creating.value = false
  }
}
async function deleteRole(r) {
  if (!confirm(`确定要删除角色「${r.name}」(${r.code}) 吗？\n\n该角色下的所有用户将失去该角色权限。`)) return
  try {
    await http.delete(`/roles/${r.id}`)
    if (selected.value?.id === r.id) selected.value = null
    await loadRoles()
  } catch (e) {
    alert(e.response?.data?.message || '删除失败')
  }
}
onMounted(() => { loadMenus(); loadRoles() })
</script>

<style scoped>
.rpc-layout { display: flex; flex-direction: column; gap: 16px; }
.rpc-top-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 0 0 8px 0;
  font-size: 13px; font-weight: 600; color: var(--text-secondary);
}
.rpc-role-row { display: flex; flex-wrap: wrap; gap: 6px; }
.rpc-role-item {
  padding: 7px 12px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  cursor: pointer; transition: all var(--transition);
  display: inline-flex; align-items: center; gap: 8px;
  white-space: nowrap;
}
.rpc-role-item:hover { border-color: var(--primary); background: #f5f9ff; }
.rpc-role-item.active {
  border-color: var(--primary);
  background: var(--primary-bg);
  font-weight: 600;
}
.rpc-role-name { font-size: 13px; font-weight: inherit; color: var(--text-primary); }
.rpc-role-code { font-size: 11px; color: var(--text-tertiary); }
.rpc-role-right { display: inline-flex; align-items: center; gap: 4px; }
.rpc-role-del {
  width: 18px; height: 18px; padding: 0; border: none; background: none;
  color: var(--text-tertiary); font-size: 14px; cursor: pointer; border-radius: 8px;
  display: inline-flex; align-items: center; justify-content: center;
  opacity: 0; transition: opacity 0.15s, color 0.15s;
}
.rpc-role-item:hover .rpc-role-del { opacity: 1; }
.rpc-role-del:hover { color: #ff3b30; background: rgba(255,59,48,0.1); }
.rpc-body-header { display: flex; align-items: baseline; gap: 10px; margin-bottom: 12px; }
.rpc-body-header h4 { font-size: 15px; font-weight: 600; color: var(--text-primary); margin: 0; }
.rpc-right-code { font-size: 12px; color: var(--text-tertiary); background: var(--border-soft); padding: 2px 8px; border-radius: 8px; }
.rpc-locked { font-size: 12px; color: #ff9500; margin-left: 8px; }
.rpc-perm-row { display: flex; gap: 16px; }
.rpc-perm-row .rpc-section { flex: 1; min-width: 0; margin-bottom: 0; }
.rpc-section {
  padding: 16px; background: rgba(0,0,0,0.02);
  border-radius: var(--radius-lg); border: 1px solid var(--border-soft);
}
.rpc-section h5 { font-size: 13px; font-weight: 600; color: var(--text-primary); margin: 0 0 4px 0; }
.rpc-section-desc { font-size: 12px; color: var(--text-tertiary); margin: 0 0 12px 0; }
.rpc-menu-tree { display: flex; flex-direction: column; }
.rpc-tree-group {
  border: 1px solid var(--border-color); border-radius: 8px;
  margin-bottom: 6px; overflow: hidden;
}
.rpc-tree-row {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; font-size: 13px; cursor: pointer;
  transition: background 0.1s;
}
.rpc-tree-row:hover { background: rgba(0,0,0,0.04); }
.rpc-tree-row input[type="checkbox"] { width: 15px; height: 15px; cursor: pointer; flex-shrink: 0; }
.rpc-tree-parent {
  background: rgba(0,0,0,0.02); font-weight: 600;
  color: var(--text-primary);
  border-bottom: 1px solid var(--border-soft);
}
.rpc-tree-child {
  padding-left: 36px; background: #fff;
  color: var(--text-secondary);
}
.rpc-tree-leaf {
  border: 1px solid var(--border-color); border-radius: 8px;
  margin-bottom: 6px; background: #fff;
}
.rpc-tree-leaf:hover { border-color: var(--primary); background: #f5f9ff; }
.rpc-tree-name { flex: 1; }
.rpc-hint { font-size: 11px; color: var(--text-tertiary); margin-left: auto; }
.rpc-func-empty {
  padding: 12px; text-align: center; font-size: 13px;
  color: var(--text-tertiary);
}
.rpc-api-note {
  margin-top: 10px; padding: 10px 14px; background: #f6f8fa;
  border-radius: var(--radius-lg); font-size: 13px; color: var(--text-secondary);
}
.rpc-actions { display: flex; align-items: center; gap: 12px; margin-top: 12px; }
.rpc-msg { font-size: 13px; }
.rpc-msg.success { color: var(--success); }
.rpc-msg.error { color: var(--danger); }
.rpc-empty {
  display: flex; align-items: center; justify-content: center;
  min-height: 200px; color: var(--text-tertiary);
  background: rgba(0,0,0,0.02); border: 1px dashed var(--border-color);
  border-radius: var(--radius-lg);
}
</style>
