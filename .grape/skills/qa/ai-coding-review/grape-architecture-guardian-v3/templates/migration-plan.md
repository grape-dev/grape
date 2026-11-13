# Migration Plan: {system}

**Based on**: {audit reference and date}
**Constraint**: {acceptable downtime, freeze windows, team capacity, budget}

---

## Principle

Controlled, incremental migration. Do not recommend a full rewrite unless the evidence genuinely supports it, which is rare: a rewrite discards working behaviour that nobody has documented, and the undocumented behaviour is usually what the business depends on.

Prefer, where they fit: strangler migration (route traffic progressively from old to new, keeping both alive), adapter layers to isolate a component before replacing it, and modular decoupling inside the existing deployment before considering separate services.

Every step must leave the system in a working, deployable, revertible state. A migration that only works once all steps are complete has no rollback and is a rewrite wearing a plan's clothing.

## Ordering

Priority is not a matter of taste:

1. Active security risks
2. Data loss risks
3. Tenant isolation failures
4. Authentication and authorization defects
5. Production outage risks
6. Non-scalable single points
7. Data integrity problems

then

8. Performance
9. Observability
10. Deployment and operations
11. Maintainability

Instrumentation is a frequent exception worth pulling forward: where a later step needs measurement to size it, the measurement must be in place before that step, not after.

---

## Steps

### Step {n}: {title}

| Field | Content |
|---|---|
| **Addresses** | {finding IDs from the audit} |
| **Depends on** | {previous steps that must complete first, or "none"} |
| **Risk** | Low / Medium / High, with the specific risk named |
| **Reversible** | Yes / No. If no, state what makes it irreversible and what the pre-step backup is |
| **Downtime** | None / {duration} / requires window |
| **Effort** | {rough size, stated as a range} |

**Change**
{What is done, concretely.}

**Verification**
{How this step is proven to have worked before the next begins. Match the type: security test, query plan, load test, concurrency test, restore test, migration plus rollback test.}

**Rollback**
{The exact procedure, and its limit. For a database change, state whether rollback is still possible after the application has written new-format data, since that is usually the point of no return.}

---

## Sequence

```mermaid
graph LR
```

{Show dependencies between steps and which may run in parallel.}

## Numbers required before starting

| Step | Number needed | Currently | How to obtain |
|---|---|---|---|

{Any step whose implementation requires a value nobody has sourced. Obtain these before that step begins rather than inventing them mid-implementation. This table is what prevents the migration from quietly reintroducing magic numbers.}

## Out of scope

{What this plan deliberately does not address, and why. Preventing scope creep is part of the plan, and a stated exclusion is easier to revisit than a silent omission.}
