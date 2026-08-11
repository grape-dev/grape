<template>
  <div class="flex gap16" style="align-items:flex-start">
    <div class="cd" style="width:280px;flex-shrink:0">
      <h4 class="mb8">角色列表</h4>
      <div v-for="r in roles" :key="r.id" :class="['flex', 'fc', 'jbtw', 'gap8', { act: selected?.id === r.id }]" class="role-item" @click="select(r)">
        <div>
          <div>{{ r.name }}</div>
          <div class="t3" style="font-size:11px">{{ r.code }}<span v-if="r.isProtected === 1" class="tg tg-q" style="margin-left:4px">保护</span></div>
        </div>
      </div>
      <div class="mt12" style="border-top:1px solid var(--b);padding-top:10px">
        <input v-model="newCode" placeholder="角色 code" />
        <input v-model="newName" placeholder="角色名" />
        <button class="bt bt-p" @click="createRole">新增角色</button>
      </div>
    </div>

    <div class="cd f1">
      <div class="cd-h">
        <h2>{{ selected ? `菜单权限：${selected.name}` : '请选择角色' }}</h2>
        <button v-if="selected && selected.isProtected !== 1" class="bt bt-r" @click="deleteRole">删除角色</button>
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
        <button v-if="selected.isProtected !== 1" class="bt bt-p mt12" @click="savePerms">保存权限</button>
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
.role-item { padding: 10px; border-radius: 7px; cursor: pointer; }
.role-item.act { background: #eef2ff; }
.role-item:hover { background: var(--bg); }
</style>
