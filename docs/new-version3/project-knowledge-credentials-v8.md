# 项目知识承载仓库凭据 — 实施记录（v8，2026-08-19）**【已废弃：2026-08-19 用户拍板移除该能力】**

> ⚠️ 本能力已整体回退（代码/前端/data 全部移除）——回退动因：仓库凭据能力不需要
> （对齐 skillsopr 流程：凭据走环境变量注入而非知识文件）。此文档仅留审计痕迹。
> 回退清单见下，当前代码已无 repo-credentials 相关逻辑。

> 已落地功能：Project Knowledge 上传 `repo-credentials.json` 作为**项目级仓库凭据单文件**，
> 随任务 project_info 打包携带，供 skill 按约定路径读取。
> 加密方式：A（明文一版 + 注释；加密后续加固，格式不变只包 value 外壳）。
> skill 契约更新：暂缓（本版不做 SKILL.md 改动）。
> v2 变更（2026-08-19）：**独立凭据接口**——不改动成员上传代码。
>   成员上传链路完全还原；凭据走独立 GET/POST/DELETE /api/knowledge/credentials。
>   登记 scope='project' + status='credentials'（复用枚举）；知识沉淀查询按 file_path 排除凭据行（防混入）。

---

## 一、本次改动（代码）

### 1. src/api/routes/knowledge.py — 上传特判
```python
# upload_knowledge 落盘逻辑：
if name == "repo-credentials.json":
    dest = os.path.join(base, "project-info", "repo-credentials.json")   # 项目级固定路径（覆盖式）
    os.makedirs(os.path.dirname(dest), exist_ok=True)
else:
    # 原有 member/{username}/ 逻辑（知识文档，重名加时间戳）
    ...
```
- `repo-credentials.json` → 覆盖写到 `project-info/` 根（最后上传者生效）
- 其他文件行为不变（member/ 分用户、重名时间戳）

### 2. src/runtime/service.py — 打包白名单加 1 文件
```python
_PROJECT_INFO_FILES = ("project-graph.md", "project-capabilities.md", "repo-credentials.json")
```
- `load_project_info()` 遍历白名单，自动把该文件随 project_info_files 打包 → worker 侧 `{{KNOWLEDGE_DIR}}/project-info/repo-credentials.json`

### 3. src/api/static/js/pages/knowledge.js — 上传区指引
上传弹窗文件选择区下加一行提示（repo-credentials.json 命名 + 格式说明）。

---

## 二、文件约定（用户上传的内容）

**路径**：`{WORKSPACE_DIR}/{owner}/{repo}/project-info/repo-credentials.json`

**格式**：
```jsonc
{
  "version": 1,
  "secrets": {                       // 凭据（生产加密 {enc:AES...}；当前明文过渡）
    "GITHUB_TOKEN":  "...",
    "GITCODE_TOKEN": "...",
    "HUAWEI_AK":     "...",
    "HUAWEI_SK":     "..."
  },
  "config": {                        // 非敏感仓库信息（明文）
    "GITHUB_PUBLISH_REPO":  "yangaiwu/grape_dev",
    "GITCODE_PUBLISH_REPO": "signway/grape_skill",
    "CLAWHUB_OWNER_HANDLE": "@signway"
  }
}
```

约定：
- `secrets.KEY` 的 key = 环境变量名约定（skill 读取时对齐）
- `config.KEY` = 仓库地址/owner 等明文

## 三、语义

- 项目级**单文件**：一个 repo 只有一份（后上传覆盖先上传，无多用户多份问题）
- skill 读取：`{{KNOWLEDGE_DIR}}/project-info/repo-credentials.json`（待 skill 契约后续做）
- 与 repo_platform_tokens 关系：并行独立、互不迁移（repo_platform_tokens 管发布令牌/目标仓库的 env 注入；本文件管项目知识凭据）

## 四、验证（已做）

- 上传 `repo-credentials.json` → 落盘 `project-info/` 根（非 member/）
- `load_project_info` 打包含该文件（project_info_files 出现 `project-info/repo-credentials.json`）
- worker 侧 `{{KNOWLEDGE_DIR}}/project-info/repo-credentials.json` 可读

## 五、后续（暂缓项）

- skill-publish 等 SKILL.md 加"读取 repo-credentials.json"契约
- secrets 值加密（`{enc:...}` 包裹，skill 侧解密）——格式不变，后续加固