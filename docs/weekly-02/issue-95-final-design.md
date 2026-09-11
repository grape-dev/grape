# Issue #95: 项目知识注入 Agent 逻辑优化——最终方案

## 0. 根本问题

Issue #67 建了 ref 表做"关联了才注入"，但只接了 2 个点（knowledge_files + _build_agent_knowledge_section），其余路径没接。加上迁移代码为 member 创建了违反设计的全局 ref。

三源设计框架是基准：
- system（baseline）：自动关联，全局注入
- agent（project/issue/issue_agent）：自动关联，按 scope 注入
- manual（member）：不自动关联，手动绑定才注入

原则：所有注入路径统一走 ref 表 + credential 过滤，member 不自动关联。过滤只做一次，content 和 URL 天然一致。三源各走各的函数，不交叉。

---

## 1. 当前注入流程（问题标注）

```
服务端 enqueue_task (service.py:360-496)
─────────────────────────────────────────

1. 打包 project_info_files（content 通道）
   load_project_info(repo_key)
     → 查 DB scope='baseline' / 读本地文件
     → {project-graph.md: 全文, project-capabilities.md: 全文}

   member_files_pi(repo_key)                    ← 问题：全量查，没走 ref 表
     → 查 DB scope='member' AND agent_type IS NULL
     → {member/user1/xxx.md: 全文}
     → 并入 project_info_files

   agent_files(repo_key, agent_type)            ← 问题：全量查，查的是用户上传不是 agent 生成
     → 查 DB scope='member' AND agent_type=?
     → {agent/coding/xxx.md: 全文}
     → 并入 project_info_files

2. 打包 knowledge_files（content 通道）
   agent_knowledge_files(repo_key, agent_type, issue_num, scene_id)
     → 查 ref 表 JOIN agent_shared_knowledge    ← 问题：缺 credential 过滤，没过滤 source
     → 返回 system + agent + manual 全部         ← 问题：三源交叉，和 member_files_pi 重复
     → {rel_path: 全文}

3. 生成 file_urls（URL 通道）
   generate_file_urls(repo_key, agent_type, issue_num)
     → 全量查 DB 4 类文件                         ← 问题：独立查 DB，和上面重复过滤
       a. project/issue/issue_agent → 按 scope 逐个查
       b. member (agent_type='') → 全量查
       c. member (agent_type非空) → 全量查
       d. baseline → 全量查
     → 给每个文件生成 OBS 预签名 URL
     → {rel_path: {url, storage_key}}

   project_info_file_urls = _file_urls           ← 问题：两个字段赋同一个值
   knowledge_file_urls = _file_urls

4. 存 task 到 DB

─────────────────────────────────────────

Worker 端执行 (agent.py:1060-1081)
─────────────────────────────────────────

5. 物化 project_info 到 .project-info/
   _materialize_files(proj_files, proj_file_urls, .project-info/)
     降级: ① URL 下载 → ② content 写入 → ③ storage_key 直连

6. 物化 knowledge 到 .knowledge/
   _materialize_files(know_files, know_file_urls, .knowledge/)

─────────────────────────────────────────

Prompt 构建 (prompt_builder.py)
─────────────────────────────────────────

7. build_devops_agent_prompt
   _build_project_info_section(repo_key, agent_type)
     → 内联 baseline 摘要（2500字截断）
     → 固定输出 "去 .project-info/member/ 读"      ← 问题：不管有没有关联都输出
     → 固定输出 "去 .project-info/agent/{type}/ 读" ← 问题：agent_files 废弃后不需要

8. _build_agent_knowledge_section（chat 路径 prompt 内联）
   → 查 ref 表不过滤 source                        ← 问题：和 knowledge_files 一样三源交叉
   → 返回 system + agent + manual 全部
   → 内联到 chat prompt
```

---

## 2. 改后注入流程图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          数据源（三源）                                  │
├──────────────────┬──────────────────────┬──────────────────────────────┤
│  system          │  manual              │  agent                       │
│  (baseline)      │  (用户上传)           │  (沉淀知识)                   │
│  codegraph生成    │  知识库页面手动创建    │  POST /events/{seq}/knowledge│
│  source='system' │  source='manual'     │  source='agent'              │
└────────┬─────────┴──────────┬───────────┴──────────────┬───────────────┘
         │                    │                          │
         ▼                    ▼                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              第一步：过滤文件（只做一次，三源不交叉）                       │
├──────────────────┬──────────────────────┬──────────────────────────────┤
│ load_project_info │ member_files_pi      │ knowledge_files              │
│ ()                │ ()                   │ ()                           │
│                   │                      │                              │
│ 查 DB             │ ref 表 JOIN          │ ref 表 JOIN                  │
│ scope='baseline'  │ source='manual'      │ source='agent'               │
│                   │                      │                              │
│ 不走 ref 表       │ + credential 过滤     │ + credential 过滤             │
│ (全局基线)        │ + key 归一化          │                              │
│                   │ (agent/→member/)     │                              │
└────────┬─────────┴──────────┬───────────┴──────────────┬───────────────┘
         │                    │                          │
         │     ┌──────────────┘                          │
         │     │  scene_knowledge_ref                    │
         │     │  三维匹配:                               │
         │     │  scene_id + agent_type + issue_num      │
         │     │  agent或场景被匹配就算关联                │
         │     └──────────────┐                          │
         │                    │                          │
         ▼                    ▼                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│         返回值: {rel: {content, obs_key}}                                │
├──────────────────┬──────────────────────┬──────────────────────────────┤
│ project-graph.md │ member/user/xxx.md   │ owner/repo/.../knowledge.md  │
│ project-caps.md  │                      │ (project/issue/issue_agent)  │
└────────┬─────────┴──────────┬───────────┴──────────────┬───────────────┘
         │                    │                          │
         └────────┬───────────┘                          │
                  │                                      │
                  ▼                                      │
┌──────────────────────────────────┐                     │
│ 第二步: generate_file_urls        │                     │
│ (基于已过滤结果，不查 DB)          │                     │
│                                  │                     │
│ 输入: 三批文件合并                 │                     │
│ {rel: {content, obs_key}}        │                     │
│                                  │                     │
│ obs_key 非空 → 生成预签名 URL     │                     │
│ obs_key 为空 → url 留空           │                     │
│                                  │                     │
│ 本地模式: storage=None            │                     │
│ → file_urls={}, 跳过此步          │                     │
└──────────────┬───────────────────┘                     │
               │                                         │
               ▼                                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              第三步：分配到 task 字段                                     │
├─────────────────────────────┬───────────────────────────────────────────┤
│ project_info_files          │ knowledge_files                           │
│ = baseline + member content │ = agent content                           │
│                             │                                           │
│ project_info_file_urls      │ knowledge_file_urls                       │
│ = baseline + member URL     │ = agent URL                               │
└─────────────────────────────┴───────────────────────────────────────────┘
                 │                                   │
                 ▼                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              存 task 到 DB（只存 content，不存 obs_key）                   │
└─────────────────────────────┬───────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
┌─────────────────────────────┐ ┌─────────────────────────────────────────┐
│ Worker: 物化到 .project-info/│ │ Worker: 物化到 .knowledge/               │
│                             │ │                                         │
│ _materialize_files(         │ │ _materialize_files(                     │
│   proj_files,               │ │   know_files,                           │
│   proj_file_urls,           │ │   know_file_urls,                       │
│   .project-info/)           │ │   .knowledge/)                          │
│                             │ │                                         │
│ 降级: ①URL下载 → ②content   │ │ 降级: 同上                              │
│      → ③storage_key直连     │ │                                         │
│                             │ │                                         │
│ replace {{PROJECT_INFO_DIR}}│ │ replace {{KNOWLEDGE_DIR}} (死占位符)     │
└─────────────────────────────┘ └─────────────────────────────────────────┘
              │                               │
              ▼                               ▼
┌─────────────────────────────┐ ┌─────────────────────────────────────────┐
│ Prompt: _build_project_     │ │ Prompt: 不调 _build_agent_knowledge_section   │
│ info_section()              │ │ devops 走 worker 物化路径                │
│                             │ │                                         │
│ 内联 baseline 摘要(2500字)   │ │ _build_agent_knowledge_section 只 chat 路径用  │
│ 有 manual 关联 → 输出引导    │ │ ref 表 source='agent'                    │
│ 没有关联 → 不输出            │ │                                         │
│ 去掉 agent/{type}/ 引导      │ │                                         │
└─────────────────────────────┘ └─────────────────────────────────────────┘
```

### 本地模式简化流程

```
本地模式（无 OBS）
─────────────────────────────────────
第一步: 同上（obs_key 全为空字符串）
第二步: 跳过（file_urls = {}）
第三步: project_info_file_urls = {}
        knowledge_file_urls = {}
第四步: 同上
Worker: 纯走 content 写入（无 URL 降级）
```

### 三源职责划分

```
┌────────────┬────────────┬─────────────────────┬──────────────────────┬───────────────────┐
│ 来源       │ source     │ 注入函数            │ 物化目录             │ task 字段         │
├────────────┼────────────┼─────────────────────┼──────────────────────┼───────────────────┤
│ baseline   │ system     │ load_project_info   │ .project-info/       │ project_info_files│
│ (系统知识) │            │                     │                      │                   │
├────────────┼────────────┼─────────────────────┼──────────────────────┼───────────────────┤
│ 用户上传   │ manual     │ member_files_pi     │ .project-info/member/│ project_info_files│
├────────────┼────────────┼─────────────────────┼──────────────────────┼───────────────────┤
│ agent沉淀  │ agent      │ agent_knowledge_    │ .knowledge/          │ knowledge_files   │
│            │ files()             │                      │ (DB字段不改)      │
└────────────┴────────────┴─────────────────────┴──────────────────────┴───────────────────┘

三源不交叉 → load_project_info 只查 system, member_files_pi 只查 manual, knowledge_files 只查 agent
过滤只做一次 → content 和 URL 天然一致 → generate_file_urls 不查 DB 只生成 URL
```

### scope=project 知识的注入路径

```
scope=project, source='agent'
  文件源: {repo_base}/project-info/knowledge.md
  注入:   agent_knowledge_files() → ref 表 source='agent' 匹配到
  物化:   .knowledge/owner/repo/project-info/knowledge.md
  task:   knowledge_files 字段

  注意: 文件源在 project-info/ 目录，但物化到 .knowledge/ 目录
  这是原始设计（e22dbce7），不在 #95 范围内
```

---

## 3. 改动清单（26 项，10 组）

### A. credential 安全过滤

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | src/api/knowledge.py:483-486 | agent_knowledge_files() ref 表查询结果加 _is_credential_file 过滤 | 循环体加 `if _is_credential_file(file_path): continue` |
| 2 | src/api/knowledge.py + configs/credential-filter.json | _is_credential_file 用 "repo-cr" 关键词 | 已完成（9a615927）。用 "repo-cr" 而非单独 "cr"——"repo-cr" 精确匹配 repo-cr.json，不会误匹配 crawler.md / cron.md / create.md 等正常文件。关键词从 configs/credential-filter.json 加载，加载失败用默认值 _DEFAULT_CREDENTIAL_SUBSTRINGS。只匹配文件名 basename，子串匹配 + 敏感扩展名。验证：repo-cr.json→拦截✓，repo-credentials.json→"credential"命中拦截✓，crawler.md→放行✓ |

### B. 注入路径统一接 ref 表（三源不交叉）

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 3 | src/api/knowledge.py:593-651 | member_files_pi() JOIN ref 表 | 签名加 agent_type/issue_num/scene_id。OBS 和本地统一走 ref 表。用 `k.source='manual'` 区分用户知识，ref 表三维匹配控制注入范围。返回值改为 `{rel: {content, obs_key}}`。返回 key 统一 member/ 前缀（老数据 agent/{type}/ 归一化为 member/） |
| 4 | src/api/knowledge.py:654-710 | agent_files() 废弃 | 当前查 scope='member' AND agent_type=? 是用户上传选了"Agent维度"的文档，不是 agent 生成的知识。ref 表统一后注入范围由绑定控制。调用方改调 member_files_pi。物化目录统一到 .project-info/member/ |
| 5 | src/api/knowledge.py:456-494 | knowledge_files() 改名为 agent_knowledge_files() + SQL 加 `AND k.source='agent'` | 当前不过滤 source，返回 system+agent+manual 全部，三源交叉。改名 agent_knowledge_files() 语义明确——只查 agent 沉淀知识。加 source='agent' 后和 member_files_pi(source='manual') / load_project_info(scope='baseline') 不交叉。DB task 字段名 knowledge_files 不改（保持兼容） |
| 6 | src/storage/obs.py:188-275 | generate_file_urls() 改为接收已过滤结果生成 URL | 不再独立查 DB。签名改为接收 `{rel: {obs_key}}`（来自 member_files_pi / knowledge_files / load_project_info 的返回值）。只做 URL 生成 |
| 7 | src/runtime/service.py:455-457 | knowledge_file_urls / project_info_file_urls 分离 | 当前赋同一个值。分离后 project_info_file_urls = baseline + member URL，knowledge_file_urls = agent URL |

**调用方改签名**（统一调 collect_knowledge_files，不再各自调 member_files_pi / agent_knowledge_files / load_project_info）：

| 调用方 | 文件:行 | 当前调用 | 改后 |
|--------|---------|---------|------|
| enqueue_task | service.py:413-457 | load_project_info + member_files_pi + agent_files + knowledge_files + generate_file_urls（各写一遍） | 调 collect_knowledge_files() → 取 content 存 task + 取 obs_key 生成 URL |
| direct 物化 | event_pipeline.py:1656-1745 | load_project_info + member_files_pi + agent_files + knowledge_files（各写一遍，没传 scene_id） | 调 collect_knowledge_files() → 取 content 写磁盘 |

collect_knowledge_files 内部调用：
- load_project_info_with_obs（新增）
- member_files_pi（改后签名，传 agent_type/issue_num/scene_id）
- agent_knowledge_files（改后签名，传 agent_type/issue_num/scene_id）
- get_issue_scene 获取 scene_id

### C. prompt 一致性

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 8 | src/prompt_builder.py:70-72, 89-91 | _build_project_info_section member 引导改条件输出 | 查 ref 表有 source='manual' 关联才输出引导文字。引导路径统一为 .project-info/member/（去掉 agent/{type}/ 引导） |
| 9 | src/prompt_builder.py:864-876 | _build_knowledge_section() 改名为 _build_agent_knowledge_section() + SQL 加 `AND k.source='agent'` | 当前 ref 表不过滤 source，chat 路径会把 baseline + member 知识也内联到 prompt。改名 _build_agent_knowledge_section() 语义明确——只内联 agent 沉淀知识。加 source='agent' 恢复 #67 之前的原始语义（#67 之前调 read_knowledge 只查 project/issue/issue_agent）。调用方 build_event_chat_prompt 同步改名 |
| 10 | src/im/vod_handler_async.py:199-243 | vod_handler_async baseline 注入统一到 _build_project_info_section | 删独立 baseline 查询。改为 `parts.append(_build_project_info_section(repo_key, ""))`，再追加原有 codegraph 提示 |
| 11 | — | build_devops_agent_prompt 不接入 _build_agent_knowledge_section | devops 走 worker 物化路径，knowledge_files 已走 ref 表过滤 |
| 19 | src/prompt_builder.py:549 | build_devops_agent_prompt 生成 {{KNOWLEDGE_DIR}} 引导文字 | 修复死占位符——prompt 里加"前序 Agent 沉淀的经验已落地：{{KNOWLEDGE_DIR}}/，存在则用 read tool 读取并遵循"。worker 物化 .knowledge/ 后 replace 为真实路径。agent 知道去读知识文件 |

### D. 迁移修复

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 12 | src/api/db_setup.py:1432-1441 | 删除 _migrate_knowledge_issue67() 两条 member ref 迁移 | 两条 SQL 为 scope='member' 创建了 ref，违反"member 不自动关联" |
| 13 | src/api/db_setup.py（新增） | 清理已生成的错误 member ref + 修复 rejected→accepted 老数据 | `DELETE FROM scene_knowledge_ref WHERE knowledge_id IN (SELECT id FROM agent_shared_knowledge WHERE scope='member')`，幂等。`UPDATE agent_shared_knowledge SET status='accepted' WHERE status='rejected' AND obs_key!='' AND content_hash!=''`，修复老代码 UPDATE 不改 status 导致的存量问题，幂等 |
| 14 | src/api/routes/knowledge.py:917-969 | add_knowledge_ref 单条 API 加空关联校验 | batch API 已有（continue 跳过），单条 API 没有。加 scene_id='' + agent_type='' → 返回 400 |

### E. fallback 处理

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 15 | src/api/knowledge.py:488-493 + src/prompt_builder.py:886-896 | agent_knowledge_files / _build_agent_knowledge_section fallback 保持只查 agent 知识 | ~~原方案说"补 member scope"是遗留错误~~。agent_knowledge_files 改后 source='agent'，fallback 调 read_knowledge 只查 project/issue/issue_agent（都是 agent 知识），member 不应该出现在 fallback 里。fallback 保持现状即可，不需要补 member。 |

### F. key 归一化

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| — | src/api/knowledge.py | member_files_pi 返回 key 统一 member/ 前缀 | 老数据 file_path 含 `project-info/agent/{type}/`，返回 key 时归一化为 `member/{type}/`。read_entry_content 用 DB 原始 file_path 读内容，不受影响 |

> 注：key 归一化是 #3 member_files_pi 改造的一部分，不单独编号。

### G. 废弃函数清理 + builder.py baseline 修复

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| — | src/api/knowledge.py:535 | member_files()（不带_pi）废弃 | 已无调用方 |
| — | src/api/knowledge.py:654-710 | agent_files() 废弃（#4 已覆盖） | 已无调用方 |
| 16 | src/project_info/builder.py:117-180 | _write_to_obs 修复 baseline 记录 | 补 source='system' / name（项目结构图谱/项目能力清单/项目元数据）/ description / created_by='system' / OBS key 格式统一为 knowledge/{owner}/{repo}/project-info/{name} / 自动创建 ref。INSERT OR REPLACE 改为先查再 INSERT/UPDATE（避免删旧 id 导致 ref 失效） |

### H. DB 写入完整性修复（深度检查发现）

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 17 | src/api/knowledge.py:348 (write_knowledge UPDATE) | UPDATE 补 source + status | 修复 rejected→accepted 不更新 status 的 bug（status 直接更新）；source 用 COALESCE(NULLIF(source, ''), ?) 补空值不覆盖已有值 |
| 18 | src/api/knowledge.py:672+770 (sync_loop + _sync_baseline_files) | OBS key 格式统一 + UPDATE 补 source/name | sync_loop baseline OBS key 从 `project-info/{repo_key}/{fname}` 改为 `knowledge/{owner}/{repo}/project-info/{fname}`；sync_loop UPDATE 补 source（COALESCE 按 scope 推导默认值）；_sync_baseline_files OBS key 同步改；_sync_baseline_files UPDATE 补 source + name（COALESCE 空值不覆盖） |

### I. 审查修复（code review 发现）

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 20 | src/api/db_setup.py:1449 | migration DELETE 加 created_by='system' 过滤 | [严重] 原 DELETE 无条件删所有 member ref，每次重启删用户手动创建的绑定。改为只删 migration 自动创建的（created_by='system'），用户手动创建的（created_by=username）不删 |
| 21 | src/im/vod_handler_async.py:237 | VOD prompt 加 {{KNOWLEDGE_DIR}} 引导 | VOD 走 enqueue_task，worker 物化 .knowledge/ 目录，但 prompt 没引导 agent 去读。和 build_devops_agent_prompt 保持一致 |
| 22 | src/api/knowledge.py:509 | _is_credential_file 加精确匹配列表 | 新增 _DEFAULT_CREDENTIAL_EXACT_NAMES = {cr.json, cr.yaml, cr.yml, cr.txt, cr.env}。解决 "cr" 子串误伤 cr-notes.md vs cr.json 漏过的矛盾。子串匹配保留 "repo-cr" |
| 23 | src/runtime/service.py:549 | collect_knowledge_files 异常时设 _collected 兜底值 | 避免 _collected is None 时 URL 生成阶段重复调用 |
| 24 | src/runtime/service.py:269 | _PROJECT_INFO_FILES 加 .metadata.json | URL 分类正确归属 project_info_file_urls |
| 25 | src/api/routes/knowledge.py:422 | knowledge context API 传 issue_num + scene_id | _build_project_info_section 条件输出 member 引导正确 |

### J. 深度检查修复（direct 模式占位符残留）

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 26 | src/im/vod_handler_async.py:329 | VOD direct 模式手动替换占位符 | [严重-#95 新引入] VOD direct 走 consume_stream_async 不物化文件。#95 改动让 _build_vod_prompt 调 _build_project_info_section → 生成 {{PROJECT_INFO_DIR}} + {{KNOWLEDGE_DIR}}。direct 模式不替换 → 残留。修复：consume_stream_async 前手动 replace 为 ws 路径 |

---

## 4. 不做

| 项 | 理由 |
|----|------|
| build_devops_agent_prompt 接入 _build_agent_knowledge_section | devops 走 worker 物化路径，prompt 内联冗余 |
| scope=project 物化路径调整 | 文件源在 project-info/ 但物化到 .knowledge/ 是原始设计(e22dbce7)，不在 #95 范围 |
| _member_docs file_path 解析 | DB file_path 带 owner/repo 前缀，解析走 else 分支用 created_by 而非路径里的 username。既有问题，不影响注入逻辑，不在 #95 范围 |
| chat direct 模式 {{PROJECT_INFO_DIR}} 残留 | handler_async.py / events.py 的 chat direct 模式不物化文件不替换占位符。既有问题，不是 #95 引入（#95 只给 devops/VOD 新增 {{KNOWLEDGE_DIR}}） |
| OBS 旧格式对象清理 | 旧 OBS key (project-info/{repo_key}/{name}) 能读到，不影响功能。新写入用新格式。清理是运维任务 |

---

## 5. member_files_pi 改 ref 表后的逻辑

走 ref 表查询，用 `k.source = 'manual'` 区分用户知识，ref 表三维匹配控制注入范围。

**核心原则**：以 ref 表为准——agent 或场景只要被匹配就算是关联。用 source 区分用户知识（manual）和系统/agent 知识，不用 scope/agent_type 硬筛。

**agent_files 废弃**：当前 agent_files 查 `scope='member' AND agent_type=?`，查的是用户上传选了"Agent维度"的文档（source='manual'），不是 agent 生成的知识。ref 表统一后，用户知识的注入范围由 ref 表绑定控制。agent_files 调用方改调 member_files_pi。

**返回值变更**：从 `{rel: content}` 改为 `{rel: {content, obs_key}}`。加 obs_key 供 generate_file_urls 生成 URL 用，不再让 generate_file_urls 独立查 DB。

**key 归一化**：老数据 file_path 含 `project-info/agent/{type}/`，返回 key 时归一化为 `member/{type}/`。新数据原样 `member/{user}/`。read_entry_content 用 DB 原始 file_path 读内容，不受影响。

```python
def member_files_pi(repo_key, agent_type="", issue_num=0, scene_id="") -> dict:
    """查 ref 表返回关联的用户知识（source='manual'），key 相对 project-info 目录。

    member 知识不自动关联——用户手动绑定到场景或 agent 才注入。
    ref 表里绑定到场景（scene_id 非空）或绑定到 agent（agent_type 非空）都算匹配。
    返回 {rel: {content, obs_key}}，obs_key 供 generate_file_urls 生成 URL。
    返回 key 统一 member/ 前缀（老数据 agent/{type}/ 归一化为 member/{type}/）。
    """
    from src.session_manager import db, db_lock
    from src.api.knowledge import read_entry_content, _is_credential_file
    files = {}
    try:
        with db_lock:
            rows = db.cursor().execute(
                "SELECT DISTINCT k.id, k.obs_key, k.file_path, k.content_hash "
                "FROM scene_knowledge_ref r "
                "JOIN agent_shared_knowledge k ON r.knowledge_id = k.id "
                "WHERE r.enabled = 1 "
                "AND (r.scene_id = ? OR r.scene_id = '') "
                "AND (r.agent_type = ? OR r.agent_type = '') "
                "AND (r.issue_num = ? OR r.issue_num = 0) "
                "AND k.repo_key = ? "
                "AND k.source = 'manual' "
                "AND k.status = 'accepted' "
                "AND k.archived = 0",
                (scene_id, agent_type, issue_num, repo_key)
            ).fetchall()
        for kid, obs_key, file_path, content_hash in rows:
            if _is_credential_file(file_path):
                continue
            ext = os.path.splitext(file_path)[1].lower()
            if ext not in (".md", ".txt", ".json"):
                continue
            content = read_entry_content(repo_key, obs_key or "", file_path or "", content_hash or "")
            if content:
                # key 归一化：去掉 project-info/ 前缀，agent/ 替换为 member/
                rel = file_path.replace("project-info/", "", 1).replace("\\", "/")
                if rel.startswith("agent/"):
                    rel = "member/" + rel[len("agent/"):]
                files[rel] = {"content": content, "obs_key": obs_key or ""}
    except Exception:
        pass  # ref 表异常时 member 不注入（符合"没关联不注入"设计）
    return files
```

**SQL 说明**：
- `k.source = 'manual'` → 只拿用户知识（不拿 system/agent 知识）
- ref 表三维匹配 → agent 或场景被匹配就算关联
- 不再用 `k.scope` / `k.agent_type` 硬筛——以 ref 表为准

---

## 6. agent_knowledge_files() source 过滤

当前 SQL 不过滤 source，返回 system + agent + manual 全部。改后加 `AND k.source = 'agent'`：

```sql
SELECT DISTINCT k.id, k.obs_key, k.file_path, k.content_hash
FROM scene_knowledge_ref r
JOIN agent_shared_knowledge k ON r.knowledge_id = k.id
WHERE r.enabled = 1
  AND (r.scene_id = ? OR r.scene_id = '')
  AND (r.agent_type = ? OR r.agent_type = '')
  AND (r.issue_num = ? OR r.issue_num = 0)
  AND k.repo_key = ?
  AND k.source = 'agent'              -- ← 新增：只拿 agent 沉淀知识
  AND k.status = 'accepted'
  AND k.archived = 0
```

**返回值变更**：从 `{rel: content}` 改为 `{rel: {content, obs_key}}`，加 credential 过滤。

---

## 7. _build_agent_knowledge_section() source 过滤

chat 路径的 prompt 内联，当前和 agent_knowledge_files() 一样不过滤 source。改后加 `AND k.source = 'agent'`，和 agent_knowledge_files() 保持一致：

```sql
SELECT DISTINCT k.id, k.scope, k.source, k.obs_key, k.file_path, k.content_hash
FROM scene_knowledge_ref r
JOIN agent_shared_knowledge k ON r.knowledge_id = k.id
WHERE r.enabled = 1
  AND (r.scene_id = ? OR r.scene_id = '')
  AND (r.agent_type = ? OR r.agent_type = '')
  AND (r.issue_num = ? OR r.issue_num = 0)
  AND k.repo_key = ?
  AND k.source = 'agent'              -- ← 新增
  AND k.status = 'accepted'
  AND k.archived = 0
```

chat 路径只内联 agent 沉淀知识，不内联 baseline（load_project_info 已处理）和 member（member_files_pi 已处理）。

---

## 8. generate_file_urls 改造后的逻辑

**核心变化**：不再独立查 DB，改为接收已过滤结果生成 URL。过滤只做一次，content 和 URL 天然一致。

**公共过滤函数**：抽取 `collect_knowledge_files()` 统一过滤逻辑，放在 `src/runtime/service.py`（和 load_project_info / _issue_num 同文件）。enqueue_task 和 direct 物化都调它，不再各写一遍。

```python
def collect_knowledge_files(repo_key, agent_type, issue_id, scene_id="") -> dict:
    """统一过滤知识文件，返回 {rel: {content, obs_key}}。

    三源不交叉：
    - load_project_info_with_obs → baseline (source='system')
    - member_files_pi → member (source='manual')
    - agent_knowledge_files → agent (source='agent')

    enqueue_task 和 direct 物化都调此函数，过滤逻辑只写一遍。
    """
    from src.api.session_store import get_issue_scene
    from src.runtime.service import load_project_info_with_obs
    from src.api.knowledge import member_files_pi, agent_knowledge_files
    from src.runtime.service import _issue_num

    _scene_id = scene_id
    if not _scene_id:
        try:
            _scene_id, _ = get_issue_scene(repo_key, issue_id)
        except Exception:
            _scene_id = ""
    _inum = _issue_num(issue_id)

    # 1. baseline
    _pif, _pim = load_project_info_with_obs(repo_key)

    # 2. member（并入 baseline）
    try:
        for rel, meta in member_files_pi(repo_key, agent_type, _inum, _scene_id or "").items():
            _pif.setdefault(rel, meta)
    except Exception:
        pass

    # 3. agent 知识（独立）
    _know = {}
    try:
        _know = agent_knowledge_files(repo_key, agent_type=agent_type, issue_num=_inum, scene_id=_scene_id or "")
    except Exception:
        pass

    return {"project_info": _pif, "meta": _pim, "knowledge": _know}
```

**generate_file_urls 签名变更**：
```python
# 改后：接收已过滤结果，只生成 URL
def generate_file_urls(self, filtered_files: dict) -> dict:
    """接收已过滤的文件列表，生成 OBS 预签名 URL。

    filtered_files: {rel: {content, obs_key}}
    返回: {rel: {url, storage_key}}
    """
    out = {}
    for rel, meta in filtered_files.items():
        obs_key = meta.get("obs_key", "")
        if obs_key:
            out[rel] = {"url": self.temporary_url(obs_key, expires=3600), "storage_key": obs_key}
        else:
            out[rel] = {"url": "", "storage_key": ""}
    return out
```

**enqueue_task 调用方改造**（service.py:413-457）：
```python
# 改后
_collected = collect_knowledge_files(repo_key, agent_type, issue_id)

# 分配到 task 字段（只存 content，不存 obs_key）
project_info_files = {rel: v["content"] for rel, v in _collected["project_info"].items()}
project_info_meta = _collected["meta"]
knowledge_files_field = {rel: v["content"] for rel, v in _collected["knowledge"].items()}

# 生成 URL（基于已过滤结果，不查 DB）
if _storage:
    _all_filtered = {}
    _all_filtered.update(_collected["project_info"])
    _all_filtered.update(_collected["knowledge"])
    _file_urls = _storage.generate_file_urls(_all_filtered)
else:
    _file_urls = {}

project_info_file_urls = {k: v for k, v in _file_urls.items()
                          if k.startswith("member/") or k in ("project-graph.md", "project-capabilities.md")}
knowledge_file_urls = {k: v for k, v in _file_urls.items()
                       if not k.startswith("member/") and k not in ("project-graph.md", "project-capabilities.md")}
```

**direct 物化调用方改造**（event_pipeline.py:1656-1745）：
```python
# 改后
_collected = collect_knowledge_files(repo_key, agent_type, _issue)

# 直接写磁盘（不需要 URL，纯走 content）
proj_dir = os.path.join(ws, ".project-info")
for rel, meta in _collected["project_info"].items():
    _dest = os.path.join(proj_dir, rel)
    os.makedirs(os.path.dirname(_dest), exist_ok=True)
    with open(_dest, "w", encoding="utf-8") as f:
        f.write(meta["content"])

know_dir = os.path.join(ws, ".knowledge")
for rel, meta in _collected["knowledge"].items():
    _dest = os.path.join(know_dir, rel)
    os.makedirs(os.path.dirname(_dest), exist_ok=True)
    with open(_dest, "w", encoding="utf-8") as f:
        f.write(meta["content"])
```

**过滤逻辑统一**：enqueue_task 和 direct 物化的步骤 1-4（load_project_info + member_files_pi + agent_knowledge_files）完全一样，抽取到 `collect_knowledge_files()` 后只写一遍。后续处理不同——enqueue_task 存 task 字段 + 生成 URL 给 worker，direct 直接写磁盘。各自处理。

**本地模式**：storage 为 None → file_urls = {}，worker 纯走 content 降级。generate_file_urls 不调用。

**storage/__init__.py 同步改造**：LocalStorage 也有 generate_file_urls 定义（line 106），需要同步改为新签名 `generate_file_urls(self, filtered_files: dict)`。LocalStorage 没有 OBS，返回空 dict 即可。

---

## 9. 返回值变更方案

为配合 generate_file_urls 改造（接收已过滤结果生成 URL），需要拿到 obs_key。但不同调用方需求不同——有的需要 obs_key，有的只需要 content。不能影响不需要 obs_key 的调用方。

### 策略：load_project_info 不动，新增 load_project_info_with_obs

load_project_info 有 4 个调用方，2 个需要 obs_key，2 个不需要：

| 调用方 | 需要 obs_key？ | 调哪个 |
|--------|--------------|--------|
| service.py:414（enqueue_task） | 是（给 generate_file_urls） | load_project_info_with_obs |
| event_pipeline.py:1660（direct 物化） | 是（给 generate_file_urls） | load_project_info_with_obs |
| prompt_builder.py:64（_build_project_info_section） | 否（只内联 content） | load_project_info（不变） |
| repos.py:831（前端 API） | 否（返回给前端） | load_project_info（不变） |

```python
# 原来不变
def load_project_info(repo_key) -> tuple[dict, dict]:
    """返回 (files, meta)，files = {rel: content}"""
    ...

# 新增
def load_project_info_with_obs(repo_key) -> tuple[dict, dict]:
    """返回 (files, meta)，files = {rel: {content, obs_key}}
    供 generate_file_urls 用，不暴露给前端。"""
    # 和 load_project_info 同样逻辑，但 files 值改为 {content, obs_key}
    ...
```

prompt_builder.py 和 repos.py 完全不受影响，不用适配。

### member_files_pi / agent_knowledge_files：直接改返回值

这两个函数只有 2 个调用方（enqueue_task + direct 物化），都需要 obs_key，直接改返回值没有影响：

- member_files_pi：`{rel: content}` → `{rel: {content, obs_key}}`
- agent_knowledge_files：`{rel: content}` → `{rel: {content, obs_key}}`

### task 字段存储

service.py 存 task 时只存 content，不存 obs_key。从返回值取 `meta["content"]` 存到 task 字段，obs_key 传给 generate_file_urls。

agent.py 读 task 字段不受影响（task 字段存的还是 `{rel: content}`）。

---

## 10. _build_project_info_section 条件输出

当前固定输出 member/agent 引导文字。改为条件输出：

```python
def _build_project_info_section(repo_key: str, agent_type: str = "",
                                issue_num: int = 0, scene_id: str = "") -> str:
    ...
    has_member = _has_manual_ref(repo_key, agent_type, issue_num, scene_id)
    ...
    if has_member:
        lines.append(f"> 项目级成员知识：{{{{PROJECT_INFO_DIR}}}}/member/，存在则用 read tool 读取并遵循。")
    # 去掉 agent/{type}/ 引导（agent_files 废弃后不再物化到 agent/ 目录）
```

`_has_manual_ref` 查 ref 表是否有 source='manual' 的关联记录（同 member_files_pi 的 SQL，只查 COUNT > 0）。

**3 个调用点的 scene_id 处理**：
- line 508（build_devops_agent_prompt）— 有 issue_id，从 get_issue_scene 获取 scene_id 传入
- line 714（build_general_chat_prompt）— Web/飞书通用对话，没有 issue_id，scene_id="" 默认。只能匹配到没绑定场景的 member ref。绑定到具体场景的 member 知识匹配不到。**设计如此**——通用对话没有 issue 上下文，不需要场景级 member 知识
- line 826（build_task_chat_prompt）— 任务对话，没有 issue_id，scene_id="" 默认。同上

---

## 11. 注入路径统一性检查

| # | 路径 | 改前 | 改后 | 状态 |
|---|------|------|------|------|
| 1 | agent_knowledge_files() content | ref 表不过滤 source，缺 credential | source='agent' + credential | 统一 |
| 2 | member_files_pi() content | 全量查 DB | source='manual' + ref 表 + credential | 统一 |
| 3 | load_project_info() content | 全量查 baseline | 不变（全局基线） | 设计如此 |
| 4 | _build_agent_knowledge_section() chat prompt | ref 表不过滤 source | source='agent' | 统一 |
| 5 | _build_project_info_section() devops prompt | 固定引导 | 条件输出 + 去掉 agent 引导 + scene_id | 统一 |
| 6 | generate_file_urls() URL | 独立全量查 DB | 接收已过滤结果 | 统一 |
| 7 | project_info_file_urls | 和 knowledge_file_urls 共用 | 分离（baseline + member） | 统一 |
| 8 | knowledge_file_urls | 和 project_info_file_urls 共用 | 分离（agent） | 统一 |
| 9 | vod_handler_async baseline | 独立查 baseline | 调 _build_project_info_section | 统一 |
| 10 | direct 物化 (event_pipeline) | member_files_pi/agent_files 不传 scene_id | 传 scene_id，删 agent_files | 统一 |
| 11 | agent_files() | 全量查 DB | 废弃 | 废弃 |
| 12 | builder.py _write_to_obs INSERT | INSERT OR REPLACE（删旧 id） | 先查再 INSERT/UPDATE | 修复 |
| 13 | builder.py baseline 记录 | 缺 source/name/description + OBS key 格式不统一 | 补全 + 统一 + 自动 ref | 修复 |
| 14 | write_knowledge UPDATE | 不更新 source + status | 补 source（COALESCE）+ status | 修复 |
| 15 | sync_loop OBS key + UPDATE | OBS key 格式不统一 + UPDATE 不补 source | 统一 + 补 source | 修复 |
| 16 | _sync_baseline_files OBS key + UPDATE | OBS key 格式不统一 + UPDATE 不补 source/name | 统一 + 补 source/name | 修复 |
| 17 | migration DELETE member ref | 无条件删所有 member ref（含用户手动创建） | 只删 created_by='system' | 修复 |
| 18 | _is_credential_file | "repo-cr" 子串，cr.json 漏过 | + 精确匹配 _DEFAULT_CREDENTIAL_EXACT_NAMES | 修复 |
| 19 | VOD prompt | 无 {{KNOWLEDGE_DIR}} 引导 | 加引导（和 devops 一致） | 修复 |
| 20 | VOD direct 模式 | #95 引入 {{PROJECT_INFO_DIR}}/{{KNOWLEDGE_DIR}} 但 direct 不替换 | consume_stream_async 前手动 replace | 修复 |
| 21 | collect_knowledge_files | 异常时 _collected=None → URL 阶段重复调用 | 异常时设兜底值 | 修复 |
| 22 | _PROJECT_INFO_FILES | 不含 .metadata.json → URL 分类错误 | 加 .metadata.json | 修复 |
| 23 | knowledge context API | 不传 issue_num/scene_id → member 引导不输出 | 传参 | 修复 |

---

## 12. 验收标准

- [ ] agent_knowledge_files() SQL 含 `source='agent'`，不含 credential 文件
- [ ] agent_knowledge_files() 返回值含 obs_key
- [ ] member_files_pi() SQL 含 `source='manual'`，不含 credential 文件
- [ ] member_files_pi() 返回值含 obs_key
- [ ] _build_agent_knowledge_section() SQL 含 `source='agent'`
- [ ] agent_files() 废弃，调用方改调 member_files_pi
- [ ] member_files_pi 返回 key 统一 member/ 前缀（老数据 agent/ 归一化）
- [ ] generate_file_urls() 不独立查 DB，接收已过滤结果生成 URL
- [ ] generate_file_urls 返回 key 和 member_files_pi / agent_knowledge_files 一致
- [ ] knowledge_file_urls 和 project_info_file_urls 独立分离
- [ ] collect_knowledge_files() 公共函数统一过滤逻辑
- [ ] enqueue_task 和 direct 物化都调 collect_knowledge_files，过滤逻辑只写一遍
- [ ] load_project_info() 返回值不变，不影响 repos.py / prompt_builder.py
- [ ] 新增 load_project_info_with_obs() 返回值含 obs_key
- [ ] repos.py 不受影响，不需要适配
- [ ] task 字段只存 content，不存 obs_key
- [ ] _build_project_info_section member 引导条件输出
- [ ] _build_project_info_section 去掉 agent/{type}/ 引导
- [ ] _build_project_info_section 3 个调用点 scene_id 处理（devops 传，通用对话/task 对话默认空）
- [ ] vod_handler_async baseline 注入走 _build_project_info_section，保留 codegraph 提示
- [ ] _migrate_knowledge_issue67() 不为 member 创建 ref
- [ ] 已生成的错误 member ref 被清理
- [ ] rejected→accepted 老数据被修复（有文件内容的 rejected 改为 accepted）
- [ ] add_knowledge_ref 单条 API 拒绝空关联
- [ ] agent_knowledge_files / _build_agent_knowledge_section fallback 保持只查 agent 知识（不补 member）
- [ ] member_files()（不带_pi）废弃
- [ ] enqueue_task 内部 if knowledge_files is None 改为调 collect_knowledge_files
- [ ] test_knowledge_ref.py 更新 _build_knowledge_section → _build_agent_knowledge_section
- [ ] _is_credential_file 关键词包含 "repo-cr" + 精确匹配 cr.json/cr.yaml/cr.yml/cr.txt/cr.env
- [ ] storage/__init__.py LocalStorage generate_file_urls 同步改签名
- [ ] builder.py _write_to_obs: source='system' + name + description + created_by='system'
- [ ] builder.py _write_to_obs: OBS key 格式 knowledge/{owner}/{repo}/project-info/{name}
- [ ] builder.py _write_to_obs: 自动创建 ref（baseline 全局关联）
- [ ] builder.py _write_to_obs: 先查再 INSERT/UPDATE（不用 INSERT OR REPLACE，避免 ref 失效）
- [ ] write_knowledge UPDATE: 补 source（COALESCE 空值不覆盖）+ status（直接更新，修复 rejected→accepted bug）
- [ ] sync_loop: baseline OBS key 统一为 knowledge/{owner}/{repo}/project-info/{fname}
- [ ] sync_loop: UPDATE 补 source（COALESCE 按 scope 推导默认值）
- [ ] _sync_baseline_files: OBS key 同步统一
- [ ] _sync_baseline_files: UPDATE 补 source + name（COALESCE 空值不覆盖）
- [ ] 所有 INSERT agent_shared_knowledge 含 source/name/description（rejected 缺 name/description 可接受）
- [ ] 所有需要补 source 的 UPDATE 已用 COALESCE 补空值
- [ ] build_devops_agent_prompt 生成 {{KNOWLEDGE_DIR}} 引导文字（修复死占位符）
- [ ] VOD prompt 生成 {{KNOWLEDGE_DIR}} 引导
- [ ] migration DELETE 只删 created_by='system' 的 member ref（不删用户手动创建的）
- [ ] collect_knowledge_files 异常时设 _collected 兜底值（避免重复调用）
- [ ] _PROJECT_INFO_FILES 含 .metadata.json
- [ ] knowledge context API 传 issue_num + scene_id
- [ ] VOD direct 模式 consume_stream_async 前替换 {{PROJECT_INFO_DIR}} 和 {{KNOWLEDGE_DIR}}
- [ ] 所有 OBS key 格式统一为 knowledge/{owner}/{repo}/... 前缀
- [ ] 现有测试通过，新增测试覆盖上述逻辑

---

## 13. 改动文件

| 文件 | 改动项 |
|------|--------|
| src/api/knowledge.py | #1 credential 过滤 + #22 精确匹配列表；#3 member_files_pi 接 ref 表 + 返回值加 obs_key + key 归一化；#4 agent_files 废弃；#5 knowledge_files 改名 agent_knowledge_files + source='agent' + 返回值加 obs_key；#15 fallback 保持只查 agent；member_files（不带_pi）废弃；#17 write_knowledge UPDATE 补 source+status；#18 sync_loop+_sync_baseline_files OBS key 统一 + UPDATE 补 source/name |
| src/runtime/service.py | #7 URL 字段分离；新增 load_project_info_with_obs；新增 collect_knowledge_files 公共函数；enqueue_task 改调 collect_knowledge_files；#23 异常兜底值；#24 _PROJECT_INFO_FILES 加 .metadata.json |
| src/storage/obs.py | #6 generate_file_urls 改为接收已过滤结果生成 URL |
| src/storage/__init__.py | #6 LocalStorage generate_file_urls 同步改签名 |
| src/api/event_pipeline.py | direct 物化改调 collect_knowledge_files |
| src/prompt_builder.py | #8 _build_project_info_section 条件输出 + 去掉 agent 引导 + 3 调用点 scene_id 处理 + _has_manual_ref；#9 _build_agent_knowledge_section 改名 + source='agent'；#15 fallback 保持只查 agent；#19 生成 {{KNOWLEDGE_DIR}} 引导文字 |
| src/im/vod_handler_async.py | #10 baseline 注入统一 + 保留 codegraph 提示；#21 VOD prompt 加 {{KNOWLEDGE_DIR}} 引导；#26 VOD direct 模式手动替换占位符 |
| src/api/db_setup.py | #12 删除 member ref 迁移；#13 清理错误 ref（只删 created_by='system'）+ 修复 rejected→accepted 老数据 |
| src/api/routes/knowledge.py | #14 单条 API 空关联校验；knowledge_context 函数名更新 + #25 传 issue_num/scene_id |
| src/project_info/builder.py | #16 _write_to_obs 修复: source='system' + name + description + created_by + OBS key 格式 + 自动 ref + 先查再 INSERT/UPDATE |
| tests/ | test_knowledge_board + test_knowledge_ref 更新 |

> 注：repos.py 和 prompt_builder.py 里调 load_project_info 的地方不变，不受影响。
