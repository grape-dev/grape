# Scalability Review

The audit must answer one question concretely: **what happens technically when a second application instance starts?**

Everything else in this file exists to answer that question with evidence rather than optimism.

## Horizontal scaling blockers

Search the code for each of these. Each one is a reason a second instance breaks correctness rather than adding capacity.

| Blocker | What to grep for | Why it breaks |
|---|---|---|
| In-memory sessions | session middleware with a memory store, module-level session maps | User hits instance B, session is on instance A |
| Local file storage | `fs.write`, local upload directories, temp paths that outlive a request | File exists on one instance only |
| In-memory cache treated as shared | module-level `Map`, `dict`, LRU caches with correctness assumptions | Instances disagree, invalidation reaches one instance |
| Singleton state | module-level mutable objects, counters, registries | Diverges per instance |
| In-process locks | mutexes, semaphores, `synchronized` | Guards nothing across instances |
| In-process queues | arrays or lists used as work queues | Work lost on restart, duplicated across instances |
| Background jobs inside the web process | `setInterval`, `@scheduled` in the API process | Runs N times with N instances |
| Cron in the app container | scheduler running per replica | Duplicate execution |
| WebSocket state | connection maps, room membership in memory | Message reaches only the instance holding the socket |
| Sticky sessions | load balancer affinity config | Masks statelessness defects and blocks even rebalancing |
| Non-idempotent processing | handlers that assume exactly-once | Retry or duplicate delivery corrupts state |

Persistent state belongs in an appropriate backing service. Where a deviation is deliberate, it must be documented with its reason and its ceiling.

A local in-memory cache is legitimate when it is a pure performance optimisation, tolerates divergence between instances, and has no correctness assumption attached. Say that explicitly rather than flagging every `Map` as a defect.

## Cron and scheduled work

With more than one instance, a scheduler inside the application runs on every instance. Check for leader election, a distributed lock with a lease, a platform scheduler that invokes a single endpoint, or a dedicated single-replica worker. Absent all four, duplicate execution is not a risk, it is the current behaviour.

## Database as the real ceiling

Application instances are usually cheap to add. The database is usually what actually limits scale. Check connection limits, connection pooling behaviour, lock contention, hot rows, and write amplification before recommending more application replicas.

See `references/databases.md` for connection pool derivation, which is a Rule 1 calculation and never a copied number.

## Session and state strategy

If sessions move to an external store, that store becomes a availability dependency of every request. Check its failure behaviour: does an outage log everyone out, or does it fail the request entirely.

## Startup and shutdown

Autoscaling only works if instances start fast enough to matter and stop without losing work.

Check: startup time and what dominates it, readiness versus liveness probes and whether they actually reflect capacity, graceful shutdown, in-flight request draining, queue consumer lease release on shutdown, and connection draining at the load balancer.

An instance that reports ready before its dependencies are usable will receive traffic it cannot serve.

## External dependencies as scaling limits

Every third-party API has a rate limit, and it belongs to the account, not to the instance. Doubling instances does not double that budget. Check whether limits are respected globally rather than per process, and what happens when the limit is hit.

Provider limits must come from that provider's documentation, with the plan and region noted, because they differ by both.

## Load testing

If scalability is to be verified rather than argued, a reproducible load test plan is required. Do not invent user counts or requests per second, and do not present a fictional load as realistic.

Required inputs, which come from the user or from production data:

current usage, expected peak usage, growth assumptions, traffic pattern (steady, spiky, diurnal, campaign driven), request mix, read/write ratio, typical payload sizes, session behaviour, geographic distribution, and which external dependencies participate.

When these are unknown, produce a **parameterized** test plan: the scenarios, the ramp shape, the measurements to capture and the pass criteria, with the load figures left as named parameters. A parameterized plan is honest and immediately usable once the numbers exist. A plan with invented numbers is neither.

Measure and report: p50, p95, p99 latency per endpoint, error rate, throughput achieved, saturation of CPU, memory, connections and queue depth, and where the first bottleneck appeared. The first bottleneck is the finding. Everything past it is noise.

## Scaling patterns, and when they are not justified

| Pattern | Justified when | Do not introduce because |
|---|---|---|
| Read replicas | Measured read saturation, and the application tolerates replication lag | It sounds more scalable |
| Sharding | A single instance provably cannot hold the data or the write rate | Anticipated future growth with no data |
| Caching layer | Measured hit ratio and query cost justify it, staleness is tolerable | Every query "should" be cached |
| Queue and workers | Work is genuinely asynchronous by nature or exceeds the request budget | A blanket latency threshold |
| Microservices | Domain, scaling profile, security boundary, deployment or ownership demands separation | Perceived professionalism |
| Kubernetes | Existing operational need and the team to run it | It is the default in tutorials |

Each of these adds failure surface, security surface, operational load and cost. The recommendation is the simplest architecture that satisfies the verified requirements.
