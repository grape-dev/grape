# Spec: Issue #1635 — issue-cli 物化技能包与仓库基线内容漂移

## Objective
消除技能物化包与仓库基线的双向漂移，使漂移在物化时可见/可阻断，Agent 不再依据
仓库中不存在的文件做分析。

## 现状（线上数据）
- 本任务物化 `issue-cli` v3.3.1 / skill_id=3842 / scope=user，references 含
  `cross-scene-handoff.md`（Step 4b 跨场景转交）。
- 官方基线 v3.1.0 / skill_id=2755 / scope=official，references 9 件，无该文件。
- 现 `validate_skill_dir` 只查「声明清单 ⊆ 包内」，不查「包 ⊆ 基线」与版本一致，
  故反向漂移（包多基线外文件）静默通过。

## Scope
### In Scope
1. 双向校验：`collect_manifest(dir)`、`package_digest()`（排序 rel+size sha256 前 16 位）。
2. `validate_skill_dir(..., baseline_dir=None, scope="")` 返回
   `(ok, missing, extra, version_drift)`；`extra = 包文件 - 基线文件`。
3. zip/files/dir 三渠道统一校验，结果增 `drift{extra,missing,version}` 与
   `source{skill_id,scope,type,version,digest}`。
4. 分 scope 处置：official 漂移 → 既有回退 + 阻断告警；user/repo → 仅告警不阻断。
5. 来源透传：`build_skill_source_message` 扩展字段，服务端/worker 调用点补传。
6. 版本语义：`baseline_lag` / `stale_package` / `content_mismatch`，基线缺失
   `baseline_unavailable` 不阻断。

### Out of Scope
- 不改 #367 scope 优先级、上传/市场 UI；不做基线自动发 PR；不清历史物化缓存。
- `grape-dev/grape-skills` 仓库资产补齐（cross-scene-handoff.md、3.4.0、兜底正则）
  需在关联仓库单独提交，本仓只落平台校验链路。

## Acceptance
- 同一 issue-cli 重新物化后 `drift=0`，来源行含 `skill_id/scope/version/digest`。
- official 漂移阻断并回退基线；user/repo 仅告警。
- 基线缺失不误阻断；新增 7 条用例通过，`test_issue1070/887/367/574` 全绿。