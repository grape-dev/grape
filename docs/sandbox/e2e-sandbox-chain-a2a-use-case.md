# 沙箱全链 E2E 用例：装 worker → 探测识别 → 场景绑定 → 触发 → A2A 流转

> 自动化脚本：`tests/e2e/test_sandbox_chain.py`（pytest，默认跳过，`GRAPE_E2E_SANDBOX=1` + 环境变量启用）。
> 首轮人工执行：2026-11-06，i7 分支（重建+arch 合流），8081 控制器 + <sandbox-host> 双 worker。

## 前置条件

| 条件 | 说明 |
|---|---|
| 控制器运行中 | 8081，`configs/crypto.json` 就绪（chmod 600） |
| 沙箱平台可达 | CubeMaster（如 https://cube.example.com），**平台资源充足** |
| worker 目标机 | SSH 可达，python3/curl 可用 |
| 托管仓库 | managed_repos 有 webhook token |
| LLM 凭据 | 仓库级 hermes LLM 凭据（沙箱内执行必需，缺=任务失败"缺少 LLM 凭据"） |

## 步骤表

| # | 步骤 | 命令/操作要点 | 执行记录 | 结果 |
|---|---|---|---|---|
| 1 | 登录拿 JWT | `POST /api/auth/login` | 200 | ✅ |
| 2 | 建全局沙箱凭据 | `POST /api/settings/runtime-credentials` `{cred_type:sandbox, platform:cubesandbox, scope:global}` | id=67, token rta_ | ✅ |
| 3 | 建仓库级沙箱凭据 | 同上 `scope:repo, repo_key` | id=68 | ✅ |
| 4 | worker 一键安装 ×2 | `curl -sSL <ctrl>/api/runtime/install -o iw.sh && bash iw.sh --server <ctrl> --token <rta_>`（<sandbox-host> 远程执行） | 目录 `/opt/grape-cw/<cred_uid8>`，服务 `sandbox-worker-<cred_uid8>` | ✅ |
| 5 | 等待探测批 | worker 启动→上报 provider→逐模板建箱探测→上报 detected | `sandbox provider reported: engines_detected=N` | ✅ |
| 6 | 引擎注册 | 60s 模板刷新周期 → `template refresh: N runtimes updated` | 10 引擎 online+engine_uid | ✅ |
| 7 | 场景绑定 | active 场景 nodes `engine_runtime_ids=[engine_uid...]` + `managed_repos.dev_scene` 指向该场景 | scene 4 四节点绑定 | ✅ |
| 8 | 仓库 LLM 凭据 | `POST /api/settings/llm-credentials/repo` `{engine_type:hermes, api_key, base_url, model}` | id=2 已保存 | ✅ |
| 9 | 触发 | 直发 Issue Hook（任务型 issue：带开发动作诉求） | 200 pipeline scheduled | ✅ |
| 10 | 断言 vod 沙箱执行 | 池广播 [沙箱引擎] → v2 认领 → 建箱 → 完成 | t_45c903f85dc4 completed @ runtime 38 | ✅ |
| 11 | 断言 A2A 流转 | vod 发 `grape a2a handoff` → a2a_event → 下一节点任务被另一沙箱引擎认领 | seq=57 a2a_event；issue 任务认领 @ runtime 41 | ✅ |
| 12 | 无泄漏断言 | 探测批后平台 running 箱数回归 0 | `batch sweep: all probe sandboxes terminated`；平台 0 | ✅ |

## 断言表

| 断言 | 依据 | 预期 | 实测 | 结果 |
|---|---|---|---|---|
| A1 凭据 token 一次性展示 | 响应体 | rta_ 前缀 | rta_… 仅返回一次 | ✅ |
| A2 Provider 自动登记 | `sandbox_provider_config` | runtime_credential_id=新凭据 且 **provider_uid 非空** | 修复后非空（修复前恒空=缺陷①） | ✅ |
| A3 worker 服务名 uid 锚 | systemctl | `sandbox-worker-<cred_uid8>` | 一致 | ✅ |
| A4 模板自动上报 enabled=1 | `sandbox_templates` | worker 上报路径全部 1（手动 scan-templates 路径默认 0 属设计） | 10/10 | ✅ |
| A5 binding detected | `engine_template_binding` | (provider, engine, template) 三元组 UPSERT，status=detected | 命中 | ✅ |
| A6 引擎四段名+uid | `agent_runtimes` | `hermes:<tpl>:<platform>:<provider_id>` + engine_uid 非空 + online | 命中 | ✅ |
| A7 认领走 uid 链 | 日志 | `claim_by_tag: runtime=<uid 四段名>` / complete v2 | 命中 | ✅ |
| A8 A2A 事件 | `events` | a2a_event 落库 + 下一节点任务创建 | seq=57 | ✅ |
| A9 无泄漏 | `Sandbox.list` | 探测批结束平台 running=0 | 0（修复前残留 6） | ✅ |

## 已知边界与既往缺陷（修复记录）

| 缺陷 | 现象 | 修复 commit |
|---|---|---|
| Provider 新建缺 provider_uid | uid 恒空（回填迁移不覆盖新行） | settings.py INSERT 补 uid |
| 探测重试不武装 | endpoint 启动后补配→探测永不重跑 | 空 endpoint 计入 _failed |
| 探测箱泄漏 | SDK DELETE 2xx 但平台未终止→累积→130597 | kill 后 get_info 验证重试 + 批末清扫 |
| 存量库 binding 坏 FK | INSERT 报 no such table: …_oldname | 迁移 202611040009 重建表 |
| vod 咨询类不 A2A | 咨询类 issue 直接闭环（runbook 设计行为） | 触发用例必须用**任务型** issue |

## 运行方式

```bash
GRAPE_E2E_SANDBOX=1 \
GRAPE_CONTROLLER=http://<controller-host>:8081 \
GRAPE_ADMIN_USER=admin GRAPE_ADMIN_PASSWORD=*** \
GRAPE_REPO=org/repo \
GRAPE_WORKER_HOST=<worker-host> GRAPE_WORKER_USER=<worker-user> GRAPE_WORKER_PASSWORD=*** \
pytest tests/e2e/test_sandbox_chain.py -v
```

注意：同一平台并发跑多批会互相挤占配额（130597）；用例间串行。
