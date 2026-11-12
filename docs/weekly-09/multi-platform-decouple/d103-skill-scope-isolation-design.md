# D103 技能作用域与跨平台同名仓库隔离设计（2026-11-09）

## 一、背景

用户实弹提问驱动：①「上传技能是仓库通用的吧，没有和仓库耦合吧」；②「gitee 有仓库 example-user，github 也有同名仓库 example-user，仓库里上传技能有冲突吗」。核查结论：现行实现已满足（个人域仓库无关 + 仓库域 provider 段隔离 = Issue #444 根治），本文将口径落档并补场景用例与测试锁。

## 二、作用域模型（skills.py upload_skill）

| scope | 归属（owner） | 存储键 | 可见性 |
|---|---|---|---|
| user（个人） | `str(user_id)` | `skills/custom/<user_id>/<skill_name>/<name>.zip` | 仅本人，全仓库通用（与仓库零耦合） |
| repo（仓库） | 完整 repo_key（`gitee:acme/web`） | `skills/repo/<provider>/<owner>/<repo>/<skill_name>/<name>.zip` | 该仓库成员 |
| official | — | 只读 | 所有用户 |

- scope 必须显式二选一（缺省 400）；scope=repo 必须指定 repo（缺省 400）。
- 会话 token（tk_）上传仅允许 scope=repo，且仓库必须与 token 绑定仓库一致（防越权写）。

## 三、跨平台同名仓库隔离（#444 根治）

- repo_key 带平台前缀：`gitee:example-user` ≠ `github:example-user`——两个独立仓库标识。
- 存储对象键补 provider 段（S12/#444）：`skills/repo/{provider}/{owner}/{repo}/...`——跨平台同名仓库不再共享对象键。
- DB 归属行 owner_id = 完整 repo_key，行级隔离。
- **裸名歧义硬拒**（`_resolve_repo_key(strict=True)`，仅写路径）：裸 `acme/web` 同时命中 `gitcode:` 与 `github:` 两行时显式 400 报错（「请指定完整仓库标识」），消灭"静默 LIMIT 1 不确定归属"。
- 存量旧键**懒迁移**：旧行 file_path 读侧直读兼容；同键覆盖上传时自然迁新键（零删除）。

## 四、覆盖语义（设计行为，非异常）

同键（同 scope + 同 owner + 同 skill_name）重复上传 = putObject 原子覆盖，最后写者胜；user 域不同 owner 段天然隔离；repo 域同仓库同名覆盖不限角色。#444 收尾（P-B）：save 前留存旧对象字节，upsert 失败可回滚。

## 五、UC 与测试

- UC45 个人技能仓库无关（user 域键不含仓库维度）
- UC46 仓库技能跨平台隔离（provider 段，gitee:example-user ≠ github:example-user）
- UC47 裸仓库名歧义硬拒 + 覆盖语义（同键最后写者胜）
- 测试：tests/test_uc45_skill_scope_isolation.py（键函数族 + repo_key 幂等 + 白名单校验）

## 六、边界与不做的

- 不做跨仓库技能共享/引用（官方 scope 即全局共享通道）。
- 不做存量旧键主动迁移（懒迁移已覆盖，零删除原则）。
- Hermes 本地技能目录（~/.hermes/skills/）与平台 skill 表为两个独立空间，同名互不影响（口径说明，非功能）。
