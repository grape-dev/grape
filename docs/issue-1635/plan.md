# Plan: Issue #1635 技能基线双向漂移校验

## 技术方案
1. `src/runtime/skill_materialize.py`
   - `collect_manifest(dir)`：os.walk 相对路径，排除 `.tmp*` 与 `FALLBACK.md`，排序。
   - `package_digest(dir)`：排序 `rel+size` sha256 前 16 位。
   - `read_skill_version(dir)` / `_semver_tuple` / `_version_kind`：frontmatter 版本与语义比较。
   - `resolve_baseline_dir(name)`：复用 `src.config.resolve_official_skill_path()` 取 dirname。
   - `compute_skill_drift(pkg, base)`：`{baseline_available, extra, missing, version{package,baseline,kind}}`。
   - `drift_blocks(drift, scope)`：official + stale_package/content_mismatch 才阻断；
     baseline_lag 仅提示补基线；baseline_unavailable 与 user/repo 不阻断。
   - `validate_skill_dir(..., baseline_dir=None, scope="")` → 4 元组。
   - 三个 `materialize_skill_*_atomic` 增加 `baseline_dir/scope/meta`，安装前统一调用
     `_apply_baseline_check`；official 阻断返回失败走既有回退。
   - `materialize_skill_with_fallback` 增加 `scope/skill_type/skill_id/baseline_dir`，
     成功/回退结果增 `drift` 与 `source`。
2. `src/runtime/task_log_stream.py`：`build_skill_source_message(..., source_info, drift)`
   把 skill_id/scope/type/version/digest 与 drift 写入来源行与 `skill_source`。
3. 调用点：`event_pipeline._materialize_direct_workdir` 从 `get_skill_by_id` 取 scope 透传，
   `evt._skill_materialize` 携带 drift/source；`direct_executor` 来源日志补传；
   `agent._materialize_worker_skill` 解析 scope、计算最终 drift/source 存
   `task._skill_materialize_meta`；`agent._skill_consistency_gate` 来源上报补传。

## 风险
| 风险 | 处置 |
|------|------|
| worker 无基线目录 | baseline_unavailable，仅版本占位，不阻断 |
| 旧 frontmatter 无 version | 记 unknown，按文件差异 content_mismatch 判定 |
| 个人技能被误阻断 | 仅 official 阻断，user/repo 告警 |
| 兼容旧调用 | baseline_dir/scope 缺省不启用漂移校验，返回元组扩为 4 元组并同步内部调用 |

## 测试
新增 `tests/test_issue1635_skill_baseline_drift.py` 7 条：manifest/digest、extra+baseline_lag、
official 不误阻断、user 告警+zip 渠道、stale_package 阻断、content_mismatch 阻断、
baseline_unavailable + 来源行字段。回归 1070/887/367/574 与 skill_materialize。