# Spec: Issue #482 — 场景流水线完成后自动登记部署 URL 到场景详情

> 承接 Issue Agent Spec（评论 id 196395917）| 目标分支: weekly-08-ljt

## 目标
SRE 完成后（直连 `_execute_agent_inner` / 远程 worker `complete_task` 双路径）自动把部署 URL 登记到需求绑定场景的 `devops_pipeline_scene.deploy_url`。

## 范围
- In：双收尾路径增加场景级登记；复用 #468 `register_sre_deploy_url` 的提取校验逻辑；按 issue 绑定 scene_id 定位场景；UPDATE 幂等覆盖；登记日志。
- Out：仓库级 `managed_repos.deploy_url` 行为不变；手动 `PUT /api/scenes/{scene_id}/deploy-url` 语义不变；SRE 失败（exit_code≠0）不登记；无场景绑定不登记不报错。

## 验收
- 双路径自动更新绑定场景 deploy_url
- 仅更新绑定场景，其他场景/仓库级不受影响
- 手动/仓库级语义不回退
- 失败/无场景不登记不报错；登记失败不影响收尾主流程
- 含日志；新旧测试覆盖
