# 需求：codegraph 未索引时 project-info 仍报 degraded=false，误导 agent 跳过基线探索

- 来源：Issue #1374（平台反馈单，已按本单转接开发）
- 基线：`origin/weekly-10`（`40be0820a`），本地 `weekly-mys` 已对齐该提交

## 背景

### 现象

`grape git-repo repos project info`（`src/project_info/`）对**未建 codegraph 索引**的仓库返回 `meta.degraded=false` 且 `files` 非空，但生成的 `project-graph.md` / `project-capabilities.md` 各小节内容全是 `No relevant code found / No files indexed. Run codegraph index first`。runbook Step 0 以 `degraded=false` 判定「基线可用、无需逐文件探索」，agent 照做后发现无内容，再退回 `contents/tree` 逐文件核对，判定与执行反复空转。

复现场景（反馈原始）：`GTDTeam/CodeArts`，`meta.degraded=false`、files 非空、所有小节均为占位文案。

### 已核实根因（基线 `origin/weekly-10` 逐函数核对，行号仅参考、以函数名锚定）

1. `src/project_info/builder.py::_codegraph_available()` 只做 `shutil.which("codegraph")`——**装了二进制即视为可用**，不判断该仓库是否已建索引。
2. 只有二进制缺失（`build_project_info` 里的 `if not _codegraph_available()` 分支）、worktree 准备失败、分布式锁丢失三种情况会写 `meta["degraded"]=True`。
3. 正常路径末尾**无条件** `meta["degraded"] = False`；即使 `_codegraph_explore()` / `_codegraph_files()` 全部返回占位文案，也照样写 `degraded=False` 并落盘。
4. 占位文案由 codegraph CLI 输出（不在本仓代码里），所以不能靠「改本仓字符串」修，只能靠**输出特征 + 空段双信号**判定。

### 关键事实（避免误改）

- `_codegraph_explore()` / `_codegraph_files()` 返回 `out or err or ""`，失败时可能是空串也可能是占位文案——两种都要覆盖。
- `meta` 由 `_read_meta` / `_write_meta` 统一读写并写 OBS，新增语义只能复用 `degraded` + `note` 两个既有字段。
- runbook（grape-skills）按 `degraded` 判定分支，本单**不改 runbook**；只保证平台侧信号正确。

## 改动范围

严格 2 组共 5 项；多改一处即打回。

### A 组：未索引判定与落盘（3 项）

- A1 新增模块级常量 `_CODEGRAPH_UNINDEXED_MARKERS = ("no files indexed", "run codegraph index", "no relevant code found")`，与 helper `_codegraph_unindexed(texts: list) -> bool`：任一段 `strip()` 为空，或任一段 `lower()` 命中任一 marker → 返回 True；空列表返回 False。
- A2 `build_project_info` 正常路径末尾（计算完 `arch/modules/chains/files` 四段、写 `meta` 之前）插入判定：`_codegraph_unindexed([files, arch, modules, chains])` 为真时，写 `meta["degraded"]=True`、`meta["note"]="codegraph index missing (no files listed)"`，仍照常写 `project-graph.md` / `project-capabilities.md`，返回 `{"ok": True, "degraded": True, "reason": "codegraph index missing"}`；为假时保持现有 `degraded=False` / `note=""` 行为。
- A3 不动既有三条降级分支（二进制缺失、worktree 失败、lock lost）、不动 `meta` 其它字段、不新增 `indexed` 字段、不改 API 响应结构。

### B 组：测试（2 项）

- B1 新增 `tests/test_project_info_degraded.py`（sqlite 不参与；stub `_run` + 临时目录，不调真实 codegraph）：
  1. `files`/`arch`/`modules`/`chains` 全为占位文案 → `degraded=True`、`note` 含 `index missing`、返回 reason 正确，且两个 md 文件仍被写出；
  2. 四段均为正常内容 → `degraded=False`、`note=""`（防误判）；
  3. `files` 正常、`arch` 为空串 → `degraded=False`（单段缺失不触发）；
  4. `files` 返回空串 → `degraded=True`；
  5. `_codegraph_unindexed` 对空列表返回 False、对中文/大小写混排占位文案命中。
- B2 回归：`_codegraph_available()` 为假时仍走原降级分支（返回 `reason="codegraph unavailable"`）；lock lost / worktree 失败分支返回值不变。

## 明确禁止

1. 不改 runbook / skill 文档（属 grape-skills，另单处理）。
2. 不改 `codegraph` 调用参数、超时、`_KEY_FILES_MAX_DEPTH` 等分析参数。
3. 不新增 API 字段或响应结构（不加 `indexed`；如评审认为必要，另行申请）。
4. 不触发全仓重新索引、不批量重建 project-info、不做数据回填。
5. 不改前端/CLI、不新增依赖、不顺手重构、不改无关日志级别。
6. **不 push、不建 MR、不部署、不重启服务**；只提交到本地 `weekly-mys` 分支并标注「待人工审批」。

## 验收标准

1. 静态核对：`git diff` 只含 `src/project_info/builder.py` 与新增测试文件。
2. 单测 5 个场景（B1）全绿；回归 3 条（B2）不受影响。
3. 实测未索引仓（如 `GTDTeam/CodeArts`）：重建 project-info 后 `meta.degraded=true`、`meta.note` 含 `codegraph index missing`，`repos project info` 接口原样回显。
4. 实测已索引仓（如本仓 `grape-dev/grape`）：`degraded=false` 且 `meta.sections_char_counts` 各项非 0（防误判回归）。
5. 落盘文件仍存在且内容与降级前一致（graph/caps 两个 md 不因判定改动而丢失）。
6. 日志：未索引触发时一条 `log.warning` 说明仓库与原因，不刷屏。

## 参考资料

- 代码锚点：`_codegraph_available()`（仅 which 判断）、`build_project_info` 的 `if not _codegraph_available()` 降级分支、正常路径末尾 `meta["degraded"] = False` 与 `sections_char_counts` 统计。
- 判定依据：`_codegraph_explore` / `_codegraph_files` 均返回 `out or err or ""`，故「空串」与「占位文案」是一对必须同时覆盖的信号。
- 规模预估：生产代码约 25–40 行（常量 + helper + 一处判定），测试约 90–130 行。
- 测试口径：stub `_run` 与 `_codegraph_*`，临时目录；不装/不调真实 codegraph，不连生产。
- 影响面：仅影响 project-info 生成与 `repos project info` 回显的 `degraded/note`；runbook 侧行为变化属 grape-skills，交付说明里单列。