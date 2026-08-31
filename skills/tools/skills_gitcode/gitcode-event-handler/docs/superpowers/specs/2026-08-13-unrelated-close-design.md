# 设计文档：完全无关 issue 质量不达标直接关闭（不追问不入库）

日期: 2026-08-13
状态: 已批准（用户确认"不需要入库"）
目标脚本: /root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py
触发案例: issue #125（"上海天气很好"灌水，lccode）

## 问题现象

#125（标题/正文"上海天气很好"）最终被 2.1.4 无关关闭，但处理链有两条多余副作用：
1. **先收到质量追问**："需求信息不完整: 实质描述仅 6 字"——创建时 body 6 字，
   通用 codeowner 分支的质量检查失败 → need-info 追问（走不到后面的 2.1.4）
2. **错误入库**：need-info 路径写入 requirement(id=37, status=pending) + flow

用户确认：无关 issue 不需要入库，直接关闭即可。

## 根因

质量检查在三处先于"无关判定"执行，无关内容描述短时被质量检查拦截：
- 通用 codeowner 分支（#244 引入的质量检查，评论文案"以便定位处理"）
- 主路径质量检查
- handle_issue_update 质量检查

## 改动

新增 `_close_unrelated_issue(repo, number, req_id=None)`：
- 打 invalid 标签 + 评论"与华为云无关" + 关闭（幂等 marker，与 2.1.4 一致）
- req_id 非 None 时删除 flow+requirement（update 路径清理旧记录）

三处质量检查失败分支加前置判断：
`huawei_related_level()=="none" 且 not has_optimization_intent` → 直接关闭
- 通用 codeowner 分支（4778 段）
- 主路径质量检查（4861 段）
- handle_issue_update（5139 段）

描述短但相关的需求（如"查询华为云ecs列表"22 字）→ 相关性 strong → 仍正常追问。

## 测试

test_unrelated_close.py 3 用例：
- codeowner 分支：无关+短描述 → invalid_unrelated 且不入库（db_insert_requirement 断言不调用）
- 主路径质量检查：is_skill_task=True + 无关+短描述 → invalid_unrelated
- update 路径：无关+短描述+已有记录 → invalid_unrelated 且清理 DB
全量 112 passed

## 清理

#125 的 DB 残留（requirement 37 + flow）已手动删除，验证 0 残留。
