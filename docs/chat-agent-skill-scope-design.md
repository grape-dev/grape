# Chat Agent Skill 配置改造方案

## 一、背景

Chat Agent 页面的 Skill 下拉只显示官方 skill，仓库和个人 skill 看不到。和 DevOps Agent 改前同样的问题。后端 chat.py 的归一化已在本轮修复完成，只需改前端 chat.js。

## 二、改动文件

| 文件 | 改动 |
|------|------|
| src/api/static/js/pages/chat.js | Skill 行加 scope 下拉框 + option value 改 skill_id + saveConfig 传 skill_id + 回显用 skill_id |

## 三、具体改动

### 3.1 Skill 行改为 scope + skill 同行布局

改前：
```javascript
<div style="font-weight:600;margin-bottom:8px">Skill</div>
<select id="cfgSkill" style="...">
  <option value="${s.scope}:${s.name}" ...>${s.display_name || s.name} (${scopeLabel})</option>
</select>
```

改后：
```javascript
<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
  <span style="font-weight:600;flex-shrink:0">Skill</span>
  <select id="cfgSkillScope" onchange="_switchChatSkillScope(this.value)" style="flex:0 0 70px;max-width:70px;padding:5px 6px;border:1px solid var(--b);border-radius:4px;font-size:11px;background:var(--bg);outline:none">
    <option value="official">全局</option>
    <option value="repo">项目</option>
    <option value="user">个人</option>
  </select>
  <select id="cfgSkill" data-skill-id="${d.skill_id || 0}" style="flex:1;padding:5px 8px;border:1px solid var(--b);border-radius:4px;font-size:11px;background:var(--bg)">
    <option value="0">默认官方技能（自动）</option>
  </select>
</div>
```

### 3.2 新增 _switchChatSkillScope 函数

```javascript
let _chatSkillScope = 'official';

function _switchChatSkillScope(scope) {
  _chatSkillScope = scope;
  _loadChatSkillOptions();
}
```

### 3.3 skill 下拉加载改为按 scope 查询 + skill_id 匹配

改前：
```javascript
const skillsOpts = (d._skills || []).map(s => {
  const scopeLabel = ...;
  return `<option value="${s.scope}:${s.name}" ...>${s.display_name || s.name} (${scopeLabel})</option>`;
});
```

改后：
```javascript
function _loadChatSkillOptions() {
  const skillSelect = document.getElementById('cfgSkill');
  if (!skillSelect) return;
  const currentSkillId = skillSelect.dataset.skillId || '0';
  let repo = '';
  try {
    const c = JSON.parse(localStorage.getItem('currentRepo') || 'null');
    repo = c ? (c.fullName || '') : '';
  } catch {}
  const scope = _chatSkillScope || 'official';
  let qs = '?scope=' + scope;
  if (repo) qs += '&repo=' + encodeURIComponent(repo);
  API.get('/skills' + qs).then(d => {
    const allSkills = d.skills || [];
    const filtered = allSkills.filter(s => s.agent_type === 'chat');
    const matchCurrent = filtered.find(s => String(s.id) === currentSkillId);
    const defaultOpt = scope === 'official' ? '<option value="0">默认官方技能（自动）</option>' : '';
    let html = defaultOpt +
      filtered.map((s, i) => {
        const selected = matchCurrent ? (String(s.id) === currentSkillId) : (i === 0);
        return `<option value="${s.id}" ${selected ? 'selected' : ''}>${s.display_name || s.name}</option>`;
      }).join('');
    if (filtered.length === 0) {
      html = `<option value="0" disabled>${scope === 'repo' ? '该仓库暂无项目技能' : '暂无个人技能'}</option>`;
    }
    skillSelect.innerHTML = html;
  }).catch(() => {});
}
```

### 3.4 scope 初始值从已配置 skill 的 scope 推导

```javascript
// 在渲染 Chat Agent 页面时
_chatSkillScope = (d.skill_scope === 'repo') ? 'repo' : (d.skill_scope === 'user') ? 'user' : 'official';
const scopeSel = document.getElementById('cfgSkillScope');
if (scopeSel) scopeSel.value = _chatSkillScope;
_loadChatSkillOptions();
```

### 3.5 saveConfig 传 skill_id

改前：
```javascript
const skillVal = document.getElementById('cfgSkill')?.value || '';
let skill = skillVal, skillScope = '';
if (skillVal.indexOf(':') > 0) {
  const _sp = skillVal.indexOf(':');
  skillScope = skillVal.slice(0, _sp);
  skill = skillVal.slice(_sp + 1);
}
const _body = {backend, skill, im};
if (skillScope) _body.skill_scope = skillScope;
```

改后：
```javascript
const skillVal = document.getElementById('cfgSkill')?.value || '0';
const skillId = parseInt(skillVal) || 0;
const _body = {backend, skill_id: skillId, im};
```

## 四、不受影响

- 后端 chat.py（归一化已完成）
- 后端 agents.py（DevOps Agent 保存逻辑已完成 skill_id 直传）
- 后端 skills.py（list_skills scope 参数已完成）
- DevOps Agent 前端（agents.js 已完成）

## 五、验证

1. Chat Agent 页面默认显示全局 skill
2. 点项目 → 下拉刷新为仓库 skill
3. 点个人 → 下拉刷新为个人 skill
4. 保存后重新进入 → scope 自动切到已配置 skill 的 scope
5. 保存传 skill_id → 后端正确入库
