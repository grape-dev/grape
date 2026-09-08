# 场景同步优化：逐文件 API 下载 → ZIP 归档下载

> 日期：2026-09-07
> 关联设计：[issue-11-scene-skill-market-tech-design.md §8.6](./issue-11-scene-skill-market-tech-design.md)
> 影响文件：`src/storage/scene_sync.py`、`tests/test_scene_sync.py`
> 不影响：DB 表结构、configs、Dockerfile、API 接口、前端

---

## 一、背景

### 1.1 现状

`src/storage/scene_sync.py` 通过 GitCode API v5 逐文件下载场景：

1. `_api_list_dir` — 调 `/api/v5/repos/{owner}/{repo}/contents/{path}` 列目录
2. `_api_get_file` — 逐个文件下载（base64 解码）
3. `_download_dir_recursive` — 递归遍历目录，根目录只取 scene.json/README.md/icon.*，references/ 下全量下载

7 个场景 × 每个 2-3 个文件 = 20+ 次 HTTP 请求。

### 1.2 问题

| 问题 | 说明 |
|------|------|
| 请求数多 | 20+ 次 API 调用，每次都有网络开销和 429 限流风险 |
| 递归下载脆弱 | 单文件失败需 per-file try-catch 兜底，逻辑复杂 |
| 下架基准不可靠 | 需要 `repo_scene_names`（API 列目录结果）做下架基准，因为逐文件下载可能部分成功部分失败。这是上一轮修 bug 时加的补丁，本质是逐文件下载的中间态问题 |
| 分页防御冗余 | `_api_list_dir` 加了分页循环防御，但 GitCode contents 接口当前忽略分页参数全量返回，循环只跑一轮——多余代码 |

### 1.3 原设计文档的理由（已证伪）

原设计文档 §8.6（第 1133-1137 行）选择逐文件下载而非 ZIP 归档下载，理由：

> - GitCode 的 ZIP 归档 URL（`/-/archive/{ref}/{repo}-{ref}.zip`）返回 HTML 页面而非 ZIP 文件，无法直接下载

**实测结论**：`https://gitcode.com/{owner}/{repo}/-/archive/{ref}.zip` 确实返回 HTML（SPA 前端拦截，`x-rendered-by: proxy`）。但 GitCode 真实的 ZIP 下载域名是 `raw.gitcode.com`，URL 格式为：

```
https://raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip
```

这个端点实测可用（公开仓库直接下载，私有仓库用 `Authorization: Bearer {token}` 认证），返回标准 ZIP 文件。

---

## 二、实测验证

### 2.1 测试环境

- 仓库：`grape-dev/grape-skills`（私有），分支 `dev`
- token：`configs/scene-sync.json` 的 `gitcode_api_token`
- 工具：curl + Python httpx

### 2.2 测试结果

| URL | 认证方式 | 结果 |
|-----|---------|------|
| `gitcode.com/{owner}/{repo}/-/archive/{ref}.zip` | PRIVATE-TOKEN / Bearer / access_token / Basic auth / oauth2 URL 嵌入 | 全部返回 HTML（3576 字节 SPA 壳） |
| `api.gitcode.com/api/v5/repos/{owner}/{repo}/archive/{ref}.zip` | Bearer | 404 |
| `api.gitcode.com/api/v5/repos/{owner}/{repo}/repository/archive` | Bearer | 404 |
| `raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip` | 无（公开仓库） | ✅ ZIP 文件（1.8MB） |
| `raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip` | Authorization: Bearer {token} | ✅ ZIP 文件（637KB，私有仓库 grape-skills） |
| `raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip` | access_token={token} 参数 | ✅ ZIP 文件（md5 与 Bearer 一致） |
| `raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip` | oauth2:{token} URL 嵌入 | ❌ JSON 错误（不支持 Basic auth） |

### 2.3 发现过程

GitCode 前端是 Vue SPA，`/-/archive/` 路径被前端代理拦截返回 `index.html`。用 Playwright 渲染仓库页面，点击"下载zip"按钮，监听到浏览器实际发起的下载请求指向 `raw.gitcode.com` 域名。

### 2.4 ZIP 包内容

```
grape-skills-dev/              ← 顶层目录 {repo}-{branch}
├── .gitignore
├── README.md
├── skill-description.json
└── scenes/
    ├── agent-dev/
    │   ├── README.md
    │   └── scene.json
    ├── blog-publish/
    │   ├── README.md
    │   └── scene.json
    ├── cloud-native/
    │   ├── README.md
    │   └── scene.json
    ├── common-skill-dev/
    │   ├── README.md
    │   └── scene.json
    ├── default/
    │   ├── README.md
    │   └── scene.json
    ├── huawei-skill/
    │   ├── README.md
    │   └── scene.json
    └── iac/
        ├── README.md
        └── scene.json
```

注意：ZIP 包不含 `.git` 目录，是分支最新代码的文件快照。

---

## 三、方案

### 3.1 改动本质

当前：逐文件调 GitCode API v5 列目录 + 下载，7 个场景 20+ 次请求
改后：一次 HTTP 请求下载整个仓库分支 ZIP 包，zipfile 标准库解压后遍历 scenes/ 目录

### 3.2 新增函数

#### `_download_repo_zip(client, token) -> str`

```python
async def _download_repo_zip(client: httpx.AsyncClient, token: str) -> str:
    """下载仓库 ZIP 归档并解压到临时目录，返回解压后的本地根路径。

    URL: https://raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip
    认证: Authorization: Bearer {token}
    ZIP 内顶层目录名: {repo}-{branch}
    """
```

- 下载 ZIP bytes → `zipfile.ZipFile(io.BytesIO(data))` 解压到临时目录
- 返回解压后的本地路径（`{tmp_dir}/{repo}-{branch}/`）
- 下载失败抛异常，由调用方 `sync_official_scenes` 的 try-catch 处理

### 3.3 删除函数

| 函数 | 行数 | 删除原因 |
|------|------|---------|
| `_api_list_dir` | 202-229 | 逐文件列目录，不再需要 |
| `_api_get_file` | 232-243 | 逐文件下载，不再需要 |
| `_download_dir_recursive` | 246-275 | 递归下载目录，不再需要 |
| `import base64` | 15 | 只被 `_api_get_file` 用 |

### 3.4 重写 `sync_official_scenes` 主流程

**删除**（第 324-355 行）：API 列目录 + 逐场景下载循环 + `repo_scene_names`

**替换为**：

```python
# 1. 下载 ZIP 归档并解压
try:
    repo_root = await _download_repo_zip(client, bot_token)
except Exception as e:
    log.warning(f"Scene sync: download repo zip failed: {e}")
    return

# 2. 定位 scenes/ 目录
scenes_dir = os.path.join(repo_root, "scenes")
if not os.path.isdir(scenes_dir):
    log.warning("Scene sync: scenes/ directory not found in zip")
    return
```

**不变**（第 363-525 行）：刷新 category 缓存 → 遍历场景目录 → 读 scene.json → 校验 → 入库 → 重建 scene_skill_ref

### 3.5 下架逻辑变化

| | 当前 | 改后 |
|---|------|------|
| 代码 | `db_names - repo_scene_names` | `db_names - synced_names` |
| 基准 | API 列目录结果 | 成功入库的场景集合 |
| 原因 | 逐文件下载可能部分成功部分失败，需要 API 列目录结果做兜底基准 | ZIP 包一次性全量下载，要么全部成功要么全部失败。下载失败时 sync_official_scenes 在下载阶段就 return，不会走到下架逻辑 |

`repo_scene_names` 变量删除。

### 3.6 模块文档字符串更新

```
1. 每 30 分钟从 grape-skills 仓库通过 GitCode API v5 逐文件下载 scenes/ 目录
```
→
```
1. 每 30 分钟从 grape-skills 仓库下载 ZIP 归档包并解压 scenes/ 目录
```

### 3.7 不改动

| 模块 | 说明 |
|------|------|
| `_load_config` / `_get_sync_token` | 配置和 token 获取逻辑不变 |
| `_compute_scene_hash` | hash 计算逻辑不变 |
| `_validate_scene_nodes` | nodes 校验逻辑不变 |
| `_check_skills_exist` | skill 预检不变 |
| `_parse_version` | 版本比较不变 |
| `_refresh_cat_cache` | category 缓存不变 |
| `_rebuild_scene_skill_ref` / `_rebuild_scene_skill_ref_by_name` | skill_ref 重建不变 |
| `_scene_sync_worker` / `start_scene_sync_scheduler` / `stop_scene_sync_scheduler` | 调度逻辑不变 |
| `configs/scene-sync.json` | token 和 repo 配置复用 |
| `Dockerfile` | 不依赖 git 命令，zipfile 是 Python 标准库 |
| DB 表结构 | 不变 |
| API 接口 / 前端 | 不变 |

---

## 四、测试改动

### 4.1 删除

| 测试类/函数 | 测试数 | 删除原因 |
|------------|--------|---------|
| `TestApiListDir` | 4 | `_api_list_dir` 不再存在 |
| `TestApiGetFile` | 2 | `_api_get_file` 不再存在 |
| `TestDownloadDirRecursive` | 5 | `_download_dir_recursive` 不再存在 |
| `test_download_failure_continues` | 1 | 逐场景下载失败场景不再适用 |
| `test_download_failure_does_not_delist` | 1 | `repo_scene_names` 补丁不再需要 |
| import 里的 `_api_list_dir` / `_api_get_file` / `_download_dir_recursive` | — | 清理 import |

### 4.2 新增

| 测试 | 验证点 |
|------|--------|
| `TestDownloadRepoZip.test_download_and_extract` | mock httpx 返回 ZIP bytes，验证解压后目录结构正确 |
| `TestDownloadRepoZip.test_auth_header` | 验证请求头包含 `Authorization: Bearer {token}` |
| `test_zip_download_failure_skips` | ZIP 下载失败时 `sync_official_scenes` 直接 return，不执行下架 |
| `test_full_sync_new_scene` | 改为 mock `_download_repo_zip`（替代原来的 mock `_download_dir_recursive`） |

### 4.3 保留不变

| 测试 | 数量 |
|------|------|
| `TestComputeSceneHash` | 4 |
| `TestValidateSceneNodes` | 5 |
| `TestCheckSkillsExist` | 2 |
| `TestParseVersion` | 1 |
| `test_no_token_skips` | 1 |
| `test_only_template_dirs_skips` | 1（调整 mock 路径） |
| `_rebuild_scene_skill_ref` 相关 | 不变 |

### 4.4 预计

- 当前：45 个测试
- 改后：~35 个测试（删 13 个，新增 4 个）

---

## 五、影响评估

| 维度 | 影响 |
|------|------|
| DB | 无变更 |
| API | 无变更 |
| 前端 | 无变更 |
| 配置 | 无变更（`configs/scene-sync.json` 复用） |
| Dockerfile | 无变更（zipfile 是标准库，不依赖 git） |
| 依赖 | 无新增（zipfile + io 都是 Python 标准库，httpx 已有） |
| 部署 | 无变化（改 Python 代码后重启 3000 端口即生效） |

---

## 六、确认点

1. ZIP 下载 URL：`https://raw.gitcode.com/{owner}/{repo}/archive/refs/heads/{branch}.zip`
2. 认证方式：`Authorization: Bearer {token}`（不用 access_token URL 参数，避免日志泄露）
3. ZIP 包内顶层目录名：`{repo}-{branch}`，scenes/ 在其下一级，`_download_repo_zip` 返回后由 `sync_official_scenes` 拼接 `os.path.join(repo_root, "scenes")`
4. 下架基准回到 `db_names - synced_names`，删除 `repo_scene_names`
5. 不改 Dockerfile，不依赖 git 命令
