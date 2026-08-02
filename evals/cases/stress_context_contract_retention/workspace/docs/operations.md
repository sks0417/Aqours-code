# Operational boundaries

The recipient quota is checked after idempotency lookup and before any provider attempt. Each new logical request consumes one unit, regardless of how many channels are attempted. A request rejected by quota raises `RateLimitExceeded`, calls no provider, and creates no idempotency record. It is not a provider failure and cannot be bypassed through fallbacks.

Disabled channels are an application policy, not a provider condition. They are skipped silently and do not appear in `DeliveryFailed.attempted_channels`. If no eligible configured channel exists, `DeliveryFailed.attempted_channels` is empty.

Provider implementations used by this package expose a synchronous `deliver` method. The included `ScriptedProvider` is a deterministic test double: each outcome is either a provider id string, an exception instance to raise, or a callable accepting the validated request.

Validation happens before quota checks. Booleans are not accepted where integers are expected. Application configuration requires a positive integer quota and a mapping whose normalized channel names are unique.

Repositories and application objects are process-local and intentionally in-memory. Thread safety and persistence are out of scope, but atomic ordering within one `send` call is required.
