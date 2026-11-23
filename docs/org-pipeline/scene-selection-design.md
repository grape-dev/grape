# 组织下派场景选择设计（2026-11-20）

## 背景

组织需求下派到关联仓库时，target 使用哪个场景此前由 `_resolve_target_scene` 隐式决定：
显式 `scene_id` → `managed_repos.dev_scene` → 唯一场景 → 失败。问题：

- 前端传的 `scene_id` 是组织场景，target 仓库里查不到，实际上不生效；
- 多场景仓库没有 `dev_scene` 时直接 `NO_TARGET_SCENE`；
- 默认“通用开发”场景的 SRE 节点未绑引擎，显式传给子 Issue 时会被预检查直接拦单。

现在把选择动作前移到组织层：**先选 targets，再为每个 target 选场景，最后一次性下派**。

## 流程

```
组织需求 Issue
  → org_dispatch AI 选 targets（repo-analysis/docs）
  → 对每个 target 拉取 scene-catalog
  → 为每个 target 选择 scene_id
  → POST /orgs/{id}/dispatches（含 target_scenes）
  → 服务端逐 target 校验/回退后建子 Issue
```

### 选择顺序

1. 显式 per-target 场景（请求 `target_scenes[repo_key]`）；
2. 仓库 `scene_recommend_config.rules` 条件命中（确定性）；
3. 场景目录中的语义匹配（AI，且只允许 `executable=true`）；
4. 仓库 `dev_scene`（须可执行）；
5. 仓库唯一场景（须可执行）；
6. 仍没有 → 该 target 失败，其他 target 继续。

服务端不会静默猜多场景中的某一个；推荐结果不可执行时按 4/5 回退。

## API

### 场景目录

```http
GET /api/orgs/{org_id}/dispatch/scene-catalog
```

参数：

- `repo_key`：可选；为空返回全部关联仓库（排除组织自动仓库）；
- `include=executable`：可选；返回每个场景的预检查结果。

返回：

```json
{
  "org_id": "...",
  "repos": [
    {
      "repo_key": "gitcode:org/repo-a",
      "full_name": "org/repo-a",
      "dev_scene": "custom-xxx",
      "scene_recommend_config": {
        "fallback_scene": "custom-yyy",
        "rules": [{"scene_id": "custom-zzz", "condition": "..."}]
      },
      "scenes": [
        {
          "scene_id": "custom-zzz",
          "name": "后端开发",
          "display_name": "后端开发",
          "description": "...",
          "skill_chain": ["issue:issue-cli", "coding:coding-cli"],
          "is_default": false,
          "executable": true,
          "problems": []
        }
      ]
    }
  ]
}
```

安全边界：

- 只读，鉴权 `require_org_api_user` + 团队组织 + 组织成员；
- `repo_key` 必须是当前组织关联仓库；
- 不返回 `advanced_options`、custom field 值、engine id、token；
- `executable` 复用 `runtime.public.precheck_scene`，与建单侧同一把尺子。

### 下派请求

现有 `POST /api/orgs/{id}/dispatches` 增加可选字段：

```json
{
  "targets": ["gitcode:org/repo-a", "gitcode:org/repo-b"],
  "scene_id": "",
  "target_scenes": {
    "gitcode:org/repo-a": "custom-xxx",
    "gitcode:org/repo-b": "custom-yyy"
  },
  "scene_reasons": {
    "gitcode:org/repo-a": "需求匹配后端开发场景"
  }
}
```

- `targets` 仍兼容字符串数组；`target_scenes/scene_reasons` 可不传；
- 批次级 `scene_id` 仅作旧调用方兼容兜底，不推荐再传组织场景；
- 服务端把最终场景写 `grape_t_org_dispatch_target.scene_id`，推荐理由落
  `plan_json`，不新增表/列。

## 组织技能

`org-dispatch-cli` 在选完 targets 后逐个调用：

```bash
grape org dispatches scene catalog --org_id "$ORG_ID" --repo_key "$REPO_KEY" \
  --include executable --json-out
```

选场景时只允许 executable，规则优先；最后把 `target_scenes/scene_reasons` 一并传给
`grape org dispatches create`。

## 失败与重试

- 某个 target 无可用场景 → target `failed`，错误带候选原因；
- 其他 target 继续，dispatch 进入 `partial_failed`；
- 修复仓库场景绑定后，可对失败 target 调 `target retry`，复用原 `child_issue_number`；
- 组织 Issue 按现有 `finalize_org_issue_aggregation` 收口（失败/部分失败进入
  `pending_review`）。

## 兼容性

- 旧请求不带 `target_scenes`：行为退化为原 `_resolve_target_scene`（但增加了可执行性回退）；
- 旧批次/重试：`target.scene_id` 已存在时直接复用；
- 不新增数据库表/迁移，只扩展现有请求/响应字段。