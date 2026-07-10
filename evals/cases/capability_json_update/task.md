Update `app_config.json` with these exact changes while keeping it valid JSON:

- Set `retries.max_attempts` to `5`
- Set `feature_flags.beta_dashboard` to `true`
- Add `audit.owner` with value `platform-eval`

Do not remove the existing service name, region, or timeout settings.
