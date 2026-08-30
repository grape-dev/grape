# Issue 无关评论过滤 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 handle_issue_comment 中过滤无关评论(灌水短文本/纯 emoji/与需求无关的闲聊),静默跳过不转发 Multica;有实质内容评论与明确意图(关闭请求/need_info 补充)不受影响。

**Architecture:** 在 `gitcode_event_handler.py` 的 handle_issue_comment 中,意图1(关闭请求)之后、意图2(need_info 补充)之前插入无关评论过滤。新增 `is_irrelevant_comment()` 双层判定:层2 短文本(≤8 字且命中灌水词集或纯 emoji/符号)、层3 需求相关性(与产品词库/功能词/issue 标题归一化交集为空)。判定异常 fail-open。预留 LLM 开关位。

**Tech Stack:** Python 3.12, 现有脚本(requests/pymysql 已有), 无新依赖。

## Global Constraints

- 修改文件: `scripts/gitcode_event_handler.py`(唯一代码文件)
- 开关默认: `ENABLE_COMMENT_FILTER=1`, `ENABLE_COMMENT_LLM_FILTER=0`(预留,本次不实现调用)
- 灌水词集 `IRRELEVANT_COMMENT_WORDS` 环境变量可覆盖(逗号分隔)
- 判定异常必须 fail-open(不过滤,继续原流程)
- 过滤位置:意图1 之后、意图2 之前(关闭请求豁免,自查记录 §6)
- 测试用 `importlib` 加载真实脚本 + monkeypatch 外部副作用;真实只读 API/DB 调用安全
- 完成后同步归档副本 skillsopr(tools/skills_gitcode/gitcode-event-handler/)并脱敏 + git commit
- 回复与文档语言: 中文

---

### Task 1: 配置项与常量

**Files:**
- Modify: `scripts/gitcode_event_handler.py`(配置区,约 150-170 行附近)

**Interfaces:**
- Produces: 模块级常量 `ENABLE_COMMENT_FILTER: bool`、`ENABLE_COMMENT_LLM_FILTER: bool`、`IRRELEVANT_COMMENT_WORDS: list[str]`、`COMMENT_FUNC_WORDS: list[str]`(功能词集)

- [ ] **Step 1: 写失败测试**(在测试脚本中断言常量存在且默认值正确:ENABLE_COMMENT_FILTER=True、ENABLE_COMMENT_LLM_FILTER=False、IRRELEVANT_COMMENT_WORDS 含"好的"、"👍"、"+1")
- [ ] **Step 2: 运行测试确认失败**(常量未定义 → AttributeError)
- [ ] **Step 3: 实现配置区新增**(放 ENABLE_COMMENT_INTENT 附近):
  ```python
  # 无关评论过滤(2026-08-05 用户要求): 灌水短文本/纯 emoji/与需求无关的
  # 闲聊评论静默过滤, 不转发 Multica; 判定异常 fail-open
  ENABLE_COMMENT_FILTER = os.environ.get("ENABLE_COMMENT_FILTER", "1") == "1"
  # LLM 兜底判断开关(预留位, 本次不实现调用; 后续开启时层3 未命中走 LLM)
  ENABLE_COMMENT_LLM_FILTER = os.environ.get("ENABLE_COMMENT_LLM_FILTER", "0") == "1"
  # 灌水词集: 命中且评论短(≤8 字)或无实质信息 -> 无关评论
  IRRELEVANT_COMMENT_WORDS = [w.strip() for w in os.environ.get(
      "IRRELEVANT_COMMENT_WORDS",
      "好的,收到,顶,辛苦了,👍,+1,明白,了解,ok,OK,可以,行,谢谢,感谢,666,牛,支持,赞,不错,没问题,收到啦,好的呢,嗯嗯,哦哦,哈哈,了解啦,知道,知晓,确认,收到收到,棒,厉害,牛批,给力,真棒,优秀").split(",") if w.strip()]
  # 功能词集: 评论需求相关性判定(层3); 命中任一即视为有实质内容
  COMMENT_FUNC_WORDS = os.environ.get(
      "COMMENT_FUNC_WORDS",
      "查询,列表,创建,删除,修改,修复,问题,bug,报错,错误,支持,希望,需要,补充,信息,需求,skill,技能,华为云,产品,接口,api,参数,返回,结果,数据,测试,验证,发布,上线,优化,改进,建议,方案,增加,添加,实现,开发").split(",")
  ```
- [ ] **Step 4: 运行测试确认通过**
- [ ] **Step 5: 提交**(git commit 在归档同步时统一做,本任务先留工作区)

---

### Task 2: is_irrelevant_comment 判定函数

**Files:**
- Modify: `scripts/gitcode_event_handler.py`(放在 is_test_report 函数之后,约 1880 行附近)

**Interfaces:**
- Consumes: `IRRELEVANT_COMMENT_WORDS`、`COMMENT_FUNC_WORDS`、`HUAWEI_CLOUD_PRODUCTS`(产品词库,已有)
- Produces: `is_irrelevant_comment(note_body: str, issue_title: str, issue_body: str) -> bool` — True=无关(应静默过滤)

- [ ] **Step 1: 写失败测试**(测试脚本中直接调用判定函数,断言 6 种场景):
  - "好的" / "收到" / "顶" / "辛苦了" → True
  - "👍" / "😀😀" / "!!!" → True
  - "咱们下周团建去哪?"(无需求相关词)→ True
  - "请增加按区域筛选功能"(含功能词"增加/功能")→ False
  - "补充: 需要支持按名称模糊查询"(含功能词)→ False
  - "请关闭这个 issue"(意图1 词,应 False——不过滤;实际由意图1 先处理,函数也应为 False)
- [ ] **Step 2: 运行确认失败**(函数不存在 → AttributeError)
- [ ] **Step 3: 实现函数**:
  ```python
  def _strip_comment_noise(text):
      """去除空白与 emoji/符号, 返回可判定长度的核心文本"""
      import re as _re
      t = _re.sub(r"[\s\W_]+", "", text or "", flags=_re.UNICODE)
      # 去 emoji: 保留中文/字母/数字
      return _re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", t)

  def is_irrelevant_comment(note_body, issue_title="", issue_body=""):
      """判断 issue 评论是否为无关评论(2026-08-05 用户要求)。

      层2 短文本: 去除空白/emoji 后 ≤8 字 且(命中灌水词集 OR 纯 emoji/符号)
      层3 需求相关性: 归一化后与需求相关词(产品词库+功能词+issue 标题
      归一化交集)无任何交集 -> 无关
      两处都不命中 -> 有实质内容(相关)。
      异常由调用方 fail-open, 本函数内部不做吞异常。
      """
      b = note_body or ""
      core = _strip_comment_noise(b)
      if not core:
          return True  # 空/纯符号
      # 层2: 短文本灌水
      if len(core) <= 8:
          low = b.lower()
          for w in IRRELEVANT_COMMENT_WORDS:
              if w in low or w in b:
                  return True
          # 纯 emoji/符号: core 去 emoji 后为空
          if not core:
              return True
      # 层3: 需求相关性(针对较长评论, 也兜底层2 未命中)
      combined = f"{issue_title or ''} {issue_body or ''}"
      hit = False
      for w in COMMENT_FUNC_WORDS:
          if w in b:
              hit = True
              break
      if not hit:
          for p in HUAWEI_CLOUD_PRODUCTS:
              if p.isascii():
                  if p in b.lower():
                      hit = True
                      break
              elif p in b:
                  hit = True
                  break
      if not hit and issue_title:
          tn = _norm(issue_title)  # 复用现有归一化
          if tn and tn in _norm(b):
              hit = True
      return not hit
  ```
  注意:层3 对 ≤8 字未命中灌水词的短评论(如"好的收到")也执行(其未命中灌水词但无需求词 → 无关);"请关闭这个 issue" 归一化含"issue"但功能词无"关闭"——但意图1 在过滤前已处理,函数本身对"请关闭"返回 True 也不影响(永远走不到);不过为保险,函数中把意图1 关键词"关闭/取消"加入豁免判断(命中 → False)。
- [ ] **Step 4: 运行测试确认通过**(若"请关闭"返回 True,补豁免逻辑)
- [ ] **Step 5: 提交**

---

### Task 3: handle_issue_comment 接入过滤

**Files:**
- Modify: `scripts/gitcode_event_handler.py`(handle_issue_comment,约 2772-2790 行)

**Interfaces:**
- Consumes: `is_irrelevant_comment()`、`ENABLE_COMMENT_FILTER`
- Produces: 评论处理流中"无关评论静默跳过"分支

- [ ] **Step 1: 写失败测试**(monkeypatch 全流程):
  - 灌水短文本("好的")评论事件 → action=skip, reason 含"无关评论", 不调用 multica_issue_comment
  - 纯 emoji 评论 → skip
  - 无关闲聊("咱们下周团建去哪?")→ skip
  - 有实质内容("请增加按区域筛选功能")→ forwarded(仍转发)
  - 关闭请求("请关闭")→ closed_by_request(不被过滤)
  - need_info 补充("补充: 需要支持 rds 模糊查询")→ upgraded_from_need_info(不被过滤)
- [ ] **Step 2: 运行确认失败**(无关评论仍转发)
- [ ] **Step 3: 实现接入**(意图1 之后、意图2 之前插入):
  ```python
  # 无关评论过滤(2026-08-05 用户要求): 灌水短文本/纯 emoji/与需求无关的
  # 闲聊评论静默过滤, 不转发 Multica、不触发意图2 追问。意图1(关闭请求)
  # 已在上方处理, 天然豁免。判定异常 fail-open(继续原流程)。
  if ENABLE_COMMENT_FILTER:
      try:
          if is_irrelevant_comment(note_body,
                                   issue.get("title") or "",
                                   issue.get("body") or ""):
              logger.info(f"[comment-filter] issue #{issue_number} 评论 {note_id} 无关, 静默跳过: {note_body[:50]}")
              return {"action": "skip", "reason": "无关评论(灌水/闲聊), 静默过滤"}
      except Exception as e:
          logger.warning(f"[comment-filter] 判定异常, fail-open: {e}")
  ```
- [ ] **Step 4: 运行测试确认通过**
- [ ] **Step 5: 提交**

---

### Task 4: 回归、文档与归档

**Files:**
- Modify: `SKILL.md`(逻辑2 说明 + 配置表 + Pitfall 41)
- Modify: 归档副本 `skillsopr/tools/skills_gitcode/gitcode-event-handler/`(同步+脱敏)
- Create: 设计文档已在 `docs/superpowers/specs/2026-08-05-issue-comment-filter-design.md`(本计划同目录)

- [ ] **Step 1: 全量回归**(既有 issue 处理测试集 + 本功能测试;`--test` 自检 7 项)
- [ ] **Step 2: 更新 SKILL.md**(逻辑2 加"无关评论过滤"步骤;配置表加 ENABLE_COMMENT_FILTER/ENABLE_COMMENT_LLM_FILTER/IRRELEVANT_COMMENT_WORDS;Pitfall 41 记录: 灌水评论曾转发 Multica 打扰小队, 过滤位置意图1 后意图2 前的原因)
- [ ] **Step 3: 同步归档 + 脱敏**(3 个占位符 + 注释块;16-17 项 ad-hoc 验证: 语法/零残留/环境变量注入/逐字节一致)
- [ ] **Step 4: 提交并推送 skillsopr**(git commit + push origin master;若有远端新提交先 pull --rebase)
- [ ] **Step 5: fresh ad-hoc 验证**(tempfile 生成 /tmp/hermes-verify-*.py, 跑完即删)

---

## 验收核对(完成时逐条打勾)

- [x] 灌水短文本/纯 emoji 评论不再转发 Multica(静默)
- [x] 与需求无关的长评论不再转发
- [x] 有实质内容评论仍转发(现状不变)
- [x] 关闭请求/need_info 补充不被误过滤
- [x] ENABLE_COMMENT_FILTER=0 时完全恢复现状(测试验证)
- [x] 判定异常 fail-open, 不误吞评论(测试验证)
- [x] 归档副本脱敏零残留, 已推送 skillsopr(提交 dbbb41b)

## 执行记录(2026-08-05)

- Task 1 配置项: 9 项测试 PASS
- Task 2 is_irrelevant_comment: 18 项测试 PASS(含"支持rds"边界修正:
  灌水词+产品词不过滤; 功能词集剔除"支持"避免与灌水词集重叠)
- Task 3 handle_issue_comment 接入: 12 项测试 PASS(红阶段曾误建 Multica
  SKI-143, 已取消并清理; 测试补充 multica_issue_create monkeypatch 隔离)
- Task 4 回归/SKILL.md/归档: --test 自检 OK + fresh 验证 8 项 PASS;
  提交 dbbb41b 已推送 origin/master
