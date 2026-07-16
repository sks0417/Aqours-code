# Inventory reservation consistency

Customers have reported inventory drift when reservation requests are retried,
when one item in a multi-item reservation is unavailable, and when an order is
canceled more than once.

Investigate the service and fix the consistency problems according to the
reservation, state-transition, and idempotency contracts in `README.md`.
Preserve the public API and documented exception behavior. Do not modify the
README, project configuration, or tests. Run the test suite before finishing.
