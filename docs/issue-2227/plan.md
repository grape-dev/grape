# Plan: Issue #2227 — grape CLI 漂移守卫

## 方案

1. **能力探测函数化（install-worker.sh）**
   安装 grape 后不信任 `grape --version`，改为执行
   `grape git-repo commits --help` 并匹配 `--sha`。命中 → 记录已验证版本；
   未命中 → 告警并打印实际版本；无二进制 → 告警。失败不中断安装。

2. **独立守卫 `scripts/check_grape_cli_drift.py`**
   - 解析 `GRAPE_CLI_BIN`（绝对路径直接判断文件存在；否则 `shutil.which`）。
   - 先取 `--version`（单行、失败降级 `unknown`），再跑
     `git-repo commits --help`。
   - 用正则 `(?<![\w-])--sha(?![\w-])` 精确匹配独立长选项（避免误吃 `--sha-extra`）。
   - 缺失/SKIP/探测失败三类输出统一 `[grape-cli-drift]` 前缀，退出码 0/0/1。

3. **门禁接入 `scripts/check-all.sh`**
   紧随 shared-versions 之后执行守卫；`|| exit 1` 与现有 check 家族一致，
   无 grape 环境守卫自身 SKIP（exit 0），不会误报。

4. **文档校准 `help/api/10-git-repos.md`**
   commits 段补充 `?sha=` 入参、版本要求、能力探测命令与三条降级核验命令。

5. **测试 `tests/test_issue_2227_grape_cli_drift.py`**
   临时假二进制覆盖 PASS / FAIL / SKIP / 探测异常 / 前缀误匹配，另加
   install-worker.sh、check-all.sh、文档的静态接线断言。

## 影响与风险

| 项 | 影响 |
|----|------|
| install-worker.sh | 新增只读探测（命令失败被 `|| true` 吞掉），不改变安装成功性 |
| check-all.sh | 新门禁：真实漂移会让契约检查整体失败（预期行为） |
| 文档 | 仅 commits 段增补，不影响其他命令契约 |

风险：旧版 grape 完全不识别 `git-repo` 子命令 → 探测返回非零，守卫判 FAIL（符合预期）。