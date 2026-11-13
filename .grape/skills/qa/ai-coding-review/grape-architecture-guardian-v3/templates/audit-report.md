# Production Architecture Audit

**System**: {name}
**Repository state**: {commit SHA or date}
**Audit date**: {date}
**Scope**: {what was examined, and explicitly what was not}
**Evidence base**: {repository / repository plus configuration / plus production metrics / plus load test}

The evidence base line governs how strongly anything below may be claimed. An audit from source alone cannot verify runtime behaviour, and that limit belongs at the top rather than buried in a caveat.

---

## Executive Assessment

{Three to six sentences. Current state, the most serious issue, and whether the system is fit for its intended production use. No marketing language, no reassurance, no adjectives that cannot be checked.}

---

## Verified Architecture

{The architecture that actually exists, established from code and configuration. Where intent and reality differ, describe reality and note the difference.}

```mermaid
graph TD
```

{Mark trust boundaries. Mark where sensitive data lives and crosses.}

**Stack**

| Layer | Technology | Version | Source |
|---|---|---|---|

---

## Architecture Gaps

{Structural problems that are not single findings: missing layers, absent boundaries, decisions never made. Each with its consequence.}

---

## Findings

Ordered by severity within each section. Use `templates/finding.md` for each.

### Security Findings
### Scalability Findings
### Performance Findings
### Database Findings
### Reliability Findings
### DevSecOps and Supply Chain Findings
### Observability Findings

---

## Numerical Recommendations Register

Every number this audit proposes, with its source. Use `templates/numerical-recommendation.md`.

| Parameter | Proposed | Kind | Source | Confidence |
|---|---|---|---|---|

---

## Unsupported Numbers Found

Every magic number already present in the project. Output of `scripts/magic_number_audit.py` plus manual review.

| Value | Location | Origin | Documented | Measured | Still valid |
|---|---|---|---|---|---|

{For each: where did it come from, is there any documentation, was it ever measured, is it still correct for the current deployment. "Unknown" is a legitimate and important answer, and a value nobody can account for is itself the finding.}

---

## Missing Metrics

{Which measurements are absent that would be needed to make the architectural decisions this audit could not close. Be specific enough to be actionable: not "more monitoring" but "p99 latency of the payment provider call, to size its timeout".}

---

## Target Architecture

{The recommended architecture. Justify every component that does not already exist, including why the simpler option was rejected. Where the current architecture is adequate, say so rather than proposing change for its own sake.}

```mermaid
graph TD
```

---

## Migration Plan

{Prioritised, low-risk sequence. Use `templates/migration-plan.md`. Security and data loss risks first, then availability, then performance, then maintainability. State dependencies between steps.}

---

## Verification Plan

{For each remediation, how it will be objectively proven to work. Match verification to finding type.}

| Finding | Remediation | Verification method | Pass criterion |
|---|---|---|---|

---

## Remaining Unknowns

{Everything that could not be reliably verified, and what would be needed to resolve each. This section is mandatory and must not be empty in a source-only audit. An audit that claims to have verified everything has not been honest about its evidence base.}

| Unknown | Why unresolved | What would resolve it |
|---|---|---|
