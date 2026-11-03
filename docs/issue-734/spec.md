# Issue #734 需求文档：统一 git-repo contents/tree --ref 对不存在 ref/路径的 404 语义

## 背景

- 来源：grape-dev/grape-skills#820（issue-cli 处理 #783 时发现跨分支读取行为不一致）。
- `contents --ref` 侧：#549 已实现上游错误码透传，实测对不存在路径/分支统一 404，行为已归一。
- `tree --ref` 侧：GitCode 上游 `git/trees/{ref}` 对不存在 ref（不含 `/`）返回 **200 + 空树**，
  CLI 退出码 0；含 `/` 的不存在 ref 经解析降级后透传 404——两种「ref 不存在」行为不一致
  （同 #643 根因）。

## 目标

统一 `tree --ref`（含斜杠与不含斜杠）对不存在 ref 的语义为 **HTTP 404 + 统一错误包络**，
与 contents 的 #549 透传语义对齐；contents 侧行为已正确，仅加回归测试锁定。

## 验收标准

- [ ] `tree --ref <不存在 ref>`（含 `/` 与不含 `/`）返回 HTTP 404 + 统一包络，不再 200 空树
- [ ] `contents --ref` 对不存在/不可读路径（深/浅、目录、不存在分支）统一 404（回归锁定）
- [ ] 所有现有测试通过；新增测试覆盖上述场景
- [ ] 跨仓项（grape-cli 输出契约、grape-cli-reference 文档）不在本单验收内

## 范围外

- CLI `--json-out` 输出面（stderr/退出码表现，#783 场景）；
- grape-skills 资产（cli-reference 文档）跨仓协同项。
