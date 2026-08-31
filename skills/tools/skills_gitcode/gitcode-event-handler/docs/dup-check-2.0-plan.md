# issue 判重逻辑 2.0 方案(LLM 语义驱动)

> 2026-08-07 用户需求: 判重从"规则匹配"升级为"LLM 语义判断"
> 状态: 方案待确认, 未实施

## 一、需求原文

issue 提交 → 2.0 判重逻辑(LLM 处理):
1. 判断当前代码仓中 PR 列表能否包含 issue 功能点, 若能 → 提示已有该功能(贴 PR 链接)并关闭该 issue
2. 在 1 不满足情况下, 判断仓库中已有 skills 能否包含 issue 功能点, 若能 → 提示已有该功能(贴 skill 名称)并关闭该 issue
3. 优化类需求不走判重逻辑

## 二、现状(判重 1.0, 规则驱动)

| 检查 | 实现 | 判据 |
|------|------|------|
| 实时 API 判重 | find_duplicate_issue | 技能名精确匹配 / 标题归一化完全一致(查全部状态含 closed) |
| 仓库已开发 | find_skill_in_repo | 技能名全称匹配仓库目录 |
| 已有 PR | find_skill_in_open_prs | 技能名全称 / 产品词+PR 文件(词边界) |
| DB 类似需求 | find_similar_requirement_in_db + LLM 确认 | 同产品+功能词交集 → LLM 语义确认 |
| 优化类跳过 | is_skill_modification | 提到 huawei-cloud-xxx + 修改/优化意图词 → 跳过已存在检查 |

**问题**: 规则只能识别"词面重复"(同名/同产品/同标题), 无法理解"功能点等价"——
「查询ECS规格」vs「查询云主机规格」、换说法、不同词表表达, 规则判不出。

## 三、新逻辑 2.0 设计

### 3.1 整体流程

```
handle_issue 创建路径(判重段, 在质量检查之前)
│
├─ [前置] 优化类需求判断(is_optimization_request)
│    └─ 命中 → 跳过全部判重, 直接进入开发流程
│
├─ [LLM 第 1 步] PR 覆盖判断(_llm_pr_covers_issue)
│    输入: 当前 open PR 列表(标题+body 摘要) + issue 标题+正文
│    输出: (covered, pr_number, reason)
│    └─ covered=True → 评论"已有该功能(PR #N 链接)" + 关闭 issue(不入库)
│
├─ [LLM 第 2 步] 仓库 skills 覆盖判断(_llm_skills_covers_issue)
│    仅第 1 步不满足时执行
│    输入: 仓库已有 skills 列表(名称+SKILL.md 简介) + issue 标题+正文
│    输出: (covered, skill_name, reason)
│    └─ covered=True → 评论"已有该功能(skill 名称)" + 关闭 issue(不入库)
│
├─ [兜底] DB 相似需求检查(保留, 规则+LLM 确认)
│    find_similar_requirement_in_db → LLM 语义确认
│    └─ 命中 → 评论"已有类似需求(issue #N)" + 关闭(不入库)
│
└─ 全部不满足 → 正常进入质量检查/建任务流程
```

### 3.2 前置: 优化类需求识别(新函数 is_optimization_request)

**定义**: 对已有 skill 的优化/增强/迭代/修改类需求, 不判重。

**规则实现**(复用现有 is_skill_modification 思路, 扩展意图词):
```python
OPTIMIZE_WORDS = ("优化", "增强", "改进", "升级", "扩充", "扩展", "迭代",
                  "完善", "提升", "改进", "调整", "变更", "增加", "新增功能",
                  "改成", "改为", "重写", "重构", "修改")
def is_optimization_request(title, body):
    text = f"{title} {body}"
    # 必须提到已有 skill 名(huawei-cloud-xxx 或 xxx-skill)
    has_skill_ref = bool(re.search(r"(huawei[-_]?cloud-[\w-]+|[\w-]+-skill)", text))
    if not has_skill_ref:
        return False
    return any(w in text for w in OPTIMIZE_WORDS)
```
- 命中 → 跳过判重(与 1.0 的 is_skill_modification 跳过已存在检查一致)
- 注意: "优化"在仓库里是高频词(如"优化查询速度"), 必须配合 skill 名全称出现才生效
  (防止"帮我优化一下"无 skill 名时误跳过判重)

### 3.3 LLM 第 1 步: PR 覆盖判断(_llm_pr_covers_issue)

```python
def _llm_pr_covers_issue(repo, title, body):
    """LLM 判断当前 PR 列表(open + merged)是否已覆盖 issue 功能点。
    返回 (covered, pr_number, reason); 失败返回 (False, None, reason) fail-open。
    """
    # 1. 拉取 PR 列表(state=all 最近 30 条, 按更新时间倒序), 过滤:
    #    - open: 开发/审查中, 算"已有该功能(开发中)"
    #    - merged: 已合入主仓, 算"已有该功能(已实现)"
    #    - closed 未合并: 功能未进仓库, 排除(不算已实现, 防止废弃 PR 关新 issue)
    prs = gitcode_get(f"/repos/{repo}/pulls?state=all&per_page=30&sort=updated&direction=desc")
    # 2. 组装 PR 清单文本(每条: #N [状态] 标题 | body 前 200 字)
    # 3. LLM prompt:
    #    "你是代码仓库需求判重器。以下 issue 是用户提交的新需求。判断当前
    #    仓库的 PR 列表中, 是否有 PR 已经实现了该 issue 的功能点(语义等价,
    #    不要求字面相同)。PR 列表中 open=开发中, merged=已合入, 两者都算
    #    已覆盖; closed 未合并的 PR 已排除(其功能未进仓库, 不算已实现)。
    #    issue 标题: {title}
    #    issue 正文: {body}
    #    当前 PR 列表:
    #    {pr_list}
    #    只输出 JSON: {\"covered\": true|false, \"pr_number\": N, \"reason\": \"依据\"}"
    # 4. 解析 JSON; covered=true 且 pr_number 有效 → 返回 (True, pr_number, reason)
    # 5. 失败/超时/解析失败 → (False, None, "LLM 调用失败") fail-open
```

**关键设计点**:
- PR 查 **open + merged**(state=all 过滤 closed 未合并)——merged 的也放第 1 步,
  因为: a) 贴 PR 链接比 skill 名称更直观; b) 覆盖"功能已实现但 skill 目录名
  不同"的边界(第 2 步靠 skill 名/description 匹配可能漏); c) 省去第 2 步
  遍历仓库目录的 N 次 API 调用
- PR body 摘要截断(每条约 200 字), 30 条 PR 控制 prompt 在 ~6K token 内
- LLM 判定"功能点等价"而非"标题相似"——换说法/同义词也能识别

### 3.4 LLM 第 2 步: 仓库 skills 覆盖判断(_llm_skills_covers_issue)

```python
def _llm_skills_covers_issue(repo, title, body):
    """LLM 判断仓库已有 skills 是否已覆盖 issue 功能点。
    返回 (covered, skill_name, reason); 失败返回 (False, None, reason) fail-open。
    """
    # 1. 遍历仓库 skills/ 目录获取已有 skill 清单(名称 + SKILL.md 的
    #    frontmatter description, 用 _get_repo_products 类似逻辑遍历 12 类目)
    # 2. 组装 skills 清单文本(每条: skill_name | description 前 100 字)
    # 3. LLM prompt:
    #    "... 判断仓库已有 skills 中, 是否有 skill 已实现了该 issue 的功能点
    #    (语义等价)。issue: ...; 仓库已有 skills: {skills_list}
    #    只输出 JSON: {\"covered\": true|false, \"skill_name\": \"...\", \"reason\": \"依据\"}"
    # 4. 失败/超时 → (False, None, reason) fail-open
```

**关键设计点**:
- skills 清单来源: 遍历仓库 skills/<cat>/<name> 目录(12 类目, 缓存 10 分钟,
  复用/扩展 _get_repo_products)
- 每个 skill 附带 description(读 SKILL.md frontmatter, 已有类似读取逻辑)
- 仓库 skill 数量多时(如 100+), prompt 可能大 → 需评估: 先按类目分批, 或
  只给 name+description 前 50 字; 或先按产品词粗筛再 LLM 精判
- **性能考虑**: 第 2 步遍历目录 + 读 SKILL.md 是 N 次 API 调用, 建议缓存
  (10 分钟), 与 _get_repo_products 的 _REPO_PRODUCTS_CACHE 合并

### 3.5 与现有检查的关系(保留 vs 替换)

| 现有检查 | 处置 | 理由 |
|----------|------|------|
| find_duplicate_issue(实时 API) | **替换**为 LLM 两步 | 它的"标题完全一致"是 LLM 第 1/2 步的特例, LLM 更强 |
| find_skill_in_repo | **替换**为 LLM 第 2 步 | 同上 |
| find_skill_in_open_prs | **替换**为 LLM 第 1 步 | 同上 |
| find_similar_requirement_in_db | **保留**(第 4 层兜底) | 防"同标题重复提交"(#187/#188/#189), LLM 两步不查 DB 历史 |
| is_skill_modification | **升级**为 is_optimization_request | 扩展意图词, 语义对齐"优化类需求" |

### 3.6 兜底与降级(fail-safe)

| 场景 | 行为 |
|------|------|
| LLM 第 1 步失败(超时/网络/解析) | 降级走第 2 步; 第 2 步也失败 → 走 DB 兜底; 全失败 → 放行(fail-open 不误关) |
| LLM 判 covered 但 pr_number 无效 | 按未覆盖处理, 继续后续步骤 |
| LLM 输出非 JSON | 按未覆盖处理(fail-open) |
| ENABLE_LLM_DUP_CHECK=0 | 跳过 LLM 两步, 直接走 DB 兜底(保留 1.0 的 DB 检查能力) |

### 3.7 新增配置

```python
ENABLE_LLM_DUP_CHECK = os.environ.get("ENABLE_LLM_DUP_CHECK", "1") == "1"  # 已有, 复用
DUP_PR_LIST_LIMIT = 30    # LLM 第 1 步 PR 列表上限
DUP_SKILL_DESC_LEN = 100  # LLM 第 2 步 skill 描述截断长度
```

## 四、行为对比

| 场景 | 判重 1.0(现状) | 判重 2.0(新) |
|------|----------------|--------------|
| 同标题重复提交(#187 类) | find_duplicate_issue 命中 → 关闭 | DB 兜底命中 → 关闭(LLM 两步不查 DB, 但 DB 兜底保留) |
| 同名 skill 已开发(查询eip列表) | find_skill_in_repo 命中 → 关闭 | LLM 第 1 步(merged PR)或第 2 步识别 → 关闭 |
| 已有 PR 在开发(#181 类) | find_skill_in_open_prs 命中 → 关闭 | LLM 第 1 步识别 open PR 已覆盖 → 关闭 |
| 废弃 PR(closed 未合并) | (1.0 不查 closed PR) | **不命中**(第 1 步排除 closed 未合并, 防止废弃 PR 误关新 issue) |
| 换说法(查询ECS规格 vs 云主机规格) | 规则可能漏判 | LLM 语义等价 → 关闭 |
| 优化类需求(huawei-cloud-xxx 改成只查数量) | is_skill_modification 跳过 | is_optimization_request 跳过判重 |
| 同产品不同操作(查询vpc vs 创建vpc) | LLM 确认层判不重复 → 放行 | LLM 两步判不覆盖 → 放行 |
| 竞品云(阿里云需求) | 竞品云检查前置 → invalid | 不变(竞品云检查仍在判重之前) |

## 五、实现步骤

1. 新增 `is_optimization_request()`(扩展 OPTIMIZE_WORDS)
2. 新增 `_llm_pr_covers_issue()`(PR 列表拉取 + LLM prompt + 解析)
3. 新增 `_llm_skills_covers_issue()`(skills 清单 + LLM prompt + 解析)
4. 改造 handle_issue 判重段: 前置优化类判断 → LLM 第 1 步 → LLM 第 2 步 → DB 兜底
   (替换 find_duplicate_issue / find_skill_in_repo / find_skill_in_open_prs 的调用)
5. handle_issue_update / handle_issue_comment 升级路径同步改造(判重前置到质量检查前)
6. 验证: 真实用例(重复 PR/重复 skill/换说法/优化类/竞品云/LLM 失败)
7. SKILL.md 更新 + 归档推送

## 六、风险与缓解

| 风险 | 缓解 |
|------|------|
| LLM 误判 covered 关错 issue | fail-open(失败不关); covered 时必须带 pr_number/skill_name 证据; 评论可人工看到 |
| PR 列表/skills 清单 prompt 过大 | 截断(body 200 字/desc 100 字); 上限 30 PR; skills 多时按产品词预筛 |
| 性能(遍历目录 N 次 API) | _REPO_PRODUCTS_CACHE 缓存 10 分钟复用 |
| 与 1.0 行为差异(同标题重复依赖 DB) | DB 兜底保留, 行为不回归 |
| closed 未合并 PR 误判已实现 | 第 1 步拉 state=all 后**过滤掉 closed 未合并**, 仅 open+merged 进入 LLM 判断 |

## 七、待确认点

- [ ] 优化类意图词表是否够(现有 SKILL_MODIFY_WORDS + 优化/增强/改进 等)
- [ ] DB 相似需求检查保留为第 4 层兜底, 可否接受
- [ ] 仓库 skills 清单: 全量给 LLM vs 按产品词预筛后再给
- [ ] 关闭时评论格式: "已有该功能(PR #N: 标题)" / "已有该功能(skill: 名称)"
- [x] PR 范围: **open + merged, 排除 closed 未合并**(2026-08-07 已确认)
