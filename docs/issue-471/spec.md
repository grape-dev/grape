# Issue #471 P0 首批（C1/C2/E1）需求文档

## 背景
Issue #471 汇总「安装 → 接设备 → 建场景 → 建需求 → 跑流水线」全流程实测第二批 13 项问题。本 sprint 实现 P0 三项（阻塞 worker 上线与设备接入核心流程）。

## 目标
- C1：无引擎 worker 注册不再被判失败——注册请求成功即成功，引擎列表为空为正常状态；后续安装引擎无需重启即可自动注册上线。
- C2：一行式安装脚本 best-effort 内置可用引擎；修复 CLI 引擎（codearts）在无 TTY 环境下因「目录信任」闸门致命退出的问题。
- E1：拆分 `server_base_url` 双语义为 `browser_callback_url`（浏览器回调）+ `worker_connect_url`（worker 回连），未配新项时向前兼容回退旧 `server_base_url`，配置错误给出明确报错引导。

## 范围
- In：上述三项；`src/runtime/agent.py`、`src/runtime/codearts.py`、`scripts/install-worker.sh`、`src/user/db.py`、`src/config.py`、`src/api/routes/health.py`、`src/api/routes/devstage.py`、`src/runtime/service.py`、`src/prompt_builder.py`、`src/api/issues_center.py`、`src/api/main.py`、`configs/server-config.example.json`、`help/usage/faq.md`。
- Out：P1（A1/A2/A4/B1/C3/D1/D2）、P2（A3/A5/B2）；`install-worker.ps1`（Windows 侧后续批次）；引擎外部 IDE/服务本身可用性。

## 验收标准
- [ ] 全新无引擎 worker 注册成功（日志出现 `RuntimeAgent registered: []`），daemon/systemd 不再无限重启，安装脚本不再触发回滚。
- [ ] worker 运行中安装引擎后，周期性 re-register 自动发现并注册新引擎（无需重启）。
- [ ] codearts 引擎在无 TTY 环境下执行任务/探测不再因目录信任闸门 exit 1（per-task 数据目录预写 settings.json 信任任务 cwd）。
- [ ] 安装脚本支持 `--engine`（auto/opencode/hermes/none），auto 模式 best-effort 安装 opencode（npm 可用时）；codearts 已装时自动把 worker 目录加入 trustedFolders。
- [ ] `worker_connect_url` / `browser_callback_url` 拆分生效：devstage RUNTIME_SERVER_URL、service 任务 server_url、prompt_builder 远程地址走 worker_connect_url；附件/OAuth 等浏览器面走 browser_callback_url；/api/health 暴露两个新字段与告警；均回退 server_base_url。
- [ ] QA Required（PR #663 首轮）：双地址部署只配 browser_callback_url+worker_connect_url（不配 server_base_url）时，健康检查与启动日志不再误报「server_base_url 未配置」（任一拆分项已配置即消除未配置告警；拆分项回环按对应面告警）。
- [ ] 新增测试覆盖三项；现有测试全绿。
