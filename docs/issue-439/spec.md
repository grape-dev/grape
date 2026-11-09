# Spec: Issue #439 — Windows 提权引擎 overlay home 全量 symlink 致 hermes ACP 任务必死 + 健康分拒派死锁

> 来源：Issue #439 Spec Document（Issue Agent 评级 ready）｜2026-10-23

## 背景与根因

- `src/agent/hermes_acp.py::_build_overlay_home` 把共享 HERMES_HOME 的全部条目 symlink 进 per-task overlay home。
- Windows 提权引擎（管理员运行）下，symlink = NTFS reparse point；hermes 侧 `hermes_dir_acl` 拒绝给 reparse point 授权 → 引擎写 cron/memories 即失败 → 任务 exit_code=-1。
- 一次失败后健康分按 `round(100*(1-failure_rate)) - 连败惩罚` 计算：total=1/failed=1 → 0 分；`claim_by_tag` 健康分门禁（阈值 30 / 连败 ≥5）拒派 → 引擎永久 queued（实测连续 28 次 skip claiming）。

## 目标

1. overlay home 可写状态目录为真实目录、lock 类为真实文件、只读资产保留 symlink。
2. 构建期清理历史遗留同类旧链接（替换 `if os.path.exists(dst): continue` 放过行为）。
3. worker 侧兜底：启动前对可写目录做 reparse point 检测与就地替换（兼容存量 home / 竞态）。
4. 健康分小样本保护：任务样本数 < N 时豁免连败惩罚、稀释失败率，避免单次失败打入拒派区；N 可配置（env `HEALTH_MIN_SAMPLES`，默认 5）。

## 范围

In：`src/agent/hermes_acp.py`（分类 + 清理 + 兜底）、`src/runtime/health_scorer.py`（小样本豁免）、tests 新增单测。
Out：hermes 上游 hermes_dir_acl 放宽、Linux overlay 隔离语义变更（不得回归）。

## 分类规则（显式白名单）

| 条目类型 | 判定 | 处理 |
|---|---|---|
| 可写状态目录 | cron/sessions/logs/log/cache/tmp/temp/memories/history/backups/output/outputs/data | 真实目录 `os.makedirs` |
| lock/pid 文件 | processes.json/.port-locks/gateway.pid/hermes.pid 及 `*.lock` | 真实空文件 |
| 只读资产目录 | skills/plugins/venv/assets/themes/prompts | symlink（失败降级 `_mirror_without_symlink`） |
| 只读资产文件 | 其余文件（auth.json 等） | symlink（失败降级复制） |
| 未知目录 | 不在白名单的目录 | 真实目录（保守——宁可多建真实目录，不再产生 reparse point） |

遗留清理：dst 已存在且为指向共享 home 同名条目的旧链接/类型不符 → 删除后按新规则重建；类型相符的真实目录/文件保持幂等跳过。

## 健康分小样本保护

- `_compute_health_score`：`0 < total < N` 时失败率按 `failed / N` 稀释、连败惩罚置 0，再按试错友好公式（失败率权重 0.5）计分（total=1/failed=1 → 90 分，高于拒派阈值 30）。
- `is_healthy`：连败 ≥ MAX 的拒绝仅在样本数已知且 ≥ N 时生效（健康分阈值判定维持不变）。
- N = env `HEALTH_MIN_SAMPLES`（默认 5）。

## 验收标准

- Windows 提权引擎写 cron/memories 不再触发 ACL 拒绝且任务 exit_code=0（代码路径保证：可写目录为真实目录，无 reparse point）。
- overlay home 可写目录为真实目录、只读资产仍为 symlink；遗留旧链接构建期被清理。
- 低样本引擎单次失败不打入拒派区（90 ≥ 30）。
- Linux 不回归（symlink 路径保留 + 现有测试全过）+ 新增单测覆盖上述逻辑。
