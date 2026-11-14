# Observability and Operations

A system that cannot be observed cannot be operated, and cannot be audited for performance with any honesty. Where instrumentation is absent, performance findings can only ever be labelled `plausible`, and that limitation belongs in the report.

## The four signals

Latency, traffic, errors, saturation. Check each is actually collected rather than merely available in principle.

Beyond those, check: request rate per endpoint, error rate by class (client error versus server error, since a spike in 4xx is a different incident from a spike in 5xx), database query performance, connection pool usage, queue depth and age of oldest message, worker throughput and failure rate, cache hit ratio, external API latency and error rate, and resource exhaustion signals.

Queue depth and connection pool usage deserve particular attention, because both are leading indicators. By the time latency rises, the pool has usually been saturated for some time.

## Logs

Structured logs, machine parseable, with a consistent schema. Free-text logs are not searchable at incident time, which is the only time they matter.

Every log line for a request should carry a correlation ID, and that ID should propagate into background jobs, queue messages and outbound calls. Without propagation, a failure in a worker cannot be connected to the user action that caused it.

Log levels should be meaningful and configurable without a redeploy. Check for the two common defects: logging at debug level in production, which costs money and buries signal, and logging nothing on the error path except a generic message that omits the actual exception.

See `references/security.md` for what must never appear in logs. Check the error path specifically, since full request dumps on exception are the usual leak.

Distinguish audit logs from debug logs. Audit events (authentication, authorization denial, privilege change, data export, admin action, secret access) need retention and integrity guarantees that debug logs do not.

## Tracing

Distributed tracing is valuable where a request crosses process boundaries. For a single-process application it is often not worth the operational cost, and recommending it there is over-architecture. Judge by topology.

Where tracing exists, check that context actually propagates across HTTP calls, queue boundaries and database spans, since a trace that stops at the first async boundary answers no useful question.

## Error tracking

Check that unhandled exceptions reach a system a human sees, that errors are grouped rather than delivered one alert per occurrence, that release versions are attached so a regression can be tied to a deploy, and that personal data is scrubbed before it reaches a third-party error service, which is both a privacy and a contractual concern.

## Alerting

`A09:2025` covers alerting, not only logging. A metric nobody alerts on detects nothing.

Alerts should fire on symptoms the user feels rather than on causes. High CPU is not an incident, and a failing checkout is. Check for alert fatigue: an alert that fires daily and is routinely ignored is worse than no alert, because it trains the response.

Alert thresholds are numbers under Rule 1. Derive them from SLOs and error budgets. Where no SLO exists, that gap is the finding, and an invented threshold would paper over it.

## Deployment and operations

Check build, test, release, deployment, migration, rollback, configuration and environment promotion.

Verify deployments are reproducible from source, that no production change depends on undocumented manual action, that rollback is genuinely possible including for database changes, and that environments are consistent enough that testing in staging predicts production behaviour.

Ask what happens during a deploy: whether old and new versions run simultaneously, and whether the database schema and the message formats are compatible with both. This is the moment most well-tested systems break.

## Runbooks

For each realistic failure mode, check whether a written procedure exists that someone who did not build the system could follow. Recovery knowledge held only by the author is a single point of failure that no amount of redundancy addresses.

Note the recursive dependency: a runbook stored only in the system being recovered is unavailable exactly when needed.

## Cost

Cost is an operational risk, not only a finance concern. Check for unbounded growth in log volume, metric cardinality, object storage, egress, and metered API calls in retry loops.

High metric cardinality is the common surprise: a label containing a user ID or a request ID multiplies series count and can cost more than the application it monitors. Cost figures themselves come from the provider's current pricing for the actual plan and region, and are numbers under Rule 1.
