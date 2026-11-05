# Plan: Issue #666

## 改动清单

| 文件 | Milestone | 改动 |
|------|-----------|------|
| src/project_info/builder.py | M1/M2 | `_warn_codegraph_missing`（一次性显式告警，含影响+恢复指引）；降级分支调 `_degraded_module_candidates`（git tree 优先，GitCode API 兜底）填充 Architecture Overview / Module Dependency Graph / Key Files；meta.note 记录候选来源 |
| Dockerfile | M3 | 构建期 npm 安装 @colbymchenry/codegraph（自包含二进制），完成后卸载 nodejs/npm；失败不阻断构建 |
| requirements-worker.txt | M4 | 注释说明 codegraph 为 npm 二进制分发，非 pip 依赖 |
| src/worker_package.py | M4 | MANIFEST 纳入 scripts/install-codegraph.sh（随包离线安装入口） |
| scripts/install-worker.sh | M4 | 新增 codegraph 预装步骤（已装跳过；npm 可用则在线装；否则提示离线脚本）+ 安装后 `codegraph --version` 校验（缺失告警不阻断） |
| tests/test_project_info_builder.py | M5 | 新增：降级候选生成（git tree mock）/ API 兜底 / 无来源回退 N/A / 告警一次性 |
| tests/test_worker_package.py | M5 | 新增：MANIFEST 含 install-codegraph.sh 守卫 |

## 降级候选数据流（M2）

```
build_project_info
  └─ codegraph 不可用分支
       ├─ _git_tree_paths(repo_key)        # bare mirror git ls-tree（origin/HEAD→main→master，两级深度，条目上限）
       ├─ 空 → _api_tree_paths(...)        # GitCode API contents 根路径（token 兜底）
       └─ _degraded_candidates_md(paths)   # 顶层目录=模块候选；src/ 等关键目录 depth-2 展开=Key Files
```

全部 best-effort：来源不可用时回落现状（N/A + 告警），不影响主链路。

## 验证（M5）
- `pytest tests/test_project_info_builder.py tests/test_worker_package.py -q`
- 降级 meta 仍 `degraded=true`（现有断言不回归）
