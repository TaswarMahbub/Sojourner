# Tests

This directory is for checks that cross module boundaries. Each area will keep unit tests beside its own code when that code is added.

The first cross-component checks should cover schema 1 in `docs/contracts.md`:

- A camera feature message is actionable only in the `tracking` state with a valid pose and the full blendshape set.
- `neutral` does not request motion. `unknown`, `lost`, and `partial` do not request motion.
- A motor command outside its ranges, with a stale sequence, or past `expires_at_ms` is rejected.
- `stop` true, a non-moving task state, `estop`, and the watchdog all end in zero linear and angular motion.

No test runner, fixture, or sample result is included in this revision. Do not add fixtures that look like measured model output.
