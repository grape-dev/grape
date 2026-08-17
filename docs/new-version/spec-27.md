# Spec: Issue 分诊前有效性检查

## Objective
在 Issue 分类打标签之前增加前置检查：判断 Issue 是否有效，避免无效 Issue 进入分诊流程。

## Success Criteria
1. 在分类前检查 master 是否已实现该功能
2. 检查是否与现有 Issue 重复
3. 检查描述是否清晰（正文长度、关键信息）
4. 无效 Issue 直接关闭并给出理由

## Implementation Plan
修改 `skills/issue/SKILL.md`，在"分类与打标签"前增加"有效性检查"步骤。
