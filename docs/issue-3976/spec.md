# Spec: Issue #3976 — 技能详情页优化

> Coding Agent | 2026-11-23 | 目标分支 arch-weekly-11-ldw-02

## 需求

在「【场景·技能】→ 技能 → 详情」（`SkillsView.vue`）补齐三项能力，官方技能全程只读：

1. 仓库技能 ↔ 个人技能在详情页右上角一键原地互转 scope。
2. 个人技能可在详情页在线编辑 SKILL.md，保存后版本号不递增。
3. 「编辑信息」抽屉新增「AI 填写」，异步识别 SKILL.md 填充摘要/描述/五槽/标签。

## 范围

### In Scope
- `POST /api/skills/by-id/{id}/convert`：原地改 scope（user ↔ repo），保留 id、历史版本行、`scene_skill_ref`、点赞。
- `PUT /api/skills/by-id/{id}/content`：仅个人技能可保存 SKILL.md 正文；不写入版本表（版本号不变）。
- `POST /api/skills/by-id/{id}/ai-fill`：后端读取 SKILL.md，调用平台既有 LLM（沿用 autopilot 的 hermes 配置读取方式），返回结构化建议，不落库。
- 前端：互转按钮随 scope 渲染；SKILL.md 编辑态；抽屉「AI 填写」仅回填空字段，失败保留已填内容。
- 测试：互转原地性、版本不增、官方/仓库 SKILL.md 只读、AI 失败不破坏已填内容。

### Out of Scope
- 官方技能任何写操作；技能包其它文件在线编辑；跨仓库迁移；新增外部模型服务。

## 验收标准
- [x] 仓库技能显示「转为个人技能」，个人技能显示「转为仓库技能」；官方不渲染；转换后 id/历史版本/场景引用不变。
- [x] 官方技能无互转入口，SKILL.md 与信息编辑只读。
- [x] 个人技能保存 SKILL.md 后版本号不递增；官方/仓库技能 403。
- [x] 抽屉「AI 填写」异步填充，失败有提示且不清空/覆盖已填内容。
- [x] 现有测试通过，新增测试覆盖上述行为。

## A4 时延口径豁免申请（QA 架构守护复评，head `ce430227`）

`POST /api/skills/by-id/{id}/ai-fill` 申请排除 Issue #2403 的「端点 <200ms」A4 口径，理由如下：

1. **交互语义**：ai-fill 是用户在技能详情抽屉里显式点击「AI 填写」触发的交互式动作，用户预期就是等待模型生成建议——不是高频机器调用，200ms 口径针对的是服务端自身处理时延，本端点时延由上游 LLM 决定（结构上不可能满足，也不应满足：更快的模型产出质量不可控）。
2. **事件循环保护已关闭**：上一轮指认的「async handler 内同步 DB/OBS 读阻塞事件循环」已全部卸载 `asyncio.to_thread`（skills.py ai_fill_skill_meta），同进程其他请求不再被串行化。
3. **时延有界**：上游调用受 `AI_FILL_TIMEOUT_SECONDS`（默认 60s，env `GRAPE_AI_FILL_TIMEOUT_SECONDS` 可调）硬约束，超时即 502，不会无限挂起。
4. **全链路可观测**：kernel 侧每次调用记录调用方/模型/耗时/HTTP 状态/输出字符数，失败四分类（QA C2），为后续实测与限流提供数据基础。
5. **前端已有等待态**：按钮进入「AI 填写中...」busy 态，失败保留已填内容。

后续路线：上线后按真实调用时延实测（p95/p99），若确需更快体验再评估「提任务 + 轮询」的异步化改造（本轮不做，见 QA 修复顺序建议第 2 步）。

## 临时运行参数登记（QA B2 复评口径：五源占位 + env 可配）

以下数值五源依据不足，以「暂用值 + 环境变量覆盖 + `<undecided>` 占位登记」形态运行，
实测定值后回填本节与代码注释（与 `DEDUP_SUFFIX_MAX` 同一整改模式）：

| 参数 | 暂用值 | env 覆盖 | 定值所需测量 |
| --- | --- | --- | --- |
| AI_FILL_MAX_TOKENS | 1200 | `GRAPE_AI_FILL_MAX_TOKENS` | 真实 SKILL.md 样本「摘要+描述+五槽」输出 token 分布与合法 JSON 率 |
| AI_FILL_TEMPERATURE | 0.2 | `GRAPE_AI_FILL_TEMPERATURE` | 同输入多次输出的字段可用率 |
| AI_FILL_TIMEOUT_SECONDS | 60 | `GRAPE_AI_FILL_TIMEOUT_SECONDS` | 上游 p95/p99 时延，并与前端/网关超时对齐 |
| _AI_FILL_CONTENT_MAX_CHARS | 20000 | `GRAPE_AI_FILL_CONTENT_MAX_CHARS` | SKILL.md 正文字长分布 + 截断字段缺失率 |
| _AI_FILL_DESCRIPTION_MAX | 2000 | `GRAPE_AI_FILL_DESCRIPTION_MAX` | DB 列宽 / 前端展示长度约束 |
| DEDUP_SUFFIX_MAX | 999 | `GRAPE_DEDUP_SUFFIX_MAX` | 同 scope 同名变体规模测量 |
| _SKILL_MD_MAX_BYTES | 1048576 | `GRAPE_SKILL_MD_MAX_BYTES` | SKILL.md 正文字长分布实测（QA Informational 2 登记形态对齐） |

A4 豁免确认状态：**已确认（业务方书面登记）**。
分支所有者 / 业务负责人 liudewei_david 于 2026-11-24 在 PR #2080/#2161 评审会话中，
针对 A4 整改方式给出如下业务决定（原文）：

> ai-fill 是用户主动点击「AI 填写」的交互式端点，时延由上游 LLM 决定属预期行为，
> 豁免 <200ms 口径；以 60s 超时硬约束 + to_thread 事件循环保护 + 调用日志 +
> 前端 busy 态作为约束条件；P95/P99 与超时率在上线后实测并回填本登记表。

据此：ai-fill 端点排除 Issue #2403 的 <200ms 端点口径（仅此端点，不外推）；
上线后实测为**强制后续项**，未回填前本豁免保持「有条件」状态。

