# Issue 附件可见性分级方案

> 状态：待审批 | 2026-09-14
> 基于：issue-attachment-design-v2.md（v2 已实现附件上传/存储/prompt 注入）

## 一、问题

v2 方案已落地，但附件可见性存在盲区：

1. **只有第一个 agent（VOD）能看到附件**——附件链接嵌在 issue body 里，只有 issue_opened 事件的 payload 携带完整 body
2. **下游 agent（issue/coding/qa/sre）通过 A2A 交办**——A2A payload 只有 issue_number + context（前序 agent 文字摘要），没有 issue body，附件链接丢失
3. **当前没有分级机制**——要么全看到（浪费算力），要么全看不到（信息断裂）

## 二、分级策略

按 agent_type 决定附件信息注入级别，不依赖 A2A 传递，在 prompt_builder 里直接从文件系统扫描附件目录注入：

| agent_type | 注入内容 | 图片附件 | 非图片附件 | 下载指引 |
|------------|----------|----------|------------|----------|
| vod | issue body 里的附件链接 + 完整处理指引 | ✅ 调 analyze-image | ✅ curl 下载读取 | ✅ 完整 |
| issue | 附件文件名清单（仅列表，无 URL） | ❌ 不注入 | ✅ 文件名列表 | ❌ 不注入 |
| coding | 非图片附件的文件名 + URL + 下载指引 | ❌ 不注入 | ✅ 文件名+URL | ✅ 简短 |
| qa | 不注入 | ❌ | ❌ | ❌ |
| sre | 不注入 | ❌ | ❌ | ❌ |

设计原则：

- **VOD 是附件的唯一全量消费者**：图片调 VLM 分析（5s/张），代码/文档自行下载读取。开销只发生一次。
- **Issue 只需知道"有什么"**：文件名清单足够支撑可开发性分析和 Spec 撰写（如"参考用户提供的 xxx.sql"），不需要自己下载。非图片附件的下载由 coding 负责。
- **Coding 需要拿到原始文件**：用户上传的参考代码、配置模板、SQL 脚本，coding 要 curl 下载后 read tool 读取。图片不传——已被 VOD 分析，描述走 A2A context。
- **QA/SRE 完全不碰附件**：QA 看 PR diff，SRE 做部署，与用户附件无关。
- **不改 A2A 结构**：webhook.py 和 event_pipeline.py 不动，附件信息在 prompt_builder 层按 agent_type 分级注入。

## 三、改动清单

### 3.1 prompt_builder.py — 新增统一附件注入函数

新建 `_inject_attachments_by_role` 函数，**所有 agent_type 的附件注入都走这一个函数**。函数内部按 agent_type 分流，统一从文件系统扫描附件目录获取信息（不再从 issue body 正则匹配）：

```python
def _inject_attachments_by_role(parts, repo_key, issue_id, agent_type, runtime_id=0):
    """统一附件注入入口。按 agent_type 分级，从文件系统扫描附件目录。
    
    所有 agent_type 都走此函数，内部按角色决定注入内容：
    - vod:     图片+文件 URL + 完整处理指引（调 analyze-image / curl 下载）
    - issue:   只注入文件名清单（无 URL、无下载指引）
    - coding:  非图片附件的文件名 + URL + 简短下载指引
    - qa/sre:  不注入
    """
    if agent_type not in ("vod", "issue", "coding"):
        return  # qa/sre 不注入
    
    if not repo_key or not issue_id:
        return
    
    from src.repo_key import split_owner_repo
    from src.config import WORKSPACE_DIR
    import os
    
    try:
        _owner, _rname = split_owner_repo(repo_key)
    except Exception:
        return
    
    _issue_num = str(issue_id).lstrip("#")
    _attach_dir = os.path.join(WORKSPACE_DIR, "..", "data", "issue_attachments",
                               _owner, _rname, _issue_num)
    if not os.path.isdir(_attach_dir):
        return
    
    # 扫描附件文件，分类
    _images = []
    _files = []
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    for _fname in sorted(os.listdir(_attach_dir)):
        _fpath = os.path.join(_attach_dir, _fname)
        if not os.path.isfile(_fpath):
            continue
        _ext = os.path.splitext(_fname)[1].lower()
        if _ext in _IMAGE_EXTS:
            _images.append(_fname)
        else:
            _files.append((_fname, _ext))
    
    if not _images and not _files:
        return
    
    from src.user.db import get_meta_value as _gmv
    _base_url = (_gmv("server_base_url") or "").rstrip("/")
    
    # --- VOD：完整附件处理指引 ---
    if agent_type == "vod":
        _lines = []
        for _fname in _images:
            _url = f"{_base_url}/static/issue_attachments/{_owner}/{_rname}/{_issue_num}/{_fname}"
            _lines.append(f"- 图片 {_fname}: {_url}")
        for _fname, _ext in _files:
            _url = f"{_base_url}/static/issue_attachments/{_owner}/{_rname}/{_issue_num}/{_fname}"
            _lines.append(f"- 文件 {_fname}: {_url}")
        parts.append(
            f"\n## 用户附件\n"
            f"Issue 包含以下用户上传附件，请按需读取：\n"
            + "\n".join(_lines) + "\n"
            f"- 图片：用 bash 工具执行 curl {SERVER_URL}/api/analyze-image?url=<图片URL>，"
            f"返回 JSON 中 description 字段为图片内容描述。\n"
            f"- 文本/代码/文档：用 bash 工具 curl 下载到当前工作目录，然后用 read tool 读取内容。\n"
            f"- 图片分析完成后，在 A2A 交办的 context 中写明图片描述结果，供下游 Agent 参考。\n"
            f"不相关的附件可忽略，不要浪费时间下载所有附件。\n"
        )
    
    # --- Issue：只注入文件名清单 ---
    elif agent_type == "issue":
        _names = [f[0] for f in _files]
        if _images:
            _names.extend(_images)
        if _names:
            parts.append(
                f"\n## 用户附件\n"
                f"用户上传了以下附件：{', '.join(_names)}\n"
                f"图片附件已由 VOD Agent 分析，描述见前序 Agent 摘要。"
                f"非图片附件可在 A2A 交办 coding 时说明需参考的文件名。\n"
            )
    
    # --- Coding：非图片附件文件名 + URL + 简短指引 ---
    elif agent_type == "coding":
        if _files:
            _lines = []
            for _fname, _ext in _files:
                _url = f"{_base_url}/static/issue_attachments/{_owner}/{_rname}/{_issue_num}/{_fname}"
                _lines.append(f"- {_fname}: {_url}")
            parts.append(
                f"\n## 用户附件\n"
                f"以下非图片附件可供参考，按需 curl 下载到当前工作目录后用 read tool 读取：\n"
                + "\n".join(_lines) + "\n"
                f"不需要的附件可忽略。图片附件已由 VOD Agent 分析，描述见前序 Agent 摘要。\n"
            )
```

### 3.2 prompt_builder.py — 替换原有 body 正则逻辑

当前 build_devops_agent_prompt 第 555-566 行的 body 正则检测 + 附件处理指引**整体删除**，替换为一行调用：

```python
# 删除原有的 body 正则附件检测逻辑（第 555-566 行）
# 替换为：
_inject_attachments_by_role(parts, _rk, issue_id, agent_type, runtime_id)
```

body 本身的注入（`parts.append(f"Body: {body}")`）保留不动——body 包含 issue 描述，不只是附件。附件指引统一走新函数从文件系统获取，不再依赖 body 正则。

### 3.3 设计要点

- **统一入口**：所有 agent_type 都走 `_inject_attachments_by_role`，VOD 不再走单独的 body 正则路径
- **信息来源统一为文件系统**：扫描 `data/issue_attachments/{owner}/{repo}/{issue_number}/` 目录，比 body 正则更可靠（body 可能被 4000 字符截断砍掉链接）
- **body 正则逻辑移除**：不再从 issue body 里正则匹配 `![...](url)`，附件信息全部来自文件系统扫描

## 四、不改动的部分

| 文件 | 说明 |
|------|------|
| `src/api/routes/webhook.py` | A2A 结构不变，不传 issue body |
| `src/api/event_pipeline.py` | 流水线逻辑不变 |
| `src/api/routes/tasks.py` | 上传/清理/目录结构不变 |
| `src/api/routes/chat.py` | analyze-image 端点不变 |
| `src/api/main.py` | 静态服务挂载不变 |
| `src/api/static/js/pages/tasks.js` | 前端不变 |
| DB schema | 不新增表/列 |

## 五、影响分析

**算力开销**：

| agent | 附件相关开销 |
|-------|-------------|
| VOD | 图片 VLM 分析 5s/张 + 文件下载读取（按需）——与 v2 一致，无增量 |
| Issue | 扫描目录（<1ms）+ 文件名清单注入（几十 token）——极小 |
| Coding | 扫描目录（<1ms）+ 非图片附件 URL 列表（几十 token）——只在需要时 curl 下载 |
| QA/SRE | 零 |

对比"全量传 issue body"方案：每个下游 agent prompt 增加 4000 字符 body + 完整指引，且每个 agent 都可能重复下载/分析图片。分级方案只传精简清单，图片只分析一次。

**数据一致性**：附件目录是文件系统真相，扫描目录与 issue body 里的链接指向同一批文件，不存在不一致。

**远端 worker**：附件 URL 用 `server_base_url`（公网地址），worker 可达。扫描目录在服务端 prompt 构建时执行（`_enqueue_pipeline_task` / direct 模式都在服务端），worker 端不感知文件系统。

**边界情况**：

- 附件目录不存在（无附件）：函数直接 return，零影响
- issue_id 为空：函数直接 return
- pending 目录残留（理论上创建 issue 时已 rename 到 issue_number 目录）：不扫 pending，只扫 issue_number 目录
- 用户在 issue 创建后通过 Task Center 追加附件：coding/issue agent 执行时扫描到最新文件列表

## 六、文件改动总表

| 文件 | 改动 | 量级 |
|------|------|------|
| `src/prompt_builder.py` | 新增 `_inject_attachments_by_role` 函数 + 调用 + VOD 指引微调 | 中（~50行） |

仅改动一个文件，不碰流水线、不碰 DB、不碰前端。
