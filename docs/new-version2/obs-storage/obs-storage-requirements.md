# Grape 文件存储 OBS 化改造 —— 需求文档

> 文档编号：GRP-REQ-20260813-01
> 版本：v1.0
> 状态：已评审确认
> 日期：2026-08-13
> 关联：方案设计 `obs-storage-design.md`；评审稿 `obs-storage-reform-design.md`（历史）

---

## 1. 背景与问题陈述

Grape 是 Multi-Agent DevOps 工作流平台，由主服务（Python/FastAPI）、管理端
（Java/Spring admin）、Worker 边端（Python RuntimeAgent）组成。当前**所有业务文件
均落本地磁盘**，四个根：`skills/`（技能库）、`WORKSPACE_DIR`（git mirror/worktree、
project-info、knowledge、event 档案、会话目录）、`data/cache/`（worker 安装包）、
`configs/`（配置）。

在**服务多集群部署**（多主服务实例/多节点）与**分布式客户端使用**（多 Worker、
admin 控制台、WebUI 分散访问）的诉求下，本地磁盘模型产生三类问题：

1. **共享缺失**：A 节点上传的技能/产物，B 节点不可见；admin 已出现
   "技能文件不在当前服务器" 的降级分支（SkillService.java:217/240）。
2. **分发成本**：worker 安装包需各节点自行构建或经服务端流式下载，
   服务端带宽成为瓶颈，且版本难以保证全集群一致。
3. **清理不彻底**：解除托管仅删除本机目录，其他节点残留数据。

## 2. 目标

1. **统一存储抽象**：所有业务文件落盘点收口到 StorageBackend，支持
   `local` / `obs` 双实现，配置切换，单机部署行为不变。
2. **上传 OBS 化**：用户产生/运行期产生的文件（技能、产物、档案）写入华为云
   OBS，多集群任意节点读写同一份数据。
3. **下载临时 URL 化**：下载不再经服务端流式代理，由服务端签发 **OBS 临时签名
   URL**（预签名 GET），客户端/Worker 直连 OBS；有效期可配、权限可控。
4. **分布式客户端一致**：Worker（安装包、技能、project-info）、admin（技能维护）、
   WebUI（模板/导出下载）三类客户端拿到一致、可用的文件视图。

## 3. 范围

### 3.1 纳入范围（In Scope）

| 编号 | 场景 | 说明 |
|------|------|------|
| B1 | 技能库（custom/repo） | 上传、详情、转换、提升、模板下载 |
| B2 | worker 安装包 | 构建产物上传 OBS，分发走临时 URL |
| B3 | project-info 产物 | project-graph.md / project-capabilities.md / .metadata.json |
| B4 | 共享知识 | knowledge.md（project/issue/issue_agent 三 scope） |
| B5 | 事件档案 event.json | 双写 OBS（低优先级，可选） |
| B6 | 一次性生成物 | 技能模板 zip、导出包，临时 URL 分发 |
| B7 | 清理联动 | 解除托管时删除 OBS 对应前缀 |
| B8 | 存量数据迁移 | 本地 → OBS 一次性迁移脚本 |
| B9 | admin 技能链路 | admin 技能上传/提升/详情走共享存储 |

### 3.2 排除范围（Out of Scope）

| 编号 | 场景 | 排除理由 |
|------|------|---------|
| C1 | git bare mirror / worktree | git 对象库随机读写+原子性，OBS 不适用；保持每机本地 |
| C2 | codegraph 索引（.codegraph/） | 本地生成、体量大、单点跑 |
| C3 | 任务物化目录（.skills/.project-info/.knowledge） | Worker 本地落地，随任务快照重建 |
| C4 | 会话目录（vod/feishu/history/onboarding-agent） | 高频追加小文件，OBS 成本不划算；会话亲和兜底 |
| C5 | 业务数据库 | SQLite 仅单机；多集群切 MySQL RDS（已有 mysql_compat） |
| C6 | 日志 | 每节点本地，集中采集另行 |
| C7 | 官方技能库 | 部署物，随代码发布保持本地 |
| C8 | 静态资源、密钥配置、代码 | 部署物 |

## 4. 功能需求（FR）

### FR1 统一存储抽象
- FR1.1 提供 `StorageBackend` 接口：save_bytes / save_stream / read_bytes /
  read_file / exists / delete_prefix / temporary_url / backend_name。
- FR1.2 提供 `LocalStorage`（现状行为）与 `ObsStorage`（esdk-obs-python）双实现。
- FR1.3 `get_storage()` 单例按配置返回实现；`OBS_ENABLED=false` 时行为与现状完全一致。

### FR2 技能上传 OBS（主服务）
- FR2.1 `POST /api/skills/upload` 校验逻辑全部保留（10MB、ZIP 魔数、路径穿越、
  front matter），校验通过后文件写入 OBS 对象键
  `skills/{scope}/{owner}/{skill_name}/...`。
- FR2.2 上传失败返回 502 且不产生半成品（DB 先行 + 失败回滚删除 OBS 前缀）。

### FR3 技能读取与下载
- FR3.1 技能详情 `GET /api/skills/{name}` 正文经存储抽象读取，调用方无感。
- FR3.2 技能模板 `GET /api/skills/template`：打包后上传 OBS，返回
  `{url, expires_in}` 临时签名 URL（原 FileResponse 逻辑退役）。
- FR3.3 技能转换 `convert_skill`：目录复制改 OBS 对象复制（list+read+write）。

### FR4 worker 安装包 OBS 分发
- FR4.1 构建产物（grape-worker.tar.gz + VERSION + SHA256SUMS）上传 OBS；
  版本未变不重复上传。
- FR4.2 `GET /api/runtime/install/package` 返回 `{url, expires_in: 1800, sha256}`；
  保留 302 兼容（老 install 脚本重定向）。
- FR4.3 本地缓存（data/cache/worker-package/）保留为 OBS 故障回退源。

### FR5 project-info / knowledge 双写共享
- FR5.1 产物/知识文件本地写完后异步上传 OBS（write-through），失败仅告警不阻塞。
- FR5.2 读取优先本地缓存，miss 回源 OBS 并落缓存。
- FR5.3 Worker 侧任务快照下发机制不变（快照由服务端从共享存储读全）。

### FR6 事件档案（可选）
- FR6.1 event.json 原子写后异步上传 OBS，供任务换节点续接读取。

### FR7 清理联动
- FR7.1 解除托管（/api/repos/remove）时，除删除本地目录外，按前缀删除 OBS
  对象：skills/repo/{key}/、project-info/{owner}/{repo}/、knowledge/{owner}/{repo}/。

### FR8 存量迁移
- FR8.1 提供 `scripts/migrate_to_obs.py`：枚举纳入范围目录，按对象键规则上传，
  sha256 比对校验；支持 `--dry-run` 与 `--delete-after`。

### FR9 admin 技能链路（评审点 B，默认 B1）
- FR9.1 admin 技能上传/提升/详情改为调用主服务 `/api/skills/*`（或共享存储抽象），
  admin 侧不再直接读写本地 skills/ 目录。
- FR9.2 admin 技能列表等 DB 读逻辑不变（共享 MySQL 已一致）。

## 5. 非功能需求（NFR）

### NFR1 兼容性
- 单机部署 `OBS_ENABLED=false` 全链路行为不变，零影响。
- worker 侧协议向后兼容：老安装脚本可经 302 重定向下载；任务快照格式不变。

### NFR2 安全
- OBS bucket 私有（private ACL），禁止公共读。
- AK/SK 仅经环境变量注入（Python）/ k8s secret（admin），不入配置仓库、不入日志。
- 临时 URL 仅签发 GET；默认有效期 300s（技能/知识），安装包 1800s。
- 对象键动态片段（user_id / repo_key / skill_name）全部过白名单正则，防注入。
- 上传校验（大小/类型/zip slip）全部在服务端完成后再转存 OBS。

### NFR3 性能
- 下载流量不经过服务端（临时 URL 直连 OBS）。
- 高频读（技能打包）走本地缓存，降低 OBS 请求量与延迟。
- 安装包分发单点构建、多点直连下载。

### NFR4 可用性
- OBS 故障：上传类返回明确错误不产生半成品；下载类本地缓存命中回退
  FileResponse，未命中返回 503 + 告警；配置开关可秒级回滚 local。
- 临时 URL 过期：客户端失败重取，不缓存复用过期 URL。

### NFR5 可运维
- 配置项：OBS_ENABLED / ENDPOINT / BUCKET / PREFIX / URL_EXPIRES / 缓存根，
  全部经环境变量或 configs 配置文件（AK/SK 除外）。
- 提供存量迁移脚本与 --dry-run 预演。
- 关键路径日志包含 backend_name 与对象键前缀（不含密钥）。

## 6. 验收标准

1. 配置 OBS_ENABLED=true（测试桶）：A 节点上传技能 → B 节点技能列表可见、
   详情可读、任务可打包使用；admin 技能维护一致。
2. 技能模板/安装包下载返回临时 URL，浏览器/curl 直连可下载；过期后访问失败。
3. 安装包 sha256 与包内 SHA256SUMS 一致；VERSION 未变更不重复上传。
4. project-info / knowledge 写入后 OBS 存在对应对象；删除本地缓存后读取回源成功。
5. 解除托管后 OBS 对应前缀对象清零（listObjects 校验）。
6. 迁移脚本 --dry-run 输出正确清单；实跑后 OBS 与本地 sha256 全量一致。
7. OBS_ENABLED=false 回归：pytest tests/ -q 全绿，单机功能无变化。
8. 日志与响应体中不出现 AK/SK；临时 URL 不含密钥信息。

## 7. 约束与假设

- 华为云 OBS 作为唯一对象存储目标；测试期可用 MinIO（S3 兼容）模拟。
- 生产环境已具备共享 MySQL RDS（多集群 DB 前置条件，不在本文档改造范围）。
- worker 端（requirements-worker.txt）不引入 OBS SDK；文件获取仅经
  任务快照与服务端签发的临时 URL。
- 多集群节点需能访问 OBS 公网/内网 endpoint；内网优先。

## 8. 术语

| 术语 | 说明 |
|------|------|
| OBS | 华为云对象存储服务（Object Storage Service） |
| 临时签名 URL | OBS createTemporarySignature 生成的带签名 GET 地址，到期失效 |
| StorageBackend | 文件存储抽象，local/obs 双实现 |
| 对象键（Object Key） | OBS 桶内文件路径，形如 grape/{env}/skills/... |
