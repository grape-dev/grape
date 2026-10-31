# Plan: Issue #564 技能包原子物化与完整性校验

## 1. 新增公共模块 `src/runtime/skill_materialize.py`

服务端（event_pipeline / scheduler direct 物化）与 worker（agent.py 任务执行）两侧共用，零 DB 依赖（worker 无库）。

API：
- `parse_skill_md_references(skill_md_path) -> list[str]`：解析 SKILL.md 中提及的 `references/...` 相对路径（含 frontmatter 之外正文引用）。
- `validate_skill_dir(skill_dir, expected_files=None) -> (ok, missing)`：SKILL.md 必须存在；expected_files（技能定义清单：zip 内文件名 / skill_files 键）逐一存在；另扫描 SKILL.md 引用的 references 文件存在。
- `materialize_skill_files_atomic(skills_root, skill_name, files) -> (ok, reason)`：写 `skills_root/.tmp/<uuid>/` → 校验 → 原子 rename 到 `skills_root/<skill_name>/`；目标已存在且校验通过 → 幂等跳过；空/坏目标先删再 rename；失败清理 tmp。
- `materialize_skill_zip_atomic(skills_root, zip_bytes) -> (ok, skills, reason)`：解压 tmp（按 external_attr 恢复 Unix 权限，剥离 setuid/setgid/sticky）→ 校验每个顶层技能目录 → 逐个原子 rename。
- `materialize_skill_with_fallback(skills_root, skill_name, primary) -> dict`：primary 为闭包，返回 `("zip", bytes)` / `("files", dict)` / `("dir", path)` / `("none", reason)`。主渠道失败或缺 SKILL.md/references → 回退官方技能包（`resolve_official_skill_path` → copytree 原子落盘）→ 仍失败返回降级。返回 `{"status": "ok"|"fallback"|"degraded", "detail": str}`。

## 2. 接入点

| 模块 | 改动 |
|------|------|
| src/api/event_pipeline.py `_materialize_direct_workdir` | 技能段改调 `materialize_skill_with_fallback`；结果写入 `evt["_skill_materialize"]`；fallback/degraded 时向 prompt 追加降级说明（含 DEVOPS_SKILLS_DIR 路径或缺失原因） |
| src/api/event_pipeline.py `_execute_agent_inner` | 物化后读取 `evt["_skill_materialize"]`，fallback/degraded 追加一条 agent_logs 落入事件（事件级降级标注） |
| src/runtime/agent.py（worker _execute_task 技能物化段） | zip 路径改 `materialize_skill_zip_atomic`；skill_files 路径改 `materialize_skill_files_atomic`；均带校验+官方包回退；仅 ok/fallback 时设置 `env["DEVOPS_SKILLS_DIR"]`（不静默空注入）；degraded 时向 prompt 追加降级说明 |
| src/runtime/scheduler.py | autopilot direct 复用 `_materialize_direct_workdir`，自动获得同能力（无需单独改动） |
| src/prompt_builder.py `build_devops_agent_prompt` 技能段 | 追加技能内容位置说明：SKILL.md 主文件 + `references/` 布局 + `DEVOPS_SKILLS_DIR` 环境变量指向同一目录；文件缺失时用 `grape skills detail` 取回的指引 |
| src/runtime/scheduler.py `_build_autopilot_prompt` | 同步补充布局说明 |

## 3. 关键决策（沿 Spec）

- 物化时机：派发侧同步物化（与路径变量注入原子配套）。
- 失败策略：回退官方包 + 降级标注后继续派发（不阻断）。
- 校验依据：技能定义清单（zip 名单 / skill_files 键）∪ SKILL.md 内引用的 references 文件。
- 写入方式：tmp + 原子 rename；失败清理 tmp，不留半写目录。

## 4. 测试 `tests/test_skill_materialize.py`

1. files 物化成功（SKILL.md + references 齐全，rename 后可见）。
2. zip 物化成功（含权限恢复）。
3. 清单缺 SKILL.md / 缺 references → 主渠道判失败 → 官方包回退 → status=fallback。
4. 回退也失败（官方包不存在）→ status=degraded，目标目录未被污染。
5. 原子性：rename 阶段注入异常 → tmp 清理、目标目录不存在。
6. 幂等：目标已存在且合法 → 跳过不报错。
7. event_pipeline `_materialize_direct_workdir` 降级时 evt 标注 + prompt 追加说明（monkeypatch 主渠道为 none、官方包不存在）。
