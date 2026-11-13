# Database Review

## Schema

Check primary keys, foreign keys, constraints (`NOT NULL`, `UNIQUE`, `CHECK`), normalization, and any denormalization that exists.

Denormalization is legitimate when it is deliberate and its consistency cost is handled. Report it as a finding only when the duplicate has no synchronization mechanism, since that is a data integrity defect rather than a design choice.

Invariants belong in the database where the database can express them. Application-only uniqueness fails under concurrency: two requests both check, both find nothing, both insert. A unique index is the fix, not a longer transaction in application code.

Check for missing foreign keys specifically. AI-generated schemas frequently omit them, which permits orphaned rows and silently breaks cascade semantics.

## Queries

Look for N+1 query patterns, which is the single most common performance defect in ORM-based code: a loop that lazily loads a relation, a serializer that resolves associations per item, a GraphQL resolver without batching.

Also check full table scans on user-facing paths, `SELECT *` where the row is wide, missing indexes on foreign keys and on columns used in `WHERE`, `JOIN` and `ORDER BY`, indexes that exist but cannot be used because of a function or type cast on the column, over-indexing that slows writes, and unbounded result sets with no pagination.

Verify with an actual query plan (`EXPLAIN ANALYZE` or the database's equivalent) rather than by reading the query. A plan is evidence, a reading is a hypothesis, and the difference matters when reporting.

## Transactions

Check transaction boundaries, isolation level and whether the code's assumptions match it, lock scope and duration, deadlock potential from inconsistent lock ordering, and long transactions that hold locks across external calls.

Calling an external HTTP API inside an open database transaction is a defect worth reporting on its own: the transaction is held open for the remote system's latency and failure modes.

Check read-modify-write races. If code reads a value, computes, and writes it back without optimistic locking, a version column, or an atomic operation, concurrent requests lose updates.

## Connection management

Connection pool size is a calculation, never a copied number. It is derived from:

- The database's own maximum connection limit, from the provider's documentation for the actual plan
- Connections reserved for administration, monitoring, migrations and backups
- The number of application instances, including the maximum during a rolling deploy when old and new run together
- The number of worker processes and their pools
- Any connection pooler in the path (PgBouncer, Supavisor, RDS Proxy) and its pooling mode, since transaction mode changes what the application may rely on
- Expected concurrency per instance

The classic production incident is a pool size that works with two instances and exhausts the database during a deploy at four. Compute the worst case, not the steady state.

Serverless and edge runtimes invert this: each invocation may open its own connection, so a pooler is usually mandatory rather than optional. Check the runtime before recommending a pool size at all.

Record the result with `templates/numerical-recommendation.md`, showing the arithmetic and citing the database limit source. If any input is unknown, the output is `INSUFFICIENT DATA TO VERIFY` plus the specific missing input.

## Migrations

Check that migrations are versioned, ordered, reproducible, and applied automatically rather than by hand.

For zero or low downtime, verify the expand, migrate, contract sequence: add the new structure, backfill, write to both, switch reads, then remove the old structure. Each step must be deployable independently and compatible with the application version before and after it.

Report as defects: a migration that adds a `NOT NULL` column without a default to a populated table, a rename done as drop and add, a blocking index build on a large table where the database supports a concurrent build, a backfill executed as one statement over a large table, and any migration with no tested rollback path.

## Data lifecycle

Check retention, deletion, archival and whether deletion actually removes data from backups, logs, caches, search indexes and analytics.

Retention periods are numbers under Rule 1 and are usually business or regulatory requirements. Do not invent them.

## Backup and restore

Check whether backups exist, are automated, are encrypted, are stored separately from the primary, and whether point-in-time recovery is available on the actual plan in use.

The decisive question is whether a **restore has ever been tested**. An untested backup is an assumption, not a control. Report it that way.

Also check that the credentials and runbook needed to restore are reachable when the primary system is down. A recovery procedure stored only in the system being recovered is not a recovery procedure.

**RPO and RTO are business requirements.** Never invent them. When they are unknown, state `INSUFFICIENT DATA TO VERIFY`, mark it as a business decision to be made, and describe what the current setup implies in practice, since "the current configuration means up to 24 hours of data loss" is a verifiable statement even when the target is unknown.

## Replicas, partitioning, sharding

Do not introduce prophylactically. Each requires demonstrated need.

Read replicas add replication lag, which turns a read-after-write into a correctness bug unless the code routes those reads to the primary. Check for that routing before recommending replicas, and report it if replicas already exist without it.

Partitioning helps when queries and retention align with the partition key. Sharding is a last resort with a large permanent cost in application complexity, cross-shard queries and rebalancing.
