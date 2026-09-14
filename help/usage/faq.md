# 9. 常见问题 (FAQ)

### Q0: 创建任务前需要做什么准备？为什么任务报"无可用引擎"？

> 💡 **创建任务前必须先在场景中心配置并应用场景**
>
> 新建 Issue 触发流水线之前，必须先完成以下步骤，否则 Agent 会报 `engine_unavailable（无可用引擎）` 导致任务失败：

1. **进入场景中心**：选择仓库后，点击左侧导航栏的 **场景中心**
2. **选择场景**：在下拉框中选择一个场景（默认场景 `default` 已预置 VOD→Issue→Coding→QA→SRE 全链路 Agent 节点）
3. **配置引擎**：点击 **配置引擎** 按钮，为每个 Agent 节点选择要使用的引擎实例（从已注册的 Worker 引擎中选择）：
   - VOD Agent → 选择 hermes 引擎实例
   - Issue Agent → 选择 opencode 引擎实例
   - Coding Agent → 选择 opencode 引擎实例
   - QA Agent → 选择 opencode 引擎实例
   - SRE Agent → 选择 hermes 引擎实例
4. **保存引擎配置**：配置完成后点击 **保存引擎** 按钮
5. **应用场景**：点击 **应用** 按钮，将场景配置绑定到当前仓库

> ⚠️ **注意事项**
>
> - 如果没有点击"应用"，场景配置不会生效，Webhook 事件仍使用默认配置
> - 如果引擎未配置（engine_runtime_ids 为空），Agent 会报"无可用引擎"并失败
> - 新增 Worker 后需要回到场景中心重新配置引擎，将新引擎分配给 Agent
> - 不同仓库可以应用不同场景（如开发仓库用 default 场景，运维仓库用自定义 SRE 场景）
> - 自定义场景：在下拉框选择"创建自定义场景"，配置每个 Agent 的技能（skill）、触发模式（mode）、下游路由（next）等

### Q1: Issue 创建后没有触发流水线？

**可能原因及排查：**

1. **Webhook 未配置**：检查 GitCode 仓库的 Webhook 设置，确认已配置指向 Grape 的 webhook URL（`https://webhook.grape.example.com/webhook/gitcode`）
2. **仓库未授权**：在 Grape 控制台"GitCode 仓库"页面确认仓库已导入
3. **Webhook Secret 不匹配**：在 GitCode Webhook 设置中确认 Secret 与 Grape 配置一致
4. **事件类型未勾选**：GitCode Webhook 需勾选 Issue 和 PR 相关事件
5. **查看 webhook 日志**：在 GitCode Webhook 管理页面查看推送结果（HTTP 200 表示成功）

### Q2: 任务状态显示"失败"，如何排查？

1. 在 **任务中心** 或 **流水线** 页面点击失败的任务，查看详细日志
2. 常见失败原因：
   - **exit_code=-1, duration=30s**：引擎启动失败（如 Hermes ACP 依赖缺失、CLI 路径错误）
   - **exit_code=-15**：任务被信号终止（服务重启、超时 kill）
   - **exit_code=1**：Agent 执行出错（代码编译失败、API 调用失败）
   - **"无可用引擎"**：没有在线的 Worker 运行所需引擎，检查 Agent 运行时页面
3. 在 **系统监控** 页面检查对应 Worker 的引擎探活状态
4. SSH 到对应 Worker 查看 `journalctl -u runtime-agent` 日志

### Q3: 控制台页面显示"加载中..."不消失？

**排查步骤：**

1. **强制刷新**：按 Ctrl+Shift+R 强制刷新页面，清除浏览器缓存的旧版 JS
2. **检查网络**：确认能访问 Grape 域名（如 `grape.example.com`）
3. **检查登录状态**：如果 token 过期，页面会跳转到登录页
4. **检查后端服务**：直接访问 `https://<域名>/health` 确认服务在线
5. **查看浏览器控制台**：F12 打开开发者工具，查看 Network 标签中是否有请求超时

### Q4: Agent 执行的任务一直卡在"运行中"？

1. **任务超时**：Worker 默认任务超时 30 分钟，超时后自动标记失败
2. **Worker 离线**：检查系统监控中对应 Worker 是否在线（心跳是否正常）
3. **引擎卡死**：可能是 AI 模型 API 限流或网络中断，检查 Worker 日志
4. **手动处理**：在流水线页面点击"重试"重新执行，或在 Issue 评论区重新触发
5. **清理僵尸任务**：联系管理员在数据库中执行 `UPDATE events SET status='failed', exit_code=-15 WHERE status='running' AND started_at < ...`

### Q5: 如何添加新的 Worker 机器？

1. 在目标机器上安装 Python 3.10+ 和所需 AI CLI（opencode/hermes/codearts）
2. 在 Grape 控制台 **Agent 运行时** → **通用设置** 中创建新的 Worker 凭据
3. 在目标机器执行一键安装命令：

   ```bash
   curl -fsSL https://<grape域名>/api/runtime/install | bash -s -- --token <rta_...> --server https://<grape域名>
   ```

4. 安装完成后 Worker 自动注册，在运行时页面可见
5. 确认引擎探活状态为 ✅

### Q6: 引擎探活显示"pending"或"fail"？

**pending（等待）**：引擎刚注册，等待首次探活（每 10 分钟执行一次）。等待几分钟后刷新即可。

**fail（失败）**：

- 检查引擎 CLI 是否已安装：`which opencode` / `which hermes`
- 手动测试：`opencode run "hello"` 或 `hermes chat -q "hello" --yolo`
- 检查 AI 模型 API Key 是否配置正确（`/etc/grape/grape.env` 或 `~/.hermes/.env`）
- 如果使用 hermes 引擎，确认已安装 ACP 依赖：`hermes acp` 应输出 "ACP client connected"

### Q7: SRE Agent 销毁云资源时需要注意什么？

> 🚨 **⚠️ 安全提示**
>
> SRE Agent 执行资源销毁前**必须经过用户确认**。流程如下：

1. 用户创建 Issue 要求销毁资源
2. SRE Agent 查询并列出所有待销毁资源清单，在 Issue 评论区发布
3. 用户在评论区回复"全部销毁"或指定资源确认
4. SRE Agent 确认后执行销毁操作
5. 销毁完成后在 Issue 评论区发布验证结果

如果需要中止销毁，在确认前关闭 Issue 或在评论区回复"取消"即可。

### Q8: 如何查看 Agent 的详细执行日志？

- **控制台**：在任务中心/流水线页面点击任务行，弹出日志详情弹窗，显示每一步的思考和工具调用
- **Worker 日志**：SSH 到 Worker 机器执行 `journalctl -u runtime-agent -n 100 --no-pager`
- **服务端日志**：在系统监控页面查看错误日志来源，或 SSH 到主服务器查看 `/opt/grape/data/main.log`
- **数据库**：事件的 `result` 字段包含 Agent 的最终输出摘要

### Q9: Coding Agent 提交的 PR 需要手动合并吗？

是的。Coding Agent 提交 PR 后，QA Agent 会自动审查并给出意见，但**PR 合并需要仓库维护者手动操作**。这是设计上的安全机制——AI 可以编写代码和提交 PR，但代码合入主线需要人工确认。

在 GitCode 的 PR 页面可以查看 QA Agent 的审查意见和代码变更，确认后点击"合并"即可。

### Q10: 如何配置 AI 模型的 API Key？

AI 模型配置在以下位置：

- **主服务器**：`/etc/grape/grape.env` 中的 `HERMES_CUSTOM_API_DEEPSEEK_COM_API_KEY`
- **Worker**：`~/.hermes/.env` 中的 `HERMES_CUSTOM_OPENGW_CLOUDDEVELOPER_CLUB_API_KEY`
- **opencode**：`~/.config/opencode/opencode.json` 中的 `apiKey`

修改后需重启对应服务（主服务器：grape-main/grape-webhook；Worker：runtime-agent）。

### Q11: 如何为仓库配置自定义技能？

1. 在 **DevOps 技能** 页面点击"创建技能"
2. 选择作用域：仓库级（选择目标仓库）或全局
3. 编写 SKILL.md：包含 `name`、`description`（触发条件）、`steps`（执行步骤）
4. 可选：添加 `references/`（参考文档）、`scripts/`（脚本）、`templates/`（模板）
5. 保存后技能自动分发到 OBS（如已启用），Worker 执行时自动下载

### Q12: 系统监控中 Worker 的 tmp 百分比很高怎么办？

Worker 的 `/tmp` 目录可能被 AI 引擎运行时生成的 JIT 缓存文件（隐藏 `.so` 文件）占满。症状：scp 传输文件报 "No space left on device"，但 Worker 运行正常。

**清理方法**（在 Worker 上执行）：

```bash
find /tmp -maxdepth 1 -name ".*.so" -delete
```

建议定期清理所有 Worker 的 /tmp 目录。如果频繁占满，可以在 crontab 中添加定时清理任务。
