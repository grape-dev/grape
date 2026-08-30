# Agent Skill 配置保存改为 skill_id 直传方案（定稿）

## 一、改动文件

| 文件 | 改动 |
|------|------|
| src/api/static/js/pages/agents.js | option value 改 skill_id + saveAgentConfig 传 skill_id + 回显用 skill_id |
| src/api/routes/agents.py | AgentConfig 加 skill_id + 保存优先用 skill_id + 同步写入 skill_name |

## 二、前端改动

### option value 改为 skill_id
### saveAgentConfig 传 skill_id
### 回显用 skill_id 匹配

## 三、后端改动

### AgentConfig 加 skill_id 字段
### 保存优先用 skill_id
### skill_id 有值时查出 skill_name 同步写入 skill 字段（兜底）

## 四、兼容
- 不传 skill_id 时回退旧逻辑（skill + skill_scope）
- skill_id=0 时清空配置（skill='', skill_id=0）
