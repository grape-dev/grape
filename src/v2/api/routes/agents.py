"""Agent 路由：Agent 详情、事件历史、配置"""
from fastapi import APIRouter, HTTPException, Depends
from src.user import db as user_db
from src.v2.api.deps import get_current_user

router = APIRouter(prefix="/api/v2/agents", tags=["agents"])

AGENT_META = {
    "vod": {
        "name": "VOD Agent", "subtitle": "Voice of Developer",
        "description": "从开发者声音中收集反馈, 聚类分析共性, 提炼价值, 自动创建 Issue",
        "color": "#ec4899", "status": "active",
        "metrics": [
            {"value": "47", "label": "Signals/Week"},
            {"value": "8", "label": "Issues Created"},
            {"value": "92%", "label": "Signal Quality"},
        ],
        "events": [
            {"time": "12:05", "text": "Signal: CI pipeline too slow → created Issue #345"},
            {"time": "10:30", "text": "Analysis: 3 signals cluster around test flakiness"},
            {"time": "08:00", "text": "Weekly report: 8 key signals, 2 trending up"},
            {"time": "Yesterday", "text": "Collected 12 feedback from PR comments"},
        ],
        "skill": {"default": "default", "customs": []},
    },
    "issue": {
        "name": "Issue Agent", "subtitle": "Issue Manager",
        "description": "处理 Issue, 分类打标签, 提炼为可开发需求, 指派负责人, 触发 Coding Agent",
        "color": "#ef4444", "status": "active",
        "metrics": [
            {"value": "3", "label": "Active"},
            {"value": "12", "label": "Resolved/Week"},
            {"value": "2m", "label": "Avg Triage"},
        ],
        "events": [
            {"time": "13:50", "text": "Assigned Issue #342 (bug: login timeout) to @alice"},
            {"time": "13:22", "text": "A2A triggered Coding Agent for Issue #338"},
            {"time": "11:05", "text": "Labelled Issue #335 as enhancement"},
            {"time": "09:30", "text": "Auto-closed Issue #329 (fixed in PR #1270)"},
        ],
        "skill": {"default": "skills/issue/", "customs": ["custom-issue-skill"]},
    },
    "coding": {
        "name": "Coding Agent", "subtitle": "Developer",
        "description": "分析需求 → 设计方案 → 编码实现 → 自我审查 → 提交 Pull Request",
        "color": "#3b82f6", "status": "active",
        "metrics": [
            {"value": "3", "label": "Today"},
            {"value": "45s", "label": "Avg Time"},
            {"value": "98%", "label": "First-Pass"},
        ],
        "events": [
            {"time": "14:15", "text": "Submitted PR #1278 for Issue #342"},
            {"time": "12:30", "text": "Designed solution for Issue #342: add session refresh"},
            {"time": "11:00", "text": "Implemented webhook retry logic (PR #1275)"},
            {"time": "09:45", "text": "Self-review completed for PR #1275"},
        ],
        "skill": {"default": "implementation", "customs": []},
    },
    "qa": {
        "name": "QA Agent", "subtitle": "Quality Assurance",
        "description": "Code Review, 自动化测试, 功能验证, 安全检查, 确保代码质量",
        "color": "#8b5cf6", "status": "active",
        "metrics": [
            {"value": "4", "label": "Today"},
            {"value": "92%", "label": "Pass Rate"},
            {"value": "3", "label": "Issues Caught"},
        ],
        "events": [
            {"time": "14:30", "text": "Approved PR #1278 (1 minor suggestion)"},
            {"time": "13:15", "text": "Review for PR #1275: 2 issues found"},
            {"time": "12:00", "text": "Auto-test suite passed: 156 tests OK"},
            {"time": "10:30", "text": "Security scan OK for PR #1272"},
        ],
        "skill": {"default": "skills/pr/", "customs": []},
    },
    "sre": {
        "name": "SRE Agent", "subtitle": "DevOps",
        "description": "构建部署, 发布管理, 监控运维, 故障排查, 合规检查",
        "color": "#f97316", "status": "active",
        "metrics": [
            {"value": "3", "label": "Deploys/Week"},
            {"value": "99.9%", "label": "Uptime"},
            {"value": "12s", "label": "MTTR"},
        ],
        "events": [
            {"time": "13:22", "text": "Deployed v2.3.1 to production · success"},
            {"time": "13:18", "text": "Build #482 completed"},
            {"time": "12:05", "text": "Auto-resolved incident INC-023"},
            {"time": "10:15", "text": "Compliance scan OK for backend-service"},
        ],
        "skill": {"default": "deploy-guard", "customs": []},
    },
}


@router.get("/{agent_name}/{owner}/{repo}")
async def get_agent_detail(agent_name: str, owner: str, repo: str, user: dict = Depends(get_current_user)):
    """获取 Agent 详情（指标、事件、Skill）"""
    if agent_name not in AGENT_META:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "Agent 不存在"})
    return AGENT_META[agent_name]
