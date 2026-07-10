Fix the authentication vulnerability in this mini auth service.

The current service incorrectly allows an empty password to authenticate an existing user. Update the implementation under `src/` so this bypass is impossible while preserving normal successful login behavior.

Do not modify anything under `tests/`. Run the tests before you finish.
