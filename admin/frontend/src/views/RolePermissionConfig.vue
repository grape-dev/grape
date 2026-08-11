<template>
  <div class="page role-page">
    <aside class="role-list">
      <h4>角色列表</h4>
      <div v-for="r in roles" :key="r.id" :class="['role-item', selected?.id === r.id ? 'active' : '']" @click="select(r)">
        <div>{{ r.name }}</div>
        <div class="sub">{{ r.code }}<span v-if="r.isProtected === 1" class="lock">🔒</span></div>
      </div>
      <div class="role-actions">
        <input v-model="newCode" placeholder="角色code" />
        <input v-model="newName" placeholder="角色名" />
        <button @click="createRole">新增</button>
      </div>
    </aside>
    <section class="perm-panel">
      <h4>
        {{ selected ? `菜单权限：${selected.name}` : '请选择角色' }}
        <button class="danger" v-if="selected && selected.isProtected !== 1" @click="deleteRole">删除角色</button>
      </h4>
      <div v-if="selected" class="tree">
        <div v-for="group in menuTree" :key="group.code" class="group">
          <div class="group-title">
            <label>
              <input type="checkbox" :checked="allChecked(group)" @change="toggleGroup(group, $event.target.checked)" />
              {{ group.name }}
            </label>
          </div>
          <div v-for="child in group.children" :key="child.code" class="child">
            <label>
              <input type="checkbox" :value="child.code" v-model="checkedCodes" :disabled="selected.isProtected === 1" />
              {{ child.name }}
            </label>
          </div>
        </div>
      </div>
      <div v-if="selected && selected.isProtected === 1" class="tip">保护角色的权限不可修改</div>
      <button v-if="selected && selected.isProtected !== 1" class="primary" @click="savePerms">保存权限</button>
    </section>
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

async function loadRoles() {
  const { data } = await http.get('/roles')
  roles.value = data
  if (selected.value) {
    const s = roles.value.find(r => r.id === selected.value.id)
    if (s) { selected.value = s; syncChecked() }
  }
}
async function loadMenus() {
  const { data } = await http.get('/menus')
  menuTree.value = data
}
function syncChecked() {
  const allMenus = []
  menuTree.value.forEach(g => allMenus.push(g.code, ...g.children.map(c => c.code)))
  checkedCodes.value = allMenus.filter(code =>
    selected.value.menuIds.includes(menuIdByCode(code)))
}
function menuIdByCode(code) {
  for (const g of menuTree.value) {
    if (g.code === code) return g.id
    const c = g.children.find(x => x.code === code)
    if (c) return c.id
  }
  return null
}
function codeById(id) {
  for (const g of menuTree.value) {
    if (g.id === id) return g.code
    const c = g.children.find(x => x.id === id)
    if (c) return c.code
  }
  return null
}
async function select(r) {
  selected.value = r
  checkedCodes.value = []
  syncChecked()
}
function allChecked(group) {
  return group.children.every(c => checkedCodes.value.includes(c.code))
}
function toggleGroup(group, checked) {
  group.children.forEach(c => {
    const i = checkedCodes.value.indexOf(c.code)
    if (checked && i < 0) checkedCodes.value.push(c.code)
    if (!checked && i >= 0) checkedCodes.value.splice(i, 1)
  })
}
async function savePerms() {
  const ids = []
  menuTree.value.forEach(g => {
    if (checkedCodes.value.includes(g.code)) ids.push(g.id)
    g.children.forEach(c => { if (checkedCodes.value.includes(c.code)) ids.push(c.id) })
  })
  try {
    await http.put(`/roles/${selected.value.id}/permissions`, { menuIds: ids })
    alert('保存成功，权限立即生效')
    loadRoles()
  } catch (e) {
    alert(e.response?.data?.message || '保存失败')
  }
}
async function createRole() {
  try {
    await http.post('/roles', { code: newCode.value, name: newName.value })
    newCode.value = ''; newName.value = ''
    loadRoles()
  } catch (e) {
    alert(e.response?.data?.message || '创建失败')
  }
}
async function deleteRole() {
  if (!confirm(`确认删除角色 ${selected.value.name}？`)) return
  try {
    await http.delete(`/roles/${selected.value.id}`)
    selected.value = null
    loadRoles()
  } catch (e) {
    alert(e.response?.data?.message || '删除失败')
  }
}
onMounted(() => { loadRoles(); loadMenus() })
</script>

<style scoped>
.role-page { display: flex; gap: 16px; align-items: flex-start; }
.role-list { width: 260px; background: #fff; border-radius: 6px; padding: 14px; }
.role-item { padding: 10px; border-radius: 4px; cursor: pointer; }
.role-item.active { background: #ecf5ff; }
.sub { font-size: 12px; color: #999; }
.lock { margin-left: 4px; }
.role-actions input { width: 100%; padding: 6px 8px; margin-top: 6px; border: 1px solid #dcdfe6; border-radius: 4px; }
.role-actions button { margin-top: 8px; }
.perm-panel { flex: 1; background: #fff; border-radius: 6px; padding: 14px; }
.tree { margin: 10px 0; }
.group { margin-bottom: 12px; }
.group-title { font-weight: 600; margin-bottom: 6px; }
.child { padding-left: 24px; padding: 4px 0 4px 24px; }
.tip { color: #999; font-size: 13px; margin: 8px 0; }
button { padding: 6px 14px; border: 1px solid #dcdfe6; background: #fff; border-radius: 4px; cursor: pointer; }
button.primary { background: #409eff; color: #fff; border-color: #409eff; }
button.danger { float: right; background: #f56c6c; color: #fff; border-color: #f56c6c; }
h4 { margin-bottom: 10px; }
</style>
