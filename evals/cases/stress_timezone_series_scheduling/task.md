# Time-zone series scheduling correctness

Recurring appointments drift across daylight-saving transitions, adjacent
series are rejected as conflicts, and booking retries corrupt capacity and
operation history. Investigate the service and fix every defect according to
the recurrence, time-zone, override, capacity, and exactly-once contracts in
`README.md`.

Preserve the public API and documented exception behavior. Do not modify the
README, project configuration, or tests. Run the complete public suite before
finishing.
