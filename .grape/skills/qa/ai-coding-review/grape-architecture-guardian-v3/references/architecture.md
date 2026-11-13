# Architecture

## Logical baseline

Presentation layer, then application and domain layer, then data and infrastructure layer. Dependencies point inward. Business rules do not import the HTTP framework, and the domain does not know which database it is stored in.

The physical deployment topology is a separate decision and may be a single process, several processes, or many services. Conflating the two is the origin of most unjustified microservice architectures: a clean layering need is answered with a network boundary, which adds partial failure, serialization, versioning and operational cost to solve a problem that a module boundary solves for free.

Common violations worth reporting:

- Business logic inside route handlers or controllers, making it untestable without HTTP
- Database queries in the presentation layer
- ORM entities used as API response types, which couples the wire contract to the schema and leaks fields
- Framework types threaded through the domain
- Circular dependencies between modules
- A "utils" or "helpers" module that has become an undeclared second domain layer

## Service boundaries

A service boundary is justified by domain boundaries, differing scaling profiles, security boundaries, differing deployment requirements, or team ownership. It is not justified by fashion, by file count, or by a desire to look professional.

When reviewing existing microservices, check whether the boundaries hold up: services that always deploy together, services that share a database, chatty synchronous call chains, and distributed transactions across services are all signals that the boundary is in the wrong place. A distributed monolith has the costs of both models and the benefits of neither.

A well structured modular monolith is a legitimate target architecture and should be recommended where nothing demands separation. Modules with explicit interfaces inside one deployable unit can be extracted later if a real need appears. The reverse migration is far more expensive.

## Statelessness

See `references/scalability.md` for the full checklist. The principle: HTTP application servers should be horizontally scalable, so persistent state belongs in backing services.

## Configuration

Configuration belongs in the environment, not in the build. Check that the same artifact can be promoted across environments, that no environment-specific value is baked into the image, that configuration is validated at startup rather than failing at first use, and that a missing required value fails loudly and immediately.

Every configuration value that is a number falls under Rule 1.

## API design

Check: coherent resource modelling, schema contracts, input validation, output validation, versioning strategy, backward compatibility, a consistent error model, idempotency for unsafe operations, pagination, filtering, sorting, rate limiting, request and response size limits, timeouts, retry semantics, a complete API inventory, deprecated endpoints still reachable, undocumented endpoints, and debug or introspection endpoints exposed in production.

`API9:2023` Improper Inventory Management is frequently found in AI-generated codebases, because routes accumulate faster than documentation. An endpoint nobody remembers is an endpoint nobody secures.

HTTP status codes follow HTTP semantics rather than habit. `202 Accepted` is correct for work accepted for later processing, and requires a way for the client to learn the outcome. Returning `200` with an error body, or `500` for a validation failure, are both defects worth reporting.

Pagination limits, request size limits and rate limits are numbers under Rule 1. Derive them from payload sizes, downstream capacity and the deployment platform's own limits, which are usually documented and often lower than the application's.

## Idempotency

Any operation that can be retried by a client, a queue, a webhook sender or a load balancer needs to be safe to execute more than once. Check for an idempotency key on payment and order creation, unique constraints backing the invariant in the database rather than only in application code, and upsert semantics where appropriate.

"The client will not retry" is not a design. Networks retry.

## Deployment and change

Analyse build, test, release, deployment, database migration, rollback, configuration and environment promotion.

Deployments must be reproducible. Production must not depend on undocumented manual steps. Check whether a rollback is actually possible, which for database migrations usually means the migration must be backward compatible with the previous application version, deployed in the expand, migrate, contract order rather than as a single breaking change.

## Over-architecture

Actively prevent: microservices, Kafka, Kubernetes, Redis, read replicas, sharding, event sourcing and CQRS introduced without a verified need.

Every additional infrastructure component increases complexity, failure surface, security surface, operational effort and cost. State that cost when declining to recommend one, and name what evidence would change the answer.

**The simplest architecture that satisfies the verified requirements.**
