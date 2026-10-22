# Task: Issue #396 任务拆解

## 任务清单
1. [x] #384：community.py 三处错误字典改抛 `HTTPException(404)`，兜底链加 `except HTTPException: raise`；`toggle_like` 补帖子存在性校验
2. [x] #385：feedback.py PUT /status 不存在分支抛 404（detail 与 DELETE 对齐），兜底链加 `except HTTPException: raise`
3. [x] 新增 `tests/test_community_404.py`（404 分支 + 合法帖不回归）
4. [x] 新增 `tests/test_feedback_status.py`（404 分支 + 合法记录不回归 + 无效状态不变）
5. [x] 回归验证：相关测试文件全通过；基线既有失败（Knowledge/OpenAPI，环境相关）无新增
6. [x] 文档：spec.md / plan.md / task.md
7. [ ] 提交 feat/issue-396 分支并推送
8. [ ] 创建 PR（base=功能分支）并发 Issue 评论告知
9. [ ] A2A 通知 QA 审查

## 验证命令
```bash
python3 -m pytest tests/test_community_404.py tests/test_feedback_status.py tests/test_feedback_delete.py -v
```

## 说明
- AT-COMM-04 / AT-FBA-03 属外部原子测试集（本仓不含，见 docs/issue-386/spec.md 同款说明），以本仓新增 404 断言测试等效覆盖，验收时以外部原子集结果为准。
