BUSINESS_LOGIC_PROMPT = """You are an expert in analyzing Huawei Cloud skills and SDKs. 

Given the following skill information and its API interfaces, provide a detailed analysis of:
1. The business logic and purpose of this skill
2. The business semantics of each API interface used
3. Whether the skill's implementation approach is optimal

Skill Information:
- Name: {skill_name}
- Implementation Type: {impl_type}
- Business Goal: {business_goal}

SDK Evidence:
{sdk_evidence}

CLI Evidence:
{cli_evidence}

API Interfaces Used:
{api_interfaces}

Please respond in Chinese, structured as:
## 业务逻辑分析
[analysis content]

## 接口业务语义
[interface semantics]

## 实现方式评估
[evaluation]
"""

ALTERNATIVE_FEASIBILITY_PROMPT = """You are an expert in Huawei Cloud CLI and SDK APIs.

Evaluate the feasibility of using the following CLI command as an alternative to the unsupported API interface.

Unsupported API:
- Name: {api_name}
- Service: {service_name}
- Business Role: {business_role}

Proposed Alternative CLI Command:
- Command: {alternative_cli}
- Current Feasibility Assessment: {current_feasibility}

Please evaluate:
1. Can this CLI command fully replace the API interface in the business scenario?
2. What are the functional differences?
3. Is this alternative acceptable for the skill's business goal?

Please respond in Chinese, structured as:
## 可行性评估
[feasibility assessment: 完全可行/部分可行/不可行]
## 功能差异
[differences]
## 建议
[recommendation]
"""

SUGGEST_ALTERNATIVES_PROMPT = """You are an expert in Huawei Cloud CLI commands.

For the following unsupported API interface, suggest CLI command combinations that could achieve similar functionality.

Unsupported API:
- Name: {api_name}
- Service: {service_name}
- Business Role: {business_role}

Available CLI Commands for this service:
{available_commands}

Please suggest up to 3 alternative approaches using CLI commands, and explain how they can partially or fully achieve the same business goal.

Please respond in Chinese, structured as:
## 替代方案1
- CLI命令: [command]
- 可行性: [完全替代/部分替代/不可替代]
- 说明: [explanation]

## 替代方案2
[if applicable]

## 替代方案3
[if applicable]
"""

ANALYSIS_SUMMARY_PROMPT = """You are an expert in Huawei Cloud skills analysis.

Based on the following analysis results, provide a comprehensive summary and improvement suggestions.

Skill: {skill_name}
Implementation Type: {impl_type}
Business Goal: {business_goal}

SDK API Interfaces Used: {api_count}
CLI Coverage Rate: {coverage_rate}%
CLI Gaps: {gap_count}
Critical Gaps: {critical_gap_count}

Gap Details:
{gap_details}

Alternatives Found: {alternative_count}

Please provide:
1. Overall assessment of this skill's CLI compatibility
2. Key risks and recommendations
3. Suggested improvements

Please respond in Chinese, structured as:
## 总体评估
[overall assessment]
## 关键风险
[key risks]
## 改进建议
[improvement suggestions]
"""