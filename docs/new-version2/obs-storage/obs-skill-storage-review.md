# B1 技能库 OBS zip 化改造 —— 方案评审稿

> 文档编号：GRP-REVIEW-20260813-02
> 版本：v1.0（评审稿，待确认）
> 日期：2026-08-13
> 范围：B1 技能库（上传、详情、转换、提升、模板下载、任务下发联动）
> 关联：`obs-object-key-spec.md`（对象键规范）、`obs-storage-design.md`（总体设计）、
>       `obs-storage-requirements.md`（需求）

---

## 1. 方案总览（讨论收敛后的最终形态）

**OBS zip 单对象为正体（可版本化）→ 服务端只做 zip 字节搬运（无本地目录树，
仅 LRU zip 缓存）→ 任务快照内嵌 zip base64 → worker 解压物化 + 显式恢复权限。**

```
  OBS（正体，zip 单对象，版本化）
    skills/custom/{user_id}/{skill_name}/v{n}/{skill_name}.zip
    skills/repo/{owner}/{repo}/{skill_name}/v{n}/{skill_name}.zip
    skills/template/skill-template.zip
      │
  服务端（无技能目录树，只有 zip 缓存：key=技能+版本，LRU 容量上限）
    enqueue：缓存命中 → zip 字节 → base64 进快照（零下载零解压）
             缓存 miss  → OBS 下载一次入缓存 → base64 进快照
   详情：   缓存 zip → 内存解压提取 SKILL.md（+ 内存 LRU 提取缓存）
   转换/提升：OBS copyObject（版本跟随）
   删除：   OBS deleteObject + DB 记录
      │
  任务快照：新增 skill_zip 字段（zip base64，自包含）；旧 skill_files 字段
            保留双写过渡（老 worker 兼容）
      │
  worker 物化：base64 解码 → 写临时 zip → extractall 到 .skills/{skill}/
              → 手动恢复权限（external_attr>>16 & 0o777，防提权）
      │
  opencode：DEVOPS_SKILLS_DIR 加载（结构 = 官方规范，含 references/ scripts/）
```

关键设计决策（讨论中已对齐）：
1. OBS 存 **zip**（不是目录树）—— 单对象原子、可版本、可分享、权限随
   zip external_attr 保留、二进制无损。
2. **无本地目录树** —— 节点无技能目录状态，只有不可变 zip 缓存
   （key 带版本 → 永不失效、永不读旧数据），真正解耦。
3. 快照内嵌 **zip base64**（不是解压后文本 JSON）—— 服务端零解压，
   二进制/权限完整下沉到 worker 物化时处理。
4. worker 物化**显式恢复权限** —— Python 3.12+ zipfile 不再自动恢复
   Unix 权限位（安全考虑），物化时按 external_attr 手动 chmod（实测验证）。
5. **OBS_ENABLED=false 全链路行为与现状完全一致**（默认关闭，零影响）。

---

## 2. 影响点清单

### 2.1 存储形态（核心变化）
| 维度 | 现状 | 改造后 |
|------|------|--------|
| 技能正体 | 本地目录树 skills/custom|repo/ | OBS zip 单对象（版本化） |
| 服务端技能状态 | 有（目录树） | 无（仅 LRU zip 缓存，非状态） |
| 权限/二进制 | 上传保留，下发时静默丢失 | 全程保留，物化时显式恢复 |
| 多版本 | 无 | zip 版本化（v{n}，可回滚） |
| 分享 | 无能力 | 临时 URL 直下 zip（天然支持） |

### 2.2 任务下发协议（worker 影响）
| 维度 | 现状 | 改造后 |
|------|------|--------|
| 快照字段 | skill_files（{rel: 文本} JSON） | 新增 skill_zip（zip base64），skill_files 双写过渡 |
| worker 物化 | 逐文件写文本 | 解码→解压→恢复权限 |
| 老 worker | — | 兼容（双写期间读 skill_files） |
| opencode 消费 | DEVOPS_SKILLS_DIR | 不变 |

### 2.3 不变的（明确不受影响）
- 官方技能库（部署物，留本地）
- 模板源 skills/_template/（本地）
- DB skill 表主体结构（新增 current_version 字段，见改动 N9）
- worker 执行流程（opencode 运行、任务回报）
- OBS_ENABLED=false 时全部现有行为
- admin 的 DB 读写逻辑（共享 MySQL 现状）

### 2.4 新增运维面
- OBS 配置：endpoint / bucket / AK-SK（环境变量）/ prefix / URL 有效期
- 本地 zip 缓存容量管理（LRU，建议上限 2GB、每技能保留最近 N 版）
- 存量数据迁移脚本
- zip 炸弹防护（上传时校验解压后总大小，建议上限 100MB）

---

## 3. 改动点清单（逐文件）

### 主服务（Python）

| # | 文件 | 位置 | 改动 | 风险 |
|---|------|------|------|------|
| N1 | src/config.py | 新增段 | OBS_ENABLED/ENDPOINT/BUCKET/ACCESS_KEY/SECRET_KEY/PREFIX/URL_EXPIRES/LOCAL_CACHE 8 项（环境变量，AK/SK 不入文件） | 低 |
| N2 | src/storage/__init__.py | 新增 | StorageBackend 抽象 + get_storage() 单例（OBS 未启用→None）+ zip 缓存工具（LRU、按 技能+版本 key）+ save_dir_tree 等通用函数 | 低 |
| N3 | src/storage/obs.py | 新增 | ObsStorage：putObject/copyObject/deleteObject/listObjects/getObject/createTemporarySignature（esdk-obs-python，import obs） | 中（SDK 行为） |
| N4 | src/api/routes/skills.py | upload_skill | OBS 分支：临时解压校验（zip slip/炸弹/front matter）→ 规范化重打包（根={skill}/、external_attr 保权限、弃系统文件）→ 传 OBS v{n} → DB current_version+1 → 写/清 zip 缓存；local 分支不变 | 中 |
| N5 | skills.py | get_skill_detail | OBS 分支：缓存 zip → 内存解压提取 SKILL.md（+ 内存 LRU 提取缓存）；local 分支不变 | 低 |
| N6 | skills.py | convert_skill | OBS 分支：copyObject（个人→仓库，版本跟随）；local 分支不变 | 低 |
| N7 | skills.py | delete_skill | OBS 分支：deleteObject + 清缓存；local 分支不变 | 低 |
| N8 | skills.py | download_skill_template | OBS 分支：本地 _template 打包 → 传 OBS → 返回 {url, expires_in}（临时 URL）；local 分支返回原 FileResponse | 低 |
| N9 | src/user/db.py | skill 表/函数 | 加 current_version（INT 默认 1）；create_skill 记录版本；delete_skill 适配 OBS 删除；get_skill_path 适配 zip 缓存取用 | 中（DB 迁移） |
| N10 | src/runtime/service.py | enqueue_task | 快照新增 skill_zip（zip base64，经 zip 缓存获取）；skill_files 双写（老 worker 过渡）；_pack_skill_dir 改读 zip 缓存 | 中（协议） |
| N11 | src/runtime/agent.py | _execute_task 物化 | skill_zip 优先：base64 解码 → 写临时 zip → extractall → 恢复权限（0o777 剥离 setuid/sticky）；无 skill_zip 时走旧 skill_files | 中（worker 升级） |

### admin（Java）

| # | 文件 | 位置 | 改动 | 风险 |
|---|------|------|------|------|
| N12 | admin/backend/.../service/SkillService.java | uploadSkill | 解压校验 → 规范化重打包 → OBS 上传（esdk-obs-java） | 中 |
| N13 | SkillService.java | promoteToOfficial | copyRecursively+deleteRecursively → OBS copyObject（源/目标前缀均按版本） | 中 |
| N14 | SkillService.java | getSkillContent | 读本地 SKILL.md → OBS 下载 zip → 内存提取 SKILL.md（Java ZipInputStream） | 中 |
| N15 | admin pom.xml | 依赖 | 新增 esdk-obs-java（华为 maven 镜像） | 低 |

### 其他

| # | 文件 | 位置 | 改动 | 风险 |
|---|------|------|------|------|
| N16 | src/api/static/js/ 前端 | api.js/skills.js | 模板下载响应改 {url} 适配；技能上传表单不变 | 低 |
| N17 | scripts/migrate_skills_to_obs.py | 新增 | 存量 skills/custom、skills/repo 目录树 → 逐技能规范化打包 zip 上传（--dry-run / sha256 校验 / --delete-after） | 中 |
| N18 | requirements.txt | 新增 | esdk-obs-python（仅主服务；worker requirements-worker.txt 不引入） | 低 |
| N19 | tests/test_storage.py、test_skills_obs.py | 新增 | 见 §5 | 低 |

---

## 4. 兼容性与回退

1. **开关回退**：OBS_ENABLED=false（默认）→ get_storage() 返回 None → 全部走现状
   本地流程，行为零变化；配置有误/缺 AK-SK 等同未启用。
2. **OBS 故障**：上传类返回明确错误不产生半成品（DB 先行+失败回滚删对象）；
   详情/任务打包走 zip 缓存兜底（缓存命中仍可用），冷缓存未命中返回 503+告警。
3. **老 worker 兼容**：快照双写 skill_files（旧）+ skill_zip（新），老 worker 读旧
   字段继续工作；新 worker 优先 skill_zip。过渡期后移除双写（快照瘦身）。
4. **老数据**：迁移脚本把现有目录树打包上传（规范重打包），上传前本地仍是正体，
   上传后 OBS 为正体；--dry-run 预演 + sha256 校验。
5. **回滚版本**：版本化后 DB current_version 指回旧版本号即可回滚（zip 对象仍在）。

---

## 5. 测试与验证点

| 层 | 内容 |
|----|------|
| 单测 | StorageBackend 契约；zip 打包/解压往返（含权限位）；对象键白名单；缓存 LRU/版本隔离 |
| 路由单测 | skills.py OBS 分支（mock ObsStorage）：上传/详情/转换/删除/模板全流程；local 分支回归 |
| 链路单测 | enqueue 快照 skill_zip 生成；worker 物化（解码→解压→权限恢复）→ 脚本可执行断言 |
| 回归 | OBS_ENABLED=false 下 pytest tests/ 全量（基线 713 用例）零失败 |
| 端到端（可选，OBS_E2E=1） | 真实桶：上传→详情→任务→worker 物化→脚本执行 |
| admin | mvn -q compile 编译验证；技能提升 copyObject 单测（mock） |

---

## 6. 实施顺序

| 里程碑 | 内容 |
|--------|------|
| M0 | N1/N2/N3/N18：配置 + 存储抽象 + ObsStorage + 依赖 |
| M1 | N4~N8/N16：skills.py 五接口 OBS 化 + 前端适配 |
| M2 | N9/N10/N11：DB 版本字段 + 快照 skill_zip + worker 物化 |
| M3 | N12~N15：admin Java 直连 OBS（含编译验证） |
| M4 | N17：存量迁移脚本 |
| M5 | N19：全量测试 + 回归 + 提交特性分支（待用户确认后合入） |

---

## 7. 待评审确认点

| # | 决策点 | 推荐 |
|---|--------|------|
| 1 | 版本策略：key 带版本段 v{n} + DB current_version（支持回滚、缓存天然隔离） | ✅ 推荐 |
| 2 | 老 worker 过渡：双写 skill_files + skill_zip | ✅ 推荐 |
| 3 | admin Java 直连 OBS（esdk-obs-java，因主服务 API 为 OAuth 用户态无法复用） | ✅ 确认 |
| 4 | 模板下载本期改临时 URL 形态 | ✅ 确认（B1 范围含模板下载） |
| 5 | zip 炸弹防护：解压后总大小上限 100MB | ✅ 推荐 |
| 6 | zip 缓存 LRU：容量 2GB、每技能保留最近 3 版 | ✅ 推荐 |
| 7 | 本地缓存位置：data/cache/skills/（OBS_LOCAL_CACHE 可配） | ✅ 确认 |

---

## 8. 明确不做（本期范围外）

- worker 直连 OBS（模式 b）：URL 有效期、OBS 可达性、协议改造三硬伤，本期不做
- 官方技能上云、project-info/knowledge/event.json/安装包 OBS 化（B2~B6）
- 技能在线编辑、文件树浏览等新功能
