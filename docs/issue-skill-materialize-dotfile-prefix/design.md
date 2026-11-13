# 修复：技能物化前缀剥离用 lstrip("./") 导致点开头文件被削名

- 目标分支：`weekly-mys`（运行域，`3f40dd97`）
- 基线：`origin/weekly-mys` @ `28f8948ff`；对照 `origin/weekly-10` @ `4ebe33b7b`
- 文档日期：2026-11-10
- 类型：确定性缺陷（可复现，已实测）

## 1. 问题

`src/runtime/skill_materialize.py` 里两处把相对路径归一化写成：

```python
rel = str(rel).lstrip("./")
```

`str.lstrip(chars)` 是**按字符集合**剥离开头的任意字符，不是剥前缀字符串。字符集合 `{".", "/"}` 会把路径开头的**所有点**和斜杠都吃掉：

```
".gitignore"    -> "gitignore"
"./.gitignore"  -> "gitignore"
"../evil"       -> "evil"        # 旧写法"顺带"挡住的目录穿越，其实是被削成普通名
```

只要技能包里有 `.gitignore` / `.env.example` / `.grader/` 这类点开头条目，路径就被改写。

## 2. 现状（weekly-mys 行号实读）

| 位置 | 内容 |
|---|---|
| `skill_materialize.py:260` | `def validate_skill_dir(skill_dir, expected_files=None, baseline_dir=None, scope="")` |
| `skill_materialize.py:281` | `rel = str(rel).lstrip("./")` ← 校验清单归一化 |
| `skill_materialize.py:370` | `def materialize_skill_files_atomic(skills_root, skill_name, files, ...)` |
| `skill_materialize.py:387` | `rel = str(rel).lstrip("./")` ← 写盘 + expected 同时归一化 |

**weekly-10 同一缺陷存在**：`validate_skill_dir` 在 `:99`、`lstrip` 在 `:115`；`materialize_skill_files_atomic` 在 `:139`、`lstrip` 在 `:153`。修复需按函数名在两分支分别对齐（不要整段 cherry-pick）。

## 3. 两种失败模式（都已实测，非推断）

实测环境：`/tmp/wt-mys`（detach 到 `origin/weekly-mys`），`.venv` 直跑。

### 3.1 校验路径整包失败（用户报的现象）

`materialize_skill_zip_atomic`（`skill_materialize.py:410`）与 `materialize_skill_dir_atomic`（`:475`）用 `os.walk` + `os.path.relpath` 生成 `expected`，点文件得到的是 `.gitignore`（不带 `./`）。`validate_skill_dir` 再 lstrip 成 `gitignore`，磁盘上没有 `gitignore` → 判缺失 → `ok=False` → **整个技能包物化失败**（`reasons` 里写 `missing: gitignore`，调用方再走降级/FALLBACK）。

实测输出：

```
validate_skill_dir(d, ['.gitignore'])  -> (False, ['gitignore'])   # 文件其实存在
validate_skill_dir(d, ['gitignore'])   -> (False, ['gitignore'])
```

### 3.2 老 task 文本格式静默改名（更隐蔽的数据损坏）

`materialize_skill_files_atomic` 对**写盘路径**和 **expected** 用同一个 lstrip，两边一起被削，校验反而通过，但落盘文件名已经变了：

```
materialize_skill_files_atomic(root, 'demo', {'SKILL.md': '# demo', './.gitignore': 'dist/'})
-> ok=True
-> on-disk: ['SKILL.md', 'gitignore']
```

即：`.gitignore` 变成 `gitignore`，git 不再识别，技能包内容与源包不一致且没有任何报错。

## 4. 期望改动

### 4.1 真正的前缀剥离

```python
rel = rel[2:] if rel.startswith("./") else rel
```

两处都改（`:281` 与 `:387`）。注意 `os.path.relpath` 产出的路径本来就不带 `./`，所以这条改动对 zip/dir 路径是**保持原值**（正是要的），对 files 字典里带 `./` 前缀的键才是去前缀。

### 4.2 必须同时补路径安全闸（去掉 lstrip 就失去了"顺带"的穿越防护）

旧写法把 `../evil` 削成 `evil` 属于偶然挡道；改成真前缀剥离后，`os.path.join(tmp_dir, "../evil")` 会**写出 tmp_dir 之外**。所以归一化后必须显式拒绝：

- 绝对路径（以 `/` 开头，或带盘符如 `C:`）
- 任一路径段为 `..`（先按 `\` 和 `/` 切段判断，兼容 Windows 包）
- 归一化后为空 / `.`

建议抽成模块内私有 helper `_normalize_rel_path(rel) -> str`（非法抛 `ValueError`），两个函数共用：

- `validate_skill_dir`：非法 expected 计入 `missing`（fail closed，不放行）
- `materialize_skill_files_atomic`：非法返回 `(False, "illegal relative path: <rel>")`

### 4.3 单测（新增到 `tests/test_skill_materialize.py`）

| # | 用例 | 断言 |
|---|---|---|
| T1 | zip 包内含 `SKILL.md` + `.gitignore` + `.grader/rules.md` | `materialize_skill_zip_atomic` 返回 ok=True；落盘存在 `.gitignore`、`.grader/rules.md` |
| T2 | files 字典键 `./.gitignore` / `.gitignore` 两种形态 | `materialize_skill_files_atomic` ok=True；磁盘上是 `.gitignore`，**不存在** `gitignore` |
| T3 | 目录物化源目录含点文件 | `materialize_skill_dir_atomic` ok=True 且点文件保留 |
| T4 | expected 含 `../evil`、`/abs/path`、`C:\x` | 判为非法（missing 或 False），且**不在 skills_root 之外/内产生该文件** |
| T5 | 回归：既有 `tests/test_skill_materialize.py` 全绿 | 无新增失败 |

## 5. 风险与边界

- **不要用 `os.path.normpath` 替代归一化**：它不处理 `./` 语义变化、且会把 `a/../b` 折叠成 `b`，与"拒绝穿越"的口径冲突。
- **Windows 包**：zip 内可能用 `\` 分隔；段判断需同时认 `\` 与 `/`（本缺陷修复后再决定是否统一转 `/` 落盘，属可选）。
- **降级/FALLBACK 不动**：物化失败后的 FALLBACK 写入与提示不在本单。
- **交叉引用 #1694**（`grape-architecture-guardian` 物化降级只剩 FALLBACK）：如果该技能包内含点文件，本缺陷就是"整包物化失败"的直接原因之一；但 #1694 里的 FALLBACK 命令 `--skill` → `--skill_id` 属 CLI/文档问题，**不在本单**。修完本单可用该包做一次真实复验，把结论回写 #1694，不要顺手改 CLI。
- 与 #1070（跨技能引用误判、content_hash_package 污染）根因不同，不合并处理。

## 6. 验收

- A1 含 `.gitignore` 的技能包：zip / 目录 / files 三条物化路径全部成功，落盘文件名与源包逐字相同。
- A2 含嵌套点目录（`.grader/rules.md`）同样成功。
- A3 非法相对路径（`..`、绝对路径）被拒且不落盘到目标目录之外。
- A4 `tests/test_skill_materialize.py` 既有用例全绿；新增 4 条用例覆盖 T1–T4。
- A5 复验 #1694 的技能包（若其确实含点文件），把"是否由本缺陷导致"的结论写回 #1694。

## 7. 明确不做

- 不改物化失败后的 FALLBACK/降级逻辑与文案。
- 不改 `grape skills detail` 的 CLI 参数（#1694 的第二层问题）。
- 不改 `compute_skill_drift` / 基线漂移判定口径。
- 不重构 `skill_materialize.py` 的其他函数。