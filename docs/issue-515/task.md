# Task List: Issue #515 — worker 安装包支持 x86_64 Linux 接入

## Task 1: `_find_pythons()` 按架构扫描 + uv --arch 补齐

**Status:** ✅ 已完成

**Description:** 重构 `src/worker_package.py` 的 `_find_python()` 为 `_find_pythons()`：扫描 `UV_PYTHON_DIR` 下各架构 cpython-* 目录（兼容 `cpython-<ver>-<arch>-unknown-linux-gnu` 与 `cpython-<ver>-linux-<arch>-gnu` 两种命名），每个架构取版本最高者；缺失架构尝试 `uv python install 3.12 --arch <arch>` 补齐。返回 `dict[str, str]`。

**Acceptance criteria:**
- [ ] `_scan_pythons()` 能解析两种 uv 目录命名并返回 `{"x86_64": ..., "aarch64": ...}`
- [ ] 同架构多版本时取版本最高者
- [ ] 无 uv / 安装失败时仅告警，返回已找到的架构
- [ ] 保留 `_python_fingerprint(py_root)` 单目录指纹函数

**Verification:**
- [ ] 新增红测试（monkeypatch `UV_PYTHON_DIR` 假目录）→ 绿实现
- [ ] `python3 -m pytest tests/test_worker_package.py -q` 通过

**Dependencies:** None

**Files likely touched:**
- `src/worker_package.py`
- `tests/test_worker_package.py`

**Estimated scope:** S（2 文件）

## Task 2: `build_package()` 双架构分目录捆绑 + 联合指纹

**Status:** ✅ 已完成

**Description:** `build_package()` 将找到的各架构 python 打入 `grape-worker/python-<arch>/`；移除旧 `python/` 单目录捆绑。`package_version()` 的 python 指纹改为 `_pythons_fingerprint()`（各架构目录联合哈希）。`_ensure_wheels(py_ver)` 的 py_ver 取各架构最高版本。

**Acceptance criteria:**
- [ ] tar 内出现 `python-x86_64/...` 与 `python-aarch64/...`（仅含存在的架构）
- [ ] tar 内不再出现 `python/` 单目录
- [ ] 任一架构目录内容变化 → 包版本指纹变化

**Verification:**
- [ ] 新增红测试（假双架构 python 目录 → build_package → 检查 tar 结构）
- [ ] `test_build_package_contents` 的 python 前缀跳过逻辑同步更新
- [ ] `python3 -m pytest tests/test_worker_package.py -q` 通过

**Dependencies:** Task 1

**Files likely touched:**
- `src/worker_package.py`
- `tests/test_worker_package.py`

**Estimated scope:** M（2 文件）

## Task 3: install-worker.sh 架构校验 + 兜底引导

**Status:** ✅ 已完成

**Description:** `scripts/install-worker.sh`：脚本前置计算 `TARGET_ARCH`（`uname -m` 归一化）并加入首条日志；3.5 步改为 `_pick_bundled_python()` 函数——按 `python-$TARGET_ARCH/bin/python3` 优先、旧 `python/bin/python3` 兜底，选中后再试运行 `--version` 双保险；失败则跳过包内解释器、打印提示引导安装系统 Python≥3.10；离线 wheels 安装完成打印架构匹配说明。

**Acceptance criteria:**
- [ ] `TARGET_ARCH` 归一化 `x86_64|amd64→x86_64`、`aarch64|arm64→aarch64`
- [ ] 架构匹配且 `--version` 可执行 → 选用包内解释器
- [ ] 架构不匹配/不可执行 → 跳过并打印系统 Python 引导提示（不出现 Exec format error）
- [ ] 旧包 `python/` 目录仍可兜底（通过 `--version` 校验）
- [ ] 日志打印 `Target arch` 与所选解释器/wheels 架构（全英文，过 `test_sh_outputs_english`）

**Verification:**
- [ ] 新增红测试：提取 `_pick_bundled_python` 函数体 harness（匹配/不匹配/旧包兜底三分支）
- [ ] `python3 -m pytest tests/test_worker_install_arch_check.py tests/test_worker_install_ux.py -q` 通过

**Dependencies:** None（与 Task 1/2 并行可，安装端独立）

**Files likely touched:**
- `scripts/install-worker.sh`
- `tests/test_worker_install_arch_check.py`（新增）

**Estimated scope:** M（2 文件）

## Task 4: 回归

**Status:** ✅ 已完成

**Description:** 全量相关测试回归，确认 aarch64 既有 worker 升级路径与 Windows 安装不受影响。

**Acceptance criteria:**
- [ ] worker_package / install ux / rollback / windows install 相关测试全绿

**Verification:**
- [ ] `python3 -m pytest tests/test_worker_package.py tests/test_worker_install_ux.py tests/test_install_rollback_regression.py tests/test_issue_452_windows_install.py tests/test_windows_worker.py -q`

**Dependencies:** Task 1-3

**Files likely touched:** 无（仅运行测试）

**Estimated scope:** S

## Checkpoint

- [x] 测试全绿
- [x] 自审查 5 轴（code-review-and-quality.md）通过
- [x] 敏感信息增量扫描（pre-pr-release-check.md §2）通过
- [ ] 建 PR + Issue 评论 + A2A QA