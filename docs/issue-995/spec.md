# Issue #995 Spec — 统一 issues list 与 git-repo issues get 编号口径

## 目标
`GET /api/issues/{owner}/{repo}` 默认不再返回 VCS（GitCode/GitHub）侧已不存在的台账卡片；
输出显式给出 `vcs_exists` / `is_vcs_deleted` / `vcs_check`，新增 `include_deleted` 开关供审计；
文档明确 `id`（台账卡片 id）与 `issue_number`（VCS 编号）不可互用。

## 背景
列表只查本地 `issue` 台账，无 VCS 存在性校验；已删卡片仍参与查重，和 `git-repo issues get` 404 口径分裂。
实测 1416 张卡片中 `number=1`（card_id=9601）与 `number=2`（card_id=419）在 GitCode 侧 404。

## 范围
- 三种列表模式统一存在性过滤，分页 `total`/`has_more` 与过滤集合一致。
- 台账新增 `vcs_exists`、`vcs_checked_at` 幂等迁移 + 存量异步回填。
- 新增 provider 感知的批量 VCS 编号索引（TTL 缓存、失败 fail-open）。
- 契约/`--help`/API 文档补充新参数与字段语义。

## 非范围
- VCS `state`/`closed_at` 字段（#1123）、`issues detail` 404 兜底（#667）、vod-runbook 止血（grape-skills#2675）。

## 验收
- 默认列表不出现 VCS 404 卡片；`include_deleted=1` 可审计返回并标记。
- 分页 total/has_more 与过滤后集合一致。
- 校验失败 fail-open，`vcs_check=unavailable`。
- 跨 provider 不混用编号；回填幂等；全部测试通过。
