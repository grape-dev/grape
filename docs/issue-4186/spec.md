# Issue #4186 需求文档（spec）

> 目标：任务派发时注入给 Agent 的技能路径与实际物化的工作区目录一致，Agent 按提示即可 read 到 SKILL.md，无需回退试错。

- 来源反馈 1304（VOD-Project issue 263 / event 24949）：prompt 提示 `issues/263/issue/.skills`（按 issue agent 命名），实际物化 `issues/263/sre/.skills`（按路由命名）；提示路径 read → File not found，靠 DEVOPS_SKILLS_DIR 兜底。
- 与 #564（DEVOPS_SKILLS_DIR 目录全空）/ #3663（DEVOPS_SKILLS_DIR 指向未物化目录）注入面相反：本单 DEVOPS_SKILLS_DIR 可读，坏的是 prompt 提示路径。
- 完整 Spec 见 Issue #4186 评论「Spec Document」（issue agent 发布，评级 ready）。

## 验收标准

1. prompt 提示的技能目录存在，SKILL.md 直接 read 成功；
2. 工作区目录与 prompt 注入路径构造逻辑同源（同一 agent 名/路由来源），含「路由名 ≠ agent 类型」单测；
3. 物化降级/目录缺失时 prompt 显式标注 degraded 并给出回退路径（DEVOPS_SKILLS_DIR 或 skills detail）；
4. #564 / #3663 既有场景回归通过。
