# Issue #471 P0 首批方案设计

## C1 无引擎注册判正常（worker 侧）
现状：`_amain` 中 `if not ids: log.error("Register failed"); return` —— 无引擎 worker 注册（服务端 200、runtimes=[]）被当失败退出 → daemon/nohup 与 systemd `Restart=always` 每 5s 无限重启；安装脚本 30s 等 `RuntimeAgent registered` 失败 → 回滚循环。
方案：
1. 注册判定：`ids` 为空且 `installed` 为空 → 成功（正常状态，进入主循环，ids=[]）；`ids` 为空但 `installed` 非空 → 维持失败退出。
2. `_reregister` 重注册前重新执行 `_discover_local_runtimes()`（原逻辑用启动时快照，无引擎 worker 永远注册空）；返回值改为 `(ids, engine_by_rid)`，调用方同步刷新 `runtime_ids / engine_by_rid / installed`——运行中安装引擎后 ≤300s 自动注册上线。
3. 「No runtimes installed locally」warning → info + 引擎安装指引文案。
服务端 `/api/runtime/register` 空列表已返回 200 + `runtimes: []`，无需改动；`/api/runtime/heartbeat` 空 ids 已有 `if body.runtime_ids:` 守卫。

## C2 安装脚本内置引擎 + 目录信任闸门
信任闸门根因（codearts 二进制逆向实证）：`folder-trust-middleware` 在 `settings.json`（`$KERNEL_DATA_DIR/settings.json`）`security.folderTrust.enabled=true` 且 cwd 不在 `security.trustedFolders` 且 stdin 非 TTY 时 `exit 1`。
方案：
1. `src/runtime/codearts.py` 新增 `write_trust_settings(data_dir, trusted_dirs)`：写/合并 settings.json（保留既有 enabled，trustedFolders 并入指定目录）。
2. `agent.py` per-task codearts 数据目录：预写 settings.json 信任任务 cwd（与既有 global.json/sandbox/config.json 同位预写）。
3. `agent.py` codearts 探测：使用专用探测数据目录（预写信任 + 复用宿主 system/skills symlink），避免宿主 settings 误伤探测。
4. `scripts/install-worker.sh`：新增 `--engine auto|opencode|hermes|none`（默认 auto=有 npm 且未装 opencode 时 `npm install -g opencode-ai`，失败降级提示）；codearts 已装时自动合并 trustedFolders（worker 安装目录 + codearts tasks 目录）；更新引擎提示文案与文档指引。
5. `help/usage/faq.md` 增补「执行引擎安装与目录信任」小节。

## E1 server_base_url 双语义拆分
现状：单一 `server_base_url` 同时用于浏览器回调（OAuth/附件/前端安装命令展示）与 worker 回连（devstage RUNTIME_SERVER_URL、service 任务 server_url、prompt_builder 远程 worker 地址），双地址部署必然配错。
方案（向前兼容，风险缓解：未配新项回退旧值）：
1. `src/user/db.py`：`get_browser_callback_url()` / `get_worker_connect_url()`（server-config.json > meta > 回退 `get_server_base_url()`）；`get_url_split_warning()` 输出配置错误引导（新项指向回环等）。
2. `src/config.py`：启动时把 `browser_callback_url` / `worker_connect_url` 从 server-config.json 双写 meta（对齐既有 server_base_url 逻辑）。
3. 消费端切换：worker 面（devstage.py、service.py、prompt_builder.py）→ `get_worker_connect_url()`；浏览器面（issues_center.py 附件 URL ×2）→ `get_browser_callback_url()`。
4. `/api/health`：新增 `browser_callback_url` / `worker_connect_url` 字段 + 告警；`src/api/main.py` 启动告警并入拆分告警。
5. `configs/server-config.example.json` 增补两个键及说明。

### QA 审查修复（PR #663 首轮审查后追加）
- Required：配置检查与告警纳入拆分项——`is_server_base_url_configured` / `get_server_base_url_warning` 任一拆分项（browser_callback_url/worker_connect_url）已配置且非回环即视为已配置（双地址部署只配拆分项不再误报「server_base_url 未配置」）；拆分项指向回环按对应面输出告警；全未配置保留「未配置」告警并提示两条配置路径。
- S1（建议）：`runtime.py _server_url()`（register 响应 server_url 字段）对齐 `get_worker_connect_url()`（worker 回连语义）。
- S2（建议）：`_reregister` engine_by_rid 按 name 配对（`RuntimeClient.register` 暴露 `last_register_detail`，个别引擎被跳过时不再 zip 位置错位）。
- S3（建议）：`_reregister` 重扫瞬时为空（此前快照有引擎）→ 沿用上次快照返回 None（不清空 runtime_ids）；真实无引擎（快照也为空）仍注册空列表。
- S4（建议）：`install-worker.sh provision_codearts_trust` 优先复用 `write_trust_settings`（venv python），内联实现保留为回退并加同步注释防漂移。

## 测试 seam
- C1：`_reregister`（mock client + monkeypatch `_discover_local_runtimes`）与注册判定纯逻辑；`tests/test_runtime_agent.py` 既有 `_reregister` 用例同步新契约。
- C2：`write_trust_settings` 文件级单测；agent per-task 预写产物断言（tmp_path）。
- E1：`tests/test_issue_471_url_split.py`——monkeypatch server-config/meta 验证两级来源、回退、告警文案、消费端函数取值。
