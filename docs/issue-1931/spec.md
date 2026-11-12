# Issue #1931（新架构适配版）— 契约生成物不随发布包下发 + 契约不被单进程缩小

## 背景
四进程拆分后，`/static/openapi.json` 由各进程启动时落盘。arch-weekly-11 已用
`src/api/openapi_merge.py:dump_contract()` 在 mgmt 进程合并四份 spec 写盘（单一入口，
合并失败降级为自身 spec）。因此原 issue 的 B 项「独立的 merge 脚本」在 arch 上被
`openapi_merge` 取代，**不再随本分支提交**。

剩下两项在新架构上仍然成立且有价值：

## A 项：发布包不得携带契约生成物
`src/api/static/openapi.json` 与 `help/api/openapi.json` 都是启动期生成物（.gitignore），
但 `release_pack.py` 按目录遍历打包，gitignore 不生效——`_tar_filter()` 此前只过滤了
`help/api/openapi.json`，`src/api/static/openapi.json`（随 `src/` 整目录打包）会漏进发行包，
把构建机上的旧契约带给用户。修法：`_tar_filter()` 同时过滤两个路径。

## C 项（适配版）：契约不得被「子集 schema」缩小
`dump_contract()` 合并失败时会降级为当前进程 spec（265 条）并写盘，这会静默覆盖磁盘上
更全的契约（269 条）——CLI 随之丢命令组。原 issue 用「必要端点清单 + 拒绝写盘」实现哨兵；
新架构不需要固定清单，直接按磁盘对照：

> 目标文件已存在、且其 `paths` 里存在本次 schema 缺失的路径 ⇒ 拒绝写盘并打 WARNING
> （`allow_partial=True` 显式放行）；磁盘契约损坏（非 JSON）视为无对照物，正常写入。

判定只看「会不会缩小」，与进程数量、路由归属无关，因此对 4 进程/将来 N 进程都成立。

## 验收
1. `release_pack._tar_filter` 对 `src/api/static/openapi.json`、`help/api/openapi.json` 均返回 None
2. 打包出的 tar 里不含任何 `openapi.json`
3. `write_openapi_contract`：子集 schema 被拒写（返回 0，磁盘不变 + WARNING）；超集正常写；
   `allow_partial=True` 放行；磁盘损坏时覆盖
4. 既有 `tests/api/test_check_openapi_endpoints.py` 仅保留 1 条 arch 基线既有失败（与本改动无关）