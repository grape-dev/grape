# Asynchronous Processing and Queues

## The rule this replaces

"Anything over 200 ms must be asynchronous" is not permissible. The number has no basis without this project's SLOs and infrastructure, and applying it turns trivially fast synchronous work into a distributed system with a queue, a worker, a result store and four new failure modes.

## Decision inputs

Decide synchronous versus asynchronous from:

- The request SLO, where one exists
- The platform's hard request timeout, which is a documented vendor limit and often the real constraint
- Client behaviour, including the client's own timeout and whether a user is waiting
- Expected execution duration and, more importantly, its variance
- Whether retries are needed
- Whether an external API is in the path, since its latency is not controlled
- CPU intensity versus I/O intensity
- Batch character
- Whether the result is needed to render the response
- Idempotency and fault tolerance requirements

The platform timeout deserves emphasis. Serverless and edge platforms impose maximum execution durations, and a request exceeding one is terminated regardless of how well written it is. That limit comes from the provider's documentation for the actual plan, and it is a legitimate cited number.

## Typical candidates

Email sending, image processing, video processing, PDF generation, large imports, large exports, AI jobs, report generation, webhook fan-out, long-running aggregation, re-indexing, notifications and large file operations.

These are candidates, not conclusions. A small report generated in tens of milliseconds does not need a queue because reports appear on this list.

## Design when async is warranted

```
API  ->  Queue / Broker  ->  Worker  ->  Result store / Domain state
```

Check every element below.

**Delivery semantics.** Never assume exactly-once delivery exists. Read the actual broker's documented semantics. Most systems provide at-least-once, which makes consumer idempotency mandatory rather than optional. A system that claims exactly-once usually means it within a narrow transactional boundary that does not extend to the side effects the worker performs.

**Idempotency.** Every consumer must tolerate receiving the same message twice. Enforce with a processed-message table, a unique constraint on the business invariant, or a natural upsert. "The broker will not deliver twice" is not a design.

**Retry safety.** Retry only operations that are idempotent or protected by an idempotency key. Retrying a payment capture without protection charges twice.

**Poison messages.** A message that always fails must not block the queue or retry forever. Check for a maximum attempt count and a dead letter destination, and check that something actually looks at the dead letter queue. An unmonitored DLQ is silent data loss.

**Visibility and lease semantics.** Understand what happens when a worker takes longer than the visibility timeout: the message becomes visible again and a second worker starts the same job while the first still runs. Long jobs need lease extension or a lease long enough for worst-case duration.

**Backpressure and queue growth.** What happens when producers outpace consumers. Queue depth must be monitored, because a growing queue is the leading indicator of every async incident.

**Crash recovery.** In-flight work when a worker dies. Whether the job is redelivered, and whether partial side effects have already occurred.

**Graceful shutdown.** Stop accepting new messages, finish or release current work, release leases, then exit. Verify the deployment actually allows the time this needs.

**Cancellation.** Whether an enqueued or running job can be cancelled, if the product requires it.

**Observability.** Correlation IDs propagated from the originating request through the queue into the worker, and trace context propagation. Without it, a failed background job cannot be tied back to the user action that caused it.

**Transactional consistency.** The classic defect: a database transaction commits and then the message is published, or the message is published and then the transaction rolls back. Either way the two diverge. Where this matters, use the outbox pattern: write the message into the same transaction as the state change, and relay it separately. Introduce the outbox where the divergence has real consequences, not everywhere.

## Numbers in this area

Retry counts, backoff base and cap, jitter, visibility timeouts, lease durations, maximum queue depth, worker concurrency, prefetch counts and dead letter thresholds are all numbers under Rule 1.

Derive them from broker documentation, measured job duration distribution, downstream capacity and the failure budget. Where the data is missing, leave them as named parameters with the required measurement stated. Job duration in particular must come from measurement, since the distribution's tail is what sets the visibility timeout, and the tail is exactly what an estimate gets wrong.

## Worker sizing

Worker count and concurrency depend on job profile. CPU-bound work saturates at roughly the available cores, while I/O-bound work can run far higher concurrency. Both are bounded by downstream capacity, especially database connections, which is where a worker fleet usually breaks the database rather than the queue.

Compute worker pool sizing together with the database connection pool. They draw on the same limit. See `references/databases.md`.
