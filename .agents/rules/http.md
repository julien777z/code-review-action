---
alwaysApply: true
---

# HTTP Rules

## HTTP Clients

- Prefer the repository's shared HTTP helper or client abstraction over spawning ad-hoc clients deep in application code.
- If the project already centralizes retries, auth headers, or response parsing, reuse that shared layer instead of reimplementing it per call site.
- Keep raw `response.json()` parsing at the boundary layer; do not scatter transport parsing logic across core business logic.

## Internal Services

- For requests to internal services, prefer a generated or shared typed client when one exists.
- If no shared client exists for a service boundary that is used repeatedly, create or generate one instead of hand-rolling the same HTTP integration in multiple places.
