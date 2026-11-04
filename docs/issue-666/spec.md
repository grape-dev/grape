# Spec: Issue #666 — codegraph 不可用导致项目知识基线降级生成

> 发布自 Issue #666 评论 id=197549542（评级 ready，标签 spec-ready）。开发基准分支：weekly-mys（原 Spec 标注 weekly-09，本次以当前流水线基线为准）。

## Objective
恢复项目知识基线全量生成能力——在引擎/worker 基线镜像预装 codegraph，并增强 codegraph 不可用时的降级生成路径（补充模块级候选），消除降级链路向下游 Spec 的传导。

## 成功标准
- codegraph 就位后 `meta.degraded=false`、全量恢复；
- 缺失时降级文档含模块级候选（非 N/A），且环境自检显式告警（含影响说明与恢复指引）。

## Scope
1. 引擎基线镜像（Dockerfile）预装 codegraph
2. worker 侧（requirements-worker.txt / worker_package.py / install-worker.sh）预装 + 校验
3. 环境自检：codegraph 缺失显式告警（首次 generate，一次性）
4. 降级生成路径增强：git tree + GitCode API 路径推导模块级候选

## Milestones
- M1 环境自检与告警增强（builder.py）
- M2 降级路径模块级候选增强（builder.py 降级分支 + git tree/API）
- M3 引擎基线镜像预装（Dockerfile）
- M4 worker 侧预装（requirements-worker.txt / worker_package.py / install-worker.sh）
- M5 验证：就位全量恢复 + 缺失降级候选 + 告警

## 关键决策（实现时确认）
- codegraph 分发形式：npm 平台二进制包 `@colbymchenry/codegraph-linux-<arch>`（自包含：内置 node 运行时 + Rust 内核），**非 pip 包**（`scripts/install-codegraph.sh` 已注明）。
  - 引擎镜像（M3）：构建期 `npm install -g @colbymchenry/codegraph`，随后卸载 nodejs/npm 保持镜像精简；失败 best-effort 不阻断构建（运行时降级路径兜底）。
  - worker 侧（M4）：codegraph 非 pip 依赖 → requirements-worker.txt 仅注释说明分发形式；安装包随包下发 `scripts/install-codegraph.sh`（MANIFEST 纳入）；install-worker.sh 增加安装后 `codegraph --version` 校验（缺失告警，不阻断注册）。
