# Grape OBS 对象键（路径）规范

> 文档编号：GRP-SPEC-20260813-01
> 版本：v1.0
> 状态：待评审确认
> 日期：2026-08-13
> 关联：`obs-storage-design.md`（方案设计）、`obs-storage-requirements.md`（需求）

---

## 1. 目的与范围

定义 Grape 写入华为云 OBS 的**对象键（Object Key）路径规范**，作为主服务
（Python）、管理端（Java）、迁移脚本、运维排查的共同约定。所有读写 OBS 的
代码必须以本规范为准拼装 key，禁止业务侧自由造路径。

范围：技能库（B1，本期实施）+ 其余业务域（B2~B6，预留规范，后续按此扩展）。

---

## 2. 总原则

1. **前缀隔离**：所有对象键以环境前缀开头，多环境可共桶共存。
2. **与本地路径同构**：OBS key 与本地磁盘相对路径一一对应，迁移/缓存/还原
   零映射逻辑。
3. **目录即前缀**：技能等目录型数据以"目录前缀"为操作单位
   （上传、枚举、删除、物化全部按前缀批量处理）。
4. **静态段固定、动态段白名单**：`skills/custom/repo/official/template` 等
   静态段固定小写 ASCII；`user_id / repo_key / skill_name` 等动态段保持业务
   原值，入库前过白名单正则。
5. **区分大小写**：OBS key 区分大小写；动态段保持业务原值，不做大小写转换
   （避免与本地目录、DB 记录脱钩）。
6. **单文件临时 URL**：临时签名 URL 按**对象级**签发（单个文件下载）；
   技能目录整体获取走"任务打包物化"通道，不走 URL 枚举。

---

## 3. 全局结构

```
对象键 = {OBS_PREFIX}/{业务域}/{域内路径}
```

### 3.1 环境前缀 OBS_PREFIX

| 取值 | 场景 |
|------|------|
| `grape/prod` | 生产环境（默认） |
| `grape/test` | 测试环境 |
| 其他 | 由运维按环境配置（OBS_PREFIX 环境变量） |

- 默认 `grape/prod`；测试/生产可共用一桶（前缀隔离），也可一环境一桶
  （OBS_BUCKET 区分），二选一即可，本规范两种都兼容。
- 前缀以 `环境/环境名` 两段表达，段间斜杠分隔，不以下划线替代。

### 3.2 业务域一览

| 业务域 | 前缀段 | 本期 |
|--------|--------|------|
| 技能库 | `skills/` | ✅ B1 |
| worker 安装包 | `worker-package/` | ⏳ B2 预留 |
| project-info | `project-info/` | ⏳ B3 预留 |
| 共享知识 | `knowledge/` | ⏳ B4 预留 |
| 事件档案 | `event-archive/` | ⏳ B5 预留 |

---

## 4. 技能库对象键规范（B1，本期实施）

### 4.1 个人技能（scope=user）

```
{PREFIX}/skills/custom/{user_id}/{skill_name}/{相对路径}
```

| 段 | 规则 | 示例 |
|----|------|------|
| user_id | 数字 uid，正则 `^\d+$` | `123` |
| skill_name | `^[\w.\-]+$` | `hello-skill` |
| 相对路径 | 技能目录内相对路径（SKILL.md / references/** / scripts/**） | `references/design.md` |

完整示例：

```
grape/prod/skills/custom/123/hello-skill/SKILL.md
grape/prod/skills/custom/123/hello-skill/references/design.md
grape/prod/skills/custom/123/hello-skill/scripts/greet.py
```

### 4.2 仓库技能（scope=repo）

```
{PREFIX}/skills/repo/{owner}/{repo}/{skill_name}/{相对路径}
```

| 段 | 规则 | 示例 |
|----|------|------|
| owner | `^[\w.\-]+$` | `Jason7602` |
| repo | `^[\w.\-]+$` | `grape` |
| skill_name | `^[\w.\-]+$` | `my-skill` |

> **repo_key 含斜杠的处理**：repo_key = `owner/repo`，直接展开为两级目录
> （不转义、不替换）。理由：与本地 `skills/repo/{repo_key}/` 布局完全同构，
> 迁移/缓存零映射；`listObjects(prefix=.../skills/repo/Jason7602/grape/)`
> 前缀语义天然清晰。

完整示例：

```
grape/prod/skills/repo/Jason7602/grape/my-skill/SKILL.md
grape/prod/skills/repo/Jason7602/grape/my-skill/references/guide.md
```

### 4.3 官方技能（scope=official，预留）

```
{PREFIX}/skills/official/{skill_name}/{相对路径}
```

- 本期官方技能仍随代码发布、留本地目录（部署物），**不写 OBS**；
- key 规范占位，未来若官方技能上云，迁移无需改规范。

### 4.4 技能模板（一次性产物）

```
{PREFIX}/skills/template/skill-template.zip
```

- 固定 key；每次生成覆盖写（模板源 `skills/_template/` 仍在本地）。
- 下载走临时签名 URL；对象可保留（幂等覆盖）或按 TTL 清理（运维侧）。

### 4.5 技能操作语义（以"技能前缀"为单位）

| 操作 | 对象键操作 |
|------|-----------|
| 上传技能 | 递归 `putObject`，key 前缀 `skills/{scope}/{owner}/{skill_name}/` |
| 读取详情 | 单对象 `getObject`（SKILL.md），本地缓存优先 |
| 转换（个人→仓库） | `listObjects(源前缀)` + 逐对象复制到目标前缀 |
| 删除技能 | `listObjects(技能前缀)` + 批量 `deleteObjects`（前缀删除） |
| 任务打包物化 | `listObjects(技能前缀)` + 拉取到本地缓存目录 |
| 模板下载 | 单对象 `getObject`（固定 key） |

---

## 5. 其余业务域预留规范

```
{PREFIX}/worker-package/grape-worker.tar.gz        # B2：安装包（+ VERSION / SHA256SUMS）
{PREFIX}/project-info/{owner}/{repo}/project-graph.md          # B3
{PREFIX}/project-info/{owner}/{repo}/project-capabilities.md
{PREFIX}/project-info/{owner}/{repo}/.metadata.json
{PREFIX}/knowledge/{owner}/{repo}/{scope}/knowledge.md         # B4（scope=project/issue/issue_agent）
{PREFIX}/event-archive/{owner}/{repo}/issues/{n}/event.json    # B5
```

- `owner/repo` 同上展开两级；`scope` 小写固定枚举；`n` 为 issue 数字。
- 各域操作单位：B2 单对象；B3/B4/B5 按 `{owner}/{repo}` 前缀
  批量（生成、缓存、清理）。

---

## 6. 安全与边界约束

### 6.1 动态片段白名单（拼 key 前必过）

| 段 | 正则 | 说明 |
|----|------|------|
| user_id | `^\d+$` | 数字 uid |
| owner / repo | `^[\w.\-]+$` | 各段独立校验，防路径穿越 |
| skill_name | `^[\w.\-]+$` | 同本地目录名校验 |
| scope | `^(custom\|repo\|official\|template)$` | 固定枚举 |
| issue_num | `^\d+$` | 数字 |

> 禁止动态段包含 `/`、`..`、空白、控制字符。repo_key 仅在**拆分为
> owner/repo 两段各自校验后**才允许展开为两级目录。

### 6.2 字符与长度

- 静态段：小写字母 + 连字符；动态段：`[\w.\-]`（ASCII）。
- 全 key 建议 ≤ 512 字符（OBS 上限 1024 字节），技能目录嵌套
  （references/**）通常远低于此，无需额外限制。
- 禁止：空格（需编码，易错）、中文（URL 兼容性）、`+`、`%`、`#`、`?`。

### 6.3 覆盖与版本

- 同 key 覆盖写即新版本（OBS 默认无版本控制）；技能同名冲突已由 DB
  唯一约束（skill_name + scope + owner_id）在业务层拦截，不依赖 OBS。
- 上传采用"先 DB 记录、后 OBS 写入，失败回滚删除前缀"的事务顺序，
  避免半成品对象残留。

### 6.4 临时 URL 边界

- 仅对**单个对象**签发 GET 签名 URL；不签发目录级/前缀级 URL。
- 有效期：技能文件默认 300s（OBS_URL_EXPIRES 可配）。
- URL 不落日志（记录 key + expires 即可）。

---

## 7. 本地 ↔ OBS 映射速查

| 本地路径 | OBS 对象键（prod 示例） |
|----------|------------------------|
| skills/custom/123/hello/SKILL.md | grape/prod/skills/custom/123/hello/SKILL.md |
| skills/repo/Jason7602/grape/my-skill/SKILL.md | grape/prod/skills/repo/Jason7602/grape/my-skill/SKILL.md |
| skills/_template/（源，本地） | （打包产物）grape/prod/skills/template/skill-template.zip |
| data/cache/worker-package/grape-worker.tar.gz | grape/prod/worker-package/grape-worker.tar.gz |
| {WORKSPACE_DIR}/o/r/project-info/project-graph.md | grape/prod/project-info/o/r/project-graph.md |
| {WORKSPACE_DIR}/o/r/project-info/knowledge.md | grape/prod/knowledge/o/r/project/knowledge.md |
| {WORKSPACE_DIR}/o/r/issues/5/event.json | grape/prod/event-archive/o/r/issues/5/event.json |

> 映射规则一句话：**本地相对路径前面加 `{OBS_PREFIX}/{业务域}/`**，
> 唯一例外是 project-info / knowledge / event-archive 三个域的
> `owner/repo` 段提前（与本地 `WORKSPACE_DIR/{owner}/{repo}/` 对应）。

---

## 8. 待确认项

1. 环境前缀 `grape/prod`、`grape/test`（前缀隔离）——是否确认？
2. repo_key 保持两级目录展开（不转义）——是否确认？
3. 官方技能 key `skills/official/{name}/` 占位预留——是否确认？
4. 静态段全部小写、动态段保持业务原值——是否确认？
