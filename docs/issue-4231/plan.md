# Plan: Issue #4231 — 方案设计（arch-weekly-11-ljt 重落位）

> 承接基线：`arch-weekly-11-ljt`（原 `arch-weekly-11-yaw` 基线上的 PR #2196 未合入本基线，按 Spec v3 重新落位，勿沿用旧分支）。

## 方案概述
新增纯函数式名称校验工具（ASCII 字符集正则 + 长度上限常量），check-name 与 create 均调用；
后端错误只回裸原因，由前端统一加一次「检查失败」前缀。

## 关键决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 前缀归属 | 前端统一加（后端去掉） | 文案属呈现层，前端已有 `err_check_failed` / `templates.check_failed` 模板，改动面最小 |
| 长度上限 | 100（各上游平台最小公约数） | 避免误挡历史上可建仓库 |
| 字符集 | 显式 ASCII `[A-Za-z0-9._-]` | Python `\w` 默认匹配 Unicode，旧 create 正则会放行中文名 |
| 校验实现 | 抽公共工具（新增 `repo_naming.py`） | 需求 A 明确要求，防再次漂移 |

## 实现路径
1. 新增 `src/domains/mgmt/user/repo_naming.py`：`validate_repo_name(name, label) -> (ok, reason)`，
   内含 `REPO_NAME_PATTERN`（ASCII）与 `REPO_NAME_MAX_LENGTH = 100`；空/超长/非法字符/`.`/`..` 四类裸原因。
2. `src/domains/mgmt/routes/repos.py`：
   - `check_repo_name()` 在 token 解析前先 `validate_repo_name`，失败 400 + 裸原因（上游 0 调用）；
   - 后端 502 文案去掉 `f"检查失败: {…}"` 拼接（:1274/:1280），只回脱敏后的裸原因；
   - `create_new_repo()` name/path 改用同一工具，删除两处内联 `^[\w\-\.]+$`。
3. 前端 `src/api/frontend/app/src/views/TemplatesView.vue` `doCreate()`：err 为对象时用
   `t('templates.check_failed', { msg })` 补一次前缀；字符串 err（auto rename 耗尽文案）直接展示。
4. 测试：
   - 新增 `tests/mgmt/test_issue_4231_repo_name_validation.py`（合法/空格/中文/超长/点段；
     check-name 非法 400 且零 token/上游调用；create 拦中文；两入口同输入同原因）；
   - 更新 `tests/mgmt/test_issue_3563_reauth_401_split.py`：502 message 不再含「检查失败」。

## 影响模块
| 模块 | 改动 | 说明 |
|------|------|------|
| `src/domains/mgmt/user/repo_naming.py` | 新增 | 名称校验工具（正则 + 长度） |
| `src/domains/mgmt/routes/repos.py` | 修改 | check-name 前置换校验；create 复用；错误去前缀 |
| `src/api/frontend/app/src/views/TemplatesView.vue` | 修改 | 模板创建流程补单层前缀 |
| `src/api/frontend/shell/locales/{zh,en}/repos.json` | 核对 | `err_check_failed` 保留唯一一层前缀（不改） |
| `tests/mgmt/` | 新增/更新 | 校验单测 + 接口回归 |

## 风险与缓解
- 规则收紧挡掉历史可建名：字符集/上限取各平台最小公约数（100 + ASCII），create 原口径本就近似，仅修中文放行漏洞；
- 前后端前缀约定改动文案回归：仅保留一层，补 zh/en 断言与 3563 回归翻转。
