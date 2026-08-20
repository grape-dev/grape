# Service Selection Procedure

Use when the user asks which service to use or compares services.

## Decision Flow

1. Classify workload: compute, storage, database, network, serverless, AI, messaging, security, observability
2. Check Service Map in huaweicloud-core SKILL.md
3. If multiple services match, ask clarifying questions:
   - Expected scale (small demo vs production)
   - Data model (relational vs document vs key-value)
   - Latency requirements
   - Budget constraints
4. Present top 1-2 recommendations with rationale
5. Hand off to the selected service skill

## Example: I need a database

Ask:
- What type of data? Structured (tables) or flexible (documents)?
- What scale? Small app or enterprise?
- Do you need SQL queries?

Route based on answers:
- SQL + standard scale -> huawei-rds
- SQL + massive scale / distributed -> huawei-gaussdb
- Document model (MongoDB-compatible) -> huawei-dds-dcs
- Cache / key-value -> huawei-dds-dcs (DCS)