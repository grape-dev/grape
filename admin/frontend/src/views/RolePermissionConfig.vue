<template>
  <div class="page-container" style="padding:0">
    <div class="page-header">
      <h1>角色权限配置</h1>
    </div>
    <div style="padding:20px 28px;display:flex;gap:16px;align-items:flex-start">
      <div class="card" style="width:280px;flex-shrink:0;padding:14px">
        <h4 class="mb8" style="font-size:14px">角色列表</h4>
        <div
          v-for="r in roles"
          :key="r.id"
          :class="['role-item', { act: selected?.id === r.id }]"
          @click="select(r)"
        >
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
            <div>
              <div>{{ r.name }}</div>
              <div class="t3" style="font-size:11px">{{ r.code }}<span v-if="r.isProtected === 1" class="status-tag info" style="margin-left:4px">保护</span></div>
            </div>
          </div>
        </div>
        <div class="mt12" style="border-top:1px solid var(--border-color);padding-top:12px">
          <input v-model="newCode" placeholder="角色 code" style="width:100%;margin-bottom:8px" />
          <input v-model="newName" placeholder="角色名" style="width:100%;margin-bottom:8px" />
          <button class="btn btn-primary btn-sm" @click="createRole">+ 新增角色</button>
        </div>
      </div>

      <div class="card f1" style="padding:16px 20px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
          <h2 style="font-size:14px;font-weight:600">{{ selected ? `菜单权限：${selected.name}` : '请选择角色' }}</h2>
          <button v-if="selected && selected.isProtected !== 1" class="btn btn-danger btn-sm" @click="deleteRole">删除角色</button>
        </div>
        <div v-if="selected" class="mt8">
          <div v-for="group in menuTree" :key="group.code" class="mb8">
            <label class="chk" style="font-weight:600">
              <input type="checkbox" :checked="allChecked(group)" @change="toggleGroup(group, $event.target.checked)" />
              {{ group.name }}
            </label>
            <div v-for="child in group.children" :key="child.code" style="padding-left:26px">
              <label class="chk">
                <input type="checkbox" :value="child.code" v-model="checkedCodes" :disabled="selected.isProtected === 1" />
                {{ child.name }}
              </label>
            </div>
          </div>
          <p v-if="selected.isProtected === 1" class="t3">保护角色的权限不可修改</p>
          <button v-if="selected.isProtected !== 1" class="btn btn-primary btn-sm mt12" @click="savePerms">保存权限</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
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
.role-item { padding: 10px; border-radius: 8px; cursor: pointer; transition: background 0.12s; }
.role-item.act { background: var(--primary-bg); }
.role-item:hover { background: #f5f7fa; }
</style>
