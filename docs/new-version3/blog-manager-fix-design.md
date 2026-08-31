# 修复方案 B：blog-skill-creator 根因修复（审查修订版）

## 根因

blog-skill-creator 生成 skill 时可能遗漏文件或结构不符合 spec_check 要求，
原因是 Step 3.5 自测只跑了 `--help` 语法检查，没有跑 spec_check.py 做文件完整性校验。
另外 spec_check 本身有 2 处检查级别和目录结构的不一致需要修复。

## 修复内容（3 项）

### B1. 修改 Step 3.5：加 spec_check.py 强制校验

blog-skill-creator/SKILL.md 的 Step 3.5 改为：

```
- 跑 python3 skills/{name}/scripts/{name}.py --help → 确认无语法错误
- 跑 spec_check.py 做文件完整性校验：
  python3 {{SKILL_DIR}}/blog-skill-tester/scripts/spec_check.py skills/{name}/
  校验项：
    - SKILL.md frontmatter（name/version/description/triggers/tags/tools）
    - SKILL.md 必填段落（Overview/Prerequisites/Workflow/Core Commands/Parameter Confirmation/Reference Documents）
    - scripts/ 目录有可执行 .py
    - references/api-reference.md 存在
    - templates/test-vars.json 存在（含 skill_name/auth_type/cred_prefix/base_url/test_cases）
    - 无 mock 代码
    - 无密钥泄露
- 跑真实 API 调用一次（只读端点）→ 确认 API 可达
- spec_check 或 API 调用失败 → 修复后重试；无法修复 → Issue 评论说明，不创建 PR
- 全部通过 → 继续 Step 4 创建 PR
```

### B2. spec_check.py：test-vars.json 从"推荐"改为"必须"

当前 spec_check.py 对 test-vars.json 的检查是：
```python
# templates/test-vars.json 推荐存在
test_vars = os.path.join(skill_dir, "templates", "test-vars.json")
if not os.path.exists(test_vars):
    problems.append("templates/test-vars.json 缺失（推荐：测试用例文件）")
```

改为：
```python
# templates/test-vars.json 必须存在（tester 依赖此文件读取子命令列表和 base_url）
test_vars = os.path.join(skill_dir, "templates", "test-vars.json")
if not os.path.exists(test_vars):
    problems.append("templates/test-vars.json 缺失（必须：tester 依赖此文件）")
```

### B3. spec_check.py：scripts/ 目录结构适配

当前 spec_check 只检查 `scripts/` 目录下有没有 .py 文件。
但生成的 skill 可能用 `main.py`（根目录）+ `包名/`（Python 包）的结构，不一定有 `scripts/` 目录。

改为：检查 scripts/ 目录 或 根目录有 .py 入口文件：
```python
# Python 入口文件：scripts/ 目录 或 根目录 *.py（排除 __init__.py / setup.py / conftest.py）
scripts_dir = os.path.join(skill_dir, "scripts")
root_py = [
    f for f in os.listdir(skill_dir)
    if f.endswith(".py") and f not in ("__init__.py", "setup.py", "conftest.py")
]
if not os.path.isdir(scripts_dir) and not root_py:
    problems.append("无 Python 入口文件（scripts/*.py 或根目录 *.py）")
elif os.path.isdir(scripts_dir):
    py_files = [f for f in os.listdir(scripts_dir) if f.endswith(".py")]
    if not py_files:
        problems.append("scripts/ 无 .py 脚本")
    for f in py_files:
        p = os.path.join(scripts_dir, f)
        if not os.access(p, os.X_OK):
            problems.append(f"scripts/{f} 不可执行（缺 +x）")
```

注意：根目录 .py 排除 `__init__.py`（包标识）、`setup.py`（打包配置）、`conftest.py`（pytest 配置），只认真正的入口文件（如 `main.py`）。os.listdir 不递归，不会把 `tests/conftest.py` 算进去。

### B4. blog-skill-creator/SKILL.md：脚本路径不强制 scripts/{name}.py

当前 creator 的生成清单和模板都写死了 `scripts/{name}.py` 路径：
- Step 3：`scripts/{name}.py`
- Step 3.5：`python3 skills/{name}/scripts/{name}.py --help`
- blog-skill-template.md：`python3 scripts/{skill_name}.py {subcommand}`
- test-vars.json 示例：`python3 scripts/{skill_name}.py list-articles`

但 agent 实际生成时用了 `main.py` + `blog_manager/` 包结构——这是合理的工程实践（模块化）。

改为：不强制脚本路径，允许两种结构：
- `scripts/{name}.py`（单文件结构，简单 skill）
- `main.py` + `{name}/`（包结构，复杂 skill）

具体改动：
- Step 3 生成清单：`scripts/{name}.py` → `入口脚本（scripts/{name}.py 或 main.py + 包结构）`
- Step 3.5 自测：`python3 skills/{name}/scripts/{name}.py --help` → `python3 skills/{name}/{入口文件} --help`
- blog-skill-template.md：使用示例改为 `python3 {入口文件} {subcommand}`
- test-vars.json 示例 command：`python3 scripts/{skill_name}.py` → `python3 {entry_file}`（creator 生成时替换为实际入口路径）

## 不改的部分

- Step 0-3 的流程不变
- Step 4 创建 PR 不变
- Step 6 A2A 交棒不变
- blog-skill-template.md 不改（已有段落模板和 test-vars.json 结构定义）
- test-vars.json 结构定义不改

## 修复后的执行链路

```
Step 3 骨架生成
  ↓ 生成 SKILL.md + scripts/{name}.py + templates/test-vars.json + references/api-reference.md + requirements.txt
Step 3.5 自测
  ↓ python3 {name}.py --help（语法检查）
  ↓ spec_check.py skills/{name}/（文件完整性校验——必须通过）
  ↓ 真实 API 调用（只读端点验证）
  ↓ 全部通过 → Step 4
  ↓ 失败 → 修复后重试 / 不创建 PR
Step 4 创建 PR
```

## 影响范围

| 文件 | 改动 | 说明 |
|------|------|------|
| blog-skill-creator/SKILL.md | Step 3.5 加 spec_check 校验 | B1 根因修复 |
| blog-skill-creator/SKILL.md | 脚本路径不强制 scripts/{name}.py | B4 允许 main.py + 包结构 |
| blog-skill-creator/references/blog-skill-template.md | 使用示例路径通用化 | B4 |
| blog-skill-tester/scripts/spec_check.py | test-vars.json 推荐→必须 | B2 对齐 tester 依赖 |
| blog-skill-tester/scripts/spec_check.py | scripts/ 目录检查适配根目录入口 | B3 兼容两种结构 |

## 验证方式

修复后重新用 creator 生成 blog-manager，检查：
1. spec_check.py 在 Step 3.5 被调用
2. 缺 test-vars.json → spec_check 报失败 → 不创建 PR
3. test-vars.json 自动生成（含 base_url + test_cases）
4. references/api-reference.md 自动生成
5. SKILL.md frontmatter 6 字段齐全
6. SKILL.md 6 个必填段落齐全
7. scripts/ 目录或根目录 .py 入口都能通过 spec_check
