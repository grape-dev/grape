# Reliability and Failure Modes

## Every external dependency

For each one, check: timeout, retry policy, backoff, jitter, circuit breaking, bulkheading, fallback behaviour, idempotency, behaviour on total failure, behaviour on partial failure, and timeout propagation.

**A call without an explicit timeout is a finding.** Library defaults are frequently very long or absent entirely, which converts a slow dependency into an exhausted connection pool and then into an outage of a system that did not fail. Verify the timeout in the actual client configuration rather than assuming the library has a sensible default.

**Timeout propagation.** If the inbound request has 30 seconds and the handler makes three sequential calls each allowed 30 seconds, the budget is incoherent. Timeouts should descend through the call chain so that inner calls cannot outlive the outer budget.

**Retry storms.** Retries multiply load on a dependency that is already failing. Required protections: a bounded attempt count, exponential backoff, jitter to prevent synchronised retry waves, and a circuit breaker so a hard-down dependency stops receiving traffic. Retries at multiple layers compound, so check whether the HTTP client, the framework and the queue each retry the same operation.

Retry only what is safe to repeat. See `references/async-queues.md`.

## Numbers here are Rule 1 numbers

Timeout values, retry counts, backoff base and cap, jitter range, circuit breaker error thresholds, open duration and half-open probe counts, and health check intervals are all numbers requiring a source.

Derive them from the provider's documented latency or SLA, from measured latency distribution in this system, and from the request budget. Where the data is absent, define the parameter and state the measurement needed. The measurement needed for a timeout is the p99 latency of that dependency, which is a concrete and cheap thing to ask for.

## Failure mode analysis

For every central component, answer: what happens when this fails?

Cover at minimum: application server, database, cache, queue, worker, CDN, object storage, DNS, authentication provider, payment provider, email provider, and each external API.

Document each as:

| Field | Content |
|---|---|
| Failure mode | What breaks, and in which way (down, slow, partial, corrupt) |
| Impact | What the user experiences, and what data is at risk |
| Detection | How this is noticed, and how quickly |
| Containment | What limits the blast radius |
| Recovery | How service is restored, and by whom |

Slow is a distinct failure mode from down, and usually worse. A dependency that is down fails fast and trips a breaker. A dependency that is slow holds connections, fills pools and propagates saturation upstream while every health check still passes.

Partial failure deserves the same treatment: a dependency that succeeds for some requests and fails for others defeats naive circuit breakers and produces inconsistent state.

## Graceful degradation

Determine which features can fail without taking the system down. A recommendation widget, an analytics call or a non-essential enrichment should not be able to fail a checkout. Check whether optional dependencies are actually optional in code, or merely optional in intent.

## Health checks

Distinguish liveness (is the process alive, should it be restarted) from readiness (can it serve traffic right now).

Common defects: a readiness probe that returns `200` unconditionally and therefore reports capacity that does not exist, and a liveness probe that checks the database and therefore restarts every application instance during a database blip, converting a degraded system into a crash loop.

Probe intervals, timeouts and failure thresholds are numbers under Rule 1, and the orchestrator's documentation supplies defaults worth citing.

## Backup, restore, disaster recovery

Check: backups exist, are automated, are encrypted, are stored separately from the primary, point-in-time recovery availability on the actual plan, whether a restore has ever been tested, whether recovery credentials are reachable during an outage of the primary system, and whether a runbook exists.

**RPO and RTO are business requirements and must never be invented.** When unknown, state `INSUFFICIENT DATA TO VERIFY`, mark it as a business decision, and state what the current configuration implies. The implication is verifiable even when the target is not: a nightly backup means up to 24 hours of data loss, and that sentence is a fact about the configuration rather than a guess about the requirement.

## SLOs

Do not invent SLOs. Where none are defined, that absence is itself an architecture gap worth reporting, because without an SLO there is no basis for a timeout, an autoscaling threshold, an alert threshold or an error budget. Request the business and product requirement rather than substituting a plausible target.

Where SLOs do exist, check that they are measured against something the user experiences rather than against infrastructure metrics that happen to be easy to collect.
